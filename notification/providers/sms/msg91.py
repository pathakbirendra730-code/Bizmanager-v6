"""
notification/providers/sms/msg91.py — MSG91 provider.

Reads from platform_settings first (App Admin -> Settings -> MSG91),
falling back to MSG91_AUTH_KEY / MSG91_TEMPLATE_ID env vars.

Note: MSG91's OTP API expects the OTP value itself (as VAR1), not an
arbitrary pre-built message — this provider extracts digits from the
message text as a pragmatic bridge so it fits the generic
SMSProvider.send(to, message) contract used by every other provider.
"""

import os
import re
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


class MSG91Provider(SMSProvider):
    name = "msg91"

    def _auth_key(self):     return _setting("msg91_auth_key", "MSG91_AUTH_KEY")
    def _template_id(self):  return _setting("msg91_template_id", "MSG91_TEMPLATE_ID")

    def is_configured(self) -> bool:
        return bool(self._auth_key() and self._template_id())

    def send(self, to_mobile: str, message: str) -> bool:
        self.require_configured()

        digits_match = re.search(r"\d{4,8}", message)
        otp_value = digits_match.group(0) if digits_match else message

        payload = json.dumps({
            "template_id": self._template_id(),
            "short_url":   "0",
            "mobiles":     to_mobile.lstrip("+"),
            "VAR1":        otp_value,
        }).encode()
        req = urllib.request.Request(
            "https://api.msg91.com/api/v5/otp",
            data=payload,
            headers={
                "authkey":      self._auth_key(),
                "Content-Type": "application/json",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                return data.get("type") == "success"
        except Exception as e:
            raise SendError(f"MSG91 send failed: {e}") from e
