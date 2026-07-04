"""
notification/providers/ses.py — AWS SES provider.

Requires the `boto3` package (not installed by default — add it to
requirements.txt only if you actually use SES, to keep the base install
light for people using SMTP/Gmail/Brevo/SendGrid instead).

Env vars:
  AWS_REGION              (default: ap-south-1)
  AWS_ACCESS_KEY_ID       (required — or use an IAM role, in which case
                           boto3 picks up credentials automatically and
                           these two vars aren't needed)
  AWS_SECRET_ACCESS_KEY   (required, see above)
  MAIL_FROM               (required — must be a verified SES sender)
"""

import os

from . import EmailProvider
from ..exceptions import SendError, ProviderNotConfiguredError
from ..utils import default_from_address


class SESProvider(EmailProvider):
    name = "ses"

    def is_configured(self) -> bool:
        # A verified sender address is the one thing SES always needs;
        # access keys may be supplied via an IAM role instead of env vars.
        return bool(os.environ.get("MAIL_FROM", "").strip()
                    or default_from_address() != "noreply@bizmanager.app")

    def send(self, to_email: str, subject: str, html_body: str,
              plain_body: str = "") -> bool:
        self.require_configured()
        try:
            import boto3
        except ImportError as e:
            raise ProviderNotConfiguredError(
                "boto3 is not installed. Run: pip install boto3"
            ) from e

        region = os.environ.get("AWS_REGION", "ap-south-1")
        from_addr = os.environ.get("MAIL_FROM", default_from_address())

        body = {"Html": {"Data": html_body, "Charset": "utf-8"}}
        if plain_body:
            body["Text"] = {"Data": plain_body, "Charset": "utf-8"}

        try:
            client = boto3.client("ses", region_name=region)
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
