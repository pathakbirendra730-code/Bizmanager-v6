"""
notification/providers/ses.py -- AWS SES provider.

Requires the `boto3` package (not installed by default -- add it to
requirements.txt only if you actually use SES, to keep the base install
light for people using SMTP/Gmail/Brevo/SendGrid instead).

Reads its configuration from the platform_settings engine first
(App Admin -> Settings -> AWS SES), falling back to environment
variables if no admin has saved a value yet:

  AWS_REGION              (default: ap-south-1)
  AWS_ACCESS_KEY_ID       (optional -- if blank, boto3 falls back to an
                           IAM role or its own credential chain)
  AWS_SECRET_ACCESS_KEY   (optional, see above)
  MAIL_FROM               (required -- must be a verified SES sender)
"""

import os

from . import EmailProvider
from ..exceptions import SendError, ProviderNotConfiguredError
from ..utils import default_from_address


def _setting(key: str, env_key: str, env_default: str = "") -> str:
    try:
        from utils.platform_settings import get_setting
        val = get_setting(key).strip()
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(env_key, env_default).strip()


class SESProvider(EmailProvider):
    name = "ses"

    def _access_key(self):    return _setting("aws_access_key_id", "AWS_ACCESS_KEY_ID")
    def _secret_key(self):    return _setting("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY")
    def _region(self):        return _setting("aws_region", "AWS_REGION", "ap-south-1")

    def is_configured(self) -> bool:
        # A verified sender address is the one thing SES always needs;
        # access keys may be supplied via an IAM role instead.
        return bool(os.environ.get("MAIL_FROM", "").strip()
                    or default_from_address() != "noreply@bizmanager.app")

    def send(self, to_email: str, subject: str, html_body: str,
              plain_body: str = "", attachments: list | None = None) -> bool:
        self.require_configured()
        if attachments:
            # SES supports attachments via send_raw_email (MIME), not the
            # simpler send_email call used below — not yet wired here
            # since nothing in this app sends attachments via SES today.
            print("[notification] ⚠️  SES provider does not support "
                  "attachments yet — sending without them.")
        try:
            import boto3
        except ImportError as e:
            raise ProviderNotConfiguredError(
                "boto3 is not installed. Run: pip install boto3"
            ) from e

        from_addr = os.environ.get("MAIL_FROM", default_from_address())
        access_key, secret_key = self._access_key(), self._secret_key()

        body = {"Html": {"Data": html_body, "Charset": "utf-8"}}
        if plain_body:
            body["Text"] = {"Data": plain_body, "Charset": "utf-8"}

        try:
            client_kwargs = {"region_name": self._region()}
            if access_key and secret_key:
                client_kwargs["aws_access_key_id"] = access_key
                client_kwargs["aws_secret_access_key"] = secret_key
            client = boto3.client("ses", **client_kwargs)
            client.send_email(
                Source=from_addr,
                Destination={"ToAddresses": [to_email]},
                Message={
                    "Subject": {"Data": subject, "Charset": "utf-8"},
                    "Body": body,
                },
            )
            return True
        except Exception as e:
            raise SendError(f"SES send failed: {e}") from e
