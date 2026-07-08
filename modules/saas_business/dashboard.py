"""
modules/saas_business/dashboard.py — SaaS-Native Dashboard
============================================================
Tenant-scoped business dashboard for the SaaS multi-tenant system.
Replaces the old bridge logic in modules/dashboard.py (saas_dashboard()),
which only rendered hardcoded stub stats. Every figure here is pulled
live from the real SaaS-native tables and the double-entry ledger.
"""

from flask import Blueprint, render_template, session, redirect, url_for, flash
from models.saas_auth import saas_fetchone, saas_fetchall, _is_postgres
from utils.saas_helpers import saas_business_required
from utils.saas_middleware import get_tenant_id
from utils.tax_helpers import today_str

saas_dashboard_bp = Blueprint("saas_dashboard", __name__, url_prefix="/biz/dashboard")

P = lambda: "%s" if _is_postgres() else "?"


@saas_dashboard_bp.route("/")
@saas_business_required
def index():
    user_id = session.get("saas_user_id")
    biz_id  = get_tenant_id()
    p = P()

    user = saas_fetchone(f"SELECT * FROM saas_users WHERE id={p}", (user_id,))
    biz  = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    if not user or not biz:
        from utils.saas_helpers import clear_saas_session
        clear_saas_session()
        flash("Your session is out of date. Please log in again.", "warning")
        return redirect(url_for("unified_login.login"))

    today = today_str()

    # ── Today's sales & receivables ─────────────────────────────────────────
    sales_today = saas_fetchone(
        f"""SELECT COUNT(*) as cnt, COALESCE(SUM(total), 0) as total,
                   COALESCE(SUM(due_amount), 0) as due
            FROM saas_invoices
            WHERE business_id={p} AND DATE(created_at)={p}""",
        (biz_id, today)
    )

    total_receivables = saas_fetchone(
        f"""SELECT COALESCE(SUM(due_amount), 0) as total
            FROM saas_invoices WHERE business_id={p} AND due_amount > 0""",
        (biz_id,)
    )

    total_payables = saas_fetchone(
        f"""SELECT COALESCE(SUM(due_amount), 0) as total
            FROM saas_purchases WHERE business_id={p} AND due_amount > 0""",
        (biz_id,)
    )

    # ── Inventory ────────────────────────────────────────────────────────────
    product_count = saas_fetchone(
        f"SELECT COUNT(*) as cnt FROM saas_products WHERE business_id={p} AND is_active=TRUE",
        (biz_id,)
    )

    low_stock = saas_fetchall(
        f"""SELECT id, name, stock_quantity, low_stock_threshold
            FROM saas_products
            WHERE business_id={p} AND is_active=TRUE
              AND stock_quantity <= low_stock_threshold
            ORDER BY stock_quantity ASC LIMIT 10""",
        (biz_id,)
    )

    # ── Customers / Suppliers ───────────────────────────────────────────────
    customer_count = saas_fetchone(
        f"SELECT COUNT(*) as cnt FROM saas_customers WHERE business_id={p}",
        (biz_id,)
    )
    supplier_count = saas_fetchone(
        f"SELECT COUNT(*) as cnt FROM saas_suppliers WHERE business_id={p} AND is_active=TRUE",
        (biz_id,)
    )

    # ── Cash & Bank balances (from the double-entry ledger, not a guess) ────
    cash_balance = 0.0
    bank_balance = 0.0
    try:
        from utils.chart_of_accounts import get_account_by_subtype
        from utils.ledger_service import get_account_balance

        cash_acct = get_account_by_subtype(biz_id, "cash")
        bank_acct = get_account_by_subtype(biz_id, "bank")
        if cash_acct:
            cash_balance = get_account_balance(biz_id, cash_acct["id"])
        if bank_acct:
            bank_balance = get_account_balance(biz_id, bank_acct["id"])
    except Exception:
        # Chart of accounts not seeded yet for this business — show zeros
        # rather than failing the whole dashboard.
        pass

    stats = {
        "business_name":      biz.get("name", ""),
        "plan":                biz.get("plan", "free"),
        "sales_today_count":   sales_today["cnt"] if sales_today else 0,
        "sales_today_total":   sales_today["total"] if sales_today else 0,
        "sales_today_due":     sales_today["due"] if sales_today else 0,
        "total_receivables":   total_receivables["total"] if total_receivables else 0,
        "total_payables":      total_payables["total"] if total_payables else 0,
        "product_count":       product_count["cnt"] if product_count else 0,
        "customer_count":      customer_count["cnt"] if customer_count else 0,
        "supplier_count":      supplier_count["cnt"] if supplier_count else 0,
        "cash_balance":        cash_balance,
        "bank_balance":        bank_balance,
        "low_stock_count":     len(low_stock),
    }

    return render_template("saas_auth/dashboard.html",
                           user=user, biz=biz, stats=stats,
                           low_stock=low_stock)
