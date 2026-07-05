"""
utils/otp_manager.py — The One OTP Manager (Phase 3).

Every authentication feature that needs an OTP — signup email/mobile
verification, login, PIN reset, admin login 2FA, resend — should go
through THIS class rather than calling generate_otp/store_otp/
send_email_otp/send_sms_otp individually. That's what makes "one OTP
Manager" actually true, rather than just a name for scattered functions.

This is a facade, not a rewrite: it wraps utils/otp_service.py's
existing, already-correct generate/store/verify logic (expiry, max
attempts, invalidate-previous-OTP-on-resend) rather than reimplementing
it, and notification/{email_service,sms_service}.py for actual delivery
(which is itself where NotificationManager's retry/failover/logging
live, as of Phase 2). OTPManager adds the two things that were missing
before Phase 3:

  1. Centralized rate limiting on OTP *generation* (previously each
     call site — signup, admin login, resend — had to remember to
     rate-limit itself, or didn't).
  2. Centralized audit logging for every OTP generate/verify event,
     in one place instead of scattered across call sites.

Responsibilities (from the architecture brief):
  ✓ Generate OTP           -- generate_and_send() -> otp_service.generate_otp
  ✓ Store OTP              -- otp_service.store_otp (already invalidates
                               any previous unused OTP for the same
                               identifier+purpose -- see its docstring)
  ✓ Expire OTP             -- otp_service.verify_and_consume_otp (checks
                               expires_at, itself driven by the
                               otp_expiry_minutes platform setting)
  ✓ Maximum attempts       -- otp_service.verify_and_consume_otp (checks
                               attempts >= max_attempts, already enforced
                               at the DB row level)
  ✓ Invalidate previous OTP -- otp_service.store_otp (see above)
  ✓ Resend OTP             -- generate_and_send() again; store_otp's
                               invalidate-previous behavior makes any
                               earlier OTP for the same purpose dead
                               the moment a new one is requested
  ✓ Rate limiting          -- NEW in this class (see above)
  ✓ Audit logging          -- NEW in this class (see above)
"""

from utils.otp_service import generate_otp, store_otp, verify_and_consume_otp
from utils.saas_helpers import check_rate_limit, audit_log


class OTPManager:

    # How many OTP *generation* requests (not verify attempts -- those
    # are already capped by max_attempts in otp_service) a single
    # identifier+purpose can make in a rolling window, before being
    # told to wait. Prevents someone from hammering "resend" to spam a
    # phone/inbox or run up a provider bill.
    GENERATE_MAX_REQUESTS = 5
    GENERATE_WINDOW_SECONDS = 600  # 10 minutes

    def _rate_limit_key(self, identifier: str, purpose: str) -> str:
        return f"otp_generate:{purpose}:{identifier}"

    def generate_and_send(self, identifier: str, purpose: str, channel: str,
                          email: str = None, mobile: str = None) -> tuple[bool, str, str]:
        """
        Generate an OTP, store it (auto-invalidating any earlier unused
        one for this identifier+purpose), send it via the requested
        channel(s), and audit-log the attempt.

        channel: "email" | "sms" | "both"
        identifier: what verify() will later be called with -- usually
                    the mobile number or f"admin:{admin_id}", matching
                    whatever otp_service call sites already use today.
        email / mobile: actual delivery addresses (identifier itself is
                    sometimes an opaque key like "admin:5", not a real
                    address -- these are passed separately so this
                    class doesn't need to guess).

        Returns (success, message, dev_otp) -- message is user-facing-safe.
        dev_otp is the raw OTP value ONLY in development (for on-screen
        display, the same convenience otp_service always offered) and
        is always "" in production -- this class never hands the raw
        value back to a caller in prod, matching the rest of this app's
        dev/prod convention.
        """
        from utils.otp_service import _is_production

        rl_key = self._rate_limit_key(identifier, purpose)
        if not check_rate_limit(rl_key, max_requests=self.GENERATE_MAX_REQUESTS,
                                window_seconds=self.GENERATE_WINDOW_SECONDS):
            audit_log("otp_generate_rate_limited", status="failure",
                     detail=f"identifier={identifier} purpose={purpose}")
            return False, "Too many OTP requests. Please wait a few minutes and try again.", ""

        otp = generate_otp()
        if not store_otp(identifier, otp, purpose):
            audit_log("otp_generate_store_failed", status="failure",
                     detail=f"identifier={identifier} purpose={purpose}")
            return False, "Could not generate OTP. Please try again.", ""

        sent_email = sent_sms = None
        if channel in ("email", "both") and email:
            from notification.email_service import send_otp_email
            sent_email = send_otp_email(email, otp, purpose)

        if channel in ("sms", "both") and mobile:
            from notification.sms_service import send_otp_sms
            sent_sms = send_otp_sms(mobile, otp, purpose)

        ok = bool(sent_email) or bool(sent_sms)
        audit_log("otp_generated", status="success" if ok else "failure",
                 detail=f"identifier={identifier} purpose={purpose} "
                        f"channel={channel} email_sent={sent_email} sms_sent={sent_sms}")

        dev_otp = "" if _is_production() else otp
        if not ok:
            return False, "No delivery channel configured for this OTP.", dev_otp
        return True, "OTP sent.", dev_otp

    def verify(self, identifier: str, otp: str, purpose: str) -> tuple[bool, str]:
        """Verify + consume an OTP. Thin wrapper adding audit logging
        around otp_service's already-correct expiry/max-attempts logic."""
        ok, message = verify_and_consume_otp(identifier, otp, purpose)
        audit_log("otp_verified" if ok else "otp_verify_failed",
                 status="success" if ok else "failure",
                 detail=f"identifier={identifier} purpose={purpose}")
        return ok, message


# Module-level singleton, same convention as notification.manager.manager.
otp_manager = OTPManager()
