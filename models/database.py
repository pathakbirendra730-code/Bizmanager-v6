"""
models/database.py  — BizManager Multi-Shop ERP
================================================
• Creates ALL tables on first run (safe IF NOT EXISTS)
• Runs column-level migrations for existing DBs (backward compatible)
• Seeds superadmin + 2 demo shops with full sample data
• 60+ HSN codes seeded for Indian GST compliance
"""

import sqlite3, os, random
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash

# late import to avoid circular; resolved before any call
def _db_path():
    from config import ActiveConfig
    return ActiveConfig.DB_PATH


def get_db():
    """Return a WAL-mode SQLite connection with row_factory."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ═══════════════════════════════════════════ SCHEMA ═══════════════════════════

def init_db():
    conn = get_db()
    c = conn.cursor()

    # ── shops ──────────────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS shops (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT    NOT NULL,
        gstin           TEXT    DEFAULT '',
        address         TEXT    DEFAULT '',
        phone           TEXT    DEFAULT '',
        email           TEXT    DEFAULT '',
        state_code      TEXT    DEFAULT '27',
        city            TEXT    DEFAULT '',
        pincode         TEXT    DEFAULT '',
        invoice_prefix  TEXT    DEFAULT 'INV',
        business_type   TEXT    DEFAULT 'general',
        is_template     INTEGER DEFAULT 0,
        is_active       INTEGER DEFAULT 1,
        created_at      TEXT    DEFAULT (datetime('now')),
        updated_at      TEXT    DEFAULT (datetime('now'))
    )""")

    # ── users ─────────────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT    NOT NULL UNIQUE,
        password_hash TEXT    NOT NULL,
        role          TEXT    NOT NULL DEFAULT 'staff',
        full_name     TEXT    DEFAULT '',
        shop_id       INTEGER REFERENCES shops(id) ON DELETE SET NULL,
        email         TEXT    DEFAULT '',
        phone         TEXT    DEFAULT '',
        is_active     INTEGER DEFAULT 1,
        last_login    TEXT,
        created_at    TEXT    DEFAULT (datetime('now'))
    )""")

    # ── HSN master (global) ────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS hsn_master (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        hsn_code         TEXT NOT NULL UNIQUE,
        description      TEXT NOT NULL,
        default_gst_rate REAL NOT NULL DEFAULT 18,
        category         TEXT DEFAULT ''
    )""")

    # ── categories (per-shop) ─────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS categories (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        name    TEXT NOT NULL,
        shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
        UNIQUE(name, shop_id)
    )""")

    # ── products (per-shop, with HSN) ──────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS products (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id             INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
        name                TEXT NOT NULL,
        sku                 TEXT DEFAULT '',
        category_id         INTEGER REFERENCES categories(id),
        hsn_code            TEXT DEFAULT '',
        gst_rate            REAL NOT NULL DEFAULT 18,
        cost_price          REAL NOT NULL DEFAULT 0,
        selling_price       REAL NOT NULL DEFAULT 0,
        stock_quantity      INTEGER NOT NULL DEFAULT 0,
        low_stock_threshold INTEGER NOT NULL DEFAULT 5,
        barcode             TEXT DEFAULT '',
        description         TEXT DEFAULT '',
        is_active           INTEGER DEFAULT 1,
        created_at          TEXT DEFAULT (datetime('now')),
        updated_at          TEXT DEFAULT (datetime('now'))
    )""")

    # ── customers (per-shop) ──────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS customers (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id    INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        phone      TEXT DEFAULT '',
        email      TEXT DEFAULT '',
        address    TEXT DEFAULT '',
        state_code TEXT DEFAULT '',
        gstin      TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )""")

    # ── invoices (GST-compliant) ───────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS invoices (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id         INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
        invoice_number  TEXT NOT NULL,
        customer_id     INTEGER REFERENCES customers(id),
        customer_name   TEXT DEFAULT 'Walk-in Customer',
        customer_gstin  TEXT DEFAULT '',
        customer_state  TEXT DEFAULT '',
        supply_type     TEXT DEFAULT 'intra',
        subtotal        REAL NOT NULL DEFAULT 0,
        discount        REAL NOT NULL DEFAULT 0,
        discount_pct    REAL NOT NULL DEFAULT 0,
        taxable_amount  REAL NOT NULL DEFAULT 0,
        cgst_amount     REAL NOT NULL DEFAULT 0,
        sgst_amount     REAL NOT NULL DEFAULT 0,
        igst_amount     REAL NOT NULL DEFAULT 0,
        total_tax       REAL NOT NULL DEFAULT 0,
        total           REAL NOT NULL DEFAULT 0,
        payment_method  TEXT DEFAULT 'Cash',
        place_of_supply TEXT DEFAULT '',
        notes           TEXT DEFAULT '',
        status          TEXT DEFAULT 'paid',
        created_by      INTEGER REFERENCES users(id),
        created_at      TEXT DEFAULT (datetime('now')),
        UNIQUE(invoice_number, shop_id)
    )""")

    # ── invoice_items (per-item GST breakdown) ────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS invoice_items (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id     INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
        shop_id        INTEGER NOT NULL REFERENCES shops(id),
        product_id     INTEGER REFERENCES products(id),
        product_name   TEXT NOT NULL,
        hsn_code       TEXT DEFAULT '',
        quantity       REAL NOT NULL DEFAULT 1,
        unit_price     REAL NOT NULL DEFAULT 0,
        discount       REAL NOT NULL DEFAULT 0,
        taxable_amount REAL NOT NULL DEFAULT 0,
        gst_rate       REAL NOT NULL DEFAULT 0,
        cgst_rate      REAL NOT NULL DEFAULT 0,
        sgst_rate      REAL NOT NULL DEFAULT 0,
        igst_rate      REAL NOT NULL DEFAULT 0,
        cgst_amount    REAL NOT NULL DEFAULT 0,
        sgst_amount    REAL NOT NULL DEFAULT 0,
        igst_amount    REAL NOT NULL DEFAULT 0,
        total_price    REAL NOT NULL DEFAULT 0
    )""")

    # ── expenses (per-shop) ───────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS expenses (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id      INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
        category     TEXT NOT NULL,
        description  TEXT DEFAULT '',
        amount       REAL NOT NULL DEFAULT 0,
        expense_date TEXT DEFAULT (date('now')),
        created_by   INTEGER REFERENCES users(id),
        created_at   TEXT DEFAULT (datetime('now'))
    )""")

    # ── settings (per-shop) ───────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id INTEGER REFERENCES shops(id) ON DELETE CASCADE,
        key     TEXT NOT NULL,
        value   TEXT DEFAULT '',
        UNIQUE(shop_id, key)
    )""")

    # ══════════════════════════════════════════════════════════════════════════
    # NEW TABLES: Purchase Management + Accounting (v3)
    # ══════════════════════════════════════════════════════════════════════════

    # ── suppliers ──────────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS suppliers (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id       INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
        name          TEXT    NOT NULL,
        phone         TEXT    DEFAULT '',
        email         TEXT    DEFAULT '',
        address       TEXT    DEFAULT '',
        gstin         TEXT    DEFAULT '',
        state_code    TEXT    DEFAULT '',
        opening_balance REAL  DEFAULT 0,   -- positive = we owe them
        balance       REAL    DEFAULT 0,   -- running payable balance
        is_active     INTEGER DEFAULT 1,
        created_at    TEXT    DEFAULT (datetime('now'))
    )""")

    # ── purchases (purchase bills) ────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS purchases (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id          INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
        purchase_number  TEXT    NOT NULL,
        supplier_id      INTEGER REFERENCES suppliers(id),
        supplier_name    TEXT    DEFAULT '',
        supplier_gstin   TEXT    DEFAULT '',
        bill_number      TEXT    DEFAULT '',   -- supplier's own bill/invoice no
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
        status           TEXT    DEFAULT 'received',  -- received | partial | cancelled
        created_by       INTEGER REFERENCES users(id),
        created_at       TEXT    DEFAULT (datetime('now')),
        UNIQUE(purchase_number, shop_id)
    )""")

    # ── purchase_items ────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS purchase_items (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_id     INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
        shop_id         INTEGER NOT NULL REFERENCES shops(id),
        product_id      INTEGER REFERENCES products(id),
        product_name    TEXT    NOT NULL,
        hsn_code        TEXT    DEFAULT '',
        quantity        REAL    NOT NULL DEFAULT 1,
        unit_price      REAL    NOT NULL DEFAULT 0,   -- purchase cost per unit
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

    # ── ledger ────────────────────────────────────────────────────────────────
    # Universal double-entry-style ledger for customers, suppliers, and shop
    c.execute("""CREATE TABLE IF NOT EXISTS ledger (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id       INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
        party_type    TEXT    NOT NULL,   -- 'customer' | 'supplier' | 'shop'
        party_id      INTEGER,            -- customer.id or supplier.id (NULL for shop)
        party_name    TEXT    DEFAULT '',
        txn_type      TEXT    NOT NULL,   -- 'sale'|'purchase'|'payment_in'|'payment_out'|'expense'|'opening'|'journal'
        ref_type      TEXT    DEFAULT '', -- 'invoice'|'purchase'|'expense'|'manual'
        ref_id        INTEGER DEFAULT 0,
        ref_number    TEXT    DEFAULT '',
        debit         REAL    NOT NULL DEFAULT 0,   -- amount going out / receivable
        credit        REAL    NOT NULL DEFAULT 0,   -- amount coming in / payable
        balance       REAL    NOT NULL DEFAULT 0,   -- running balance
        narration     TEXT    DEFAULT '',
        txn_date      TEXT    DEFAULT (date('now')),
        created_by    INTEGER REFERENCES users(id),
        created_at    TEXT    DEFAULT (datetime('now'))
    )""")

    # ── cash_book ─────────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS cash_book (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id     INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
        txn_date    TEXT    NOT NULL DEFAULT (date('now')),
        txn_type    TEXT    NOT NULL,   -- 'receipt' | 'payment'
        category    TEXT    DEFAULT '', -- 'sale'|'purchase_payment'|'expense'|'other'
        description TEXT    DEFAULT '',
        ref_type    TEXT    DEFAULT '',
        ref_id      INTEGER DEFAULT 0,
        amount      REAL    NOT NULL DEFAULT 0,
        balance     REAL    NOT NULL DEFAULT 0,   -- running cash balance
        created_by  INTEGER REFERENCES users(id),
        created_at  TEXT    DEFAULT (datetime('now'))
    )""")

    # ── bank_book ─────────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS bank_book (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id      INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
        account_name TEXT    DEFAULT 'Main Account',
        txn_date     TEXT    NOT NULL DEFAULT (date('now')),
        txn_type     TEXT    NOT NULL,   -- 'credit' | 'debit'
        description  TEXT    DEFAULT '',
        ref_number   TEXT    DEFAULT '',  -- cheque/UTR number
        amount       REAL    NOT NULL DEFAULT 0,
        balance      REAL    NOT NULL DEFAULT 0,
        created_by   INTEGER REFERENCES users(id),
        created_at   TEXT    DEFAULT (datetime('now'))
    )""")

    # ── payments (invoice payment tracking) v5 ───────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS payments (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id        INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
        invoice_id     INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
        invoice_number TEXT    DEFAULT '',
        customer_id    INTEGER REFERENCES customers(id),
        customer_name  TEXT    DEFAULT '',
        amount         REAL    NOT NULL DEFAULT 0,
        payment_method TEXT    DEFAULT 'Cash',
        payment_date   TEXT    DEFAULT (date('now')),
        reference      TEXT    DEFAULT '',
        notes          TEXT    DEFAULT '',
        created_by     INTEGER REFERENCES users(id),
        created_at     TEXT    DEFAULT (datetime('now'))
    )""")

    conn.commit()
    _migrate(conn)
    conn.close()
    print("[DB] Schema initialised.")

    # Seed template shops (idempotent — skips if already done)
    try:
        from utils.template_products import seed_template_shops
        seed_template_shops()
    except Exception as e:
        print(f"[DB] Warning: template seeding skipped — {e}")


