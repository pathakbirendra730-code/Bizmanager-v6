"""
modules/saas_business/billing.py — SaaS-Native Billing / POS / Invoicing
============================================================================
Tenant-scoped GST invoice management for the SaaS multi-tenant system.
Mirrors legacy modules/billing.py feature-for-feature, but every query
is scoped by business_id and reads/writes saas_invoices / saas_invoice_items
/ saas_payments / saas_products / saas_ledger / saas_cash_book.

Reuses the existing pure-function GST engine (utils.helpers.calculate_gst,
determine_supply_type) unchanged — that logic has no DB dependency and is
identical regardless of which tenant system owns the invoice.

Permissions:
  view_invoice    → staff and above
  create_invoice  → manager and above
  edit_invoice    → manager and above (used for payment recording)
  delete_invoice  → owner only (used for cancellation)
"""

from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import saas_business_required, validate_csrf, audit_log
from utils.saas_middleware import permission_required, get_tenant_id, assert_tenant_access
from utils.tax_helpers import calculate_gst, determine_supply_type

saas_billing_bp = Blueprint("saas_billing", __name__, url_prefix="/biz/billing")

P = lambda: "%s" if _is_postgres() else "?"


def _invoice_status(total: float, paid: float) -> str:
    if paid <= 0:     return "unpaid"
    if paid >= total: return "paid"
    return "partial"


def _generate_invoice_number(biz_id: int) -> str:
    """
    Auto-generate next sequential invoice number for this business.
    Format: INV-<4-digit-seq>, scoped per business (each business starts
    its own sequence at 1001, independent of every other tenant).
    """
    p = P()
    last = saas_fetchone(
        f"SELECT invoice_number FROM saas_invoices WHERE business_id={p} "
        f"ORDER BY id DESC LIMIT 1",
        (biz_id,)
    )
    if last:
        parts = last["invoice_number"].split("-")
        try:
            seq = int(parts[-1]) + 1
        except ValueError:
            seq = 1001
    else:
        seq = 1001
    return f"INV-{seq}"


# ════════════════════════════════ POS ═════════════════════════════════════════

@saas_billing_bp.route("/pos")
@saas_business_required
@permission_required("create_invoice")
def pos():
    biz_id = get_tenant_id()
    p = P()

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    customers = saas_fetchall(
        f"SELECT id, name, phone, state_code, gstin FROM saas_customers "
        f"WHERE business_id={p} ORDER BY name",
        (biz_id,)
    )
    inv_number = _generate_invoice_number(biz_id)

    return render_template("saas_business/billing/pos.html",
                           biz=biz, customers=customers, inv_number=inv_number)


# ════════════════════════════════ SAVE INVOICE (AJAX) ═════════════════════════

