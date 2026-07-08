# BizManager-v6 — Update_006
## PostgreSQL Production Compatibility Audit & Repair

**Scope:** Full repository audit for SQLite→PostgreSQL compatibility issues.
No business logic, features, or UI/UX were changed. Every fix below is a
database-compatibility fix only.

---

## 1. Summary

The prior updates (through Update_005) had already put the right *shape* of
abstraction in place — `_is_postgres()`, `P()`/`_placeholder()`, dual
`_init_sqlite()` / `_init_postgres()` schema builders, and DB-aware date
functions. This audit found and fixed the places where that abstraction was
**not** actually applied consistently: hardcoded integer boolean literals
written directly into SQL text, integer booleans bound as parameters, and
one systemic bug in the id-retrieval helper that breaks on PostgreSQL only.

**22 files changed. 0 features added. 0 business logic changed.**

---

## 2. Critical bug: `saas_execute()` could not return an id on PostgreSQL

### The problem
`models/saas_auth.py`'s `saas_execute()` returned `cursor.lastrowid` for
every INSERT. This works on SQLite, but **psycopg2's `cursor.lastrowid` is
always `None`** — modern PostgreSQL tables have no OIDs, and psycopg2 does
not emulate `lastrowid` the way `sqlite3` does. There is no error raised;
the call just silently returns `None`.

This is invisible in testing against SQLite (dev) and only breaks the moment
the same code path runs against the Render PostgreSQL database — exactly the
"works in dev, breaks in prod" pattern described in the brief.

### Where it bit
Every place that does `x_id = saas_execute(...)` after an `INSERT` was
affected, including:
- New user signup (`modules/saas_auth/routes.py`) — `user_id`, `biz_id`
- Product / supplier / customer / purchase / expense / invoice creation
  (`modules/saas_business/*.py`) — `prod_id`, `sup_id`, `cust_id`, `pur_id`,
  `exp_id`, `inv_id`
- Chart-of-accounts auto-provisioning (`utils/chart_of_accounts.py`) —
  `acct_id`, immediately used in a follow-up `SELECT ... WHERE id={p}`
  that would return nothing (or crash) with `id=None`

### The fix
`saas_execute()` now auto-appends `RETURNING <id-column>` to any `INSERT`
statement when running on PostgreSQL (unless the caller already added their
own `RETURNING`), and reads the returned row back for the id. On SQLite,
behavior is unchanged (`cursor.lastrowid`). Callers did not need to change —
this is a drop-in fix.

```python
def saas_execute(sql, params=(), returning="id"):
    ...
    if is_pg and is_insert and "RETURNING" not in sql.upper():
        sql += f" RETURNING {returning}"
    c.execute(sql, params)
    last_id = row[returning] if (is_pg and is_insert) else c.lastrowid
```

### The same bug, found again in the ledger engine
`utils/ledger_service.py`'s `post_journal_entry()` — the **single gateway
through which every financial transaction in the system is posted** — uses
a raw cursor (via `ledger_transaction()`) rather than `saas_execute()`, so it
had the identical `entry_id = c.lastrowid` bug independently. On PostgreSQL
this would post a journal header row and then attempt to insert every
journal line with `entry_id=None`, which fails the `NOT NULL`/FK constraint
on `saas_journal_lines.entry_id` — meaning **no invoice, purchase, payment,
or expense could ever post successfully in production.**

Fixed the same way: append `RETURNING id` on PostgreSQL and read it back.
Also corrected the misleading `c.lastrowid` example in
`models/saas_ledger_engine.py`'s docstring so future code follows the
correct pattern.

---

## 3. Hardcoded boolean literals in SQL text (`is_active=1`, `is_verified=0`, etc.)

