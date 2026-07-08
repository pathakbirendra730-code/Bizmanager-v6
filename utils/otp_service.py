"""
utils/otp_service.py — OTP Generation, Storage & Verification

This file owns generating, hashing, storing, and verifying OTPs.
Actually SENDING them (email or SMS) is delegated entirely to the
notification/ package as of Phase 2 — see notification/email_service.py
and notification/sms_service.py, and notification/manager.py for the
provider selection, retry, failover, and logging behind that.

Behaviour (unchanged from before that migration):
  - APP_ENV=development  → prints OTP to console AND attempts a real
                           send if a provider is configured
  - APP_ENV=production   → always sends via the configured provider;
                           never prints OTP to console

Environment variables — provider selection and credentials are read by
notification/, in this order: DB setting (App Admin → Settings) then
env var. Listed here for reference; set via whichever you prefer:

  APP_ENV             = development | production      (default: development)
  EMAIL_PROVIDER      = smtp | gmail | brevo | sendgrid | ses  (default: smtp)
  SMS_PROVIDER        = twilio | fast2sms | msg91 | brevo      (default: fast2sms)

  OTP_EXPIRY_MINUTES  = 10   (default; also App Admin -> Settings)
  OTP_LENGTH          = 6    (default; also App Admin -> Settings)
"""

import os
import random
import string
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash, check_password_hash
from models.saas_auth import get_saas_db, saas_execute, _is_postgres, parse_dt


# ── Runtime config helpers (read env on every call, never cached) ─────────────

def _app_env():
    return os.environ.get("APP_ENV", "development").lower()

def _is_production():
    return _app_env() == "production"

def _otp_expiry():
    try:
        from utils.platform_settings import get_int_setting
        return get_int_setting("otp_expiry_minutes")
    except Exception:
        return int(os.environ.get("OTP_EXPIRY_MINUTES", 10))

def _otp_length():
    try:
        from utils.platform_settings import get_int_setting
        return get_int_setting("otp_length")
    except Exception:
        return int(os.environ.get("OTP_LENGTH", 6))


# ═════════════════════════════ OTP GENERATION ═════════════════════════════════

def generate_otp() -> str:
    """Generate a cryptographically random numeric OTP."""
    return ''.join(random.SystemRandom().choices(string.digits, k=_otp_length()))


def hash_otp(otp: str) -> str:
    return generate_password_hash(otp)


def verify_otp_hash(otp: str, otp_hash: str) -> bool:
    return check_password_hash(otp_hash, otp)


# ═════════════════════════════ OTP STORAGE ════════════════════════════════════

def _ph():
    return "%s" if _is_postgres() else "?"


def store_otp(identifier: str, otp: str, purpose: str) -> bool:
    """
    Invalidate any previous unused OTPs for identifier+purpose,
    then store the new hashed OTP. Returns True on success.
    """
    p          = _ph()
    expires_at = (datetime.utcnow() + timedelta(minutes=_otp_expiry())).isoformat()
    otp_hash   = hash_otp(otp)

    conn = get_saas_db()
    c    = conn.cursor()
    try:
        c.execute(
            f"UPDATE saas_otp_tokens SET used_at={p} "
            f"WHERE identifier={p} AND purpose={p} AND used_at IS NULL",
            (datetime.utcnow().isoformat(), identifier, purpose)
        )
        c.execute(
            f"INSERT INTO saas_otp_tokens "
            f"(identifier, otp_hash, purpose, expires_at) VALUES ({p},{p},{p},{p})",
            (identifier, otp_hash, purpose, expires_at)
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"[OTP] store_otp error: {e}")
        return False
    finally:
        conn.close()


def verify_and_consume_otp(identifier: str, otp: str, purpose: str) -> tuple[bool, str]:
    """
    Verify OTP. Returns (success, message).
    Increments attempt counter before checking — prevents timing attacks.
    Marks token as used on success to prevent replay.
    """
    p    = _ph()
    conn = get_saas_db()
    c    = conn.cursor()
    try:
        c.execute(
            f"SELECT * FROM saas_otp_tokens "
            f"WHERE identifier={p} AND purpose={p} AND used_at IS NULL "
            f"ORDER BY created_at DESC LIMIT 1",
            (identifier, purpose)
        )
        row = c.fetchone()

        if not row:
            return False, "No OTP request found. Please request a new OTP."

        token = dict(row)

        # Expiry check
        if datetime.utcnow() > parse_dt(token["expires_at"]):
            return False, "OTP has expired. Please request a new one."

        # Attempt-limit check (before incrementing to show correct count)
        if token["attempts"] >= token["max_attempts"]:
            return False, "Too many incorrect attempts. Please request a new OTP."

        # Increment attempts first (blocks brute-force even on timing issues)
        c.execute(
            f"UPDATE saas_otp_tokens SET attempts=attempts+1 WHERE id={p}",
            (token["id"],)
        )
        conn.commit()

        # Hash comparison
        if not verify_otp_hash(otp, token["otp_hash"]):
            remaining = token["max_attempts"] - token["attempts"] - 1
            msg = (f"Incorrect OTP. {remaining} attempt(s) remaining."
                   if remaining > 0 else "No attempts remaining. Please request a new OTP.")
            return False, msg

        # Mark as consumed
        c.execute(
            f"UPDATE saas_otp_tokens SET used_at={p} WHERE id={p}",
            (datetime.utcnow().isoformat(), token["id"])
        )
        conn.commit()
        return True, "OTP verified successfully."

    except Exception as e:
        print(f"[OTP] verify_and_consume_otp error: {e}")
        return False, "Verification error. Please try again."
    finally:
        conn.close()


# ═════════════════════════════ EMAIL DELIVERY ═════════════════════════════════

def send_email_otp(email: str, otp: str, purpose: str) -> bool:
    """
    Send OTP email. Delegates to notification.email_service, which
    picks the configured provider (smtp/gmail/brevo/sendgrid/ses) and
    handles the dev/prod send behaviour described in that package.

    In development this also prints the OTP to console for convenience
    — kept here (rather than in the notification package) since it's
    specific to the OTP flow, not a general "every email" behaviour.
    """
    if not _is_production():
        _print_otp_console("EMAIL", email, otp, purpose)

    from notification.email_service import send_otp_email
    return send_otp_email(email, otp, purpose)


def send_sms_otp(mobile: str, otp: str, purpose: str) -> bool:
    """
    Send OTP via SMS. Delegates to notification.sms_service, which picks
    the configured provider (fast2sms/twilio/msg91/brevo) and handles
    retry, failover, and logging — see notification/manager.py.

    In development this also prints the OTP to console for convenience
    — kept here (rather than in the notification package) since it's
    specific to the OTP flow, not a general "every SMS" behaviour.
    """
    if not _is_production():
        _print_otp_console("SMS", mobile, otp, purpose)

    from notification.sms_service import send_otp_sms
    return send_otp_sms(mobile, otp, purpose)


# ─── Console helper ────────────────────────────────────────────────────────────

def _print_otp_console(channel: str, destination: str, otp: str, purpose: str):
    icon = "📧" if channel == "EMAIL" else "📱"
    print("\n" + "═" * 52)
    print(f"  {icon}  {channel} OTP  [{purpose.upper()}]")
    print(f"  To      : {destination}")
    print(f"  OTP     : {otp}   (expires in {_otp_expiry()} min)")
    print(f"  Env     : {_app_env()}")
    print("═" * 52 + "\n")