@saas_billing_bp.route("/save", methods=["POST"])
@saas_business_required
@permission_required("create_invoice")
def save_invoice():
    biz_id = get_tenant_id()
    p = P()

    biz  = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "No data received"}), 400

    items         = data.get("items", [])
    customer_id   = data.get("customer_id") or None
    customer_name = data.get("customer_name", "Walk-in Customer")
    cust_gstin    = data.get("customer_gstin", "")
    cust_state    = data.get("customer_state", "")
    disc_pct      = float(data.get("discount_pct", 0) or 0)
    payment       = data.get("payment_method", "Cash")
    notes         = data.get("notes", "")
    # NOTE: paid_amount=0 is a legitimate, meaningful value (a fully-credit
    # sale) and must NOT be coerced to the "use default" sentinel. The
    # previous `data.get("paid_amount", -1) or -1` pattern silently turned
    # any genuine 0 into -1 (since 0 is falsy in Python), which made every
    # credit-sale attempt post as if it had been paid in full. Checking
    # explicitly for None/missing avoids this.
    _raw_paid     = data.get("paid_amount", None)
    paid_amount   = -1.0 if _raw_paid is None else float(_raw_paid)

    if not items:
        return jsonify({"success": False, "message": "Cart is empty"}), 400

    # Tenant-scoped customer ownership check — if a customer_id was supplied,
    # make sure it actually belongs to this business before trusting it.
    if customer_id:
        owned = saas_fetchone(
            f"SELECT id FROM saas_customers WHERE id={p} AND business_id={p}",
            (customer_id, biz_id)
        )
        if not owned:
            return jsonify({"success": False, "message": "Invalid customer."}), 400

    supply_type = determine_supply_type(biz.get("state_code", ""), cust_state)

    try:
        # Validate stock — tenant-scoped product lookups
        for item in items:
            prod = saas_fetchone(
                f"SELECT name, stock_quantity FROM saas_products WHERE id={p} AND business_id={p}",
                (item["product_id"], biz_id)
            )
            if prod and prod["stock_quantity"] < item["quantity"]:
                return jsonify({"success": False,
                    "message": f"Insufficient stock for '{prod['name']}'. "
                               f"Available: {prod['stock_quantity']}"}), 400

        # Compute totals using the shared GST engine
        subtotal = 0
        item_calcs = []
        for item in items:
            gst_r = float(item.get("gst_rate", 18))
            g = calculate_gst(float(item["unit_price"]), float(item["quantity"]),
                              gst_r, supply_type)
            subtotal += g["taxable"]
            item_calcs.append((item, g))

        disc_amt    = round(subtotal * disc_pct / 100, 2)
        taxable_tot = round(subtotal - disc_amt, 2)
        scale       = taxable_tot / subtotal if subtotal else 1

        cgst_tot = sgst_tot = igst_tot = 0
        for _, g in item_calcs:
            cgst_tot += g["cgst_amount"] * scale
            sgst_tot += g["sgst_amount"] * scale
            igst_tot += g["igst_amount"] * scale
        cgst_tot  = round(cgst_tot, 2)
        sgst_tot  = round(sgst_tot, 2)
        igst_tot  = round(igst_tot, 2)
        total_tax = round(cgst_tot + sgst_tot + igst_tot, 2)
        grand_tot = round(taxable_tot + total_tax, 2)

        if paid_amount < 0:
            paid_amount = grand_tot
        paid_amount = round(min(paid_amount, grand_tot), 2)
        due_amount  = round(grand_tot - paid_amount, 2)
        status      = _invoice_status(grand_tot, paid_amount)

        inv_number = _generate_invoice_number(biz_id)
        user_id    = session.get("saas_user_id")
        today      = datetime.utcnow().date().isoformat()

        inv_id = saas_execute(
            f"""INSERT INTO saas_invoices
                (business_id, invoice_number, customer_id, customer_name,
                 customer_gstin, customer_state, supply_type,
                 subtotal, discount, discount_pct, taxable_amount,
                 cgst_amount, sgst_amount, igst_amount, total_tax, total,
                 paid_amount, due_amount, payment_method, place_of_supply,
                 notes, status, created_by)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},
                        {p},{p},{p},{p},{p},{p},{p})""",
            (biz_id, inv_number, customer_id, customer_name,
             cust_gstin, cust_state, supply_type,
             subtotal, disc_amt, disc_pct, taxable_tot,
             cgst_tot, sgst_tot, igst_tot, total_tax, grand_tot,
             paid_amount, due_amount, payment,
             cust_state or biz.get("state_code", ""), notes, status, user_id)
        )

        # Invoice line items + tenant-scoped stock decrement
        for item, g in item_calcs:
            saas_execute(
                f"""INSERT INTO saas_invoice_items
                    (invoice_id, business_id, product_id, product_name, hsn_code,
                     quantity, unit_price, taxable_amount, gst_rate,
                     cgst_rate, sgst_rate, igst_rate,
                     cgst_amount, sgst_amount, igst_amount, total_price)
                    VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
                (inv_id, biz_id, item["product_id"], item["product_name"],
                 item.get("hsn_code", ""), item["quantity"], item["unit_price"],
                 g["taxable"] * scale, g["gst_rate"],
                 g["cgst_rate"], g["sgst_rate"], g["igst_rate"],
                 g["cgst_amount"] * scale, g["sgst_amount"] * scale,
                 g["igst_amount"] * scale, (g["taxable"] + g["total_tax"]) * scale)
            )
            saas_execute(
                f"""UPDATE saas_products
                    SET stock_quantity = CASE WHEN stock_quantity - {p} < 0 THEN 0
                                              ELSE stock_quantity - {p} END,
                        updated_at = {p}
                    WHERE id={p} AND business_id={p}""",
                (item["quantity"], item["quantity"], datetime.utcnow().isoformat(),
                 item["product_id"], biz_id)
            )

        # ── Accounting entries (double-entry engine) ─────────────────────────
        # Replaces the old direct writes to saas_cash_book / saas_ledger.
        # record_sale() decides internally whether this is a pure-cash sale,
        # pure-credit sale, or a partial payment at time of sale, and posts
        # the correct balanced journal entry/entries either way.
        from utils.ledger_transactions import record_sale
        record_sale(
            biz_id, taxable_tot, paid_amount=paid_amount,
            customer_id=customer_id, customer_name=customer_name,
            payment_method=payment, cgst=cgst_tot, sgst=sgst_tot, igst=igst_tot,
            source_id=inv_id, narration=f"Invoice {inv_number}", created_by=user_id
        )

        if paid_amount > 0:
            saas_execute(
                f"""INSERT INTO saas_payments
                    (business_id, invoice_id, invoice_number, customer_id,
                     customer_name, amount, payment_method, payment_date,
                     notes, created_by)
                    VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
                (biz_id, inv_id, inv_number, customer_id, customer_name,
                 paid_amount, payment, today, "Initial payment at billing", user_id)
            )

        audit_log("invoice_created", user_id=user_id, business_id=biz_id,
                  entity_type="invoice", entity_id=str(inv_id),
                  detail=f"number={inv_number} total={grand_tot}")

        return jsonify({"success": True, "invoice_id": inv_id,
                        "invoice_number": inv_number, "total": grand_tot,
                        "paid": paid_amount, "due": due_amount, "status": status})

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ════════════════════════════════ ADD PAYMENT ═════════════════════════════════

