"""
modules/saas_auth/routes.py — SaaS Authentication Routes
=========================================================
Blueprint: saas_auth  |  URL prefix: /saas

Routes:
  GET/POST  /saas/signup              — step 1: collect mobile + email + name
  GET/POST  /saas/verify-email        — step 2: verify email OTP
  GET/POST  /saas/verify-mobile       — step 3: verify mobile OTP (prod only)
  GET/POST  /saas/set-pin             — step 4: set 6-digit PIN
  GET/POST  /saas/business-setup      — step 5: create business profile
  GET/POST  /saas/login               — login: mobile + PIN
  GET/POST  /saas/forgot-pin          — request PIN reset OTP
  GET/POST  /saas/reset-pin/<token>   — reset PIN after OTP verification
  GET       /saas/logout              — clear session
  GET       /saas/profile             — view/edit user profile
  GET       /saas/switch-business     — switch active business
  POST      /saas/resend-otp          — resend OTP
"""

from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, jsonify, abort)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import os

from models.saas_auth import (
    saas_fetchone, saas_fetchall, saas_execute, get_saas_db, _is_postgres
)
from utils.otp_service import (
    generate_otp, store_otp, verify_and_consume_otp,
    send_email_otp, send_sms_otp
)
from utils.saas_helpers import (
    set_saas_session, clear_saas_session,
    get_current_saas_user, get_current_business, get_user_businesses,
    generate_slug, generate_reset_token,
    validate_pin, validate_mobile,
    audit_log, generate_csrf_token, validate_csrf, csrf_protect,
    check_rate_limit, get_avatar_initials,
    saas_login_required, saas_business_required,
    SAAS_SESSION_KEY, SAAS_BIZ_KEY, SAAS_ROLE_KEY,
    SAAS_PENDING_USER, SAAS_PENDING_MOBILE, SAAS_PENDING_EMAIL
)

APP_ENV      = os.environ.get("APP_ENV", "development")
IS_PROD      = APP_ENV == "production"
P            = lambda: "%s" if _is_postgres() else "?"

def _require_mobile_verification() -> bool:
    """
    Whether signup requires a verified mobile OTP step. Controlled from
    App Admin → Settings (platform_settings table); falls back to the
    REQUIRE_MOBILE_VERIFICATION env var if no admin has set it yet.
    Defaults to OFF — sending real SMS OTPs to Indian numbers needs DLT
    registration (a TRAI requirement for every SMS provider, not
    specific to whichever one is configured), which isn't set up.
    """
    from utils.platform_settings import get_bool_setting
    return get_bool_setting("require_mobile_verification")

saas_auth_bp = Blueprint("saas_auth", __name__, url_prefix="/saas")


# ══════════════════════════════ HELPERS ══════════════════════════════════════

def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")


def _rate_check(key: str, limit: int = 5, window: int = 300) -> bool:
    if not check_rate_limit(key, limit, window):
        flash("Too many requests. Please wait a few minutes.", "danger")
        return False
    return True


def _get_role_for_user_in_business(user_id, business_id) -> str:
    p = P()
    row = saas_fetchone(
        f"SELECT role FROM saas_user_roles WHERE user_id={p} AND business_id={p}",
        (user_id, business_id)
    )
    return row["role"] if row else "staff"


def _apply_pending_invite_or_continue(user_id: int) -> bool:
    """
    After a user's email/mobile is verified, check if there's a pending
    team invite matching their email or mobile. If found:
      • Create the saas_user_roles membership immediately
      • Mark the invite as accepted
      • Return True (caller should skip business_setup)
    If no invite found, return False (caller proceeds to business_setup
    as normal — this is a brand-new business owner).

    A user can be matched by EITHER their email OR mobile, since the
    inviter may only have had one of the two on hand.
    """
    p = P()
    user = saas_fetchone(f"SELECT * FROM saas_users WHERE id={p}", (user_id,))
    if not user:
        return False

    now = datetime.utcnow().isoformat()
    invite = saas_fetchone(
        f"""SELECT * FROM saas_pending_invites
            WHERE status='pending' AND expires_at > {p}
            AND (email={p} OR mobile={p})
            ORDER BY created_at ASC LIMIT 1""",
        (now, user["email"], user["mobile"])
    )
    if not invite:
        return False

    # Create (or reactivate) the membership
    existing = saas_fetchone(
        f"SELECT * FROM saas_user_roles WHERE user_id={p} AND business_id={p}",
        (user_id, invite["business_id"])
    )
    if existing:
        saas_execute(
            f"UPDATE saas_user_roles SET is_active=1, role={p} WHERE user_id={p} AND business_id={p}",
            (invite["role"], user_id, invite["business_id"])
        )
    else:
        saas_execute(
            f"""INSERT INTO saas_user_roles (user_id, business_id, role, invited_by)
                VALUES ({p},{p},{p},{p})""",
            (user_id, invite["business_id"], invite["role"], invite["invited_by"])
        )

    saas_execute(
        f"""UPDATE saas_pending_invites
            SET status='accepted', accepted_by={p}, accepted_at={p}
            WHERE id={p}""",
        (user_id, now, invite["id"])
    )

    audit_log("invite_auto_accepted", user_id=user_id,
              business_id=invite["business_id"],
              entity_type="invite", entity_id=str(invite["id"]),
              detail=f"role={invite['role']}")

    # Stash so set_pin() knows where to send the user after PIN setup
    session["saas_joined_business_id"] = invite["business_id"]
    session["saas_joined_role"] = invite["role"]
    return True


