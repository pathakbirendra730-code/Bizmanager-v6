"""
models/saas_auth.py — SaaS Authentication Database Schema
==========================================================
Tables:
  • saas_users         — registered users (mobile + email based)
  • saas_businesses    — business profiles (multi-tenant)
  • saas_user_roles    — user <-> business role mappings
  • saas_otp_tokens    — OTP tokens (email/SMS, time-limited)
  • saas_sessions      — server-side session tracking
  • saas_audit_logs    — security audit trail
  • saas_pin_reset     — PIN reset tokens

Env-based DB:
  • Development  → SQLite  (DB_PATH in config)
  • Production   → PostgreSQL (DATABASE_URL env var)
"""

import os
import sqlite3
from datetime import datetime
from decimal import Decimal
from utils.money import normalize_row

# sqlite3 has no default adapter for decimal.Decimal — binding one as a
# query parameter raises "Error binding parameter - probably unsupported
# type" unless we register one. Postgres needs no equivalent change:
# psycopg2 already adapts Decimal to NUMERIC natively. Registered once,
# process-wide, since sqlite3 adapters are global by design.
sqlite3.register_adapter(Decimal, lambda d: float(d))

# ── DB abstraction: SQLite (dev) or PostgreSQL (prod) ─────────────────────────

def _is_postgres():
    url = os.environ.get("DATABASE_URL", "")
    return url.startswith("postgres://") or url.startswith("postgresql://")


