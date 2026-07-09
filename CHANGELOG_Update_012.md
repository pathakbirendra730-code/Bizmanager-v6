# BizManager-v6 — Update_012
## PostgreSQL-only crash: `Decimal + float` in Cash Book / Cash Receipt / Ledger

**6 files changed (1 new).** Pure type-consistency fix — no business logic,
accounting rules, GST math, routes, templates, or UI changed anywhere.

---

## 1. Root Cause

Every monetary column in the schema is declared **`NUMERIC`** on
PostgreSQL and **`REAL`** on SQLite (confirmed across
`models/saas_auth.py`, `models/saas_business_data.py`,
`models/saas_ledger_engine.py` — every single money column follows this
same dual declaration, by design, from earlier updates).

- **psycopg2** (PostgreSQL driver) returns `NUMERIC` columns as Python
  **`decimal.Decimal`** objects. This is psycopg2's default, deliberate
  behavior — NUMERIC is arbitrary-precision, and Decimal is the only
  Python type that can represent it exactly.
- **sqlite3** (SQLite driver) returns `REAL` columns as plain **`float`**.
- The application's own arithmetic — GST math, form-parsed payment
  amounts, running ledger totals — was written using plain `float`
  throughout (`float(request.form.get(...))`, `0.0` accumulators,
  `round(x, 2)`).

Python's `decimal.Decimal` deliberately raises `TypeError` when mixed
with `float` in arithmetic (`+ - * /`) — this isn't a bug in Python, it's
intentional: a `float` can't exactly represent most decimal fractions, so
silently allowing `Decimal + float` would quietly reintroduce the exact
rounding errors `Decimal` exists to prevent. Comparisons (`<`, `>`,
`min()`, `max()`) between Decimal and float ARE allowed, which matters
below.

The specific crash: `add_cash_entry()` → `record_adjustment()` →
`post_journal_entry()` → `_update_account_balance()` reads the account's
existing running total from the database (`Decimal` on Postgres) and
adds the new transaction's debit amount (a plain `float`, coerced by the
old code via `round(float(...), 2)`) — `Decimal + float` → `TypeError`.

## 2. Why SQLite Didn't Expose It

On SQLite, the exact same code produces `float + float` throughout —
the DB read is a float, the form input is a float, every step of the
arithmetic is float. It "worked," but only because both sides of every
operation happened to be the same (imprecise) type — the bug was latent,
not absent.

## 3. Why PostgreSQL Exposes It

The moment the DB read becomes `Decimal` (Postgres's correct, intended
behavior for NUMERIC) while application code still produces `float`
values from form input and internal calculations, every arithmetic
operation that combines the two raises `TypeError`. This isn't specific
to Cash Book — it's structural to how the app handled money everywhere,
which is why the audit had to cover the whole repository, not just the
failing line.

---

## 4. The Fix — One Standard Type: `Decimal`, Enforced at Two Boundaries

Per the brief's preference, `Decimal` is now the one money type used
throughout, enforced at the two places a monetary value enters Python:

### a) Every DB read (`utils/money.py` — new, `models/saas_auth.py`)
`saas_fetchone()`/`saas_fetchall()` — the central functions almost the
entire app already uses for every DB read — now run each row through a
new `normalize_row()` helper that converts any `float` value to
`Decimal`. On PostgreSQL this is a no-op (values are already Decimal);
on SQLite it makes REAL columns come back as Decimal too, so **from this
point on, a DB read is Decimal on both backends, identically** — no
caller anywhere in the app needs to know or care which database is
running underneath.

SQLite also needed one companion fix: `sqlite3` has no default adapter
for `Decimal`, so *writing* one back as a query parameter would raise
`sqlite3.ProgrammingError`. Registered
`sqlite3.register_adapter(Decimal, lambda d: float(d))` once, at import
time in `models/saas_auth.py`. PostgreSQL needs no equivalent — psycopg2
already adapts `Decimal` to `NUMERIC` natively.

