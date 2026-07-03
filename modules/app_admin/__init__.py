"""modules/app_admin/__init__.py"""
from .routes import app_admin_bp, app_admin_required, super_admin_required

# Import sub-modules so their routes register on the blueprint
from . import dashboard       # noqa: F401
from . import manage_admins   # noqa: F401
