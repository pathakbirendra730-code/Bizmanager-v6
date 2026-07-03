"""
modules/unified_login.py — Single Login Entry Point
========================================================
Blueprint: unified_login  |  URL prefix: (none — mounted at /login)

One page, one input field. Based on what the user types:
  • Looks like a mobile number (digits, starts with 6-9, 10 digits)
      → SaaS business login → 6-digit PIN entry
  • Looks like a username (letters/mixed, not a valid mobile pattern)
      → App Admin login → password entry

The detection happens TWICE:
  1. Client-side (JS) — purely for UX, decides which input to show next
  2. Server-side (this route) — the actual security decision, never trusts
     the client. The same detection rule is re-applied to the submitted
     identifier before deciding which backend auth path to run.

This route does not replace /saas/login or /app-admin/login — both still
exist and work standalone. This is a convenience front door that delegates
to the same underlying logic (same DB queries, same OTP/PIN checks, same
audit logging) so there is exactly one source of truth for each auth path.
"""

import os
import re
from datetime import datetime
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, jsonify)
from werkzeug.security import check_password_hash

from models.saas_auth import saas_fetchone, saas_execute, _is_postgres
from utils.saas_helpers import (
    validate_mobile, validate_csrf, generate_csrf_token,
    audit_log, check_rate_limit, set_saas_session, get_user_businesses,
    SAAS_SESSION_KEY, SAAS_PENDING_USER, SAAS_PENDING_EMAIL, SAAS_PENDING_MOBILE
)
from utils.otp_service import generate_otp, store_otp, send_email_otp, send_sms_otp

unified_bp = Blueprint("unified_login", __name__)

P       = lambda: "%s" if _is_postgres() else "?"
IS_PROD = os.environ.get("APP_ENV", "development").lower() == "production"


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")


def _looks_like_mobile(identifier: str) -> bool:
    """
    Server-side identifier classification — the real security decision.
    Mirrors the client-side JS rule exactly:
      • Strip all non-digits
      • If what's left is 10 digits starting with 6-9 (optionally prefixed
        with 91/+91 making it 12 digits) → treat as mobile
      • Otherwise → treat as a username
    """
    digits = re.sub(r"\D", "", identifier)
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    return len(digits) == 10 and digits[0] in "6789"


@unified_bp.context_processor
def unified_globals():
    return {"csrf_token": generate_csrf_token(), "is_production": IS_PROD}


# ════════════════════════════ GET — render shell ═════════════════════════════

@unified_bp.route("/login", methods=["GET"])
def login():
    # Already logged into either system → go straight to the right place
    if session.get(SAAS_SESSION_KEY):
        return redirect(url_for("saas_dashboard.index"))
    if session.get("admin_id"):
        return redirect(url_for("app_admin.dashboard"))
    return render_template("unified_login.html")


# ════════════════════ POST — identify which system to use ════════════════════

@unified_bp.route("/login/identify", methods=["POST"])
def identify():
    """
    AJAX endpoint: client sends what was typed, server classifies it
    and tells the browser which second field to show. This is purely
    for the UI to react correctly — it carries no security weight by
    itself, since /login/submit re-validates everything server-side.
    """
    identifier = request.form.get("identifier", "").strip()
    if not identifier:
        return jsonify({"ok": False, "message": "Enter a mobile number or user ID."})

    is_mobile = _looks_like_mobile(identifier)
    return jsonify({
        "ok": True,
        "mode": "mobile_pin" if is_mobile else "username_password",
    })


# ════════════════════════════ POST — actual login ════════════════════════════

@unified_bp.route("/login/submit", methods=["POST"])
def submit():
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security validation failed. Please try again.", "danger")
        return redirect(url_for("unified_login.login"))

    identifier = request.form.get("identifier", "").strip()
    if not identifier:
        flash("Enter a mobile number or user ID.", "danger")
        return redirect(url_for("unified_login.login"))

    # Server makes its OWN decision — never trusts a hidden "mode" field
    if _looks_like_mobile(identifier):
        return _handle_mobile_pin_login(identifier)
    else:
        return _handle_username_password_login(identifier)


# ──────────────────────── Path A: Mobile + PIN (SaaS) ────────────────────────

