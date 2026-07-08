"""
modules/saas_business/purchase.py — SaaS-Native Purchase Management
========================================================================
Tenant-scoped purchase bill management for the SaaS multi-tenant system.
Mirrors legacy modules/purchase.py feature-for-feature, but every query
is scoped by business_id and reads/writes saas_purchases / saas_purchase_items
/ saas_products / saas_suppliers / saas_ledger / saas_cash_book.

Reuses the existing pure-function GST engine (utils.helpers.calculate_gst,
determine_supply_type) unchanged — identical math to Billing, just applied
to input tax (purchases) instead of output tax (sales).

Accounting convention (matches modules/saas_business/suppliers.py exactly):
  Only the NET due_amount is posted to saas_ledger as a single debit entry
  on save — not the gross total. This avoids double-counting against
  saas_suppliers.balance, which already tracks the running net obligation
  independently and is updated directly here on every purchase and payment.

Permissions:
  view_purchase    → manager and above (staff never sees purchase data)
  manage_purchase  → manager and above
"""

from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import saas_business_required, validate_csrf, audit_log
from utils.saas_middleware import permission_required, get_tenant_id, assert_tenant_access
from utils.tax_helpers import calculate_gst, determine_supply_type

saas_purchase_bp = Blueprint("saas_purchase", __name__, url_prefix="/biz/purchase")

P = lambda: "%s" if _is_postgres() else "?"


def _purchase_status(total: float, paid: float) -> str:
    if paid <= 0:     return "pending"
    if paid >= total: return "received"
    return "partial"


def _generate_purchase_number(biz_id: int) -> str:
    """Auto-generate next sequential purchase number, scoped per business."""
    p = P()
    last = saas_fetchone(
        f"SELECT purchase_number FROM saas_purchases WHERE business_id={p} "
        f"ORDER BY id DESC LIMIT 1",
        (biz_id,)
    )
    if last:
        parts = last["purchase_number"].split("-")
        try:
            seq = int(parts[-1]) + 1
        except ValueError:
            seq = 1001
    else:
        seq = 1001
    return f"PUR-{seq}"


# ════════════════════════════════ NEW PURCHASE FORM ════════════════════════════

@saas_purchase_bp.route("/new")
@saas_business_required
@permission_required("manage_purchase")
def new():
    biz_id = get_tenant_id()
    p = P()

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    suppliers = saas_fetchall(
        f"SELECT id, name, phone, gstin, state_code, balance FROM saas_suppliers "
        f"WHERE business_id={p} AND is_active=TRUE ORDER BY name",
        (biz_id,)
    )
    pur_number = _generate_purchase_number(biz_id)
    today = datetime.utcnow().date().isoformat()

    return render_template("saas_business/purchase/new.html",
                           biz=biz, suppliers=suppliers,
                           pur_number=pur_number, today=today)


# ════════════════════════════════ SAVE (AJAX) ══════════════════════════════════

