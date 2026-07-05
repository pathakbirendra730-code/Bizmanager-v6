"""
notification/providers/smtp.py — Generic SMTP provider.

Reads its configuration from the platform_settings engine first
(App Admin -> Settings -> SMTP), falling back to environment variables
if no admin has saved a value yet:

  SMTP_HOST      (default: smtp.gmail.com)
  SMTP_PORT      (default: 587)
  SMTP_USER      (required)
  SMTP_PASS      (required -- for Gmail this must be an App Password,
                  not your normal account password)
  SMTP_FROM      (default: "BizManager <SMTP_USER>")
  SMTP_USE_TLS   (default: true)
  SMTP_USE_SSL   (default: false -- usually leave this off; most
                  providers, including Gmail, use TLS on port 587)
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

from . import EmailProvider
from ..exceptions import SendError
from ..utils import brand_name, default_from_address


def _setting(key: str, env_key: str, env_default: str = "") -> str:
    try:
        from utils.platform_settings import get_setting
        val = get_setting(key).strip()
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(env_key, env_default).strip()


class SMTPProvider(EmailProvider):
    name = "smtp"

    def _host(self):     return _setting("smtp_host", "SMTP_HOST", "smtp.gmail.com")
    def _port(self):     return int(_setting("smtp_port", "SMTP_PORT", "587") or 587)
    def _user(self):     return _setting("smtp_username", "SMTP_USER")
    def _password(self): return _setting("smtp_password", "SMTP_PASS")

    def _use_tls(self):
        return _setting("smtp_use_tls", "SMTP_USE_TLS", "true").lower() == "true"

    def _use_ssl(self):
        return _setting("smtp_use_ssl", "SMTP_USE_SSL", "false").lower() == "true"

    def _from_addr(self):
        explicit = os.environ.get("SMTP_FROM", "").strip()
        if explicit:
            return explicit
        user = self._user()
        return f"{brand_name()} <{user or default_from_address()}>"

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
            if self._use_ssl():
                with smtplib.SMTP_SSL(self._host(), self._port(), timeout=15) as server:
                    server.login(self._user(), self._password())
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self._host(), self._port(), timeout=15) as server:
                    server.ehlo()
                    if self._use_tls():
                        server.starttls()
                        server.ehlo()
                    server.login(self._user(), self._password())
                    server.send_message(msg)
            return True
        except Exception as e:
            raise SendError(f"SMTP send failed: {e}") from e
