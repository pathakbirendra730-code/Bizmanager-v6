"""
notification/providers/smtp.py — Generic SMTP provider.

Env vars:
  SMTP_HOST   (default: smtp.gmail.com)
  SMTP_PORT   (default: 587)
  SMTP_USER   (required)
  SMTP_PASS   (required — for Gmail this must be an App Password, not
               your normal account password)
  SMTP_FROM   (default: "BizManager <SMTP_USER>")
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from . import EmailProvider
from ..exceptions import SendError
from ..utils import brand_name


class SMTPProvider(EmailProvider):
    name = "smtp"

    def _host(self):     return os.environ.get("SMTP_HOST", "smtp.gmail.com")
    def _port(self):     return int(os.environ.get("SMTP_PORT", 587))
    def _user(self):     return os.environ.get("SMTP_USER", "").strip()
    def _password(self): return os.environ.get("SMTP_PASS", "").strip()

    def _from_addr(self):
        explicit = os.environ.get("SMTP_FROM", "").strip()
        if explicit:
            return explicit
        return f"{brand_name()} <{self._user()}>"

    def is_configured(self) -> bool:
        return bool(self._user() and self._password())

    def send(self, to_email: str, subject: str, html_body: str,
              plain_body: str = "") -> bool:
        self.require_configured()

        msg = MIMEMultipart("alternative")
        msg["Subject"]   = subject
        msg["From"]      = self._from_addr()
        msg["To"]        = to_email
        msg["X-Mailer"]  = f"{brand_name()} Mailer"

        if plain_body:
            msg.attach(MIMEText(plain_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            with smtplib.SMTP(self._host(), self._port(), timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self._user(), self._password())
                server.send_message(msg)
            return True
        except Exception as e:
            raise SendError(f"SMTP send failed: {e}") from e
