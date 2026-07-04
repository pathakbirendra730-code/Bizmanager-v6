"""
utils/platform_settings.py — Admin-editable, DB-backed configuration.

The point of this module: things that would otherwise need an env var
change + redeploy (e.g. "does signup require mobile OTP?", "which email
provider is active?") can instead be flipped from the App Admin
Settings page at runtime, with no code change and no redeploy.

Design:
  • SETTINGS_SCHEMA is the single source of truth for what settings
    exist, their type, choices, and default. The admin Settings page
    renders itself entirely from this list — adding a new setting is
    "add one entry here", not "build a new form field".
  • get_setting() checks the DB first, falls back to the schema's
    default (which is usually itself read from an env var) if the DB
    has no row yet. This means a fresh install with an empty
    platform_settings table behaves exactly as it did before this
    feature existed — nothing breaks by adding this.
  • Secret-type settings (API keys) are stored in the DB like everything
    else, but the raw value is NEVER sent back to the browser once saved
    — the form shows a masked placeholder instead, and only overwrites
    the stored value if the admin actually types a new one. Leaving the
    field blank on save keeps whatever was already stored.
"""

import os
from models.saas_auth import saas_fetchone, saas_execute, _is_postgres

P = lambda: "%s" if _is_postgres() else "?"


def _env_default(key: str, fallback: str) -> str:
    return os.environ.get(key, fallback)


# ── Schema: every admin-configurable setting lives here ─────────────────────
#
# type: "bool"   → rendered as a toggle switch
#       "select" → rendered as a dropdown, needs "options"
#
# "default" is computed lazily (a function) so it reflects the current
# env var if the DB has no override yet, matching pre-existing behavior
# for anyone upgrading without having touched the Settings page.

SETTINGS_SCHEMA = [
    {
        "key": "require_mobile_verification",
        "label": "Require Mobile OTP at Signup",
        "type": "bool",
        "default": lambda: _env_default("REQUIRE_MOBILE_VERIFICATION", "false"),
        "help": ("Off by default — sending real SMS OTPs to Indian numbers "
                 "needs DLT registration (a TRAI requirement for every SMS "
                 "provider). Turn this on once that's done; email "
                 "verification is always required regardless."),
    },
    {
        "key": "email_provider",
        "label": "Email Provider",
        "type": "select",
        "options": ["smtp", "gmail", "brevo", "sendgrid", "ses"],
        "default": lambda: _env_default("EMAIL_PROVIDER", "smtp"),
        "help": ("Which service sends OTP/welcome/invoice emails. Fill in "
                 "that provider's credentials below."),
    },
    {
        "key": "sms_provider",
        "label": "SMS Provider",
        "type": "select",
        "options": ["twilio", "fast2sms", "msg91", "brevo"],
        "default": lambda: _env_default("SMS_PROVIDER", "fast2sms"),
        "help": "Only relevant once mobile OTP verification is turned on above.",
    },
    {
        "key": "brevo_api_key",
        "label": "Brevo API Key",
        "type": "secret",
        "default": lambda: _env_default("BREVO_API_KEY", ""),
        "help": "From Brevo → Settings → SMTP & API → API Keys. Used for both email and SMS.",
        "group": "Brevo",
    },
    {
        "key": "mail_from",
        "label": "From Email Address",
        "type": "text",
        "default": lambda: _env_default("MAIL_FROM", ""),
        "help": "Must be a sender you've verified in Brevo (Settings → Senders & IPs).",
        "group": "Brevo",
    },
    {
        "key": "mail_from_name",
        "label": "From Name",
        "type": "text",
        "default": lambda: _env_default("MAIL_FROM_NAME", "BizManager"),
        "help": "The sender name recipients see, e.g. \"BizManager\".",
        "group": "Brevo",
    },
    {
        "key": "brevo_sms_sender",
        "label": "SMS Sender ID",
        "type": "text",
        "default": lambda: _env_default("BREVO_SMS_SENDER", "BizMgr"),
        "help": ("Max 11 alphanumeric characters. Some countries require this "
                 "to be pre-registered with Brevo before SMS will deliver."),
        "group": "Brevo",
    },
]

_SCHEMA_BY_KEY = {s["key"]: s for s in SETTINGS_SCHEMA}
SECRET_MASK = "••••••••"  # what a saved secret looks like in the UI — never the real value


def is_secret_set(key: str) -> bool:
    """True if a real (non-empty) value exists for a secret-type setting,
    without ever returning that value to the caller."""
    return bool(get_setting(key).strip())


def get_setting(key: str) -> str:
    """
    Return the current value for `key` — from the DB if an admin has
    ever saved it, otherwise the schema's live default (which itself
    usually reflects an env var).
    """
    row = saas_fetchone(
        f"SELECT value FROM platform_settings WHERE key={P()}", (key,)
    )
    if row is not None:
        return row["value"]

    schema = _SCHEMA_BY_KEY.get(key)
    if schema is None:
        return ""
    return schema["default"]()


def get_bool_setting(key: str) -> bool:
    return get_setting(key).strip().lower() == "true"


def set_setting(key: str, value: str, updated_by=None) -> None:
    if key not in _SCHEMA_BY_KEY:
        raise ValueError(f"Unknown platform setting: {key}")

    p = P()
    if _is_postgres():
        saas_execute(
            f"""INSERT INTO platform_settings (key, value, updated_by, updated_at)
                VALUES ({p},{p},{p}, NOW())
                ON CONFLICT (key) DO UPDATE
                SET value={p}, updated_by={p}, updated_at=NOW()""",
            (key, value, updated_by, value, updated_by)
        )
    else:
        saas_execute(
            f"""INSERT INTO platform_settings (key, value, updated_by, updated_at)
                VALUES ({p},{p},{p}, datetime('now'))
                ON CONFLICT (key) DO UPDATE
                SET value=excluded.value, updated_by=excluded.updated_by,
                    updated_at=datetime('now')""",
            (key, value, updated_by)
        )


def all_settings() -> list:
    """Every setting in schema order, with its current effective value —
    what the Settings page renders itself from. Secret-type values are
    masked here (this is the only function templates should ever call)."""
    result = []
    for s in SETTINGS_SCHEMA:
        entry = dict(s)
        if s["type"] == "secret":
            entry["value"] = SECRET_MASK if is_secret_set(s["key"]) else ""
            entry["is_set"] = is_secret_set(s["key"])
        else:
            entry["value"] = get_setting(s["key"])
        result.append(entry)
    return result
