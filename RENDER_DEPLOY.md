# BizManager — Deploy to Render via GitHub
## Complete Step-by-Step Guide

---

## Prerequisites

- GitHub account (free) → https://github.com
- Render account (free) → https://render.com
- Your BizManager project folder

---

## STEP 1 — Push to GitHub

### 1.1 Create a new repository

1. Go to https://github.com/new
2. Repository name: `bizmanager`
3. Set to **Private** (recommended — contains your business code)
4. Click **Create repository**

### 1.2 Push your code

Open a terminal inside your `bms/` folder and run:

```bash
cd bms/

# Initialise git (if not already done)
git init

# Add all files (respects .gitignore — won't add .env or .db files)
git add .

# First commit
git commit -m "BizManager v6 — initial commit with SaaS auth"

# Connect to your GitHub repo (replace YOUR_USERNAME)
git remote add origin https://github.com/YOUR_USERNAME/bizmanager.git

# Push to GitHub
git push -u origin main
```

> ✅ Verify your `.env` file is NOT visible on GitHub after pushing.
> Only `.env.example` should be there.

---

## STEP 2 — Create PostgreSQL Database on Render

1. Go to https://dashboard.render.com
2. Click **New +** → **PostgreSQL**
3. Fill in:
   - **Name:** `bizmanager-db`
   - **Database:** `bizmanager`
   - **User:** `bizmanager`
   - **Region:** `Singapore` (closest to India)
   - **Plan:** `Free` (expires after 90 days) or `Starter` ($7/mo, no expiry)
4. Click **Create Database**
5. Wait ~2 minutes for it to provision
6. **Copy the "Internal Database URL"** — you'll need it in Step 4

---

## STEP 3 — Create Web Service on Render

1. Go to https://dashboard.render.com
2. Click **New +** → **Web Service**
3. Click **Connect a repository** → Select your `bizmanager` GitHub repo
4. Fill in:

| Field | Value |
|-------|-------|
| **Name** | `bizmanager` |
| **Region** | `Singapore` |
| **Branch** | `main` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn "app:create_app()" -c gunicorn.conf.py` |
| **Plan** | `Free` (or Starter for always-on) |

5. **Do NOT click Deploy yet** — set environment variables first (Step 4)

---

## STEP 4 — Set Environment Variables

Still on the "Create Web Service" page, scroll to **Environment Variables**.
Click **Add Environment Variable** for each one below:

### Required (must set)

| Key | Value |
|-----|-------|
| `APP_ENV` | `production` |
| `BMS_SECRET` | Run `python -c "import secrets; print(secrets.token_hex(32))"` and paste output |
| `DATABASE_URL` | Paste the **Internal Database URL** from Step 2 |

### Email (required for OTP)

| Key | Value |
|-----|-------|
| `EMAIL_PROVIDER` | `smtp` |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `your@gmail.com` |
| `SMTP_PASS` | Your Gmail App Password (16 chars, no spaces) |
| `SMTP_FROM` | `BizManager <your@gmail.com>` |

> **Gmail App Password setup:**
> 1. Go to https://myaccount.google.com/apppasswords
> 2. Enable 2-Step Verification if not done
> 3. Select "Mail" → Generate
> 4. Copy the 16-character password → paste into `SMTP_PASS`

### SMS (optional — for mobile OTP in production)

| Key | Value |
|-----|-------|
| `SMS_PROVIDER` | `fast2sms` |
| `FAST2SMS_API_KEY` | Your Fast2SMS API key from fast2sms.com |

---

## STEP 5 — Deploy

1. Click **Create Web Service**
2. Render will:
   - Clone your GitHub repo
   - Run `pip install -r requirements.txt`
   - Start gunicorn
   - Run health check on `/health`
3. Watch the build logs in the Render dashboard
4. When you see **"Your service is live"** → done! ✅

Your app URL will be: `https://bizmanager.onrender.com`

---

## STEP 6 — Verify It's Working

Open your app URL and check:

```
https://bizmanager.onrender.com/health
```

Should return:
```json
{
  "status": "ok",
  "db": "ok",
  "env": "production"
}
```

