"""
models/saas_business_data.py — SaaS-Native Business Operations Schema
========================================================================
Tenant-scoped tables for every ERP feature, rebuilt natively for the
SaaS multi-tenant system. Every table is keyed by business_id
(→ saas_businesses.id) instead of the legacy shop_id, and created_by
references saas_users.id instead of the legacy users.id.

This is a parallel schema to models/database.py — it does NOT touch
or migrate the legacy shop/users data. SaaS businesses get their own
fully isolated operational tables from day one.

Tables (13):
  saas_categories       — product categories, per business
  saas_products         — inventory items, per business
  saas_customers         — customer master, per business
  saas_invoices          — GST-compliant sales invoices
  saas_invoice_items     — per-line GST breakdown
  saas_payments          — invoice payment tracking
  saas_expenses          — business expenses
  saas_suppliers         — supplier master
  saas_purchases         — purchase bills
  saas_purchase_items    — per-line purchase GST breakdown
  saas_ledger            — universal double-entry-style ledger
  saas_cash_book         — cash receipts/payments register
  saas_bank_book         — bank account register

HSN master (hsn_master) remains global/shared — it's reference data,
not tenant data, so the existing legacy table is reused as-is via a
read-only helper in this module.

DB backend: same SQLite (dev) / PostgreSQL (prod) abstraction as
models/saas_auth.py, reusing its connection helpers.
"""

import os
from models.saas_auth import get_saas_db, _is_postgres, saas_fetchone, saas_fetchall, saas_execute


def init_saas_business_tables():
    """Create all SaaS business-data tables. Safe to call multiple times."""
    conn = get_saas_db()
    c = conn.cursor()

    if _is_postgres():
        _init_postgres(c)
    else:
        _init_sqlite(c)

    conn.commit()
    conn.close()
    print("[SaaS Business Data] Tables initialised.")


# ═══════════════════════════════ SQLITE SCHEMA ════════════════════════════════

