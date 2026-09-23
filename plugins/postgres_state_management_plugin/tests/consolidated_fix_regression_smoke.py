#!/usr/bin/env python3
"""Live regression smoke for the 2026-06-20 capability-primitive consolidated fix.

Covers every defect Codex's adversarial review caught on the original build, so
none can silently recur. Runs the REAL local-postgres AND RDS provider/txn
classes against the LOCAL Postgres (the RDS provider connects via plain
conninfo for localhost — cloud IAM is NOT required, contrary to the original
smokes' docs; this is what would have caught BLOCKER-2).

* **BLOCKER-1 (filter serializer).** Autocommit ``update`` and all three typed
  txn filter paths (``query_state`` / ``update_state`` WHERE /
  ``increment_and_return``) must serialize a scalar WHERE value the same way
  the write path did, else a datetime filter silently matches 0 rows. Proven
  with a tz-aware datetime round-trip + a composition guard.
* **BLOCKER-1b (=ANY array serializer).** The same applies ELEMENT-WISE to
  list/tuple (``= ANY``) filters: Codex's gating re-review found the first fix
  threaded the serializer to the scalar branch only, leaving the array branch
  raw, so an array of tz-aware datetimes never matched the naive-UTC stored
  values. Proven with a unit guard on ``_build_filter_clauses`` (element-wise
  + raw-when-no-serializer, both providers) plus autocommit-update and typed-txn
  array-datetime round-trips on local AND RDS.
* **BLOCKER-2 (NUL parity).** RDS ``serialize_value_for_txn`` /
  ``_serialize_value_for_sql`` must strip NULs like local; an RDS txn
  ``write_state`` of NUL-bearing text must store it stripped, not raise.
* **BLOCKER-2b (NUL strip must not mangle a literal escape).** The NUL strip
  must remove ACTUAL chr(0) bytes from Python strings BEFORE ``json.dumps`` --
  NEVER textually strip ``\\u0000`` from the serialized JSON output, which
  cannot tell an embedded NUL from legitimate source text literally containing
  those 6 chars and corrupts the latter into invalid JSON (Codex gating
  re-review). Proven pure + live-JSONB, both providers: actual NUL -> stripped;
  literal backslash-u-0-0-0-0 -> preserved + round-trips.
* **MAJOR-1 (predicate validation).** A malformed ``conflict_predicate`` must
  raise ``ValueError`` (→ a proper error ActionResult via the facade), never an
  uncaught KeyError/TypeError.
* **MAJOR-2 (smoke discipline).** Real RDS behavioral runs (above) + the
  conflict predicate is asserted against the LIVE ``session_ledger__session``
  partial-unique index introspected from ``pg_indexes`` (not a hardcoded
  fixture), so index drift fails loudly.

Sandboxed via temporary schemas (one per provider); cleanup drops them in a
``finally``. Env-gated behind ``CONSOLIDATED_FIX_SMOKE=1``.

Run::

    CONSOLIDATED_FIX_SMOKE=1 \\
      .venv/bin/python3 \\
      plugins/postgres_state_management_plugin/tests/consolidated_fix_regression_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, LiteralString, cast

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(_REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(
    0,
    str(_REPO_ROOT / "plugins" / "rds_postgres_state_management_plugin" / "src"),
)

from postgres_state_management_plugin.plugin import (  # noqa: E402
    _PostgresStateTransaction,
)
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    serialize_value_for_txn as pg_serialize_txn,
)
from rds_postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig as RdsConfig,
)
from rds_postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider as RdsProvider,
)
from rds_postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    serialize_value_for_txn as rds_serialize_txn,
)
from rds_postgres_state_management_plugin.rds_crud import (  # noqa: E402
    state_upsert as rds_state_upsert,
)
from rds_postgres_state_management_plugin.rds_transaction import (  # noqa: E402
    RdsPostgresStateManagementTransaction,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


_PROFILE_PG_CONFIG = (
    _REPO_ROOT / "profile" / "config" / "plugins"
    / "postgres_state_management_plugin.json"
)

_NS = "fixprobe"
_TABLE = "row"
_PHYSICAL = f"{_NS}__{_TABLE}"


def _raw_config() -> dict[str, Any]:
    return json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8"))


def _create_probe_table(provider: PostgresProvider | RdsProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'CREATE TABLE "{schema}"."{_PHYSICAL}" ('
                "id text PRIMARY KEY, val text, counter integer NOT NULL DEFAULT 0, "
                "status text, ts timestamp, meta jsonb, "
                "is_deleted integer NOT NULL DEFAULT 0)",
            )
        )


def _drop_schema(provider: PostgresProvider | RdsProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        )


# --- BLOCKER-1: scalar-filter serializer parity (autocommit update) ---------


def case_b1_autocommit_update_datetime(provider: PostgresProvider) -> None:
    aware = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    _, params = provider.build_update_sql(
        _NS, _TABLE, {"ts": aware}, {"val": "x"}
    )
    _check(
        aware.isoformat() in params and aware not in params,
        "autocommit build_update_sql serializes the WHERE datetime (isoformat, "
        f"not raw) (params={params!r})",
    )
    provider.insert(_NS, _TABLE, {"id": "b1a", "ts": aware, "val": "before"})
    n = provider.update(_NS, _TABLE, conditions={"ts": aware}, updates={"val": "after"})
    rows = provider.select(_NS, _TABLE, conditions={"id": "b1a"})
    _check(
        n == 1 and rows[0]["val"] == "after",
        f"autocommit UPDATE WHERE ts=<aware> matches 1 row (n={n}); raw-bind "
        "regression matched 0",
    )


# --- BLOCKER-1: all 3 typed txn filter paths honor the seam ------------------


def case_b1_txn_filter_datetime(
    provider: PostgresProvider | RdsProvider,
    txn_cls: type[_PostgresStateTransaction] | type[RdsPostgresStateManagementTransaction],
    label: str,
) -> None:
    aware = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone(timedelta(hours=-8)))
    with provider.get_transactional_connection() as conn:
        txn = txn_cls(conn, provider)  # type: ignore[arg-type]
        txn.write_state(
            _NS,
            {"table": _TABLE, "record": {"id": "b1t", "ts": aware, "counter": 1}},
        )
        q = txn.query_state(_NS, {"table": _TABLE, "filters": {"ts": aware}})
        u = txn.update_state(
            _NS, {"table": _TABLE, "filters": {"ts": aware}}, {"val": "upd"}
        )
        inc = txn.increment_and_return(
            _NS, {"table": _TABLE, "filters": {"ts": aware}, "column": "counter"}
        )
    _check(
        len(q) == 1 and u == 1 and inc == 2,
        f"[{label}] txn query_state/update_state/increment WHERE aware-dt all "
        f"match via the seam (q={len(q)}, u={u}, inc={inc}); raw-filter matched 0",
    )


# --- BLOCKER-1b: =ANY array filters honor the seam element-wise --------------
# Codex's gating re-review: the original fix threaded the serializer to the
# SCALAR filter branch only; the list/tuple (``= ANY``) branch still bound the
# array RAW, so an array of tz-aware datetimes never matched the naive-UTC
# values the typed-txn write path stored. Fix: ``_build_filter_clauses`` applies
# the serializer ELEMENT-WISE when one is provided (raw otherwise).


def case_b1b_filter_clauses_array_serialized() -> None:
    """Unit guard: the ``= ANY`` array branch serializes element-wise."""
    aware = datetime(2026, 3, 5, 6, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    expected = pg_serialize_txn(aware)  # tz-aware -> naive UTC datetime
    _, pg_params = PostgresProvider._build_filter_clauses(
        {"ts": [aware]}, serialize=pg_serialize_txn
    )
    _, rds_params = RdsProvider._build_filter_clauses(
        {"ts": [aware]}, serialize=rds_serialize_txn
    )
    _check(
        pg_params == [[expected]] and rds_params == [[expected]],
        f"_build_filter_clauses serializes =ANY array element-wise, RDS<->local "
        f"parity (pg={pg_params!r}, rds={rds_params!r}, expected={[[expected]]!r})",
    )
    _, raw_params = PostgresProvider._build_filter_clauses({"ts": [aware]})
    _check(
        raw_params == [[aware]],
        f"_build_filter_clauses leaves =ANY array RAW when no serializer (the "
        f"autocommit select path) (params={raw_params!r})",
    )


def case_b1b_autocommit_update_array_datetime(provider: PostgresProvider) -> None:
    aware = datetime(2026, 3, 3, 8, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    _, params = provider.build_update_sql(
        _NS, _TABLE, {"ts": [aware]}, {"val": "x"}
    )
    arr = next((p for p in params if isinstance(p, list)), None)
    _check(
        arr is not None and aware.isoformat() in arr and aware not in arr,
        "autocommit build_update_sql serializes the WHERE =ANY array element "
        f"(isoformat, not raw) (params={params!r})",
    )
    provider.insert(_NS, _TABLE, {"id": "b1ba", "ts": aware, "val": "before"})
    n = provider.update(
        _NS, _TABLE, conditions={"ts": [aware]}, updates={"val": "after"}
    )
    rows = provider.select(_NS, _TABLE, conditions={"id": "b1ba"})
    _check(
        n == 1 and rows[0]["val"] == "after",
        f"autocommit UPDATE WHERE ts = ANY([<aware>]) matches 1 row (n={n}); "
        "raw-array regression matched 0",
    )


def case_b1b_txn_filter_array_datetime(
    provider: PostgresProvider | RdsProvider,
    txn_cls: type[_PostgresStateTransaction] | type[RdsPostgresStateManagementTransaction],
    label: str,
) -> None:
    aware = datetime(2026, 3, 4, 10, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    with provider.get_transactional_connection() as conn:
        txn = txn_cls(conn, provider)  # type: ignore[arg-type]
        txn.write_state(
            _NS,
            {"table": _TABLE, "record": {"id": "b1tb", "ts": aware, "counter": 1}},
        )
        q = txn.query_state(_NS, {"table": _TABLE, "filters": {"ts": [aware]}})
        u = txn.update_state(
            _NS, {"table": _TABLE, "filters": {"ts": [aware]}}, {"val": "upd"}
        )
        inc = txn.increment_and_return(
            _NS, {"table": _TABLE, "filters": {"ts": [aware]}, "column": "counter"}
        )
    _check(
        len(q) == 1 and u == 1 and inc == 2,
        f"[{label}] txn query/update/increment WHERE ts = ANY([aware-dt]) all "
        f"match via element-wise seam (q={len(q)}, u={u}, inc={inc}); raw-array "
        "matched 0",
    )


# --- BLOCKER-2: NUL parity (pure) + RDS behavioral --------------------------


def case_b2_nul_parity() -> None:
    nul_str = "a\x00b"
    nul_json = {"k": "x\x00y"}
    _check(
        pg_serialize_txn(nul_str) == "ab" and rds_serialize_txn(nul_str) == "ab",
        f"serialize_value_for_txn strips NUL in str, both providers "
        f"(pg={pg_serialize_txn(nul_str)!r}, rds={rds_serialize_txn(nul_str)!r})",
    )
    pg_j = cast(str, pg_serialize_txn(nul_json))
    rds_j = cast(str, rds_serialize_txn(nul_json))
    _check(
        pg_j == rds_j and "\\u0000" not in rds_j and "\x00" not in rds_j,
        f"serialize_value_for_txn strips NUL in JSON, RDS↔local parity "
        f"(pg={pg_j!r}, rds={rds_j!r})",
    )


def case_b2_rds_nul_behavioral(rds_provider: RdsProvider) -> None:
    with rds_provider.get_transactional_connection() as conn:
        txn = RdsPostgresStateManagementTransaction(conn, rds_provider)
        txn.write_state(
            _NS, {"table": _TABLE, "record": {"id": "nul1", "val": "a\x00b"}}
        )
        rows = txn.query_state(_NS, {"table": _TABLE, "filters": {"id": "nul1"}})
    _check(
        rows[0]["val"] == "ab",
        f"RDS txn write_state of NUL-bearing text stored stripped 'ab' "
        f"(got {rows[0]['val']!r}); pre-fix raised DataError + rolled back",
    )


# --- BLOCKER-2b: NUL strip must NOT mangle a legitimate literal escape -------
# Codex gating re-review: the BLOCKER-2 fix textually stripped the 6-char
# sequence backslash-u-0-0-0-0 from the SERIALIZED json.dumps output. That
# cannot distinguish an actual embedded NUL byte (which serializes to that
# escape) from legitimate source text literally containing those 6 chars
# (which serializes to a DOUBLED backslash) -> it corrupted the latter into
# invalid JSON, raising InvalidTextRepresentation on the JSONB write. Fix:
# strip actual chr(0) from Python strings BEFORE json.dumps; never touch output.
# Source strings built via chr(0)/chr(92) so this test file stays escape-free.

_NUL_VALUE = "a" + chr(0) + "b"          # actual NUL byte -> "ab" after strip
_ESC_VALUE = "x" + chr(92) + "u0000y"    # x \ u 0 0 0 0 y -> PRESERVED intact


def case_b2b_json_escape_preservation() -> None:
    """Pure: actual NUL stripped; literal backslash-u0000 preserved + valid JSON."""
    nul_payload = {"k": _NUL_VALUE}
    esc_payload = {"k": _ESC_VALUE}
    for ser, name in ((pg_serialize_txn, "pg"), (rds_serialize_txn, "rds")):
        nul_json = cast(str, ser(nul_payload))
        esc_json = cast(str, ser(esc_payload))
        _check(
            json.loads(nul_json) == {"k": "ab"},
            f"{name}: actual NUL byte stripped, valid JSON ({nul_json!r})",
        )
        try:
            esc_round: Any = json.loads(esc_json)
        except json.JSONDecodeError:
            esc_round = None  # pre-fix: textual strip produced INVALID JSON
        _check(
            esc_round == {"k": _ESC_VALUE},
            f"{name}: literal backslash-u0000 PRESERVED + valid JSON "
            f"(json={esc_json!r}, parsed={esc_round!r}); pre-fix corrupted it",
        )


def case_b2b_jsonb_escape_behavioral(
    provider: PostgresProvider | RdsProvider,
    txn_cls: type[_PostgresStateTransaction] | type[RdsPostgresStateManagementTransaction],
    label: str,
) -> None:
    payload = {"k_normal": _NUL_VALUE, "k_literal": _ESC_VALUE}
    with provider.get_transactional_connection() as conn:
        txn = txn_cls(conn, provider)  # type: ignore[arg-type]
        txn.write_state(
            _NS, {"table": _TABLE, "record": {"id": "b2b", "meta": payload}}
        )
        rows = txn.query_state(_NS, {"table": _TABLE, "filters": {"id": "b2b"}})
    raw = rows[0]["meta"]
    meta: dict[str, Any] = (
        json.loads(raw) if isinstance(raw, str) else cast(dict[str, Any], raw)
    )
    _check(
        meta.get("k_normal") == "ab" and meta.get("k_literal") == _ESC_VALUE,
        f"[{label}] live JSONB write: actual NUL stripped ('ab') AND literal "
        f"backslash-u0000 PRESERVED (got {meta!r}); pre-fix raised "
        "InvalidTextRepresentation on the corrupted literal escape",
    )


# --- MAJOR-2: live RDS behavioral run of the typed ops ----------------------


def case_live_rds_behavioral(rds_provider: RdsProvider) -> None:
    with rds_provider.get_transactional_connection() as conn:
        txn = RdsPostgresStateManagementTransaction(conn, rds_provider)
        rid = txn.write_state(
            _NS, {"table": _TABLE, "record": {"id": "r1", "val": "x", "counter": 5}}
        )
        rows = txn.query_state(_NS, {"table": _TABLE, "filters": {"id": "r1"}})
        n = txn.update_state(
            _NS, {"table": _TABLE, "filters": {"id": "r1"}}, {"val": "y"}
        )
        inc = txn.increment_and_return(
            _NS, {"table": _TABLE, "filters": {"id": "r1"}, "column": "counter"}
        )
    _check(
        rid == "r1" and len(rows) == 1 and n == 1 and inc == 6,
        f"live RDS typed-txn ops round-trip against local PG "
        f"(rid={rid!r}, rows={len(rows)}, n={n}, inc={inc})",
    )


# --- MAJOR-1: malformed conflict_predicate rejected -------------------------

_MALFORMED: tuple[list[Any], ...] = (
    [{}],
    [{"column": "c"}],
    [{"column": "c", "op": "eq"}],
    [{"column": 123, "op": "is_null"}],
    [{"column": "c", "op": "between", "value": 1}],
)


def case_major1_provider_raises(provider: PostgresProvider | RdsProvider) -> None:
    for bad in _MALFORMED:
        raised = False
        try:
            provider.upsert_conditional(
                _NS, _TABLE, {"id": "mp"}, ["id"], conflict_predicate=bad
            )
        except ValueError:
            raised = True
        _check(raised, f"malformed predicate {bad!r} -> upsert_conditional ValueError")


def case_major1_facade_error_result(rds_provider: RdsProvider) -> None:
    """The public facade converts the ValueError into an error ActionResult."""
    result = rds_state_upsert(
        rds_provider,
        _NS,
        {
            "table": _TABLE,
            "record": {"id": "mpf"},
            "conflict_columns": ["id"],
            "on_conflict": "do_nothing",
            "conflict_predicate": [{}],
        },
    )
    err = result.get("error")
    _check(
        err is not None,
        "malformed predicate via facade (state_upsert) -> error ActionResult, "
        f"not a crash (action_status={result.get('action_status')!r}, error={err!r})",
    )


# --- MAJOR-2: live-index introspection --------------------------------------


def case_live_index_introspection(provider: PostgresProvider) -> None:
    """Assert my conflict predicate matches the LIVE partial-unique index."""
    with provider.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexdef ILIKE %s AND indexname LIKE %s",
            ),
            ("%canonical_external_session_id IS NULL%", "%session_ledger__session%"),
        )
        rows = cur.fetchall()
    if not rows:
        _check(False, "live session_ledger__session partial-unique index found")
        return
    indexdef = str(dict(rows[0])["indexdef"])
    rendered = PostgresProvider._build_conflict_predicate(
        [
            {"column": "canonical_external_session_id", "op": "is_null"},
            {"column": "is_deleted", "op": "eq", "value": 0},
        ]
    )
    with provider.get_connection() as conn:
        predicate_sql = rendered.as_string(conn)
    _check(
        "canonical_external_session_id IS NULL" in indexdef
        and "is_deleted = 0" in indexdef
        and predicate_sql == '"canonical_external_session_id" IS NULL AND "is_deleted" = 0',
        f"conflict predicate matches the LIVE index (live={indexdef!r}; "
        f"mine={predicate_sql!r})",
    )


def _make_local(schema: str) -> PostgresProvider:
    cfg = PostgresConfig(**_raw_config())
    cfg.pg_schema = schema
    p = PostgresProvider(cfg)
    p.initialize()
    return p


def _make_rds(schema: str) -> RdsProvider:
    cfg = RdsConfig(**_raw_config())
    cfg.pg_schema = schema
    p = RdsProvider(cfg)
    p.initialize()
    return p


def main() -> int:
    if os.environ.get("CONSOLIDATED_FIX_SMOKE") != "1":
        print(
            "  SKIP  CONSOLIDATED_FIX_SMOKE != 1; "
            "creates/drops sandbox schemas in the live DB.",
        )
        return 0
    case_b2_nul_parity()  # pure, no DB
    case_b1b_filter_clauses_array_serialized()  # pure, no DB
    case_b2b_json_escape_preservation()  # pure, no DB

    local_schema = f"example_test_fix_local_{secrets.token_hex(4)}"
    rds_schema = f"example_test_fix_rds_{secrets.token_hex(4)}"
    local = _make_local(local_schema)
    rds = _make_rds(rds_schema)
    try:
        _create_probe_table(local, local_schema)
        _create_probe_table(rds, rds_schema)
        # local (postgres provider + txn class)
        case_b1_autocommit_update_datetime(local)
        case_b1b_autocommit_update_array_datetime(local)
        case_b1_txn_filter_datetime(local, _PostgresStateTransaction, "local")
        case_b1b_txn_filter_array_datetime(local, _PostgresStateTransaction, "local")
        case_b2b_jsonb_escape_behavioral(local, _PostgresStateTransaction, "local")
        case_major1_provider_raises(local)
        case_live_index_introspection(local)
        # RDS (real RDS provider + txn class, against local PG)
        case_b1_txn_filter_datetime(rds, RdsPostgresStateManagementTransaction, "rds")
        case_b1b_txn_filter_array_datetime(
            rds, RdsPostgresStateManagementTransaction, "rds"
        )
        case_b2b_jsonb_escape_behavioral(
            rds, RdsPostgresStateManagementTransaction, "rds"
        )
        case_b2_rds_nul_behavioral(rds)
        case_live_rds_behavioral(rds)
        case_major1_provider_raises(rds)
        case_major1_facade_error_result(rds)
    finally:
        _drop_schema(local, local_schema)
        _drop_schema(rds, rds_schema)
    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
