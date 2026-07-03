"""
utils/ledger_transactions.py — Named Transaction-Type Helpers
====================================================================
This is the layer application code actually calls. Every function here
is a thin, named wrapper around utils.ledger_service.post_journal_entry()
that knows which accounts to debit/credit for one specific kind of
business event. None of these functions write to the database directly
— they all build a `lines` list and hand it to post_journal_entry(),
so every guarantee from the core service (validation, atomicity,
balance caching, audit logging) applies uniformly to all of them.

Payment method handling:
  Every function that involves money in/out takes a `payment_method`
  argument ('cash' | 'bank' | 'upi' | 'card' | 'cheque' | 'credit').
  'upi', 'card', and 'cheque' all post against the Bank account (they
  are bank-mediated payment rails, not separate accounts) — only
  'cash' posts against the Cash account, and 'credit' means no
  cash/bank account moves at all (the corresponding receivable/payable
  changes instead). This matches standard small-business bookkeeping
  practice: a business doesn't need a separate ledger account per UPI
  app, only Cash vs Bank.

GST handling:
  Functions take optional cgst/sgst/igst amounts. When provided, GST
  is posted to the GST Payable (sales) or GST Input Credit (purchases)
  accounts as its own line(s) — never blended into the Sales/Purchases
  line, so GST returns can be computed straight from the ledger.

Discounts:
  Discounts on sales reduce the Sales Revenue recognised (debited to
  "Discounts Given", a contra-revenue expense account) rather than
  netting silently inside the Sales line, so management can see total
  discounts given as its own number.
"""

from datetime import datetime
from utils.chart_of_accounts import get_account_by_subtype, get_or_create_party_account
from utils.ledger_service import post_journal_entry, reverse_entry, InvalidLineError


def _cash_or_bank_account(business_id: int, payment_method: str):
    """Map a payment method string to the correct GL account."""
    method = (payment_method or "cash").lower()
    if method == "cash":
        return get_account_by_subtype(business_id, "cash")
    if method in ("bank", "upi", "card", "cheque", "neft", "rtgs", "bank_transfer"):
        return get_account_by_subtype(business_id, "bank")
    raise InvalidLineError(
        f"Unrecognised payment_method '{payment_method}'. Use cash, bank, upi, card, "
        f"cheque, or credit (for no immediate cash/bank movement)."
    )


def _today():
    return datetime.utcnow().date().isoformat()


# ═══════════════════════════════ SALES ════════════════════════════════════════

def record_cash_sale(business_id: int, amount: float, *, payment_method: str = "cash",
                      cgst: float = 0, sgst: float = 0, igst: float = 0,
                      discount: float = 0, customer_id: int = None,
                      customer_name: str = "", source_id: int = None,
                      narration: str = "", entry_date: str = None,
                      created_by: int = None) -> dict:
    """
    Cash Sale: Increase Cash/Bank, Increase Sales (net of discount), record GST.
    `amount` is the gross sale amount BEFORE GST and discount are applied
    to the lines (i.e. amount = taxable value + discount, GST passed separately).
    """
    cash_acct  = _cash_or_bank_account(business_id, payment_method)
    sales_acct = get_account_by_subtype(business_id, "sales_revenue")

    taxable = round(amount - discount, 2)
    total_received = round(taxable + cgst + sgst + igst, 2)

    lines = [{"account_id": cash_acct["id"], "debit": total_received, "credit": 0,
              "description": "Cash sale received"}]

    if discount > 0:
        disc_acct = get_account_by_subtype(business_id, "discount_given")
        lines.append({"account_id": disc_acct["id"], "debit": discount, "credit": 0,
                      "description": "Discount given on sale"})
        lines.append({"account_id": sales_acct["id"], "debit": 0, "credit": round(amount, 2),
                      "description": "Sale revenue (gross)"})
    else:
        lines.append({"account_id": sales_acct["id"], "debit": 0, "credit": taxable,
                      "description": "Sale revenue"})

    if cgst or sgst or igst:
        gst_acct = get_account_by_subtype(business_id, "gst_payable")
        gst_total = round(cgst + sgst + igst, 2)
        lines.append({"account_id": gst_acct["id"], "debit": 0, "credit": gst_total,
                      "description": "GST collected on sale"})

    party_kwargs = {}
    if customer_id:
        cust_acct = get_or_create_party_account(business_id, "customer", customer_id, customer_name)
        party_kwargs = {"party_type": "customer", "party_id": customer_id}
        lines[0].update(party_kwargs)

    return post_journal_entry(
        business_id, lines, source_type="cash_sale", source_id=source_id,
        narration=narration or f"Cash sale" + (f" to {customer_name}" if customer_name else ""),
        entry_date=entry_date, created_by=created_by
    )


