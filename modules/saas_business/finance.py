"""
modules/saas_business/finance.py — SaaS-Native Finance Dashboard
=====================================================================
Tenant-scoped financial reporting and expense tracking for the SaaS
multi-tenant system. Mirrors legacy modules/finance.py's reporting and
expense feature set, but every query is scoped by business_id and reads
from saas_invoices / saas_invoice_items / saas_products / saas_expenses.

Deliberately NOT ported from the legacy module:
  • finance.settings — superseded by saas_auth.business_settings (built
    in an earlier phase), which already covers business profile fields.
    GST-rate-per-shop and low-stock-threshold are now per-product
    (saas_products.gst_rate / low_stock_threshold), not global settings.
  • finance.backup — superadmin/infra concern, out of scope for a
    per-tenant business module.

CSV export is self-contained here (not delegated to a not-yet-built
Reports module) so Finance doesn't depend on a module built later.

Permissions:
  view_finance    → accountant and above
  manage_finance  → accountant and above
"""

import io
import csv
from datetime import datetime
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, Response)
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import saas_business_required, validate_csrf, audit_log
from utils.saas_middleware import permission_required, get_tenant_id
from utils.tax_helpers import today_str

saas_finance_bp = Blueprint("saas_finance", __name__, url_prefix="/biz/finance")

P = lambda: "%s" if _is_postgres() else "?"


# ════════════════════════════════ DASHBOARD ════════════════════════════════════

