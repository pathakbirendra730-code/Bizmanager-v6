# Migration Guide: BizManager v1 → v2 Multi-Shop ERP

## Overview
v2 adds multi-tenancy (shops), HSN codes, full GST compliance (CGST/SGST/IGST),
and a super-admin panel — while keeping all v1 data intact.

---

## Steps to Upgrade an Existing v1 Database

### 1. Backup first (always!)
```bash
cp database.db database_v1_backup.db
```

### 2. Replace all Python files
Copy the new `app.py`, `config.py`, `models/`, `modules/`, `utils/` into your project root.

### 3. Replace all templates and static files
Copy `templates/` and `static/` — all old templates are replaced.

### 4. Install (same deps, no new packages required)
```bash
pip install -r requirements.txt
```

### 5. Run — migrations happen automatically
```bash
python app.py
```

On startup the app:
- Creates new tables (`shops`, `hsn_master`) if they don't exist
- Runs safe `ALTER TABLE ADD COLUMN` for every new column
  (existing columns are silently skipped — **no data loss**)
- Existing data gets `shop_id = 1` (default) via column default

### 6. Create your first shop (or use seed data)
If starting fresh, seed data creates 2 demo shops automatically.

If upgrading an existing DB:
1. Log in as `superadmin` (created automatically on first run if not present)
2. Go to **Admin → Manage Shops → Add Shop**
3. Fill in your GSTIN, state code, invoice prefix
4. Go to **Admin → Users** and assign your existing users to the new shop

---

## Column Migrations Applied Automatically

| Table          | New Columns Added                                    |
|----------------|------------------------------------------------------|
| users          | shop_id, is_active, last_login, email, phone         |
| products       | shop_id, hsn_code, gst_rate, is_active               |
| customers      | shop_id, state_code, gstin                           |
| invoices       | shop_id, customer_gstin, customer_state, supply_type,|
|                | taxable_amount, cgst_amount, sgst_amount, igst_amount|
|                | total_tax, place_of_supply                           |
| invoice_items  | shop_id, hsn_code, taxable_amount, gst_rate,         |
|                | cgst_rate, sgst_rate, igst_rate,                     |
|                | cgst_amount, sgst_amount, igst_amount                |
| expenses       | shop_id                                              |
| categories     | shop_id                                              |

---

## New Tables Created

| Table       | Purpose                                   |
|-------------|-------------------------------------------|
| shops       | Multi-tenant shop registry                |
| hsn_master  | 55+ Indian HSN codes with default GST %   |

---

## Credential Changes

| Role        | Username    | Password    |
|-------------|-------------|-------------|
| Super Admin | superadmin  | Super@1234  |
| Shop Owner  | owner1      | Owner@123   |
| Shop Owner  | owner2      | Owner@123   |
| Staff       | staff1      | Staff@123   |

Old `admin`/`admin123` login still works if it exists in your DB —
just assign it a shop via the Users page.
