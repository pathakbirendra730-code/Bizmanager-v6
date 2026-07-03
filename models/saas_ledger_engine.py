"""
models/saas_ledger_engine.py — Double-Entry Accounting Schema & Core
========================================================================
This is the foundation of the double-entry accounting engine. It defines:

  • saas_chart_of_accounts  — every account a business can post to
  • saas_journal_entries    — transaction headers (one per business event)
  • saas_journal_lines      — individual debit/credit lines (≥2 per entry,
                               always balanced: sum(debit) == sum(credit))
  • saas_account_balances   — running balance cache per account, updated
                               atomically alongside every journal posting
                               (avoids re-summing the whole journal table
                               on every report page load)

Design principles:
  1. EVERY financial transaction becomes a journal entry with ≥2 balanced
     lines. There is no other way to move money in this system once this
     engine is wired in — Cash Book, Bank Book, Customer/Supplier Ledger,
     Trial Balance, P&L, and Balance Sheet are all just FILTERED VIEWS
     over saas_journal_lines, not separate sources of truth.
  2. Atomicity: posting a journal entry (header + all lines + balance
     updates) happens on ONE connection inside ONE transaction. If any
     line fails to validate or insert, the entire posting rolls back —
     never a half-written journal entry.
  3. Reversal, not deletion: a posted journal entry is never UPDATEd or
     DELETEd once committed. Corrections post a new REVERSING entry that
     exactly negates the original, preserving a full audit trail. This
     mirrors how real accounting systems (and auditors) require books to
     work — you can't quietly edit history.
  4. Account balances follow the standard accounting equation sign
     convention:
       Assets & Expenses   → normal balance is DEBIT  (debit increases)
       Liabilities, Equity,
       Income/Revenue       → normal balance is CREDIT (credit increases)
     saas_account_balances.balance is always stored in "natural" terms
     for that account type (a positive number means "more of what's
     normal" for that account), computed as:
       for debit-normal accounts:  balance = total_debit - total_credit
       for credit-normal accounts: balance = total_credit - total_debit

Tenant scoping: every table here is keyed by business_id, identical to
every other SaaS-native table in this system. Multi-tenant isolation is
enforced the same way — callers must always filter by business_id, and
the service layer (utils/ledger_service.py) does this automatically.
"""

import os
import json
import contextlib
from datetime import datetime
from models.saas_auth import get_saas_db, _is_postgres


# ═══════════════════════════════ ACCOUNT TYPES ════════════════════════════════
# The five fundamental account types, each with a "normal balance" side.
# This drives both validation (you can't post a type that doesn't exist)
# and report classification (which section of the Balance Sheet / P&L an
# account belongs in).

ACCOUNT_TYPES = {
    "asset":     {"normal_balance": "debit",  "statement": "balance_sheet"},
    "liability": {"normal_balance": "credit", "statement": "balance_sheet"},
    "equity":    {"normal_balance": "credit", "statement": "balance_sheet"},
    "income":    {"normal_balance": "credit", "statement": "profit_loss"},
    "expense":   {"normal_balance": "debit",  "statement": "profit_loss"},
}

# Standard account subtype tags, used for report grouping and for the
# posting service to find "the" cash account, "the" sales account, etc.
# without hardcoding account IDs (since each business has its own COA rows).
ACCOUNT_SUBTYPES = [
    # Assets
    "cash", "bank", "accounts_receivable", "inventory", "fixed_asset", "other_asset",
    # Liabilities
    "accounts_payable", "gst_payable", "other_liability",
    # Equity
    "owner_equity", "retained_earnings",
    # Income
    "sales_revenue", "other_income",
    # Expense
    "cogs", "operating_expense", "gst_input_credit", "discount_given", "returns_expense",
]


def P():
    return "%s" if _is_postgres() else "?"


# ═══════════════════════════════ TRANSACTIONAL CORE ═══════════════════════════

@contextlib.contextmanager
def ledger_transaction():
    """
    Context manager giving ONE connection + cursor for the lifetime of a
    multi-statement posting operation. Commits on clean exit, rolls back
    and re-raises on any exception — this is what makes journal postings
    atomic (header + lines + balance updates all succeed or all fail).

    Usage:
        with ledger_transaction() as (conn, c, p):
            c.execute(f"INSERT INTO saas_journal_entries (...) VALUES ({p}...)", (...))
            entry_id = c.lastrowid
            for line in lines:
                c.execute(f"INSERT INTO saas_journal_lines (...) VALUES ({p}...)", (...))
            # no explicit commit needed — happens automatically on exit
    """
    conn = get_saas_db()
    c = conn.cursor()
    p = P()
    try:
        yield conn, c, p
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ═══════════════════════════════ SCHEMA CREATION ══════════════════════════════