@saas_finance_bp.route("/")
@saas_business_required
@permission_required("view_finance")
def index():
    biz_id = get_tenant_id()
    p = P()
    today  = today_str()
    month  = datetime.now().strftime("%Y-%m")
    year   = datetime.now().strftime("%Y")

    t_sales = saas_fetchone(
        f"""SELECT COALESCE(SUM(total),0) as t, COALESCE(SUM(total_tax),0) as tax, COUNT(*) as cnt
            FROM saas_invoices
            WHERE business_id={p} AND DATE(created_at)={p} AND status IN ('paid','partial')""",
        (biz_id, today)
    )
    m_sales = saas_fetchone(
        f"""SELECT COALESCE(SUM(total),0) as t, COALESCE(SUM(total_tax),0) as tax, COUNT(*) as cnt
            FROM saas_invoices
            WHERE business_id={p} AND strftime('%Y-%m',created_at)={p} AND status IN ('paid','partial')"""
        if not _is_postgres() else
        f"""SELECT COALESCE(SUM(total),0) as t, COALESCE(SUM(total_tax),0) as tax, COUNT(*) as cnt
            FROM saas_invoices
            WHERE business_id={p} AND TO_CHAR(created_at,'YYYY-MM')={p} AND status IN ('paid','partial')""",
        (biz_id, month)
    )
    y_sales = saas_fetchone(
        f"""SELECT COALESCE(SUM(total),0) as t FROM saas_invoices
            WHERE business_id={p} AND strftime('%Y',created_at)={p} AND status IN ('paid','partial')"""
        if not _is_postgres() else
        f"""SELECT COALESCE(SUM(total),0) as t FROM saas_invoices
            WHERE business_id={p} AND TO_CHAR(created_at,'YYYY')={p} AND status IN ('paid','partial')""",
        (biz_id, year)
    )
    t_exp = saas_fetchone(
        f"SELECT COALESCE(SUM(amount),0) as t FROM saas_expenses WHERE business_id={p} AND expense_date={p}",
        (biz_id, today)
    )
    m_exp = saas_fetchone(
        f"""SELECT COALESCE(SUM(amount),0) as t FROM saas_expenses
            WHERE business_id={p} AND strftime('%Y-%m',expense_date)={p}"""
        if not _is_postgres() else
        f"""SELECT COALESCE(SUM(amount),0) as t FROM saas_expenses
            WHERE business_id={p} AND TO_CHAR(expense_date,'YYYY-MM')={p}""",
        (biz_id, month)
    )
    m_cogs = saas_fetchone(
        f"""SELECT COALESCE(SUM(ii.quantity * pr.cost_price), 0) as cogs
            FROM saas_invoice_items ii
            JOIN saas_invoices i ON i.id = ii.invoice_id
            JOIN saas_products pr ON pr.id = ii.product_id
            WHERE ii.business_id={p} AND strftime('%Y-%m', i.created_at)={p}
              AND i.status IN ('paid','partial')"""
        if not _is_postgres() else
        f"""SELECT COALESCE(SUM(ii.quantity * pr.cost_price), 0) as cogs
            FROM saas_invoice_items ii
            JOIN saas_invoices i ON i.id = ii.invoice_id
            JOIN saas_products pr ON pr.id = ii.product_id
            WHERE ii.business_id={p} AND TO_CHAR(i.created_at,'YYYY-MM')={p}
              AND i.status IN ('paid','partial')""",
        (biz_id, month)
    )

    daily = saas_fetchall(
        f"""SELECT DATE(created_at) as day, COALESCE(SUM(total),0) as sales,
                   COALESCE(SUM(total_tax),0) as tax, COUNT(*) as orders
            FROM saas_invoices
            WHERE business_id={p} AND created_at >= date('now','-30 days')
              AND status IN ('paid','partial')
            GROUP BY day ORDER BY day"""
        if not _is_postgres() else
        f"""SELECT DATE(created_at) as day, COALESCE(SUM(total),0) as sales,
                   COALESCE(SUM(total_tax),0) as tax, COUNT(*) as orders
            FROM saas_invoices
            WHERE business_id={p} AND created_at >= NOW() - INTERVAL '30 days'
              AND status IN ('paid','partial')
            GROUP BY day ORDER BY day""",
        (biz_id,)
    )

    exp_cat = saas_fetchall(
        f"""SELECT category, COALESCE(SUM(amount),0) as total
            FROM saas_expenses WHERE business_id={p} AND strftime('%Y-%m',expense_date)={p}
            GROUP BY category ORDER BY total DESC"""
        if not _is_postgres() else
        f"""SELECT category, COALESCE(SUM(amount),0) as total
            FROM saas_expenses WHERE business_id={p} AND TO_CHAR(expense_date,'YYYY-MM')={p}
            GROUP BY category ORDER BY total DESC""",
        (biz_id, month)
    )

    pay_split = saas_fetchall(
        f"""SELECT payment_method, COUNT(*) as cnt, COALESCE(SUM(total),0) as total
            FROM saas_invoices
            WHERE business_id={p} AND strftime('%Y-%m',created_at)={p} AND status IN ('paid','partial')
            GROUP BY payment_method"""
        if not _is_postgres() else
        f"""SELECT payment_method, COUNT(*) as cnt, COALESCE(SUM(total),0) as total
            FROM saas_invoices
            WHERE business_id={p} AND TO_CHAR(created_at,'YYYY-MM')={p} AND status IN ('paid','partial')
            GROUP BY payment_method""",
        (biz_id, month)
    )

    gross = round(m_sales["t"] - m_cogs["cogs"], 2)
    net   = round(gross - m_exp["t"], 2)

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/finance/index.html",
        biz=biz,
        today_sales=round(t_sales["t"], 2), today_orders=t_sales["cnt"],
        today_tax=round(t_sales["tax"], 2), today_exp=round(t_exp["t"], 2),
        month_sales=round(m_sales["t"], 2), month_orders=m_sales["cnt"],
        month_tax=round(m_sales["tax"], 2), month_exp=round(m_exp["t"], 2),
        month_cogs=round(m_cogs["cogs"], 2), year_sales=round(y_sales["t"], 2),
        gross_profit=gross, net_profit=net,
        daily_chart=daily, exp_by_cat=exp_cat, pay_split=pay_split)


# ════════════════════════════════ EXPENSES ═════════════════════════════════════

