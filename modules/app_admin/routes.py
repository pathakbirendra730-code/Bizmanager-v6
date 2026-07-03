"""
modules/app_admin/routes.py — App Admin Authentication
=========================================================
Blueprint: app_admin  |  URL prefix: /app-admin

SECURITY MODEL (by design):
  • There is NO public signup route for app admins. Ever.
  • App admin accounts can only be created by:
      1. The seed script (scripts/create_app_admin.py) — run once on first deploy
      2. An existing app admin with is_super=1, via /app-admin/admins/create
  • Public /saas/signup can NEVER create an app_admins row — separate table,
    separate blueprint, separate session keys. No code path connects them.

LOGIN FLOW:
  Step 1 — User ID + Password           (always required, first factor)
  Step 2 — OTP verification             (second factor, always required)
            • Development : OTP shown on-screen AND emailed to registered email
            • Production  : OTP sent to registered mobile AND registered email

SESSION KEYS (completely separate from saas_* and legacy user_id/role):
  admin_id, admin_userid, admin_fullname, admin_is_super
"""

import os
from datetime import datetime, timedelta
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, jsonify)
from werkzeug.security import check_password_hash, generate_password_hash

from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.otp_service import generate_otp, store_otp, verify_and_consume_otp, send_email_otp, send_sms_otp
from utils.saas_helpers import (
    validate_csrf, generate_csrf_token, audit_log, check_rate_limit
)

app_admin_bp = Blueprint("app_admin", __name__, url_prefix="/app-admin")

P = lambda: "%s" if _is_postgres() else "?"

ADMIN_SESSION_KEY = "admin_id"
ADMIN_PENDING_KEY = "admin_pending_id"   # set after step-1, before OTP verified

IS_PROD = os.environ.get("APP_ENV", "development").lower() == "production"


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")


def _is_app_admin_session() -> bool:
    return bool(session.get(ADMIN_SESSION_KEY))


# ── Decorator ──────────────────────────────────────────────────────────────────

from functools import wraps

def app_admin_required(f):
    """Restrict route to authenticated app admins only."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get(ADMIN_SESSION_KEY):
            flash("Please log in as an app administrator.", "warning")
            return redirect(url_for("app_admin.login"))
        return f(*args, **kwargs)
    return decorated


def super_admin_required(f):
    """Restrict route to is_super=1 app admins (can manage other admins)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get(ADMIN_SESSION_KEY):
            flash("Please log in as an app administrator.", "warning")
            return redirect(url_for("app_admin.login"))
        if not session.get("admin_is_super"):
            flash("Only super administrators can access this.", "danger")
            return redirect(url_for("app_admin.dashboard"))
        return f(*args, **kwargs)
    return decorated


# ════════════════════════════ CONTEXT ════════════════════════════════════════

@app_admin_bp.context_processor
def admin_globals():
    return {
        "csrf_token":      generate_csrf_token(),
        "admin_id":        session.get(ADMIN_SESSION_KEY),
        "admin_userid":    session.get("admin_userid", ""),
        "admin_fullname":  session.get("admin_fullname", ""),
        "admin_is_super":  session.get("admin_is_super", False),
        "is_production":   IS_PROD,
    }


# ════════════════════════ STEP 1: USER ID + PASSWORD ═════════════════════════

@app_admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get(ADMIN_SESSION_KEY):
        return redirect(url_for("app_admin.dashboard"))

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security validation failed. Please try again.", "danger")
            return redirect(url_for("app_admin.login"))

        user_id  = request.form.get("user_id", "").strip()
        password = request.form.get("password", "")

        if not _rate_limit_ok(f"admin_login:{_client_ip()}"):
            flash("Too many login attempts. Please wait a few minutes.", "danger")
            return render_template("app_admin/login.html", user_id=user_id)

        p = P()
        admin = saas_fetchone(
            f"SELECT * FROM app_admins WHERE user_id={p} AND is_active=1",
            (user_id,)
        )

        if not admin or not check_password_hash(admin["password_hash"], password):
            audit_log("app_admin_login_failed", status="failure",
                      detail=f"user_id={user_id}")
            flash("Invalid user ID or password.", "danger")
            return render_template("app_admin/login.html", user_id=user_id)

        # ── First factor passed — now require OTP ───────────────────────────
        session[ADMIN_PENDING_KEY] = admin["id"]

        otp = generate_otp()
        # OTP purpose namespaced separately from SaaS OTPs
        store_otp(f"admin:{admin['id']}", otp, "admin_login")

        # Always email the OTP (dev + prod)
        if admin.get("email"):
            send_email_otp(admin["email"], otp, "login")

        # Production also sends to registered mobile
        if IS_PROD and admin.get("mobile"):
            send_sms_otp(admin["mobile"], otp, "login")

        audit_log("app_admin_password_ok", status="success",
                  detail=f"user_id={user_id}")

        if not IS_PROD:
            # Development convenience: show OTP directly on screen too
            flash(f"Development mode — your OTP is also shown below.", "info")
            return render_template("app_admin/verify_otp.html",
                                   dev_otp=otp, admin_email=admin.get("email", ""))

        flash("Password verified. Enter the OTP sent to your email/mobile.", "info")
        return render_template("app_admin/verify_otp.html",
                               admin_email=admin.get("email", ""))

    return render_template("app_admin/login.html")