def init_ledger_engine_tables():
    """Create all double-entry accounting tables. Safe to call multiple times."""
    conn = get_saas_db()
    c = conn.cursor()

    if _is_postgres():
        _init_postgres(c)
    else:
        _init_sqlite(c)

    conn.commit()
    conn.close()
    print("[Ledger Engine] Double-entry accounting tables initialised.")


def _init_sqlite(c):
    # ── Chart of Accounts ────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_chart_of_accounts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        code            TEXT    NOT NULL,           -- e.g. '1000', '2100'
        name            TEXT    NOT NULL,            -- e.g. 'Cash', 'Accounts Payable'
        account_type    TEXT    NOT NULL,            -- asset|liability|equity|income|expense
        account_subtype TEXT    NOT NULL DEFAULT '', -- cash|bank|accounts_receivable|...
        parent_id       INTEGER REFERENCES saas_chart_of_accounts(id),
        party_type      TEXT    DEFAULT '',          -- 'customer'|'supplier'|'' for control accounts
        party_id        INTEGER DEFAULT NULL,        -- links a sub-ledger account to one specific party
        is_system       INTEGER NOT NULL DEFAULT 0,  -- 1 = auto-created, cannot be deleted via UI
        is_active       INTEGER NOT NULL DEFAULT 1,
        description     TEXT    DEFAULT '',
        created_at      TEXT    DEFAULT (datetime('now')),
        UNIQUE(business_id, code)
    )""")

    # ── Journal Entries (transaction headers) ───────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_journal_entries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        entry_number    TEXT    NOT NULL,            -- JE-1001, JE-1002, ...
        entry_date      TEXT    NOT NULL DEFAULT (date('now')),
        source_type     TEXT    NOT NULL,            -- 'sale'|'purchase'|'payment_in'|...
        source_id       INTEGER DEFAULT NULL,        -- FK to saas_invoices.id etc, when applicable
        narration       TEXT    DEFAULT '',
        total_debit     REAL    NOT NULL DEFAULT 0,  -- cached sum, must equal total_credit
        total_credit    REAL    NOT NULL DEFAULT 0,
        status          TEXT    NOT NULL DEFAULT 'posted',  -- posted|reversed
        reversed_by     INTEGER DEFAULT NULL REFERENCES saas_journal_entries(id),
        reverses        INTEGER DEFAULT NULL REFERENCES saas_journal_entries(id),
        created_by      INTEGER REFERENCES saas_users(id),
        created_at      TEXT    DEFAULT (datetime('now')),
        UNIQUE(business_id, entry_number)
    )""")

    # ── Journal Lines (the actual debit/credit postings) ────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_journal_lines (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        entry_id        INTEGER NOT NULL REFERENCES saas_journal_entries(id) ON DELETE CASCADE,
        account_id      INTEGER NOT NULL REFERENCES saas_chart_of_accounts(id),
        debit           REAL    NOT NULL DEFAULT 0,
        credit          REAL    NOT NULL DEFAULT 0,
        party_type      TEXT    DEFAULT '',          -- denormalised from account for fast filtering
        party_id        INTEGER DEFAULT NULL,
        description     TEXT    DEFAULT '',
        line_order      INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT    DEFAULT (datetime('now'))
    )""")

    # ── Account balance cache (one row per account, updated on every post) ──
    c.execute("""CREATE TABLE IF NOT EXISTS saas_account_balances (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        account_id      INTEGER NOT NULL REFERENCES saas_chart_of_accounts(id),
        total_debit     REAL    NOT NULL DEFAULT 0,
        total_credit    REAL    NOT NULL DEFAULT 0,
        balance         REAL    NOT NULL DEFAULT 0,  -- in natural terms (see module docstring)
        updated_at      TEXT    DEFAULT (datetime('now')),
        UNIQUE(business_id, account_id)
    )""")

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_coa_biz        ON saas_chart_of_accounts(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_coa_type        ON saas_chart_of_accounts(business_id, account_type)",
        "CREATE INDEX IF NOT EXISTS idx_coa_subtype     ON saas_chart_of_accounts(business_id, account_subtype)",
        "CREATE INDEX IF NOT EXISTS idx_coa_party       ON saas_chart_of_accounts(business_id, party_type, party_id)",
        "CREATE INDEX IF NOT EXISTS idx_je_biz          ON saas_journal_entries(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_je_date         ON saas_journal_entries(business_id, entry_date)",
        "CREATE INDEX IF NOT EXISTS idx_je_source       ON saas_journal_entries(business_id, source_type, source_id)",
        "CREATE INDEX IF NOT EXISTS idx_jl_biz          ON saas_journal_lines(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_jl_entry        ON saas_journal_lines(entry_id)",
        "CREATE INDEX IF NOT EXISTS idx_jl_account      ON saas_journal_lines(business_id, account_id)",
        "CREATE INDEX IF NOT EXISTS idx_jl_party        ON saas_journal_lines(business_id, party_type, party_id)",
        "CREATE INDEX IF NOT EXISTS idx_bal_biz_acct    ON saas_account_balances(business_id, account_id)",
    ]
    for idx in indexes:
        c.execute(idx)


def _init_postgres(c):
    c.execute("""CREATE TABLE IF NOT EXISTS saas_chart_of_accounts (
        id              SERIAL PRIMARY KEY,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        code            VARCHAR(20)  NOT NULL,
        name            VARCHAR(200) NOT NULL,
        account_type    VARCHAR(20)  NOT NULL,
        account_subtype VARCHAR(40)  NOT NULL DEFAULT '',
        parent_id       INTEGER REFERENCES saas_chart_of_accounts(id),
        party_type      VARCHAR(20)  DEFAULT '',
        party_id        INTEGER DEFAULT NULL,
        is_system       BOOLEAN NOT NULL DEFAULT FALSE,
        is_active       BOOLEAN NOT NULL DEFAULT TRUE,
        description     TEXT DEFAULT '',
        created_at      TIMESTAMP DEFAULT NOW(),
        UNIQUE(business_id, code)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_journal_entries (
        id              SERIAL PRIMARY KEY,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        entry_number    VARCHAR(50) NOT NULL,
        entry_date      DATE NOT NULL DEFAULT CURRENT_DATE,
        source_type     VARCHAR(30) NOT NULL,
        source_id       INTEGER DEFAULT NULL,
        narration       TEXT DEFAULT '',
        total_debit     NUMERIC(14,2) NOT NULL DEFAULT 0,
        total_credit    NUMERIC(14,2) NOT NULL DEFAULT 0,
        status          VARCHAR(20) NOT NULL DEFAULT 'posted',
        reversed_by     INTEGER DEFAULT NULL REFERENCES saas_journal_entries(id),
        reverses        INTEGER DEFAULT NULL REFERENCES saas_journal_entries(id),
        created_by      INTEGER REFERENCES saas_users(id),
        created_at      TIMESTAMP DEFAULT NOW(),
        UNIQUE(business_id, entry_number)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_journal_lines (
        id              SERIAL PRIMARY KEY,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        entry_id        INTEGER NOT NULL REFERENCES saas_journal_entries(id) ON DELETE CASCADE,
        account_id      INTEGER NOT NULL REFERENCES saas_chart_of_accounts(id),
        debit           NUMERIC(14,2) NOT NULL DEFAULT 0,
        credit          NUMERIC(14,2) NOT NULL DEFAULT 0,
        party_type      VARCHAR(20) DEFAULT '',
        party_id        INTEGER DEFAULT NULL,
        description     TEXT DEFAULT '',
        line_order      INTEGER NOT NULL DEFAULT 0,
        created_at      TIMESTAMP DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_account_balances (
        id              SERIAL PRIMARY KEY,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        account_id      INTEGER NOT NULL REFERENCES saas_chart_of_accounts(id),
        total_debit     NUMERIC(14,2) NOT NULL DEFAULT 0,
        total_credit    NUMERIC(14,2) NOT NULL DEFAULT 0,
        balance         NUMERIC(14,2) NOT NULL DEFAULT 0,
        updated_at      TIMESTAMP DEFAULT NOW(),
        UNIQUE(business_id, account_id)
    )""")

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_coa_biz        ON saas_chart_of_accounts(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_coa_type        ON saas_chart_of_accounts(business_id, account_type)",
        "CREATE INDEX IF NOT EXISTS idx_coa_subtype     ON saas_chart_of_accounts(business_id, account_subtype)",
        "CREATE INDEX IF NOT EXISTS idx_coa_party       ON saas_chart_of_accounts(business_id, party_type, party_id)",
        "CREATE INDEX IF NOT EXISTS idx_je_biz          ON saas_journal_entries(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_je_date         ON saas_journal_entries(business_id, entry_date)",
        "CREATE INDEX IF NOT EXISTS idx_je_source       ON saas_journal_entries(business_id, source_type, source_id)",
        "CREATE INDEX IF NOT EXISTS idx_jl_biz          ON saas_journal_lines(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_jl_entry        ON saas_journal_lines(entry_id)",
        "CREATE INDEX IF NOT EXISTS idx_jl_account      ON saas_journal_lines(business_id, account_id)",
        "CREATE INDEX IF NOT EXISTS idx_jl_party        ON saas_journal_lines(business_id, party_type, party_id)",
        "CREATE INDEX IF NOT EXISTS idx_bal_biz_acct    ON saas_account_balances(business_id, account_id)",
    ]
    for idx in indexes:
        c.execute(idx)
