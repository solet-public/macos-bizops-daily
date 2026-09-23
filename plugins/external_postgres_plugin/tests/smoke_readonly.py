#!/usr/bin/env python3
"""LIVE read-only enforcement smoke (§8.5) for external_postgres_plugin.

This is the LOAD-BEARING proof: the psycopg3 connection read-only characteristic
is the developer-proof write-stopper, not the belt guards. It needs a REAL
Postgres (MagicMock cannot prove SQLSTATE 25006), so it runs against a local
SCRATCH database — NEVER the platform's own DB.

FIXTURE (documented): a local Homebrew Postgres, database ``epg_smoke_scratch``,
connected as the local OS user running this smoke (``getpass.getuser()`` —
the passwordless trust SUPERUSER) over ``localhost:5432``, ``sslmode=disable``.
Connecting as the superuser is deliberate — it proves that even an
OVER-PRIVILEGED registered credential cannot write through this tool (the
read-only characteristic binds regardless of role). Setup/teardown of the
probe table go through ``psql`` (a read-WRITE path) so this smoke never needs a
raw driver import.

VISIBLE-SKIP-ON-UNREACHABLE (Coordinator-Day Q2 ruling, GTE-09; UPDATED
2026-08-08 — undeclared-dependency audit): if Postgres/the local trust
role/``psql`` are unavailable at SETUP time, the smoke exits the dedicated
SKIP code (77 — the automake/Meson/CTest convention) with an unmissable
SKIPPED-LIVE-DB-UNREACHABLE banner, and ``run_smokes.py`` now reports that
distinctly from both a pass and a fail (it used to only surface a smoke's
output on FAIL, so the original GTE-09 ruling made this fail loud instead
of a silent exit-0 skip — that gap is closed, so the workaround is too). A
failure once checks are already RUNNING (fixture broke mid-run, not just
absent) still fails loud (exit 1) rather than folding into the skip path,
since that could be a real bug. The scratch cluster is the same 24/7
instance the platform needs, so this path is pathological. It runs FOR
REAL wherever the scratch DB is reachable.

Proves:
  1. conn.read_only is True
  2. test_connection echoes the RESOLVED host it actually connected to and it
     equals the host we connected with (F1 — Rev-A R-D2 "surfaces resolved host")
  3. SHOW transaction_read_only == 'on' in the SAME transaction as the user query
     (the exact Codex BLOCKER: proves the CURRENT txn is read-only, not just
     default_transaction_read_only)
  4. EXPLAIN / SHOW / SELECT succeed
  5. INSERT/UPDATE/DELETE/CREATE/CREATE TEMP/EXPLAIN ANALYZE <write> all refused
     with SQLSTATE 25006
  6. a write as the VERY FIRST statement on a fresh connection is refused — no
     write-capable window (the rev-E point)

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/external_postgres_plugin/tests/smoke_readonly.py

Exits 0 only when the live proof actually ran and passed; exits 1 on a real
failure OR when the fixture is unreachable (loud, never silent).
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

from external_postgres_plugin import query_actions  # noqa: E402
from external_postgres_plugin.app_config import ExternalDsn  # noqa: E402
from external_postgres_plugin.connection import connect  # noqa: E402

_SCRATCH_DB = "epg_smoke_scratch"
_HOST = "localhost"
_PORT = 5432
_USER = getpass.getuser()
_SSLMODE = "disable"
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


def _dsn() -> ExternalDsn:
    return ExternalDsn(
        name="scratch",
        host=_HOST,
        port=_PORT,
        dbname=_SCRATCH_DB,
        user=_USER,
        password="",
        sslmode=_SSLMODE,
    )


def _psql(sql: str, dbname: str = _SCRATCH_DB) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", "-U", _USER, "-d", dbname, "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True,
        text=True,
        timeout=20,
    )


def _setup() -> bool:
    """Create the scratch DB + seed the probe table via psql (read-write path)."""
    try:
        subprocess.run(
            ["createdb", "-U", _USER, _SCRATCH_DB], capture_output=True, text=True, timeout=20
        )  # ignore failure — the DB may already exist
        seed = _psql(
            f"DROP TABLE IF EXISTS {_PROBE}; CREATE TABLE {_PROBE}(id int); "
            f"INSERT INTO {_PROBE} VALUES (1), (2);"
        )
        return seed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _teardown() -> None:
    try:
        _psql(f"DROP TABLE IF EXISTS {_PROBE};")
    except (OSError, subprocess.SubprocessError):
        pass


def _sqlstate_of_write(conn: Any, sql: str) -> str | None:
    """Attempt a write; return its SQLSTATE (rolling the txn back), or None if it slipped."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.rollback()
        return None
    except Exception as exc:  # a real psycopg error carries .sqlstate
        conn.rollback()
        return getattr(exc, "sqlstate", None)


def _run_live_checks() -> None:
    conn = connect(_dsn(), statement_timeout_ms=30_000, platform_pg_port=_PORT)
    try:
        _assert("conn.read_only is True", conn.read_only is True)

        # LIVE test_connection: the resolved host echoed back must equal the host
        # we actually connected with (F1 — Rev-A R-D2 "test_connection surfaces
        # the resolved host so the operator can confirm/catch a mis-registration").
        tc = query_actions.test_connection(conn, {})
        conn.rollback()
        print(f"    observed test_connection host = {tc.get('host')!r} (connected with {_HOST!r})")
        _assert("test_connection returns a host field", "host" in tc)
        _assert("resolved host equals the connected host", tc.get("host") == _HOST, str(tc.get("host")))
        _assert("test_connection read_only True", tc.get("read_only") is True)

        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            row = cur.fetchone()
            show_val = row[0] if row else None
            cur.execute(f"SELECT count(*) FROM {_PROBE}")
            count_row = cur.fetchone()
        conn.rollback()
        print(f"    observed SHOW transaction_read_only = {show_val!r} (same txn as the SELECT)")
        _assert("SHOW transaction_read_only == 'on' in the user query's txn", show_val == "on")

        with conn.cursor() as cur:
            cur.execute("EXPLAIN SELECT 1")
            explained = cur.fetchall()
        conn.rollback()
        _assert("EXPLAIN succeeds", len(explained) > 0)
        _assert("SELECT on the probe table returned the seeded rows", count_row is not None and count_row[0] == 2)

        writes = (
            "CREATE TEMP TABLE t_probe (a int)",
            f"INSERT INTO {_PROBE} VALUES (99)",
            f"UPDATE {_PROBE} SET id = 0",
            f"DELETE FROM {_PROBE}",
            "CREATE TABLE t_probe2 (a int)",
            f"EXPLAIN ANALYZE INSERT INTO {_PROBE} VALUES (99)",
        )
        for sql in writes:
            state = _sqlstate_of_write(conn, sql)
            print(f"    observed SQLSTATE {state} for: {sql}")
            _assert(f"write refused with 25006: {sql!r}", state == _READ_ONLY_SQLSTATE)
    finally:
        conn.close()

    # First-statement-is-a-write on a FRESH connection: no write-capable window.
    fresh = connect(_dsn(), statement_timeout_ms=30_000, platform_pg_port=_PORT)
    try:
        first_state = _sqlstate_of_write(fresh, "CREATE TABLE t_first (a int)")
        print(f"    observed SQLSTATE {first_state} for the FIRST statement (a write) on a fresh conn")
        _assert("first-statement write refused (no window)", first_state == _READ_ONLY_SQLSTATE)
    finally:
        fresh.close()


_SKIP_EXIT_CODE = 77


def _unreachable_skip(detail: str) -> int:
    # UPDATED 2026-08-08 (undeclared-dependency audit): the Coordinator-Day Q2 /
    # GTE-09 ruling made this FAIL LOUD (exit 1) rather than a silent exit-0
    # skip, because run_smokes.py only surfaced a smoke's output on FAIL --
    # a genuine skip would have been invisible in the suite. That gap is now
    # closed: run_smokes.py reports passed/skipped/failed distinctly via the
    # automake/Meson/CTest SKIP_RETURN_CODE convention (77), so a setup-time
    # unreachable fixture can now be a VISIBLE skip instead of an over-broad
    # fail that blocked every commit on a machine without a local scratch
    # Postgres. workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md.
    # This function covers ONLY setup-time unreachability (the fixture never
    # came up) -- a failure mid-run, once checks are already executing, stays
    # a genuine FAIL via _mid_run_fail below, since that could be a real bug,
    # not just an absent dependency.
    print("=" * 70)
    print("SKIPPED-LIVE-DB-UNREACHABLE")
    print('BORN_CLONE_SKIP_WITNESS={"reason":"local_postgres_fixture_unreachable"}')
    print(f"  {detail}")
    print("  The LIVE read-only 25006 proof DID NOT RUN. Bring up local Postgres")
    print("  (the local trust-auth user @ localhost:5432 + createdb/psql) and re-run.")
    print("=" * 70)
    return _SKIP_EXIT_CODE


def _mid_run_fail(detail: str) -> int:
    # Genuine FAIL (exit 1), distinct from _unreachable_skip above: the
    # fixture came up but something broke WHILE checks were running -- that
    # could be a real bug in the checks themselves, not just an absent
    # dependency, so this stays loud rather than folding into the skip path.
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
    module_name = f"_smoke_readonly_fresh_{_fresh_load_counter[0]}"
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
    Offline; never touches Postgres — runs even when the live fixture is
    unreachable, so a regression here is never masked by the
    unreachable-skip path.
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
    prose (docstring, print, error string). Composed from two concatenated
    halves (see ``_OPERATOR_USERNAME_TOKEN``) so this guard's own source
    never contains the contiguous token it hunts for. Word-bounded so it does
    not collide with an unrelated substring.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(_OPERATOR_USERNAME_TOKEN)}(?![A-Za-z0-9_])")
    _assert(
        "source carries no bare operator-username token",
        pattern.search(source) is None,
    )


def main() -> int:
    print("\nexternal_postgres_plugin LIVE read-only smoke")
    print("=" * 44)
    _check_user_is_dynamic_getuser()
    _check_source_carries_no_operator_username()
    if _failed:
        print()
        print(f"Results: {_passed} passed, {len(_failed)} failed")
        print("FAILED:", _failed)
        return 1
    if not _setup():
        return _unreachable_skip(
            "scratch Postgres fixture unreachable (createdb/psql/local-trust-user/localhost:5432)."
        )
    try:
        _run_live_checks()
    except Exception as exc:  # fixture failed mid-run -> loud FAIL, not a silent skip
        _teardown()
        return _mid_run_fail(f"live fixture failed mid-run: {exc}")
    _teardown()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All LIVE read-only checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
