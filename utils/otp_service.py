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
  EMAIL_PROVIDER      = smtp | sendgrid | ses         (default: smtp)
  SMTP_HOST           = smtp.gmail.com                (default)
  SMTP_PORT           = 587                           (default)
  SMTP_USER           = your@gmail.com
  SMTP_PASS           = your_app_password
  SMTP_FROM           = BizManager <your@gmail.com>   (default: SMTP_USER)

  SMS_PROVIDER        = twilio | fast2sms | msg91     (default: fast2sms)
  TWILIO_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM
  FAST2SMS_API_KEY
  MSG91_AUTH_KEY / MSG91_TEMPLATE_ID / MSG91_SENDER_ID

  OTP_EXPIRY_MINUTES  = 10   (default)
  OTP_LENGTH          = 6    (default)
"""

import os
import random
import string
import smtplib
import traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

def _smtp_configured():
    """True when SMTP_USER and SMTP_PASS are both non-empty."""
    return bool(os.environ.get("SMTP_USER", "").strip() and
                os.environ.get("SMTP_PASS", "").strip())


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
    Send OTP email.

    Logic:
      • Always tries to send a real email when SMTP_USER + SMTP_PASS are set.
      • In development, ALSO prints OTP to console for convenience.
      • In production, only sends real email (no console print).
      • If email send fails in dev → logs error but returns True so signup
        flow can continue (OTP is still visible in console).
      • If email send fails in prod → returns False so the route shows an error.
    """
    subject, html_body, plain_body = _build_email_content(otp, purpose)
    is_prod = _is_production()

    # ── Always print in development ───────────────────────────────────────────
    if not is_prod:
        _print_otp_console("EMAIL", email, otp, purpose)

    # ── Attempt real email delivery ───────────────────────────────────────────
    if _smtp_configured() or is_prod:
        provider = os.environ.get("EMAIL_PROVIDER", "smtp").lower()
        try:
            if provider == "sendgrid":
                ok = _send_via_sendgrid(email, subject, html_body)
            elif provider == "ses":
                ok = _send_via_ses(email, subject, html_body)
            else:
                ok = _send_via_smtp(email, subject, html_body, plain_body)

            if ok:
                print(f"[OTP] ✅ Email sent via {provider} → {email}")
            return ok if is_prod else True   # dev: don't fail even if email fails

        except Exception as e:
            _log_email_error(provider, email, e, is_prod)
            return False if is_prod else True  # dev: still return True (console OTP visible)

    # Dev with no SMTP configured — console-only is fine
    if not is_prod:
        print("[OTP] ℹ️  No SMTP configured. OTP shown in console above.")
        return True

    # Production with no SMTP — this is a misconfiguration
    print("[OTP] ❌ Production email failed: SMTP_USER / SMTP_PASS not set.")
    return False


def send_sms_otp(mobile: str, otp: str, purpose: str) -> bool:
    """
    Send OTP via SMS.
    Dev: prints to console. Also attempts real SMS if provider is configured.
    Prod: always sends real SMS; returns False on failure.
    """
    message  = _build_sms_content(otp, purpose)
    is_prod  = _is_production()
    provider = os.environ.get("SMS_PROVIDER", "fast2sms").lower()

    if not is_prod:
        _print_otp_console("SMS", mobile, otp, purpose)

    sms_key = ("FAST2SMS_API_KEY" if provider == "fast2sms" else
                "TWILIO_SID"       if provider == "twilio"   else
                "MSG91_AUTH_KEY")

    if not os.environ.get(sms_key, "").strip():
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


def _log_email_error(provider: str, email: str, exc: Exception, is_prod: bool):
    print(f"[OTP] ❌ Email send error ({provider}) → {email}: {exc}")
    if not is_prod:
        traceback.print_exc()


# ─── Email content builder ────────────────────────────────────────────────────

