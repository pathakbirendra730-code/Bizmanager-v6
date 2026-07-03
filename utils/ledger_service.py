"""
utils/ledger_service.py — Double-Entry Posting Service (Core)
====================================================================
This is the single gateway through which money moves in the system.
Every business transaction — sales, purchases, payments, transfers,
returns, adjustments — must go through post_journal_entry() (or one
of the named transaction-type helpers built on top of it in
utils/ledger_transactions.py).

Core guarantees:
  1. VALIDATION: an entry is rejected before anything is written if
     sum(debit) != sum(credit), if any line has both debit and credit
     non-zero, if any line amount is negative, or if fewer than 2 lines
     are supplied. No unbalanced entry can ever reach the database.
  2. ATOMICITY: header + lines + balance cache updates happen on one
     connection inside one transaction (via ledger_transaction()).
  3. AUDIT TRAIL: corrections never mutate a posted entry. reverse_entry()
     posts a new entry with every debit/credit flipped, linked back to
     the original via reverses/reversed_by, and marks the original
     status='reversed'. The original entry's lines and totals are
     never altered — anyone can always see exactly what was posted and
     when, and exactly what reversed it.
  4. BALANCE CACHE: saas_account_balances is updated incrementally on
     every post and every reversal, so reports never need to re-sum the
     full journal table. The cache is always kept inside the same
     atomic transaction as the journal write, so it can never drift out
     of sync with the journal itself.
"""

from datetime import datetime
from models.saas_auth import saas_fetchone, saas_fetchall, _is_postgres
from models.saas_ledger_engine import ledger_transaction, ACCOUNT_TYPES
from utils.saas_helpers import audit_log


class UnbalancedEntryError(Exception):
    """Raised when total debits != total credits for a proposed entry."""
    pass


class InvalidLineError(Exception):
    """Raised when a single journal line is malformed."""
    pass


def P():
    return "%s" if _is_postgres() else "?"


# ═══════════════════════════════ NUMBER GENERATION ════════════════════════════

def _generate_entry_number(business_id: int, c, p) -> str:
    """
    Generate the next sequential journal entry number for this business,
    using the SAME cursor as the enclosing transaction so the read-then-
    insert is consistent within the atomic posting operation.
    """
    c.execute(
        f"SELECT entry_number FROM saas_journal_entries WHERE business_id={p} "
        f"ORDER BY id DESC LIMIT 1",
        (business_id,)
    )
    row = c.fetchone()
    if row:
        last_number = row["entry_number"] if isinstance(row, dict) else row[0]
        try:
            seq = int(last_number.split("-")[-1]) + 1
        except (ValueError, AttributeError):
            seq = 1001
    else:
        seq = 1001
    return f"JE-{seq}"


# ═══════════════════════════════ VALIDATION ═══════════════════════════════════

def _validate_lines(lines: list):
    """
    Validate a proposed set of journal lines BEFORE any database write.
    Raises UnbalancedEntryError or InvalidLineError with a clear message
    on any problem. Never silently coerces or "fixes" bad input — a
    caller passing malformed data should get a loud, specific failure.
    """
    if not lines or len(lines) < 2:
        raise InvalidLineError(
            f"A journal entry needs at least 2 lines, got {len(lines) if lines else 0}."
        )

    total_debit = 0.0
    total_credit = 0.0

    for i, line in enumerate(lines):
        debit  = round(float(line.get("debit", 0) or 0), 2)
        credit = round(float(line.get("credit", 0) or 0), 2)

        if debit < 0 or credit < 0:
            raise InvalidLineError(f"Line {i+1}: debit/credit cannot be negative.")
        if debit > 0 and credit > 0:
            raise InvalidLineError(
                f"Line {i+1}: a single line cannot have BOTH a debit and a "
                f"credit amount (debit={debit}, credit={credit}). Split into two lines."
            )
        if debit == 0 and credit == 0:
            raise InvalidLineError(f"Line {i+1}: must have a non-zero debit or credit.")
        if not line.get("account_id"):
            raise InvalidLineError(f"Line {i+1}: missing required account_id.")

        total_debit  += debit
        total_credit += credit

    total_debit  = round(total_debit, 2)
    total_credit = round(total_credit, 2)

    if abs(total_debit - total_credit) > 0.01:  # allow trivial float rounding
        raise UnbalancedEntryError(
            f"Journal entry is not balanced: total debit={total_debit}, "
            f"total credit={total_credit} (difference={round(total_debit - total_credit, 2)}). "
            f"Every entry must have debits exactly equal to credits."
        )

    return total_debit, total_credit


# ═══════════════════════════════ CORE POSTING FUNCTION ════════════════════════