def record_credit_sale(business_id: int, amount: float, *, customer_id: int,
                        customer_name: str, cgst: float = 0, sgst: float = 0,
                        igst: float = 0, discount: float = 0, source_id: int = None,
                        narration: str = "", entry_date: str = None,
                        created_by: int = None) -> dict:
    """
    Sales on Credit: Increase Customer Due (Accounts Receivable), Increase Sales.
    No Cash/Bank account is touched — the customer owes the business.
    """
    if not customer_id:
        raise InvalidLineError("record_credit_sale requires a customer_id — credit sales must be tied to a party.")

    cust_acct  = get_or_create_party_account(business_id, "customer", customer_id, customer_name)
    sales_acct = get_account_by_subtype(business_id, "sales_revenue")

    taxable = round(amount - discount, 2)
    total_due = round(taxable + cgst + sgst + igst, 2)

    lines = [{"account_id": cust_acct["id"], "debit": total_due, "credit": 0,
              "party_type": "customer", "party_id": customer_id,
              "description": f"Credit sale to {customer_name}"}]

    if discount > 0:
        disc_acct = get_account_by_subtype(business_id, "discount_given")
        lines.append({"account_id": disc_acct["id"], "debit": discount, "credit": 0,
                      "description": "Discount given on sale"})
        lines.append({"account_id": sales_acct["id"], "debit": 0, "credit": round(amount, 2),
                      "description": "Sale revenue (gross)"})
    else:
        lines.append({"account_id": sales_acct["id"], "debit": 0, "credit": taxable,
                      "description": "Sale revenue"})

    if cgst or sgst or igst:
        gst_acct = get_account_by_subtype(business_id, "gst_payable")
        gst_total = round(cgst + sgst + igst, 2)
        lines.append({"account_id": gst_acct["id"], "debit": 0, "credit": gst_total,
                      "description": "GST collected on sale"})

    return post_journal_entry(
        business_id, lines, source_type="credit_sale", source_id=source_id,
        narration=narration or f"Credit sale to {customer_name}",
        entry_date=entry_date, created_by=created_by
    )


# ═══════════════════════════════ PURCHASES ════════════════════════════════════

def record_cash_purchase(business_id: int, amount: float, *, payment_method: str = "cash",
                          cgst: float = 0, sgst: float = 0, igst: float = 0,
                          supplier_id: int = None, supplier_name: str = "",
                          source_id: int = None, narration: str = "",
                          entry_date: str = None, created_by: int = None) -> dict:
    """
    Cash Purchase: Reduce Cash/Bank, Increase Purchases. GST input credit
    recorded separately so it can be claimed against output GST later.
    """
    cash_acct = _cash_or_bank_account(business_id, payment_method)
    pur_acct  = get_account_by_subtype(business_id, "cogs")  # 'Purchases' subtype shares cogs

    taxable    = round(amount, 2)
    total_paid = round(taxable + cgst + sgst + igst, 2)

    lines = [
        {"account_id": pur_acct["id"], "debit": taxable, "credit": 0,
         "description": "Purchase of goods"},
    ]

    if cgst or sgst or igst:
        gst_acct = get_account_by_subtype(business_id, "gst_input_credit")
        gst_total = round(cgst + sgst + igst, 2)
        lines.append({"account_id": gst_acct["id"], "debit": gst_total, "credit": 0,
                      "description": "GST input credit on purchase"})

    cash_line = {"account_id": cash_acct["id"], "debit": 0, "credit": total_paid,
                 "description": "Cash paid for purchase"}
    if supplier_id:
        get_or_create_party_account(business_id, "supplier", supplier_id, supplier_name)
        cash_line.update({"party_type": "supplier", "party_id": supplier_id})
    lines.append(cash_line)

    return post_journal_entry(
        business_id, lines, source_type="cash_purchase", source_id=source_id,
        narration=narration or "Cash purchase" + (f" from {supplier_name}" if supplier_name else ""),
        entry_date=entry_date, created_by=created_by
    )