@saas_purchase_bp.route("/save", methods=["POST"])
@saas_business_required
@permission_required("manage_purchase")
def save():
    biz_id = get_tenant_id()
    p = P()

    biz  = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "No data received"}), 400

    items         = data.get("items", [])
    supplier_id   = data.get("supplier_id") or None
    supplier_name = data.get("supplier_name", "Unknown Supplier")
    sup_gstin     = data.get("supplier_gstin", "")
    sup_state     = data.get("supplier_state", "")
    bill_number   = data.get("bill_number", "")
    bill_date     = data.get("bill_date") or datetime.utcnow().date().isoformat()
    disc_pct      = float(data.get("discount_pct", 0) or 0)
    payment       = data.get("payment_method", "Cash")
    paid_amount   = float(data.get("paid_amount", 0) or 0)
    notes         = data.get("notes", "")

    if not items:
        return jsonify({"success": False, "message": "No items added"}), 400

    # Tenant-scoped supplier ownership check
    if supplier_id:
        owned = saas_fetchone(
            f"SELECT id FROM saas_suppliers WHERE id={p} AND business_id={p}",
            (supplier_id, biz_id)
        )
        if not owned:
            return jsonify({"success": False, "message": "Invalid supplier."}), 400

    supply_type = determine_supply_type(biz.get("state_code", ""), sup_state)

    try:
        subtotal = 0
        item_calcs = []
        for item in items:
            gst_r = float(item.get("gst_rate", 0))
            g = calculate_gst(float(item["unit_price"]), float(item["quantity"]),
                              gst_r, supply_type)
            subtotal += g["taxable"]
            item_calcs.append((item, g))

        disc_amt = round(subtotal * disc_pct / 100, 2)
        taxable  = round(subtotal - disc_amt, 2)
        scale    = taxable / subtotal if subtotal else 1

        cgst_tot = sgst_tot = igst_tot = 0
        for _, g in item_calcs:
            cgst_tot += g["cgst_amount"] * scale
            sgst_tot += g["sgst_amount"] * scale
            igst_tot += g["igst_amount"] * scale
        cgst_tot  = round(cgst_tot, 2)
        sgst_tot  = round(sgst_tot, 2)
        igst_tot  = round(igst_tot, 2)
        total_tax = round(cgst_tot + sgst_tot + igst_tot, 2)
        total     = round(taxable + total_tax, 2)

        paid_amount = round(max(0, min(paid_amount, total)), 2)
        due         = round(total - paid_amount, 2)
        status      = _purchase_status(total, paid_amount)

        pur_number = _generate_purchase_number(biz_id)
        user_id    = session.get("saas_user_id")

        pur_id = saas_execute(
            f"""INSERT INTO saas_purchases
                (business_id, purchase_number, supplier_id, supplier_name,
                 supplier_gstin, bill_number, bill_date,
                 subtotal, discount, discount_pct, taxable_amount,
                 cgst_amount, sgst_amount, igst_amount, total_tax, total,
                 paid_amount, due_amount, payment_method, supply_type,
                 notes, status, created_by)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},
                        {p},{p},{p},{p},{p},{p},{p})""",
            (biz_id, pur_number, supplier_id, supplier_name,
             sup_gstin, bill_number, bill_date,
             subtotal, disc_amt, disc_pct, taxable,
             cgst_tot, sgst_tot, igst_tot, total_tax, total,
             paid_amount, due, payment, supply_type,
             notes, status, user_id)
        )

        # Items + tenant-scoped stock increment + cost price update
        for item, g in item_calcs:
            item_taxable = g["taxable"] * scale
            saas_execute(
                f"""INSERT INTO saas_purchase_items
                    (purchase_id, business_id, product_id, product_name, hsn_code,
                     quantity, unit_price, taxable_amount, gst_rate,
                     cgst_rate, sgst_rate, igst_rate,
                     cgst_amount, sgst_amount, igst_amount, total_price)
                    VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
                (pur_id, biz_id, item.get("product_id"), item["product_name"],
                 item.get("hsn_code", ""), item["quantity"], item["unit_price"],
                 item_taxable, g["gst_rate"], g["cgst_rate"], g["sgst_rate"], g["igst_rate"],
                 g["cgst_amount"] * scale, g["sgst_amount"] * scale, g["igst_amount"] * scale,
                 item_taxable + (g["cgst_amount"] + g["sgst_amount"] + g["igst_amount"]) * scale)
            )

            if item.get("product_id"):
                saas_execute(
                    f"""UPDATE saas_products
                        SET stock_quantity = stock_quantity + {p},
                            cost_price     = {p},
                            updated_at     = {p}
                        WHERE id={p} AND business_id={p}""",
                    (item["quantity"], item["unit_price"], datetime.utcnow().isoformat(),
                     item["product_id"], biz_id)
                )

        # ── Accounting entries (double-entry engine) ─────────────────────────
        # Replaces the old direct writes to saas_suppliers.balance,
        # saas_ledger, and saas_cash_book. record_purchase() decides
        # internally whether this is a pure-cash purchase, pure-credit
        # purchase, or a partial payment at time of purchase, and posts
        # the correct balanced journal entry/entries either way. The
        # supplier's saas_suppliers.balance is a UI convenience cache used
        # by the Suppliers module's own pages — keep updating it here so
        # those pages stay in sync, but the ledger itself (now the journal)
        # no longer double-counts against it the way the old code didn't
        # either.
        if supplier_id and due > 0:
            saas_execute(
                f"UPDATE saas_suppliers SET balance = balance + {p} WHERE id={p} AND business_id={p}",
                (due, supplier_id, biz_id)
            )

        from utils.ledger_transactions import record_purchase
        record_purchase(
            biz_id, taxable, paid_amount=paid_amount,
            supplier_id=supplier_id, supplier_name=supplier_name,
            payment_method=payment, cgst=cgst_tot, sgst=sgst_tot, igst=igst_tot,
            source_id=pur_id, narration=f"Purchase {pur_number}", created_by=user_id
        )

        audit_log("purchase_created", user_id=user_id, business_id=biz_id,
                  entity_type="purchase", entity_id=str(pur_id),
                  detail=f"number={pur_number} total={total}")

        return jsonify({"success": True, "purchase_id": pur_id,
                        "purchase_number": pur_number, "total": total})

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ════════════════════════════════ HISTORY ═════════════════════════════════════

@saas_purchase_bp.route("/history")
@saas_business_required
@permission_required("view_purchase")
def history():
    biz_id    = get_tenant_id()
    search    = request.args.get("q", "")
    date_from = request.args.get("from", "")
    date_to   = request.args.get("to", "")
    sup_f     = request.args.get("supplier", "")
    status_f  = request.args.get("status", "")
    p = P()

    sql  = f"SELECT * FROM saas_purchases WHERE business_id={p}"
    args = [biz_id]
    if search:
        sql += f" AND (purchase_number LIKE {p} OR supplier_name LIKE {p} OR bill_number LIKE {p})"
        args += [f"%{search}%"] * 3
    if date_from:
        sql += f" AND DATE(created_at) >= {p}"
        args.append(date_from)
    if date_to:
        sql += f" AND DATE(created_at) <= {p}"
        args.append(date_to)
    if sup_f:
        sql += f" AND supplier_id={p}"
        args.append(sup_f)
    if status_f:
        sql += f" AND status={p}"
        args.append(status_f)
    sql += " ORDER BY created_at DESC"

    purchases = saas_fetchall(sql, tuple(args))
    suppliers = saas_fetchall(
        f"SELECT id, name FROM saas_suppliers WHERE business_id={p} AND is_active=TRUE ORDER BY name",
        (biz_id,)
    )
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/purchase/history.html",
                           purchases=purchases, biz=biz, suppliers=suppliers,
                           search=search, date_from=date_from, date_to=date_to,
                           sup_f=sup_f, status_f=status_f)


# ════════════════════════════════ VIEW / PRINT ════════════════════════════════

@saas_purchase_bp.route("/view/<int:pid>")
@saas_business_required
@permission_required("view_purchase")
def view(pid):
    biz_id = get_tenant_id()
    p = P()

    purchase = saas_fetchone(
        f"SELECT * FROM saas_purchases WHERE id={p} AND business_id={p}", (pid, biz_id)
    )
    if not purchase:
        flash("Purchase not found.", "danger")
        return redirect(url_for("saas_purchase.history"))

    assert_tenant_access(purchase["business_id"])

    items = saas_fetchall(
        f"SELECT * FROM saas_purchase_items WHERE purchase_id={p} AND business_id={p}",
        (pid, biz_id)
    )
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/purchase/view.html",
                           purchase=purchase, items=items, biz=biz)


# ════════════════════════════════ CANCEL ══════════════════════════════════════

@saas_purchase_bp.route("/cancel/<int:pid>", methods=["POST"])
@saas_business_required
@permission_required("manage_purchase")
def cancel(pid):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_purchase.history"))

    biz_id = get_tenant_id()
    p = P()

    pur = saas_fetchone(
        f"SELECT * FROM saas_purchases WHERE id={p} AND business_id={p} AND status!='cancelled'",
        (pid, biz_id)
    )
    if not pur:
        flash("Purchase not found or already cancelled.", "warning")
        return redirect(url_for("saas_purchase.history"))

    assert_tenant_access(pur["business_id"])

    # Reverse stock (tenant-scoped, never goes below zero)
    items = saas_fetchall(
        f"SELECT product_id, quantity FROM saas_purchase_items WHERE purchase_id={p} AND business_id={p}",
        (pid, biz_id)
    )
    for item in items:
        if item["product_id"]:
            saas_execute(
                f"""UPDATE saas_products
                    SET stock_quantity = CASE WHEN stock_quantity - {p} < 0 THEN 0
                                              ELSE stock_quantity - {p} END,
                        updated_at = {p}
                    WHERE id={p} AND business_id={p}""",
                (item["quantity"], item["quantity"], datetime.utcnow().isoformat(),
                 item["product_id"], biz_id)
            )

    # Reverse supplier balance (never below zero)
    if pur["supplier_id"]:
        saas_execute(
            f"""UPDATE saas_suppliers
                SET balance = CASE WHEN (balance - {p}) < 0 THEN 0 ELSE balance - {p} END
                WHERE id={p} AND business_id={p}""",
            (pur["due_amount"], pur["due_amount"], pur["supplier_id"], biz_id)
        )

    # Reverse every posted journal entry that referenced this purchase
    # (the original purchase entry, and any payment entries recorded
    # against it afterwards). Reversed via reverse_entry(), never deleted
    # — preserves a full audit trail of exactly what was posted.
    from utils.ledger_service import reverse_entry
    user_id = session.get("saas_user_id")
    linked_entries = saas_fetchall(
        f"""SELECT id FROM saas_journal_entries
            WHERE business_id={p} AND source_id={p}
              AND source_type IN ('cash_purchase','credit_purchase','payment_out')
              AND status='posted'""",
        (biz_id, pid)
    )
    for entry in linked_entries:
        reverse_entry(biz_id, entry["id"], reason=f"Purchase {pur['purchase_number']} cancelled",
                      created_by=user_id)

    saas_execute(
        f"UPDATE saas_purchases SET status='cancelled' WHERE id={p} AND business_id={p}",
        (pid, biz_id)
    )

    audit_log("purchase_cancelled", business_id=biz_id,
              entity_type="purchase", entity_id=str(pid),
              detail=f"number={pur['purchase_number']}")

    flash(f"Purchase {pur['purchase_number']} cancelled. Stock reversed.", "success")
    return redirect(url_for("saas_purchase.history"))


# ════════════════════════════════ API: PRODUCT SEARCH ═════════════════════════

@saas_purchase_bp.route("/api/products")
@saas_business_required
@permission_required("manage_purchase")
def api_products():
    biz_id = get_tenant_id()
    q = request.args.get("q", "").strip()
    p = P()

    rows = saas_fetchall(
        f"""SELECT id, name, sku, hsn_code, gst_rate, cost_price, selling_price, stock_quantity
            FROM saas_products
            WHERE business_id={p} AND is_active=TRUE
              AND (name LIKE {p} OR sku LIKE {p} OR hsn_code LIKE {p})
            ORDER BY name LIMIT 12""",
        (biz_id, f"%{q}%", f"%{q}%", f"%{q}%")
    )
    return jsonify(rows)
