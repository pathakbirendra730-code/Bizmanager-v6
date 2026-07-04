"""
notification/manager.py — Central dispatcher for outgoing email.

This is the ONLY place that decides *which* provider handles a send.
Everything else (email_service.py, and any future domain function)
calls manager.send(...) and doesn't know or care whether that ends up
going through Gmail, SMTP, Brevo, SendGrid, or SES.

Behaviour matches the convention already established in this app
(utils/otp_service.py) so switching over doesn't change what anyone
sees:
  • development → always prints to console; ALSO sends a real email if
    a provider is configured, but a send failure doesn't fail the
    calling flow (signup/login can continue — the OTP is visible in
    the console either way).
  • production  → never prints secrets to console; a send failure DOES
    propagate as a failed result, since there's no console fallback.

EMAIL_PROVIDER env var selects the provider: smtp | gmail | brevo |
sendgrid | ses (default: smtp). Read at call time, not cached, so it
can be changed without restarting in a hot-reload dev setup.
"""

import os
import traceback

from .providers import get_provider
from .exceptions import NotificationError
from .utils import is_production, render_template


class NotificationManager:

    def _provider_name(self) -> str:
        try:
            from utils.platform_settings import get_setting
            return get_setting("email_provider").lower().strip()
        except Exception:
            # Settings table not reachable (e.g. very first boot before
            # init_saas_db has run) — fall back straight to env var.
            return os.environ.get("EMAIL_PROVIDER", "smtp").lower().strip()

    def send(self, to_email: str, subject: str, template_name: str,
              context: dict | None = None, plain_body: str = "",
              fail_soft_in_dev: bool = True) -> bool:
        """
        Render `template_name` from notification/templates/ with
        `context`, and send it to `to_email` via the configured provider.

        Returns True/False for success. In development, a send failure
        returns True anyway (fail_soft_in_dev=True) unless the caller
        opts out — matching the historical OTP behaviour where the
        console-printed code is the real fallback.
        """
        context = context or {}
        html_body = render_template(template_name, **context)
        provider_name = self._provider_name()
        prod = is_production()

        try:
            provider = get_provider(provider_name)
        except NotificationError as e:
            print(f"[notification] ❌ {e}")
            return False if prod else fail_soft_in_dev

        if not provider.is_configured():
            if not prod:
                print(f"[notification] ℹ️  Provider '{provider_name}' not "
                      f"configured — skipping real send in development.")
                return fail_soft_in_dev
            print(f"[notification] ❌ Provider '{provider_name}' not configured "
                  f"in production — cannot send to {to_email}.")
            return False

        try:
            ok = provider.send(to_email, subject, html_body, plain_body)
            if ok:
                print(f"[notification] ✅ Sent via {provider_name} → {to_email}")
            return ok if prod else True
        except NotificationError as e:
            print(f"[notification] ❌ Send error ({provider_name}) → {to_email}: {e}")
            if not prod:
                traceback.print_exc()
            return False if prod else fail_soft_in_dev
        except Exception as e:
            # Unexpected (non-NotificationError) failure — still don't let
            # it crash the calling request; log and report failure.
            print(f"[notification] ❌ Unexpected send error ({provider_name}) "
                  f"→ {to_email}: {e}")
            if not prod:
                traceback.print_exc()
            return False if prod else fail_soft_in_dev


# Module-level singleton — this is what email_service.py (and anything
# else) should import and use, rather than instantiating its own.
manager = NotificationManager()