def record_credit_purchase(business_id: int, amount: float, *, supplier_id: int,
                            supplier_name: str, cgst: float = 0, sgst: float = 0,
                            igst: float = 0, source_id: int = None,
                            narration: str = "", entry_date: str = None,
                            created_by: int = None) -> dict:
    """
    Credit Purchase: Increase Purchases & Supplier Due (Accounts Payable).
    No Cash/Bank impact — the business owes the supplier.
    """
    if not supplier_id:
        raise InvalidLineError("record_credit_purchase requires a supplier_id.")

    sup_acct = get_or_create_party_account(business_id, "supplier", supplier_id, supplier_name)
    pur_acct = get_account_by_subtype(business_id, "cogs")

    taxable   = round(amount, 2)
    total_due = round(taxable + cgst + sgst + igst, 2)

    lines = [
        {"account_id": pur_acct["id"], "debit": taxable, "credit": 0,
         "description": "Purchase of goods"},
    ]

    if cgst or sgst or igst:
        gst_acct = get_account_by_subtype(business_id, "gst_input_credit")
        gst_total = round(cgst + sgst + igst, 2)
        lines.append({"account_id": gst_acct["id"], "debit": gst_total, "credit": 0,
                      "description": "GST input credit on purchase"})

    lines.append({"account_id": sup_acct["id"], "debit": 0, "credit": total_due,
                  "party_type": "supplier", "party_id": supplier_id,
                  "description": f"Credit purchase from {supplier_name}"})

    return post_journal_entry(
        business_id, lines, source_type="credit_purchase", source_id=source_id,
        narration=narration or f"Credit purchase from {supplier_name}",
        entry_date=entry_date, created_by=created_by
    )


# ═══════════════════════════════ PAYMENTS ═════════════════════════════════════

def record_payment_from_customer(business_id: int, amount: float, *, customer_id: int,
                                  customer_name: str, payment_method: str = "cash",
                                  source_id: int = None, narration: str = "",
                                  entry_date: str = None, created_by: int = None) -> dict:
    """
    Cash/Bank received from Customer: Increase Cash/Bank, Reduce Customer Due.
    Used for both full settlement and partial payments against outstanding
    credit sales — caller passes whatever amount was actually received.
    """
    cash_acct = _cash_or_bank_account(business_id, payment_method)
    cust_acct = get_or_create_party_account(business_id, "customer", customer_id, customer_name)
    amount = round(amount, 2)

    lines = [
        {"account_id": cash_acct["id"], "debit": amount, "credit": 0,
         "party_type": "customer", "party_id": customer_id,
         "description": f"Payment received from {customer_name}"},
        {"account_id": cust_acct["id"], "debit": 0, "credit": amount,
         "party_type": "customer", "party_id": customer_id,
         "description": "Customer due reduced"},
    ]

    return post_journal_entry(
        business_id, lines, source_type="payment_in", source_id=source_id,
        narration=narration or f"Payment received from {customer_name}",
        entry_date=entry_date, created_by=created_by
    )


def record_payment_to_supplier(business_id: int, amount: float, *, supplier_id: int,
                                supplier_name: str, payment_method: str = "cash",
                                source_id: int = None, narration: str = "",
                                entry_date: str = None, created_by: int = None) -> dict:
    """
    Payment to Supplier (Cash/Bank/UPI/Card): Reduce Cash/Bank, Reduce Supplier Due.
    """
    cash_acct = _cash_or_bank_account(business_id, payment_method)
    sup_acct  = get_or_create_party_account(business_id, "supplier", supplier_id, supplier_name)
    amount = round(amount, 2)

    lines = [
        {"account_id": sup_acct["id"], "debit": amount, "credit": 0,
         "party_type": "supplier", "party_id": supplier_id,
         "description": "Supplier due reduced"},
        {"account_id": cash_acct["id"], "debit": 0, "credit": amount,
         "party_type": "supplier", "party_id": supplier_id,
         "description": f"Payment made to {supplier_name}"},
    ]

    return post_journal_entry(
        business_id, lines, source_type="payment_out", source_id=source_id,
        narration=narration or f"Payment to {supplier_name}",
        entry_date=entry_date, created_by=created_by
    )


# ═══════════════════════════════ COMPOSITE HELPERS ════════════════════════════
# These cover the full spectrum from pure-cash to pure-credit to a partial
# payment taken at the moment of sale/purchase — exactly how the Billing
# and Purchase modules' UI actually behaves (a single "amount paid now"
# field that can be anywhere from 0 to the full total). Application code
# should call these rather than reasoning about which primitive applies.