def _migrate(conn):
    """
    Safe column-level ALTER TABLE for existing databases.
    Each statement is wrapped so existing columns are silently skipped.
    """
    migrations = [
        ("shops",    "state_code",     "TEXT DEFAULT '27'"),
        ("shops",    "invoice_prefix", "TEXT DEFAULT 'INV'"),
        ("shops",    "business_type",  "TEXT DEFAULT 'general'"),
        ("shops",    "is_template",    "INTEGER DEFAULT 0"),
        ("users",    "shop_id",        "INTEGER"),
        ("users",    "is_active",      "INTEGER DEFAULT 1"),
        ("users",    "last_login",     "TEXT"),
        ("users",    "email",          "TEXT DEFAULT ''"),
        ("users",    "phone",          "TEXT DEFAULT ''"),
        ("products", "shop_id",        "INTEGER DEFAULT 1"),
        ("products", "hsn_code",       "TEXT DEFAULT ''"),
        ("products", "gst_rate",       "REAL DEFAULT 18"),
        ("products", "is_active",      "INTEGER DEFAULT 1"),
        ("customers","shop_id",        "INTEGER DEFAULT 1"),
        ("customers","state_code",     "TEXT DEFAULT ''"),
        ("customers","gstin",          "TEXT DEFAULT ''"),
        ("invoices", "shop_id",        "INTEGER DEFAULT 1"),
        ("invoices", "customer_gstin", "TEXT DEFAULT ''"),
        ("invoices", "customer_state", "TEXT DEFAULT ''"),
        ("invoices", "supply_type",    "TEXT DEFAULT 'intra'"),
        ("invoices", "taxable_amount", "REAL DEFAULT 0"),
        ("invoices", "cgst_amount",    "REAL DEFAULT 0"),
        ("invoices", "sgst_amount",    "REAL DEFAULT 0"),
        ("invoices", "igst_amount",    "REAL DEFAULT 0"),
        ("invoices", "total_tax",      "REAL DEFAULT 0"),
        ("invoices", "place_of_supply","TEXT DEFAULT ''"),
        ("invoice_items","shop_id",        "INTEGER DEFAULT 1"),
        ("invoice_items","hsn_code",       "TEXT DEFAULT ''"),
        ("invoice_items","taxable_amount", "REAL DEFAULT 0"),
        ("invoice_items","gst_rate",       "REAL DEFAULT 0"),
        ("invoice_items","cgst_rate",      "REAL DEFAULT 0"),
        ("invoice_items","sgst_rate",      "REAL DEFAULT 0"),
        ("invoice_items","igst_rate",      "REAL DEFAULT 0"),
        ("invoice_items","cgst_amount",    "REAL DEFAULT 0"),
        ("invoice_items","sgst_amount",    "REAL DEFAULT 0"),
        ("invoice_items","igst_amount",    "REAL DEFAULT 0"),
        ("expenses", "shop_id",        "INTEGER DEFAULT 1"),
        ("categories","shop_id",       "INTEGER DEFAULT 1"),
        # v3 purchase / accounting additions
        ("suppliers", "opening_balance","REAL DEFAULT 0"),
        ("purchases", "paid_amount",    "REAL DEFAULT 0"),
        ("purchases", "due_amount",     "REAL DEFAULT 0"),
        ("purchases", "bill_number",    "TEXT DEFAULT ''"),
        ("purchases", "supply_type",    "TEXT DEFAULT 'intra'"),
        # v5 invoice paid/unpaid/partial system
        ("invoices",  "paid_amount",    "REAL DEFAULT 0"),
        ("invoices",  "due_amount",     "REAL DEFAULT 0"),
    ]
    for table, col, defn in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            conn.commit()
        except Exception:
            pass


