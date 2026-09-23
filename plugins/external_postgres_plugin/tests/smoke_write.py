#!/usr/bin/env python3
"""LIVE write-capability smoke (operator ruling 2026-08-09 + Amendment 1) for
external_postgres_plugin.

This is the LOAD-BEARING proof for the write reversal, complementing
smoke_readonly.py's proof that a read-only connection CANNOT write:
a connection opened with ``read_only=False`` (connection.py) CAN actually
write, and run_statement's commit really lands (visible from a SEPARATE
fresh connection, not just sitting in an open transaction this process
happens to still hold). It needs a REAL Postgres (MagicMock cannot prove a
real COMMIT persisted across connections), so it runs against the SAME local
SCRATCH database as smoke_readonly.py — NEVER the platform's own DB.

FIXTURE (documented, shared with smoke_readonly.py): a local Homebrew
Postgres, database ``epg_smoke_scratch``, connected as the local OS user
running this smoke (``getpass.getuser()`` — the passwordless trust
SUPERUSER) over ``localhost:5432``, ``sslmode=disable``. Setup/teardown of
the probe table go through ``psql`` (a read-write path) so this smoke never
needs the driver for anything but the connection/statement under test.

VISIBLE-SKIP-ON-UNREACHABLE (same convention as smoke_readonly.py, GTE-09):
if Postgres/the local trust role/``psql`` are unavailable at SETUP time, the
smoke exits the dedicated SKIP code (77) with an unmissable
SKIPPED-LIVE-DB-UNREACHABLE banner. A failure once checks are already
RUNNING stays a genuine FAIL (exit 1).

Proves:
  1. conn.read_only is False when connect(..., read_only=False) is used
  2. run_statement (no RETURNING): commits, rowcount reflects the real write,
     and the write is VISIBLE from a fresh separate connection (a real commit,
     not an open transaction this process happens to still hold)
  3. run_statement (RETURNING): the result set is real (driver-populated
     cur.description), writes a real TSV, and the underlying write also
     commits and is visible from a fresh connection
  4. A plain read_only=True connection (unchanged default) still cannot write
     against the SAME scratch DB in the SAME run — the write reversal is
     verb-scoped, not a global posture flip
  5. the single-statement SHAPE guard still refuses a two-statement string on
     the write path, but a write LEADER (DELETE/INSERT/UPDATE) is NOT refused
     — the access-control guard genuinely never runs here, live

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/external_postgres_plugin/tests/smoke_write.py

Exits 0 only when the live proof actually ran and passed; exits 1 on a real
failure OR when the fixture is unreachable (loud, never silent).
"""

from __future__ import annotations

import getpass
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin import query_actions  # noqa: E402
from external_postgres_plugin.app_config import ExternalDsn  # noqa: E402
from external_postgres_plugin.connection import connect  # noqa: E402
from external_postgres_plugin.statement_guard import StatementGuardError  # noqa: E402

_SCRATCH_DB = "epg_smoke_scratch"
_HOST = "localhost"
_PORT = 5432
_USER = getpass.getuser()
_SSLMODE = "disable"
_PROBE = "epg_write_probe"

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


def _psql(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", "-U", _USER, "-d", _SCRATCH_DB, "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True,
        text=True,
        timeout=20,
    )