### b) Every point money enters from a form or gets computed locally
`utils/money.py`'s `to_decimal(value)` safely converts `None`, `""`,
`int`, `float`, `str`, or an already-`Decimal` value into `Decimal` —
the ONE conversion function used everywhere, replacing the various
`float(request.form.get(...))` / `round(float(x), 2)` patterns at the
specific points where they fed into arithmetic against a DB-sourced
value.

This is the "shared helper instead of repeating conversions" the brief
asked for — no file reimplements its own Decimal-parsing logic.

---

## 5. Files Changed — Exact Fixes

### `utils/money.py` (new)
`to_decimal()`, `normalize_row()`, `money()` — see above. Fully
documented inline with the root-cause explanation, so future code
changes don't reintroduce this by hand-rolling a new float parse.

### `models/saas_auth.py`
- Registered the SQLite `Decimal` adapter (see 4a).
- `saas_fetchone()` / `saas_fetchall()` now call `normalize_row()` on
  every row before returning it. This single change is what makes the
  vast majority of the app's money reads (dashboard totals, invoice
  history, customer/supplier balances, reports) Decimal-consistent on
  both backends with zero changes needed at those call sites.

### `utils/ledger_service.py` — the exact reported crash site
- `_validate_lines()`: `total_debit = 0.0` / `total_credit = 0.0`
  accumulators and `round(float(line.get("debit", 0) or 0), 2)` →
  `Decimal("0")` accumulators and `to_decimal(...).quantize(Decimal("0.01"))`.
  This function is the chokepoint every journal entry (from all 17
  transaction-type functions in `ledger_transactions.py`) passes
  through, so fixing it here — rather than in each of the 17 callers —
  is what avoids "dozens of local fixes."
- `post_journal_entry()`: same `float()` → `to_decimal()` swap for the
  per-line debit/credit before insert and balance update.
- **`_update_account_balance()` — line-for-line the function named in
  the traceback.** The raw cursor read (`c.fetchone()` on the
  `ledger_transaction()` connection) bypasses `saas_fetchone()`, so it
  needed its own explicit `normalize_row()` call — this is the exact
  `existing["total_debit"] + debit` line from the bug report. `debit`/
  `credit` parameters are now also explicitly passed through
  `to_decimal()` on entry, and every `round(x, 2)` became
  `x.quantize(Decimal("0.01"))` (Decimal's own equivalent — same
  rounding behavior, no float involved).
- `get_account_balance()`: the "no balance yet" fallback was `0.0`
  (float) while the normal case now returns `Decimal` — inconsistent
  return typing that could crash a caller doing arithmetic on either
  branch. Changed the fallback to `Decimal("0")`.

### `utils/ledger_transactions.py`
Every function that does its own money arithmetic before calling into
`post_journal_entry()` now converts its `amount`/`discount`/`cgst`/
`sgst`/`igst`/`paid_amount` parameters via `to_decimal()` right at entry,
before any `+`/`-` against them:
`record_cash_sale`, `record_credit_sale`, `record_cash_purchase`,
`record_credit_purchase`, `record_payment_from_customer`,
`record_payment_to_supplier`, `record_cash_deposit_to_bank`,
`record_bank_withdrawal_to_cash`, `record_transfer`,
`record_sales_return`, `record_purchase_return`,
`record_opening_balance`, `record_adjustment`, `record_sale`,
`record_purchase`. (`record_advance_from_customer` /
`record_advance_to_supplier` do no arithmetic of their own — they just
forward to the payment functions above, which now normalize — so they
needed no change.)

This matters independently of the ledger-engine fix above: these
functions are called directly by `billing.py`, `purchase.py`,
`accounts.py`, and `suppliers.py` with whatever mix of types those
callers happen to have on hand (a locally-parsed float, or occasionally
a DB-sourced Decimal) — normalizing at entry means it's correct no
matter what a current or future caller passes in.

