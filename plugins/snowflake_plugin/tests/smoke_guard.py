#!/usr/bin/env python3
"""Read-only statement guard smoke tests for snowflake_plugin.

Hermetic — pure functions, no connection needed. Red-first: every check
asserts REAL behavior of the leader classification. This guard is FAST-FAIL
ONLY (§1 of the KB overview) — it is not the write boundary (there is no
Snowflake session-level read-only flag); the true boundary is the
operator-granted read-only role, which cannot be exercised hermetically.

Exercises:
  1. Read leaders (SELECT/SHOW/DESCRIBE/DESC/EXPLAIN/WITH) all pass
  2. Write/DDL leaders (INSERT/UPDATE/DELETE/MERGE/CREATE incl. TEMPORARY,
     PUT/GET/COPY/CALL/USE) all refuse with snowflake.read_only_violation
  3. Comment-led input is refused outright (fail loud, no stripping)
  4. A leading parenthesis is tolerated ("(SELECT ...)")
  5. Empty / whitespace-only input refused with snowflake.invalid_params

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/snowflake_plugin/tests/smoke_guard.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "snowflake_plugin" / "src"))

from snowflake_plugin.statement_guard import (  # noqa: E402
    StatementGuardError,
    assert_read_statement,
    leading_keyword,
)

_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _refused(sql: str) -> str:
    try:
        assert_read_statement(sql)
    except StatementGuardError as exc:
        return exc.code
    return ""


def test_read_leaders_pass() -> None:
    for sql in (
        "SELECT * FROM t",
        "SHOW TABLES",
        "DESCRIBE TABLE t",
        "DESC TABLE t",
        "EXPLAIN SELECT * FROM t",
        "WITH cte AS (SELECT 1) SELECT * FROM cte",
    ):
        code = _refused(sql)
        _assert(f"read leader admitted: {sql.split()[0]}", code == "", code)


def test_write_leaders_refused() -> None:
    for sql in (
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.x = s.x",
        "CREATE TABLE t (id INT)",
        "CREATE TEMPORARY TABLE t (id INT)",
        "PUT file://x @stage",
        "GET @stage file://x",
        "COPY INTO t FROM @stage",
        "CALL my_proc()",
        "USE DATABASE d",
    ):
        code = _refused(sql)
        _assert(
            f"write/DDL leader refused: {sql.split()[0]}",
            code == "snowflake.read_only_violation",
            code,
        )


def test_comment_led_refused() -> None:
    code = _refused("-- comment\nSELECT 1")
    _assert("comment-led input refused (invalid_params)", code == "snowflake.invalid_params", code)
    code = _refused("/* comment */ SELECT 1")
    _assert("block-comment-led input refused (invalid_params)", code == "snowflake.invalid_params", code)


def test_leading_parenthesis_tolerated() -> None:
    code = _refused("(SELECT 1)")
    _assert("parenthesized SELECT admitted", code == "", code)
    _assert("leading_keyword unwraps parenthesis", leading_keyword("(SELECT 1)") == "SELECT")


def test_empty_input_refused() -> None:
    for sql in ("", "   "):
        code = _refused(sql)
        _assert(f"empty input {sql!r} refused (invalid_params)", code == "snowflake.invalid_params", code)


def main() -> int:
    print("\nsnowflake_plugin statement-guard smoke tests")
    print("=" * 47)
    test_read_leaders_pass()
    test_write_leaders_refused()
    test_comment_led_refused()
    test_leading_parenthesis_tolerated()
    test_empty_input_refused()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All statement-guard smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
