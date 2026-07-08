"""
utils/auth_service.py — The One Authentication Service (Phase 3).

Extracts the credential-verification logic that was duplicated (or
would be, as more login entry points get added) across unified_login.py
and app_admin/routes.py into one place.

Deliberately narrow scope: this class verifies credentials and returns
plain dicts/tuples — it does NOT touch Flask session, redirect, or
flash. That orchestration stays in the route handlers, where it
belongs (a login route's job is deciding what the user sees next; an
auth service's job is "are these credentials valid"). Merging those
two concerns is what makes so many auth systems hard to change safely
later — keeping them separate is the actual architectural point here,
more than the specific class boundary.

This service never touches notification providers directly — OTP
delivery goes through utils.otp_manager.OTPManager, which itself goes
through NotificationManager. Neither this class nor OTPManager needs
to know or care whether an OTP is delivered via Brevo, SMTP, Twilio, or
anything else.
"""

from werkzeug.security import check_password_hash

from models.saas_auth import saas_fetchone, _is_postgres

P = lambda: "%s" if _is_postgres() else "?"


class AuthenticationService:

    def verify_admin_credentials(self, user_id: str, password: str) -> dict | None:
        """Return the app_admins row if user_id+password are valid and
        the account is active, else None. Does not check OTP — that's
        the second factor, handled separately via OTPManager."""
        admin = saas_fetchone(
            f"SELECT * FROM app_admins WHERE user_id={P()} AND is_active=TRUE",
            (user_id,)
        )
        if not admin or not check_password_hash(admin["password_hash"], password):
            return None
        return admin

    def verify_saas_pin(self, mobile_normalized: str, pin: str) -> tuple[dict | None, str]:
        """
        Return (user_row, message). user_row is None if credentials are
        invalid or the account isn't ready to log in yet (unverified /
        no PIN set) — message explains which, so the caller can decide
        where to route the user next (it still needs to, since that's
        orchestration, not a credentials question).
        """
        user = saas_fetchone(
            f"SELECT * FROM saas_users WHERE mobile={P()} AND is_active=TRUE",
            (mobile_normalized,)
        )
        if not user:
            return None, "Mobile number not registered. Please sign up."
        if not user.get("is_verified"):
            return None, "account_unverified"
        if not user.get("pin_hash"):
            return None, "pin_not_set"
        if not check_password_hash(user["pin_hash"], pin):
            return None, "Incorrect PIN. Please try again."
        return user, "OK"


# Module-level singleton, same convention as notification.manager.manager
# and otp_manager.otp_manager.
auth_service = AuthenticationService()
