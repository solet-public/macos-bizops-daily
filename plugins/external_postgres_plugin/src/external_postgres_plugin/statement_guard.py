"""Read-only statement guards — BELT-tier, defense-in-depth only.

These are NOT the write boundary. The LOAD-BEARING write-stopper is the psycopg3
connection read-only characteristic (``conn.read_only = True``, connection.py),
which fails any write at SQLSTATE 25006 regardless of what slips past here. These
guards are fast-fail UX + result-shape hygiene:

- :func:`assert_single_statement` — exactly one top-level statement per call
  (bounds ``SET``-injection + multi-result), via a REAL SQL parser (``sqlparse``,
  BSD; operator-ratified rev-F-final over pglast/GPL). Worst case if a smuggled
  second statement slips sqlparse: the only useful append is a ``SET`` the
  read-only session tolerates (belt-tier DoS at worst — not a write, not an
  escape; fresh-connection-per-call + internal-only reachability bound it).
- :func:`assert_read_statement` — the leading keyword must be in the Datagrip
  read/introspection family ``{SELECT, WITH, EXPLAIN, SHOW, VALUES, TABLE}``.
  ``EXPLAIN ANALYZE <write>`` is admitted here but still fails at the read-only
  connection layer (25006), so the belt need not model it.

This module contains NO SQL string literals and imports NO database driver, so
it stays FULLY GATED (no allowlist entry) — the §3 honesty condition. Folding
these guards into the S2-exempt ``query_actions.py`` would hide them inside the
exemption.
"""

from __future__ import annotations

import re

import sqlparse

from .constants import (
    ERROR_INVALID_PARAMS,
    ERROR_READ_ONLY_VIOLATION,
    READ_LEADERS,
)


class StatementGuardError(RuntimeError):
    """Raised when a statement is refused by a belt-tier guard."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def assert_single_statement(sql: str) -> None:
    """Refuse anything other than exactly one non-empty top-level statement.

    ``sqlparse`` is dollar-quote / string / comment aware — a ``;`` inside a
    string, dollar-quoted body, or comment does NOT split the statement.
    Whitespace/comment-only fragments (no meaningful first token) are dropped
    before counting.
    """
    statements = [
        stmt for stmt in sqlparse.parse(sql) if stmt.token_first(skip_cm=True) is not None
    ]
    if not statements:
        raise StatementGuardError(ERROR_INVALID_PARAMS, "no SQL statement was provided")
    if len(statements) > 1:
        raise StatementGuardError(
            ERROR_INVALID_PARAMS,
            "only a single SQL statement is permitted per call",
        )


def assert_read_statement(sql: str) -> None:
    """Refuse a statement whose leading keyword is not a permitted read leader.

    Belt-tier shape hygiene only — ``WITH`` is admitted because a CTE query is the
    common read shape, NOT because a ``WITH`` leader is provably read-only. A
    data-modifying CTE DOES lead with ``WITH``
    (``WITH t AS (DELETE FROM … RETURNING …) SELECT …``), so this guard cannot and
    does not decide read-vs-write. The LOAD-BEARING write boundary is the read-only
    connection: any write, including one hidden in a leading-``WITH`` CTE, is
    refused server-side at SQLSTATE 25006 regardless of what this guard admits.
    """
    leader = leading_keyword(sql)
    if leader not in READ_LEADERS:
        raise StatementGuardError(
            ERROR_READ_ONLY_VIOLATION,
            f"statement leader '{leader or '?'}' is not a permitted read statement; "
            "this connection is read-only",
        )


def leading_keyword(sql: str) -> str:
    """The upper-cased leading SQL keyword, tolerating a leading parenthesis.

    Comments and leading whitespace are skipped (``skip_cm=True``) so the leader
    reflects the real statement, not a comment. A parenthesized leader
    (``(SELECT …)`` / ``(VALUES …)``) is unwrapped.
    """
    parsed = sqlparse.parse(sql)
    if not parsed:
        return ""
    token = parsed[0].token_first(skip_cm=True)
    if token is None:
        return ""
    # Tolerate a leading parenthesis ("(SELECT …)") and a no-space row
    # constructor ("VALUES(1)") — take the leading run of word characters.
    head = token.value.lstrip("(").strip()
    match = re.match(r"[A-Za-z_]+", head)
    return match.group(0).upper() if match else ""