def record_sale(business_id: int, amount: float, *, paid_amount: float,
                 customer_id: int = None, customer_name: str = "Walk-in Customer",
                 payment_method: str = "cash", cgst: float = 0, sgst: float = 0,
                 igst: float = 0, discount: float = 0, source_id: int = None,
                 narration: str = "", entry_date: str = None,
                 created_by: int = None) -> dict:
    """
    amount       = taxable value (before GST), same convention as
                   record_cash_sale / record_credit_sale.
    paid_amount  = cash/bank actually received now. 0 = fully on credit.
                   >= total (incl GST, net of discount) = fully paid.
                   In between = partial: posts a credit sale for the full
                   amount, then an immediate payment for paid_amount.

    Returns {"sale_entry": {...}, "payment_entry": {...} or None, "status": str}
    """
    taxable = round(amount - discount, 2)
    total_with_tax = round(taxable + cgst + sgst + igst, 2)
    paid_amount = round(max(0, paid_amount), 2)

    if paid_amount <= 0:
        result = record_credit_sale(
            business_id, amount, customer_id=customer_id, customer_name=customer_name,
            cgst=cgst, sgst=sgst, igst=igst, discount=discount, source_id=source_id,
            narration=narration, entry_date=entry_date, created_by=created_by
        )
        return {"sale_entry": result, "payment_entry": None, "status": "unpaid"}

    if paid_amount >= total_with_tax:
        result = record_cash_sale(
            business_id, amount, payment_method=payment_method, cgst=cgst, sgst=sgst,
            igst=igst, discount=discount, customer_id=customer_id, customer_name=customer_name,
            source_id=source_id, narration=narration, entry_date=entry_date, created_by=created_by
        )
        return {"sale_entry": result, "payment_entry": None, "status": "paid"}

    if not customer_id:
        raise InvalidLineError(
            "A partial payment at time of sale requires a customer_id — the "
            "remaining balance must be tracked against a specific party."
        )
    sale_result = record_credit_sale(
        business_id, amount, customer_id=customer_id, customer_name=customer_name,
        cgst=cgst, sgst=sgst, igst=igst, discount=discount, source_id=source_id,
        narration=narration, entry_date=entry_date, created_by=created_by
    )
    payment_result = record_payment_from_customer(
        business_id, paid_amount, customer_id=customer_id, customer_name=customer_name,
        payment_method=payment_method, source_id=source_id,
        narration=f"Partial payment at time of sale" + (f" — {narration}" if narration else ""),
        entry_date=entry_date, created_by=created_by
    )
    return {"sale_entry": sale_result, "payment_entry": payment_result, "status": "partial"}


def record_purchase(business_id: int, amount: float, *, paid_amount: float,
                     supplier_id: int = None, supplier_name: str = "",
                     payment_method: str = "cash", cgst: float = 0, sgst: float = 0,
                     igst: float = 0, source_id: int = None, narration: str = "",
                     entry_date: str = None, created_by: int = None) -> dict:
    """Purchase-side mirror of record_sale() — see its docstring for the pattern."""
    taxable = round(amount, 2)
    total_with_tax = round(taxable + cgst + sgst + igst, 2)
    paid_amount = round(max(0, paid_amount), 2)

    if paid_amount <= 0:
        result = record_credit_purchase(
            business_id, amount, supplier_id=supplier_id, supplier_name=supplier_name,
            cgst=cgst, sgst=sgst, igst=igst, source_id=source_id,
            narration=narration, entry_date=entry_date, created_by=created_by
        )
        return {"purchase_entry": result, "payment_entry": None, "status": "pending"}

    if paid_amount >= total_with_tax:
        result = record_cash_purchase(
            business_id, amount, payment_method=payment_method, cgst=cgst, sgst=sgst,
            igst=igst, supplier_id=supplier_id, supplier_name=supplier_name,
            source_id=source_id, narration=narration, entry_date=entry_date, created_by=created_by
        )
        return {"purchase_entry": result, "payment_entry": None, "status": "received"}

    if not supplier_id:
        raise InvalidLineError(
            "A partial payment at time of purchase requires a supplier_id — the "
            "remaining balance must be tracked against a specific party."
        )
    purchase_result = record_credit_purchase(
        business_id, amount, supplier_id=supplier_id, supplier_name=supplier_name,
        cgst=cgst, sgst=sgst, igst=igst, source_id=source_id,
        narration=narration, entry_date=entry_date, created_by=created_by
    )
    payment_result = record_payment_to_supplier(
        business_id, paid_amount, supplier_id=supplier_id, supplier_name=supplier_name,
        payment_method=payment_method, source_id=source_id,
        narration=f"Partial payment at time of purchase" + (f" — {narration}" if narration else ""),
        entry_date=entry_date, created_by=created_by
    )
    return {"purchase_entry": purchase_result, "payment_entry": payment_result, "status": "partial"}


