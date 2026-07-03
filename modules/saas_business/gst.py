"""
modules/saas_business/gst.py — SaaS-Native GST Reports
============================================================
Tenant-scoped GST compliance reporting for the SaaS multi-tenant
system. Mirrors legacy modules/gst.py's reporting routes, but every
query is scoped by business_id and reads from saas_invoices /
saas_invoice_items.

Deliberately NOT ported:
  • HSN master CRUD (hsn_list, hsn_add, hsn_delete) — hsn_master is
    global reference data shared across the whole platform, not
    tenant-scoped business data. Read-only HSN lookup for product
    forms already exists via modules/saas_business/products.py's
    api_hsn / api_hsn_code routes (built in an earlier phase).
    Adding/editing the shared HSN master is an app-admin concern,
    not a per-business one.

Permissions: view_gst / manage_gst → accountant and above.
"""

import io
import csv
from datetime import datetime
from flask import Blueprint, render_template, request, Response
from models.saas_auth import saas_fetchone, saas_fetchall, _is_postgres
from utils.saas_helpers import saas_business_required
from utils.saas_middleware import permission_required, get_tenant_id

saas_gst_bp = Blueprint("saas_gst", __name__, url_prefix="/biz/gst")

P = lambda: "%s" if _is_postgres() else "?"


def _month_filter_clause(col: str) -> str:
    """Returns the correct SQL fragment for 'YYYY-MM' extraction per DB backend."""
    if _is_postgres():
        return f"TO_CHAR({col}, 'YYYY-MM')"
    return f"strftime('%Y-%m', {col})"


@saas_gst_bp.route("/")
@saas_business_required
@permission_required("view_gst")
def index():
    biz_id = get_tenant_id()
    p = P()
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    return render_template("saas_business/gst/index.html", biz=biz)


# ════════════════════════════════ MONTHLY SUMMARY ══════════════════════════════

@saas_gst_bp.route("/monthly")
@saas_business_required
@permission_required("view_gst")
def monthly_summary():
    biz_id = get_tenant_id()
    month  = request.args.get("month", datetime.now().strftime("%Y-%m"))
    p = P()
    mf = _month_filter_clause("created_at")

    totals = saas_fetchone(
        f"""SELECT COUNT(*) as invoice_count,
                   COALESCE(SUM(subtotal),0) as subtotal,
                   COALESCE(SUM(taxable_amount),0) as taxable,
                   COALESCE(SUM(cgst_amount),0) as cgst,
                   COALESCE(SUM(sgst_amount),0) as sgst,
                   COALESCE(SUM(igst_amount),0) as igst,
                   COALESCE(SUM(total_tax),0) as total_tax,
                   COALESCE(SUM(total),0) as grand_total
            FROM saas_invoices
            WHERE business_id={p} AND {mf}={p} AND status IN ('paid','partial')""",
        (biz_id, month)
    )

    mf_items = _month_filter_clause("i.created_at")
    slabs = saas_fetchall(
        f"""SELECT ii.gst_rate,
                   COUNT(DISTINCT i.id) as inv_count,
                   COALESCE(SUM(ii.taxable_amount),0) as taxable,
                   COALESCE(SUM(ii.cgst_amount),0) as cgst,
                   COALESCE(SUM(ii.sgst_amount),0) as sgst,
                   COALESCE(SUM(ii.igst_amount),0) as igst
            FROM saas_invoice_items ii
            JOIN saas_invoices i ON i.id = ii.invoice_id
            WHERE ii.business_id={p} AND {mf_items}={p} AND i.status IN ('paid','partial')
            GROUP BY ii.gst_rate ORDER BY ii.gst_rate""",
        (biz_id, month)
    )

    supply_split = saas_fetchall(
        f"""SELECT supply_type, COUNT(*) as cnt, COALESCE(SUM(total),0) as total
            FROM saas_invoices
            WHERE business_id={p} AND {mf}={p} AND status IN ('paid','partial')
            GROUP BY supply_type""",
        (biz_id, month)
    )

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/gst/monthly.html",
                           biz=biz, month=month, totals=totals,
                           slabs=slabs, supply_split=supply_split)


# ════════════════════════════════ GSTR-1 ═══════════════════════════════════════

@saas_gst_bp.route("/gstr1")
@saas_business_required
@permission_required("view_gst")
def gstr1():
    biz_id = get_tenant_id()
    month  = request.args.get("month", datetime.now().strftime("%Y-%m"))
    p = P()
    mf = _month_filter_clause("created_at")

    b2b = saas_fetchall(
        f"""SELECT invoice_number, customer_name, customer_gstin,
                   customer_state, supply_type,
                   taxable_amount, cgst_amount, sgst_amount,
                   igst_amount, total_tax, total,
                   DATE(created_at) as inv_date
            FROM saas_invoices
            WHERE business_id={p} AND {mf}={p}
              AND status IN ('paid','partial') AND customer_gstin != ''
            ORDER BY created_at""",
        (biz_id, month)
    )

    b2c = saas_fetchall(
        f"""SELECT invoice_number, customer_name,
                   taxable_amount, cgst_amount, sgst_amount,
                   igst_amount, total_tax, total,
                   DATE(created_at) as inv_date
            FROM saas_invoices
            WHERE business_id={p} AND {mf}={p}
              AND status IN ('paid','partial') AND (customer_gstin = '' OR customer_gstin IS NULL)
            ORDER BY created_at""",
        (biz_id, month)
    )

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/gst/gstr1.html",
                           biz=biz, month=month, b2b=b2b, b2c=b2c)


