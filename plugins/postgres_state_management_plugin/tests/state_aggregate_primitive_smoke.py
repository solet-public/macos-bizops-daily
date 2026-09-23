#!/usr/bin/env python3
"""Live cross-twin smoke for the state aggregate primitive + D2 txn delete_records.

Builds the REAL local-postgres AND RDS provider/txn classes against the LOCAL
Postgres (the RDS provider connects via plain conninfo for localhost — cloud IAM
is NOT required) and exercises the new ``count`` / ``max_value`` / ``min_value``
aggregates plus the typed-txn ``delete_records`` (D2), covering every HARD
contract from the frozen design spec so none can silently regress:

* **Cross-twin parity.** ``count`` / ``max_value`` / ``min_value`` return
  identical results from both ``PostgresProvider`` twins and both
  ``StateTransaction`` twins (a one-sided fix fails loudly).
* **Empty set.** ``count`` -> ``0``; ``max_value`` / ``min_value`` -> ``None``
  (SQL ``NULL``), never a fabricated ``0``.
* **Filter grammar.** equality + ``= ANY`` (list) + ``{"op": "is_null"}`` paths.
* **``is_deleted`` NOT auto-excluded** (mirrors ``query_state``, NOT
  ``query_ordered``): a soft-deleted row IS counted unless the caller filters it.
* **F1 TZ seam (path-dependent).** Over a ``TIMESTAMP`` column (NOT
  ``timestamptz``) the TYPED-TXN ``max_value`` / ``min_value`` return a RAW
  NAIVE datetime (the seam the in-txn summarize-MAX consumer normalizes), while
  the AUTOCOMMIT surface serializes to an ISO-8601 string — matching autocommit
  ``query_state`` so the ActionResult envelope is JSON-safe at the bridge.
* **Validation fail-fast.** ``count`` + a ``column`` is rejected; ``max_value`` /
  ``min_value`` without a ``column`` is rejected — provider raises, facade
  returns an error ActionResult.
* **Typed-txn raw scalars.** the ``StateTransaction`` aggregates return raw
  Python values (count -> int; max/min -> scalar | None), not ActionResults.
* **D2 typed-txn ``delete_records``.** soft (``is_deleted = 1``) + hard
  (``DELETE``), both reporting rows-affected.
* **Facade envelope.** the autocommit facade surfaces the scalar at
  ``data.result.value`` (the canonical state-verb envelope).

Sandboxed via temporary schemas (one per provider); cleanup drops them in a
``finally``. Env-gated behind ``STATE_AGGREGATE_SMOKE=1``.

Run::

    STATE_AGGREGATE_SMOKE=1 \\
      .venv/bin/python3 \\
      plugins/postgres_state_management_plugin/tests/state_aggregate_primitive_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import datetime
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

from postgres_state_management_plugin.aggregate_ops import (  # noqa: E402
    run_aggregate,
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
from rds_postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig as RdsConfig,
)
from rds_postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider as RdsProvider,
)
from rds_postgres_state_management_plugin.rds_crud import (  # noqa: E402
    state_count as rds_state_count,
)
from rds_postgres_state_management_plugin.rds_crud import (  # noqa: E402
    state_max_value as rds_state_max_value,
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

_NS = "aggprobe"
_TABLE = "row"
_PHYSICAL = f"{_NS}__{_TABLE}"

# Naive timestamps (the F1 TZ seam: a ``timestamp`` column, NOT ``timestamptz``).
_TS1 = datetime(2026, 1, 1, 8, 0, 0)
_TS5 = datetime(2026, 5, 1, 12, 0, 0)

AnyProvider = PostgresProvider | RdsProvider
AnyTxn = type[_PostgresStateTransaction] | type[RdsPostgresStateManagementTransaction]


def _raw_config() -> dict[str, Any]:
    return json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8"))


def _create_probe_table(provider: AnyProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'CREATE TABLE "{schema}"."{_PHYSICAL}" ('
                "id text PRIMARY KEY, status text, amount integer, ts timestamp, "
                "is_deleted integer NOT NULL DEFAULT 0)",
            )
        )


def _drop_schema(provider: AnyProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        )


def _seed(provider: AnyProvider) -> None:
    """Five rows; r4 is soft-deleted. Status of r5 is NULL (for is_null)."""
    rows = [
        {"id": "r1", "status": "a", "amount": 10, "ts": _TS1, "is_deleted": 0},
        {"id": "r2", "status": "a", "amount": 20, "ts": datetime(2026, 2, 1, 9, 0), "is_deleted": 0},
        {"id": "r3", "status": "b", "amount": 30, "ts": datetime(2026, 3, 1, 10, 0), "is_deleted": 0},
        {"id": "r4", "status": "b", "amount": 40, "ts": datetime(2026, 4, 1, 11, 0), "is_deleted": 1},
        {"id": "r5", "status": None, "amount": 5, "ts": _TS5, "is_deleted": 0},
    ]
    for record in rows:
        provider.insert(_NS, _TABLE, record)


def _battery(provider: AnyProvider) -> dict[str, object]:
    """Every aggregate result keyed by a stable label (for cross-twin compare)."""
    agg = provider.aggregate
    return {
        "count_all": agg(_NS, _TABLE, "count", None, {}),
        "count_live": agg(_NS, _TABLE, "count", None, {"is_deleted": 0}),
        "count_status_a": agg(_NS, _TABLE, "count", None, {"status": "a"}),
        "count_status_any": agg(_NS, _TABLE, "count", None, {"status": ["a", "b"]}),
        "count_status_null": agg(_NS, _TABLE, "count", None, {"status": {"op": "is_null"}}),
        "count_empty": agg(_NS, _TABLE, "count", None, {"status": "zzz"}),
        "max_amount": agg(_NS, _TABLE, "max", "amount", {}),
        "max_amount_live": agg(_NS, _TABLE, "max", "amount", {"is_deleted": 0}),
        "min_amount": agg(_NS, _TABLE, "min", "amount", {}),
        "max_ts": agg(_NS, _TABLE, "max", "ts", {}),
        "min_ts": agg(_NS, _TABLE, "min", "ts", {}),
        "max_empty": agg(_NS, _TABLE, "max", "amount", {"status": "zzz"}),
        "min_empty": agg(_NS, _TABLE, "min", "ts", {"status": "zzz"}),
    }


_EXPECTED: dict[str, object] = {
    "count_all": 5,           # is_deleted NOT excluded -> r4 counted
    "count_live": 4,          # is_deleted=0 -> r4 excluded
    "count_status_a": 2,
    "count_status_any": 4,    # = ANY (a|b): r1..r4; r5 NULL excluded
    "count_status_null": 1,   # r5
    "count_empty": 0,
    "max_amount": 40,         # r4 included (no is_deleted filter)
    "max_amount_live": 30,    # r4 excluded
    "min_amount": 5,
    "max_ts": _TS5.isoformat(),   # autocommit serializes datetimes (ISO-8601)
    "min_ts": _TS1.isoformat(),
    "max_empty": None,
    "min_empty": None,
}


def case_values(provider: AnyProvider, label: str) -> dict[str, object]:
    """Assert each aggregate equals the expected value; return the battery."""
    battery = _battery(provider)
    for key, expected in _EXPECTED.items():
        _check(
            battery[key] == expected,
            f"[{label}] {key} == {expected!r} (got {battery[key]!r})",
        )
    # Empty-set None is a real None, not a 0 (count vs max/min distinction).
    _check(
        battery["max_empty"] is None and battery["min_empty"] is None,
        f"[{label}] empty-set max/min are None, not fabricated 0 "
        f"(max={battery['max_empty']!r}, min={battery['min_empty']!r})",
    )
    # is_deleted NOT auto-excluded: the unfiltered count strictly exceeds the
    # is_deleted=0 count (the soft-deleted r4 is in the unfiltered tally).
    _check(
        battery["count_all"] == 5 and battery["count_live"] == 4,
        f"[{label}] is_deleted NOT auto-excluded: count()=5 includes the "
        f"soft-deleted row, count(is_deleted=0)=4 (got {battery['count_all']!r}"
        f"/{battery['count_live']!r})",
    )
    return battery


def case_tz_seam(provider: AnyProvider, label: str) -> None:
    """AUTOCOMMIT max/min over a TIMESTAMP column -> ISO-8601 string.

    The path-dependent F1 fidelity: the autocommit exec method serializes
    datetimes (matching autocommit ``query_state``) so the ActionResult
    envelope is JSON-safe at the bridge boundary. The RAW naive datetime (the
    seam the in-txn summarize-MAX consumer normalizes) is the TYPED-TXN
    surface -- asserted in ``case_txn_aggregates``.
    """
    mx = provider.aggregate(_NS, _TABLE, "max", "ts", {})
    mn = provider.aggregate(_NS, _TABLE, "min", "ts", {})
    _check(
        isinstance(mx, str) and isinstance(mn, str)
        and mx == _TS5.isoformat() and mn == _TS1.isoformat(),
        f"[{label}] autocommit max/min over a TIMESTAMP column -> ISO-8601 "
        f"strings, JSON-safe (max={mx!r}, min={mn!r})",
    )
    # Architect: EXPLICITLY assert the ISO string is OFFSET-LESS — a naive
    # datetime's isoformat carries no tz suffix, and reparsing it yields a
    # NAIVE datetime (an offset / 'Z' suffix would reparse to an AWARE one).
    # This is the autocommit half of the per-surface transport contract; the
    # typed-txn NAIVE-datetime half is asserted in case_txn_aggregates.
    offsetless = (
        isinstance(mx, str)
        and "+" not in mx
        and not mx.endswith("Z")
        and datetime.fromisoformat(mx).tzinfo is None
    )
    _check(
        offsetless,
        f"[{label}] autocommit max-over-TIMESTAMP ISO string is OFFSET-LESS "
        f"(no tz suffix; reparses to a naive datetime) (max={mx!r})",
    )


def case_validation(provider: AnyProvider, label: str) -> None:
    """count + column rejected; max/min without column rejected (fail-fast)."""
    count_with_column = False
    try:
        provider.build_aggregate_query(_NS, _TABLE, "count", "amount", {})
    except ValueError:
        count_with_column = True
    _check(count_with_column, f"[{label}] count + 'column' -> ValueError")

    for op in ("max", "min"):
        missing_column = False
        try:
            provider.build_aggregate_query(_NS, _TABLE, op, None, {})
        except ValueError:
            missing_column = True
        _check(missing_column, f"[{label}] {op} without 'column' -> ValueError")


def case_txn_aggregates(
    provider: AnyProvider, txn_cls: AnyTxn, label: str
) -> None:
    """Typed-txn aggregates return RAW scalars (int / scalar | None)."""
    with provider.get_transactional_connection() as conn:
        txn = txn_cls(conn, provider)  # type: ignore[arg-type]
        c = txn.count(_NS, {"table": _TABLE, "filters": {"is_deleted": 0}})
        c_all = txn.count(_NS, {"table": _TABLE, "filters": {}})
        mx = txn.max_value(_NS, {"table": _TABLE, "column": "amount", "filters": {}})
        mn = txn.min_value(_NS, {"table": _TABLE, "column": "amount", "filters": {}})
        mx_ts = txn.max_value(_NS, {"table": _TABLE, "column": "ts", "filters": {}})
        empty = txn.max_value(
            _NS, {"table": _TABLE, "column": "amount", "filters": {"status": "zzz"}}
        )
    _check(
        c == 4 and c_all == 5,
        f"[{label}] txn count -> raw int (live={c!r}, all={c_all!r}; "
        "is_deleted not auto-excluded)",
    )
    _check(
        mx == 40 and mn == 5,
        f"[{label}] txn max/min amount -> raw scalars (max={mx!r}, min={mn!r})",
    )
    _check(
        isinstance(mx_ts, datetime) and mx_ts.tzinfo is None,
        f"[{label}] txn max over TIMESTAMP -> naive datetime ({mx_ts!r})",
    )
    _check(
        empty is None,
        f"[{label}] txn max over empty set -> None (got {empty!r})",
    )


def case_txn_delete_guard(
    provider: AnyProvider, txn_cls: AnyTxn, label: str
) -> None:
    """D2 fail-fast: empty / non-dict / missing filter is REJECTED up-front.

    The guard raises before any SQL runs, so it can never delete-all. Each bad
    query is rejected inside the txn (no row touched), so the connection commits
    clean.
    """
    bad_queries: tuple[tuple[dict[str, object], str], ...] = (
        ({"table": _TABLE, "filters": {}}, "empty filter"),
        ({"table": _TABLE, "filters": "nope"}, "non-dict filter"),
        ({"table": _TABLE}, "missing filter"),
    )
    for bad_query, desc in bad_queries:
        raised = False
        with provider.get_transactional_connection() as conn:
            txn = txn_cls(conn, provider)  # type: ignore[arg-type]
            try:
                txn.delete_records(_NS, bad_query)
            except ValueError:
                raised = True
        _check(
            raised,
            f"[{label}] txn delete_records rejects {desc} up-front (ValueError, "
            "no delete-all)",
        )
    # The guard must NOT have touched data: the full seed is still present.
    total = provider.aggregate(_NS, _TABLE, "count", None, {})
    _check(
        total == 5,
        f"[{label}] rejected deletes left the seed intact (count={total}, want 5)",
    )


def case_txn_delete_records(
    provider: AnyProvider, txn_cls: AnyTxn, label: str
) -> None:
    """D2: typed-txn delete_records soft (is_deleted=1) + hard (DELETE)."""
    # Soft delete the two status='a' rows -> rows-affected 2; they flip to
    # is_deleted=1 (still physically present).
    with provider.get_transactional_connection() as conn:
        txn = txn_cls(conn, provider)  # type: ignore[arg-type]
        soft_n = txn.delete_records(_NS, {"table": _TABLE, "filters": {"status": "a"}})
    after_soft = provider.aggregate(_NS, _TABLE, "count", None, {"status": "a", "is_deleted": 1})
    physical = provider.aggregate(_NS, _TABLE, "count", None, {"status": "a"})
    _check(
        soft_n == 2 and after_soft == 2 and physical == 2,
        f"[{label}] txn soft delete_records: rows-affected={soft_n}, both rows "
        f"now is_deleted=1 ({after_soft}), still physically present ({physical})",
    )
    # Hard delete the status='b' rows -> physically gone.
    with provider.get_transactional_connection() as conn:
        txn = txn_cls(conn, provider)  # type: ignore[arg-type]
        hard_n = txn.delete_records(
            _NS, {"table": _TABLE, "filters": {"status": "b"}, "soft_delete": False}
        )
    remaining = provider.aggregate(_NS, _TABLE, "count", None, {"status": "b"})
    _check(
        hard_n == 2 and remaining == 0,
        f"[{label}] txn hard delete_records: rows-affected={hard_n}, rows "
        f"physically gone (remaining={remaining})",
    )


def case_facade_envelope(provider: AnyProvider, label: str) -> None:
    """Autocommit facade surfaces the scalar at data.result.value (both twins)."""
    if isinstance(provider, PostgresProvider):
        count_result = run_aggregate(
            provider, _NS, {"table": _TABLE, "filters": {"is_deleted": 0}},
            op="count", requires_column=False, error_ns="count",
        )
        max_result = run_aggregate(
            provider, _NS, {"table": _TABLE, "column": "amount", "filters": {}},
            op="max", requires_column=True, error_ns="max_value",
        )
        ts_result = run_aggregate(
            provider, _NS, {"table": _TABLE, "column": "ts", "filters": {}},
            op="max", requires_column=True, error_ns="max_value",
        )
        bad = run_aggregate(
            provider, _NS, {"table": _TABLE, "filters": {}, "column": "amount"},
            op="count", requires_column=False, error_ns="count",
        )
    else:
        count_result = rds_state_count(
            provider, _NS, {"table": _TABLE, "filters": {"is_deleted": 0}}
        )
        max_result = rds_state_max_value(
            provider, _NS, {"table": _TABLE, "column": "amount", "filters": {}}
        )
        ts_result = rds_state_max_value(
            provider, _NS, {"table": _TABLE, "column": "ts", "filters": {}}
        )
        bad = rds_state_count(
            provider, _NS, {"table": _TABLE, "filters": {}, "column": "amount"}
        )
    cval = count_result.get("data", {}).get("result", {}).get("value")  # type: ignore[union-attr]
    mval = max_result.get("data", {}).get("result", {}).get("value")  # type: ignore[union-attr]
    tsval = ts_result.get("data", {}).get("result", {}).get("value")  # type: ignore[union-attr]
    _check(
        cval == 4 and mval == 40,
        f"[{label}] facade envelope: data.result.value (count={cval!r}, "
        f"max={mval!r})",
    )
    # Datetime envelope: a discoverable max over a TIMESTAMP column must put a
    # JSON-safe ISO-8601 string at data.result.value (NOT a raw naive datetime
    # the bridge would have to serialize at the delivery boundary). The
    # integer-amount case above can't surface this.
    _check(
        isinstance(tsval, str) and tsval == _TS5.isoformat(),
        f"[{label}] facade max over TIMESTAMP -> ISO-8601 string in "
        f"data.result.value, JSON-safe at the bridge boundary (got {tsval!r})",
    )
    _check(
        bad.get("error") is not None,
        f"[{label}] facade count + column -> error ActionResult, not a crash "
        f"(action_status={bad.get('action_status')!r}, error={bad.get('error')!r})",
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
    if os.environ.get("STATE_AGGREGATE_SMOKE") != "1":
        print(
            "  SKIP  STATE_AGGREGATE_SMOKE != 1; "
            "creates/drops sandbox schemas in the live DB.",
        )
        return 0

    local_schema = f"example_test_agg_local_{secrets.token_hex(4)}"
    rds_schema = f"example_test_agg_rds_{secrets.token_hex(4)}"
    local = _make_local(local_schema)
    rds = _make_rds(rds_schema)
    try:
        _create_probe_table(local, local_schema)
        _create_probe_table(rds, rds_schema)
        _seed(local)
        _seed(rds)

        # Value + filter-grammar + is_deleted + empty-set, both twins.
        local_battery = case_values(local, "local")
        rds_battery = case_values(rds, "rds")
        # Cross-twin parity: identical results for every labelled aggregate.
        for key in _EXPECTED:
            _check(
                local_battery[key] == rds_battery[key],
                f"cross-twin parity: {key} identical "
                f"(local={local_battery[key]!r}, rds={rds_battery[key]!r})",
            )

        # F1 TZ seam, both twins.
        case_tz_seam(local, "local")
        case_tz_seam(rds, "rds")

        # Validation fail-fast, both twins.
        case_validation(local, "local")
        case_validation(rds, "rds")

        # Typed-txn raw scalars, both txn classes.
        case_txn_aggregates(local, _PostgresStateTransaction, "local-txn")
        case_txn_aggregates(rds, RdsPostgresStateManagementTransaction, "rds-txn")

        # Facade envelope (data.result.value), both twins.
        case_facade_envelope(local, "local")
        case_facade_envelope(rds, "rds")

        # D2 fail-fast guard (raises before any SQL — seed stays intact).
        case_txn_delete_guard(local, _PostgresStateTransaction, "local-txn")
        case_txn_delete_guard(rds, RdsPostgresStateManagementTransaction, "rds-txn")
        # D2 typed-txn delete_records (soft + hard) LAST — it mutates the seed.
        case_txn_delete_records(local, _PostgresStateTransaction, "local-txn")
        case_txn_delete_records(rds, RdsPostgresStateManagementTransaction, "rds-txn")
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