def record_advance_from_customer(business_id: int, amount: float, *, customer_id: int,
                                  customer_name: str, payment_method: str = "cash",
                                  narration: str = "", entry_date: str = None,
                                  created_by: int = None) -> dict:
    """
    Advance payment received from a customer BEFORE any sale exists.
    Identical accounting to record_payment_from_customer (it still reduces
    the customer's account — a negative/credit balance there represents
    "we owe them goods or a refund"), but tagged with its own source_type
    so reports can distinguish advances from settlements of existing dues.
    """
    result = record_payment_from_customer(
        business_id, amount, customer_id=customer_id, customer_name=customer_name,
        payment_method=payment_method, narration=narration or f"Advance received from {customer_name}",
        entry_date=entry_date, created_by=created_by
    )
    return result


def record_advance_to_supplier(business_id: int, amount: float, *, supplier_id: int,
                                supplier_name: str, payment_method: str = "cash",
                                narration: str = "", entry_date: str = None,
                                created_by: int = None) -> dict:
    """Advance payment made to a supplier before any purchase bill exists."""
    return record_payment_to_supplier(
        business_id, amount, supplier_id=supplier_id, supplier_name=supplier_name,
        payment_method=payment_method, narration=narration or f"Advance paid to {supplier_name}",
        entry_date=entry_date, created_by=created_by
    )


# ═══════════════════════════════ TRANSFERS ════════════════════════════════════

def record_cash_deposit_to_bank(business_id: int, amount: float, *,
                                 narration: str = "", entry_date: str = None,
                                 created_by: int = None) -> dict:
    """Cash deposited into Bank: Reduce Cash, Increase Bank."""
    cash_acct = get_account_by_subtype(business_id, "cash")
    bank_acct = get_account_by_subtype(business_id, "bank")
    amount = round(amount, 2)

    lines = [
        {"account_id": bank_acct["id"], "debit": amount, "credit": 0,
         "description": "Cash deposited into bank"},
        {"account_id": cash_acct["id"], "debit": 0, "credit": amount,
         "description": "Cash withdrawn for bank deposit"},
    ]
    return post_journal_entry(
        business_id, lines, source_type="transfer",
        narration=narration or "Cash deposit to bank",
        entry_date=entry_date, created_by=created_by
    )


def record_bank_withdrawal_to_cash(business_id: int, amount: float, *,
                                    narration: str = "", entry_date: str = None,
                                    created_by: int = None) -> dict:
    """Bank withdrawal: Reduce Bank, Increase Cash."""
    cash_acct = get_account_by_subtype(business_id, "cash")
    bank_acct = get_account_by_subtype(business_id, "bank")
    amount = round(amount, 2)

    lines = [
        {"account_id": cash_acct["id"], "debit": amount, "credit": 0,
         "description": "Cash withdrawn from bank"},
        {"account_id": bank_acct["id"], "debit": 0, "credit": amount,
         "description": "Bank balance reduced by withdrawal"},
    ]
    return post_journal_entry(
        business_id, lines, source_type="transfer",
        narration=narration or "Bank withdrawal to cash",
        entry_date=entry_date, created_by=created_by
    )


def record_transfer(business_id: int, amount: float, *, from_subtype: str, to_subtype: str,
                     narration: str = "", entry_date: str = None,
                     created_by: int = None) -> dict:
    """
    Generic transfer between any two non-party accounts identified by
    subtype (e.g. 'cash' -> 'bank', or between two custom asset accounts
    a business has added). Use the specific helpers above for the common
    cash<->bank case; this exists for less common transfers.
    """
    from_acct = get_account_by_subtype(business_id, from_subtype)
    to_acct   = get_account_by_subtype(business_id, to_subtype)
    amount = round(amount, 2)

    lines = [
        {"account_id": to_acct["id"], "debit": amount, "credit": 0,
         "description": f"Transfer in from {from_acct['name']}"},
        {"account_id": from_acct["id"], "debit": 0, "credit": amount,
         "description": f"Transfer out to {to_acct['name']}"},
    ]
    return post_journal_entry(
        business_id, lines, source_type="transfer",
        narration=narration or f"Transfer: {from_acct['name']} → {to_acct['name']}",
        entry_date=entry_date, created_by=created_by
    )