### `modules/saas_business/accounts.py` — the exact reported failing route
`add_cash_entry()`, `add_bank_entry()`, and `add_ledger_entry()`'s
`amount = float(request.form.get("amount", 0) or 0)` all changed to
`amount = to_decimal(request.form.get("amount", 0))`.

### `modules/saas_business/billing.py` — a second, independently-discovered crash site
`add_payment()` (recording a payment against an invoice) had the exact
same bug class, not covered by the ledger-engine fix, because it crashes
*before* ever reaching the ledger engine:

```python
amount = float(request.form.get("amount", 0) or 0)   # plain float
...
amount = round(min(amount, inv["due_amount"]), 2)     # inv["due_amount"] is Decimal
new_paid = round(inv["paid_amount"] + amount, 2)      # <- TypeError here
```

`min(float, Decimal)` does **not** raise (Decimal supports ordering
comparison with float) — so this silently returned whichever value was
smaller, keeping ITS original type. If the float `amount` happened to be
the smaller value (the normal case — you can't pay more than what's
due), `amount` stayed a plain float, and the very next line's
`inv["paid_amount"] + amount` crashed exactly like the reported bug, just
on the "Add Payment" screen of an invoice instead of Cash Book. Fixed by
converting `amount` via `to_decimal()` at parse time, before it ever
reaches `min()`.

---

## 6. Helper Functions Added

- `utils/money.to_decimal(value, default="0")`
- `utils/money.normalize_row(row)`
- `utils/money.money(value, places="0.01")` (available for future use;
  not required by the current call sites since `.quantize(Decimal("0.01"))`
  was used directly in the two files where 2-decimal rounding needed to
  stay inline with existing code structure)

## 7. Duplicated Conversion Code Removed

- `_validate_lines()` and `post_journal_entry()` in `ledger_service.py`
  each had their own `round(float(line.get("debit", 0) or 0), 2)`
  one-liner — both now call the same `to_decimal()` helper instead of
  independently re-implementing the None/blank-handling logic.
- No other duplicate money-parsing logic was found elsewhere in the
  repository (see audit scope below) — `ledger_transactions.py`'s 17
  functions were the only sizeable repeated pattern, and all now share
  the one helper.

---

## 8. Audit Scope — What Was Checked and What Was Found Safe As-Is

Searched every `.py` file in the repository for the full list of terms
in the brief (debit, credit, balance, opening/closing balance, cash,
bank, amount, gst, tax, subtotal, discount, payable, receivable,
journal, ledger, invoice, purchase, payment, receipt, expense, stock
value, valuation, running totals) and every arithmetic operator/function
listed (`+ - * / += -= sum() min() max() round()`).

**Confirmed safe, no change needed:**
- `utils/chart_of_accounts.py` — account provisioning only, no money
  arithmetic.
- `modules/saas_business/reports.py`, `dashboard.py` (business-side),
  `gst.py` — all monetary computation happens **inside SQL**
  (`SUM()`, `(selling_price - cost_price)`, etc.), computed by the
  database engine itself, not in Python — server-side SQL arithmetic
  is unaffected by Python's Decimal/float type rules entirely.
- `modules/saas_business/purchase.py` create-purchase flow,
  `billing.py` create-invoice flow — both compute subtotal/tax/discount/
  total entirely from JSON body input (self-contained float arithmetic,
  never mixed with a DB-sourced value mid-calculation), then hand the
  finished totals to `saas_execute()` (fine — both drivers accept a
  plain float bound to a NUMERIC/REAL column on write) and to
  `record_sale()`/`record_purchase()` (now Decimal-safe per §5). No
  crash risk found here; left as-is as the safest choice, since
  converting a working, self-contained 130-line calculation block to
  Decimal purely for "purity" carries more risk (subtly different
  rounding at intermediate steps) than benefit.
- `modules/saas_business/suppliers.py`, `finance.py` (expenses),
  `purchase.py cancel()` — running-balance updates (`balance - {p}`,
  `due_amount - {p}`) are written as **SQL `CASE WHEN` expressions**,
  with the Python value only ever bound as a parameter, never added to
  a DB value in Python — safe by construction.
