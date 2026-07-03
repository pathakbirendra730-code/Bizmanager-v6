"""
modules/saas_business/customers.py — SaaS-Native Customer Management
========================================================================
Tenant-scoped customer CRUD for the SaaS multi-tenant system.
Mirrors the legacy modules/customers.py feature set, but every query
is scoped by business_id (from the SaaS session) instead of shop_id,
and reads/writes saas_customers / saas_invoices instead of the legacy
customers / invoices tables.

Permissions (via utils.saas_middleware):
  view_customers    → staff and above (everyone can view)
  manage_customers  → manager and above (add/edit/delete)
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import saas_business_required, validate_csrf, audit_log
from utils.saas_middleware import permission_required, get_tenant_id, assert_tenant_access
from config import ActiveConfig

saas_customers_bp = Blueprint("saas_customers", __name__, url_prefix="/biz/customers")

P = lambda: "%s" if _is_postgres() else "?"


# ════════════════════════════════ LIST ════════════════════════════════════════

@saas_customers_bp.route("/")
@saas_business_required
@permission_required("view_customers")
def index():
    biz_id = get_tenant_id()
    q = request.args.get("q", "").strip()
    p = P()

    sql = f"""SELECT c.*,
                     COUNT(i.id) as inv_cnt,
                     COALESCE(SUM(CASE WHEN i.status='paid' THEN i.total ELSE 0 END), 0) as total_spent
              FROM saas_customers c
              LEFT JOIN saas_invoices i ON i.customer_id = c.id AND i.business_id = {p}
              WHERE c.business_id = {p}"""
    args = [biz_id, biz_id]

    if q:
        sql += f" AND (c.name LIKE {p} OR c.phone LIKE {p} OR c.email LIKE {p} OR c.gstin LIKE {p})"
        args += [f"%{q}%"] * 4

    sql += " GROUP BY c.id ORDER BY c.name"

    customers = saas_fetchall(sql, tuple(args))

    return render_template("saas_business/customers/list.html",
                           customers=customers, q=q)


# ════════════════════════════════ ADD ═════════════════════════════════════════

@saas_customers_bp.route("/add", methods=["GET", "POST"])
@saas_business_required
@permission_required("manage_customers")
def add():
    biz_id = get_tenant_id()
    states = ActiveConfig.INDIAN_STATES
    p = P()

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_customers.add"))

        d = _form()
        if not d["name"]:
            flash("Customer name is required.", "danger")
            return render_template("saas_business/customers/add_edit.html",
                                   customer=d, action="Add", states=states)

        cust_id = saas_execute(
            f"""INSERT INTO saas_customers
                (business_id, name, phone, email, address, state_code, gstin)
                VALUES ({p},{p},{p},{p},{p},{p},{p})""",
            (biz_id, d["name"], d["phone"], d["email"],
             d["address"], d["state_code"], d["gstin"])
        )
        audit_log("customer_created", business_id=biz_id,
                  entity_type="customer", entity_id=str(cust_id),
                  detail=f"name={d['name']}")
        flash(f"Customer '{d['name']}' added.", "success")
        return redirect(url_for("saas_customers.index"))

    return render_template("saas_business/customers/add_edit.html",
                           customer={}, action="Add", states=states)


# ════════════════════════════════ EDIT ════════════════════════════════════════

@saas_customers_bp.route("/edit/<int:cid>", methods=["GET", "POST"])
@saas_business_required
@permission_required("manage_customers")
def edit(cid):
    biz_id = get_tenant_id()
    states = ActiveConfig.INDIAN_STATES
    p = P()

    customer = saas_fetchone(
        f"SELECT * FROM saas_customers WHERE id={p} AND business_id={p}",
        (cid, biz_id)
    )
    if not customer:
        flash("Customer not found.", "danger")
        return redirect(url_for("saas_customers.index"))

    assert_tenant_access(customer["business_id"])

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_customers.edit", cid=cid))

        d = _form()
        if not d["name"]:
            flash("Customer name is required.", "danger")
            return render_template("saas_business/customers/add_edit.html",
                                   customer=customer, action="Edit", states=states)

        saas_execute(
            f"""UPDATE saas_customers SET
                name={p}, phone={p}, email={p}, address={p}, state_code={p}, gstin={p}
                WHERE id={p} AND business_id={p}""",
            (d["name"], d["phone"], d["email"], d["address"],
             d["state_code"], d["gstin"], cid, biz_id)
        )
        audit_log("customer_updated", business_id=biz_id,
                  entity_type="customer", entity_id=str(cid))
        flash("Customer updated.", "success")
        return redirect(url_for("saas_customers.index"))

    return render_template("saas_business/customers/add_edit.html",
                           customer=customer, action="Edit", states=states)


# ════════════════════════════════ DELETE ══════════════════════════════════════

@saas_customers_bp.route("/delete/<int:cid>", methods=["POST"])
@saas_business_required
@permission_required("manage_customers")
def delete(cid):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_customers.index"))

    biz_id = get_tenant_id()
    p = P()

    customer = saas_fetchone(
        f"SELECT * FROM saas_customers WHERE id={p} AND business_id={p}",
        (cid, biz_id)
    )
    if not customer:
        flash("Customer not found.", "danger")
        return redirect(url_for("saas_customers.index"))

    saas_execute(
        f"DELETE FROM saas_customers WHERE id={p} AND business_id={p}",
        (cid, biz_id)
    )
    audit_log("customer_deleted", business_id=biz_id,
              entity_type="customer", entity_id=str(cid),
              detail=f"name={customer['name']}")
    flash("Customer deleted.", "success")
    return redirect(url_for("saas_customers.index"))


# ════════════════════════════════ HISTORY ═════════════════════════════════════

@saas_customers_bp.route("/<int:cid>/history")
@saas_business_required
@permission_required("view_customers")
def history(cid):
    biz_id = get_tenant_id()
    p = P()

    customer = saas_fetchone(
        f"SELECT * FROM saas_customers WHERE id={p} AND business_id={p}",
        (cid, biz_id)
    )
    if not customer:
        flash("Customer not found.", "danger")
        return redirect(url_for("saas_customers.index"))

    assert_tenant_access(customer["business_id"])

    invoices = saas_fetchall(
        f"""SELECT * FROM saas_invoices
            WHERE customer_id={p} AND business_id={p}
            ORDER BY created_at DESC""",
        (cid, biz_id)
    )

    stats_row = saas_fetchone(
        f"""SELECT COUNT(*) as cnt,
                   COALESCE(SUM(total), 0) as total,
                   COALESCE(AVG(total), 0) as avg
            FROM saas_invoices
            WHERE customer_id={p} AND business_id={p} AND status='paid'""",
        (cid, biz_id)
    )

    return render_template("saas_business/customers/history.html",
                           customer=customer,
                           invoices=invoices,
                           stats=stats_row or {"cnt": 0, "total": 0, "avg": 0})


# ════════════════════════════════ API SEARCH ══════════════════════════════════

@saas_customers_bp.route("/api/search")
@saas_business_required
@permission_required("view_customers")
def api_search():
    biz_id = get_tenant_id()
    q = request.args.get("q", "").strip()
    p = P()

    rows = saas_fetchall(
        f"""SELECT id, name, phone, email, state_code, gstin
            FROM saas_customers
            WHERE business_id={p} AND (name LIKE {p} OR phone LIKE {p})
            ORDER BY name LIMIT 8""",
        (biz_id, f"%{q}%", f"%{q}%")
    )
    return jsonify(rows)


# ════════════════════════════════ HELPERS ═════════════════════════════════════

def _form():
    f = request.form.get
    return {
        "name":       f("name", "").strip(),
        "phone":      f("phone", "").strip(),
        "email":      f("email", "").strip(),
        "address":    f("address", "").strip(),
        "state_code": f("state_code", "").strip(),
        "gstin":      f("gstin", "").strip(),
    }
