"""
utils/tax_helpers.py — Shared GST & date utilities
====================================================
Pure, tenant-agnostic helpers used across the SaaS-native business
modules. Split out from the old utils/helpers.py, which mixed these
in with legacy single-tenant shop/auth decorators that no longer exist.
"""

from datetime import datetime


def calculate_gst(unit_price: float, quantity: float,
                  gst_rate: float, supply_type: str = "intra",
                  item_discount: float = 0) -> dict:
    """
    Core GST calculation engine.

    supply_type: 'intra'  → CGST + SGST  (each = gst_rate / 2)
                 'inter'  → IGST         (= gst_rate)

    Returns dict with all GST components.
    """
    subtotal      = round(unit_price * quantity, 2)
    disc_amount   = round(subtotal * item_discount / 100, 2) if item_discount else 0
    taxable       = round(subtotal - disc_amount, 2)

    if supply_type == "inter":
        igst_rate  = gst_rate
        igst_amt   = round(taxable * igst_rate / 100, 2)
        cgst_rate  = cgst_amt = sgst_rate = sgst_amt = 0.0
    else:
        cgst_rate  = sgst_rate = round(gst_rate / 2, 2)
        cgst_amt   = round(taxable * cgst_rate / 100, 2)
        sgst_amt   = round(taxable * sgst_rate / 100, 2)
        igst_rate  = igst_amt = 0.0

    total_tax = round(cgst_amt + sgst_amt + igst_amt, 2)
    total     = round(taxable + total_tax, 2)

    return {
        "subtotal":      subtotal,
        "disc_amount":   disc_amount,
        "taxable":       taxable,
        "gst_rate":      gst_rate,
        "cgst_rate":     cgst_rate,
        "sgst_rate":     sgst_rate,
        "igst_rate":     igst_rate,
        "cgst_amount":   cgst_amt,
        "sgst_amount":   sgst_amt,
        "igst_amount":   igst_amt,
        "total_tax":     total_tax,
        "total":         total,
    }


def determine_supply_type(business_state: str, customer_state: str) -> str:
    """
    Return 'intra' if both states match, else 'inter'.
    Empty customer state defaults to intra (B2C local).
    """
    if not customer_state or business_state == customer_state:
        return "intra"
    return "inter"


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
