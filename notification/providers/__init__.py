"""
notification/providers/__init__.py — Provider interface + factory.

Every provider implements the same tiny contract:

    is_configured() -> bool          # required credentials present?
    send(to, subject, html, text="") -> bool   # True on success

That's it. This keeps swapping providers (or adding a new one) a matter
of dropping in one new file and one line in PROVIDER_REGISTRY below —
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
              plain_body: str = "") -> bool:
        """Send one email. Return True on success, raise SendError
        (or let the underlying exception propagate) on failure."""

    def require_configured(self):
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                f"Email provider '{self.name}' is selected but not "
                f"fully configured — check its required environment variables."
            )


def get_provider(name: str) -> EmailProvider:
    """Factory: look up a provider instance by name (case-insensitive)."""
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