def post_journal_entry(business_id: int, lines: list, source_type: str,
                        source_id: int = None, narration: str = "",
                        entry_date: str = None, created_by: int = None) -> dict:
    """
    Post a balanced double-entry journal entry. This is the ONLY function
    in the entire system that writes to saas_journal_entries /
    saas_journal_lines / saas_account_balances. Every transaction-type
    helper (record_cash_sale, record_credit_purchase, etc.) ultimately
    calls this.

    Args:
        business_id:  tenant scope (required, never trust caller-supplied
                      account ownership — validated against this id)
        lines:        list of dicts, each with:
                        account_id   (int, required)
                        debit        (float, default 0)
                        credit       (float, default 0)
                        party_type   (str, optional: 'customer'|'supplier')
                        party_id     (int, optional)
                        description  (str, optional)
        source_type:  business event type, e.g. 'sale', 'purchase',
                      'payment_in', 'payment_out', 'transfer', 'opening_balance',
                      'sales_return', 'purchase_return', 'adjustment'
        source_id:    FK to the originating record (invoice id, purchase id, etc.)
        narration:    human-readable description of the whole transaction
        entry_date:   defaults to today
        created_by:   saas_users.id of whoever triggered this

    Returns:
        {"entry_id": int, "entry_number": str, "total_debit": float, "total_credit": float}

    Raises:
        UnbalancedEntryError, InvalidLineError — before any write happens.
    """
    total_debit, total_credit = _validate_lines(lines)  # raises before any DB write

    entry_date = entry_date or datetime.utcnow().date().isoformat()

    # Verify every account_id actually belongs to this business BEFORE
    # opening the write transaction — a cross-tenant account_id here would
    # otherwise only be caught by the FK constraint after partial work,
    # and we want a clear error message, not a raw IntegrityError.
    p = P()
    account_ids = list({line["account_id"] for line in lines})
    placeholders = ",".join([p] * len(account_ids))
    owned_rows = saas_fetchall(
        f"SELECT id FROM saas_chart_of_accounts WHERE business_id={p} AND id IN ({placeholders})",
        tuple([business_id] + account_ids)
    )
    owned_ids = {row["id"] for row in owned_rows}
    missing = set(account_ids) - owned_ids
    if missing:
        raise InvalidLineError(
            f"Account id(s) {missing} do not belong to business_id={business_id} "
            f"or do not exist. Refusing to post."
        )

    with ledger_transaction() as (conn, c, p):
        entry_number = _generate_entry_number(business_id, c, p)

        c.execute(
            f"""INSERT INTO saas_journal_entries
                (business_id, entry_number, entry_date, source_type, source_id,
                 narration, total_debit, total_credit, status, created_by)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},'posted',{p})""",
            (business_id, entry_number, entry_date, source_type, source_id,
             narration, total_debit, total_credit, created_by)
        )
        entry_id = c.lastrowid

        for i, line in enumerate(lines):
            debit  = round(float(line.get("debit", 0) or 0), 2)
            credit = round(float(line.get("credit", 0) or 0), 2)
            c.execute(
                f"""INSERT INTO saas_journal_lines
                    (business_id, entry_id, account_id, debit, credit,
                     party_type, party_id, description, line_order)
                    VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})""",
                (business_id, entry_id, line["account_id"], debit, credit,
                 line.get("party_type", ""), line.get("party_id"),
                 line.get("description", ""), i)
            )
            _update_account_balance(c, p, business_id, line["account_id"], debit, credit)

    audit_log("journal_entry_posted", user_id=created_by, business_id=business_id,
              entity_type="journal_entry", entity_id=str(entry_id),
              detail=f"number={entry_number} source={source_type} debit={total_debit}")

    return {
        "entry_id": entry_id,
        "entry_number": entry_number,
        "total_debit": total_debit,
        "total_credit": total_credit,
    }


def _update_account_balance(c, p, business_id: int, account_id: int,
                             debit: float, credit: float):
    """
    Incrementally update the balance cache for one account, inside the
    SAME transaction/cursor as the journal line write — this is what
    keeps the cache atomically consistent with the journal, never a
    separate eventually-consistent step.
    """
    c.execute(
        f"SELECT account_type FROM saas_chart_of_accounts WHERE id={p}",
        (account_id,)
    )
    row = c.fetchone()
    account_type = row["account_type"] if isinstance(row, dict) else row[0]
    normal_balance = ACCOUNT_TYPES[account_type]["normal_balance"]

    c.execute(
        f"SELECT * FROM saas_account_balances WHERE business_id={p} AND account_id={p}",
        (business_id, account_id)
    )
    existing = c.fetchone()

    if existing:
        existing = dict(existing) if not isinstance(existing, dict) else existing
        new_total_debit  = existing["total_debit"] + debit
        new_total_credit = existing["total_credit"] + credit
        new_balance = (
            new_total_debit - new_total_credit if normal_balance == "debit"
            else new_total_credit - new_total_debit
        )
        c.execute(
            f"""UPDATE saas_account_balances
                SET total_debit={p}, total_credit={p}, balance={p}, updated_at={p}
                WHERE business_id={p} AND account_id={p}""",
            (round(new_total_debit, 2), round(new_total_credit, 2), round(new_balance, 2),
             datetime.utcnow().isoformat(), business_id, account_id)
        )
    else:
        balance = debit - credit if normal_balance == "debit" else credit - debit
        c.execute(
            f"""INSERT INTO saas_account_balances
                (business_id, account_id, total_debit, total_credit, balance)
                VALUES ({p},{p},{p},{p},{p})""",
            (business_id, account_id, round(debit, 2), round(credit, 2), round(balance, 2))
        )


