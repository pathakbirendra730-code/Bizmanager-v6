"""
modules/saas_business/products.py — SaaS-Native Inventory Management
========================================================================
Tenant-scoped product + category CRUD for the SaaS multi-tenant system.
Mirrors legacy modules/inventory.py, but every query is scoped by
business_id and reads/writes saas_products / saas_categories.

HSN master data is global reference data (not tenant-scoped), so the
HSN search/lookup endpoints here read the existing legacy hsn_master
table directly (read-only) — same underlying data the legacy GST module
uses, just exposed through a SaaS-session-aware route instead of one
gated by the legacy-only @login_required decorator.

Permissions:
  view_inventory    → staff and above
  manage_inventory  → manager and above
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import saas_business_required, validate_csrf, audit_log
from utils.saas_middleware import permission_required, get_tenant_id, assert_tenant_access
from config import ActiveConfig

saas_products_bp = Blueprint("saas_products", __name__, url_prefix="/biz/products")

P = lambda: "%s" if _is_postgres() else "?"


# ════════════════════════════════ LIST ════════════════════════════════════════

@saas_products_bp.route("/")
@saas_business_required
@permission_required("view_inventory")
def index():
    biz_id = get_tenant_id()
    search = request.args.get("q", "").strip()
    cat_f  = request.args.get("category", "")
    stk_f  = request.args.get("stock", "")
    gst_f  = request.args.get("gst", "")
    p = P()

    sql = f"""SELECT pr.*, c.name as cat_name
              FROM saas_products pr
              LEFT JOIN saas_categories c ON pr.category_id = c.id
              WHERE pr.business_id = {p} AND pr.is_active = 1"""
    args = [biz_id]

    if search:
        sql += f" AND (pr.name LIKE {p} OR pr.sku LIKE {p} OR pr.hsn_code LIKE {p})"
        args += [f"%{search}%"] * 3
    if cat_f:
        sql += f" AND pr.category_id = {p}"
        args.append(cat_f)
    if stk_f == "low":
        sql += " AND pr.stock_quantity <= pr.low_stock_threshold"
    elif stk_f == "out":
        sql += " AND pr.stock_quantity = 0"
    if gst_f:
        sql += f" AND pr.gst_rate = {p}"
        args.append(gst_f)
    sql += " ORDER BY pr.name"

    products   = saas_fetchall(sql, tuple(args))
    categories = saas_fetchall(
        f"SELECT * FROM saas_categories WHERE business_id={p} ORDER BY name", (biz_id,)
    )

    return render_template("saas_business/products/list.html",
                           products=products, categories=categories,
                           search=search, selected_category=cat_f,
                           stock_filter=stk_f, gst_filter=gst_f,
                           gst_slabs=ActiveConfig.GST_SLABS)


# ════════════════════════════════ ADD ═════════════════════════════════════════

@saas_products_bp.route("/add", methods=["GET", "POST"])
@saas_business_required
@permission_required("manage_inventory")
def add():
    biz_id = get_tenant_id()
    p = P()
    categories = saas_fetchall(
        f"SELECT * FROM saas_categories WHERE business_id={p} ORDER BY name", (biz_id,)
    )

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_products.add"))

        d = _form()
        if not d["name"]:
            flash("Product name is required.", "danger")
            return render_template("saas_business/products/add_edit.html",
                                   product=d, categories=categories, action="Add",
                                   gst_slabs=ActiveConfig.GST_SLABS)

        if d["sku"] and saas_fetchone(
            f"SELECT id FROM saas_products WHERE sku={p} AND business_id={p}",
            (d["sku"], biz_id)
        ):
            flash("SKU already exists for this business.", "danger")
            return render_template("saas_business/products/add_edit.html",
                                   product=d, categories=categories, action="Add",
                                   gst_slabs=ActiveConfig.GST_SLABS)

        prod_id = saas_execute(
            f"""INSERT INTO saas_products
                (business_id, name, sku, category_id, hsn_code, gst_rate, cost_price,
                 selling_price, stock_quantity, low_stock_threshold, barcode, description)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
            (biz_id, d["name"], d["sku"], d["category_id"], d["hsn_code"],
             d["gst_rate"], d["cost_price"], d["selling_price"],
             d["stock_quantity"], d["low_stock_threshold"], d["barcode"], d["description"])
        )
        audit_log("product_created", business_id=biz_id,
                  entity_type="product", entity_id=str(prod_id), detail=f"name={d['name']}")
        flash(f"Product '{d['name']}' added.", "success")
        return redirect(url_for("saas_products.index"))

    return render_template("saas_business/products/add_edit.html",
                           product={}, categories=categories, action="Add",
                           gst_slabs=ActiveConfig.GST_SLABS)


# ════════════════════════════════ EDIT ════════════════════════════════════════

