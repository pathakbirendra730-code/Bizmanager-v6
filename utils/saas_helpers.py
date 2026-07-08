"""
utils/saas_helpers.py — SaaS Auth Utilities
============================================
• login_required_saas   — decorator for SaaS authenticated routes
• role_required          — decorator for role-based access control
• get_current_saas_user  — fetch user from session
• get_current_business   — fetch active business from session
• audit_log              — write to saas_audit_logs
• generate_slug          — URL-safe slug from business name
• generate_session_token — secure random token
• csrf_token / validate_csrf — CSRF protection
"""

import os
import re
import secrets
import hashlib
from functools import wraps
from datetime import datetime, timedelta

from flask import session, redirect, url_for, flash, request, abort, g
from models.saas_auth import saas_fetchone, saas_execute, get_saas_db, _is_postgres


# ═══════════════════════════ SESSION KEYS ════════════════════════════════════

SAAS_SESSION_KEY     = "saas_user_id"
SAAS_BIZ_KEY         = "saas_business_id"
SAAS_ROLE_KEY        = "saas_role"
SAAS_VERIFIED_KEY    = "saas_verified"
SAAS_TOKEN_KEY       = "saas_session_token"
SAAS_PENDING_USER    = "saas_pending_user"   # during signup flow
SAAS_PENDING_MOBILE  = "saas_pending_mobile"
SAAS_PENDING_EMAIL   = "saas_pending_email"


# ═══════════════════════════ DECORATORS ══════════════════════════════════════

def saas_login_required(f):
    """Require authenticated SaaS user. Redirects to /saas/login."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get(SAAS_SESSION_KEY):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("saas_auth.login"))
        return f(*args, **kwargs)
    return decorated


def saas_business_required(f):
    """Require both auth + an active business context."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get(SAAS_SESSION_KEY):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("saas_auth.login"))
        if not session.get(SAAS_BIZ_KEY):
            flash("Please select or create a business first.", "info")
            return redirect(url_for("saas_auth.business_setup"))
        return f(*args, **kwargs)
    return decorated


def role_required(*allowed_roles):
    """Restrict route to specific roles. Usage: @role_required('owner', 'manager')"""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get(SAAS_SESSION_KEY):
                flash("Please log in to continue.", "warning")
                return redirect(url_for("saas_auth.login"))
            role = session.get(SAAS_ROLE_KEY, "staff")
            if role not in allowed_roles:
                flash(f"Access denied. Required role: {' or '.join(allowed_roles)}.", "danger")
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


# ═══════════════════════════ SESSION MANAGEMENT ══════════════════════════════

def set_saas_session(user: dict, business: dict = None, role: str = "owner"):
    """Populate Flask session after successful login."""
    session[SAAS_SESSION_KEY]  = user["id"]
    session["saas_fullname"]   = user.get("full_name", "")
    session["saas_mobile"]     = user.get("mobile", "")
    session["saas_email"]      = user.get("email", "")
    session[SAAS_VERIFIED_KEY] = bool(user.get("is_verified"))
    session[SAAS_ROLE_KEY]     = role

    if business:
        session[SAAS_BIZ_KEY]      = business["id"]
        session["saas_biz_name"]   = business.get("name", "")
        session["saas_biz_slug"]   = business.get("slug", "")
        session["saas_biz_plan"]   = business.get("plan", "free")


def clear_saas_session():
    keys = [SAAS_SESSION_KEY, SAAS_BIZ_KEY, SAAS_ROLE_KEY, SAAS_VERIFIED_KEY,
            SAAS_TOKEN_KEY, SAAS_PENDING_USER, SAAS_PENDING_MOBILE,
            SAAS_PENDING_EMAIL, "saas_fullname", "saas_mobile", "saas_email",
            "saas_biz_name", "saas_biz_slug", "saas_biz_plan"]
    for k in keys:
        session.pop(k, None)


def get_current_saas_user() -> dict | None:
    uid = session.get(SAAS_SESSION_KEY)
    if not uid:
        return None
    return saas_fetchone(
        f"SELECT * FROM saas_users WHERE id = {'%s' if _is_postgres() else '?'}",
        (uid,)
    )


def get_current_business() -> dict | None:
    bid = session.get(SAAS_BIZ_KEY)
    if not bid:
        return None
    return saas_fetchone(
        f"SELECT * FROM saas_businesses WHERE id = {'%s' if _is_postgres() else '?'}",
        (bid,)
    )


def get_user_businesses(user_id: int) -> list:
    """Return all businesses for a user with their role."""
    p = "%s" if _is_postgres() else "?"
    return saas_fetchall(
        f"""SELECT b.*, ur.role
            FROM saas_businesses b
            JOIN saas_user_roles ur ON ur.business_id = b.id
            WHERE ur.user_id = {p} AND ur.is_active=TRUE AND b.is_active=TRUE
            ORDER BY ur.joined_at ASC""",
        (user_id,)
    )


