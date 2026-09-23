"""Read-only statement guard — FAST-FAIL ONLY, not the write boundary.

Unlike external_postgres_plugin's guard (a belt alongside the LOAD-BEARING
``conn.read_only`` connection characteristic), Snowflake has no session-level
read-only flag. This guard is the ONLY connector-side line: it cannot, by
itself, stop an over-privileged role from writing. The TRUE developer-proof
boundary is the read-only ROLE the connection is pinned to (GRANT
SELECT/USAGE only — no INSERT/UPDATE/DELETE/MERGE/DDL); the account
connection runbook (knowledge_base/01_snowflake_overview.md) treats that
role grant as MANDATORY, not optional.

Single-statement is NATIVE (Snowflake's ``MULTI_STATEMENT_COUNT`` defaults to
1, and this plugin never calls ``execute_string``) — no statement-splitting
parser is needed here.

This module contains NO SQL string literals and imports NO database driver,
so it stays FULLY GATED (no allowlist entry) — the same honesty condition as
external_postgres_plugin's statement_guard.py.
"""

from __future__ import annotations

import re

from .constants import ERROR_INVALID_PARAMS, ERROR_READ_ONLY_VIOLATION, READ_LEADERS


class StatementGuardError(RuntimeError):
    """Raised when a statement is refused by the read-only leader guard."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def assert_read_statement(sql: str) -> None:
    """Refuse a statement whose leading keyword is not a permitted read leader.

    Comment-led input is refused outright (fail loud, no comment-stripping
    parser) — matching the rev-D near-code spec.
    """
    stripped = sql.lstrip()
    if stripped.startswith("--") or stripped.startswith("/*"):
        raise StatementGuardError(
            ERROR_INVALID_PARAMS, "strip comments before submitting sql"
        )
    if not stripped:
        raise StatementGuardError(ERROR_INVALID_PARAMS, "no SQL statement was provided")
    leader = leading_keyword(sql)
    if leader not in READ_LEADERS:
        raise StatementGuardError(
            ERROR_READ_ONLY_VIOLATION,
            f"statement leader '{leader or '?'}' is not a permitted read statement; "
            "this connector is read-only",
        )


def leading_keyword(sql: str) -> str:
    """The upper-cased leading SQL keyword, tolerating a leading parenthesis."""
    head = sql.lstrip().lstrip("(").strip()
    match = re.match(r"[A-Za-z_]+", head)
    return match.group(0).upper() if match else ""