@saas_products_bp.route("/edit/<int:pid>", methods=["GET", "POST"])
@saas_business_required
@permission_required("manage_inventory")
def edit(pid):
    biz_id = get_tenant_id()
    p = P()

    product = saas_fetchone(
        f"SELECT * FROM saas_products WHERE id={p} AND business_id={p}", (pid, biz_id)
    )
    if not product:
        flash("Product not found.", "danger")
        return redirect(url_for("saas_products.index"))

    assert_tenant_access(product["business_id"])

    categories = saas_fetchall(
        f"SELECT * FROM saas_categories WHERE business_id={p} ORDER BY name", (biz_id,)
    )

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_products.edit", pid=pid))

        d = _form()
        if not d["name"]:
            flash("Product name is required.", "danger")
            return render_template("saas_business/products/add_edit.html",
                                   product=product, categories=categories, action="Edit",
                                   gst_slabs=ActiveConfig.GST_SLABS)

        if d["sku"] and saas_fetchone(
            f"SELECT id FROM saas_products WHERE sku={p} AND business_id={p} AND id!={p}",
            (d["sku"], biz_id, pid)
        ):
            flash("SKU already taken.", "danger")
            return render_template("saas_business/products/add_edit.html",
                                   product=product, categories=categories, action="Edit",
                                   gst_slabs=ActiveConfig.GST_SLABS)

        from datetime import datetime
        saas_execute(
            f"""UPDATE saas_products SET
                name={p}, sku={p}, category_id={p}, hsn_code={p}, gst_rate={p},
                cost_price={p}, selling_price={p}, stock_quantity={p},
                low_stock_threshold={p}, barcode={p}, description={p}, updated_at={p}
                WHERE id={p} AND business_id={p}""",
            (d["name"], d["sku"], d["category_id"], d["hsn_code"], d["gst_rate"],
             d["cost_price"], d["selling_price"], d["stock_quantity"],
             d["low_stock_threshold"], d["barcode"], d["description"],
             datetime.utcnow().isoformat(), pid, biz_id)
        )
        audit_log("product_updated", business_id=biz_id,
                  entity_type="product", entity_id=str(pid))
        flash("Product updated.", "success")
        return redirect(url_for("saas_products.index"))

    return render_template("saas_business/products/add_edit.html",
                           product=product, categories=categories, action="Edit",
                           gst_slabs=ActiveConfig.GST_SLABS)


# ════════════════════════════════ DELETE (soft) ═══════════════════════════════

@saas_products_bp.route("/delete/<int:pid>", methods=["POST"])
@saas_business_required
@permission_required("manage_inventory")
def delete(pid):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_products.index"))

    biz_id = get_tenant_id()
    p = P()

    product = saas_fetchone(
        f"SELECT * FROM saas_products WHERE id={p} AND business_id={p}", (pid, biz_id)
    )
    if not product:
        flash("Product not found.", "danger")
        return redirect(url_for("saas_products.index"))

    saas_execute(
        f"UPDATE saas_products SET is_active=0 WHERE id={p} AND business_id={p}",
        (pid, biz_id)
    )
    audit_log("product_deleted", business_id=biz_id,
              entity_type="product", entity_id=str(pid), detail=f"name={product['name']}")
    flash("Product removed.", "success")
    return redirect(url_for("saas_products.index"))


# ════════════════════════════════ STOCK ADJUSTMENT ════════════════════════════

@saas_products_bp.route("/stock/<int:pid>", methods=["POST"])
@saas_business_required
@permission_required("manage_inventory")
def adjust_stock(pid):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_products.index"))

    biz_id = get_tenant_id()
    p = P()

    product = saas_fetchone(
        f"SELECT * FROM saas_products WHERE id={p} AND business_id={p}", (pid, biz_id)
    )
    if not product:
        flash("Product not found.", "danger")
        return redirect(url_for("saas_products.index"))

    action = request.form.get("action", "add")
    try:
        qty = int(request.form.get("quantity", 0) or 0)
    except ValueError:
        qty = 0

    from datetime import datetime
    now = datetime.utcnow().isoformat()

    if action == "add":
        saas_execute(
            f"UPDATE saas_products SET stock_quantity=stock_quantity+{p}, updated_at={p} "
            f"WHERE id={p} AND business_id={p}",
            (qty, now, pid, biz_id)
        )
    else:
        saas_execute(
            f"UPDATE saas_products SET stock_quantity={p}, updated_at={p} "
            f"WHERE id={p} AND business_id={p}",
            (qty, now, pid, biz_id)
        )

    audit_log("product_stock_adjusted", business_id=biz_id,
              entity_type="product", entity_id=str(pid),
              detail=f"action={action} qty={qty}")
    flash("Stock updated.", "success")
    return redirect(url_for("saas_products.index"))


# ════════════════════════════════ CATEGORIES ══════════════════════════════════

