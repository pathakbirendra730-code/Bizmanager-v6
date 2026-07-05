"""
notification/providers/__init__.py — Provider interfaces + factories.

Two sibling interfaces live here, one per channel:

  EmailProvider:  is_configured() -> bool
                  send(to, subject, html, text="") -> bool
  SMSProvider:    is_configured() -> bool
                  send(to, message) -> bool

Adding a new provider (either channel) is one new file + one line in
the matching factory (get_provider / get_sms_provider) below —
nothing else in the app needs to change.
"""

from abc import ABC, abstractmethod

from ..exceptions import ProviderNotConfiguredError, ProviderNotFoundError


class EmailProvider(ABC):
    """Base class every email provider must implement."""

    name = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if this provider has everything it needs
        (API key, SMTP credentials, etc.) to actually send mail."""

    @abstractmethod
    def send(self, to_email: str, subject: str, html_body: str,
              plain_body: str = "", attachments: list | None = None) -> bool:
        """Send one email. Return True on success, raise SendError
        (or let the underlying exception propagate) on failure.

        attachments: optional list of dicts, each
            {"filename": str, "content": bytes, "mimetype": str}
        Not every provider implementation supports this yet — see each
        provider's docstring. Passing attachments to one that doesn't
        should not silently drop them; it should raise or log clearly."""

    def require_configured(self):
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                f"Email provider '{self.name}' is selected but not "
                f"fully configured — check its required environment variables."
            )


def get_provider(name: str) -> EmailProvider:
    """Factory: look up an EMAIL provider instance by name (case-insensitive)."""
    # Local imports avoid importing every provider's dependencies
    # (boto3, etc.) unless that specific provider is actually requested.
    name = (name or "smtp").lower().strip()

    if name == "smtp":
        from .smtp import SMTPProvider
        return SMTPProvider()
    if name == "gmail":
        from .gmail import GmailProvider
        return GmailProvider()
    if name == "brevo":
        from .brevo import BrevoProvider
        return BrevoProvider()
    if name == "sendgrid":
        from .sendgrid import SendGridProvider
        return SendGridProvider()
    if name == "ses":
        from .ses import SESProvider
        return SESProvider()

    raise ProviderNotFoundError(
        f"Unknown EMAIL_PROVIDER '{name}'. "
        f"Valid options: smtp, gmail, brevo, sendgrid, ses."
    )


class SMSProvider(ABC):
    """Base class every SMS provider must implement — the SMS sibling
    of EmailProvider above. Same tiny contract, same reasoning: adding
    a new SMS provider is one new file + one line in get_sms_provider()."""

    name = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if this provider has everything it needs
        (API key, account SID, etc.) to actually send an SMS."""

    @abstractmethod
    def send(self, to_mobile: str, message: str) -> bool:
        """Send one SMS. Return True on success, raise SendError (or
        let the underlying exception propagate) on failure."""

    def require_configured(self):
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                f"SMS provider '{self.name}' is selected but not fully "
                f"configured — check its required environment variables "
                f"or App Admin → Settings."
            )


def get_sms_provider(name: str) -> SMSProvider:
    """Factory: look up an SMS provider instance by name (case-insensitive)."""
    name = (name or "fast2sms").lower().strip()

    if name == "fast2sms":
        from .sms.fast2sms import Fast2SMSProvider
        return Fast2SMSProvider()
    if name == "twilio":
        from .sms.twilio import TwilioSMSProvider
        return TwilioSMSProvider()
    if name == "msg91":
        from .sms.msg91 import MSG91Provider
        return MSG91Provider()
    if name == "brevo":
        from .sms.brevo import BrevoSMSProvider
        return BrevoSMSProvider()

    raise ProviderNotFoundError(
        f"Unknown SMS_PROVIDER '{name}'. "
        f"Valid options: fast2sms, twilio, msg91, brevo."
    )