- `modules/saas_business/products.py`, `customers.py` — form-parsed
  prices/rates are stored directly, never combined with a DB Decimal in
  Python.
- `utils/tax_helpers.py` (`calculate_gst`) — pure function, no DB
  access, self-contained float math, never mixed with a Decimal.

**Systematic pattern search** (not just the files above) for direct
Python-level arithmetic between a DB dict field and another operand —
covering both `row["field"] <op>` and `<op> row["field"]` orderings —
found exactly the two sites fixed in §5
(`ledger_service._update_account_balance` and `billing.add_payment`)
and no others.

---

## 9. Testing Performed

- `python3 -m py_compile` on all 6 changed files — passes.
- Full Flask app creation (`create_app()`) against a fresh SQLite DB —
  succeeds, all schemas initialize, 117 routes register with no import
  errors (confirms no circular-import issue from the new
  `utils/money.py` ↔ `models/saas_auth.py` wiring).
- **Isolated reproduction of the exact reported error**: confirmed
  `Decimal("500.00") + 123.45` raises the identical
  `TypeError: unsupported operand type(s) for +: 'decimal.Decimal' and 'float'`
  with the old code's types, and that `to_decimal()`/`normalize_row()`
  resolve it.
- **Real end-to-end functional test** through the actual production
  code path — `record_adjustment()` → `post_journal_entry()` →
  `_update_account_balance()` — posting two sequential cash adjustments
  against a live SQLite-backed test business (the second call
  specifically exercises the UPDATE branch that reads back an existing
  Decimal-normalized balance and adds a new amount to it — the exact
  operation that crashed). Result: no exception, balance correctly
  accumulates to `623.45` as a `Decimal`.
- Not tested against a live PostgreSQL instance in this environment (no
  network access here) — the normalization logic is symmetric by
  design (Postgres already returns Decimal natively; SQLite is made to
  match it), and the SQLite test above exercises the identical code
  paths. Recommend a staging smoke test on Render: add a cash receipt,
  add a bank entry, record an invoice payment, record a supplier
  payment, twice in a row each (to exercise both the INSERT and UPDATE
  branches of the balance cache).

## 10. Remaining Risks

- **Type hints** in `ledger_transactions.py`'s function signatures
  (`amount: float`) are now inaccurate — they don't affect runtime
  behavior (Python doesn't enforce type hints), only documentation
  accuracy. Left unchanged to keep this diff scoped to the actual bug;
  flagging in case your team wants a follow-up docs-only pass.
- **New code going forward**: any future route that reads a money value
  via `saas_fetchone`/`saas_fetchall` (safe automatically) but then
  parses a NEW form value with `float(...)` instead of `to_decimal(...)`
  before combining the two will reintroduce this exact bug. There's no
  way to make Python's `float()` builtin itself safe — this is a
  convention that has to be followed by whoever writes the next money
  route. Worth a one-line mention in your team's PR checklist.
- Genuinely not tested against live PostgreSQL in this environment —
  see §9. The fix is symmetric and should behave identically, but a
  staging smoke test before relying on it in production is warranted
  given the financial nature of this code.

---

## Confirmation

All monetary arithmetic in the ledger engine (`ledger_service.py`,
`ledger_transactions.py`), the Cash Book / Cash Receipt flow
(`accounts.py`), and invoice payments (`billing.py`) now operates on
`Decimal` values exclusively, sourced identically from either database
backend via the centralized `saas_fetchone`/`saas_fetchall` +
`normalize_row()` path, with form/API input normalized via the same
`to_decimal()` helper at every point it enters a calculation. SQLite
(development) and PostgreSQL (production) now produce identical types,
and therefore identical arithmetic behavior, for every money value in
the app. No business logic, GST calculation, accounting rule, route, or
template was altered — only the numeric type each value is represented
in along the way.
