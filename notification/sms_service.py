"""
notification/sms_service.py — Domain-level SMS functions.

This is the public API the rest of the app should import from — e.g.
    from notification.sms_service import send_otp_sms

Mirrors email_service.py's structure for the SMS channel: nothing
outside this package should reach into manager.py or providers/sms/
directly.
"""

from .manager import manager


_OTP_SMS_LABELS = {
    "signup_email":  "email verification",
    "signup_mobile": "mobile verification",
    "pin_reset":      "PIN reset",
    "login":          "login",
}


def _otp_expiry_minutes() -> int:
    try:
        from utils.platform_settings import get_int_setting
        return get_int_setting("otp_expiry_minutes")
    except Exception:
        import os
        return int(os.environ.get("OTP_EXPIRY_MINUTES", 10))


def send_otp_sms(mobile: str, otp: str, purpose: str) -> bool:
    """
    Send a one-time-password SMS for the given purpose
    (signup_email | signup_mobile | pin_reset | login).

    Message wording matches what otp_service.py sent before this
    migration, so switching over changes nothing a user sees.
    """
    label = _OTP_SMS_LABELS.get(purpose, "verification")
    message = (f"BizManager: OTP for {label} is {otp}. "
              f"Valid {_otp_expiry_minutes()} mins. Do NOT share. -BizManager")

    return manager.send_sms(mobile, message, purpose=f"otp_{purpose}")
