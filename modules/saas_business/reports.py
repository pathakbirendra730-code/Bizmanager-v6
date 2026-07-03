"""
modules/saas_business/reports.py — SaaS-Native Reports
============================================================
Tenant-scoped sales and inventory reporting for the SaaS multi-tenant
system. Mirrors legacy modules/reports.py, but every query is scoped
by business_id and reads from saas_invoices / saas_invoice_items /
saas_products.

Expenses CSV export is NOT duplicated here — already covered by
modules/saas_business/finance.py's export_expenses route, built in an
earlier phase.

Permissions: view_reports → accountant and above (read-only by nature,
no separate manage_reports permission exists).
"""

import io
import csv
from datetime import datetime
from flask import Blueprint, render_template, request, Response
from models.saas_auth import saas_fetchone, saas_fetchall, _is_postgres
from utils.saas_helpers import saas_business_required
from utils.saas_middleware import permission_required, get_tenant_id
from utils.tax_helpers import today_str

saas_reports_bp = Blueprint("saas_reports", __name__, url_prefix="/biz/reports")

P = lambda: "%s" if _is_postgres() else "?"


@saas_reports_bp.route("/")
@saas_business_required
@permission_required("view_reports")
def index():
    biz_id = get_tenant_id()
    p = P()
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    return render_template("saas_business/reports/index.html", biz=biz)


# ════════════════════════════════ SALES REPORT ═════════════════════════════════

@saas_reports_bp.route("/sales")
@saas_business_required
@permission_required("view_reports")
def sales():
    biz_id    = get_tenant_id()
    period    = request.args.get("period", "daily")
    date_from = request.args.get("from", today_str())
    date_to   = request.args.get("to", today_str())
    p = P()

    invoices = saas_fetchall(
        f"""SELECT invoice_number, customer_name, customer_gstin, supply_type,
                   subtotal, discount, taxable_amount, cgst_amount, sgst_amount,
                   igst_amount, total_tax, total, payment_method, status,
                   DATE(created_at) as date
            FROM saas_invoices
            WHERE business_id={p} AND DATE(created_at) BETWEEN {p} AND {p}
            ORDER BY created_at DESC""",
        (biz_id, date_from, date_to)
    )

    summary = saas_fetchone(
        f"""SELECT COUNT(*) as cnt, COALESCE(SUM(total),0) as total,
                   COALESCE(SUM(taxable_amount),0) as taxable,
                   COALESCE(SUM(cgst_amount),0) as cgst,
                   COALESCE(SUM(sgst_amount),0) as sgst,
                   COALESCE(SUM(igst_amount),0) as igst,
                   COALESCE(SUM(total_tax),0) as total_tax,
                   COALESCE(AVG(total),0) as avg_order
            FROM saas_invoices
            WHERE business_id={p} AND DATE(created_at) BETWEEN {p} AND {p}
              AND status IN ('paid','partial')""",
        (biz_id, date_from, date_to)
    )

    if period == "monthly":
        grp = "strftime('%Y-%m',created_at)" if not _is_postgres() else "TO_CHAR(created_at,'YYYY-MM')"
    elif period == "yearly":
        grp = "strftime('%Y',created_at)" if not _is_postgres() else "TO_CHAR(created_at,'YYYY')"
    else:
        grp = "DATE(created_at)"

    grouped = saas_fetchall(
        f"""SELECT {grp} as label, COUNT(*) as orders, COALESCE(SUM(total),0) as total,
                   COALESCE(SUM(total_tax),0) as tax
            FROM saas_invoices
            WHERE business_id={p} AND DATE(created_at) BETWEEN {p} AND {p}
              AND status IN ('paid','partial')
            GROUP BY label ORDER BY label""",
        (biz_id, date_from, date_to)
    )

    top_products = saas_fetchall(
        f"""SELECT ii.product_name, ii.hsn_code,
                   SUM(ii.quantity) as qty, COALESCE(SUM(ii.taxable_amount),0) as revenue,
                   COALESCE(SUM(ii.cgst_amount+ii.sgst_amount+ii.igst_amount),0) as tax
            FROM saas_invoice_items ii
            JOIN saas_invoices i ON i.id = ii.invoice_id
            WHERE ii.business_id={p} AND DATE(i.created_at) BETWEEN {p} AND {p}
              AND i.status IN ('paid','partial')
            GROUP BY ii.product_name ORDER BY revenue DESC LIMIT 10""",
        (biz_id, date_from, date_to)
    )

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/reports/sales.html",
                           biz=biz, invoices=invoices, summary=summary,
                           grouped=grouped, top_products=top_products,
                           period=period, date_from=date_from, date_to=date_to)