@saas_finance_bp.route("/expenses")
@saas_business_required
@permission_required("view_finance")
def expenses():
    biz_id    = get_tenant_id()
    date_from = request.args.get("from", "")
    date_to   = request.args.get("to", "")
    cat_f     = request.args.get("category", "")
    p = P()

    sql  = f"SELECT * FROM saas_expenses WHERE business_id={p}"
    args = [biz_id]
    if date_from:
        sql += f" AND expense_date >= {p}"
        args.append(date_from)
    if date_to:
        sql += f" AND expense_date <= {p}"
        args.append(date_to)
    if cat_f:
        sql += f" AND category = {p}"
        args.append(cat_f)
    sql += " ORDER BY expense_date DESC, id DESC"

    exps = saas_fetchall(sql, tuple(args))
    cats = [r["category"] for r in saas_fetchall(
        f"SELECT DISTINCT category FROM saas_expenses WHERE business_id={p} ORDER BY category",
        (biz_id,)
    )]
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/finance/expenses.html",
                           expenses=exps, biz=biz, categories=cats,
                           date_from=date_from, date_to=date_to, category=cat_f)


@saas_finance_bp.route("/expenses/add", methods=["POST"])
@saas_business_required
@permission_required("manage_finance")
def add_expense():
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_finance.expenses"))

    biz_id = get_tenant_id()
    cat    = request.form.get("category", "").strip()
    desc   = request.form.get("description", "").strip()
    try:
        amt = float(request.form.get("amount", 0) or 0)
    except ValueError:
        amt = 0
    date   = request.form.get("expense_date", today_str())
    p = P()

    if not cat or amt <= 0:
        flash("Category and a valid amount are required.", "danger")
        return redirect(url_for("saas_finance.expenses"))

    user_id = session.get("saas_user_id")
    exp_id = saas_execute(
        f"""INSERT INTO saas_expenses (business_id, category, description, amount, expense_date, created_by)
            VALUES ({p},{p},{p},{p},{p},{p})""",
        (biz_id, cat, desc, amt, date, user_id)
    )

    # Cash-flow visibility: record as a cash_book payment entry too, so
    # the expense shows up in the same running ledger view as sales/purchases.
    saas_execute(
        f"""INSERT INTO saas_cash_book
            (business_id, txn_date, txn_type, category, description, ref_type, ref_id, amount, created_by)
            VALUES ({p},{p},'payment',{p},{p},'expense',{p},{p},{p})""",
        (biz_id, date, cat, desc or cat, exp_id, amt, user_id)
    )

    audit_log("expense_created", user_id=user_id, business_id=biz_id,
              entity_type="expense", entity_id=str(exp_id),
              detail=f"category={cat} amount={amt}")
    flash("Expense recorded.", "success")
    return redirect(url_for("saas_finance.expenses"))


@saas_finance_bp.route("/expenses/delete/<int:eid>", methods=["POST"])
@saas_business_required
@permission_required("manage_finance")
def delete_expense(eid):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_finance.expenses"))

    biz_id = get_tenant_id()
    p = P()

    exp = saas_fetchone(
        f"SELECT * FROM saas_expenses WHERE id={p} AND business_id={p}", (eid, biz_id)
    )
    if not exp:
        flash("Expense not found.", "danger")
        return redirect(url_for("saas_finance.expenses"))

    saas_execute(
        f"DELETE FROM saas_expenses WHERE id={p} AND business_id={p}", (eid, biz_id)
    )
    audit_log("expense_deleted", business_id=biz_id,
              entity_type="expense", entity_id=str(eid),
              detail=f"category={exp['category']} amount={exp['amount']}")
    flash("Expense deleted.", "success")
    return redirect(url_for("saas_finance.expenses"))


# ════════════════════════════════ CSV EXPORT ═══════════════════════════════════

@saas_finance_bp.route("/expenses/export")
@saas_business_required
@permission_required("view_finance")
def export_expenses():
    biz_id    = get_tenant_id()
    date_from = request.args.get("from", "")
    date_to   = request.args.get("to", "")
    p = P()

    sql  = f"SELECT * FROM saas_expenses WHERE business_id={p}"
    args = [biz_id]
    if date_from:
        sql += f" AND expense_date >= {p}"
        args.append(date_from)
    if date_to:
        sql += f" AND expense_date <= {p}"
        args.append(date_to)
    sql += " ORDER BY expense_date DESC"

    rows = saas_fetchall(sql, tuple(args))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Category", "Description", "Amount"])
    for r in rows:
        writer.writerow([r["expense_date"], r["category"], r.get("description", ""), r["amount"]])

    audit_log("expenses_exported", business_id=biz_id, detail=f"rows={len(rows)}")

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=expenses_{today_str()}.csv"}
    )
