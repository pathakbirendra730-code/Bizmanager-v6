"""
notification/utils.py — Shared helpers: template rendering + env config.

Templates live in notification/templates/ and are rendered with a
standalone Jinja2 environment (not Flask's), so this package can be
used from anywhere — including scripts and background jobs — without
needing an active Flask app/request context.
"""

import os
from datetime import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .exceptions import TemplateRenderError

_TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)
_env.globals["current_year"] = lambda: datetime.utcnow().year


def render_template(name: str, **context) -> str:
    """Render an HTML email template from notification/templates/."""
    try:
        template = _env.get_template(name)
        return template.render(**context)
    except Exception as e:
        raise TemplateRenderError(f"Failed to render '{name}': {e}") from e


# ── Environment config helpers ─────────────────────────────────────────────
# Read live on every call (never cached at import time) so changes to env
# vars — or to os.environ in tests — take effect immediately, matching the
# convention already used elsewhere in this app (utils/otp_service.py).

def app_env() -> str:
    return os.environ.get("APP_ENV", "development").lower()


def is_production() -> bool:
    return app_env() == "production"


def default_from_address() -> str:
    """The address emails appear to come from, if a provider doesn't
    have a more specific one configured."""
    try:
        from utils.platform_settings import get_setting
        db_val = get_setting("mail_from").strip()
        if db_val:
            return db_val
    except Exception:
        pass
    return (os.environ.get("MAIL_FROM")
            or os.environ.get("SMTP_FROM")
            or os.environ.get("SMTP_USER")
            or "noreply@bizmanager.app")


def brand_name() -> str:
    try:
        from utils.platform_settings import get_setting
        db_val = get_setting("mail_from_name").strip()
        if db_val:
            return db_val
    except Exception:
        pass
    return os.environ.get("MAIL_BRAND_NAME", "BizManager")
