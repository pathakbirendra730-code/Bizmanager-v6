"""
notification/providers/sms/brevo.py — Brevo transactional SMS provider.

Same account/API key as Brevo email — SMS_PROVIDER=brevo and
EMAIL_PROVIDER=brevo share one BREVO_API_KEY (and one Settings section).
"""

import os
import json
import urllib.request

from .. import SMSProvider
from ...exceptions import SendError


def _setting(key: str, env_key: str, default: str = "") -> str:
    try:
        from utils.platform_settings import get_setting
        val = get_setting(key).strip()
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(env_key, default).strip()


class BrevoSMSProvider(SMSProvider):
    name = "brevo"

    def _api_key(self):
        return _setting("brevo_api_key", "BREVO_API_KEY")

    def _sender(self):
        return _setting("brevo_sms_sender", "BREVO_SMS_SENDER", "BizMgr")[:11]

    def is_configured(self) -> bool:
        return bool(self._api_key())

    def send(self, to_mobile: str, message: str) -> bool:
        self.require_configured()
        number = to_mobile if to_mobile.startswith("+") else f"+91{to_mobile.lstrip('0')}"

        payload = json.dumps({
            "sender":    self._sender(),
            "recipient": number,
            "content":   message,
            "type":      "transactional",
        }).encode()
        req = urllib.request.Request(
            "https://api.brevo.com/v3/transactionalSMS/sms",
            data=payload,
            headers={
                "api-key":      self._api_key(),
                "Content-Type": "application/json",
                "Accept":       "application/json",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                return "reference" in data or resp.status in (200, 201)
        except Exception as e:
            raise SendError(f"Brevo SMS send failed: {e}") from e
