"""Timestamp coercion for ordering, cursor, and range comparisons.

Shared core-domain helper for any code that migrated a raw ``ORDER BY
created_at`` / ``(created_at, id)`` cursor / ``created_at < X`` range off SQL
onto the state primitives and now compares timestamps in Python. Such
comparisons MUST compare timestamp VALUES, never ISO-8601 SPELLINGS: the state
primitives surface ``created_at`` as an ISO string (``provider._serialize_for_json``
→ ``isoformat``), and two equal instants can carry different spellings —
``'…T00:00:00'`` vs ``'…T00:00:00.000000'``, or a tz-aware cursor value vs a naive
cell. A lexical string compare treats those as unequal and silently drops
equal-instant rows at a page boundary (Codex MAJOR, 2026-06-21, context_management
Slice-1). Coerce both sides to one naive-UTC datetime VALUE first; fail loud on
anything unparseable — never fall back to lexical.
"""

from datetime import UTC, datetime


def to_naive_utc(value: object) -> datetime:
    """Coerce an ISO-8601 string or ``datetime`` timestamp cell to a naive-UTC ``datetime``.

    A naive cell is taken as UTC (the F1 TZ-storage seam); an aware cell is
    converted to UTC then stripped to naive. Fail fast on any other type or an
    unparseable string — a non-timestamp here is an upstream contract violation,
    not something to silently coerce (``datetime.fromisoformat`` raises
    ``ValueError`` on a malformed string; there is deliberately no lexical
    fallback).
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        raise TypeError(
            "expected a datetime or ISO-8601 string timestamp, got "
            f"{type(value).__name__!r}",
        )
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt
