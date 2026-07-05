"""
notification/ — Outgoing email AND SMS for BizManager.

    from notification.email_service import send_otp_email, send_welcome_email
    from notification.sms_service import send_otp_sms

Structure:
    manager.py           — picks a provider (per channel), renders email
                            templates, retries, fails over to a backup
                            provider, logs every attempt, dispatches
                            through the queue seam
    email_service.py     — public API for email: one function per kind
    sms_service.py        — public API for SMS: one function per kind
    log.py                — notification_log audit trail (PII-masked)
    queue.py               — enqueue() seam for future async processing
    exceptions.py          — NotificationError and subclasses
    utils.py                — template rendering + shared config helpers
    providers/               — EmailProvider implementations (smtp, gmail,
                                brevo, sendgrid, ses)
    providers/sms/            — SMSProvider implementations (twilio,
                                fast2sms, msg91, brevo)
    templates/                — the actual HTML email bodies

Adding a new email provider: create providers/newthing.py implementing
EmailProvider, add one line to providers/get_provider(). Same pattern
for SMS via providers/sms/ and get_sms_provider().

Adding a new kind of email: add a template under templates/, add one
function to email_service.py that calls manager.send(...). Adding a
new kind of SMS: add one function to sms_service.py that calls
manager.send_sms(...). Done — both channels get retry, failover, and
logging automatically, for free.
"""

from .email_service import (
    send_otp_email,
    send_welcome_email,
    send_password_reset_email,
    send_invoice_email,
    send_notice_email,
)
from .sms_service import send_otp_sms

__all__ = [
    "send_otp_email",
    "send_welcome_email",
    "send_password_reset_email",
    "send_invoice_email",
    "send_notice_email",
    "send_otp_sms",
]