# ══════════════════════════ CONTEXT PROCESSOR ════════════════════════════════

@saas_auth_bp.context_processor
def saas_globals():
    return {
        "csrf_token":        generate_csrf_token(),
        "saas_user_id":      session.get(SAAS_SESSION_KEY),
        "saas_fullname":     session.get("saas_fullname", ""),
        "saas_role":         session.get(SAAS_ROLE_KEY, ""),
        "saas_business_id":  session.get(SAAS_BIZ_KEY),
        "saas_biz_name":     session.get("saas_biz_name", ""),
        "is_production":     IS_PROD,
    }


# ════════════════════════════ STEP 1: SIGNUP ══════════════════════════════════

@saas_auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get(SAAS_SESSION_KEY):
        return redirect(url_for("saas_auth.profile"))

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security validation failed. Please try again.", "danger")
            return redirect(url_for("saas_auth.signup"))

        full_name = request.form.get("full_name", "").strip()
        mobile    = request.form.get("mobile", "").strip()
        email     = request.form.get("email", "").strip().lower()

        # ── Validate inputs ────────────────────────────────────────────────
        errors = []
        if not full_name or len(full_name) < 2:
            errors.append("Full name must be at least 2 characters.")
        if not email or "@" not in email:
            errors.append("Please enter a valid email address.")

        ok, mobile_or_err = validate_mobile(mobile)
        if not ok:
            errors.append(mobile_or_err)
        else:
            mobile = mobile_or_err  # normalised +91XXXXXXXXXX

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("saas_auth/signup.html",
                                   full_name=full_name, mobile=mobile, email=email)

        # ── Rate limit ─────────────────────────────────────────────────────
        if not _rate_check(f"signup:{_client_ip()}", limit=10, window=3600):
            return render_template("saas_auth/signup.html")

        p = P()

        # ── Check duplicates ───────────────────────────────────────────────
        existing_email  = saas_fetchone(
            f"SELECT id, is_verified FROM saas_users WHERE email={p}", (email,))
        existing_mobile = saas_fetchone(
            f"SELECT id, is_verified FROM saas_users WHERE mobile={p}", (mobile,))

        if existing_email and existing_email["is_verified"]:
            flash("This email is already registered. Please log in.", "warning")
            return redirect(url_for("saas_auth.login"))
        if existing_mobile and existing_mobile["is_verified"]:
            flash("This mobile number is already registered. Please log in.", "warning")
            return redirect(url_for("saas_auth.login"))

        # ── Create or update pending user ──────────────────────────────────
        initials = get_avatar_initials(full_name)

        if existing_email:
            user_id = existing_email["id"]
            saas_execute(
                f"UPDATE saas_users SET full_name={p}, mobile={p}, avatar_initials={p} WHERE id={p}",
                (full_name, mobile, initials, user_id)
            )
        else:
            user_id = saas_execute(
                f"""INSERT INTO saas_users (mobile, email, full_name, avatar_initials, is_verified)
                    VALUES ({p},{p},{p},{p},0)""",
                (mobile, email, full_name, initials)
            )

        # ── Send email OTP ─────────────────────────────────────────────────
        otp = generate_otp()
        store_otp(email, otp, "signup_email")
        sent = send_email_otp(email, otp, "signup_email")

        if not sent:
            flash("Failed to send OTP email. Please check your email address.", "danger")
            return render_template("saas_auth/signup.html",
                                   full_name=full_name, mobile=mobile, email=email)

        # ── Store pending state in session ─────────────────────────────────
        session[SAAS_PENDING_USER]   = user_id
        session[SAAS_PENDING_EMAIL]  = email
        session[SAAS_PENDING_MOBILE] = mobile

        audit_log("signup_initiated", user_id=user_id,
                  detail=f"email={email} mobile={mobile}")

        flash(f"OTP sent to {email}. Please verify to continue.", "info")
        return redirect(url_for("saas_auth.verify_email"))

    return render_template("saas_auth/signup.html")


