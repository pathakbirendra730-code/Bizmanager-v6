"""
notification/providers/sendgrid.py — SendGrid transactional email provider.

Env vars:
  SENDGRID_API_KEY   (required)
  MAIL_FROM          (default: noreply@bizmanager.app)
"""

import os
import json
import urllib.request
import urllib.error

from . import EmailProvider
from ..exceptions import SendError
from ..utils import default_from_address

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"


class SendGridProvider(EmailProvider):
    name = "sendgrid"

    def _api_key(self):
        try:
            from utils.platform_settings import get_setting
            val = get_setting("sendgrid_api_key").strip()
            if val:
                return val
        except Exception:
            pass
        return os.environ.get("SENDGRID_API_KEY", "").strip()

    def is_configured(self) -> bool:
        return bool(self._api_key())

    def send(self, to_email: str, subject: str, html_body: str,
              plain_body: str = "", attachments: list | None = None) -> bool:
        self.require_configured()
        if attachments:
            # SendGrid supports base64 attachments via payload["attachments"] —
            # not yet wired here since nothing in this app sends attachments
            # via SendGrid today. Same pattern as brevo.py when needed.
            print("[notification] ⚠️  SendGrid provider does not support "
                  "attachments yet — sending without them.")

        content = [{"type": "text/html", "value": html_body}]
        if plain_body:
            content.insert(0, {"type": "text/plain", "value": plain_body})

        payload = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": os.environ.get("MAIL_FROM", default_from_address())},
            "subject": subject,
            "content": content,
        }

        req = urllib.request.Request(
            SENDGRID_API_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status in (200, 202)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise SendError(f"SendGrid send failed ({e.code}): {body}") from e
        except Exception as e:
            raise SendError(f"SendGrid send failed: {e}") from e