def _handle_mobile_pin_login(identifier: str):
    pin = request.form.get("pin", "")

    ok, mobile_norm = validate_mobile(identifier)
    if not ok:
        flash(mobile_norm, "danger")
        return render_template("unified_login.html",
                               identifier=identifier, mode="mobile_pin")

    rl_key = f"login:{mobile_norm}:{_client_ip()}"
    if not check_rate_limit(rl_key, max_requests=10, window_seconds=600):
        audit_log("login_rate_limited", status="failure", detail=f"mobile={mobile_norm}")
        flash("Too many login attempts. Please wait 10 minutes.", "danger")
        return render_template("unified_login.html",
                               identifier=identifier, mode="mobile_pin")

    p = P()
    user = saas_fetchone(
        f"SELECT * FROM saas_users WHERE mobile={p} AND is_active=1", (mobile_norm,)
    )

    if not user:
        audit_log("login_user_not_found", status="failure", detail=f"mobile={mobile_norm}")
        flash("Mobile number not registered. Please sign up.", "warning")
        return render_template("unified_login.html",
                               identifier=identifier, mode="mobile_pin")

    if not user.get("is_verified"):
        flash("Account not verified. Please complete signup.", "warning")
        session[SAAS_PENDING_USER]   = user["id"]
        session[SAAS_PENDING_EMAIL]  = user["email"]
        session[SAAS_PENDING_MOBILE] = mobile_norm
        return redirect(url_for("saas_auth.verify_email"))

    if not user.get("pin_hash"):
        flash("No PIN set. Please complete registration.", "warning")
        session[SAAS_PENDING_USER] = user["id"]
        return redirect(url_for("saas_auth.set_pin"))

    if not check_password_hash(user["pin_hash"], pin):
        audit_log("login_failed", user_id=user["id"], status="failure", detail="wrong_pin")
        flash("Incorrect PIN. Please try again.", "danger")
        return render_template("unified_login.html",
                               identifier=identifier, mode="mobile_pin")

    businesses = get_user_businesses(user["id"])

    if not businesses:
        session[SAAS_PENDING_USER]   = user["id"]
        session[SAAS_PENDING_EMAIL]  = user["email"]
        session[SAAS_PENDING_MOBILE] = mobile_norm
        saas_execute(
            f"UPDATE saas_users SET last_login={p} WHERE id={p}",
            (datetime.utcnow().isoformat(), user["id"])
        )
        flash("Please create your business profile to continue.", "info")
        return redirect(url_for("saas_auth.business_setup"))

    if len(businesses) == 1:
        set_saas_session(user, businesses[0], role=businesses[0]["role"])
    else:
        session[SAAS_PENDING_USER] = user["id"]
        return redirect(url_for("saas_auth.select_business"))

    saas_execute(
        f"UPDATE saas_users SET last_login={p} WHERE id={p}",
        (datetime.utcnow().isoformat(), user["id"])
    )
    audit_log("login_success", user_id=user["id"],
              business_id=session.get("saas_business_id"))
    flash(f"Welcome back, {user['full_name']}!", "success")
    return redirect(url_for("saas_dashboard.index"))


# ──────────────────── Path B: User ID + Password (App Admin) ─────────────────

def _handle_username_password_login(identifier: str):
    password = request.form.get("password", "")
    user_id  = identifier

    from utils.saas_helpers import check_rate_limit as _crl
    if not _crl(f"admin_login:{_client_ip()}", max_requests=5, window_seconds=600):
        flash("Too many login attempts. Please wait a few minutes.", "danger")
        return render_template("unified_login.html",
                               identifier=identifier, mode="username_password")

    p = P()
    admin = saas_fetchone(
        f"SELECT * FROM app_admins WHERE user_id={p} AND is_active=1", (user_id,)
    )

    if not admin or not check_password_hash(admin["password_hash"], password):
        audit_log("app_admin_login_failed", status="failure", detail=f"user_id={user_id}")
        flash("Invalid user ID or password.", "danger")
        return render_template("unified_login.html",
                               identifier=identifier, mode="username_password")

    # First factor passed — hand off to the existing app_admin OTP flow.
    # (Same session keys / OTP namespace as /app-admin/login uses, so the
    # rest of the two-factor flow is identical — single source of truth.)
    session["admin_pending_id"] = admin["id"]

    otp = generate_otp()
    store_otp(f"admin:{admin['id']}", otp, "admin_login")

    if admin.get("email"):
        send_email_otp(admin["email"], otp, "login")
    if IS_PROD and admin.get("mobile"):
        send_sms_otp(admin["mobile"], otp, "login")

    audit_log("app_admin_password_ok", status="success", detail=f"user_id={user_id}")

    if not IS_PROD:
        flash("Development mode — your OTP is also shown below.", "info")
        return render_template("app_admin/verify_otp.html",
                               dev_otp=otp, admin_email=admin.get("email", ""))

    flash("Password verified. Enter the OTP sent to your email/mobile.", "info")
    return render_template("app_admin/verify_otp.html",
                           admin_email=admin.get("email", ""))