# ════════════════════════ STEP 2: VERIFY EMAIL OTP ═══════════════════════════

@saas_auth_bp.route("/verify-email", methods=["GET", "POST"])
def verify_email():
    user_id = session.get(SAAS_PENDING_USER)
    email   = session.get(SAAS_PENDING_EMAIL)
    if not user_id or not email:
        flash("Session expired. Please sign up again.", "warning")
        return redirect(url_for("saas_auth.signup"))

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_auth.verify_email"))

        otp = "".join(request.form.get("otp", "").split())  # strip spaces

        if not _rate_check(f"otp_verify:{user_id}", limit=10, window=600):
            return render_template("saas_auth/verify_otp.html",
                                   step="email", email=email)

        success, message = verify_and_consume_otp(email, otp, "signup_email")

        if not success:
            # Idempotency guard: if this OTP was already consumed by an
            # earlier (near-simultaneous) submission for THIS SAME user —
            # e.g. a double-tap or a duplicate auto-submit firing twice
            # before the UI could disable itself — the user is already
            # verified in the database even though THIS request's OTP
            # lookup correctly found nothing left to consume. In that
            # case, treat it as success and move the person forward
            # instead of showing "No OTP request found", which would
            # otherwise read as a failure for someone who actually
            # completed verification correctly on their first attempt.
            already_verified = saas_fetchone(
                f"SELECT is_verified FROM saas_users WHERE id={P()}", (user_id,)
            )
            if already_verified and already_verified.get("is_verified"):
                audit_log("email_verify_idempotent_replay", user_id=user_id,
                          detail="duplicate submit after already-verified")
                flash("Email already verified! Continuing…", "success")
                _apply_pending_invite_or_continue(user_id)
                return redirect(url_for("saas_auth.set_pin"))

            audit_log("email_otp_failed", user_id=user_id, status="failure",
                      detail=message)
            flash(message, "danger")
            return render_template("saas_auth/verify_otp.html",
                                   step="email", email=email)

        audit_log("email_verified", user_id=user_id, detail=f"email={email}")

        if _require_mobile_verification():
            mobile = session.get(SAAS_PENDING_MOBILE, "")
            otp_m  = generate_otp()
            store_otp(mobile, otp_m, "signup_mobile")
            send_sms_otp(mobile, otp_m, "signup_mobile")
            flash(f"Email verified! OTP sent to {mobile[-4:].rjust(10, '*')}.", "info")
            return redirect(url_for("saas_auth.verify_mobile"))
        else:
            # Mobile OTP verification is off (see _require_mobile_verification) —
            # the mobile number is still collected and stored, just not
            # OTP-verified. Mark the account fully verified on email alone.
            p = P()
            saas_execute(
                f"UPDATE saas_users SET is_verified=1 WHERE id={p}", (user_id,)
            )
            # Check for a pending team invite matching this email/mobile —
            # if found, auto-join that business instead of forcing setup.
            redirect_target = _apply_pending_invite_or_continue(user_id)
            flash("Email verified! Now set your 6-digit PIN.", "success")
            return redirect(url_for("saas_auth.set_pin"))

    return render_template("saas_auth/verify_otp.html",
                           step="email", email=email)


# ════════════════════════ STEP 3: VERIFY MOBILE OTP (PROD) ══════════════════

@saas_auth_bp.route("/verify-mobile", methods=["GET", "POST"])
def verify_mobile():
    user_id = session.get(SAAS_PENDING_USER)
    mobile  = session.get(SAAS_PENDING_MOBILE)
    if not user_id or not mobile:
        flash("Session expired. Please sign up again.", "warning")
        return redirect(url_for("saas_auth.signup"))

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_auth.verify_mobile"))

        otp = "".join(request.form.get("otp", "").split())

        if not _rate_check(f"otp_verify_mobile:{user_id}", limit=10, window=600):
            return render_template("saas_auth/verify_otp.html",
                                   step="mobile", mobile=mobile)

        success, message = verify_and_consume_otp(mobile, otp, "signup_mobile")

        if not success:
            # Same idempotency guard as verify_email — a duplicate/race
            # submission after the user already completed mobile
            # verification should move forward, not show a failure.
            already_verified = saas_fetchone(
                f"SELECT is_verified FROM saas_users WHERE id={P()}", (user_id,)
            )
            if already_verified and already_verified.get("is_verified"):
                audit_log("mobile_verify_idempotent_replay", user_id=user_id,
                          detail="duplicate submit after already-verified")
                flash("Mobile already verified! Continuing…", "success")
                return redirect(url_for("saas_auth.set_pin"))

            audit_log("mobile_otp_failed", user_id=user_id, status="failure",
                      detail=message)
            flash(message, "danger")
            return render_template("saas_auth/verify_otp.html",
                                   step="mobile", mobile=mobile)

        p = P()
        saas_execute(
            f"UPDATE saas_users SET is_verified=1 WHERE id={p}", (user_id,)
        )
        audit_log("mobile_verified", user_id=user_id, detail=f"mobile={mobile}")
        _apply_pending_invite_or_continue(user_id)
        flash("Mobile verified! Now set your 6-digit PIN.", "success")
        return redirect(url_for("saas_auth.set_pin"))

    return render_template("saas_auth/verify_otp.html",
                           step="mobile", mobile=mobile)


