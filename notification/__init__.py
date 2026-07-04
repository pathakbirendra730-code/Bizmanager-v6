"""
notification/ — Outgoing email for BizManager.

    from notification.email_service import send_otp_email, send_welcome_email

Structure:
    manager.py           — picks a provider, renders templates, handles
                            dev/prod send behavior (the only "brain" here)
    email_service.py     — the public API: one function per kind of email
    exceptions.py         — NotificationError and subclasses
    utils.py              — template rendering + shared config helpers
    providers/            — one file per provider (smtp, gmail, brevo,
                            sendgrid, ses), all implementing the same
                            EmailProvider interface
    templates/            — the actual HTML email bodies

Adding a new provider: create providers/newthing.py implementing
EmailProvider, add one line to providers/get_provider(). Nothing else
in the app needs to change.

Adding a new kind of email: add a template under templates/, add one
function to email_service.py that calls manager.send(...). Done.
"""

from .email_service import (
    send_otp_email,
    send_welcome_email,
    send_password_reset_email,
    send_invoice_email,
)

__all__ = [
    "send_otp_email",
    "send_welcome_email",
    "send_password_reset_email",
    "send_invoice_email",
]