@saas_products_bp.route("/categories")
@saas_business_required
@permission_required("view_inventory")
def categories():
    biz_id = get_tenant_id()
    p = P()
    cats = saas_fetchall(
        f"""SELECT c.*, COUNT(pr.id) as cnt
            FROM saas_categories c
            LEFT JOIN saas_products pr ON pr.category_id = c.id AND pr.is_active = 1
            WHERE c.business_id = {p}
            GROUP BY c.id ORDER BY c.name""",
        (biz_id,)
    )
    return render_template("saas_business/categories/list.html", categories=cats)


@saas_products_bp.route("/categories/add", methods=["POST"])
@saas_business_required
@permission_required("manage_inventory")
def add_category():
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_products.categories"))

    biz_id = get_tenant_id()
    name = request.form.get("name", "").strip()
    p = P()

    if name:
        existing = saas_fetchone(
            f"SELECT id FROM saas_categories WHERE name={p} AND business_id={p}",
            (name, biz_id)
        )
        if not existing:
            cat_id = saas_execute(
                f"INSERT INTO saas_categories (business_id, name) VALUES ({p},{p})",
                (biz_id, name)
            )
            audit_log("category_created", business_id=biz_id,
                      entity_type="category", entity_id=str(cat_id), detail=f"name={name}")
        flash(f"Category '{name}' added.", "success")

    return redirect(url_for("saas_products.categories"))


@saas_products_bp.route("/categories/delete/<int:cid>", methods=["POST"])
@saas_business_required
@permission_required("manage_inventory")
def delete_category(cid):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_products.categories"))

    biz_id = get_tenant_id()
    p = P()

    cat = saas_fetchone(
        f"SELECT * FROM saas_categories WHERE id={p} AND business_id={p}", (cid, biz_id)
    )
    if not cat:
        flash("Category not found.", "danger")
        return redirect(url_for("saas_products.categories"))

    saas_execute(
        f"DELETE FROM saas_categories WHERE id={p} AND business_id={p}", (cid, biz_id)
    )
    audit_log("category_deleted", business_id=biz_id,
              entity_type="category", entity_id=str(cid), detail=f"name={cat['name']}")
    flash("Category deleted.", "success")
    return redirect(url_for("saas_products.categories"))


# ════════════════════════════════ API SEARCH ══════════════════════════════════

@saas_products_bp.route("/api/search")
@saas_business_required
@permission_required("view_inventory")
def api_search():
    biz_id = get_tenant_id()
    q = request.args.get("q", "").strip()
    p = P()

    rows = saas_fetchall(
        f"""SELECT id, name, sku, hsn_code, gst_rate, selling_price, stock_quantity, barcode
            FROM saas_products
            WHERE business_id={p} AND is_active=1 AND stock_quantity>0
              AND (name LIKE {p} OR sku LIKE {p} OR barcode LIKE {p})
            ORDER BY name LIMIT 12""",
        (biz_id, f"%{q}%", f"%{q}%", f"%{q}%")
    )
    return jsonify(rows)


# ════════════════════════════════ HSN LOOKUP (global, read-only) ══════════════

@saas_products_bp.route("/api/hsn")
@saas_business_required
def api_hsn():
    """HSN master is global reference data — same source as the legacy
    GST module, exposed here via a SaaS-session-aware route so the
    product form's HSN autocomplete works for SaaS users."""
    from models.saas_business_data import get_hsn_master
    q = request.args.get("q", "").strip()
    return jsonify(get_hsn_master(q)[:15])


@saas_products_bp.route("/api/hsn/<string:code>")
@saas_business_required
def api_hsn_code(code):
    from models.database import get_db
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM hsn_master WHERE hsn_code=?", (code,)).fetchone()
        return jsonify(dict(row) if row else {})
    finally:
        conn.close()


# ════════════════════════════════ HELPERS ═════════════════════════════════════

def _form():
    f = request.form.get
    try:
        gst_rate = float(f("gst_rate", 18) or 18)
    except ValueError:
        gst_rate = 18.0
    try:
        cost_price = float(f("cost_price", 0) or 0)
    except ValueError:
        cost_price = 0.0
    try:
        selling_price = float(f("selling_price", 0) or 0)
    except ValueError:
        selling_price = 0.0
    try:
        stock_quantity = int(f("stock_quantity", 0) or 0)
    except ValueError:
        stock_quantity = 0
    try:
        low_stock_threshold = int(f("low_stock_threshold", 5) or 5)
    except ValueError:
        low_stock_threshold = 5

    return {
        "name":                f("name", "").strip(),
        "sku":                 f("sku", "").strip(),
        "category_id":         f("category_id") or None,
        "hsn_code":            f("hsn_code", "").strip(),
        "gst_rate":            gst_rate,
        "cost_price":          cost_price,
        "selling_price":       selling_price,
        "stock_quantity":      stock_quantity,
        "low_stock_threshold": low_stock_threshold,
        "barcode":             f("barcode", "").strip(),
        "description":         f("description", "").strip(),
    }
