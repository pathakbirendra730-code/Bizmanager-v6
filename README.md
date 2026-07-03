# 🏪 BizManager – Business Management System

A lightweight, full-featured **Mini ERP** for shopkeepers and business owners.
Runs fully **offline** on a local Flask server — optimized for tablets (Pydroid 3).

---

## ✅ Features

| Module            | Features                                                        |
|-------------------|-----------------------------------------------------------------|
| 🔐 Auth           | Login/Logout, Role-based (Admin/Staff), Session management      |
| 🧾 Billing (POS)  | Create bills, multi-item cart, GST/discount calc, print invoice |
| 📦 Inventory      | Add/Edit/Delete products, categories, low-stock alerts          |
| 👥 Customers      | CRUD, purchase history, search                                  |
| 💰 Finance        | Daily/monthly P&L, expense tracker, charts                      |
| 📊 Reports        | Sales (daily/monthly), inventory report, CSV exports            |
| ⚙️ Settings       | Shop info, GST rate, invoice prefix, DB backup                  |
| 🌙 Dark Mode      | Toggle from topbar, saved in browser                            |

---

## 🚀 Quick Start

### Option A – Standard Python / PC

```bash
# 1. Install dependencies (Python 3.7+ required)
pip install -r requirements.txt

# 2. Run the app
python app.py

# 3. Open in browser
#    http://127.0.0.1:5000
```

### Option B – Pydroid 3 (Android Tablet)

```bash
# Inside Pydroid 3 Terminal:
pip install flask werkzeug

# Run:
python app.py

# Open browser and go to:
http://127.0.0.1:5000
```

### Option C – Access from another device (same Wi-Fi)

The app binds to `0.0.0.0:5000` so other devices on the same network can access it:
```
http://<your-device-ip>:5000
```

---

## 🔑 Demo Credentials

| Role  | Username | Password  |
|-------|----------|-----------|
| Admin | admin    | admin123  |
| Staff | staff    | staff123  |

---

## 📁 Project Structure

```
bms/
├── app.py                  ← Main Flask app + blueprint registration
├── requirements.txt        ← pip dependencies
├── database.db             ← SQLite DB (auto-created on first run)
├── README.md
│
├── models/
│   └── database.py         ← Schema creation + sample data seeder
│
├── modules/                ← Flask Blueprints (one per feature)
│   ├── auth.py             ← Login/logout/users
│   ├── dashboard.py        ← Dashboard stats
│   ├── billing.py          ← POS + invoice history
│   ├── inventory.py        ← Product CRUD + stock API
│   ├── customers.py        ← Customer CRUD + history
│   ├── finance.py          ← Finance dashboard + expenses + settings
│   └── reports.py          ← Reports + CSV export
│
├── utils/
│   └── helpers.py          ← Auth decorators, invoice gen, stats
│
├── templates/              ← Jinja2 HTML templates
│   ├── base.html           ← Shared layout (sidebar, topbar)
│   ├── auth/
│   ├── dashboard/
│   ├── billing/
│   ├── inventory/
│   ├── customers/
│   ├── finance/
│   └── reports/
│
└── static/
    ├── css/style.css       ← Complete design system (light + dark)
    └── js/main.js          ← Sidebar, dark mode, utilities
```

---

## 🗄️ Database Tables

| Table           | Purpose                          |
|-----------------|----------------------------------|
| users           | Auth accounts with roles         |
| categories      | Product categories               |
| products        | Inventory items with stock       |
| customers       | Customer profiles                |
| invoices        | Bill headers                     |
| invoice_items   | Line items per invoice           |
| expenses        | Expense records                  |
| settings        | Key-value app config             |

---

## 💾 Backup & Restore

**Backup:** Admin → Backup DB (downloads `bms_backup_<date>.db`)

**Restore:** Replace `database.db` in the project root with your backup file.

---

## 🔧 Customization

Edit `models/database.py` → `seed_sample_data()` to change sample data.

Edit `finance/settings` from within the app to update:
- Shop name, address, phone, GST number
- Default GST rate
- Currency symbol
- Invoice number prefix

---

## 📦 Dependencies

```
Flask==2.3.3
Werkzeug==2.3.7
```

**Chart.js** is loaded from CDN (cached after first load – works offline thereafter).

---

## ⚠️ Security Note

This is designed for **local/LAN use only**.
Do not expose to the public internet without adding proper security measures
(HTTPS, stronger auth, rate limiting, etc.).

Change `app.secret_key` in `app.py` before deployment.