# ═══════════════════════════════════════════ SEED ═════════════════════════════

def seed_sample_data():
    conn = get_db()
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        conn.close()
        return

    print("[DB] Seeding …")
    c = conn.cursor()

    # Super admin (no shop)
    c.execute("INSERT INTO users (username,password_hash,role,full_name) VALUES (?,?,?,?)",
        ("superadmin", generate_password_hash("Super@1234"), "superadmin", "Super Administrator"))

    # Shop 1 – Electronics (Maharashtra, state 27)
    c.execute("""INSERT INTO shops
        (name,gstin,address,phone,email,state_code,city,pincode,invoice_prefix)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        ("Sharma Electronics","27AABCU9603R1ZX",
         "12 MG Road, Andheri West, Mumbai","9876543210",
         "sharma@electronics.com","27","Mumbai","400058","SE-INV"))
    shop1 = c.lastrowid

    # Shop 2 – Grocery (Gujarat, state 24)
    c.execute("""INSERT INTO shops
        (name,gstin,address,phone,email,state_code,city,pincode,invoice_prefix)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        ("Patel Grocery Mart","24AAACS3006P1ZF",
         "45 Park Street, Navrangpura, Ahmedabad","9845612300",
         "patel@grocery.com","24","Ahmedabad","380009","PG-INV"))
    shop2 = c.lastrowid

    # Users
    for uname, pwd, role, name, sid in [
        ("owner1","Owner@123","shop_owner","Rajesh Sharma",shop1),
        ("staff1","Staff@123","staff",     "Priya Sharma", shop1),
        ("owner2","Owner@123","shop_owner","Suresh Patel",  shop2),
        ("staff2","Staff@123","staff",     "Anita Patel",   shop2),
    ]:
        c.execute("INSERT INTO users (username,password_hash,role,full_name,shop_id) VALUES (?,?,?,?,?)",
            (uname, generate_password_hash(pwd), role, name, sid))

    # Default settings
    for sid in [shop1, shop2]:
        for k, v in [("currency","₹"),("gst_rate","18"),("low_stock_alert","5")]:
            c.execute("INSERT OR IGNORE INTO settings (shop_id,key,value) VALUES (?,?,?)",(sid,k,v))

    # HSN master
    _seed_hsn(c)

    # Categories
    cats1 = ["Laptops & Computers","Mobile Phones","Accessories","Audio","Cables"]
    cats2 = ["Grains & Pulses","Edible Oils","Beverages","Dairy","Spices"]
    cid1, cid2 = {}, {}
    for n in cats1:
        c.execute("INSERT INTO categories (name,shop_id) VALUES (?,?)",(n,shop1))
        cid1[n] = c.lastrowid
    for n in cats2:
        c.execute("INSERT INTO categories (name,shop_id) VALUES (?,?)",(n,shop2))
        cid2[n] = c.lastrowid

    # Products shop1
    p1 = [
        ("Laptop 15.6 inch",   "LAP-001", cid1["Laptops & Computers"],  "84713010",18,42000,55000,8, 3),
        ("Gaming Laptop",      "LAP-002", cid1["Laptops & Computers"],  "84713010",18,65000,85000,4, 2),
        ("Smartphone Pro",     "MOB-001", cid1["Mobile Phones"],        "85171290",18,22000,28000,15,5),
        ("Budget Phone",       "MOB-002", cid1["Mobile Phones"],        "85171290",18, 8000,11000,20,5),
        ("Wireless Mouse",     "ACC-001", cid1["Accessories"],           "84716060",18,  350,  650,30,8),
        ("Mechanical Keyboard","ACC-002", cid1["Accessories"],           "84716041",18, 1200, 2200,12,4),
        ("BT Headphones",      "AUD-001", cid1["Audio"],                "85183000",18,  900, 1800,18,5),
        ("USB-C Cable 2m",     "CAB-001", cid1["Cables"],               "85444210",18,   80,  180,60,15),
        ("HDMI Cable",         "CAB-002", cid1["Cables"],               "85444210",18,  120,  250, 3,5),
        ("Power Bank 20000",   "ACC-003", cid1["Accessories"],           "85044090",18,  800, 1500, 0,5),
    ]
    # Products shop2
    p2 = [
        ("Basmati Rice 5kg",    "RICE-001",cid2["Grains & Pulses"], "10063000", 5, 200, 280,120,20),
        ("Toor Dal 1kg",        "DAL-001", cid2["Grains & Pulses"], "07134000", 5,  90, 130, 80,15),
        ("Sunflower Oil 1L",    "OIL-001", cid2["Edible Oils"],     "15121100", 5, 110, 155, 60,12),
        ("Mustard Oil 1L",      "OIL-002", cid2["Edible Oils"],     "15141100", 5,  95, 135, 45,10),
        ("Milk 1L",             "MILK-001",cid2["Dairy"],           "04011000", 0,  50,  60, 30,10),
        ("Butter 500g",         "BTR-001", cid2["Dairy"],           "04051000",12, 180, 245, 20, 8),
        ("Chai Tea 500g",       "TEA-001", cid2["Beverages"],       "09024090", 5, 120, 185, 40,10),
        ("Coffee 200g",         "COF-001", cid2["Beverages"],       "09011110",12, 190, 280, 25, 8),
        ("Cumin Seeds 100g",    "SPE-001", cid2["Spices"],          "09093110", 5,  35,  55,  2, 5),
        ("Turmeric Powder 100g","SPE-002", cid2["Spices"],          "09103000", 5,  28,  45,  0, 5),
    ]
    pids1, pids2 = [], []
    for prod in p1:
        c.execute("""INSERT INTO products
            (shop_id,name,sku,category_id,hsn_code,gst_rate,cost_price,
             selling_price,stock_quantity,low_stock_threshold)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (shop1,*prod))
        pids1.append((c.lastrowid, prod))
    for prod in p2:
        c.execute("""INSERT INTO products
            (shop_id,name,sku,category_id,hsn_code,gst_rate,cost_price,
             selling_price,stock_quantity,low_stock_threshold)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (shop2,*prod))
        pids2.append((c.lastrowid, prod))

    # Customers
    cust1_ids = []
    for row in [
        (shop1,"Amit Technologies","9988776655","amit@tech.com","Pune","27","27AABCU9603R1ZX"),
        (shop1,"Neha Sharma",      "9876543210","neha@gmail.com","Mumbai","27",""),
        (shop1,"Raj Enterprises",  "8765432109","raj@ent.com","Delhi","07","07AABCR1234P1ZS"),
        (shop1,"Walk-in Customer", "","","","",""),
    ]:
        c.execute("INSERT INTO customers (shop_id,name,phone,email,address,state_code,gstin) VALUES (?,?,?,?,?,?,?)",row)
        cust1_ids.append(c.lastrowid)
    cust2_ids = []
    for row in [
        (shop2,"Priya Households","9845612300","priya@home.com","Ahmedabad","24",""),
        (shop2,"Mehta Stores",    "9812345670","mehta@store.com","Surat","24","24AABCM5432Q1ZT"),
        (shop2,"Walk-in Customer","","","","",""),
    ]:
        c.execute("INSERT INTO customers (shop_id,name,phone,email,address,state_code,gstin) VALUES (?,?,?,?,?,?,?)",row)
        cust2_ids.append(c.lastrowid)

    cnames1 = ["Amit Technologies","Neha Sharma","Raj Enterprises","Walk-in Customer"]
    cnames2 = ["Priya Households","Mehta Stores","Walk-in Customer"]
    _seed_invoices(c, shop1, "27", cust1_ids, cnames1, pids1, "SE-INV", 2)
    _seed_invoices(c, shop2, "24", cust2_ids, cnames2, pids2, "PG-INV", 4)

    # Expenses
    today = datetime.now()
    for sid in [shop1, shop2]:
        for i in range(0, 30, 4):
            exp_date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            c.execute("INSERT INTO expenses (shop_id,category,description,amount,expense_date) VALUES (?,?,?,?,?)",
                (sid, random.choice(["Rent","Electricity","Salaries","Supplies","Maintenance"]),
                 "Monthly expense", random.randint(1000,9000), exp_date))

    # Suppliers + Purchases (v3)
    _seed_suppliers_purchases(c, shop1, shop2)

    conn.commit()
    conn.close()
    print("[DB] Done. superadmin/Super@1234  |  owner1/Owner@123  |  owner2/Owner@123")


