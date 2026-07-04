"""
notification/exceptions.py — Custom exceptions for the notification package.
"""


class NotificationError(Exception):
    """Base class for all notification package errors."""


class ProviderNotConfiguredError(NotificationError):
    """Raised when a provider is selected but its required credentials
    (API key, SMTP user/pass, etc.) are missing from the environment."""


class ProviderNotFoundError(NotificationError):
    """Raised when EMAIL_PROVIDER names a provider that doesn't exist."""


class TemplateRenderError(NotificationError):
    """Raised when an email template fails to render (missing file,
    bad Jinja syntax, missing required context variable, etc.)."""


class SendError(NotificationError):
    """Raised when a provider's underlying API/SMTP call fails."""
