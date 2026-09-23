#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the #2 typed StateTransaction ops.

Pins the four typed ops added to ``StateTransaction`` (capability-primitive
UNIT C, 2026-06-20): ``write_state`` / ``update_state`` / ``query_state`` /
``increment_and_return``. They compose SQL via the injected provider's pure
builders and execute on the open transaction connection, RAISING on failure
(so the surrounding context manager rolls back) and returning plain values.

What this proves:

* **Load-safety (the load-coupling gate).** Both ``StateTransaction``
  subclasses — ``_PostgresStateTransaction`` and
  ``RdsPostgresStateManagementTransaction`` — have an EMPTY
  ``__abstractmethods__`` set, i.e. every abstractmethod (the original 4 +
  the 4 new typed ops) is implemented, so both are instantiable and both
  plugins import. Importing the two classes is itself the module-load proof.
* **Behavior (postgres, live).** write/query/update/increment round-trips;
  the Mapping-A list-filter status gate (``increment_and_return`` with a
  ``status=[...]`` filter, fused via the #4 ``= ANY`` grammar); a 0-row
  increment RAISES; and a raised exception inside ``transactional`` ROLLS BACK
  the increment.
* **The F1 TZ-storage seam (advisor BLOCK 1).** A tz-aware NON-UTC datetime
  written through ``write_state`` is stored UTC-normalized (07:00 for a
  12:00+05:00 input) — NOT the literal wall-clock that the autocommit
  isoformat path would store into a ``timestamp without time zone`` column.
  This is the divergence the shared-builder design could have silently picked
  wrong; the typed ops route values through ``serialize_value_for_txn``.

RDS gets load-safety coverage (``__abstractmethods__``) here; its live
behavioral coverage — the RDS provider/txn classes run against LOCAL Postgres,
no cloud IAM required — lives in ``consolidated_fix_regression_smoke.py``.

Sandboxed via a temporary schema; cleanup drops it in a ``finally``. Env-gated
behind ``TYPED_TXN_OPS_SMOKE=1``.

Run::

    TYPED_TXN_OPS_SMOKE=1 \\
      .venv/bin/python3 \\
      plugins/postgres_state_management_plugin/tests/typed_txn_ops_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import LiteralString, cast

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
    _REPO_ROOT
    / "profile"
    / "config"
    / "plugins"
    / "postgres_state_management_plugin.json"
)

_NS = "txnprobe"
_TABLE = "row"
_PHYSICAL = f"{_NS}__{_TABLE}"


def _load_pg_config(schema_name: str) -> PostgresConfig:
    raw = json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8"))
    config = PostgresConfig(**raw)
    config.pg_schema = schema_name
    return config


def _create_probe_table(provider: PostgresProvider, schema_name: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'CREATE TABLE "{schema_name}"."{_PHYSICAL}" ('
                "id text PRIMARY KEY, "
                "val text, "
                "counter integer NOT NULL DEFAULT 0, "
                "status text, "
                "ts timestamp, "  # timestamp WITHOUT time zone — the F1 seam target
                "is_deleted integer NOT NULL DEFAULT 0"
                ")",
            )
        )


def case_both_txn_classes_concrete() -> None:
    """The load-coupling gate: both subclasses implement every abstractmethod."""
    _check(
        not _PostgresStateTransaction.__abstractmethods__,
        "postgres _PostgresStateTransaction is concrete (all abstractmethods "
        f"implemented; __abstractmethods__={set(_PostgresStateTransaction.__abstractmethods__)})",
    )
    _check(
        not RdsPostgresStateManagementTransaction.__abstractmethods__,
        "RDS RdsPostgresStateManagementTransaction is concrete (load-safe; "
        f"__abstractmethods__={set(RdsPostgresStateManagementTransaction.__abstractmethods__)})",
    )


def case_write_and_query(provider: PostgresProvider) -> None:
    with provider.get_transactional_connection() as conn:
        txn = _PostgresStateTransaction(conn, provider)
        rid = txn.write_state(
            _NS, {"table": _TABLE, "record": {"id": "w1", "val": "hello"}}
        )
        rows = txn.query_state(_NS, {"table": _TABLE, "filters": {"id": "w1"}})
    _check(
        rid == "w1" and len(rows) == 1 and rows[0]["val"] == "hello",
        f"write_state -> id 'w1'; query_state round-trips it (rid={rid!r}, rows={rows!r})",
    )


def case_update(provider: PostgresProvider) -> None:
    with provider.get_transactional_connection() as conn:
        txn = _PostgresStateTransaction(conn, provider)
        txn.write_state(_NS, {"table": _TABLE, "record": {"id": "u1", "val": "a"}})
        n = txn.update_state(
            _NS, {"table": _TABLE, "filters": {"id": "u1"}}, {"val": "b"}
        )
        rows = txn.query_state(_NS, {"table": _TABLE, "filters": {"id": "u1"}})
    _check(
        n == 1 and rows[0]["val"] == "b",
        f"update_state -> rows-affected=1 and value changed (n={n}, val={rows[0]['val']!r})",
    )


def case_increment(provider: PostgresProvider) -> None:
    with provider.get_transactional_connection() as conn:
        txn = _PostgresStateTransaction(conn, provider)
        txn.write_state(_NS, {"table": _TABLE, "record": {"id": "c1", "counter": 5}})
        v1 = txn.increment_and_return(
            _NS, {"table": _TABLE, "filters": {"id": "c1"}, "column": "counter", "by": 1}
        )
        v2 = txn.increment_and_return(
            _NS, {"table": _TABLE, "filters": {"id": "c1"}, "column": "counter"}
        )
    _check(
        v1 == 6 and v2 == 7,
        f"increment_and_return: 5->6 (by=1) ->7 (by default 1) (got {v1}, {v2})",
    )


