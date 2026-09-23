#!/usr/bin/env python3
"""LIVE single-statement + defense-in-depth smoke (§8.6) for external_postgres_plugin.

The single-statement parser (``sqlparse``) is the BELT — it bounds multi-result
/ SET-injection. The read-only CONNECTION is the boundary. This smoke proves
BOTH, and that the belt is not load-bearing for safety:

  1. the parser refuses a two-statement string BEFORE execute (belt)
  2. the parser admits exactly one statement
  3. DEFENSE-IN-DEPTH (LIVE): if the parser were bypassed and a two-statement
     string whose SECOND statement is a write is sent straight to psycopg, the
     read-only connection STILL refuses the write with SQLSTATE 25006 — the
     parser correctness gap never reaches the destruction boundary (rev-F reframe)
  4. two READS bypassing the parser both run (proving the parser is hygiene, not
     safety)

Same local ``epg_smoke_scratch`` fixture as smoke_readonly.py, and the same
VISIBLE-SKIP-ON-UNREACHABLE contract (Coordinator-Day Q2, GTE-09; UPDATED
2026-08-08 — undeclared-dependency audit): a fixture unreachable at SETUP
time exits the dedicated SKIP code (77 — automake/Meson/CTest convention),
which ``run_smokes.py`` now reports distinctly from pass/fail (it used to
only surface a smoke's output on FAIL, which is why the original ruling
made this fail loud instead — that gap is closed, so the workaround is
too). A failure once checks are already RUNNING stays a genuine FAIL.

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/external_postgres_plugin/tests/smoke_multistatement.py

Exits 0 when the live proof ran and passed; 77 when the fixture was
unreachable at setup (disclosed skip); 1 on a real failure (belt, or a
break once the live proof was already running).
"""

from __future__ import annotations

import getpass
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin.app_config import ExternalDsn  # noqa: E402
from external_postgres_plugin.connection import connect  # noqa: E402
from external_postgres_plugin.statement_guard import (  # noqa: E402
    StatementGuardError,
    assert_single_statement,
)

_SCRATCH_DB = "epg_smoke_scratch"
_HOST = "localhost"
_PORT = 5432
_USER = getpass.getuser()
_PROBE = "epg_probe"
_READ_ONLY_SQLSTATE = "25006"

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


def _single_ok(sql: str) -> bool:
    try:
        assert_single_statement(sql)
        return True
    except StatementGuardError:
        return False


def _dsn() -> ExternalDsn:
    return ExternalDsn(
        name="scratch", host=_HOST, port=_PORT, dbname=_SCRATCH_DB,
        user=_USER, password="", sslmode="disable",
    )


def _psql(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", "-U", _USER, "-d", _SCRATCH_DB, "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, timeout=20,
    )