# ════════════════════════════════ INVENTORY REPORT ═════════════════════════════

@saas_reports_bp.route("/inventory")
@saas_business_required
@permission_required("view_reports")
def inventory():
    biz_id = get_tenant_id()
    p = P()

    products = saas_fetchall(
        f"""SELECT pr.*, c.name as cat_name,
                   (pr.selling_price - pr.cost_price) as margin,
                   CASE WHEN pr.cost_price > 0
                        THEN ROUND((pr.selling_price - pr.cost_price) * 100.0 / pr.cost_price, 1)
                        ELSE 0 END as margin_pct,
                   (pr.stock_quantity * pr.cost_price) as stock_value
            FROM saas_products pr
            LEFT JOIN saas_categories c ON pr.category_id = c.id
            WHERE pr.business_id={p} AND pr.is_active=1
            ORDER BY pr.name""",
        (biz_id,)
    )

    summary = saas_fetchone(
        f"""SELECT COUNT(*) as total,
                   COALESCE(SUM(stock_quantity * cost_price), 0) as stock_value,
                   SUM(CASE WHEN stock_quantity <= low_stock_threshold THEN 1 ELSE 0 END) as low_cnt,
                   SUM(CASE WHEN stock_quantity = 0 THEN 1 ELSE 0 END) as out_cnt
            FROM saas_products WHERE business_id={p} AND is_active=1""",
        (biz_id,)
    )

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/reports/inventory.html",
                           products=products,
                           summary=summary or {"total": 0, "stock_value": 0, "low_cnt": 0, "out_cnt": 0},
                           biz=biz)


# ════════════════════════════════ CSV EXPORTS ══════════════════════════════════

@saas_reports_bp.route("/export/sales")
@saas_business_required
@permission_required("view_reports")
def export_sales():
    biz_id    = get_tenant_id()
    date_from = request.args.get("from", today_str())
    date_to   = request.args.get("to", today_str())
    p = P()

    rows = saas_fetchall(
        f"""SELECT invoice_number, customer_name, customer_gstin, supply_type,
                   subtotal, discount, taxable_amount, cgst_amount, sgst_amount,
                   igst_amount, total_tax, total, payment_method, status, DATE(created_at) as d
            FROM saas_invoices
            WHERE business_id={p} AND DATE(created_at) BETWEEN {p} AND {p}
            ORDER BY created_at""",
        (biz_id, date_from, date_to)
    )

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Invoice No", "Customer", "GSTIN", "Supply", "Subtotal", "Discount",
                "Taxable", "CGST", "SGST", "IGST", "Tax Total", "Grand Total",
                "Payment", "Status", "Date"])
    for r in rows:
        w.writerow([r["invoice_number"], r["customer_name"], r["customer_gstin"],
                    r["supply_type"], r["subtotal"], r["discount"], r["taxable_amount"],
                    r["cgst_amount"], r["sgst_amount"], r["igst_amount"], r["total_tax"],
                    r["total"], r["payment_method"], r["status"], r["d"]])

    return Response(buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=sales_{date_from}_{date_to}.csv"})


@saas_reports_bp.route("/export/inventory")
@saas_business_required
@permission_required("view_reports")
def export_inventory():
    biz_id = get_tenant_id()
    p = P()

    rows = saas_fetchall(
        f"""SELECT pr.name, pr.sku, pr.hsn_code, c.name as cat_name, pr.gst_rate,
                   pr.cost_price, pr.selling_price, pr.stock_quantity,
                   pr.low_stock_threshold, (pr.stock_quantity * pr.cost_price) as stock_value
            FROM saas_products pr
            LEFT JOIN saas_categories c ON pr.category_id = c.id
            WHERE pr.business_id={p} AND pr.is_active=1
            ORDER BY pr.name""",
        (biz_id,)
    )

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "SKU", "HSN", "Category", "GST%", "Cost", "Price",
                "Stock", "Min Stock", "Stock Value"])
    for r in rows:
        w.writerow([r["name"], r["sku"], r["hsn_code"], r["cat_name"], r["gst_rate"],
                    r["cost_price"], r["selling_price"], r["stock_quantity"],
                    r["low_stock_threshold"], r["stock_value"]])

    return Response(buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=inventory.csv"})
