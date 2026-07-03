"""modules/saas_auth/__init__.py"""
from .routes import saas_auth_bp

# Import team routes so they register on the blueprint
from . import team  # noqa: F401
