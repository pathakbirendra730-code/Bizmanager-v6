"""
notification/providers/gmail.py — Gmail-specific convenience provider.

This is the SMTP provider pre-pointed at Gmail's servers, with its own
env vars so a "Gmail account for sending mail" can be configured
separately from a generic SMTP_* setup if both happen to be present.

Setup (Google account, one-time):
  1. Turn on 2-Step Verification on the Gmail account.
  2. Create an "App Password" (myaccount.google.com/apppasswords).
  3. Set:
       GMAIL_USER          = your@gmail.com
       GMAIL_APP_PASSWORD  = the 16-character app password (no spaces)

Falls back to SMTP_USER / SMTP_PASS if the Gmail-specific vars aren't
set, so existing deployments using the generic SMTP_* vars with Gmail's
host keep working without any changes.
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

from . import EmailProvider
from ..exceptions import SendError
from ..utils import brand_name

GMAIL_HOST = "smtp.gmail.com"
GMAIL_PORT = 587


class GmailProvider(EmailProvider):
    name = "gmail"

    def _user(self):
        return (os.environ.get("GMAIL_USER", "").strip()
                or os.environ.get("SMTP_USER", "").strip())

    def _password(self):
        return (os.environ.get("GMAIL_APP_PASSWORD", "").strip()
                or os.environ.get("SMTP_PASS", "").strip())

    def _from_addr(self):
        explicit = os.environ.get("SMTP_FROM", "").strip()
        if explicit:
            return explicit
        return f"{brand_name()} <{self._user()}>"

    def is_configured(self) -> bool:
        return bool(self._user() and self._password())

    def send(self, to_email: str, subject: str, html_body: str,
              plain_body: str = "", attachments: list | None = None) -> bool:
        self.require_configured()

        alt = MIMEMultipart("alternative")
        if plain_body:
            alt.attach(MIMEText(plain_body, "plain", "utf-8"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))

        if attachments:
            msg = MIMEMultipart("mixed")
            msg.attach(alt)
            for att in attachments:
                part = MIMEApplication(att["content"], Name=att["filename"])
                part["Content-Disposition"] = f'attachment; filename="{att["filename"]}"'
                msg.attach(part)
        else:
            msg = alt

        msg["Subject"]  = subject
        msg["From"]     = self._from_addr()
        msg["To"]       = to_email
        msg["X-Mailer"] = f"{brand_name()} Mailer"

        try:
            with smtplib.SMTP(GMAIL_HOST, GMAIL_PORT, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self._user(), self._password())
                server.send_message(msg)
            return True
        except Exception as e:
            raise SendError(f"Gmail send failed: {e}") from e
