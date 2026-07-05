"""
notification/log.py — Audit trail for every notification send attempt.

Every call to NotificationManager.send() / send_sms() writes one row
here, success or failure. This is what Phase 3's OTP audit logging
will read from, and it's also just useful for "why didn't my customer
get their invoice email" support questions.

Recipients are masked before storage — this table is for operational
debugging, not for storing full contact details (those already live in
saas_users / saas_customers).
"""

from models.saas_auth import saas_execute, saas_fetchall, _is_postgres

P = lambda: "%s" if _is_postgres() else "?"


def mask_recipient(value: str, channel: str) -> str:
    """j***@example.com  /  98765*****0"""
    if not value:
        return ""
    if channel == "email" and "@" in value:
        local, _, domain = value.partition("@")
        masked_local = local[0] + "***" if local else "***"
        return f"{masked_local}@{domain}"
    # SMS / mobile — keep first 5 and last 1 digit, mask the middle
    digits = value
    if len(digits) <= 6:
        return "*" * len(digits)
    return f"{digits[:5]}{'*' * (len(digits) - 6)}{digits[-1]}"


def record_notification(channel: str, provider: str, recipient: str,
                        purpose: str, status: str, attempts: int = 1,
                        error: str = None) -> None:
    """Best-effort logging — a logging failure must never break a send,
    so this swallows its own exceptions rather than propagating them."""
    try:
        p = P()
        saas_execute(
            f"""INSERT INTO notification_log
                (channel, provider, recipient_masked, purpose, status, attempts, error)
                VALUES ({p},{p},{p},{p},{p},{p},{p})""",
            (channel, provider, mask_recipient(recipient, channel),
             purpose, status, attempts, error)
        )
    except Exception as e:
        print(f"[notification.log] ⚠️  Failed to write log entry (non-fatal): {e}")


def recent_logs(limit: int = 100) -> list:
    """For an admin-facing notification log view, if/when one is built."""
    return saas_fetchall(
        "SELECT * FROM notification_log ORDER BY created_at DESC LIMIT " + str(int(limit))
    )
