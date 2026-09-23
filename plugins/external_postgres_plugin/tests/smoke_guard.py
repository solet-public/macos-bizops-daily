#!/usr/bin/env python3
"""Statement-guard smoke tests (belt-tier) for external_postgres_plugin.

Hermetic — no Postgres. Covers the read-leader guard + single-statement parser
so the gate has real teeth on the guard logic even when the live scratch DB is
absent (the load-bearing read-only proof lives in smoke_readonly.py).

RED-FIRST: the write-leader-REFUSED and multi-statement-REFUSED assertions are
the teeth. Neutralizing ``assert_read_statement`` / ``assert_single_statement``
to no-ops flips them to FAIL. The read-leader-ALLOWED assertions guard the
opposite error (a guard so tight it rejects a legitimate Datagrip read).

Exercises:
  1. read leaders {SELECT, WITH, EXPLAIN, SHOW, VALUES, TABLE} ALLOWED
  2. write leaders {INSERT, UPDATE, DELETE, MERGE, CREATE, CREATE TEMP, DROP,
     ALTER, TRUNCATE, GRANT} REFUSED (read_only_violation)
  3. single statement OK; two statements REFUSED
  4. a ';' inside a string does NOT split (sqlparse is string-aware)
  5. empty / whitespace / comment-only REFUSED
  6. comment-led text sees the REAL leader (a commented-out write is single+read)
  7. EXPLAIN ANALYZE <write> is ADMITTED by the belt (documented: the read-only
     connection is the real write-stopper at 25006)

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/external_postgres_plugin/tests/smoke_guard.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin.statement_guard import (  # noqa: E402
    StatementGuardError,
    assert_read_statement,
    assert_single_statement,
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


def _read_allowed(sql: str) -> bool:
    try:
        assert_read_statement(sql)
        return True
    except StatementGuardError:
        return False


def _refused_code(sql: str) -> str:
    try:
        assert_read_statement(sql)
        return ""
    except StatementGuardError as exc:
        return exc.code


def _single_ok(sql: str) -> bool:
    try:
        assert_single_statement(sql)
        return True
    except StatementGuardError:
        return False


def test_read_leaders_allowed() -> None:
    for sql in (
        "SELECT * FROM orders",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "EXPLAIN SELECT 1",
        "SHOW transaction_read_only",
        "VALUES (1, 2)",
        "TABLE orders",
        "  select 1",  # leading whitespace + lower-case
        "(SELECT 1)",  # parenthesized
    ):
        _assert(f"read leader allowed: {sql!r}", _read_allowed(sql), sql)


def test_write_leaders_refused() -> None:
    for sql in (
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "MERGE INTO t USING s ON (t.id = s.id)",
        "CREATE TABLE t (a int)",
        "CREATE TEMP TABLE t (a int)",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN b int",
        "TRUNCATE t",
        "GRANT SELECT ON t TO r",
        "COPY orders TO STDOUT",              # COPY refused (§2.1/§8.5) — not a read leader
        "COPY orders FROM '/etc/passwd'",     # server-side file read refused
    ):
        _assert(
            f"write leader REFUSED: {sql!r}",
            _refused_code(sql) == "external_pg.read_only_violation",
            sql,
        )


def test_single_vs_multi() -> None:
    _assert("single statement OK", _single_ok("SELECT * FROM t WHERE id = 1"))
    _assert("two statements REFUSED", not _single_ok("SELECT 1; DELETE FROM t"))
    _assert("trailing semicolon OK", _single_ok("SELECT 1;"))


def test_semicolon_in_string_is_single() -> None:
    _assert("';' inside a string does not split", _single_ok("SELECT ';' AS semi, id FROM t"))


def test_empty_refused() -> None:
    _assert("empty string REFUSED as multi/empty", not _single_ok("   "))
    _assert("comment-only REFUSED as empty", not _single_ok("-- just a comment"))


def test_comment_led_sees_real_leader() -> None:
    # A leading comment does not hide the real statement — a commented header on
    # a write still refuses at the read-leader guard.
    _assert(
        "comment-led DELETE still REFUSED",
        _refused_code("-- report\nDELETE FROM t") == "external_pg.read_only_violation",
    )
    # And the commented-out write is a SINGLE read statement.
    _assert("commented-out write is single+read", _single_ok("SELECT 1 -- ; DELETE FROM t"))
    _assert("commented-out write reads as SELECT", _read_allowed("SELECT 1 -- ; DELETE FROM t"))


def test_explain_analyze_write_admitted_by_belt() -> None:
    # The belt admits EXPLAIN ANALYZE <write> (leader EXPLAIN) — the read-only
    # CONNECTION refuses the actual write at SQLSTATE 25006 (smoke_readonly.py).
    # This documents the belt-vs-boundary split explicitly.
    _assert(
        "EXPLAIN ANALYZE <write> admitted by the belt (boundary is conn.read_only)",
        _read_allowed("EXPLAIN ANALYZE INSERT INTO t VALUES (1)"),
    )


def main() -> int:
    print("\nexternal_postgres_plugin statement-guard smoke tests")
    print("=" * 52)
    test_read_leaders_allowed()
    test_write_leaders_refused()
    test_single_vs_multi()
    test_semicolon_in_string_is_single()
    test_empty_refused()
    test_comment_led_sees_real_leader()
    test_explain_analyze_write_admitted_by_belt()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All statement-guard smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