# ════════════════════════ STEP 4: SET PIN ════════════════════════════════════

@saas_auth_bp.route("/set-pin", methods=["GET", "POST"])
def set_pin():
    user_id = session.get(SAAS_PENDING_USER)
    if not user_id:
        flash("Session expired. Please sign up again.", "warning")
        return redirect(url_for("saas_auth.signup"))

    user = saas_fetchone(
        f"SELECT * FROM saas_users WHERE id = {P()} AND is_verified = 1",
        (user_id,)
    )
    if not user:
        flash("Please complete OTP verification first.", "warning")
        return redirect(url_for("saas_auth.signup"))

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_auth.set_pin"))

        pin     = request.form.get("pin", "")
        confirm = request.form.get("confirm_pin", "")

        if pin != confirm:
            flash("PINs do not match. Please try again.", "danger")
            return render_template("saas_auth/set_pin.html")

        ok, err = validate_pin(pin)
        if not ok:
            flash(err, "danger")
            return render_template("saas_auth/set_pin.html")

        p = P()
        pin_hash = generate_password_hash(pin)
        saas_execute(
            f"UPDATE saas_users SET pin_hash={p}, updated_at={p} WHERE id={p}",
            (pin_hash, datetime.utcnow().isoformat(), user_id)
        )
        audit_log("pin_set", user_id=user_id)

        # ── If this user was invited to an existing business, skip setup ────
        joined_biz_id = session.pop("saas_joined_business_id", None)
        joined_role   = session.pop("saas_joined_role", None)

        if joined_biz_id:
            biz = saas_fetchone(
                f"SELECT * FROM saas_businesses WHERE id={p}", (joined_biz_id,)
            )
            if biz:
                set_saas_session(user, biz, role=joined_role or "staff")
                session.pop(SAAS_PENDING_USER, None)
                session.pop(SAAS_PENDING_EMAIL, None)
                session.pop(SAAS_PENDING_MOBILE, None)
                flash(f"Welcome to {biz['name']}! You've joined as {joined_role}.",
                      "success")
                return redirect(url_for("saas_dashboard.index"))

        flash("PIN set successfully! Now create your business profile.", "success")
        return redirect(url_for("saas_auth.business_setup"))

    return render_template("saas_auth/set_pin.html")


# ════════════════════════ STEP 5: BUSINESS SETUP ════════════════════════════

