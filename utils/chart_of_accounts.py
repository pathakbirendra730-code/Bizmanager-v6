"""
utils/chart_of_accounts.py — Standard Chart of Accounts Seeding
====================================================================
Defines the standard set of accounts every business gets automatically
when its double-entry books are initialised, and provides lookup
helpers so the posting service never has to hardcode account IDs —
it asks for "the cash account" or "the sales account" by subtype/code
and gets back whatever row that business actually has.

Customer and Supplier sub-ledger accounts are created lazily (on first
transaction with a new party) rather than all upfront, since a business
may have hundreds of customers — see get_or_create_party_account().
"""

from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres

P = lambda: "%s" if _is_postgres() else "?"


# ═══════════════════════════ STANDARD ACCOUNT TEMPLATE ════════════════════════
# (code, name, account_type, account_subtype)
# Codes follow a conventional numbering block:
#   1000-1999 Assets   2000-2999 Liabilities   3000-3999 Equity
#   4000-4999 Income   5000-5999 Expenses

STANDARD_ACCOUNTS = [
    # ── Assets ────────────────────────────────────────────────────────────────
    ("1000", "Cash",                       "asset",     "cash"),
    ("1100", "Bank Account",                "asset",     "bank"),
    ("1200", "Accounts Receivable",         "asset",     "accounts_receivable"),
    ("1300", "Inventory",                   "asset",     "inventory"),
    ("1400", "GST Input Credit",            "asset",     "gst_input_credit"),
    ("1900", "Other Current Assets",        "asset",     "other_asset"),

    # ── Liabilities ───────────────────────────────────────────────────────────
    ("2000", "Accounts Payable",            "liability", "accounts_payable"),
    ("2100", "GST Payable (Output)",        "liability", "gst_payable"),
    ("2900", "Other Current Liabilities",   "liability", "other_liability"),

    # ── Equity ────────────────────────────────────────────────────────────────
    ("3000", "Owner's Equity",              "equity",    "owner_equity"),
    ("3900", "Retained Earnings",           "equity",    "retained_earnings"),

    # ── Income ────────────────────────────────────────────────────────────────
    ("4000", "Sales Revenue",               "income",    "sales_revenue"),
    ("4900", "Other Income",                "income",    "other_income"),

    # ── Expenses ──────────────────────────────────────────────────────────────
    ("5000", "Cost of Goods Sold",          "expense",   "cogs"),
    ("5100", "Purchases",                   "expense",   "cogs"),
    ("5200", "Discounts Given",             "expense",   "discount_given"),
    ("5300", "Sales Returns & Allowances",  "expense",   "returns_expense"),
    ("5900", "Operating Expenses",          "expense",   "operating_expense"),
]


def seed_chart_of_accounts(business_id: int, created_by: int = None) -> dict:
    """
    Create the standard Chart of Accounts for a newly-onboarded business.
    Idempotent — safe to call even if some accounts already exist (skips
    duplicates by code). Returns {code: account_id} for convenience.
    """
    p = P()
    created = {}

    for code, name, acct_type, subtype in STANDARD_ACCOUNTS:
        existing = saas_fetchone(
            f"SELECT id FROM saas_chart_of_accounts WHERE business_id={p} AND code={p}",
            (business_id, code)
        )
        if existing:
            created[code] = existing["id"]
            continue

        acct_id = saas_execute(
            f"""INSERT INTO saas_chart_of_accounts
                (business_id, code, name, account_type, account_subtype, is_system)
                VALUES ({p},{p},{p},{p},{p},TRUE)""",
            (business_id, code, name, acct_type, subtype)
        )
        created[code] = acct_id

    return created


# ═══════════════════════════ LOOKUP HELPERS ═══════════════════════════════════

def get_account_by_subtype(business_id: int, subtype: str):
    """
    Return the first active account of a given subtype for this business.
    Used by the posting service to find "the" cash/bank/sales/etc account
    without hardcoding IDs. Raises if none exists (should never happen
    after seed_chart_of_accounts has run).
    """
    p = P()
    row = saas_fetchone(
        f"""SELECT * FROM saas_chart_of_accounts
            WHERE business_id={p} AND account_subtype={p} AND is_active=TRUE
              AND party_type=''
            ORDER BY id LIMIT 1""",
        (business_id, subtype)
    )
    if not row:
        raise LookupError(
            f"No '{subtype}' account found for business_id={business_id}. "
            f"Run seed_chart_of_accounts() first."
        )
    return row


def get_account_by_code(business_id: int, code: str):
    p = P()
    row = saas_fetchone(
        f"SELECT * FROM saas_chart_of_accounts WHERE business_id={p} AND code={p}",
        (business_id, code)
    )
    if not row:
        raise LookupError(f"No account with code={code} for business_id={business_id}.")
    return row


def get_or_create_party_account(business_id: int, party_type: str, party_id: int,
                                 party_name: str) -> dict:
    """
    Return (creating if necessary) the individual sub-ledger account for a
    specific customer or supplier. These are NOT part of the standard
    template — created lazily on first use, parented under the control
    account (Accounts Receivable / Accounts Payable).

    party_type: 'customer' | 'supplier'
    """
    if party_type not in ("customer", "supplier"):
        raise ValueError(f"party_type must be 'customer' or 'supplier', got: {party_type}")

    p = P()
    existing = saas_fetchone(
        f"""SELECT * FROM saas_chart_of_accounts
            WHERE business_id={p} AND party_type={p} AND party_id={p}""",
        (business_id, party_type, party_id)
    )
    if existing:
        return existing

    control_subtype = "accounts_receivable" if party_type == "customer" else "accounts_payable"
    control_acct     = get_account_by_subtype(business_id, control_subtype)
    acct_type        = "asset" if party_type == "customer" else "liability"

    # Generate a unique code scoped under the control account's block,
    # e.g. customers under 1200 become 1200-<party_id>, suppliers under
    # 2000 become 2000-<party_id>. Guaranteed unique since party_id is.
    code = f"{control_acct['code']}-{party_id}"

    acct_id = saas_execute(
        f"""INSERT INTO saas_chart_of_accounts
            (business_id, code, name, account_type, account_subtype,
             parent_id, party_type, party_id, is_system)
            VALUES ({p},{p},{p},{p},{p},{p},{p},{p},TRUE)""",
        (business_id, code, party_name, acct_type, control_subtype,
         control_acct["id"], party_type, party_id)
    )
    return saas_fetchone(f"SELECT * FROM saas_chart_of_accounts WHERE id={p}", (acct_id,))


def list_accounts(business_id: int, account_type: str = None, include_inactive: bool = False) -> list:
    """Return all accounts for a business, optionally filtered by type."""
    p = P()
    sql  = f"SELECT * FROM saas_chart_of_accounts WHERE business_id={p}"
    args = [business_id]
    if account_type:
        sql += f" AND account_type={p}"
        args.append(account_type)
    if not include_inactive:
        sql += " AND is_active=TRUE"
    sql += " ORDER BY code"
    return saas_fetchall(sql, tuple(args))