# Convenience import for routes that need fetchall
from models.saas_auth import saas_fetchall


# ═══════════════════════════ CSRF PROTECTION ═════════════════════════════════

def generate_csrf_token() -> str:
    """Generate and store a CSRF token in the session."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def validate_csrf(token: str) -> bool:
    """Compare submitted token against session token (constant-time)."""
    stored = session.get("csrf_token", "")
    return secrets.compare_digest(stored, token or "")


def csrf_protect(f):
    """Decorator: validate CSRF token on POST requests."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == "POST":
            token = (request.form.get("csrf_token")
                     or request.headers.get("X-CSRF-Token", ""))
            if not validate_csrf(token):
                flash("Invalid request. Please try again.", "danger")
                return redirect(request.referrer or url_for("saas_auth.login"))
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════ AUDIT LOGGING ═══════════════════════════════════

def audit_log(action: str, user_id: int = None, business_id: int = None,
              entity_type: str = "", entity_id: str = "",
              detail: str = "", status: str = "success"):
    """Write an audit record. Never raises — fails silently."""
    try:
        p = "%s" if _is_postgres() else "?"
        ip = _get_client_ip()
        ua = request.headers.get("User-Agent", "")[:500]

        uid = user_id or session.get(SAAS_SESSION_KEY)
        bid = business_id or session.get(SAAS_BIZ_KEY)

        saas_execute(
            f"""INSERT INTO saas_audit_logs
                (user_id, business_id, action, entity_type, entity_id,
                 detail, ip_address, user_agent, status)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})""",
            (uid, bid, action, entity_type, str(entity_id),
             detail[:1000], ip, ua, status)
        )
    except Exception as e:
        print(f"[Audit] Failed to log: {e}")


def _get_client_ip() -> str:
    """Return real client IP, respecting proxies."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


# ═══════════════════════════ SLUG GENERATION ═════════════════════════════════

def generate_slug(name: str) -> str:
    """Convert business name to URL-safe slug. Ensures uniqueness."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")
    base = base[:40] or "business"
    slug = base
    counter = 1
    p = "%s" if _is_postgres() else "?"
    while saas_fetchone(
        f"SELECT id FROM saas_businesses WHERE slug = {p}", (slug,)
    ):
        slug = f"{base}-{counter}"
        counter += 1
    return slug


# ═══════════════════════════ TOKEN UTILITIES ═════════════════════════════════

def generate_session_token() -> str:
    return secrets.token_urlsafe(64)


def generate_reset_token() -> str:
    return secrets.token_urlsafe(48)


# ═══════════════════════════ PIN VALIDATION ══════════════════════════════════

def validate_pin(pin: str) -> tuple[bool, str]:
    """Validate 6-digit numeric PIN strength."""
    if not pin or not pin.isdigit():
        return False, "PIN must contain only digits."
    if len(pin) != 6:
        return False, "PIN must be exactly 6 digits."
    # Reject trivially weak PINs
    if len(set(pin)) == 1:
        return False, "PIN cannot be all the same digit (e.g., 111111)."
    if pin in ("123456", "654321", "000000", "999999", "112233"):
        return False, "PIN is too common. Choose a stronger PIN."
    return True, ""


# ═══════════════════════════ MOBILE VALIDATION ═══════════════════════════════

def validate_mobile(mobile: str) -> tuple[bool, str]:
    """Validate and normalise Indian mobile number to +91XXXXXXXXXX."""
    digits = re.sub(r"\D", "", mobile)
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if not digits.startswith(("6", "7", "8", "9")):
        return False, "Mobile number must start with 6, 7, 8, or 9."
    if len(digits) != 10:
        return False, "Mobile number must be 10 digits."
    return True, f"+91{digits}"


# ═══════════════════════════ RATE LIMITING ═══════════════════════════════════

_rate_limit_store: dict = {}  # in-process store; replace with Redis in prod

def check_rate_limit(key: str, max_requests: int = 5,
                     window_seconds: int = 300) -> bool:
    """
    Simple in-memory rate limiter. For production use Redis.
    Returns True if request is allowed, False if rate-limited.
    """
    now = datetime.utcnow().timestamp()
    window_start = now - window_seconds
    history = _rate_limit_store.get(key, [])
    history = [ts for ts in history if ts > window_start]
    if len(history) >= max_requests:
        _rate_limit_store[key] = history
        return False
    history.append(now)
    _rate_limit_store[key] = history
    return True


def get_avatar_initials(full_name: str) -> str:
    """Generate 1-2 character initials from full name."""
    parts = full_name.strip().split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[-1][0]).upper()