def _get_database_url():
    """
    Return a psycopg2-compatible DATABASE_URL.
    Render sometimes gives 'postgres://' prefix; psycopg2 needs 'postgresql://'.
    """
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def get_saas_db():
    """
    Returns a connection/cursor pair depending on environment.
    Usage: conn, c = get_saas_db()
    Always call conn.commit() then conn.close() after writes.
    """
    if _is_postgres():
        try:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(_get_database_url())
            conn.cursor_factory = psycopg2.extras.RealDictCursor
            return conn
        except ImportError:
            raise RuntimeError("psycopg2 not installed. Run: pip install psycopg2-binary")
    else:
        from config import ActiveConfig
        conn = sqlite3.connect(ActiveConfig.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn


def _placeholder():
    """Returns ? for SQLite, %s for PostgreSQL."""
    return "%s" if _is_postgres() else "?"


P = _placeholder  # shorthand – call as P() inside functions


def parse_dt(value):
    """
    Normalize a timestamp column value to a datetime object, regardless of
    backend.

    sqlite3 stores our timestamp columns as TEXT and always returns a plain
    ISO-format string, so callers used to just do datetime.fromisoformat(value)
    directly. psycopg2 returns a native datetime.datetime object for
    PostgreSQL's TIMESTAMP columns instead of a string — calling
    datetime.fromisoformat() on that raises TypeError ("fromisoformat:
    argument must be str"), which is exactly what surfaced as an opaque
    "Verification error. Please try again." on OTP/PIN-reset checks in
    production. Route every such comparison through this helper instead.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def fmt_dt(value, chars=16):
    """
    Truncate a timestamp column value to a display string, regardless of
    backend — companion to parse_dt() for Python code (as opposed to
    Jinja templates, which should use the 'dtfmt' template filter instead).
    sqlite3 returns a string already; psycopg2 returns a datetime object
    that must be converted before it can be sliced.
    """
    if not value:
        return ""
    if isinstance(value, datetime):
        value = value.isoformat(sep="T", timespec="seconds")
    return str(value)[:chars]


# ═══════════════════════════ SCHEMA CREATION ══════════════════════════════════

def init_saas_db():
    """Create all SaaS auth tables. Safe to call multiple times (IF NOT EXISTS)."""
    conn = get_saas_db()
    c = conn.cursor()

    if _is_postgres():
        _init_postgres(c)
    else:
        _init_sqlite(c)

    conn.commit()
    conn.close()
    print("[SaaS Auth] Database tables initialised.")


def _init_sqlite(c):
    # ── saas_users ─────────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        mobile          TEXT    NOT NULL UNIQUE,
        email           TEXT    NOT NULL UNIQUE,
        full_name       TEXT    NOT NULL DEFAULT '',
        pin_hash        TEXT,                        -- bcrypt hash of 6-digit PIN
        is_verified     INTEGER NOT NULL DEFAULT 0,  -- 1 after OTP verified
        is_active       INTEGER NOT NULL DEFAULT 1,
        avatar_initials TEXT    DEFAULT '',
        timezone        TEXT    DEFAULT 'Asia/Kolkata',
        created_at      TEXT    DEFAULT (datetime('now')),
        updated_at      TEXT    DEFAULT (datetime('now')),
        last_login      TEXT
    )""")

    # ── saas_businesses ────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_businesses (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT    NOT NULL,
        slug            TEXT    NOT NULL UNIQUE,     -- url-safe identifier
        gstin           TEXT    DEFAULT '',
        pan             TEXT    DEFAULT '',
        address         TEXT    DEFAULT '',
        city            TEXT    DEFAULT '',
        state_code      TEXT    DEFAULT '27',
        pincode         TEXT    DEFAULT '',
        phone           TEXT    DEFAULT '',
        email           TEXT    DEFAULT '',
        business_type   TEXT    DEFAULT 'retail',   -- retail/wholesale/service/manufacturing
        logo_url        TEXT    DEFAULT '',
        currency        TEXT    DEFAULT 'INR',
        timezone        TEXT    DEFAULT 'Asia/Kolkata',
        is_active       INTEGER NOT NULL DEFAULT 1,
        plan            TEXT    DEFAULT 'free',     -- free/starter/pro/enterprise
        trial_ends_at   TEXT,
        created_by      INTEGER REFERENCES saas_users(id),
        created_at      TEXT    DEFAULT (datetime('now')),
        updated_at      TEXT    DEFAULT (datetime('now'))
    )""")

    # ── saas_user_roles ────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_user_roles (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        role            TEXT    NOT NULL DEFAULT 'staff',
                        -- owner | manager | accountant | staff
        is_active       INTEGER NOT NULL DEFAULT 1,
        invited_by      INTEGER REFERENCES saas_users(id),
        joined_at       TEXT    DEFAULT (datetime('now')),
        UNIQUE(user_id, business_id)
    )""")

    # ── saas_otp_tokens ────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_otp_tokens (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        identifier      TEXT    NOT NULL,            -- mobile or email
        otp_hash        TEXT    NOT NULL,            -- hashed OTP
        purpose         TEXT    NOT NULL,
                        -- signup_email | signup_mobile | login | pin_reset
        attempts        INTEGER NOT NULL DEFAULT 0,
        max_attempts    INTEGER NOT NULL DEFAULT 5,
        expires_at      TEXT    NOT NULL,
        used_at         TEXT,
        created_at      TEXT    DEFAULT (datetime('now'))
    )""")

    # ── saas_sessions ──────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_sessions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
        business_id     INTEGER REFERENCES saas_businesses(id),
        session_token   TEXT    NOT NULL UNIQUE,
        ip_address      TEXT    DEFAULT '',
        user_agent      TEXT    DEFAULT '',
        is_active       INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT    DEFAULT (datetime('now')),
        last_active     TEXT    DEFAULT (datetime('now')),
        expires_at      TEXT    NOT NULL
    )""")

    # ── saas_audit_logs ────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_audit_logs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER REFERENCES saas_users(id),
        business_id     INTEGER REFERENCES saas_businesses(id),
        action          TEXT    NOT NULL,
        entity_type     TEXT    DEFAULT '',
        entity_id       TEXT    DEFAULT '',
        detail          TEXT    DEFAULT '',
        ip_address      TEXT    DEFAULT '',
        user_agent      TEXT    DEFAULT '',
        status          TEXT    DEFAULT 'success',  -- success | failure | warning
        created_at      TEXT    DEFAULT (datetime('now'))
    )""")

    # ── saas_pin_reset ─────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS saas_pin_reset (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
        token           TEXT    NOT NULL UNIQUE,
        expires_at      TEXT    NOT NULL,
        used_at         TEXT,
        created_at      TEXT    DEFAULT (datetime('now'))
    )""")

    # ── saas_pending_invites ───────────────────────────────────────────────────
    # Lets an owner/manager invite someone by mobile/email BEFORE they sign up.
    # On successful signup verification, the invite is matched and auto-applied
    # — the invitee becomes a team member directly, skipping business_setup.
    c.execute("""CREATE TABLE IF NOT EXISTS saas_pending_invites (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        mobile          TEXT    DEFAULT '',
        email           TEXT    DEFAULT '',
        role            TEXT    NOT NULL DEFAULT 'staff',
        invited_by      INTEGER REFERENCES saas_users(id),
        status          TEXT    NOT NULL DEFAULT 'pending',
                        -- pending | accepted | revoked | expired
        expires_at      TEXT    NOT NULL,
        accepted_by     INTEGER REFERENCES saas_users(id),
        accepted_at     TEXT,
        created_at      TEXT    DEFAULT (datetime('now'))
    )""")

    # ── app_admins ─────────────────────────────────────────────────────────────
    # Completely separate from saas_users / business signup. App admins manage
    # the whole platform (all businesses) and can NEVER be created through the
    # public signup form — only via CLI seed script or by an existing app admin.
    c.execute("""CREATE TABLE IF NOT EXISTS app_admins (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT    NOT NULL UNIQUE,    -- login identifier (not email)
        password_hash   TEXT    NOT NULL,
        full_name       TEXT    NOT NULL DEFAULT '',
        mobile          TEXT    DEFAULT '',
        email           TEXT    DEFAULT '',
        is_active       INTEGER NOT NULL DEFAULT 1,
        is_super        INTEGER NOT NULL DEFAULT 0,  -- can create other app admins
        last_login      TEXT,
        created_at      TEXT    DEFAULT (datetime('now'))
    )""")

    # ── platform_settings ──────────────────────────────────────────────────────
    # Admin-editable, DB-backed configuration for things that used to require
    # an env var change + redeploy (e.g. "does signup require mobile OTP?",
    # "which email/SMS provider is active?"). A missing key falls back to
    # its env var / hardcoded default — see utils/platform_settings.py.
    c.execute("""CREATE TABLE IF NOT EXISTS platform_settings (
        key             TEXT    PRIMARY KEY,
        value           TEXT    NOT NULL DEFAULT '',
        updated_by      INTEGER,
        updated_at      TEXT    DEFAULT (datetime('now'))
    )""")

    # ── notification_log ─────────────────────────────────────────────────────
    # Every send attempt (email or SMS), across every provider — the audit
    # trail for the notification framework, and for OTP sends specifically.
    c.execute("""CREATE TABLE IF NOT EXISTS notification_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        channel             TEXT    NOT NULL,
        provider            TEXT    NOT NULL,
        recipient_masked    TEXT    NOT NULL DEFAULT '',
        purpose             TEXT    NOT NULL DEFAULT '',
        status              TEXT    NOT NULL,
        attempts            INTEGER NOT NULL DEFAULT 1,
        error               TEXT,
        created_at          TEXT    DEFAULT (datetime('now'))
    )""")


    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_saas_users_mobile ON saas_users(mobile)",
        "CREATE INDEX IF NOT EXISTS idx_saas_users_email  ON saas_users(email)",
        "CREATE INDEX IF NOT EXISTS idx_saas_otp_identifier ON saas_otp_tokens(identifier, purpose)",
        "CREATE INDEX IF NOT EXISTS idx_saas_sessions_token ON saas_sessions(session_token)",
        "CREATE INDEX IF NOT EXISTS idx_saas_roles_user ON saas_user_roles(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_roles_biz  ON saas_user_roles(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_audit_user ON saas_audit_logs(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_audit_biz  ON saas_audit_logs(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invite_mobile ON saas_pending_invites(mobile)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invite_email  ON saas_pending_invites(email)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invite_status ON saas_pending_invites(status)",
        "CREATE INDEX IF NOT EXISTS idx_app_admins_userid   ON app_admins(user_id)",
    ]
    for idx in indexes:
        c.execute(idx)


