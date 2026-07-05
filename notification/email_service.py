"""
notification/email_service.py — Domain-level email functions.

This is the public API the rest of the app should import from — e.g.
    from notification.email_service import send_otp_email

Nothing outside this package should reach into manager.py, providers/,
or utils.py directly. That keeps provider/template details fully
swappable without touching call sites elsewhere in the app.
"""

import os
from datetime import datetime

from .manager import manager
from .utils import brand_name


OTP_LABELS = {
    "signup_email":  "Email Verification",
    "signup_mobile": "Mobile Verification",
    "pin_reset":     "PIN Reset",
    "login":         "Login Verification",
}


def _otp_expiry_minutes() -> int:
    try:
        from utils.platform_settings import get_int_setting
        return get_int_setting("otp_expiry_minutes")
    except Exception:
        return int(os.environ.get("OTP_EXPIRY_MINUTES", 10))


def send_otp_email(email: str, otp: str, purpose: str) -> bool:
    """
    Send a one-time-password email for the given purpose
    (signup_email | signup_mobile | pin_reset | login).
    """
    label  = OTP_LABELS.get(purpose, "Verification")
    expiry = _otp_expiry_minutes()

    subject = f"{brand_name()} — {label} OTP: {otp}"
    plain = (
        f"{brand_name()} — {label}\n\n"
        f"Your one-time password is: {otp}\n\n"
        f"This OTP is valid for {expiry} minutes. Do not share it with anyone.\n\n"
        f"If you did not request this, please ignore this email.\n"
    )

    return manager.send(
        to_email=email,
        subject=subject,
        template_name="otp.html",
        context={"otp": otp, "label": label, "expiry": expiry, "brand": brand_name()},
        plain_body=plain,
        purpose=f"otp_{purpose}",
    )


def send_welcome_email(email: str, full_name: str, business_name: str = "") -> bool:
    """Send a welcome email after signup is fully complete (business created)."""
    subject = f"Welcome to {brand_name()}, {full_name.split()[0] if full_name else ''}!"
    plain = (
        f"Welcome to {brand_name()}!\n\n"
        f"Hi {full_name},\n\n"
        + (f"Your business \"{business_name}\" is now set up and ready to go.\n\n"
           if business_name else "Your account is now set up and ready to go.\n\n")
        + "Log in any time to start billing, tracking stock, and managing your accounts.\n"
    )

    return manager.send(
        to_email=email,
        subject=subject,
        template_name="welcome.html",
        context={"full_name": full_name, "business_name": business_name, "brand": brand_name()},
        plain_body=plain,
        # A missed welcome email is not worth blocking or retrying signup over.
        fail_soft_in_dev=True,
        purpose="welcome",
    )


def send_password_reset_email(email: str, otp: str, expiry_minutes: int | None = None) -> bool:
    """
    Send a PIN/password reset email. Currently OTP-based (reuses the same
    verification flow as login), kept as a separate template/function so
    the wording and design can diverge from the generic OTP email later
    (e.g. if a reset link is added instead of an OTP).
    """
    expiry = expiry_minutes or _otp_expiry_minutes()
    subject = f"{brand_name()} — Reset your PIN"
    plain = (
        f"{brand_name()} — PIN Reset\n\n"
        f"Your PIN reset code is: {otp}\n\n"
        f"This code is valid for {expiry} minutes. If you didn't request "
        f"this, you can safely ignore this email — your PIN will not change.\n"
    )

    return manager.send(
        to_email=email,
        subject=subject,
        template_name="reset_password.html",
        context={"otp": otp, "expiry": expiry, "brand": brand_name()},
        plain_body=plain,
        purpose="pin_reset",
    )


def send_notice_email(email: str, title: str, body_html: str,
                      kind: str = "alert", subtitle: str = "",
                      cta_url: str = "", cta_label: str = "",
                      attachments: list | None = None) -> bool:
    """
    Generic notification email covering Alerts, Reports, and Marketing —
    one parameterized template/function instead of three near-identical
    ones, since all three are "a title, a body, maybe a button" with
    just a different accent color and icon (kind: alert | report | marketing).

    SECURITY: body_html is rendered with Jinja's |safe filter (raw HTML,
    not escaped) — callers MUST NOT pass unsanitized user input here.
    This is meant for app-generated content (e.g. "low stock: 3 items"
    built from your own data), not for echoing back anything a customer
    typed. If that's ever needed, build a plain-text-only path instead.
    """
    subject = title
    plain = f"{title}\n\n{subtitle}\n" if subtitle else title

    return manager.send(
        to_email=email,
        subject=subject,
        template_name="notice.html",
        context={
            "title": title, "subtitle": subtitle, "body_html": body_html,
            "kind": kind, "cta_url": cta_url, "cta_label": cta_label,
            "brand": brand_name(),
        },
        plain_body=plain,
        purpose=kind,
        attachments=attachments,
    )


def send_invoice_email(email: str, invoice: dict, business: dict,
                       attachments: list | None = None) -> bool:
    """
    Email an invoice to a customer.

    `invoice` is expected to have at minimum: invoice_number, total,
    due_amount, status, created_at, and optionally a pdf_url.
    `business` is expected to have at minimum: name, and optionally gstin.
    """
    subject = f"Invoice {invoice.get('invoice_number', '')} from {business.get('name', brand_name())}"
    plain = (
        f"Invoice {invoice.get('invoice_number', '')}\n"
        f"From: {business.get('name', '')}\n"
        f"Total: {invoice.get('total', 0)}\n"
        f"Status: {invoice.get('status', '')}\n"
    )

    return manager.send(
        to_email=email,
        subject=subject,
        template_name="invoice.html",
        context={"invoice": invoice, "business": business, "brand": brand_name()},
        plain_body=plain,
        purpose="invoice",
        attachments=attachments,
        # A customer not receiving their invoice copy by email is worth
        # surfacing as a real failure even outside production.
        fail_soft_in_dev=False,
    )