def _seed_invoices(c, shop_id, shop_state, cust_ids, cust_names, prod_list, prefix, user_id):
    today  = datetime.now()
    inv_no = random.randint(1000, 1050)
    for day in range(30):
        for _ in range(random.randint(1, 3)):
            inv_no += 1
            inv_dt = (today - timedelta(days=day,
                       hours=random.randint(9,19), minutes=random.randint(0,59))
                     ).strftime("%Y-%m-%d %H:%M:%S")
            ci = random.randint(0, len(cust_ids)-1)
            chosen = random.sample(prod_list, min(random.randint(1,3), len(prod_list)))

            subtotal = taxable = cgst_tot = sgst_tot = 0
            item_rows = []
            for pid, pd in chosen:
                qty     = random.randint(1, 3)
                price   = pd[7]           # selling_price
                gst_r   = pd[5]           # gst_rate
                tax_val = round(price * qty, 2)
                h_cgst  = round(tax_val * gst_r / 200, 2)
                h_sgst  = round(tax_val * gst_r / 200, 2)
                subtotal += tax_val
                taxable  += tax_val
                cgst_tot += h_cgst
                sgst_tot += h_sgst
                item_rows.append((pid, pd[0], pd[4], qty, price, tax_val, gst_r,
                                   gst_r/2, gst_r/2, 0, h_cgst, h_sgst, 0,
                                   tax_val+h_cgst+h_sgst))

            total_tax = cgst_tot + sgst_tot
            total     = round(subtotal + total_tax, 2)

            c.execute("""INSERT INTO invoices
                (shop_id,invoice_number,customer_id,customer_name,supply_type,
                 subtotal,taxable_amount,cgst_amount,sgst_amount,igst_amount,
                 total_tax,total,payment_method,status,created_by,created_at)
                VALUES (?,?,?,?,'intra',?,?,?,?,0,?,?,'Cash','paid',?,?)""",
                (shop_id, f"{prefix}-{inv_no}", cust_ids[ci], cust_names[ci],
                 subtotal, taxable, cgst_tot, sgst_tot, total_tax, total,
                 user_id, inv_dt))
            inv_id = c.lastrowid
            for pid,pname,hsn,qty,price,taxable_i,gst_r,cgst_r,sgst_r,igst_r,cgst,sgst,igst,tot in item_rows:
                c.execute("""INSERT INTO invoice_items
                    (invoice_id,shop_id,product_id,product_name,hsn_code,quantity,
                     unit_price,taxable_amount,gst_rate,cgst_rate,sgst_rate,igst_rate,
                     cgst_amount,sgst_amount,igst_amount,total_price)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (inv_id,shop_id,pid,pname,hsn,qty,price,taxable_i,gst_r,
                     cgst_r,sgst_r,igst_r,cgst,sgst,igst,tot))


def _seed_suppliers_purchases(c, shop1, shop2):
    """Seed demo suppliers and purchase bills for both shops."""
    today = datetime.now()

    # ── Suppliers for Shop 1 (Electronics) ────────────────────────────────────
    sup1_rows = [
        (shop1,"TechSource India Pvt Ltd","9911223344","techsource@email.com",
         "Plot 5, MIDC, Pune","27AABCT1234P1ZX","27",50000),
        (shop1,"Global Gadgets Wholesale","9922334455","global@gadgets.com",
         "Unit 12, Sector 18, Noida","09AABCG5678Q2ZY","09",25000),
        (shop1,"Mumbai Electronics Hub","9933445566","mumbai@ehub.com",
         "Shop 3, Lamington Road, Mumbai","27AABCM9012R3ZZ","27",0),
    ]
    sup_ids_1 = []
    for row in sup1_rows:
        c.execute("""INSERT INTO suppliers
            (shop_id,name,phone,email,address,gstin,state_code,opening_balance,balance)
            VALUES (?,?,?,?,?,?,?,?,?)""", (*row, row[7]))
        sup_ids_1.append(c.lastrowid)

    # ── Suppliers for Shop 2 (Grocery) ─────────────────────────────────────────
    sup2_rows = [
        (shop2,"Rajasthan Agro Foods","9844556677","rajagro@foods.com",
         "Warehouse A, APMC Yard, Ahmedabad","24AABCR3456S4ZW","24",30000),
        (shop2,"Gujarat Dairy Cooperative","9855667788","gujaratdairy@coop.com",
         "Anand Dairy Complex, Anand","24AABCG7890T5ZV","24",0),
    ]
    sup_ids_2 = []
    for row in sup2_rows:
        c.execute("""INSERT INTO suppliers
            (shop_id,name,phone,email,address,gstin,state_code,opening_balance,balance)
            VALUES (?,?,?,?,?,?,?,?,?)""", (*row, row[7]))
        sup_ids_2.append(c.lastrowid)

    # ── Purchase bills for Shop 1 ──────────────────────────────────────────────
    # product_id, name, hsn, qty, cost_price, gst_rate
    shop1_items = [
        (1,"Laptop 15.6 inch","84713010",5,42000,18),
        (3,"Smartphone Pro",  "85171290",10,22000,18),
        (5,"Wireless Mouse",  "84716060",20,350,18),
        (8,"USB-C Cable 2m",  "85444210",50,80,18),
    ]
    pur_no = 1000
    for day_offset in range(0, 30, 7):
        pur_no += 1
        pur_date = (today - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        sid_idx  = day_offset % len(sup_ids_1)
        items    = random.sample(shop1_items, random.randint(1, 3))

        subtotal = taxable = cgst_tot = sgst_tot = 0
        item_data = []
        for pid, pname, hsn, qty, cost, gst_r in items:
            qty_r    = random.randint(2, qty)
            tax_val  = round(cost * qty_r, 2)
            h_cgst   = round(tax_val * gst_r / 200, 2)
            h_sgst   = h_cgst
            subtotal += tax_val
            taxable  += tax_val
            cgst_tot += h_cgst
            sgst_tot += h_sgst
            item_data.append((pid, pname, hsn, qty_r, cost, tax_val, gst_r,
                               gst_r/2, gst_r/2, 0, h_cgst, h_sgst, 0,
                               tax_val + h_cgst + h_sgst))

        total_tax = round(cgst_tot + sgst_tot, 2)
        total     = round(subtotal + total_tax, 2)
        paid      = total if day_offset % 14 == 0 else round(total * 0.5, 2)
        due       = round(total - paid, 2)

        c.execute("""INSERT INTO purchases
            (shop_id,purchase_number,supplier_id,supplier_name,bill_number,bill_date,
             subtotal,taxable_amount,cgst_amount,sgst_amount,igst_amount,total_tax,
             total,paid_amount,due_amount,payment_method,supply_type,status,
             created_by,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,'Cash','intra','received',2,?)""",
            (shop1, f"PUR-{pur_no}", sup_ids_1[sid_idx],
             sup1_rows[sid_idx][1], f"SUP-BILL-{pur_no}", pur_date,
             subtotal, taxable, cgst_tot, sgst_tot, total_tax,
             total, paid, due, pur_date+" 10:00:00"))
        pur_id = c.lastrowid

        for pid,pname,hsn,qty_r,cost,taxable_i,gst_r,cgst_r,sgst_r,igst_r,cgst,sgst,igst,tot in item_data:
            c.execute("""INSERT INTO purchase_items
                (purchase_id,shop_id,product_id,product_name,hsn_code,quantity,
                 unit_price,taxable_amount,gst_rate,cgst_rate,sgst_rate,igst_rate,
                 cgst_amount,sgst_amount,igst_amount,total_price)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pur_id,shop1,pid,pname,hsn,qty_r,cost,taxable_i,gst_r,
                 cgst_r,sgst_r,igst_r,cgst,sgst,igst,tot))

    # ── Purchase bills for Shop 2 ──────────────────────────────────────────────
    shop2_items = [
        (11,"Basmati Rice 5kg","10063000",20,200,5),
        (13,"Sunflower Oil 1L","15121100",30,110,5),
        (15,"Milk 1L",         "04011000",50,50,0),
        (17,"Chai Tea 500g",   "09024090",10,120,5),
    ]
    pur_no = 2000
    for day_offset in range(0, 30, 7):
        pur_no += 1
        pur_date = (today - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        sid_idx  = day_offset % len(sup_ids_2)
        items    = random.sample(shop2_items, random.randint(1, 3))

        subtotal = taxable = cgst_tot = sgst_tot = 0
        item_data = []
        for pid, pname, hsn, qty, cost, gst_r in items:
            qty_r    = random.randint(5, qty)
            tax_val  = round(cost * qty_r, 2)
            h_cgst   = round(tax_val * gst_r / 200, 2)
            h_sgst   = h_cgst
            subtotal += tax_val; taxable += tax_val
            cgst_tot += h_cgst; sgst_tot += h_sgst
            item_data.append((pid, pname, hsn, qty_r, cost, tax_val, gst_r,
                               gst_r/2, gst_r/2, 0, h_cgst, h_sgst, 0,
                               tax_val + h_cgst + h_sgst))

        total_tax = round(cgst_tot + sgst_tot, 2)
        total     = round(subtotal + total_tax, 2)
        paid      = total

        c.execute("""INSERT INTO purchases
            (shop_id,purchase_number,supplier_id,supplier_name,bill_number,bill_date,
             subtotal,taxable_amount,cgst_amount,sgst_amount,igst_amount,total_tax,
             total,paid_amount,due_amount,payment_method,supply_type,status,
             created_by,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,0,'Cash','intra','received',4,?)""",
            (shop2, f"PUR-{pur_no}", sup_ids_2[sid_idx],
             sup2_rows[sid_idx][1], f"SUP-BILL-{pur_no}", pur_date,
             subtotal, taxable, cgst_tot, sgst_tot, total_tax,
             total, paid, pur_date+" 10:00:00"))
        pur_id = c.lastrowid

        for pid,pname,hsn,qty_r,cost,taxable_i,gst_r,cgst_r,sgst_r,igst_r,cgst,sgst,igst,tot in item_data:
            c.execute("""INSERT INTO purchase_items
                (purchase_id,shop_id,product_id,product_name,hsn_code,quantity,
                 unit_price,taxable_amount,gst_rate,cgst_rate,sgst_rate,igst_rate,
                 cgst_amount,sgst_amount,igst_amount,total_price)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pur_id,shop2,pid,pname,hsn,qty_r,cost,taxable_i,gst_r,
                 cgst_r,sgst_r,igst_r,cgst,sgst,igst,tot))


def _seed_hsn(c):
    rows = [
        ("84713010","Laptop computers",18,"Electronics"),
        ("84715000","Processing units for computers",18,"Electronics"),
        ("85171290","Smartphones",18,"Electronics"),
        ("85183000","Headphones and earphones",18,"Electronics"),
        ("85044090","Power banks and chargers",18,"Electronics"),
        ("84716041","Computer keyboards",18,"Electronics"),
        ("84716060","Computer mouse",18,"Electronics"),
        ("85444210","USB and data cables",18,"Electronics"),
        ("85285100","Computer monitors",18,"Electronics"),
        ("85076000","Lithium-ion batteries",18,"Electronics"),
        ("84733099","Computer parts and accessories",18,"Electronics"),
        ("85044010","UPS and inverters",12,"Electronics"),
        ("85094000","Electric mixers and grinders",18,"Appliances"),
        ("85163200","Electric hair dryers",18,"Appliances"),
        ("85258090","CCTV cameras",18,"Electronics"),
        ("10063000","Basmati rice",5,"Grains"),
        ("10061000","Paddy rice",0,"Grains"),
        ("07134000","Toor dal pigeon peas",5,"Pulses"),
        ("07132000","Chickpeas chana",5,"Pulses"),
        ("10011900","Wheat",0,"Grains"),
        ("11010000","Wheat flour atta",5,"Grains"),
        ("17011200","Raw cane sugar",5,"Sugar"),
        ("17019100","Refined sugar",5,"Sugar"),
        ("15121100","Sunflower oil",5,"Edible Oils"),
        ("15141100","Mustard oil",5,"Edible Oils"),
        ("15131100","Coconut oil",5,"Edible Oils"),
        ("04011000","Fresh milk",0,"Dairy"),
        ("04021000","Skimmed milk powder",5,"Dairy"),
        ("04051000","Butter",12,"Dairy"),
        ("04061000","Fresh cheese paneer",5,"Dairy"),
        ("09024090","Tea leaves",5,"Beverages"),
        ("09011110","Roasted coffee",12,"Beverages"),
        ("22021090","Packaged water soda",18,"Beverages"),
        ("09103000","Turmeric powder",5,"Spices"),
        ("09093110","Cumin seeds",5,"Spices"),
        ("09042110","Black pepper whole",5,"Spices"),
        ("09042210","Cardamom",5,"Spices"),
        ("19053100","Biscuits and cookies",18,"Food"),
        ("19059090","Bread and bakery",5,"Bakery"),
        ("21069099","Food supplements protein powder",18,"Food"),
        ("61091000","Cotton T-shirts",5,"Clothing"),
        ("62034200","Cotton jeans and trousers",12,"Clothing"),
        ("61051000","Mens shirts cotton",5,"Clothing"),
        ("64029900","Rubber and plastic footwear",18,"Footwear"),
        ("64039990","Leather footwear",18,"Footwear"),
        ("48202000","Notebooks and exercise books",12,"Stationery"),
        ("96081000","Ball-point pens",18,"Stationery"),
        ("96091000","Pencils",0,"Stationery"),
        ("30049099","Medicines and tablets",12,"Pharma"),
        ("30051090","Surgical dressings",12,"Pharma"),
        ("39171000","PVC pipes",18,"Plumbing"),
        ("73083000","Steel doors and frames",18,"Hardware"),
        ("998311","IT software services SAC",18,"Services"),
        ("996111","Hotel accommodation SAC",18,"Services"),
        ("996332","Restaurant services SAC",5,"Services"),
    ]
    for row in rows:
        c.execute("INSERT OR IGNORE INTO hsn_master (hsn_code,description,default_gst_rate,category) VALUES (?,?,?,?)", row)
