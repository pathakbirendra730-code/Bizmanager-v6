"""
notification/providers/sms/ — One file per SMS provider.

Each implements notification.providers.SMSProvider:
    is_configured() -> bool
    send(to_mobile, message) -> bool

Registered in notification/providers/__init__.py's get_sms_provider().
"""