@saas_auth_bp.route("/business-setup", methods=["GET", "POST"])
def business_setup():
    user_id = session.get(SAAS_PENDING_USER) or session.get(SAAS_SESSION_KEY)
    if not user_id:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("saas_auth.login"))

    user = saas_fetchone(
        f"SELECT * FROM saas_users WHERE id = {P()} AND is_verified = 1",
        (user_id,)
    )
    if not user:
        flash("Account not verified.", "warning")
        return redirect(url_for("saas_auth.signup"))

    from config import ActiveConfig
    states = getattr(ActiveConfig, "INDIAN_STATES", [])

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_auth.business_setup"))

        biz_name      = request.form.get("business_name", "").strip()
        biz_type      = request.form.get("business_type", "retail")
        gstin         = request.form.get("gstin", "").strip().upper()
        pan           = request.form.get("pan", "").strip().upper()
        address       = request.form.get("address", "").strip()
        city          = request.form.get("city", "").strip()
        state_code    = request.form.get("state_code", "27")
        pincode       = request.form.get("pincode", "").strip()
        phone         = request.form.get("phone", "").strip()
        biz_email     = request.form.get("biz_email", "").strip().lower()

        if not biz_name or len(biz_name) < 2:
            flash("Business name must be at least 2 characters.", "danger")
            return render_template("saas_auth/business_setup.html",
                                   states=states, user=user)

        slug = generate_slug(biz_name)
        trial_ends = (datetime.utcnow() + timedelta(days=14)).isoformat()
        p = P()

        biz_id = saas_execute(
            f"""INSERT INTO saas_businesses
                (name, slug, gstin, pan, address, city, state_code, pincode,
                 phone, email, business_type, plan, trial_ends_at, created_by)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},'free',{p},{p})""",
            (biz_name, slug, gstin, pan, address, city, state_code, pincode,
             phone, biz_email, biz_type, trial_ends, user_id)
        )

        # Assign owner role
        saas_execute(
            f"INSERT INTO saas_user_roles (user_id, business_id, role) VALUES ({p},{p},'owner')",
            (user_id, biz_id)
        )

        # Seed the standard Chart of Accounts so this business's books are
        # ready for double-entry posting from the moment it's created.
        from utils.chart_of_accounts import seed_chart_of_accounts
        seed_chart_of_accounts(biz_id, created_by=user_id)

        biz = saas_fetchone(
            f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,)
        )
        set_saas_session(user, biz, role="owner")

        # Clear pending signup state
        session.pop(SAAS_PENDING_USER, None)
        session.pop(SAAS_PENDING_EMAIL, None)
        session.pop(SAAS_PENDING_MOBILE, None)

        audit_log("business_created", user_id=user_id, business_id=biz_id,
                  entity_type="business", entity_id=str(biz_id),
                  detail=f"name={biz_name} slug={slug}")

        # Best-effort welcome email — signup must succeed regardless of
        # whether this send works (matches send_welcome_email's own
        # fail-soft design, but belt-and-suspenders here too).
        try:
            from notification.email_service import send_welcome_email
            send_welcome_email(user["email"], user["full_name"], biz_name)
        except Exception as e:
            print(f"[business_setup] welcome email failed (non-fatal): {e}")

        flash(f"Welcome to BizManager, {user['full_name']}! Your business is ready.", "success")
        return redirect(url_for("saas_dashboard.index"))

    return render_template("saas_auth/business_setup.html",
                           states=states, user=user)


# ════════════════════════════ LOGIN ══════════════════════════════════════════

@saas_auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get(SAAS_SESSION_KEY):
        return redirect(url_for("saas_dashboard.index"))

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return render_template("saas_auth/login.html")

        mobile = request.form.get("mobile", "").strip()
        pin    = request.form.get("pin", "")

        ok, mobile_norm = validate_mobile(mobile)
        if not ok:
            flash(mobile_norm, "danger")
            return render_template("saas_auth/login.html", mobile=mobile)

        # ── Rate limit on login attempts ───────────────────────────────────
        rl_key = f"login:{mobile_norm}:{_client_ip()}"
        if not check_rate_limit(rl_key, max_requests=10, window_seconds=600):
            audit_log("login_rate_limited", status="failure",
                      detail=f"mobile={mobile_norm}")
            flash("Too many login attempts. Please wait 10 minutes.", "danger")
            return render_template("saas_auth/login.html", mobile=mobile)

        p = P()
        user = saas_fetchone(
            f"SELECT * FROM saas_users WHERE mobile={p} AND is_active=1",
            (mobile_norm,)
        )

        if not user:
            audit_log("login_user_not_found", status="failure",
                      detail=f"mobile={mobile_norm}")
            flash("Mobile number not registered. Please sign up.", "warning")
            return render_template("saas_auth/login.html", mobile=mobile)

        if not user.get("is_verified"):
            flash("Account not verified. Please complete signup.", "warning")
            session[SAAS_PENDING_USER]   = user["id"]
            session[SAAS_PENDING_EMAIL]  = user["email"]
            session[SAAS_PENDING_MOBILE] = mobile_norm
            return redirect(url_for("saas_auth.verify_email"))

        if not user.get("pin_hash"):
            flash("No PIN set. Please complete registration.", "warning")
            session[SAAS_PENDING_USER]   = user["id"]
            return redirect(url_for("saas_auth.set_pin"))

        if not check_password_hash(user["pin_hash"], pin):
            audit_log("login_failed", user_id=user["id"], status="failure",
                      detail="wrong_pin")
            flash("Incorrect PIN. Please try again.", "danger")
            return render_template("saas_auth/login.html", mobile=mobile)

        # ── Success: fetch businesses ──────────────────────────────────────
        businesses = get_user_businesses(user["id"])

        if not businesses:
            # No business yet — go create one
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
            biz  = businesses[0]
            role = biz["role"]
            set_saas_session(user, biz, role=role)
        else:
            # Multiple businesses → let user pick
            session[SAAS_PENDING_USER] = user["id"]
            return redirect(url_for("saas_auth.select_business"))

        saas_execute(
            f"UPDATE saas_users SET last_login={p} WHERE id={p}",
            (datetime.utcnow().isoformat(), user["id"])
        )
        audit_log("login_success", user_id=user["id"],
                  business_id=session.get(SAAS_BIZ_KEY))
        flash(f"Welcome back, {user['full_name']}!", "success")
        return redirect(url_for("saas_dashboard.index"))

    return render_template("saas_auth/login.html")