# ════════════════════════ STEP 2: OTP VERIFICATION ═══════════════════════════

@app_admin_bp.route("/verify-otp", methods=["POST"])
def verify_otp():
    admin_pk = session.get(ADMIN_PENDING_KEY)
    if not admin_pk:
        flash("Session expired. Please log in again.", "warning")
        return redirect(url_for("app_admin.login"))

    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("app_admin.login"))

    otp = "".join(request.form.get("otp", "").split())

    if not _rate_limit_ok(f"admin_otp:{admin_pk}"):
        flash("Too many attempts. Please log in again.", "danger")
        session.pop(ADMIN_PENDING_KEY, None)
        return redirect(url_for("app_admin.login"))

    success, message = verify_and_consume_otp(f"admin:{admin_pk}", otp, "admin_login")

    if not success:
        audit_log("app_admin_otp_failed", status="failure", detail=message)
        flash(message, "danger")
        return render_template("app_admin/verify_otp.html")

    p = P()
    admin = saas_fetchone(
        f"SELECT * FROM app_admins WHERE id={p} AND is_active=1", (admin_pk,)
    )
    if not admin:
        flash("Account not found or deactivated.", "danger")
        session.pop(ADMIN_PENDING_KEY, None)
        return redirect(url_for("app_admin.login"))

    # ── Full login success ────────────────────────────────────────────────
    session.pop(ADMIN_PENDING_KEY, None)
    session[ADMIN_SESSION_KEY]   = admin["id"]
    session["admin_userid"]      = admin["user_id"]
    session["admin_fullname"]    = admin["full_name"]
    session["admin_is_super"]    = bool(admin["is_super"])

    saas_execute(
        f"UPDATE app_admins SET last_login={p} WHERE id={p}",
        (datetime.utcnow().isoformat(), admin["id"])
    )
    audit_log("app_admin_login_success", status="success",
              detail=f"user_id={admin['user_id']}")

    flash(f"Welcome, {admin['full_name']}!", "success")
    return redirect(url_for("app_admin.dashboard"))


@app_admin_bp.route("/resend-otp", methods=["POST"])
def resend_otp():
    admin_pk = session.get(ADMIN_PENDING_KEY)
    if not admin_pk:
        return jsonify({"ok": False, "message": "Session expired."})

    if not _rate_limit_ok(f"admin_resend:{admin_pk}", limit=3, window=300):
        return jsonify({"ok": False, "message": "Too many resend requests."})

    p = P()
    admin = saas_fetchone(f"SELECT * FROM app_admins WHERE id={p}", (admin_pk,))
    if not admin:
        return jsonify({"ok": False, "message": "Account not found."})

    otp = generate_otp()
    store_otp(f"admin:{admin_pk}", otp, "admin_login")

    if admin.get("email"):
        send_email_otp(admin["email"], otp, "login")
    if IS_PROD and admin.get("mobile"):
        send_sms_otp(admin["mobile"], otp, "login")

    audit_log("app_admin_otp_resent", detail=f"admin_id={admin_pk}")

    msg = "OTP resent to your email" + (" and mobile" if IS_PROD else "")
    resp = {"ok": True, "message": msg}
    if not IS_PROD:
        resp["dev_otp"] = otp  # convenience for dev/testing only
    return jsonify(resp)


# ════════════════════════════ LOGOUT ═════════════════════════════════════════

@app_admin_bp.route("/logout")
def logout():
    aid = session.get(ADMIN_SESSION_KEY)
    if aid:
        audit_log("app_admin_logout", detail=f"admin_id={aid}")
    for key in (ADMIN_SESSION_KEY, ADMIN_PENDING_KEY, "admin_userid",
                "admin_fullname", "admin_is_super"):
        session.pop(key, None)
    flash("Signed out.", "info")
    return redirect(url_for("app_admin.login"))


# ════════════════════════════ RATE LIMIT HELPER ══════════════════════════════

def _rate_limit_ok(key: str, limit: int = 5, window: int = 600) -> bool:
    return check_rate_limit(key, max_requests=limit, window_seconds=window)
