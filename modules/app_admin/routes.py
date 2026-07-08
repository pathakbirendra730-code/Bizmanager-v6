"""
modules/app_admin/routes.py — App Admin Authentication
=========================================================
Blueprint: app_admin  |  URL prefix: /app-admin

SECURITY MODEL (by design):
  • There is NO public signup route for app admins. Ever.
  • App admin accounts can only be created by:
      1. The seed script (scripts/create_app_admin.py) — needs shell access
      2. The web-based bootstrap route (/app-admin/bootstrap) — needs the
         BOOTSTRAP_ADMIN_TOKEN env var AND zero existing admins; permanently
         self-disables the moment one admin exists (see that route's docstring)
      3. An existing app admin with is_super=TRUE, via /app-admin/admins/create
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
from werkzeug.security import generate_password_hash

from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.otp_manager import otp_manager
from utils.auth_service import auth_service
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
    """Restrict route to is_super=TRUE app admins (can manage other admins)."""
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


# ════════════════════════ FIRST-RUN ADMIN BOOTSTRAP ═══════════════════════════
#
# The problem this solves: scripts/create_app_admin.py requires shell access
# to wherever the app is running. On Render (and most PaaS), that means the
# Shell tab (not available on every plan) or tunnelling DATABASE_URL from a
# local machine — clumsy for what should be a one-time, five-minute setup step.
#
# This route is a web-based alternative, gated so it can NEVER be used to
# create an unauthorized admin:
#   1. Disabled entirely unless BOOTSTRAP_ADMIN_TOKEN is set as an env var —
#      a secret only the person deploying the app knows (set it in Render's
#      dashboard, not committed to git).
#   2. Disabled the moment ANY app_admins row exists — self-locking, so the
#      very first admin created (via this route OR the CLI script)
#      permanently closes this door. There is no way to use this route to
#      create a SECOND admin; that's what /app-admin/admins/create is for.
#   3. Every failure mode (feature disabled, wrong token, already set up)
#      returns the identical 404 — an attacker probing this URL can't tell
#      which case they hit.
#
# Usage: set BOOTSTRAP_ADMIN_TOKEN on Render, then visit
#   https://your-app.onrender.com/app-admin/bootstrap?token=<that value>

def _bootstrap_token_valid(request_token: str) -> bool:
    import hmac
    real_token = os.environ.get("BOOTSTRAP_ADMIN_TOKEN", "").strip()
    if not real_token or not request_token:
        return False
    return hmac.compare_digest(real_token, request_token)


def _any_admin_exists() -> bool:
    row = saas_fetchone("SELECT id FROM app_admins LIMIT 1")
    return row is not None


@app_admin_bp.route("/bootstrap", methods=["GET", "POST"])
def bootstrap():
    token = request.values.get("token", "")

    # Uniform failure response for all three gate conditions — see docstring above.
    if not _bootstrap_token_valid(token) or _any_admin_exists():
        from flask import abort
        abort(404)

    if request.method == "GET":
        return render_template("app_admin/bootstrap.html", token=token)

    # POST — create the first admin
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security validation failed. Please try again.", "danger")
        return render_template("app_admin/bootstrap.html", token=token)

    user_id   = request.form.get("user_id", "").strip()
    full_name = request.form.get("full_name", "").strip()
    email     = request.form.get("email", "").strip().lower()
    mobile    = request.form.get("mobile", "").strip()
    password  = request.form.get("password", "")
    confirm   = request.form.get("confirm_password", "")

    errors = []
    if not user_id or len(user_id) < 3:
        errors.append("User ID must be at least 3 characters.")
    if not full_name:
        errors.append("Full name is required.")
    if not email or "@" not in email:
        errors.append("A valid email is required (used for OTP).")
    if not password or len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if password != confirm:
        errors.append("Passwords do not match.")

    p = P()
    if not errors and saas_fetchone(f"SELECT id FROM app_admins WHERE user_id={p}", (user_id,)):
        errors.append(f"User ID '{user_id}' is already taken.")

    # Re-check race condition: two people hitting this at once shouldn't both succeed.
    if not errors and _any_admin_exists():
        from flask import abort
        abort(404)

    if errors:
        for e in errors:
            flash(e, "danger")
        return render_template("app_admin/bootstrap.html", token=token,
                               user_id=user_id, full_name=full_name,
                               email=email, mobile=mobile)

    saas_execute(
        f"""INSERT INTO app_admins
            (user_id, password_hash, full_name, email, mobile, is_super, is_active)
            VALUES ({p},{p},{p},{p},{p},TRUE,TRUE)""",
        (user_id, generate_password_hash(password), full_name, email, mobile)
    )
    audit_log("app_admin_bootstrap_created", detail=f"user_id={user_id}")

    flash(f"Admin account '{user_id}' created. Log in below to continue setup.", "success")
    return redirect(url_for("unified_login.login"))


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

        admin = auth_service.verify_admin_credentials(user_id, password)

        if not admin:
            audit_log("app_admin_login_failed", status="failure",
                      detail=f"user_id={user_id}")
            flash("Invalid user ID or password.", "danger")
            return render_template("app_admin/login.html", user_id=user_id)

        # ── First factor passed — now require OTP ───────────────────────────
        session[ADMIN_PENDING_KEY] = admin["id"]

        # Dev: email only (no SMS cost while testing). Prod: both channels
        # if a mobile is on file. This policy decision belongs here, in the
        # route, not in OTPManager — the manager itself stays channel-agnostic.
        channel = "both" if (IS_PROD and admin.get("mobile")) else "email"
        _, _, dev_otp = otp_manager.generate_and_send(
            f"admin:{admin['id']}", "admin_login", channel,
            email=admin.get("email"), mobile=admin.get("mobile")
        )

        audit_log("app_admin_password_ok", status="success",
                  detail=f"user_id={user_id}")

        if not IS_PROD:
            # Development convenience: show OTP directly on screen too
            flash(f"Development mode — your OTP is also shown below.", "info")
            return render_template("app_admin/verify_otp.html",
                                   dev_otp=dev_otp, admin_email=admin.get("email", ""))

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

    success, message = otp_manager.verify(f"admin:{admin_pk}", otp, "admin_login")

    if not success:
        audit_log("app_admin_otp_failed", status="failure", detail=message)
        flash(message, "danger")
        return render_template("app_admin/verify_otp.html")

    p = P()
    admin = saas_fetchone(
        f"SELECT * FROM app_admins WHERE id={p} AND is_active=TRUE", (admin_pk,)
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

    channel = "both" if (IS_PROD and admin.get("mobile")) else "email"
    _, _, dev_otp = otp_manager.generate_and_send(
        f"admin:{admin_pk}", "admin_login", channel,
        email=admin.get("email"), mobile=admin.get("mobile")
    )

    audit_log("app_admin_otp_resent", detail=f"admin_id={admin_pk}")

    msg = "OTP resent to your email" + (" and mobile" if channel == "both" else "")
    resp = {"ok": True, "message": msg}
    if not IS_PROD:
        resp["dev_otp"] = dev_otp  # convenience for dev/testing only
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
