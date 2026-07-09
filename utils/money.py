"""
utils/money.py — Shared monetary-value helpers
================================================
Root cause this module exists to fix:

PostgreSQL's NUMERIC columns come back from psycopg2 as Python
`decimal.Decimal` objects. SQLite's REAL columns come back from sqlite3
as plain `float`. Every other piece of the app — form parsing, GST math,
running totals — was written assuming plain floats, which is exactly
what SQLite gave it in development. The instant the same code ran
against PostgreSQL in production, any arithmetic that mixed a
DB-sourced Decimal with an application-side float raised:

    TypeError: unsupported operand type(s) for +: 'decimal.Decimal' and 'float'

Python deliberately does NOT allow implicit Decimal/float arithmetic
(unlike Decimal/int, which works fine) because floats can't exactly
represent most decimal fractions — silently mixing them would
reintroduce the rounding errors Decimal exists to avoid. So this isn't
a quirk to route around; it's Python correctly refusing to guess.

The fix: standardize on `Decimal` as the ONE money type everywhere in
the app (DB reads, form input, and every calculation in between), via
the helpers below, so the two types never meet. Use `to_decimal()` at
every point a monetary value enters Python (a form field, a DB row) and
plain +, -, *, / after that point works exactly like it always did.
"""

from decimal import Decimal, InvalidOperation


def to_decimal(value, default="0"):
    """
    Safely convert any incoming value (None, "", int, float, str, or
    already a Decimal) to a Decimal. This is the ONE place in the app
    that should ever do this conversion — every money-parsing call site
    (form input, DB rows from a raw cursor) should call this instead of
    float() or a one-off Decimal(...) call.

    Floats are converted via str(value) rather than Decimal(value)
    directly — Decimal(0.1) gives a long ugly binary-float artifact
    (0.1000000000000000055511151231257827021181583404541015625),
    while Decimal(str(0.1)) gives the clean Decimal('0.1') a human
    (and psycopg2) would expect. Since the float itself already lost
    whatever true precision existed before it got here, this loses
    nothing further.
    """
    if value is None or value == "":
        value = default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def normalize_row(row):
    """
    Convert every plain `float` value in a DB row dict to Decimal,
    leaving everything else (str, int, bool, None, datetime, and any
    value that's already Decimal) untouched.

    Used centrally by saas_fetchone()/saas_fetchall() so that, on
    SQLite, REAL columns (which sqlite3 returns as float) come back
    exactly as Decimal — the same type PostgreSQL's psycopg2 already
    hands back natively for NUMERIC columns. After this, every DB read
    is Decimal on both backends, so callers never have to know or care
    which database is running underneath.
    """
    if row is None:
        return None
    for key, value in row.items():
        if isinstance(value, float):
            row[key] = Decimal(str(value))
    return row


def money(value, places="0.01"):
    """
    Round a Decimal (or anything to_decimal() can parse) to 2 decimal
    places using standard rounding — the Decimal equivalent of the
    round(x, 2) calls used throughout the money code, but operating
    entirely in Decimal so it never reintroduces a float.
    """
    return to_decimal(value).quantize(Decimal(places))