def _setup() -> bool:
    try:
        subprocess.run(["createdb", "-U", _USER, _SCRATCH_DB], capture_output=True, text=True, timeout=20)
        return _psql(
            f"DROP TABLE IF EXISTS {_PROBE}; CREATE TABLE {_PROBE}(id int); "
            f"INSERT INTO {_PROBE} VALUES (1), (2);"
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _teardown() -> None:
    try:
        _psql(f"DROP TABLE IF EXISTS {_PROBE};")
    except (OSError, subprocess.SubprocessError):
        pass


def _sqlstate(conn: Any, sql: str) -> str | None:
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.rollback()
        return None
    except Exception as exc:
        conn.rollback()
        return getattr(exc, "sqlstate", None)


def test_parser_belt() -> None:
    _assert("parser admits one statement", _single_ok("SELECT * FROM t WHERE id = 1"))
    _assert("parser refuses two statements", not _single_ok("SELECT 1; DELETE FROM t"))


def _run_live_checks() -> None:
    conn = connect(_dsn(), statement_timeout_ms=30_000, platform_pg_port=_PORT)
    try:
        # Bypass the parser: send the two-statement string straight to psycopg.
        state = _sqlstate(conn, f"SELECT 1; INSERT INTO {_PROBE} VALUES (99)")
        print(f"    observed SQLSTATE {state} for a parser-bypassing 2-stmt write")
        _assert("parser-bypass write refused at the connection (25006)", state == _READ_ONLY_SQLSTATE)

        # Two READS bypassing the parser both run — the parser is hygiene, not safety.
        two_reads_ok = _sqlstate(conn, "SELECT 1; SELECT 2") is None
        _assert("two reads bypassing the parser both run (no error)", two_reads_ok)
    finally:
        conn.close()


_SKIP_EXIT_CODE = 77


def _unreachable_skip(detail: str) -> int:
    # See the module docstring's VISIBLE-SKIP-ON-UNREACHABLE note. Setup-time
    # unreachability only -- a mid-run break stays a genuine FAIL via
    # _mid_run_fail below, since that could be a real bug, not just an
    # absent dependency.
    print("=" * 70)
    print("SKIPPED-LIVE-DB-UNREACHABLE")
    print('BORN_CLONE_SKIP_WITNESS={"reason":"local_postgres_fixture_unreachable"}')
    print(f"  {detail}")
    print("  The LIVE parser-bypass 25006 defense-in-depth proof DID NOT RUN.")
    print("  Bring up local Postgres (the local trust-auth user @ localhost:5432 + createdb/psql) and re-run.")
    print("=" * 70)
    return _SKIP_EXIT_CODE


def _mid_run_fail(detail: str) -> int:
    print("=" * 70)
    print("LIVE-DB FIXTURE FAILED MID-RUN")
    print(f"  {detail}")
    print("=" * 70)
    return 1


_fresh_load_counter = [0]


def _load_module_fresh() -> Any:
    """Load a FRESH copy of this same file (unique module name, never cached)
    so a test can observe its module-level ``_USER`` under a patched
    ``getpass.getuser`` (a normal import would reuse ``sys.modules`` and freeze
    the first-seen value). Mirrors
    ``bootstrap_stdlib_only_smoke.py:_load_bootstrap_module_fresh``.
    """
    _fresh_load_counter[0] += 1
    module_name = f"_smoke_multistatement_fresh_{_fresh_load_counter[0]}"
    spec = importlib.util.spec_from_file_location(module_name, __file__)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not build an import spec for {__file__}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _check_user_is_dynamic_getuser() -> None:
    """RED-FIRST (operator-identity parameterization, 2026-07-31): ``_USER``
    must resolve to ``getpass.getuser()`` at import time, NOT a hardcoded
    operator username. Proven the same way ``bootstrap_stdlib_only_smoke.py``
    proves ``_ADMIN_ROLE`` (:99-117): load this module fresh with
    ``getpass.getuser`` patched to a sentinel — a dynamically-sourced constant
    picks the sentinel up; a hardcoded literal would ignore the patch (RED).
    Offline; never touches Postgres.
    """
    sentinel = "smoke_epg_user_sentinel_not_a_real_user"
    with patch.object(getpass, "getuser", return_value=sentinel):
        fresh = _load_module_fresh()
    _assert(
        "_USER is dynamically sourced from getpass.getuser() (not a hardcoded username)",
        fresh._USER == sentinel,
        f"got {fresh._USER!r}; a hardcoded user would ignore the patched getuser()",
    )
    real_user = getpass.getuser()
    _assert(
        "the resolved _USER equals getpass.getuser() and is non-empty",
        bool(real_user) and _USER == real_user,
        f"got {_USER!r} vs getpass.getuser()={real_user!r}",
    )


_OPERATOR_USERNAME_TOKEN = "d" + "w"


def _check_source_carries_no_operator_username() -> None:
    """Companion to the dynamic-getuser proof above: even with ``_USER``
    correctly derived, a literal could still leak elsewhere in this file's
    prose. Composed from two concatenated halves (see
    ``_OPERATOR_USERNAME_TOKEN``) so this guard's own source never contains
    the contiguous token it hunts for. Word-bounded so it does not collide
    with an unrelated substring.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(_OPERATOR_USERNAME_TOKEN)}(?![A-Za-z0-9_])")
    _assert(
        "source carries no bare operator-username token",
        pattern.search(source) is None,
    )


def main() -> int:
    print("\nexternal_postgres_plugin LIVE single-statement / defense-in-depth smoke")
    print("=" * 68)
    _check_user_is_dynamic_getuser()
    _check_source_carries_no_operator_username()
    test_parser_belt()
    if _failed:
        print("FAILED (belt):", _failed)
        return 1
    if not _setup():
        return _unreachable_skip("scratch Postgres fixture unreachable.")
    try:
        _run_live_checks()
    except Exception as exc:
        _teardown()
        return _mid_run_fail(f"live fixture failed mid-run: {exc}")
    _teardown()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All single-statement / defense-in-depth checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
