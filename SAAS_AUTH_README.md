# BizManager — SaaS Authentication Module
## Complete Deployment & Integration Guide

---

## What Was Added

```
bms/
├── models/
│   └── saas_auth.py          ← All 6 DB tables (SQLite dev / PostgreSQL prod)
│
├── modules/saas_auth/
│   ├── __init__.py            ← Blueprint export
│   ├── routes.py              ← All auth routes (signup → login → reset)
│   └── team.py                ← Team invite / remove / role-change
│
├── utils/
│   ├── otp_service.py         ← OTP generate, store, verify, deliver
│   ├── saas_helpers.py        ← Session, CSRF, decorators, validators
│   └── saas_middleware.py     ← Role guards, permission matrix, tenant scoping
│
├── templates/saas_auth/
│   ├── base_auth.html         ← Auth-page layout (purple gradient)
│   ├── signup.html            ← Step 1: name + mobile + email
│   ├── verify_otp.html        ← Step 2/3: email & mobile OTP (shared)
│   ├── set_pin.html           ← Step 4: 6-digit PIN setup
│   ├── business_setup.html    ← Step 5: business profile
│   ├── login.html             ← Login: mobile + PIN
│   ├── forgot_pin.html        ← Request PIN reset OTP
│   ├── reset_pin.html         ← Set new PIN after OTP
│   ├── select_business.html   ← Multi-business switcher
│   ├── profile.html           ← User profile + audit log
│   ├── team.html              ← Team member list + role editor
│   ├── team_invite.html       ← Invite form
│   └── _saas_nav.html         ← Topbar user dropdown partial
│
├── .env.example               ← All environment variables documented
├── requirements.txt           ← Updated with bcrypt + optional providers
└── config.py                  ← DevelopmentConfig + ProductionConfig
```

---

## Database Tables

| Table               | Purpose                              |
|---------------------|--------------------------------------|
| `saas_users`        | Registered users (mobile + email)    |
| `saas_businesses`   | Business profiles (multi-tenant)     |
| `saas_user_roles`   | User ↔ Business role mappings        |
| `saas_otp_tokens`   | Time-limited OTP records             |
| `saas_sessions`     | Server-side session tracking         |
| `saas_audit_logs`   | Full security audit trail            |
| `saas_pin_reset`    | Short-lived PIN reset tokens         |

---

## User Journey (Signup Flow)

```
/saas/signup          → Collect name + mobile + email
       ↓ Email OTP sent
/saas/verify-email    → Enter 6-digit OTP from email
       ↓ (Production only: SMS OTP also required)
/saas/verify-mobile   → Enter 6-digit OTP from SMS [PROD only]
       ↓
/saas/set-pin         → Choose 6-digit PIN (with confirm)
       ↓
/saas/business-setup  → Create business profile
       ↓
/dashboard/           → ✅ Logged in, business ready
```

---

## URL Reference

| Method    | URL                           | Purpose                     |
|-----------|-------------------------------|-----------------------------|
| GET/POST  | `/saas/signup`                | Registration step 1         |
| GET/POST  | `/saas/verify-email`          | Email OTP verification      |
| GET/POST  | `/saas/verify-mobile`         | SMS OTP verification (prod) |
| GET/POST  | `/saas/set-pin`               | Set 6-digit PIN             |
| GET/POST  | `/saas/business-setup`        | Create business profile     |
| GET/POST  | `/saas/login`                 | Login with mobile + PIN     |
| GET/POST  | `/saas/forgot-pin`            | Request PIN reset OTP       |
| GET/POST  | `/saas/verify-reset-otp`      | Enter reset OTP             |
| GET/POST  | `/saas/reset-pin/<token>`     | Set new PIN                 |
| GET       | `/saas/logout`                | Sign out                    |
| GET/POST  | `/saas/profile`               | View/edit profile           |
| GET       | `/saas/switch-business/<id>`  | Switch active business      |
| GET       | `/saas/select-business`       | Multi-business picker       |
| POST      | `/saas/resend-otp`            | Resend OTP (JSON API)       |
| GET       | `/saas/team`                  | Team member list            |
| GET/POST  | `/saas/team/invite`           | Invite team member          |
| POST      | `/saas/team/remove`           | Remove team member          |
| POST      | `/saas/team/role`             | Change member role          |

---

## Role Permission Matrix

| Feature              | Owner | Manager | Accountant | Staff |
|----------------------|:-----:|:-------:|:----------:|:-----:|
| View dashboard       |  ✓    |   ✓     |    ✓       |   ✓   |
| New invoice / POS    |  ✓    |   ✓     |    ✗       |   ✗   |
| Manage inventory     |  ✓    |   ✓     |    ✗       |   ✗   |
| View customers       |  ✓    |   ✓     |    ✓       |   ✓   |
| Manage purchases     |  ✓    |   ✓     |    ✗       |   ✗   |
| Finance / Ledger     |  ✓    |   ✗     |    ✓       |   ✗   |
| GST returns          |  ✓    |   ✓     |    ✓       |   ✗   |
| View reports         |  ✓    |   ✓     |    ✓       |   ✗   |
| Invite team members  |  ✓    |   ✓     |    ✗       |   ✗   |
| Manage team roles    |  ✓    |   ✗     |    ✗       |   ✗   |
| Business settings    |  ✓    |   ✗     |    ✗       |   ✗   |