def case_increment_list_filter_mapping_a(provider: PostgresProvider) -> None:
    """Mapping-A: the status gate fuses into the allocator WHERE via = ANY."""
    with provider.get_transactional_connection() as conn:
        txn = _PostgresStateTransaction(conn, provider)
        txn.write_state(
            _NS,
            {"table": _TABLE, "record": {"id": "m1", "counter": 0, "status": "OPEN"}},
        )
        matched = txn.increment_and_return(
            _NS,
            {
                "table": _TABLE,
                "filters": {"id": "m1", "status": ["OPEN", "IDLE"]},
                "column": "counter",
            },
        )
    _check(
        matched == 1,
        f"increment with status IN [OPEN,IDLE] gate (status=OPEN) -> incremented to {matched}",
    )


def case_increment_status_gate_miss_raises(provider: PostgresProvider) -> None:
    """Status gate miss → 0 rows → raise (the Mapping-A gate-fail path)."""
    ok = False
    with provider.get_transactional_connection() as conn:
        txn = _PostgresStateTransaction(conn, provider)
        txn.write_state(
            _NS,
            {"table": _TABLE, "record": {"id": "m2", "counter": 0, "status": "CLOSED"}},
        )
        try:
            txn.increment_and_return(
                _NS,
                {
                    "table": _TABLE,
                    "filters": {"id": "m2", "status": ["OPEN", "IDLE"]},
                    "column": "counter",
                },
            )
        except RuntimeError:
            ok = True
    _check(ok, "increment with status gate MISS (status=CLOSED) -> RuntimeError (0 rows)")


def case_tz_aware_datetime_seam(provider: PostgresProvider) -> None:
    """advisor BLOCK 1: tz-aware non-UTC datetime stored UTC-normalized, not isoformat."""
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    expected_utc_naive = datetime(2026, 1, 1, 7, 0, 0)  # 12:00+05:00 == 07:00 UTC
    with provider.get_transactional_connection() as conn:
        txn = _PostgresStateTransaction(conn, provider)
        txn.write_state(_NS, {"table": _TABLE, "record": {"id": "tz1", "ts": aware}})
        rows = txn.query_state(_NS, {"table": _TABLE, "filters": {"id": "tz1"}})
    stored = rows[0]["ts"]
    _check(
        stored == expected_utc_naive,
        f"tz-aware 12:00+05:00 stored UTC-normalized as {expected_utc_naive} (got {stored!r}); "
        "the F1 seam, NOT the autocommit isoformat-wall-clock",
    )


def case_rollback(provider: PostgresProvider) -> None:
    """A raise inside transactional() rolls the increment back."""
    with provider.get_transactional_connection() as conn:
        txn = _PostgresStateTransaction(conn, provider)
        txn.write_state(_NS, {"table": _TABLE, "record": {"id": "rb1", "counter": 10}})

    class _ForceRollbackError(Exception):
        pass

    try:
        with provider.get_transactional_connection() as conn:
            txn = _PostgresStateTransaction(conn, provider)
            txn.increment_and_return(
                _NS, {"table": _TABLE, "filters": {"id": "rb1"}, "column": "counter"}
            )
            raise _ForceRollbackError
    except _ForceRollbackError:
        pass

    with provider.get_transactional_connection() as conn:
        txn = _PostgresStateTransaction(conn, provider)
        rows = txn.query_state(_NS, {"table": _TABLE, "filters": {"id": "rb1"}})
    _check(
        rows[0]["counter"] == 10,
        f"increment rolled back on raise (counter still 10, got {rows[0]['counter']})",
    )


def case_autocommit_paths(provider: PostgresProvider) -> None:
    """C-dry regression net: autocommit insert/select/update via the builders."""
    rid = provider.insert(_NS, _TABLE, {"id": "ac1", "val": "auto", "counter": 3})
    rows = provider.select(_NS, _TABLE, conditions={"id": "ac1"})
    n = provider.update(_NS, _TABLE, conditions={"id": "ac1"}, updates={"val": "auto2"})
    rows2 = provider.select(_NS, _TABLE, conditions={"id": "ac1"})
    _check(
        rid == "ac1"
        and len(rows) == 1
        and rows[0]["val"] == "auto"
        and n == 1
        and rows2[0]["val"] == "auto2",
        f"autocommit insert/select/update via shared builders (rid={rid!r}, "
        f"insert_val={rows[0]['val']!r}, updated={n}, new_val={rows2[0]['val']!r})",
    )


def _drop_schema(provider: PostgresProvider, schema_name: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        )


def main() -> int:
    if os.environ.get("TYPED_TXN_OPS_SMOKE") != "1":
        print(
            "  SKIP  TYPED_TXN_OPS_SMOKE != 1; "
            "this smoke creates and drops a sandbox schema in the live DB.",
        )
        return 0
    # Load-safety is connection-free — assert it before touching the DB.
    case_both_txn_classes_concrete()
    schema_name = f"example_test_typed_txn_{secrets.token_hex(4)}"
    config = _load_pg_config(schema_name)
    provider = PostgresProvider(config)
    provider.initialize()
    try:
        _create_probe_table(provider, schema_name)
        case_write_and_query(provider)
        case_update(provider)
        case_increment(provider)
        case_increment_list_filter_mapping_a(provider)
        case_increment_status_gate_miss_raises(provider)
        case_tz_aware_datetime_seam(provider)
        case_rollback(provider)
        case_autocommit_paths(provider)
    finally:
        _drop_schema(provider, schema_name)
    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