# ════════════════════════ SELECT BUSINESS ════════════════════════════════════

@saas_auth_bp.route("/select-business", methods=["GET", "POST"])
def select_business():
    user_id = session.get(SAAS_PENDING_USER) or session.get(SAAS_SESSION_KEY)
    if not user_id:
        return redirect(url_for("saas_auth.login"))

    user       = saas_fetchone(f"SELECT * FROM saas_users WHERE id={P()}", (user_id,))
    businesses = get_user_businesses(user_id)

    if request.method == "POST":
        biz_id = request.form.get("business_id", type=int)
        if not biz_id:
            flash("Please select a business.", "warning")
            return render_template("saas_auth/select_business.html",
                                   businesses=businesses)

        biz  = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={P()}", (biz_id,))
        role = _get_role_for_user_in_business(user_id, biz_id)

        if not biz:
            flash("Business not found.", "danger")
            return render_template("saas_auth/select_business.html",
                                   businesses=businesses)

        set_saas_session(user, biz, role=role)
        session.pop(SAAS_PENDING_USER, None)
        saas_execute(
            f"UPDATE saas_users SET last_login={P()} WHERE id={P()}",
            (datetime.utcnow().isoformat(), user_id)
        )
        audit_log("business_selected", user_id=user_id, business_id=biz_id)
        return redirect(url_for("saas_dashboard.index"))

    return render_template("saas_auth/select_business.html",
                           businesses=businesses, user=user)


# ════════════════════════ FORGOT PIN ═════════════════════════════════════════

@saas_auth_bp.route("/forgot-pin", methods=["GET", "POST"])
def forgot_pin():
    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error.", "danger")
            return redirect(url_for("saas_auth.forgot_pin"))

        mobile = request.form.get("mobile", "").strip()
        ok, mobile_norm = validate_mobile(mobile)
        if not ok:
            flash(mobile_norm, "danger")
            return render_template("saas_auth/forgot_pin.html", mobile=mobile)

        if not _rate_check(f"forgot:{mobile_norm}", limit=3, window=600):
            return render_template("saas_auth/forgot_pin.html", mobile=mobile)

        p = P()
        user = saas_fetchone(
            f"SELECT * FROM saas_users WHERE mobile={p} AND is_active=1 AND is_verified=1",
            (mobile_norm,)
        )

        # Always show the same message to prevent user enumeration
        flash("If this number is registered, an OTP has been sent.", "info")

        if user:
            otp = generate_otp()
            store_otp(mobile_norm, otp, "pin_reset")

            if _require_mobile_verification():
                send_sms_otp(mobile_norm, otp, "pin_reset")
            # Also send to email as backup
            send_email_otp(user["email"], otp, "pin_reset")

            session["reset_user_id"] = user["id"]
            session["reset_mobile"]  = mobile_norm
            audit_log("pin_reset_requested", user_id=user["id"])
            return redirect(url_for("saas_auth.verify_reset_otp"))

    return render_template("saas_auth/forgot_pin.html")


# ════════════════════════ VERIFY RESET OTP ═══════════════════════════════════

@saas_auth_bp.route("/verify-reset-otp", methods=["GET", "POST"])
def verify_reset_otp():
    user_id = session.get("reset_user_id")
    mobile  = session.get("reset_mobile")
    if not user_id:
        flash("Session expired. Please try again.", "warning")
        return redirect(url_for("saas_auth.forgot_pin"))

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error.", "danger")
            return redirect(url_for("saas_auth.verify_reset_otp"))

        otp = "".join(request.form.get("otp", "").split())

        if not _rate_check(f"reset_otp:{user_id}", limit=10, window=600):
            return render_template("saas_auth/verify_otp.html",
                                   step="pin_reset", mobile=mobile)

        success, message = verify_and_consume_otp(mobile, otp, "pin_reset")
        if not success:
            audit_log("reset_otp_failed", user_id=user_id, status="failure")
            flash(message, "danger")
            return render_template("saas_auth/verify_otp.html",
                                   step="pin_reset", mobile=mobile)

        # Generate a short-lived reset token
        token = generate_reset_token()
        expires = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
        p = P()
        saas_execute(
            f"INSERT INTO saas_pin_reset (user_id, token, expires_at) VALUES ({p},{p},{p})",
            (user_id, token, expires)
        )
        session.pop("reset_user_id", None)
        session.pop("reset_mobile", None)
        audit_log("reset_otp_verified", user_id=user_id)
        return redirect(url_for("saas_auth.reset_pin", token=token))

    return render_template("saas_auth/verify_otp.html",
                           step="pin_reset", mobile=mobile)


