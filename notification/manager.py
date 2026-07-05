"""
notification/manager.py -- Central dispatcher for outgoing email AND SMS.

This is the ONLY place that decides *which* provider handles a send,
for both channels. Everything else (email_service.py, sms_service.py,
and any future domain function) calls manager.send(...) or
manager.send_sms(...) and doesn't know or care which provider ends up
handling it.

Two channel-specific methods, not one generic one:
  send()      -- email (unchanged signature from before Phase 2 --
                 existing callers in email_service.py needed zero changes)
  send_sms()  -- SMS (new in Phase 2)

Both funnel through the same shared machinery added in Phase 2:
  * retry with backoff on transient failures
  * automatic failover to a configured fallback provider
  * every attempt logged to notification_log (notification/log.py)
  * dispatched via notification/queue.py's enqueue() -- synchronous
    today, the seam where real async processing plugs in later

Behaviour matches the convention already established in this app:
  - development -> a send failure doesn't fail the calling flow
    (signup/login can continue -- the OTP is visible in console either way)
  - production  -> a send failure DOES propagate as a failed result
"""

import time
import traceback

from .providers import get_provider, get_sms_provider
from .exceptions import NotificationError
from .utils import is_production, render_template
from .log import record_notification
from .queue import enqueue

MAX_ATTEMPTS_PER_PROVIDER = 2   # 1 initial try + 1 retry
RETRY_DELAY_SECONDS = 1.5


class NotificationManager:

    # ── Provider selection (DB setting, falls back to env var) ─────────────

    def _setting(self, key: str, env_key: str, env_default: str) -> str:
        try:
            from utils.platform_settings import get_setting
            val = get_setting(key).lower().strip()
            if val:
                return val
        except Exception:
            pass
        import os
        return os.environ.get(env_key, env_default).lower().strip()

    def _email_provider_name(self) -> str:
        return self._setting("email_provider", "EMAIL_PROVIDER", "smtp")

    def _email_fallback_name(self) -> str:
        return self._setting("fallback_email_provider", "FALLBACK_EMAIL_PROVIDER", "none")

    def _sms_provider_name(self) -> str:
        return self._setting("sms_provider", "SMS_PROVIDER", "fast2sms")

    def _sms_fallback_name(self) -> str:
        return self._setting("fallback_sms_provider", "FALLBACK_SMS_PROVIDER", "none")

    # ── Shared retry + failover engine (channel-agnostic) ───────────────────

    def _send_with_retry(self, provider, do_send, recipient: str) -> tuple[bool, str, int]:
        """Try `do_send(provider)` up to MAX_ATTEMPTS_PER_PROVIDER times.
        Returns (success, error_message, attempts_used)."""
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS_PER_PROVIDER + 1):
            try:
                ok = do_send(provider)
                if ok:
                    return True, "", attempt
                last_error = "provider returned failure"
            except NotificationError as e:
                last_error = str(e)
            except Exception as e:
                last_error = f"unexpected error: {e}"
                if not is_production():
                    traceback.print_exc()

            if attempt < MAX_ATTEMPTS_PER_PROVIDER:
                time.sleep(RETRY_DELAY_SECONDS)
        return False, last_error, MAX_ATTEMPTS_PER_PROVIDER

    def _dispatch(self, channel: str, primary_name: str, fallback_name: str,
                  get_provider_fn, do_send, recipient: str, purpose: str,
                  fail_soft_in_dev: bool) -> bool:
        prod = is_production()
        providers_to_try = [primary_name]
        if fallback_name and fallback_name != "none" and fallback_name != primary_name:
            providers_to_try.append(fallback_name)

        last_error = ""
        for provider_name in providers_to_try:
            try:
                provider = get_provider_fn(provider_name)
            except NotificationError as e:
                last_error = str(e)
                print(f"[notification] ❌ {e}")
                continue

            if not provider.is_configured():
                msg = f"Provider '{provider_name}' not configured"
                last_error = msg
                if not prod:
                    print(f"[notification] ℹ️  {msg} — skipping in development.")
                else:
                    print(f"[notification] ❌ {msg} in production — cannot send to {recipient}.")
                record_notification(channel, provider_name, recipient, purpose,
                                    "not_configured", 0, msg)
                continue

            ok, error, attempts = self._send_with_retry(provider, do_send, recipient)
            record_notification(channel, provider_name, recipient, purpose,
                                "sent" if ok else "failed", attempts,
                                None if ok else error)

            if ok:
                print(f"[notification] ✅ Sent via {provider_name} ({channel}) → "
                      f"{recipient} [{attempts} attempt(s)]")
                return True

            last_error = error
            print(f"[notification] ❌ {provider_name} failed after {attempts} "
                  f"attempt(s) ({channel}) → {recipient}: {error}")
            if provider_name != providers_to_try[-1]:
                print(f"[notification] ↻ Trying fallback provider "
                      f"'{providers_to_try[providers_to_try.index(provider_name)+1]}'...")

        # Every provider we tried (primary + fallback) failed.
        return False if prod else fail_soft_in_dev

    # ── Public API: email ────────────────────────────────────────────────────

    def send(self, to_email: str, subject: str, template_name: str,
              context: dict | None = None, plain_body: str = "",
              fail_soft_in_dev: bool = True, purpose: str = "",
              attachments: list | None = None) -> bool:
        """
        Render `template_name` from notification/templates/ with
        `context`, and send it to `to_email` via the configured email
        provider (with retry + failover), through the queue seam.

        attachments: optional list of {"filename", "content" (bytes),
        "mimetype"} dicts — see each EmailProvider's docstring for
        which ones actually support this today.
        """
        context = context or {}
        html_body = render_template(template_name, **context)

        def do_send(provider):
            return provider.send(to_email, subject, html_body, plain_body, attachments)

        return enqueue(
            self._dispatch,
            channel="email",
            primary_name=self._email_provider_name(),
            fallback_name=self._email_fallback_name(),
            get_provider_fn=get_provider,
            do_send=do_send,
            recipient=to_email,
            purpose=purpose or template_name,
            fail_soft_in_dev=fail_soft_in_dev,
        )

    # ── Public API: SMS ──────────────────────────────────────────────────────

    def send_sms(self, to_mobile: str, message: str,
                 fail_soft_in_dev: bool = True, purpose: str = "") -> bool:
        """
        Send a plain-text SMS to `to_mobile` via the configured SMS
        provider (with retry + failover), through the queue seam.
        """
        def do_send(provider):
            return provider.send(to_mobile, message)

        return enqueue(
            self._dispatch,
            channel="sms",
            primary_name=self._sms_provider_name(),
            fallback_name=self._sms_fallback_name(),
            get_provider_fn=get_sms_provider,
            do_send=do_send,
            recipient=to_mobile,
            purpose=purpose or "sms",
            fail_soft_in_dev=fail_soft_in_dev,
        )


# Module-level singleton -- this is what email_service.py, sms_service.py,
# (and anything else) should import and use, rather than instantiating
# their own.
manager = NotificationManager()
