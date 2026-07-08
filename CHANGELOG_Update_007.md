# BizManager-v6 — Update_007
## Fix: OTP verification & PIN-reset link failing on PostgreSQL

**3 files changed. No business logic, routes, or templates changed.**

---

## The bug

Both of these threw an unhandled `TypeError` on PostgreSQL (never on SQLite):

```python
# utils/otp_service.py — every OTP verification (signup, login, admin 2FA)
if datetime.utcnow() > datetime.fromisoformat(token["expires_at"]):

# modules/saas_auth/routes.py — PIN-reset link expiry check
if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
```

`expires_at` is `TEXT` in the SQLite schema (so `sqlite3` always returns a
plain ISO string — `fromisoformat()` works) but `TIMESTAMP` in the
PostgreSQL schema. **psycopg2 automatically converts `TIMESTAMP` columns
into native `datetime.datetime` objects** on fetch, not strings. Calling
`datetime.fromisoformat()` on a value that's already a `datetime` raises
`TypeError: fromisoformat: argument must be str`.

That exception was caught by a broad `except Exception` in
`verify_and_consume_otp()` and surfaced to the user as the generic
**"Verification error. Please try again."** message — reproduced live on
the deployed instance during OTP verification for the app-admin bootstrap
login. The identical pattern in `reset_pin()` means "Forgot PIN" links
would fail the same way for any business user, unhandled (500 there, since
that call isn't wrapped in a try/except at all).

## The fix

Added one shared helper, `parse_dt()`, in `models/saas_auth.py`:

```python
def parse_dt(value):
    """Normalize a timestamp column value to a datetime object, regardless
    of backend — psycopg2 returns TIMESTAMP columns as datetime objects,
    sqlite3 returns TEXT columns as strings."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
```

and routed both call sites through it instead of calling
`datetime.fromisoformat()` directly:

- `utils/otp_service.py` — `verify_and_consume_otp()` expiry check
- `modules/saas_auth/routes.py` — `reset_pin()` expiry check

`utils/otp_manager.py` (used by app-admin login 2FA, SaaS signup/login OTP,
and resend) is a thin wrapper around `otp_service.verify_and_consume_otp()`,
so this single fix covers **every OTP flow in the app**, not just the one
reproduced.

## Files changed

```
models/saas_auth.py          — added parse_dt() helper
utils/otp_service.py          — use parse_dt() for OTP expiry check
modules/saas_auth/routes.py   — use parse_dt() for PIN-reset link expiry check
```

## Testing

- `python3 -m py_compile` on all three files — passes.
- Re-checked the rest of the codebase for the same `fromisoformat` pattern
  on DB-sourced values — confirmed these were the only two occurrences.
- Not run against a live PostgreSQL instance in this environment; please
  redeploy and re-test: app-admin login OTP (the flow you hit the error
  on), a normal signup/login OTP, and a PIN-reset link, to confirm all
  three now complete successfully.