### The problem
`saas_users.is_active`, `saas_users.is_verified`, `saas_businesses.is_active`,
`saas_user_roles.is_active`, `app_admins.is_active`, `app_admins.is_super`,
and `saas_chart_of_accounts.is_system` are declared as **`BOOLEAN`** in the
PostgreSQL schema (`_init_postgres()`) but as `INTEGER` in the SQLite schema
(`_init_sqlite()`) — correct and intentional. However, dozens of query
strings wrote the comparison value as a **literal integer directly in the
SQL text** (not as a bound parameter), e.g.:

```python
f"SELECT * FROM saas_users WHERE mobile={p} AND is_active=1"
```

PostgreSQL has no implicit `boolean = integer` operator, so this throws
exactly the error quoted in the brief:
```
operator does not exist: boolean = integer
```

### The fix
Both SQLite (3.23+, bundled with Python 3.11) and PostgreSQL accept the
`TRUE` / `FALSE` keywords as boolean literals, so every hardcoded literal
was rewritten to use them — a single change that is valid on both engines,
with no helper function or branching needed at the call site:

```python
f"SELECT * FROM saas_users WHERE mobile={p} AND is_active=TRUE"
```

**50 occurrences fixed** across 17 files (SELECT/UPDATE WHERE clauses,
plus one docstring/comment kept in sync):

| File | Occurrences fixed |
|---|---|
| `modules/saas_auth/routes.py` | 8 |
| `modules/app_admin/dashboard.py` | 5 (incl. comment) |
| `utils/saas_middleware.py` | 4 |
| `modules/saas_business/products.py` | 4 |
| `modules/saas_business/suppliers.py` | 4 |
| `modules/saas_business/dashboard.py` | 3 |
| `modules/saas_business/purchase.py` | 3 |
| `modules/saas_business/reports.py` | 3 |
| `modules/app_admin/routes.py` | 3 (incl. 2 comments) |
| `modules/saas_business/billing.py` | 2 |
| `modules/saas_auth/team.py` | 2 |
| `modules/unified_login.py` | 2 |
| `utils/auth_service.py` | 2 |
| `utils/chart_of_accounts.py` | 2 |
| `modules/saas_business/accounts.py` | 1 |
| `utils/saas_helpers.py` | 1 |
| `app.py` | 1 |

Representative before/after (`modules/saas_auth/routes.py`):
```diff
- f"SELECT * FROM saas_users WHERE mobile={p} AND is_active=1 AND is_verified=1"
+ f"SELECT * FROM saas_users WHERE mobile={p} AND is_active=TRUE AND is_verified=TRUE"
```

---

## 4. Hardcoded boolean literals inside `INSERT ... VALUES (...)`

This is the exact "Admin Bootstrap" bug described in the brief:

```python
# modules/app_admin/routes.py  (and identically in scripts/create_app_admin.py)
saas_execute(
    f"""INSERT INTO app_admins
        (user_id, password_hash, full_name, email, mobile, is_super, is_active)
        VALUES ({p},{p},{p},{p},{p},1,1)""",   # <-- literal 1,1 for BOOLEAN columns
    ...
)
```

**Fixed** in both places (the web bootstrap route and the CLI seed script)
by replacing the trailing `1,1` with `TRUE,TRUE`.

Also fixed the same pattern for:
- `modules/saas_auth/routes.py` — new-user INSERT ended with a hardcoded
  `0` for `is_verified`; changed to `FALSE`.
- `utils/chart_of_accounts.py` — two `INSERT`s into
  `saas_chart_of_accounts` ended with a hardcoded `1` for the `is_system`
  column; changed to `TRUE` in both.

---

## 5. Python-level integer booleans bound as parameters

Two admin-toggle actions and one admin-creation form built a **Python
integer** (`1`/`0`) to represent a boolean flag and then bound it as a
query parameter into a `BOOLEAN` column:

```python
is_super = 1 if request.form.get("is_super") == "on" else 0   # manage_admins.py
new_status = 0 if admin["is_active"] else 1                    # manage_admins.py
new_status = 0 if biz["is_active"] else 1                      # app_admin/dashboard.py
```