# ═══════════════════════════════ RETURNS ══════════════════════════════════════

def record_sales_return(business_id: int, amount: float, *, customer_id: int = None,
                         customer_name: str = "", cgst: float = 0, sgst: float = 0,
                         igst: float = 0, refund_method: str = "credit",
                         source_id: int = None, narration: str = "",
                         entry_date: str = None, created_by: int = None) -> dict:
    """
    Sales Return: a customer returns goods already sold. This REVERSES the
    revenue recognition (debit Sales Returns & Allowances, a contra-revenue
    expense account — never debit Sales Revenue directly, so gross sales
    and returns remain separately visible in P&L) and reverses any GST
    that was charged.

    refund_method controls what happens on the other side:
      'credit' (default) → reduces the customer's outstanding due
                            (or creates a credit balance if they'd
                            already paid in full / had no due)
      'cash' | 'bank' | 'upi' | 'card' → an actual cash/bank refund is
                            paid out to the customer immediately
    """
    returns_acct = get_account_by_subtype(business_id, "returns_expense")
    taxable = round(amount, 2)
    total_reversed = round(taxable + cgst + sgst + igst, 2)

    lines = [
        {"account_id": returns_acct["id"], "debit": taxable, "credit": 0,
         "description": "Sales return — revenue reversed"},
    ]

    if cgst or sgst or igst:
        gst_acct = get_account_by_subtype(business_id, "gst_payable")
        gst_total = round(cgst + sgst + igst, 2)
        lines.append({"account_id": gst_acct["id"], "debit": gst_total, "credit": 0,
                      "description": "GST reversed on sales return"})

    if refund_method == "credit":
        if not customer_id:
            raise InvalidLineError("record_sales_return with refund_method='credit' requires a customer_id.")
        cust_acct = get_or_create_party_account(business_id, "customer", customer_id, customer_name)
        lines.append({"account_id": cust_acct["id"], "debit": 0, "credit": total_reversed,
                      "party_type": "customer", "party_id": customer_id,
                      "description": f"Customer due reduced for return"})
    else:
        cash_acct = _cash_or_bank_account(business_id, refund_method)
        refund_line = {"account_id": cash_acct["id"], "debit": 0, "credit": total_reversed,
                       "description": "Cash/bank refund paid to customer"}
        if customer_id:
            refund_line.update({"party_type": "customer", "party_id": customer_id})
        lines.append(refund_line)

    return post_journal_entry(
        business_id, lines, source_type="sales_return", source_id=source_id,
        narration=narration or "Sales return" + (f" from {customer_name}" if customer_name else ""),
        entry_date=entry_date, created_by=created_by
    )