@saas_billing_bp.route("/payment/<int:inv_id>", methods=["POST"])
@saas_business_required
@permission_required("edit_invoice")
def add_payment(inv_id):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_billing.view_invoice", inv_id=inv_id))

    biz_id = get_tenant_id()
    p = P()

    amount = float(request.form.get("amount", 0) or 0)
    method = request.form.get("payment_method", "Cash")
    ref    = request.form.get("reference", "")
    notes  = request.form.get("notes", "")

    if amount <= 0:
        flash("Enter a valid payment amount.", "danger")
        return redirect(url_for("saas_billing.view_invoice", inv_id=inv_id))

    inv = saas_fetchone(
        f"SELECT * FROM saas_invoices WHERE id={p} AND business_id={p}", (inv_id, biz_id)
    )
    if not inv or inv["status"] == "cancelled":
        flash("Invoice not found or cancelled.", "danger")
        return redirect(url_for("saas_billing.history"))

    assert_tenant_access(inv["business_id"])

    amount = round(min(amount, inv["due_amount"]), 2)
    if amount <= 0:
        flash("No amount due on this invoice.", "info")
        return redirect(url_for("saas_billing.view_invoice", inv_id=inv_id))

    new_paid   = round(inv["paid_amount"] + amount, 2)
    new_due    = round(inv["total"] - new_paid, 2)
    new_status = _invoice_status(inv["total"], new_paid)
    user_id    = session.get("saas_user_id")
    today      = datetime.utcnow().date().isoformat()

    saas_execute(
        f"UPDATE saas_invoices SET paid_amount={p}, due_amount={p}, status={p} "
        f"WHERE id={p} AND business_id={p}",
        (new_paid, new_due, new_status, inv_id, biz_id)
    )

    saas_execute(
        f"""INSERT INTO saas_payments
            (business_id, invoice_id, invoice_number, customer_id, customer_name,
             amount, payment_method, payment_date, reference, notes, created_by)
            VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
        (biz_id, inv_id, inv["invoice_number"], inv["customer_id"], inv["customer_name"],
         amount, method, today, ref, notes, user_id)
    )

    # Double-entry posting: increase Cash/Bank, reduce Customer Due.
    # Only applicable when the invoice has a linked customer — walk-in
    # cash sales never carry a receivable in the first place, so there's
    # nothing here to settle (add_payment is only reachable for invoices
    # with status unpaid/partial, which always have a customer_id since
    # record_sale() requires one for any non-fully-paid sale).
    if inv["customer_id"]:
        from utils.ledger_transactions import record_payment_from_customer
        record_payment_from_customer(
            biz_id, amount, customer_id=inv["customer_id"], customer_name=inv["customer_name"],
            payment_method=method, source_id=inv_id,
            narration=f"Payment for {inv['invoice_number']}", created_by=user_id
        )

    audit_log("invoice_payment_recorded", user_id=user_id, business_id=biz_id,
              entity_type="invoice", entity_id=str(inv_id),
              detail=f"amount={amount} method={method}")

    flash(f"₹{amount:,.2f} payment recorded. Status: {new_status.upper()}", "success")
    return redirect(url_for("saas_billing.view_invoice", inv_id=inv_id))


# ════════════════════════════════ HISTORY ═════════════════════════════════════

@saas_billing_bp.route("/history")
@saas_business_required
@permission_required("view_invoice")
def history():
    biz_id    = get_tenant_id()
    search    = request.args.get("q", "")
    date_from = request.args.get("from", "")
    date_to   = request.args.get("to", "")
    status_f  = request.args.get("status", "")
    p = P()

    sql  = f"SELECT * FROM saas_invoices WHERE business_id={p}"
    args = [biz_id]
    if search:
        sql += f" AND (invoice_number LIKE {p} OR customer_name LIKE {p})"
        args += [f"%{search}%", f"%{search}%"]
    if date_from:
        sql += f" AND DATE(created_at) >= {p}"
        args.append(date_from)
    if date_to:
        sql += f" AND DATE(created_at) <= {p}"
        args.append(date_to)
    if status_f:
        sql += f" AND status={p}"
        args.append(status_f)
    sql += " ORDER BY created_at DESC"

    invoices = saas_fetchall(sql, tuple(args))
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/billing/history.html",
                           invoices=invoices, biz=biz, search=search,
                           date_from=date_from, date_to=date_to, status_f=status_f)


# ════════════════════════════════ VIEW / PRINT ════════════════════════════════

@saas_billing_bp.route("/invoice/<int:inv_id>")
@saas_business_required
@permission_required("view_invoice")
def view_invoice(inv_id):
    biz_id = get_tenant_id()
    p = P()

    invoice = saas_fetchone(
        f"SELECT * FROM saas_invoices WHERE id={p} AND business_id={p}", (inv_id, biz_id)
    )
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("saas_billing.history"))

    assert_tenant_access(invoice["business_id"])

    items = saas_fetchall(
        f"SELECT * FROM saas_invoice_items WHERE invoice_id={p} AND business_id={p}",
        (inv_id, biz_id)
    )
    payments = saas_fetchall(
        f"SELECT * FROM saas_payments WHERE invoice_id={p} AND business_id={p} ORDER BY created_at",
        (inv_id, biz_id)
    )
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/billing/invoice.html",
                           invoice=invoice, items=items, payments=payments, biz=biz)


# ════════════════════════════════ CANCEL ══════════════════════════════════════

@saas_billing_bp.route("/cancel/<int:inv_id>", methods=["POST"])
@saas_business_required
@permission_required("delete_invoice")
def cancel_invoice(inv_id):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_billing.history"))

    biz_id = get_tenant_id()
    p = P()

    invoice = saas_fetchone(
        f"SELECT * FROM saas_invoices WHERE id={p} AND business_id={p} AND status!='cancelled'",
        (inv_id, biz_id)
    )
    if not invoice:
        flash("Invoice not found or already cancelled.", "warning")
        return redirect(url_for("saas_billing.history"))

    assert_tenant_access(invoice["business_id"])

    items = saas_fetchall(
        f"SELECT product_id, quantity FROM saas_invoice_items WHERE invoice_id={p} AND business_id={p}",
        (inv_id, biz_id)
    )
    for item in items:
        if item["product_id"]:
            saas_execute(
                f"UPDATE saas_products SET stock_quantity = stock_quantity + {p} "
                f"WHERE id={p} AND business_id={p}",
                (item["quantity"], item["product_id"], biz_id)
            )

    # Reverse every posted journal entry that referenced this invoice
    # (the original sale entry, and any payment entries recorded against
    # it afterwards). Each is reversed individually via reverse_entry()
    # rather than deleted, preserving a full audit trail of exactly what
    # was posted and that it was later reversed on cancellation.
    from utils.ledger_service import reverse_entry
    user_id = session.get("saas_user_id")
    linked_entries = saas_fetchall(
        f"""SELECT id FROM saas_journal_entries
            WHERE business_id={p} AND source_id={p}
              AND source_type IN ('cash_sale','credit_sale','payment_in')
              AND status='posted'""",
        (biz_id, inv_id)
    )
    for entry in linked_entries:
        reverse_entry(biz_id, entry["id"], reason=f"Invoice {invoice['invoice_number']} cancelled",
                      created_by=user_id)

    saas_execute(
        f"UPDATE saas_invoices SET status='cancelled' WHERE id={p} AND business_id={p}",
        (inv_id, biz_id)
    )

    audit_log("invoice_cancelled", business_id=biz_id,
              entity_type="invoice", entity_id=str(inv_id),
              detail=f"number={invoice['invoice_number']}")

    flash(f"Invoice {invoice['invoice_number']} cancelled. Stock restored.", "success")
    return redirect(url_for("saas_billing.history"))


# ════════════════════════════════ RECEIVABLES ═════════════════════════════════

@saas_billing_bp.route("/receivables")
@saas_business_required
@permission_required("view_invoice")
def receivables():
    biz_id = get_tenant_id()
    p = P()

    rows = saas_fetchall(
        f"""SELECT id, invoice_number, customer_name, customer_id,
                   total, paid_amount, due_amount, status, payment_method,
                   created_at
            FROM saas_invoices
            WHERE business_id={p} AND status IN ('unpaid','partial')
            ORDER BY created_at ASC""",
        (biz_id,)
    )
    for r in rows:
        r["date"] = r["created_at"][:10] if r.get("created_at") else ""

    total_due = saas_fetchone(
        f"""SELECT COALESCE(SUM(due_amount),0) as t FROM saas_invoices
            WHERE business_id={p} AND status IN ('unpaid','partial')""",
        (biz_id,)
    )["t"]

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/billing/receivables.html",
                           rows=rows, total_due=total_due, biz=biz)


# ════════════════════════════════ APIS ════════════════════════════════════════

@saas_billing_bp.route("/api/products")
@saas_business_required
@permission_required("create_invoice")
def api_products():
    biz_id = get_tenant_id()
    q = request.args.get("q", "").strip()
    p = P()

    rows = saas_fetchall(
        f"""SELECT id, name, sku, hsn_code, gst_rate, selling_price, stock_quantity, barcode
            FROM saas_products
            WHERE business_id={p} AND is_active=1 AND stock_quantity>0
              AND (name LIKE {p} OR sku LIKE {p} OR barcode LIKE {p} OR hsn_code LIKE {p})
            ORDER BY name LIMIT 12""",
        (biz_id, f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%")
    )
    return jsonify(rows)


@saas_billing_bp.route("/api/barcode/<string:code>")
@saas_business_required
@permission_required("create_invoice")
def api_barcode(code):
    biz_id = get_tenant_id()
    p = P()

    row = saas_fetchone(
        f"""SELECT id, name, sku, hsn_code, gst_rate, selling_price, stock_quantity
            FROM saas_products
            WHERE business_id={p} AND (barcode={p} OR sku={p}) AND is_active=1""",
        (biz_id, code, code)
    )
    return jsonify({"found": bool(row), "product": row or {}})