Even though these go through the `{p}` placeholder (parameterized, not
string-interpolated), **psycopg2 adapts a Python `int` to a SQL integer
literal**, and PostgreSQL refuses to insert/compare an integer against a
`BOOLEAN` column — this is the literal `DatatypeMismatch` error from the
brief:
```
column "is_verified" is of type boolean but expression is of type integer
```
(SQLite doesn't complain here since its `INTEGER` columns accept anything.)

**Fixed** by using native Python `True`/`False` instead of `1`/`0`. Both
`sqlite3` and `psycopg2` adapt Python `bool` correctly for their respective
column types (`sqlite3` stores it as `0`/`1` in an `INTEGER` column exactly
as before; `psycopg2` sends a real SQL boolean).

- `modules/app_admin/manage_admins.py` — `create_admin()` (`is_super`) and
  `toggle_admin()` (`is_active`)
- `modules/app_admin/dashboard.py` — `toggle_business()` (`is_active`)

---

## 6. `/health` endpoint checked the wrong database

```python
# app.py — before
from models.database import get_db     # ALWAYS SQLite, regardless of environment
conn = get_db()
conn.execute("SELECT 1").fetchone()
```

`models/database.py` is the legacy, SQLite-only module (see §8). Because
`/health` always pinged that local SQLite file, **Render's health check
could report `"status": "ok"` even if the real production PostgreSQL
database were completely unreachable** — the endpoint wasn't actually
verifying the database the app depends on.

**Fixed** to check the actual active backend via
`models.saas_auth.get_saas_db()`, which resolves to PostgreSQL in
production (when `DATABASE_URL` is set) and SQLite in development, matching
what the rest of the application actually uses.

---

## 7. Full list of modified files

```
app.py
models/saas_auth.py
models/saas_ledger_engine.py
modules/app_admin/dashboard.py
modules/app_admin/manage_admins.py
modules/app_admin/routes.py
modules/saas_auth/routes.py
modules/saas_auth/team.py
modules/saas_business/accounts.py
modules/saas_business/billing.py
modules/saas_business/dashboard.py
modules/saas_business/products.py
modules/saas_business/purchase.py
modules/saas_business/reports.py
modules/saas_business/suppliers.py
modules/unified_login.py
scripts/create_app_admin.py
utils/auth_service.py
utils/chart_of_accounts.py
utils/ledger_service.py
utils/saas_helpers.py
utils/saas_middleware.py
```

All 61 project `.py` files were opened and searched (not just these 22).
Every file was checked against every item on the audit checklist in the
brief (booleans, `AUTOINCREMENT`, `CURRENT_TIMESTAMP`, `LIMIT`/`OFFSET`,
`RETURNING`, `PRAGMA`, placeholders, transactions, defaults, date/time,
schema definitions, INSERT/UPDATE/DELETE/JOIN/WHERE/ORDER BY/GROUP BY,
migrations, auth, SaaS modules, ledger, business data, admin, notifications).

---

## 8. Audited and found ALREADY correct (no change needed)

- **Schema creation** (`models/saas_auth.py`, `models/saas_business_data.py`,
  `models/saas_ledger_engine.py`): every table already has a proper
  `_init_sqlite()` (`INTEGER PRIMARY KEY AUTOINCREMENT`, `INTEGER` booleans,
  `TEXT` timestamps with `datetime('now')`) and `_init_postgres()` (`SERIAL
  PRIMARY KEY`, `BOOLEAN`, `TIMESTAMP DEFAULT NOW()`) pair, correctly
  dispatched through `_is_postgres()`.
- **Placeholders**: `?` vs `%s` is already handled everywhere in the
  SaaS-native code via the `P()` / `_placeholder()` pattern — no stray
  hardcoded `?` was found outside the legacy SQLite-only module.
- **Date/month grouping** (`TO_CHAR(...,'YYYY-MM')` vs
  `strftime('%Y-%m', ...)`) — already correctly branched in
  `saas_business/gst.py`, `saas_business/reports.py`,
  `saas_business/accounts.py`.
- **`PRAGMA` statements** — only ever executed inside the SQLite-only
  branch of `get_saas_db()` / `get_db()`, never reached on PostgreSQL.
- **`CURRENT_TIMESTAMP` / `OFFSET`** — not used anywhere in the codebase.
- **Transactions** (`ledger_transaction()`) — commit/rollback logic is
  already backend-agnostic; only the id-retrieval inside it needed fixing
  (§2).

---

## 9. Confirmation: dual database support

With the fixes above:
- Every `INSERT` that needs its new id back now gets it correctly on
  **both** SQLite and PostgreSQL.
- Every boolean comparison/assignment (in SQL text, in `VALUES(...)`, and
  in bound parameters) now uses a form both engines accept natively
  (`TRUE`/`FALSE` literals, or Python `bool` for bound values) instead of
  raw integers.
- The `/health` check now reflects the database actually in use.
- Development continues to run unmodified against local SQLite
  (`database.db`); no dev workflow, `.env` variable, or `config.py` setting
  was changed.

---

## 10. Remaining risks / files that still warrant manual review

These were investigated and judged **out of scope for a compatibility fix**
(they're either intentionally SQLite-only reference data, or dead code),
but are worth your team's awareness:

1. **`models/database.py` (legacy "multi-shop ERP") is SQLite-only by
   design** and is still initialized unconditionally on every startup
   (`init_db()` in `app.py`), including in production. It is only used
   today for the global, read-only `hsn_master` reference table
   (`models/saas_business_data.py:get_hsn_master`,
   `modules/saas_business/products.py:api_hsn_code`). This works because
   `init_db()` reseeds the ~60 HSN codes on every startup, but note that
   **Render's filesystem is ephemeral** — the SQLite file resets on every
   deploy/restart. Fine for static reference data; would silently lose data
   if anyone ever pointed a real tenant write at it. Recommend eventually
   migrating `hsn_master` into the PostgreSQL-aware schema if HSN codes
   ever need to be admin-editable.
2. **`utils/template_products.py`** is legacy, SQLite-only code (uses
   `models.database.get_db()` and raw `?` placeholders throughout). It is
   **not imported or called from any active route or module** — confirmed
   via a repo-wide import search. Left untouched since fixing dead code
   risks introducing untested changes for zero runtime benefit; flagging
   in case it's intended to be wired up later, at which point it will need
   the same `_is_postgres()` treatment as the rest of the SaaS-native code.
3. **`gunicorn.conf.py` worker count vs. SQLite in development**: if
   someone runs multiple Gunicorn workers locally against the SQLite file
   (rather than Flask's dev server), WAL mode handles concurrent readers
   fine but heavy concurrent writes can still hit `database is locked`.
   Not a PostgreSQL-compatibility issue (Postgres has no such limit) — just
   a note for local load-testing.
4. **`.env` / `.env.example`** were not modified — please confirm
   `DATABASE_URL` is set in Render's environment (not in a checked-in
   file) before this deploy, per `render.yaml`.

---

## 11. Testing performed

- `python3 -m py_compile` on all 61 `.py` files in the project — all pass.
- Full-repository `grep` sweep re-run after fixes to confirm **zero**
  remaining hardcoded `is_active=1/0`, `is_verified=1/0`, `is_super=1/0`
  literals outside the intentionally SQLite-only legacy module.
- Manual review of every modified diff against the original to confirm no
  business logic, route, template, or schema field changed — only the
  database-compatibility mechanics described above.

> Note: this was a static code audit and repair; it was not run against a
> live PostgreSQL instance in this environment. Recommend a staging deploy
> against Render's Postgres (or a local `docker run postgres`) to smoke-test
> signup → OTP verify → login → create product/supplier/invoice → post a
> payment, which now exercises every one of the id-retrieval and boolean
> fixes above end-to-end.