def _psql_scalar(sql: str) -> str | None:
    result = subprocess.run(
        ["psql", "-U", _USER, "-d", _SCRATCH_DB, "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _setup() -> bool:
    try:
        subprocess.run(
            ["createdb", "-U", _USER, _SCRATCH_DB], capture_output=True, text=True, timeout=20
        )  # ignore failure — the DB may already exist
        seed = _psql(f"DROP TABLE IF EXISTS {_PROBE}; CREATE TABLE {_PROBE}(id serial PRIMARY KEY, val int);")
        return seed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _teardown() -> None:
    try:
        _psql(f"DROP TABLE IF EXISTS {_PROBE};")
    except (OSError, subprocess.SubprocessError):
        pass


def _passthrough_gate(path: str) -> str:
    return path


def _run_live_checks(workspace: Path) -> None:
    write_conn = connect(_dsn(), statement_timeout_ms=30_000, platform_pg_port=_PORT, read_only=False)
    try:
        _assert("conn.read_only is False for a write connection", write_conn.read_only is False)

        # (2) no-RETURNING write: commits, visible from a FRESH separate connection.
        result = query_actions.run_statement(
            write_conn, {"sql": f"INSERT INTO {_PROBE}(val) VALUES (42)"}, _passthrough_gate,
        )
        print(f"    observed run_statement (no RETURNING) result = {result}")
        _assert("no-RETURNING has_result_set False", result.get("has_result_set") is False)
        _assert("no-RETURNING rowcount == 1", result.get("rowcount") == 1)
        count = _psql_scalar(f"SELECT count(*) FROM {_PROBE} WHERE val = 42")
        print(f"    observed count via a SEPARATE psql connection = {count!r}")
        _assert("the write is visible from a fresh separate connection (real commit)", count == "1")

        # (3) RETURNING write: real driver-populated result set, real TSV, real commit.
        out_path = str(workspace / "returning.tsv")
        result2 = query_actions.run_statement(
            write_conn,
            {
                "sql": f"INSERT INTO {_PROBE}(val) VALUES (99) RETURNING id, val",
                "output_tsv_path": out_path,
            },
            _passthrough_gate,
        )
        print(f"    observed run_statement (RETURNING) result = {result2}")
        _assert("RETURNING has_result_set True", result2.get("has_result_set") is True)
        _assert("RETURNING row_count == 1", result2.get("row_count") == 1)
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("RETURNING TSV header", lines[0] == "id\tval")
        _assert("RETURNING TSV carries the written value", any(line.endswith("\t99") for line in lines[1:]))
        count2 = _psql_scalar(f"SELECT count(*) FROM {_PROBE} WHERE val = 99")
        _assert("the RETURNING write is also visible from a fresh separate connection", count2 == "1")

        # (5) shape guard vs. access-control guard, live.
        two_stmt_refused = False
        try:
            query_actions.run_statement(
                write_conn,
                {"sql": f"UPDATE {_PROBE} SET val = 1; UPDATE {_PROBE} SET val = 2"},
                _passthrough_gate,
            )
        except StatementGuardError:
            two_stmt_refused = True
        _assert("two-statement run_statement refused (shape guard)", two_stmt_refused)

        delete_refused = False
        try:
            query_actions.run_statement(write_conn, {"sql": f"DELETE FROM {_PROBE} WHERE val = 42"}, _passthrough_gate)
        except StatementGuardError:
            delete_refused = True
        _assert("a DELETE is NOT refused by run_statement (no access-control guard on this path)", not delete_refused)
        remaining = _psql_scalar(f"SELECT count(*) FROM {_PROBE} WHERE val = 42")
        _assert("the live DELETE actually removed the row", remaining == "0")
    finally:
        write_conn.close()

    # (4) the write reversal is verb-scoped: a read_only=True connection on the
    # SAME scratch DB, in the SAME run, still cannot write.
    read_conn = connect(_dsn(), statement_timeout_ms=30_000, platform_pg_port=_PORT)
    try:
        _assert("conn.read_only is True for the DEFAULT (unaffected) connection", read_conn.read_only is True)
        refused_state: str | None = None
        try:
            with read_conn.cursor() as cur:
                cur.execute(f"INSERT INTO {_PROBE}(val) VALUES (7)")
            read_conn.rollback()
        except Exception as exc:  # a real psycopg error carries .sqlstate
            read_conn.rollback()
            refused_state = getattr(exc, "sqlstate", None)
        print(f"    observed SQLSTATE {refused_state} for a write on the default read-only connection")
        _assert(
            "the default (read_only=True) connection still refuses a write — reversal is verb-scoped",
            refused_state == "25006",
        )
    finally:
        read_conn.close()


_SKIP_EXIT_CODE = 77


def _unreachable_skip(detail: str) -> int:
    print("=" * 70)
    print("SKIPPED-LIVE-DB-UNREACHABLE")
    print('BORN_CLONE_SKIP_WITNESS={"reason":"local_postgres_fixture_unreachable"}')
    print(f"  {detail}")
    print("  The LIVE write-capability proof DID NOT RUN. Bring up local Postgres")
    print("  (the local trust-auth user @ localhost:5432 + createdb/psql) and re-run.")
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
    the first-seen value). Mirrors smoke_readonly.py's identical helper.
    """
    _fresh_load_counter[0] += 1
    module_name = f"_smoke_write_fresh_{_fresh_load_counter[0]}"
    spec = importlib.util.spec_from_file_location(module_name, __file__)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not build an import spec for {__file__}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _check_user_is_dynamic_getuser() -> None:
    """RED-FIRST: ``_USER`` must resolve to ``getpass.getuser()`` at import
    time, NOT a hardcoded operator username — same proof as
    smoke_readonly.py's identical check, independently re-run here since this
    file has its own separate ``_USER`` binding a regression there wouldn't
    catch.
    """
    sentinel = "smoke_epg_write_user_sentinel_not_a_real_user"
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
    """Companion to the dynamic-getuser proof above — even with ``_USER``
    correctly derived, a literal could still leak elsewhere in this file's
    prose. Same construction as smoke_readonly.py's identical check.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(_OPERATOR_USERNAME_TOKEN)}(?![A-Za-z0-9_])")
    _assert(
        "source carries no bare operator-username token",
        pattern.search(source) is None,
    )


def main() -> int:
    print("\nexternal_postgres_plugin LIVE write-capability smoke")
    print("=" * 53)
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
        with tempfile.TemporaryDirectory(prefix="epg_write_smoke_") as workspace:
            _run_live_checks(Path(workspace))
    except Exception as exc:  # fixture failed mid-run -> loud FAIL, not a silent skip
        _teardown()
        return _mid_run_fail(f"live fixture failed mid-run: {exc}")
    _teardown()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All LIVE write-capability checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
