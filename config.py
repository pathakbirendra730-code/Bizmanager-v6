"""
config.py  — Central configuration for BizManager Multi-Shop ERP
=================================================================
All tuneable constants live here so app.py stays clean.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    # ── Security ───────────────────────────────────────────────────────────
    SECRET_KEY = os.environ.get("BMS_SECRET", "bms-multishop-secret-2024-CHANGE-ME")
    SESSION_PERMANENT   = False
    SESSION_COOKIE_NAME = "bms_session"

    # ── Database ───────────────────────────────────────────────────────────
    DB_PATH = os.path.join(BASE_DIR, "database.db")

    # ── App behaviour ──────────────────────────────────────────────────────
    DEBUG            = True           # set False in production
    HOST             = "0.0.0.0"      # bind all interfaces (LAN access)
    PORT             = 5000
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024   # 16 MB upload limit

    # ── GST defaults ──────────────────────────────────────────────────────
    DEFAULT_GST_RATE = 18             # %
    GST_SLABS        = [0, 5, 12, 18, 28]   # valid Indian GST slabs
    CURRENCY_SYMBOL  = "₹"

    # ── Invoice numbering ──────────────────────────────────────────────────
    INVOICE_PREFIX   = "INV"          # overridden per shop in settings
    INVOICE_START    = 1000

    # ── Indian states (for IGST / CGST+SGST decision) ─────────────────────
    INDIAN_STATES = [
        ("01", "Jammu & Kashmir"),  ("02", "Himachal Pradesh"),
        ("03", "Punjab"),           ("04", "Chandigarh"),
        ("05", "Uttarakhand"),      ("06", "Haryana"),
        ("07", "Delhi"),            ("08", "Rajasthan"),
        ("09", "Uttar Pradesh"),    ("10", "Bihar"),
        ("11", "Sikkim"),           ("12", "Arunachal Pradesh"),
        ("13", "Nagaland"),         ("14", "Manipur"),
        ("15", "Mizoram"),          ("16", "Tripura"),
        ("17", "Meghalaya"),        ("18", "Assam"),
        ("19", "West Bengal"),      ("20", "Jharkhand"),
        ("21", "Odisha"),           ("22", "Chhattisgarh"),
        ("23", "Madhya Pradesh"),   ("24", "Gujarat"),
        ("25", "Daman & Diu"),      ("26", "Dadra & Nagar Haveli"),
        ("27", "Maharashtra"),      ("28", "Andhra Pradesh"),
        ("29", "Karnataka"),        ("30", "Goa"),
        ("31", "Lakshadweep"),      ("32", "Kerala"),
        ("33", "Tamil Nadu"),       ("34", "Puducherry"),
        ("35", "Andaman & Nicobar"),("36", "Telangana"),
        ("37", "Andhra Pradesh (New)"),
    ]

    # ── Super-admin default credentials (changed on first login) ──────────
    SUPERADMIN_USERNAME = "superadmin"
    SUPERADMIN_PASSWORD = "Super@1234"   # bcrypt hashed on seed


class DevelopmentConfig(Config):
    DEBUG    = True
    APP_ENV  = "development"
    # SQLite in development — no extra setup needed
    DB_PATH  = os.path.join(BASE_DIR, "database.db")


class ProductionConfig(Config):
    DEBUG    = False
    APP_ENV  = "production"

    # ── Secrets (MUST be set in environment) ──────────────────────────────
    SECRET_KEY = os.environ.get("BMS_SECRET")  # long random string, e.g. secrets.token_hex(32)

    # ── PostgreSQL (set DATABASE_URL for production) ───────────────────────
    # Format: postgresql://user:password@host:5432/dbname
    DATABASE_URL = os.environ.get("DATABASE_URL", "")

    # ── Secure cookies ────────────────────────────────────────────────────
    SESSION_COOKIE_SECURE   = True    # HTTPS only
    SESSION_COOKIE_HTTPONLY = True    # No JS access
    SESSION_COOKIE_SAMESITE = "Lax"

    # ── HSTS / security headers (set at reverse-proxy level too) ──────────
    PREFERRED_URL_SCHEME = "https"


# ── Active config: driven by APP_ENV environment variable ─────────────────────
_env = os.environ.get("APP_ENV", "development").lower()
ActiveConfig = ProductionConfig if _env == "production" else DevelopmentConfig