def _build_email_content(otp: str, purpose: str) -> tuple[str, str, str]:
    """Returns (subject, html_body, plain_text_body)."""
    labels = {
        "signup_email":  "Email Verification",
        "signup_mobile": "Mobile Verification",
        "pin_reset":     "PIN Reset",
        "login":         "Login Verification",
    }
    label   = labels.get(purpose, "Verification")
    expiry  = _otp_expiry()
    subject = f"BizManager — {label} OTP: {otp}"

    plain = (
        f"BizManager — {label}\n\n"
        f"Your one-time password is: {otp}\n\n"
        f"This OTP is valid for {expiry} minutes. Do not share it with anyone.\n\n"
        f"If you did not request this, please ignore this email.\n"
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="padding:30px 10px;">
    <tr><td align="center">
      <table width="480" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:16px;overflow:hidden;
                    box-shadow:0 4px 24px rgba(0,0,0,0.10);">

        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
                        padding:32px 40px;text-align:center;">
          <div style="font-size:36px;line-height:1;">🏪</div>
          <h1 style="color:#fff;margin:10px 0 4px;font-size:22px;font-weight:700;">BizManager</h1>
          <p  style="color:rgba(255,255,255,0.80);margin:0;font-size:14px;">{label}</p>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:36px 40px;">
          <p style="color:#374151;font-size:15px;margin:0 0 20px;">
            Your one-time password is:
          </p>
          <div style="background:#f3f4f6;border-radius:12px;padding:22px 16px;
                      text-align:center;margin:0 0 24px;
                      letter-spacing:14px;font-size:38px;font-weight:700;
                      color:#4f46e5;font-family:'Courier New',monospace;">
            {otp}
          </div>
          <p style="color:#6b7280;font-size:13px;line-height:1.6;margin:0 0 12px;">
            This OTP is valid for <strong>{expiry} minutes</strong>.
            Never share it with anyone — BizManager staff will never ask for your OTP.
          </p>
          <p style="color:#9ca3af;font-size:12px;margin:0;">
            Didn't request this? You can safely ignore this email.
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="background:#f9fafb;padding:18px 40px;text-align:center;
                        border-top:1px solid #e5e7eb;">
          <p style="color:#9ca3af;font-size:11px;margin:0;">
            © {datetime.utcnow().year} BizManager · Automated message, do not reply
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return subject, html, plain


def _build_sms_content(otp: str, purpose: str) -> str:
    labels = {
        "signup_email":  "email verification",
        "signup_mobile": "mobile verification",
        "pin_reset":     "PIN reset",
        "login":         "login",
    }
    label = labels.get(purpose, "verification")
    return f"BizManager: OTP for {label} is {otp}. Valid {_otp_expiry()} mins. Do NOT share. -BizManager"


# ─── SMTP ─────────────────────────────────────────────────────────────────────

def _send_via_smtp(to_email: str, subject: str, html_body: str, plain_body: str = "") -> bool:
    host     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port     = int(os.environ.get("SMTP_PORT", 587))
    user     = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    from_    = os.environ.get("SMTP_FROM", "") or f"BizManager <{user}>"

    if not user or not password:
        raise ValueError("SMTP_USER and SMTP_PASS must be set in environment.")

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_
    msg["To"]      = to_email
    msg["X-Mailer"] = "BizManager OTP Mailer"

    # Attach plain text first (fallback), then HTML (preferred)
    if plain_body:
        msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(user, password)
        server.send_message(msg)
    return True


def _send_via_sendgrid(to_email: str, subject: str, html_body: str) -> bool:
    import urllib.request, json
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    if not api_key:
        raise ValueError("SENDGRID_API_KEY not set.")
    from_ = os.environ.get("SMTP_FROM", "noreply@bizmanager.app")

    payload = json.dumps({
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}]
    }).encode()
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status in (200, 202)


def _send_via_ses(to_email: str, subject: str, html_body: str) -> bool:
    import boto3
    from_   = os.environ.get("SMTP_FROM", "noreply@bizmanager.app")
    region  = os.environ.get("AWS_REGION", "ap-south-1")
    client  = boto3.client("ses", region_name=region)
    client.send_email(
        Source=from_,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject, "Charset": "utf-8"},
            "Body":    {"Html": {"Data": html_body, "Charset": "utf-8"}}
        }
    )
    return True


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
