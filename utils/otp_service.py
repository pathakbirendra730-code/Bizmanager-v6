"""
utils/otp_service.py — OTP Generation, Delivery & Verification
===============================================================
Behaviour:
  - APP_ENV=development  → prints OTP to console AND sends real email if SMTP is configured
  - APP_ENV=production   → always sends via configured provider; never prints OTP to console

Fixes vs v1:
  • IS_PRODUCTION evaluated on every call (not frozen at import time)
  • Dev mode sends real email when SMTP_USER + SMTP_PASS are set
  • SMTP errors surfaced with full tracebacks in dev; swallowed+logged in prod
  • Added plain-text fallback alongside HTML in MIME email
  • store_otp uses OTP_EXPIRY_MINUTES read at call time (not import time)

Environment variables:
  APP_ENV             = development | production      (default: development)
  EMAIL_PROVIDER      = smtp | gmail | brevo | sendgrid | ses  (default: smtp)
                        (email sending itself lives in notification/ package)

  SMS_PROVIDER        = twilio | fast2sms | msg91 | brevo   (default: fast2sms)
  TWILIO_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM
  FAST2SMS_API_KEY
  MSG91_AUTH_KEY / MSG91_TEMPLATE_ID / MSG91_SENDER_ID
  BREVO_API_KEY       — shared with EMAIL_PROVIDER=brevo (same account)
  BREVO_SMS_SENDER    = BizMgr   (default; max 11 chars, alphanumeric)

  OTP_EXPIRY_MINUTES  = 10   (default)
  OTP_LENGTH          = 6    (default)
"""

import os
import random
import string
import traceback
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash, check_password_hash
from models.saas_auth import get_saas_db, saas_execute, _is_postgres


# ── Runtime config helpers (read env on every call, never cached) ─────────────

def _app_env():
    return os.environ.get("APP_ENV", "development").lower()

def _is_production():
    return _app_env() == "production"

def _otp_expiry():
    return int(os.environ.get("OTP_EXPIRY_MINUTES", 10))

