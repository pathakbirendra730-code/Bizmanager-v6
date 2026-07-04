"""
notification/providers/brevo.py — Brevo (formerly Sendinblue) provider.

Uses Brevo's transactional email HTTP API — no SMTP credentials needed,
just an API key from the Brevo dashboard.

Env vars:
  BREVO_API_KEY   (required)
  MAIL_FROM       (default: noreply@bizmanager.app)
  MAIL_FROM_NAME  (default: brand_name())
"""

import os
import json
import urllib.request
import urllib.error

from . import EmailProvider
from ..exceptions import SendError
from ..utils import brand_name, default_from_address

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


class BrevoProvider(EmailProvider):
    name = "brevo"

    def _api_key(self):
        try:
            from utils.platform_settings import get_setting
            return get_setting("brevo_api_key").strip()
        except Exception:
            # Settings table not reachable (e.g. very first boot) — fall
            # back straight to env var.
            return os.environ.get("BREVO_API_KEY", "").strip()

    def is_configured(self) -> bool:
        return bool(self._api_key())

    def send(self, to_email: str, subject: str, html_body: str,
              plain_body: str = "") -> bool:
        self.require_configured()

        payload = {
            "sender": {
                "email": default_from_address(),
                "name": brand_name(),
            },
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": html_body,
        }
        if plain_body:
            payload["textContent"] = plain_body

        req = urllib.request.Request(
            BREVO_API_URL,
            data=json.dumps(payload).encode(),
            headers={
                "api-key": self._api_key(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise SendError(f"Brevo send failed ({e.code}): {body}") from e
        except Exception as e:
            raise SendError(f"Brevo send failed: {e}") from e