# ════════════════════════════════ HSN-WISE SUMMARY ═════════════════════════════

@saas_gst_bp.route("/hsn-summary")
@saas_business_required
@permission_required("view_gst")
def hsn_summary():
    biz_id = get_tenant_id()
    month  = request.args.get("month", datetime.now().strftime("%Y-%m"))
    p = P()
    mf = _month_filter_clause("i.created_at")

    rows = saas_fetchall(
        f"""SELECT ii.hsn_code,
                   ii.product_name as description,
                   SUM(ii.quantity) as total_qty,
                   COALESCE(SUM(ii.taxable_amount),0) as taxable,
                   ii.gst_rate,
                   COALESCE(SUM(ii.cgst_amount),0) as cgst,
                   COALESCE(SUM(ii.sgst_amount),0) as sgst,
                   COALESCE(SUM(ii.igst_amount),0) as igst,
                   COALESCE(SUM(ii.cgst_amount+ii.sgst_amount+ii.igst_amount),0) as total_tax
            FROM saas_invoice_items ii
            JOIN saas_invoices i ON i.id = ii.invoice_id
            WHERE ii.business_id={p} AND {mf}={p} AND i.status IN ('paid','partial')
              AND ii.hsn_code != ''
            GROUP BY ii.hsn_code, ii.gst_rate
            ORDER BY taxable DESC""",
        (biz_id, month)
    )

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/gst/hsn_summary.html",
                           biz=biz, month=month, rows=rows)


# ════════════════════════════════ CSV EXPORTS ══════════════════════════════════

@saas_gst_bp.route("/export/monthly")
@saas_business_required
@permission_required("view_gst")
def export_monthly():
    biz_id = get_tenant_id()
    month  = request.args.get("month", datetime.now().strftime("%Y-%m"))
    p = P()
    mf = _month_filter_clause("created_at")

    rows = saas_fetchall(
        f"""SELECT invoice_number, customer_name, customer_gstin,
                   DATE(created_at) as d, supply_type,
                   subtotal, taxable_amount, cgst_amount, sgst_amount,
                   igst_amount, total_tax, total, payment_method
            FROM saas_invoices
            WHERE business_id={p} AND {mf}={p} AND status IN ('paid','partial')
            ORDER BY created_at""",
        (biz_id, month)
    )

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Invoice No", "Customer", "GSTIN", "Date", "Supply Type",
                "Subtotal", "Taxable", "CGST", "SGST", "IGST", "Total Tax",
                "Grand Total", "Payment"])
    for r in rows:
        w.writerow([r["invoice_number"], r["customer_name"], r["customer_gstin"],
                    r["d"], r["supply_type"], r["subtotal"], r["taxable_amount"],
                    r["cgst_amount"], r["sgst_amount"], r["igst_amount"],
                    r["total_tax"], r["total"], r["payment_method"]])

    return Response(buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=GST_{month}.csv"})


@saas_gst_bp.route("/export/hsn")
@saas_business_required
@permission_required("view_gst")
def export_hsn():
    biz_id = get_tenant_id()
    month  = request.args.get("month", datetime.now().strftime("%Y-%m"))
    p = P()
    mf = _month_filter_clause("i.created_at")

    rows = saas_fetchall(
        f"""SELECT ii.hsn_code, ii.product_name as description,
                   SUM(ii.quantity) as qty, ii.gst_rate,
                   COALESCE(SUM(ii.taxable_amount),0) as taxable,
                   COALESCE(SUM(ii.cgst_amount),0) as cgst,
                   COALESCE(SUM(ii.sgst_amount),0) as sgst,
                   COALESCE(SUM(ii.igst_amount),0) as igst
            FROM saas_invoice_items ii
            JOIN saas_invoices i ON i.id = ii.invoice_id
            WHERE ii.business_id={p} AND {mf}={p} AND i.status IN ('paid','partial')
            GROUP BY ii.hsn_code, ii.gst_rate ORDER BY taxable DESC""",
        (biz_id, month)
    )

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["HSN Code", "Description", "Total Qty", "GST%", "Taxable", "CGST", "SGST", "IGST"])
    for r in rows:
        w.writerow([r["hsn_code"], r["description"], r["qty"], r["gst_rate"],
                    r["taxable"], r["cgst"], r["sgst"], r["igst"]])

    return Response(buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=HSN_Summary_{month}.csv"})