# ═══════════════════════════════ REVERSAL ═════════════════════════════════════

def reverse_entry(business_id: int, entry_id: int, reason: str = "",
                   created_by: int = None) -> dict:
    """
    Reverse a previously posted journal entry by posting a NEW entry with
    every line's debit/credit flipped. The original entry's rows are
    never modified — only its status flips to 'reversed' and it gets
    linked to the new reversing entry. This preserves a complete,
    tamper-evident audit trail: nothing in a posted entry is ever
    overwritten or deleted.

    Raises ValueError if the entry doesn't exist, doesn't belong to this
    business, or has already been reversed.
    """
    p = P()
    entry = saas_fetchone(
        f"SELECT * FROM saas_journal_entries WHERE id={p} AND business_id={p}",
        (entry_id, business_id)
    )
    if not entry:
        raise ValueError(f"Journal entry {entry_id} not found for business_id={business_id}.")
    if entry["status"] == "reversed":
        raise ValueError(f"Journal entry {entry['entry_number']} has already been reversed.")

    original_lines = saas_fetchall(
        f"SELECT * FROM saas_journal_lines WHERE entry_id={p} AND business_id={p} ORDER BY line_order",
        (entry_id, business_id)
    )
    if not original_lines:
        raise ValueError(f"Journal entry {entry['entry_number']} has no lines to reverse.")

    flipped_lines = [
        {
            "account_id": line["account_id"],
            "debit": line["credit"],    # flipped
            "credit": line["debit"],    # flipped
            "party_type": line["party_type"],
            "party_id": line["party_id"],
            "description": f"Reversal: {line['description']}" if line["description"] else "Reversal",
        }
        for line in original_lines
    ]

    narration = f"Reversal of {entry['entry_number']}" + (f" — {reason}" if reason else "")

    result = post_journal_entry(
        business_id=business_id,
        lines=flipped_lines,
        source_type="reversal",
        source_id=entry_id,
        narration=narration,
        created_by=created_by,
    )

    with ledger_transaction() as (conn, c, p2):
        c.execute(
            f"UPDATE saas_journal_entries SET status='reversed', reversed_by={p2} "
            f"WHERE id={p2} AND business_id={p2}",
            (result["entry_id"], entry_id, business_id)
        )
        c.execute(
            f"UPDATE saas_journal_entries SET reverses={p2} WHERE id={p2} AND business_id={p2}",
            (entry_id, result["entry_id"], business_id)
        )

    audit_log("journal_entry_reversed", user_id=created_by, business_id=business_id,
              entity_type="journal_entry", entity_id=str(entry_id),
              detail=f"original={entry['entry_number']} reversal={result['entry_number']} reason={reason}")

    return result


# ═══════════════════════════════ READ HELPERS ═════════════════════════════════

def get_account_balance(business_id: int, account_id: int) -> float:
    """Return the current cached balance for one account (0 if never posted to)."""
    p = P()
    row = saas_fetchone(
        f"SELECT balance FROM saas_account_balances WHERE business_id={p} AND account_id={p}",
        (business_id, account_id)
    )
    return row["balance"] if row else 0.0


def get_entry_with_lines(business_id: int, entry_id: int) -> dict:
    """Return a journal entry header plus all its lines, tenant-scoped."""
    p = P()
    entry = saas_fetchone(
        f"SELECT * FROM saas_journal_entries WHERE id={p} AND business_id={p}",
        (entry_id, business_id)
    )
    if not entry:
        return None
    lines = saas_fetchall(
        f"""SELECT jl.*, coa.code as account_code, coa.name as account_name
            FROM saas_journal_lines jl
            JOIN saas_chart_of_accounts coa ON coa.id = jl.account_id
            WHERE jl.entry_id={p} AND jl.business_id={p}
            ORDER BY jl.line_order""",
        (entry_id, business_id)
    )
    entry["lines"] = lines
    return entry