def _otp_length():
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
        if datetime.utcnow() > datetime.fromisoformat(token["expires_at"]):
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
    Send OTP via SMS.
    Dev: prints to console. Also attempts real SMS if provider is configured.
    Prod: always sends real SMS; returns False on failure.
    """
    message  = _build_sms_content(otp, purpose)
    is_prod  = _is_production()
    try:
        from utils.platform_settings import get_setting
        provider = get_setting("sms_provider").lower().strip()
    except Exception:
        provider = os.environ.get("SMS_PROVIDER", "fast2sms").lower()

    if not is_prod:
        _print_otp_console("SMS", mobile, otp, purpose)

    sms_key = ("FAST2SMS_API_KEY" if provider == "fast2sms" else
                "TWILIO_SID"       if provider == "twilio"   else
                "BREVO_API_KEY"    if provider == "brevo"    else
                "MSG91_AUTH_KEY")

    if provider == "brevo":
        try:
            from utils.platform_settings import is_secret_set
            has_creds = is_secret_set("brevo_api_key") or bool(os.environ.get(sms_key, "").strip())
        except Exception:
            has_creds = bool(os.environ.get(sms_key, "").strip())
    else:
        has_creds = bool(os.environ.get(sms_key, "").strip())

    if not has_creds:
        if not is_prod:
            print(f"[OTP] ℹ️  No SMS credentials ({sms_key}). OTP shown in console above.")
            return True
        print(f"[OTP] ❌ Production SMS failed: {sms_key} not set.")
        return False

    try:
        if provider == "twilio":
            ok = _send_via_twilio(mobile, message)
        elif provider == "msg91":
            ok = _send_via_msg91(mobile, otp)
        elif provider == "brevo":
            ok = _send_via_brevo_sms(mobile, message)
        else:
            ok = _send_via_fast2sms(mobile, message)

        if ok:
            print(f"[OTP] ✅ SMS sent via {provider} → {mobile}")
        return ok if is_prod else True

    except Exception as e:
        print(f"[OTP] SMS send error ({provider}): {e}")
        if not is_prod:
            traceback.print_exc()
        return False if is_prod else True


# ─── Console helper ────────────────────────────────────────────────────────────

def _print_otp_console(channel: str, destination: str, otp: str, purpose: str):
    icon = "📧" if channel == "EMAIL" else "📱"
    print("\n" + "═" * 52)
    print(f"  {icon}  {channel} OTP  [{purpose.upper()}]")
    print(f"  To      : {destination}")
    print(f"  OTP     : {otp}   (expires in {_otp_expiry()} min)")
    print(f"  Env     : {_app_env()}")
    print("═" * 52 + "\n")


def _build_sms_content(otp: str, purpose: str) -> str:
    labels = {
        "signup_email":  "email verification",
        "signup_mobile": "mobile verification",
        "pin_reset":     "PIN reset",
        "login":         "login",
    }
    label = labels.get(purpose, "verification")
    return f"BizManager: OTP for {label} is {otp}. Valid {_otp_expiry()} mins. Do NOT share. -BizManager"


# ─── SMS providers ────────────────────────────────────────────────────────────

def _send_via_twilio(mobile: str, message: str) -> bool:
    from twilio.rest import Client
    client = Client(os.environ["TWILIO_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    msg = client.messages.create(
        body=message,
        from_=os.environ["TWILIO_FROM"],
        to=mobile
    )
    return msg.sid is not None


def _send_via_fast2sms(mobile: str, message: str) -> bool:
    import urllib.request, json, urllib.parse
    api_key = os.environ["FAST2SMS_API_KEY"]
    number  = mobile.lstrip("+").lstrip("91")[-10:]  # 10-digit only
    payload = json.dumps({
        "route":         "q",
        "message":       message,
        "language":      "english",
        "flash":         0,
        "numbers":       number,
    }).encode()
    req = urllib.request.Request(
        "https://www.fast2sms.com/dev/bulkV2",
        data=payload,
        headers={
            "authorization": api_key,
            "Content-Type":  "application/json",
            "Cache-Control": "no-cache",
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        return data.get("return", False)


def _send_via_msg91(mobile: str, otp: str) -> bool:
    import urllib.request, json
    payload = json.dumps({
        "template_id": os.environ["MSG91_TEMPLATE_ID"],
        "short_url":   "0",
        "mobiles":     mobile.lstrip("+"),
        "VAR1":        otp,
    }).encode()
    req = urllib.request.Request(
        "https://api.msg91.com/api/v5/otp",
        data=payload,
        headers={
            "authkey":      os.environ["MSG91_AUTH_KEY"],
            "Content-Type": "application/json",
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        return data.get("type") == "success"


def _send_via_brevo_sms(mobile: str, message: str) -> bool:
    """Brevo transactional SMS API — same account/API key as Brevo email,
    so SMS_PROVIDER=brevo + EMAIL_PROVIDER=brevo share one BREVO_API_KEY."""
    import urllib.request, json
    try:
        from utils.platform_settings import get_setting
        api_key = get_setting("brevo_api_key").strip() or os.environ.get("BREVO_API_KEY", "")
        sender  = (get_setting("brevo_sms_sender").strip()
                   or os.environ.get("BREVO_SMS_SENDER", "BizMgr"))
    except Exception:
        api_key = os.environ["BREVO_API_KEY"]
        sender  = os.environ.get("BREVO_SMS_SENDER", "BizMgr")

    number = mobile if mobile.startswith("+") else f"+91{mobile.lstrip('0')}"
    payload = json.dumps({
        "sender":  sender[:11],
        "recipient": number,
        "content": message,
        "type":    "transactional",
    }).encode()
    req = urllib.request.Request(
        "https://api.brevo.com/v3/transactionalSMS/sms",
        data=payload,
        headers={
            "api-key":      api_key,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        return "reference" in data or resp.status in (200, 201)
