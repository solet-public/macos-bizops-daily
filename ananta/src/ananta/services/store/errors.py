"""Normalized error types raised by every :class:`Store` backend.

Callers catch these abstraction-level errors regardless of which backend
raised them.  Each backend translates its native error type
(``psycopg.errors.UniqueViolation`` for Postgres, in-memory bookkeeping
for the dict backend) into the matching :class:`StoreError` subclass so
no consumer code needs to branch on backend identity.
"""

from __future__ import annotations


class StoreError(Exception):
    """Base class for every error raised by a :class:`Store` backend."""


class UniqueViolationError(StoreError):
    """A write violated a ``unique=True`` column constraint.

    Carries the offending ``column`` and ``value`` so callers can build
    actionable error messages without parsing the message text.  Both
    backends raise this with the same shape, including the
    soft-deleted-rows case (Postgres ``UNIQUE`` indexes ignore
    ``is_deleted``; the in-memory backend matches that semantics).
    """

    def __init__(self, column: str, value: object, *, table: str) -> None:
        self.column = column
        self.value = value
        self.table = table
        super().__init__(
            f"unique constraint violated: {table}.{column} = {value!r}",
        )


class NotNullViolationError(StoreError):
    """A write omitted a ``not_null=True`` column with no default."""

    def __init__(self, column: str, *, table: str) -> None:
        self.column = column
        self.table = table
        super().__init__(f"not-null constraint violated: {table}.{column}")


class EmptyUpdateError(StoreError):
    """``update()`` was called with an empty ``updates`` dict.

    Callers that want to bump activity-only must use :meth:`Store.touch`
    instead.  Raised before the backend is reached so neither path can
    issue an invalid empty-SET ``UPDATE`` statement.
    """


__all__ = [
    "EmptyUpdateError",
    "NotNullViolationError",
    "StoreError",
    "UniqueViolationError",
]