def _init_postgres(c):
    """PostgreSQL-compatible schema using %s placeholders and SERIAL."""
    c.execute("""CREATE TABLE IF NOT EXISTS saas_users (
        id              SERIAL PRIMARY KEY,
        mobile          VARCHAR(20)  NOT NULL UNIQUE,
        email           VARCHAR(255) NOT NULL UNIQUE,
        full_name       VARCHAR(200) NOT NULL DEFAULT '',
        pin_hash        TEXT,
        is_verified     BOOLEAN NOT NULL DEFAULT FALSE,
        is_active       BOOLEAN NOT NULL DEFAULT TRUE,
        avatar_initials VARCHAR(3)  DEFAULT '',
        timezone        VARCHAR(50) DEFAULT 'Asia/Kolkata',
        created_at      TIMESTAMP   DEFAULT NOW(),
        updated_at      TIMESTAMP   DEFAULT NOW(),
        last_login      TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_businesses (
        id              SERIAL PRIMARY KEY,
        name            VARCHAR(300) NOT NULL,
        slug            VARCHAR(100) NOT NULL UNIQUE,
        gstin           VARCHAR(20)  DEFAULT '',
        pan             VARCHAR(15)  DEFAULT '',
        address         TEXT         DEFAULT '',
        city            VARCHAR(100) DEFAULT '',
        state_code      VARCHAR(5)   DEFAULT '27',
        pincode         VARCHAR(10)  DEFAULT '',
        phone           VARCHAR(20)  DEFAULT '',
        email           VARCHAR(255) DEFAULT '',
        business_type   VARCHAR(50)  DEFAULT 'retail',
        logo_url        TEXT         DEFAULT '',
        currency        VARCHAR(10)  DEFAULT 'INR',
        timezone        VARCHAR(50)  DEFAULT 'Asia/Kolkata',
        is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
        plan            VARCHAR(30)  DEFAULT 'free',
        trial_ends_at   TIMESTAMP,
        created_by      INTEGER      REFERENCES saas_users(id),
        created_at      TIMESTAMP    DEFAULT NOW(),
        updated_at      TIMESTAMP    DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_user_roles (
        id              SERIAL PRIMARY KEY,
        user_id         INTEGER NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        role            VARCHAR(30) NOT NULL DEFAULT 'staff',
        is_active       BOOLEAN NOT NULL DEFAULT TRUE,
        invited_by      INTEGER REFERENCES saas_users(id),
        joined_at       TIMESTAMP DEFAULT NOW(),
        UNIQUE(user_id, business_id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_otp_tokens (
        id              SERIAL PRIMARY KEY,
        identifier      VARCHAR(255) NOT NULL,
        otp_hash        TEXT         NOT NULL,
        purpose         VARCHAR(50)  NOT NULL,
        attempts        INTEGER      NOT NULL DEFAULT 0,
        max_attempts    INTEGER      NOT NULL DEFAULT 5,
        expires_at      TIMESTAMP    NOT NULL,
        used_at         TIMESTAMP,
        created_at      TIMESTAMP    DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_sessions (
        id              SERIAL PRIMARY KEY,
        user_id         INTEGER NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
        business_id     INTEGER REFERENCES saas_businesses(id),
        session_token   VARCHAR(128) NOT NULL UNIQUE,
        ip_address      VARCHAR(45)  DEFAULT '',
        user_agent      TEXT         DEFAULT '',
        is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
        created_at      TIMESTAMP    DEFAULT NOW(),
        last_active     TIMESTAMP    DEFAULT NOW(),
        expires_at      TIMESTAMP    NOT NULL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_audit_logs (
        id              SERIAL PRIMARY KEY,
        user_id         INTEGER REFERENCES saas_users(id),
        business_id     INTEGER REFERENCES saas_businesses(id),
        action          VARCHAR(100) NOT NULL,
        entity_type     VARCHAR(50)  DEFAULT '',
        entity_id       VARCHAR(50)  DEFAULT '',
        detail          TEXT         DEFAULT '',
        ip_address      VARCHAR(45)  DEFAULT '',
        user_agent      TEXT         DEFAULT '',
        status          VARCHAR(20)  DEFAULT 'success',
        created_at      TIMESTAMP    DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_pin_reset (
        id              SERIAL PRIMARY KEY,
        user_id         INTEGER NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
        token           VARCHAR(128) NOT NULL UNIQUE,
        expires_at      TIMESTAMP NOT NULL,
        used_at         TIMESTAMP,
        created_at      TIMESTAMP DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS saas_pending_invites (
        id              SERIAL PRIMARY KEY,
        business_id     INTEGER NOT NULL REFERENCES saas_businesses(id) ON DELETE CASCADE,
        mobile          VARCHAR(20)  DEFAULT '',
        email           VARCHAR(255) DEFAULT '',
        role            VARCHAR(30)  NOT NULL DEFAULT 'staff',
        invited_by      INTEGER REFERENCES saas_users(id),
        status          VARCHAR(20)  NOT NULL DEFAULT 'pending',
        expires_at      TIMESTAMP NOT NULL,
        accepted_by     INTEGER REFERENCES saas_users(id),
        accepted_at     TIMESTAMP,
        created_at      TIMESTAMP DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS app_admins (
        id              SERIAL PRIMARY KEY,
        user_id         VARCHAR(100) NOT NULL UNIQUE,
        password_hash   TEXT         NOT NULL,
        full_name       VARCHAR(200) NOT NULL DEFAULT '',
        mobile          VARCHAR(20)  DEFAULT '',
        email           VARCHAR(255) DEFAULT '',
        is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
        is_super        BOOLEAN      NOT NULL DEFAULT FALSE,
        last_login      TIMESTAMP,
        created_at      TIMESTAMP    DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS platform_settings (
        key             VARCHAR(100) PRIMARY KEY,
        value           TEXT         NOT NULL DEFAULT '',
        updated_by      INTEGER,
        updated_at      TIMESTAMP    DEFAULT NOW()
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS notification_log (
        id                  SERIAL PRIMARY KEY,
        channel             VARCHAR(20)  NOT NULL,
        provider            VARCHAR(30)  NOT NULL,
        recipient_masked    VARCHAR(255) NOT NULL DEFAULT '',
        purpose             VARCHAR(100) NOT NULL DEFAULT '',
        status              VARCHAR(20)  NOT NULL,
        attempts            INTEGER      NOT NULL DEFAULT 1,
        error               TEXT,
        created_at          TIMESTAMP    DEFAULT NOW()
    )""")

    # Indexes
    pg_indexes = [
        "CREATE INDEX IF NOT EXISTS idx_saas_users_mobile ON saas_users(mobile)",
        "CREATE INDEX IF NOT EXISTS idx_saas_users_email  ON saas_users(email)",
        "CREATE INDEX IF NOT EXISTS idx_saas_otp_id_purp  ON saas_otp_tokens(identifier, purpose)",
        "CREATE INDEX IF NOT EXISTS idx_saas_sess_token   ON saas_sessions(session_token)",
        "CREATE INDEX IF NOT EXISTS idx_saas_roles_user   ON saas_user_roles(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_roles_biz    ON saas_user_roles(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_audit_user   ON saas_audit_logs(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_audit_biz    ON saas_audit_logs(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invite_mobile ON saas_pending_invites(mobile)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invite_email  ON saas_pending_invites(email)",
        "CREATE INDEX IF NOT EXISTS idx_saas_invite_status ON saas_pending_invites(status)",
        "CREATE INDEX IF NOT EXISTS idx_app_admins_userid   ON app_admins(user_id)",
    ]
    for idx in pg_indexes:
        c.execute(idx)


# ═══════════════════════════ QUERY HELPERS ════════════════════════════════════

def saas_fetchone(sql, params=()):
    """Execute a SELECT and return a single row as dict (or None).
    Monetary/NUMERIC values are normalized to Decimal — see utils/money.py."""
    conn = get_saas_db()
    c = conn.cursor()
    c.execute(sql, params)
    row = c.fetchone()
    conn.close()
    if row is None:
        return None
    return normalize_row(dict(row))


def saas_fetchall(sql, params=()):
    """Execute a SELECT and return all rows as list of dicts.
    Monetary/NUMERIC values are normalized to Decimal — see utils/money.py."""
    conn = get_saas_db()
    c = conn.cursor()
    c.execute(sql, params)
    rows = c.fetchall()
    conn.close()
    return [normalize_row(dict(r)) for r in rows]


def saas_execute(sql, params=(), returning="id"):
    """
    Execute INSERT/UPDATE/DELETE. Returns the new row's id for INSERTs
    (None for UPDATE/DELETE, unless the caller supplies its own RETURNING).

    IMPORTANT — PostgreSQL vs SQLite id retrieval:
    sqlite3's cursor.lastrowid reliably returns the id of the last inserted
    row. psycopg2's cursor.lastrowid does NOT — modern PostgreSQL tables
    have no OIDs, so it is always None. The only reliable way to get an
    inserted id back from PostgreSQL is an explicit "RETURNING <col>"
    clause. To avoid every caller having to special-case this, an INSERT
    statement that doesn't already contain RETURNING gets one appended
    automatically when running against PostgreSQL (pass returning=None to
    opt out, e.g. for bulk/no-id inserts).
    """
    conn = get_saas_db()
    c = conn.cursor()
    is_pg = _is_postgres()
    is_insert = sql.lstrip().upper().startswith("INSERT")
    needs_returning = is_pg and is_insert and returning and "RETURNING" not in sql.upper()

    if needs_returning:
        sql = sql.rstrip().rstrip(";") + f" RETURNING {returning}"

    c.execute(sql, params)

    if needs_returning:
        row = c.fetchone()
        last_id = row[returning] if row is not None else None
    elif is_pg and is_insert and "RETURNING" in sql.upper():
        # Caller already added their own RETURNING clause.
        row = c.fetchone()
        last_id = row[returning] if row is not None and returning else row
    else:
        last_id = c.lastrowid

    conn.commit()
    conn.close()
    return last_id
