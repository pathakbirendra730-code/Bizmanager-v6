# BizManager — Navigation & Login Workflow Update

## Summary of 3 Fixes

---

### 1. App Admin — Separate Login, No Self-Registration

**New blueprint:** `modules/app_admin/` mounted at `/app-admin`

A completely separate login system from the SaaS business signup:

- **Login flow:** User ID + Password (1st factor) → OTP (2nd factor, always required)
- **Development:** OTP shown on-screen AND emailed to the registered email
- **Production:** OTP sent to registered mobile AND registered email
- **No public registration route exists.** The only ways to create an app admin:
  1. Run `python scripts/create_app_admin.py` once (interactive CLI, asks for credentials)
  2. An existing super-admin creates one via `/app-admin/admins/create`

**Database:** New `app_admins` table — completely separate from `saas_users`.
The public `/saas/signup` route has zero code paths that can write to this table.

**Run once after deploy:**
```bash
cd bms/
python scripts/create_app_admin.py
```

**Then log in at:**
```
https://yourapp.com/app-admin/login
```

**Two admin levels:**
| Level | Can do |
|---|---|
| Admin | View all users, businesses, invites |
| Super Admin | All of the above + create/deactivate other admins |

---

### 2. All Users Page Now Shows New Registrations

**Root cause:** New SaaS signups go into `saas_users`, but the old `/auth/users` page
only queried the legacy `users` table — two separate stores, one page.

**Fix:** New unified page at `/app-admin/users` shows **both**:
- All SaaS business users (with their business + role memberships, verification status)
- All legacy ERP users (if any still exist)

Includes search by name, mobile, or email.

---

### 3. Team Members Skip Business Setup

**Root cause:** Every newly-verified signup was unconditionally sent to
`business_setup`, even if they were invited to an existing business.

**Fix — invite-before-signup support:**

1. Owner/manager goes to **Team → Invite Team Member**
2. Enters the person's **mobile or email** (works even if they don't have an account yet)
3. Two outcomes:
   - **Person already has a verified account** → added to the business immediately
   - **Person hasn't signed up yet** → a `saas_pending_invites` row is created (valid 14 days)
4. When that person later completes signup (OTP verification + PIN), the system
   automatically checks for a matching pending invite by their email or mobile
5. If found: they're added to that business with the invited role, and **skip
   business setup entirely** — landing straight on the business dashboard
6. If not found: they proceed to `business_setup` as a new business owner, as before

**New table:** `saas_pending_invites` — tracks invites issued before signup,
with status `pending → accepted` (or `revoked`).

Owners/managers can see and revoke pending invites from the **Team** page.

---

## New Routes Reference

### App Admin (`/app-admin`)
| Route | Access | Purpose |
|---|---|---|
| `GET/POST /app-admin/login` | Public | Step 1: user ID + password |
| `POST /app-admin/verify-otp` | Pending login | Step 2: OTP |
| `POST /app-admin/resend-otp` | Pending login | Resend OTP |
| `GET /app-admin/logout` | Any admin | Sign out |
| `GET /app-admin/dashboard` | Admin | Platform overview |
| `GET /app-admin/users` | Admin | All users (SaaS + legacy) |
| `GET /app-admin/businesses` | Admin | All businesses |
| `POST /app-admin/businesses/<id>/toggle` | Admin | Activate/deactivate a business |
| `GET /app-admin/invites` | Admin | All pending invites (platform-wide) |
| `GET /app-admin/admins` | **Super Admin** | List app admins |
| `GET/POST /app-admin/admins/create` | **Super Admin** | Create new app admin |
| `POST /app-admin/admins/<id>/toggle` | **Super Admin** | Activate/deactivate an admin |

### Team / Invites (`/saas/team`)
| Route | Access | Purpose |
|---|---|---|
| `GET /saas/team` | Owner/Manager/Accountant/Staff | View team + pending invites |
| `GET/POST /saas/team/invite` | Owner/Manager | Invite by mobile or email |
| `POST /saas/team/invite/<id>/revoke` | Owner/Manager | Revoke a pending invite |
| `POST /saas/team/remove` | Owner | Remove a member |
| `POST /saas/team/role` | Owner | Change a member's role |

---

## Database Schema Additions

```sql
-- App admin accounts — fully separate from saas_users
CREATE TABLE app_admins (
    id, user_id, password_hash, full_name,
    mobile, email, is_active, is_super, last_login, created_at
);

-- Invites issued before the invitee has an account
CREATE TABLE saas_pending_invites (
    id, business_id, mobile, email, role,
    invited_by, status, expires_at, accepted_by, accepted_at, created_at
);
```

---

## Security Notes

- App admin OTPs use a separate namespace (`admin:<id>`) from SaaS user OTPs —
  no cross-contamination between the two systems.
- `app_admin_required` and `super_admin_required` decorators check **only**
  `session["admin_id"]` — completely independent from `saas_user_id` and
  legacy `user_id`/`role` keys (no session-bleed risk, consistent with the
  earlier multi-tenant isolation fix).
- Pending invites expire after 14 days and can be revoked any time before acceptance.
- A revoked or expired invite cannot be auto-accepted — the matching logic
  filters by `status='pending' AND expires_at > now()`.
