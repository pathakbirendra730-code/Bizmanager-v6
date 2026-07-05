"""
notification/providers/sms/fast2sms.py — Fast2SMS provider.

Reads from platform_settings first (App Admin -> Settings -> Fast2SMS),
falling back to the FAST2SMS_API_KEY env var.
"""

import os
import json
import urllib.request

from .. import SMSProvider
from ...exceptions import SendError


def _setting(key: str, env_key: str) -> str:
    try:
        from utils.platform_settings import get_setting
        val = get_setting(key).strip()
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(env_key, "").strip()


class Fast2SMSProvider(SMSProvider):
    name = "fast2sms"

    def _api_key(self):
        return _setting("fast2sms_api_key", "FAST2SMS_API_KEY")

    def is_configured(self) -> bool:
        return bool(self._api_key())

    def send(self, to_mobile: str, message: str) -> bool:
        self.require_configured()
        number = to_mobile.lstrip("+").lstrip("91")[-10:]  # 10-digit only

        payload = json.dumps({
            "route":    "q",
            "message":  message,
            "language": "english",
            "flash":    0,
            "numbers":  number,
        }).encode()
        req = urllib.request.Request(
            "https://www.fast2sms.com/dev/bulkV2",
            data=payload,
            headers={
                "authorization": self._api_key(),
                "Content-Type":  "application/json",
                "Cache-Control": "no-cache",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                return bool(data.get("return"))
        except Exception as e:
            raise SendError(f"Fast2SMS send failed: {e}") from e