---

## How to Protect Routes

```python
# Option 1 — require any SaaS login
from utils.saas_helpers import saas_login_required

@app.route("/my-page")
@saas_login_required
def my_page():
    ...

# Option 2 — require login + active business
from utils.saas_helpers import saas_business_required

@app.route("/billing")
@saas_business_required
def billing():
    ...

# Option 3 — named permission
from utils.saas_middleware import permission_required

@app.route("/finance")
@permission_required("view_finance")
def finance():
    ...

# Option 4 — specific roles
from utils.saas_middleware import owner_only, manager_or_above

@app.route("/settings")
@owner_only
def settings():
    ...

# Option 5 — multi-tenant row-level guard
from utils.saas_middleware import assert_tenant_access, get_tenant_id

@app.route("/invoice/<int:inv_id>")
@saas_business_required
def view_invoice(inv_id):
    invoice = db.fetchone("SELECT * FROM invoices WHERE id=?", (inv_id,))
    assert_tenant_access(invoice["business_id"])  # raises 403 if wrong tenant
    ...
```

---

## Development Setup (5 minutes)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy environment template
cp .env.example .env
# Edit .env — set BMS_SECRET at minimum (leave everything else as-is for dev)

# 3. Run the app (SQLite auto-created, OTPs printed to terminal)
python app.py

# 4. Visit http://localhost:5000/saas/signup
# OTPs are printed in the terminal — no email/SMS needed for dev
```

---

## Production Setup

### 1. Generate a strong secret key
```bash
python -c "import secrets; print(secrets.token_hex(32))"
# Copy output into BMS_SECRET in your environment
```

### 2. PostgreSQL database
```bash
# Create database
psql -U postgres -c "CREATE DATABASE bizmanager;"
psql -U postgres -c "CREATE USER bmsuser WITH PASSWORD 'strong_password';"
psql -U postgres -c "GRANT ALL ON DATABASE bizmanager TO bmsuser;"

# Set in environment
export DATABASE_URL="postgresql://bmsuser:strong_password@localhost:5432/bizmanager"
```

### 3. Email (Gmail SMTP example)
```bash
# Enable 2FA on Gmail → App Passwords → generate one
export EMAIL_PROVIDER=smtp
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=your@gmail.com
export SMTP_PASS=your_16_char_app_password
export SMTP_FROM="BizManager <your@gmail.com>"
```

### 4. SMS — Fast2SMS (easiest for India)
```bash
# Register at fast2sms.com, get API key
export SMS_PROVIDER=fast2sms
export FAST2SMS_API_KEY=your_api_key
```

### 5. Set production environment
```bash
export APP_ENV=production
```

### 6. Run with Gunicorn
```bash
pip install gunicorn
gunicorn "app:create_app()" --bind 0.0.0.0:8000 --workers 4
```

### 7. Nginx reverse proxy (recommended)
```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

---

## Security Features

| Feature               | Implementation                                         |
|-----------------------|--------------------------------------------------------|
| PIN storage           | bcrypt via Werkzeug `generate_password_hash`           |
| OTP storage           | bcrypt hashed, single-use, expiry-checked              |
| CSRF protection       | `secrets.compare_digest` constant-time token compare   |
| Rate limiting         | In-memory (dev); swap `check_rate_limit()` for Redis   |
| Multi-tenancy         | `assert_tenant_access()` on every data-access route    |
| Session security      | HTTPONLY + SECURE + SAMESITE=Lax cookies in production |
| Security headers      | X-Frame-Options, X-XSS-Protection, Referrer-Policy     |
| Audit logging         | Every auth event written to `saas_audit_logs`          |
| User enumeration      | Forgot-PIN returns same message regardless of match    |
| OTP replay            | Used OTPs marked immediately; attempts counter capped  |

---

## Adding SaaS Auth to Existing Routes

To make any existing blueprint require SaaS auth and isolate data by business,
add these two things:

```python
# In your module's routes.py:
from utils.saas_helpers import saas_business_required
from utils.saas_middleware import assert_tenant_access, get_tenant_id

@your_bp.route("/invoices")
@saas_business_required           # ← add this
def invoices():
    biz_id = get_tenant_id()      # ← get business from session
    rows = db.fetchall(
        "SELECT * FROM invoices WHERE business_id=?", (biz_id,)
    )
    return render_template("invoices.html", rows=rows)
```

---

## OTP Flow Diagram

```
User submits mobile/email
         │
         ▼
  generate_otp()  →  6-digit random
         │
         ▼
  store_otp()     →  bcrypt(otp) stored in saas_otp_tokens
         │                         expires_at = now + 10 min
         ▼
  send_email_otp()  ─── dev:  print to console
  send_sms_otp()    └── prod: SMTP / Twilio / Fast2SMS / MSG91
         │
         ▼
  User enters OTP in browser
         │
         ▼
  verify_and_consume_otp()
    ├── check used_at IS NULL     (not already used)
    ├── check expires_at > now    (not expired)
    ├── increment attempts        (rate-limit: max 5)
    ├── check_password_hash()     (constant-time compare)
    └── mark used_at = now        (invalidate immediately)
```

---

*Generated for BizManager v6 — production-ready SaaS auth module.*