def _init_sqlite(c):
    # ── saas_categories ────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_categories (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        name        TEXT    NOT NULL,
        created_at  TEXT    DEFAULT (datetime('now')),
        UNIQUE(name, business_id)
    )""")

    # ── saas_products ──────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_products (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id         INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        name                TEXT    NOT NULL,
        sku                 TEXT    DEFAULT '',
        category_id         INTEGER REFERENCES saas_categories(id),
        hsn_code            TEXT    DEFAULT '',
        gst_rate            REAL    NOT NULL DEFAULT 18,
        cost_price          REAL    NOT NULL DEFAULT 0,
        selling_price       REAL    NOT NULL DEFAULT 0,
        stock_quantity      INTEGER NOT NULL DEFAULT 0,
        low_stock_threshold INTEGER NOT NULL DEFAULT 5,
        barcode             TEXT    DEFAULT '',
        description         TEXT    DEFAULT '',
        is_active           INTEGER DEFAULT 1,
        created_at          TEXT    DEFAULT (datetime('now')),
        updated_at          TEXT    DEFAULT (datetime('now'))
    )""")

    # ── saas_customers ─────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_customers (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        name        TEXT    NOT NULL,
        phone       TEXT    DEFAULT '',
        email       TEXT    DEFAULT '',
        address     TEXT    DEFAULT '',
        state_code  TEXT    DEFAULT '',
        gstin       TEXT    DEFAULT '',
        created_at  TEXT    DEFAULT (datetime('now'))
    )""")

    # ── saas_invoices ───────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_invoices (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        invoice_number  TEXT    NOT NULL,
        customer_id     INTEGER REFERENCES saas_customers(id),
        customer_name   TEXT    DEFAULT 'Walk-in Customer',
        customer_gstin  TEXT    DEFAULT '',
        customer_state  TEXT    DEFAULT '',
        supply_type     TEXT    DEFAULT 'intra',
        subtotal        REAL    NOT NULL DEFAULT 0,
        discount        REAL    NOT NULL DEFAULT 0,
        discount_pct    REAL    NOT NULL DEFAULT 0,
        taxable_amount  REAL    NOT NULL DEFAULT 0,
        cgst_amount     REAL    NOT NULL DEFAULT 0,
        sgst_amount     REAL    NOT NULL DEFAULT 0,
        igst_amount     REAL    NOT NULL DEFAULT 0,
        total_tax       REAL    NOT NULL DEFAULT 0,
        total           REAL    NOT NULL DEFAULT 0,
        paid_amount     REAL    NOT NULL DEFAULT 0,
        due_amount      REAL    NOT NULL DEFAULT 0,
        payment_method  TEXT    DEFAULT 'Cash',
        place_of_supply TEXT    DEFAULT '',
        notes           TEXT    DEFAULT '',
        status          TEXT    DEFAULT 'paid',
        created_by      INTEGER REFERENCES saas_users(id),
        created_at      TEXT    DEFAULT (datetime('now')),
        UNIQUE(invoice_number, business_id)
    )""")

    # ── saas_invoice_items ──────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_invoice_items (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id     INTEGER NOT NULL REFERENCES saas_invoices(id) ON DELETE CASCADE,
        business_id    INTEGER NOT NULL REFERENCES saas_businesses(id),
        product_id     INTEGER REFERENCES saas_products(id),
        product_name   TEXT    NOT NULL,
        hsn_code       TEXT    DEFAULT '',
        quantity       REAL    NOT NULL DEFAULT 1,
        unit_price     REAL    NOT NULL DEFAULT 0,
        discount       REAL    NOT NULL DEFAULT 0,
        taxable_amount REAL    NOT NULL DEFAULT 0,
        gst_rate       REAL    NOT NULL DEFAULT 0,
        cgst_rate      REAL    NOT NULL DEFAULT 0,
        sgst_rate      REAL    NOT NULL DEFAULT 0,
        igst_rate      REAL    NOT NULL DEFAULT 0,
        cgst_amount    REAL    NOT NULL DEFAULT 0,
        sgst_amount    REAL    NOT NULL DEFAULT 0,
        igst_amount    REAL    NOT NULL DEFAULT 0,
        total_price    REAL    NOT NULL DEFAULT 0
    )""")

    # ── saas_payments ───────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_payments (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id    INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        invoice_id     INTEGER NOT NULL REFERENCES saas_invoices(id) ON DELETE CASCADE,
        invoice_number TEXT    DEFAULT '',
        customer_id    INTEGER REFERENCES saas_customers(id),
        customer_name  TEXT    DEFAULT '',
        amount         REAL    NOT NULL DEFAULT 0,
        payment_method TEXT    DEFAULT 'Cash',
        payment_date   TEXT    DEFAULT (date('now')),
        reference      TEXT    DEFAULT '',
        notes          TEXT    DEFAULT '',
        created_by     INTEGER REFERENCES saas_users(id),
        created_at     TEXT    DEFAULT (datetime('now'))
    )""")

    # ── saas_expenses ───────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_expenses (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id  INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        category     TEXT    NOT NULL,
        description  TEXT    DEFAULT '',
        amount       REAL    NOT NULL DEFAULT 0,
        expense_date TEXT    DEFAULT (date('now')),
        created_by   INTEGER REFERENCES saas_users(id),
        created_at   TEXT    DEFAULT (datetime('now'))
    )""")

    # ── saas_suppliers ──────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_suppliers (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        name            TEXT    NOT NULL,
        phone           TEXT    DEFAULT '',
        email           TEXT    DEFAULT '',
        address         TEXT    DEFAULT '',
        gstin           TEXT    DEFAULT '',
        state_code      TEXT    DEFAULT '',
        opening_balance REAL    DEFAULT 0,
        balance         REAL    DEFAULT 0,
        is_active       INTEGER DEFAULT 1,
        created_at      TEXT    DEFAULT (datetime('now'))
    )""")

    # ── saas_purchases ──────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_purchases (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id      INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        purchase_number  TEXT    NOT NULL,
        supplier_id      INTEGER REFERENCES saas_suppliers(id),
        supplier_name    TEXT    DEFAULT '',
        supplier_gstin   TEXT    DEFAULT '',
        bill_number      TEXT    DEFAULT '',
        bill_date        TEXT    DEFAULT (date('now')),
        subtotal         REAL    NOT NULL DEFAULT 0,
        discount         REAL    NOT NULL DEFAULT 0,
        discount_pct     REAL    NOT NULL DEFAULT 0,
        taxable_amount   REAL    NOT NULL DEFAULT 0,
        cgst_amount      REAL    NOT NULL DEFAULT 0,
        sgst_amount      REAL    NOT NULL DEFAULT 0,
        igst_amount      REAL    NOT NULL DEFAULT 0,
        total_tax        REAL    NOT NULL DEFAULT 0,
        total            REAL    NOT NULL DEFAULT 0,
        paid_amount      REAL    NOT NULL DEFAULT 0,
        due_amount       REAL    NOT NULL DEFAULT 0,
        payment_method   TEXT    DEFAULT 'Cash',
        supply_type      TEXT    DEFAULT 'intra',
        notes            TEXT    DEFAULT '',
        status           TEXT    DEFAULT 'received',
        created_by       INTEGER REFERENCES saas_users(id),
        created_at       TEXT    DEFAULT (datetime('now')),
        UNIQUE(purchase_number, business_id)
    )""")

    # ── saas_purchase_items ─────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_purchase_items (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_id     INTEGER NOT NULL REFERENCES saas_purchases(id) ON DELETE CASCADE,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id),
        product_id      INTEGER REFERENCES saas_products(id),
        product_name    TEXT    NOT NULL,
        hsn_code        TEXT    DEFAULT '',
        quantity        REAL    NOT NULL DEFAULT 1,
        unit_price      REAL    NOT NULL DEFAULT 0,
        taxable_amount  REAL    NOT NULL DEFAULT 0,
        gst_rate        REAL    NOT NULL DEFAULT 0,
        cgst_rate       REAL    NOT NULL DEFAULT 0,
        sgst_rate       REAL    NOT NULL DEFAULT 0,
        igst_rate       REAL    NOT NULL DEFAULT 0,
        cgst_amount     REAL    NOT NULL DEFAULT 0,
        sgst_amount     REAL    NOT NULL DEFAULT 0,
        igst_amount     REAL    NOT NULL DEFAULT 0,
        total_price     REAL    NOT NULL DEFAULT 0
    )""")

    # ── saas_ledger ──────────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_ledger (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        party_type  TEXT    NOT NULL,
        party_id    INTEGER,
        party_name  TEXT    DEFAULT '',
        txn_type    TEXT    NOT NULL,
        ref_type    TEXT    DEFAULT '',
        ref_id      INTEGER DEFAULT 0,
        ref_number  TEXT    DEFAULT '',
        debit       REAL    NOT NULL DEFAULT 0,
        credit      REAL    NOT NULL DEFAULT 0,
        balance     REAL    NOT NULL DEFAULT 0,
        narration   TEXT    DEFAULT '',
        txn_date    TEXT    DEFAULT (date('now')),
        created_by  INTEGER REFERENCES saas_users(id),
        created_at  TEXT    DEFAULT (datetime('now'))
    )""")

    # ── saas_cash_book ───────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_cash_book (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        txn_date    TEXT    NOT NULL DEFAULT (date('now')),
        txn_type    TEXT    NOT NULL,
        category    TEXT    DEFAULT '',
        description TEXT    DEFAULT '',
        ref_type    TEXT    DEFAULT '',
        ref_id      INTEGER DEFAULT 0,
        amount      REAL    NOT NULL DEFAULT 0,
        balance     REAL    NOT NULL DEFAULT 0,
        created_by  INTEGER REFERENCES saas_users(id),
        created_at  TEXT    DEFAULT (datetime('now'))
    )""")

    # ── saas_bank_book ───────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_bank_book (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id  INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        account_name TEXT    DEFAULT 'Main Account',
        txn_date     TEXT    NOT NULL DEFAULT (date('now')),
        txn_type     TEXT    NOT NULL,
        description  TEXT    DEFAULT '',
        ref_number   TEXT    DEFAULT '',
        amount       REAL    NOT NULL DEFAULT 0,
        balance      REAL    NOT NULL DEFAULT 0,
        created_by   INTEGER REFERENCES saas_users(id),
        created_at   TEXT    DEFAULT (datetime('now'))
    )""")

    # ── Indexes ───────────────────────────────────────────────────────────────────
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_saas_categories_biz   ON saas_categories(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_products_biz     ON saas_products(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_products_cat     ON saas_products(category_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_customers_biz    ON saas_customers(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invoices_biz     ON saas_invoices(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invoices_cust    ON saas_invoices(customer_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invitems_inv     ON saas_invoice_items(invoice_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invitems_biz     ON saas_invoice_items(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_payments_biz     ON saas_payments(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_payments_inv     ON saas_payments(invoice_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_expenses_biz     ON saas_expenses(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_suppliers_biz    ON saas_suppliers(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_purchases_biz    ON saas_purchases(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_purchases_sup    ON saas_purchases(supplier_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_purchitems_pur   ON saas_purchase_items(purchase_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_purchitems_biz   ON saas_purchase_items(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_ledger_biz       ON saas_ledger(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_ledger_party     ON saas_ledger(party_type, party_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_cashbook_biz     ON saas_cash_book(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_bankbook_biz     ON saas_bank_book(business_id)",
    ]
    for idx in indexes:
        c.execute(idx)


# ═══════════════════════════════ POSTGRESQL SCHEMA ════════════════════════════

def _init_postgres(c):
    c.execute("""CREATE TABLE IF NOT EXISTS saas_categories (
        id          SERIAL PRIMARY KEY,
        business_id INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        name        VARCHAR(200) NOT NULL,
        created_at  TIMESTAMP DEFAULT NOW(),
        UNIQUE(name, business_id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_products (
        id                  SERIAL PRIMARY KEY,
        business_id         INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        name                VARCHAR(300) NOT NULL,
        sku                 VARCHAR(100) DEFAULT '',
        category_id         INTEGER REFERENCES saas_categories(id),
        hsn_code            VARCHAR(20)  DEFAULT '',
        gst_rate            NUMERIC(5,2) NOT NULL DEFAULT 18,
        cost_price          NUMERIC(12,2) NOT NULL DEFAULT 0,
        selling_price       NUMERIC(12,2) NOT NULL DEFAULT 0,
        stock_quantity      INTEGER NOT NULL DEFAULT 0,
        low_stock_threshold INTEGER NOT NULL DEFAULT 5,
        barcode             VARCHAR(100) DEFAULT '',
        description         TEXT DEFAULT '',
        is_active           BOOLEAN DEFAULT TRUE,
        created_at          TIMESTAMP DEFAULT NOW(),
        updated_at          TIMESTAMP DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_customers (
        id          SERIAL PRIMARY KEY,
        business_id INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        name        VARCHAR(300) NOT NULL,
        phone       VARCHAR(20)  DEFAULT '',
        email       VARCHAR(255) DEFAULT '',
        address     TEXT DEFAULT '',
        state_code  VARCHAR(5)   DEFAULT '',
        gstin       VARCHAR(20)  DEFAULT '',
        created_at  TIMESTAMP DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_invoices (
        id              SERIAL PRIMARY KEY,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        invoice_number  VARCHAR(50) NOT NULL,
        customer_id     INTEGER REFERENCES saas_customers(id),
        customer_name   VARCHAR(300) DEFAULT 'Walk-in Customer',
        customer_gstin  VARCHAR(20)  DEFAULT '',
        customer_state  VARCHAR(50)  DEFAULT '',
        supply_type     VARCHAR(20)  DEFAULT 'intra',
        subtotal        NUMERIC(12,2) NOT NULL DEFAULT 0,
        discount        NUMERIC(12,2) NOT NULL DEFAULT 0,
        discount_pct    NUMERIC(5,2)  NOT NULL DEFAULT 0,
        taxable_amount  NUMERIC(12,2) NOT NULL DEFAULT 0,
        cgst_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
        sgst_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
        igst_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
        total_tax       NUMERIC(12,2) NOT NULL DEFAULT 0,
        total           NUMERIC(12,2) NOT NULL DEFAULT 0,
        paid_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
        due_amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
        payment_method  VARCHAR(30) DEFAULT 'Cash',
        place_of_supply VARCHAR(50) DEFAULT '',
        notes           TEXT DEFAULT '',
        status          VARCHAR(20) DEFAULT 'paid',
        created_by      INTEGER REFERENCES saas_users(id),
        created_at      TIMESTAMP DEFAULT NOW(),
        UNIQUE(invoice_number, business_id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_invoice_items (
        id             SERIAL PRIMARY KEY,
        invoice_id     INTEGER NOT NULL REFERENCES saas_invoices(id) ON DELETE CASCADE,
        business_id    INTEGER NOT NULL REFERENCES saas_businesses(id),
        product_id     INTEGER REFERENCES saas_products(id),
        product_name   VARCHAR(300) NOT NULL,
        hsn_code       VARCHAR(20) DEFAULT '',
        quantity       NUMERIC(12,3) NOT NULL DEFAULT 1,
        unit_price     NUMERIC(12,2) NOT NULL DEFAULT 0,
        discount       NUMERIC(12,2) NOT NULL DEFAULT 0,
        taxable_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
        gst_rate       NUMERIC(5,2)  NOT NULL DEFAULT 0,
        cgst_rate      NUMERIC(5,2)  NOT NULL DEFAULT 0,
        sgst_rate      NUMERIC(5,2)  NOT NULL DEFAULT 0,
        igst_rate      NUMERIC(5,2)  NOT NULL DEFAULT 0,
        cgst_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
        sgst_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
        igst_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
        total_price    NUMERIC(12,2) NOT NULL DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_payments (
        id             SERIAL PRIMARY KEY,
        business_id    INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        invoice_id     INTEGER NOT NULL REFERENCES saas_invoices(id) ON DELETE CASCADE,
        invoice_number VARCHAR(50) DEFAULT '',
        customer_id    INTEGER REFERENCES saas_customers(id),
        customer_name  VARCHAR(300) DEFAULT '',
        amount         NUMERIC(12,2) NOT NULL DEFAULT 0,
        payment_method VARCHAR(30) DEFAULT 'Cash',
        payment_date   DATE DEFAULT CURRENT_DATE,
        reference      VARCHAR(100) DEFAULT '',
        notes          TEXT DEFAULT '',
        created_by     INTEGER REFERENCES saas_users(id),
        created_at     TIMESTAMP DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_expenses (
        id           SERIAL PRIMARY KEY,
        business_id  INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        category     VARCHAR(100) NOT NULL,
        description  TEXT DEFAULT '',
        amount       NUMERIC(12,2) NOT NULL DEFAULT 0,
        expense_date DATE DEFAULT CURRENT_DATE,
        created_by   INTEGER REFERENCES saas_users(id),
        created_at   TIMESTAMP DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_suppliers (
        id              SERIAL PRIMARY KEY,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        name            VARCHAR(300) NOT NULL,
        phone           VARCHAR(20)  DEFAULT '',
        email           VARCHAR(255) DEFAULT '',
        address         TEXT DEFAULT '',
        gstin           VARCHAR(20)  DEFAULT '',
        state_code      VARCHAR(5)   DEFAULT '',
        opening_balance NUMERIC(12,2) DEFAULT 0,
        balance         NUMERIC(12,2) DEFAULT 0,
        is_active       BOOLEAN DEFAULT TRUE,
        created_at      TIMESTAMP DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_purchases (
        id               SERIAL PRIMARY KEY,
        business_id      INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        purchase_number  VARCHAR(50) NOT NULL,
        supplier_id      INTEGER REFERENCES saas_suppliers(id),
        supplier_name    VARCHAR(300) DEFAULT '',
        supplier_gstin   VARCHAR(20)  DEFAULT '',
        bill_number      VARCHAR(100) DEFAULT '',
        bill_date        DATE DEFAULT CURRENT_DATE,
        subtotal         NUMERIC(12,2) NOT NULL DEFAULT 0,
        discount         NUMERIC(12,2) NOT NULL DEFAULT 0,
        discount_pct     NUMERIC(5,2)  NOT NULL DEFAULT 0,
        taxable_amount   NUMERIC(12,2) NOT NULL DEFAULT 0,
        cgst_amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
        sgst_amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
        igst_amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
        total_tax        NUMERIC(12,2) NOT NULL DEFAULT 0,
        total            NUMERIC(12,2) NOT NULL DEFAULT 0,
        paid_amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
        due_amount       NUMERIC(12,2) NOT NULL DEFAULT 0,
        payment_method   VARCHAR(30) DEFAULT 'Cash',
        supply_type      VARCHAR(20) DEFAULT 'intra',
        notes            TEXT DEFAULT '',
        status           VARCHAR(20) DEFAULT 'received',
        created_by       INTEGER REFERENCES saas_users(id),
        created_at       TIMESTAMP DEFAULT NOW(),
        UNIQUE(purchase_number, business_id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_purchase_items (
        id              SERIAL PRIMARY KEY,
        purchase_id     INTEGER NOT NULL REFERENCES saas_purchases(id) ON DELETE CASCADE,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id),
        product_id      INTEGER REFERENCES saas_products(id),
        product_name    VARCHAR(300) NOT NULL,
        hsn_code        VARCHAR(20) DEFAULT '',
        quantity        NUMERIC(12,3) NOT NULL DEFAULT 1,
        unit_price      NUMERIC(12,2) NOT NULL DEFAULT 0,
        taxable_amount  NUMERIC(12,2) NOT NULL DEFAULT 0,
        gst_rate        NUMERIC(5,2)  NOT NULL DEFAULT 0,
        cgst_rate       NUMERIC(5,2)  NOT NULL DEFAULT 0,
        sgst_rate       NUMERIC(5,2)  NOT NULL DEFAULT 0,
        igst_rate       NUMERIC(5,2)  NOT NULL DEFAULT 0,
        cgst_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
        sgst_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
        igst_amount     NUMERIC(12,2) NOT NULL DEFAULT 0,
        total_price     NUMERIC(12,2) NOT NULL DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_ledger (
        id          SERIAL PRIMARY KEY,
        business_id INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        party_type  VARCHAR(20) NOT NULL,
        party_id    INTEGER,
        party_name  VARCHAR(300) DEFAULT '',
        txn_type    VARCHAR(30) NOT NULL,
        ref_type    VARCHAR(30) DEFAULT '',
        ref_id      INTEGER DEFAULT 0,
        ref_number  VARCHAR(50) DEFAULT '',
        debit       NUMERIC(12,2) NOT NULL DEFAULT 0,
        credit      NUMERIC(12,2) NOT NULL DEFAULT 0,
        balance     NUMERIC(12,2) NOT NULL DEFAULT 0,
        narration   TEXT DEFAULT '',
        txn_date    DATE DEFAULT CURRENT_DATE,
        created_by  INTEGER REFERENCES saas_users(id),
        created_at  TIMESTAMP DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_cash_book (
        id          SERIAL PRIMARY KEY,
        business_id INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        txn_date    DATE NOT NULL DEFAULT CURRENT_DATE,
        txn_type    VARCHAR(20) NOT NULL,
        category    VARCHAR(50) DEFAULT '',
        description TEXT DEFAULT '',
        ref_type    VARCHAR(30) DEFAULT '',
        ref_id      INTEGER DEFAULT 0,
        amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
        balance     NUMERIC(12,2) NOT NULL DEFAULT 0,
        created_by  INTEGER REFERENCES saas_users(id),
        created_at  TIMESTAMP DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_bank_book (
        id           SERIAL PRIMARY KEY,
        business_id  INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        account_name VARCHAR(200) DEFAULT 'Main Account',
        txn_date     DATE NOT NULL DEFAULT CURRENT_DATE,
        txn_type     VARCHAR(20) NOT NULL,
        description  TEXT DEFAULT '',
        ref_number   VARCHAR(100) DEFAULT '',
        amount       NUMERIC(12,2) NOT NULL DEFAULT 0,
        balance      NUMERIC(12,2) NOT NULL DEFAULT 0,
        created_by   INTEGER REFERENCES saas_users(id),
        created_at   TIMESTAMP DEFAULT NOW()
    )""")

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_saas_categories_biz   ON saas_categories(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_products_biz     ON saas_products(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_products_cat     ON saas_products(category_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_customers_biz    ON saas_customers(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invoices_biz     ON saas_invoices(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invoices_cust    ON saas_invoices(customer_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invitems_inv     ON saas_invoice_items(invoice_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invitems_biz     ON saas_invoice_items(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_payments_biz     ON saas_payments(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_payments_inv     ON saas_payments(invoice_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_expenses_biz     ON saas_expenses(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_suppliers_biz    ON saas_suppliers(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_purchases_biz    ON saas_purchases(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_purchases_sup    ON saas_purchases(supplier_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_purchitems_pur   ON saas_purchase_items(purchase_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_purchitems_biz   ON saas_purchase_items(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_ledger_biz       ON saas_ledger(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_ledger_party     ON saas_ledger(party_type, party_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_cashbook_biz     ON saas_cash_book(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_bankbook_biz     ON saas_bank_book(business_id)",
    ]
    for idx in indexes:
        c.execute(idx)


# ═══════════════════════════════ SHARED QUERY HELPERS ═════════════════════════
# Thin re-exports so modules can `from models.saas_business_data import ...`
# without also needing to import models.saas_auth directly.

def P():
    return "%s" if _is_postgres() else "?"


def get_hsn_master(search: str = "") -> list:
    """
    HSN codes remain a GLOBAL reference table (not tenant-scoped) —
    reused from the legacy SQLite database, read-only.
    """
    from models.database import get_db
    conn = get_db()
    try:
        if search:
            rows = conn.execute(
                "SELECT * FROM hsn_master WHERE hsn_code LIKE ? OR description LIKE ? "
                "ORDER BY hsn_code LIMIT 50",
                (f"%{search}%", f"%{search}%")
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM hsn_master ORDER BY hsn_code LIMIT 100"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[saas_business_data] HSN lookup failed: {e}")
        return []
    finally:
        conn.close()