# ════════════════════════ RESET PIN ══════════════════════════════════════════

@saas_auth_bp.route("/reset-pin/<token>", methods=["GET", "POST"])
def reset_pin(token):
    p   = P()
    row = saas_fetchone(
        f"SELECT * FROM saas_pin_reset WHERE token={p} AND used_at IS NULL",
        (token,)
    )

    if not row:
        flash("Invalid or expired reset link.", "danger")
        return redirect(url_for("saas_auth.forgot_pin"))

    if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
        flash("Reset link has expired. Please request a new one.", "danger")
        return redirect(url_for("saas_auth.forgot_pin"))

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error.", "danger")
            return redirect(url_for("saas_auth.reset_pin", token=token))

        pin     = request.form.get("pin", "")
        confirm = request.form.get("confirm_pin", "")

        if pin != confirm:
            flash("PINs do not match.", "danger")
            return render_template("saas_auth/reset_pin.html", token=token)

        ok, err = validate_pin(pin)
        if not ok:
            flash(err, "danger")
            return render_template("saas_auth/reset_pin.html", token=token)

        pin_hash = generate_password_hash(pin)
        now = datetime.utcnow().isoformat()

        saas_execute(
            f"UPDATE saas_users SET pin_hash={p}, updated_at={p} WHERE id={p}",
            (pin_hash, now, row["user_id"])
        )
        saas_execute(
            f"UPDATE saas_pin_reset SET used_at={p} WHERE token={p}",
            (now, token)
        )
        audit_log("pin_reset_success", user_id=row["user_id"])
        flash("PIN reset successfully! Please log in with your new PIN.", "success")
        return redirect(url_for("saas_auth.login"))

    return render_template("saas_auth/reset_pin.html", token=token)


# ════════════════════════════ RESEND OTP ═════════════════════════════════════

@saas_auth_bp.route("/resend-otp", methods=["POST"])
def resend_otp():
    purpose = request.form.get("purpose", "signup_email")
    user_id = session.get(SAAS_PENDING_USER)
    if not user_id:
        return jsonify({"ok": False, "message": "Session expired."})

    if not _rate_check(f"resend:{user_id}", limit=3, window=300):
        return jsonify({"ok": False, "message": "Too many resend requests."})

    user = saas_fetchone(
        f"SELECT * FROM saas_users WHERE id={P()}", (user_id,)
    )
    if not user:
        return jsonify({"ok": False, "message": "User not found."})

    otp = generate_otp()
    if "email" in purpose:
        store_otp(user["email"], otp, purpose)
        send_email_otp(user["email"], otp, purpose)
        msg = f"OTP resent to {user['email']}"
    else:
        mobile = session.get(SAAS_PENDING_MOBILE, user["mobile"])
        store_otp(mobile, otp, purpose)
        send_sms_otp(mobile, otp, purpose)
        msg = f"OTP resent to {mobile[-4:].rjust(10, '*')}"

    audit_log("otp_resent", user_id=user_id, detail=f"purpose={purpose}")
    return jsonify({"ok": True, "message": msg})


# ════════════════════════════ LOGOUT ═════════════════════════════════════════

@saas_auth_bp.route("/logout")
def logout():
    uid = session.get(SAAS_SESSION_KEY)
    if uid:
        audit_log("logout", user_id=uid)
    clear_saas_session()
    flash("You have been signed out.", "info")
    return redirect(url_for("saas_auth.login"))


# ════════════════════════════ PROFILE ════════════════════════════════════════