Then go to:
```
https://bizmanager.onrender.com/saas/signup
```

Register a new account — the OTP should arrive in your email inbox.

---

## Automatic Deploys (Git Push = Auto Deploy)

Every time you push to GitHub, Render automatically:
1. Pulls latest code
2. Runs `pip install -r requirements.txt`
3. Restarts gunicorn with zero downtime

```bash
# Example: make a change, push, auto-deployed in ~2 minutes
git add .
git commit -m "Fix: update email template"
git push
```

---

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `APP_ENV` | ✅ | Set to `production` |
| `BMS_SECRET` | ✅ | Long random secret key for Flask sessions |
| `DATABASE_URL` | ✅ | PostgreSQL connection string from Render |
| `EMAIL_PROVIDER` | ✅ | `smtp` / `sendgrid` / `ses` |
| `SMTP_HOST` | ✅ | `smtp.gmail.com` for Gmail |
| `SMTP_PORT` | ✅ | `587` |
| `SMTP_USER` | ✅ | Your email address |
| `SMTP_PASS` | ✅ | Gmail App Password (NOT your login password) |
| `SMTP_FROM` | ✅ | Display name + email |
| `SMS_PROVIDER` | Optional | `fast2sms` / `twilio` / `msg91` |
| `FAST2SMS_API_KEY` | Optional | Fast2SMS API key |
| `OTP_EXPIRY_MINUTES` | Optional | Default: `10` |

---

## Troubleshooting Render Deployments

### Build fails: "Module not found"
```
Check requirements.txt has all packages.
Flask and Werkzeug versions must match exactly.
```

### App crashes on start: "BMS_SECRET is None"
```
Set BMS_SECRET in Render environment variables.
Generate with: python -c "import secrets; print(secrets.token_hex(32))"
```

### Database error: "could not connect to server"
```
1. Check DATABASE_URL is set correctly in Render env vars
2. Make sure the database region matches the web service region
3. Use Internal Database URL (not External) for same-region connections
```

### Health check fails: "Service unavailable"
```
1. Check build logs — look for Python errors
2. Check gunicorn is starting: "Listening at: http://0.0.0.0:PORT"
3. Check /health endpoint manually
```

### OTP emails not sending
```
1. Verify SMTP_USER and SMTP_PASS are set in Render env vars
2. Check Render logs for "[OTP] ❌" messages
3. Test with Gmail App Password (NOT regular password)
4. Check spam folder
```

### Free tier sleeping (Render free plan spins down after 15 min inactivity)
```
Free tier web services sleep after 15 minutes of no traffic.
First request after sleep takes ~30 seconds to wake up.

Solutions:
- Upgrade to Starter plan ($7/mo) — always-on
- Use a cron service like cron-job.org to ping /health every 10 minutes
```

---

## Keeping the Free Database Alive

Render's free PostgreSQL expires after **90 days**.

**Options:**
1. **Upgrade to Starter database** ($7/mo) — no expiry, recommended for production
2. **Export + reimport** before 90 days:
   ```bash
   # Export (run from your computer)
   pg_dump YOUR_DATABASE_URL > backup.sql
   # Import to new DB
   psql NEW_DATABASE_URL < backup.sql
   ```

---

## Custom Domain (Optional)

1. In Render dashboard → your web service → **Settings** → **Custom Domain**
2. Add your domain: `app.yourdomain.com`
3. Render gives you a CNAME record to add in your DNS provider
4. Wait for DNS to propagate (5–30 minutes)
5. SSL certificate is auto-provisioned by Render ✅

---

## File Structure for GitHub

Your repo should look like this:
```
bizmanager/                    ← GitHub repo root
├── .gitignore                 ← Excludes .env, .db, __pycache__
├── .env.example               ← Template (safe to commit)
├── render.yaml                ← Render Blueprint (optional)
├── runtime.txt                ← python-3.11.0
├── requirements.txt           ← All dependencies
├── gunicorn.conf.py           ← Production server config
├── app.py                     ← Application factory
├── config.py
├── models/
├── modules/
├── utils/
├── templates/
└── static/
```

---

*BizManager v6 — Render + GitHub Deployment Guide*
