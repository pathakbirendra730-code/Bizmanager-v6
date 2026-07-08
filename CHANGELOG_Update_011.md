# BizManager-v6 — Update_011
## Fix: "Delete Account Permanently" 500 error, plus account-takeover hardening

**4 files changed.**

---

## 1. Bug fix: Delete Account Permanently → Internal Server Error

**Cause:** `saas_users` is referenced by `created_by`/`invited_by`/`accepted_by`
columns in 13 other tables (invoices, payments, expenses, purchases,
ledger, cash book, bank book, journal entries, businesses, user_roles,
audit logs, pending invites) — **without** `ON DELETE CASCADE`. The
moment you tried to delete a user who had created even one real record
(an invoice, an expense, anything), the database rejected the `DELETE`
with a foreign-key violation, and since nothing caught that exception, it
surfaced as a generic 500.

(Three other references — `saas_user_roles.user_id`, `saas_sessions.user_id`,
`saas_pin_reset.user_id` — already cascade correctly and needed no change.)

**Fix:** Before deleting the user row, `delete_user()` now sets each of
those 13 columns to `NULL` wherever they point at the user being deleted.
This is deliberate, not just a workaround: deleting a demo/test account
shouldn't take real business data down with it — an old invoice just
becomes "created by: unknown" instead of vanishing or blocking the
deletion forever. The whole operation is also now wrapped in a
try/except, so if some other constraint is ever added later, you'll get
a clear flash message instead of a 500.

File: `modules/app_admin/dashboard.py`.

---

## 2. Security hardening — the scenario you asked about

To recap what was already safe vs. what wasn't, from our conversation:

- **Already safe:** a business "manager"/"staff" user (like Rahul) has no
  way to edit *another* account's contact details, change anyone's role,
  or remove anyone — those are all scoped to the acting user's own
  session or gated by `role == 'owner'` server-side. Confirmed by code
  review, no change needed there.
- **The real gap:** if someone gets hold of an already-logged-in session
  (shared device, unattended login, stolen cookie), the change-email/
  change-mobile feature from Update_010 would let them quietly redirect
  the account's contact details with nothing more than being logged in —
  and the real owner would get no warning.

Two changes close that gap:

### a) Step-up authentication: current PIN required
Both **Change Email** and **Change Mobile** on the Profile page now
require re-entering the account's current 6-digit PIN before the OTP is
even sent. Being logged in is no longer sufficient on its own — this
matches how most services require re-authentication for sensitive account
changes.

### b) Notify the OLD contact details when a change completes
Once a change is confirmed:
- **Email changed** → an alert email goes to the **old** email address:
  *"Your BizManager email address was changed to X. If you didn't make
  this change, contact support immediately."*
- **Mobile changed** → an alert SMS goes to the **old** mobile number,
  *and* an alert email goes to the account's email (a channel the
  attacker hasn't touched), with the same message.

Both notifications are best-effort — a failed/unsent alert never blocks
the already-verified change itself, so this can't create a new way to
get stuck. Added a small `send_notice_sms()` helper (`notification/sms_service.py`,
mirroring the existing `send_notice_email()`) since no generic
non-OTP SMS function existed yet.

Files: `modules/saas_auth/routes.py` (PIN check + notifications in all 4
change-email/change-mobile routes), `notification/sms_service.py` (new
`send_notice_sms()`), `templates/saas_auth/profile.html` (added the PIN
field to both change forms).

---

## Files changed

```
modules/app_admin/dashboard.py       — fixed delete_user() 500 (FK null-out + try/except)
modules/saas_auth/routes.py          — PIN re-auth + old-contact notifications
notification/sms_service.py          — new send_notice_sms() helper
templates/saas_auth/profile.html     — added "Confirm your PIN" field to both forms
```

## Testing

- `python3 -m py_compile` on all modified `.py` files — passes.
- `templates/saas_auth/profile.html` re-parsed with `jinja2.Environment().parse()`
  — passes.
- Verified all 13 table/column names in the FK null-out list against the
  actual `CREATE TABLE` statements in `models/saas_auth.py`,
  `models/saas_business_data.py`, and `models/saas_ledger_engine.py` — all
  correct, none missed.
- Not run against a live instance in this environment. Please redeploy and
  test: deleting a user who has created at least one invoice/expense/purchase
  (the exact case that crashed before); changing your own email/mobile with
  a wrong PIN (should be rejected) and the right PIN (should proceed and
  you should receive the old-contact alert).

## What this does and doesn't cover

This closes the "hijacked session changes contact info silently" gap.
It does **not** protect against: someone who already knows the PIN (e.g.
an owner who shares their PIN with staff — no software fix replaces not
sharing credentials), or compromise of the email/SMS provider itself. If
you want session-level hardening on top of this (e.g. auto-logout on
inactivity, forcing re-login on a new device), that'd be a separate,
larger change — let me know if you'd like that scoped out.