@saas_auth_bp.route("/profile", methods=["GET", "POST"])
@saas_login_required
def profile():
    user = get_current_saas_user()
    if not user:
        return redirect(url_for("saas_auth.login"))

    businesses = get_user_businesses(user["id"])
    p = P()

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error.", "danger")
            return redirect(url_for("saas_auth.profile"))

        full_name = request.form.get("full_name", "").strip()
        timezone  = request.form.get("timezone", "Asia/Kolkata")

        if not full_name or len(full_name) < 2:
            flash("Full name must be at least 2 characters.", "danger")
            return render_template("saas_auth/profile.html",
                                   user=user, businesses=businesses)

        initials = get_avatar_initials(full_name)
        saas_execute(
            f"UPDATE saas_users SET full_name={p}, timezone={p}, "
            f"avatar_initials={p}, updated_at={p} WHERE id={p}",
            (full_name, timezone, initials, datetime.utcnow().isoformat(), user["id"])
        )
        session["saas_fullname"] = full_name
        audit_log("profile_updated", user_id=user["id"])
        flash("Profile updated successfully.", "success")
        return redirect(url_for("saas_auth.profile"))

    # Recent audit log for this user
    audit_rows = saas_fetchall(
        f"SELECT * FROM saas_audit_logs WHERE user_id={p} "
        f"ORDER BY created_at DESC LIMIT 10",
        (user["id"],)
    )

    return render_template("saas_auth/profile.html",
                           user=user,
                           businesses=businesses,
                           audit_logs=audit_rows)


# ════════════════════════ BUSINESS SETTINGS ══════════════════════════════════

@saas_auth_bp.route("/business-settings", methods=["GET", "POST"])
@saas_business_required
def business_settings():
    """
    Edit the CURRENT business's profile (name, GSTIN, address, etc.).
    Owner and Manager only — same visibility rule as the nav link.
    Distinct from /saas/business-setup, which only ever CREATES a new
    business; this route EDITS the one already selected in session.
    """
    role = session.get(SAAS_ROLE_KEY, "staff")
    if role not in ("owner", "manager"):
        flash("Only the owner or manager can edit business settings.", "danger")
        return redirect(url_for("saas_dashboard.index"))

    biz_id = session.get(SAAS_BIZ_KEY)
    p = P()
    biz = saas_fetchone(
        f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,)
    )
    if not biz:
        flash("Business not found.", "danger")
        return redirect(url_for("saas_dashboard.index"))

    from config import ActiveConfig
    states = getattr(ActiveConfig, "INDIAN_STATES", [])

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_auth.business_settings"))

        biz_name   = request.form.get("business_name", "").strip()
        biz_type   = request.form.get("business_type", biz.get("business_type", "retail"))
        gstin      = request.form.get("gstin", "").strip().upper()
        pan        = request.form.get("pan", "").strip().upper()
        address    = request.form.get("address", "").strip()
        city       = request.form.get("city", "").strip()
        state_code = request.form.get("state_code", biz.get("state_code", "27"))
        pincode    = request.form.get("pincode", "").strip()
        phone      = request.form.get("phone", "").strip()
        biz_email  = request.form.get("biz_email", "").strip().lower()

        if not biz_name or len(biz_name) < 2:
            flash("Business name must be at least 2 characters.", "danger")
            return render_template("saas_auth/business_settings.html",
                                   biz=biz, states=states)

        saas_execute(
            f"""UPDATE saas_businesses SET
                name={p}, business_type={p}, gstin={p}, pan={p}, address={p},
                city={p}, state_code={p}, pincode={p}, phone={p}, email={p},
                updated_at={p}
                WHERE id={p}""",
            (biz_name, biz_type, gstin, pan, address, city, state_code,
             pincode, phone, biz_email, datetime.utcnow().isoformat(), biz_id)
        )

        # Keep session display name in sync if it changed
        if biz_name != biz.get("name"):
            session["saas_biz_name"] = biz_name

        audit_log("business_settings_updated", business_id=biz_id,
                  entity_type="business", entity_id=str(biz_id),
                  detail=f"name={biz_name}")

        flash("Business settings updated successfully.", "success")
        return redirect(url_for("saas_auth.business_settings"))

    return render_template("saas_auth/business_settings.html",
                           biz=biz, states=states)


# ════════════════════════ SWITCH BUSINESS ════════════════════════════════════

@saas_auth_bp.route("/switch-business/<int:biz_id>")
@saas_login_required
def switch_business(biz_id):
    uid  = session.get(SAAS_SESSION_KEY)
    role = _get_role_for_user_in_business(uid, biz_id)
    biz  = saas_fetchone(
        f"SELECT * FROM saas_businesses WHERE id={P()} AND is_active=1",
        (biz_id,)
    )
    if not biz or not role:
        flash("Business not found or access denied.", "danger")
        return redirect(url_for("saas_auth.profile"))

    user = get_current_saas_user()
    set_saas_session(user, biz, role=role)
    audit_log("business_switched", user_id=uid, business_id=biz_id)
    flash(f"Switched to {biz['name']}.", "success")
    return redirect(url_for("saas_dashboard.index"))