def record_purchase_return(business_id: int, amount: float, *, supplier_id: int = None,
                            supplier_name: str = "", cgst: float = 0, sgst: float = 0,
                            igst: float = 0, refund_method: str = "credit",
                            source_id: int = None, narration: str = "",
                            entry_date: str = None, created_by: int = None) -> dict:
    """
    Purchase Return: goods bought are returned to the supplier. This
    REVERSES the purchase (credit Purchases/COGS — reduces the expense
    that was recorded) and reverses any GST input credit that was claimed.

    refund_method:
      'credit' (default) → reduces what the business owes the supplier
                            (or creates a receivable from them if the
                            bill was already paid in full)
      'cash' | 'bank' | 'upi' | 'card' → the supplier refunds cash/bank
                            directly
    """
    pur_acct = get_account_by_subtype(business_id, "cogs")
    taxable = round(amount, 2)
    total_reversed = round(taxable + cgst + sgst + igst, 2)

    lines = [
        {"account_id": pur_acct["id"], "debit": 0, "credit": taxable,
         "description": "Purchase return — expense reversed"},
    ]

    if cgst or sgst or igst:
        gst_acct = get_account_by_subtype(business_id, "gst_input_credit")
        gst_total = round(cgst + sgst + igst, 2)
        lines.append({"account_id": gst_acct["id"], "debit": 0, "credit": gst_total,
                      "description": "GST input credit reversed on purchase return"})

    if refund_method == "credit":
        if not supplier_id:
            raise InvalidLineError("record_purchase_return with refund_method='credit' requires a supplier_id.")
        sup_acct = get_or_create_party_account(business_id, "supplier", supplier_id, supplier_name)
        lines.append({"account_id": sup_acct["id"], "debit": total_reversed, "credit": 0,
                      "party_type": "supplier", "party_id": supplier_id,
                      "description": "Supplier due reduced for return"})
    else:
        cash_acct = _cash_or_bank_account(business_id, refund_method)
        refund_line = {"account_id": cash_acct["id"], "debit": total_reversed, "credit": 0,
                       "description": "Cash/bank refund received from supplier"}
        if supplier_id:
            refund_line.update({"party_type": "supplier", "party_id": supplier_id})
        lines.append(refund_line)

    return post_journal_entry(
        business_id, lines, source_type="purchase_return", source_id=source_id,
        narration=narration or "Purchase return" + (f" to {supplier_name}" if supplier_name else ""),
        entry_date=entry_date, created_by=created_by
    )


# ═══════════════════════════════ OPENING BALANCES ═════════════════════════════

def record_opening_balance(business_id: int, account_subtype: str, amount: float, *,
                            party_id: int = None, party_name: str = "",
                            party_type: str = None, narration: str = "",
                            entry_date: str = None, created_by: int = None) -> dict:
    """
    Opening balance for any account (cash on hand at start, opening bank
    balance, opening customer due, opening supplier due, opening capital,
    etc). The offsetting entry always goes to Owner's Equity — this is
    standard practice for entering a business's starting position into
    a fresh set of books.

    amount is always entered as a positive number representing the
    natural balance for that account (e.g. positive = customer owes us,
    positive = we owe the supplier, positive = cash we have on hand).
    """
    equity_acct = get_account_by_subtype(business_id, "owner_equity")

    if party_id and party_type:
        target_acct = get_or_create_party_account(business_id, party_type, party_id, party_name)
    else:
        target_acct = get_account_by_subtype(business_id, account_subtype)

    amount = round(amount, 2)
    is_debit_normal = target_acct["account_type"] in ("asset", "expense")

    if is_debit_normal:
        lines = [
            {"account_id": target_acct["id"], "debit": amount, "credit": 0,
             "party_type": party_type or "", "party_id": party_id,
             "description": "Opening balance"},
            {"account_id": equity_acct["id"], "debit": 0, "credit": amount,
             "description": "Opening balance offset"},
        ]
    else:
        lines = [
            {"account_id": equity_acct["id"], "debit": amount, "credit": 0,
             "description": "Opening balance offset"},
            {"account_id": target_acct["id"], "debit": 0, "credit": amount,
             "party_type": party_type or "", "party_id": party_id,
             "description": "Opening balance"},
        ]

    return post_journal_entry(
        business_id, lines, source_type="opening_balance",
        narration=narration or f"Opening balance — {target_acct['name']}",
        entry_date=entry_date, created_by=created_by
    )


# ═══════════════════════════════ ADJUSTMENTS ══════════════════════════════════

def record_adjustment(business_id: int, amount: float, *, debit_subtype: str,
                       credit_subtype: str, narration: str, entry_date: str = None,
                       created_by: int = None) -> dict:
    """
    Generic manual adjustment between any two accounts identified by
    subtype — for corrections, write-offs, manual journal entries that
    don't fit a named transaction type. `narration` is required (not
    optional) since an adjustment with no explanation is not auditable.
    """
    if not narration:
        raise InvalidLineError("record_adjustment requires a narration explaining the adjustment.")

    debit_acct  = get_account_by_subtype(business_id, debit_subtype)
    credit_acct = get_account_by_subtype(business_id, credit_subtype)
    amount = round(amount, 2)

    lines = [
        {"account_id": debit_acct["id"], "debit": amount, "credit": 0, "description": narration},
        {"account_id": credit_acct["id"], "debit": 0, "credit": amount, "description": narration},
    ]
    return post_journal_entry(
        business_id, lines, source_type="adjustment",
        narration=narration, entry_date=entry_date, created_by=created_by
    )
