#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the #4 ``= ANY`` + is_null filter grammar.

Pins the sanctioned per-value WHERE grammar that
``PostgresProvider._build_filter_clauses`` compiles and that ``select`` /
``select_ordered`` / ``update`` all share (capability-primitive UNIT A,
2026-06-20). For each ``col: val`` the grammar is:

* scalar value          -> ``col = %s``           (behavior-preserving)
* list / tuple value    -> ``col = ANY(%s)``      (IN-list; empty -> 0 rows)
* ``{"op": "is_null"}`` -> ``col IS NULL``
* ``{"op": "is_not_null"}`` -> ``col IS NOT NULL``
* ``{"op": "lt"|"lte"|"gt"|"gte", "value": X}`` -> ``col <op> %s`` (Gap-A
  AND-range; a missing ``value`` fails loud)

Coverage model (surfaced, not silent):

* **Local postgres provider** gets FULL live behavioral coverage — a sandbox
  schema is created in a live local Postgres DB, fixture rows are seeded,
  and the grammar is exercised end-to-end through ``provider.select`` /
  ``select_ordered`` / ``update``.
* **RDS provider** grammar parity is proved here by rendering
  ``RdsProvider._build_filter_clauses`` (a staticmethod, no DB) byte-identical
  to the postgres provider for every grammar case. Live RDS *behavioral*
  coverage — the RDS provider/txn classes run against LOCAL Postgres, no cloud
  IAM required — lives in ``consolidated_fix_regression_smoke.py``.

Sandboxed via a temporary schema ``example_test_filter_grammar_<random>`` in the
live DB; the cleanup ``DROP SCHEMA CASCADE`` runs in a ``finally`` block so a
crash never leaves the schema behind. Env-gated behind ``FILTER_GRAMMAR_SMOKE=1``.

Run::

    FILTER_GRAMMAR_SMOKE=1 \\
      .venv/bin/python3 \\
      plugins/postgres_state_management_plugin/tests/filter_grammar_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
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

from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)
from rds_postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider as RdsProvider,
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

_NS = "grammar"
_TABLE = "probe"
_PHYSICAL = f"{_NS}__{_TABLE}"


def _load_pg_config(schema_name: str) -> PostgresConfig:
    raw = json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8"))
    config = PostgresConfig(**raw)
    config.pg_schema = schema_name
    return config


def _create_probe_table(provider: PostgresProvider, schema_name: str) -> None:
    """Create a minimal probe table under the sandbox schema (raw DDL setup)."""
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'CREATE TABLE "{schema_name}"."{_PHYSICAL}" ('
                "id text PRIMARY KEY, "
                "grp text NOT NULL, "
                "tag text, "
                "marker text, "
                "is_deleted integer NOT NULL DEFAULT 0, "
                "created_at timestamptz NOT NULL"
                ")",
            )
        )


# (id, grp, tag, is_deleted, created_at) — created_at distinct so ordering is
# deterministic; tag NULL on r3/r4 exercises the is_null grammar; r5 is
# soft-deleted so select_ordered's is_deleted=0 filter excludes it.
_SEED: tuple[tuple[str, str, str | None, int, str], ...] = (
    ("r1", "A", "x", 0, "2026-01-01T00:00:00Z"),
    ("r2", "B", "y", 0, "2026-01-02T00:00:00Z"),
    ("r3", "C", None, 0, "2026-01-03T00:00:00Z"),
    ("r4", "A", None, 0, "2026-01-04T00:00:00Z"),
    ("r5", "B", "z", 1, "2026-01-05T00:00:00Z"),
)


def _seed_rows(provider: PostgresProvider, schema_name: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        for row_id, grp, tag, is_deleted, created_at in _SEED:
            cur.execute(
                cast(
                    LiteralString,
                    f'INSERT INTO "{schema_name}"."{_PHYSICAL}" '
                    "(id, grp, tag, is_deleted, created_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                ),
                (row_id, grp, tag, is_deleted, created_at),
            )


def _ids(rows: list[dict[str, Any]]) -> set[str]:
    return {str(r["id"]) for r in rows}


def case_scalar_equality(provider: PostgresProvider) -> None:
    rows = provider.select(_NS, _TABLE, conditions={"grp": "A"})
    _check(_ids(rows) == {"r1", "r4"}, f"scalar grp='A' -> {{r1,r4}} (got {_ids(rows)})")


def case_list_any(provider: PostgresProvider) -> None:
    rows = provider.select(_NS, _TABLE, conditions={"grp": ["A", "B"]})
    _check(
        _ids(rows) == {"r1", "r2", "r4", "r5"},
        f"list grp IN [A,B] -> ANY match {{r1,r2,r4,r5}} (got {_ids(rows)})",
    )


def case_empty_list(provider: PostgresProvider) -> None:
    rows = provider.select(_NS, _TABLE, conditions={"grp": []})
    _check(
        rows == [],
        f"empty list grp IN [] -> 0 rows, no SQL error (got {len(rows)} rows)",
    )


def case_is_null(provider: PostgresProvider) -> None:
    rows = provider.select(_NS, _TABLE, conditions={"tag": {"op": "is_null"}})
    _check(_ids(rows) == {"r3", "r4"}, f"tag IS NULL -> {{r3,r4}} (got {_ids(rows)})")


def case_is_not_null(provider: PostgresProvider) -> None:
    rows = provider.select(_NS, _TABLE, conditions={"tag": {"op": "is_not_null"}})
    _check(
        _ids(rows) == {"r1", "r2", "r5"},
        f"tag IS NOT NULL -> {{r1,r2,r5}} (got {_ids(rows)})",
    )


def case_unsupported_op_raises(provider: PostgresProvider) -> None:
    try:
        provider.select(_NS, _TABLE, conditions={"tag": {"op": "like"}})
    except ValueError:
        _check(True, "unsupported op 'like' raises ValueError (fast-fail)")
        return
    _check(False, "unsupported op 'like' did NOT raise (expected ValueError)")


def case_comparison_ops(provider: PostgresProvider) -> None:
    """Gap-A AND-range comparison ops on the timestamptz created_at column."""
    cutoff = "2026-01-03T00:00:00Z"
    rows = provider.select(_NS, _TABLE, conditions={"created_at": {"op": "gte", "value": cutoff}})
    _check(_ids(rows) == {"r3", "r4", "r5"}, f"created_at >= Jan3 -> {{r3,r4,r5}} (got {_ids(rows)})")
    rows = provider.select(_NS, _TABLE, conditions={"created_at": {"op": "gt", "value": cutoff}})
    _check(_ids(rows) == {"r4", "r5"}, f"created_at > Jan3 -> {{r4,r5}} (got {_ids(rows)})")
    rows = provider.select(_NS, _TABLE, conditions={"created_at": {"op": "lt", "value": "2026-01-02T00:00:00Z"}})
    _check(_ids(rows) == {"r1"}, f"created_at < Jan2 -> {{r1}} (got {_ids(rows)})")
    rows = provider.select(_NS, _TABLE, conditions={"created_at": {"op": "lte", "value": "2026-01-02T00:00:00Z"}})
    _check(_ids(rows) == {"r1", "r2"}, f"created_at <= Jan2 -> {{r1,r2}} (got {_ids(rows)})")
    # AND-range + scalar combine: created_at >= Jan2 AND grp = 'A' -> {r4}.
    rows = provider.select(
        _NS, _TABLE,
        conditions={"created_at": {"op": "gte", "value": "2026-01-02T00:00:00Z"}, "grp": "A"},
    )
    _check(_ids(rows) == {"r4"}, f"created_at>=Jan2 AND grp=A -> {{r4}} (got {_ids(rows)})")


def case_comparison_missing_value_raises(provider: PostgresProvider) -> None:
    try:
        provider.select(_NS, _TABLE, conditions={"created_at": {"op": "gt"}})
    except ValueError:
        _check(True, "comparison op with no 'value' raises ValueError (fast-fail)")
        return
    _check(False, "comparison op missing 'value' did NOT raise (expected ValueError)")


def case_ordered_comparison_range(provider: PostgresProvider) -> None:
    """select_ordered with an AND-range comparison + is_deleted=0 default."""
    rows = provider.select_ordered(
        _NS,
        _TABLE,
        conditions={"created_at": {"op": "gte", "value": "2026-01-02T00:00:00Z"}},
        order_columns=("created_at", "id"),
        direction="asc",
        limit=10,
    )
    ordered_ids = [str(r["id"]) for r in rows]
    _check(
        ordered_ids == ["r2", "r3", "r4"],
        f"select_ordered created_at>=Jan2 + is_deleted=0 asc -> [r2,r3,r4] "
        f"(got {ordered_ids}; r5 excluded as soft-deleted)",
    )


def case_ordered_list_with_is_deleted(provider: PostgresProvider) -> None:
    """select_ordered: grp IN [A,B] AND is_deleted=0, ordered by (created_at,id)."""
    rows = provider.select_ordered(
        _NS,
        _TABLE,
        conditions={"grp": ["A", "B"]},
        order_columns=("created_at", "id"),
        direction="asc",
        limit=10,
    )
    ordered_ids = [str(r["id"]) for r in rows]
    _check(
        ordered_ids == ["r1", "r2", "r4"],
        f"select_ordered grp IN [A,B] + is_deleted=0 asc -> [r1,r2,r4] "
        f"(got {ordered_ids}; r5 excluded as soft-deleted)",
    )


def case_ordered_ledger_shape(provider: PostgresProvider) -> None:
    """The ledger ``id IN (...) AND is_deleted=0`` consumer shape."""
    rows = provider.select_ordered(
        _NS,
        _TABLE,
        conditions={"id": ["r1", "r3", "r5"]},
        order_columns=("created_at", "id"),
        direction="asc",
        limit=10,
    )
    _check(
        _ids(rows) == {"r1", "r3"},
        f"select_ordered id IN [r1,r3,r5] + is_deleted=0 -> {{r1,r3}} "
        f"(got {_ids(rows)}; r5 deleted)",
    )


def case_update_list_where(provider: PostgresProvider) -> None:
    """update WHERE grp = ANY([A,B]) — the W3 status-set CAS shape."""
    affected = provider.update(
        _NS, _TABLE, conditions={"grp": ["A", "B"]}, updates={"marker": "g"}
    )
    _check(affected == 4, f"update grp IN [A,B] affects 4 rows (got {affected})")
    rows = provider.select(_NS, _TABLE, conditions={"marker": "g"})
    _check(
        _ids(rows) == {"r1", "r2", "r4", "r5"},
        f"marker='g' landed on {{r1,r2,r4,r5}} (got {_ids(rows)})",
    )


def case_update_is_null_where(provider: PostgresProvider) -> None:
    """update WHERE tag IS NULL — is_null grammar on the update path."""
    affected = provider.update(
        _NS, _TABLE, conditions={"tag": {"op": "is_null"}}, updates={"marker": "n"}
    )
    _check(affected == 2, f"update tag IS NULL affects 2 rows (got {affected})")
    rows = provider.select(_NS, _TABLE, conditions={"marker": "n"})
    _check(_ids(rows) == {"r3", "r4"}, f"marker='n' landed on {{r3,r4}} (got {_ids(rows)})")


def case_update_scalar_where(provider: PostgresProvider) -> None:
    """update WHERE grp = 'C' — scalar equality unchanged on the update path."""
    affected = provider.update(
        _NS, _TABLE, conditions={"grp": "C"}, updates={"marker": "c"}
    )
    _check(affected == 1, f"update grp='C' affects 1 row (got {affected})")


def case_update_comparison_where(provider: PostgresProvider) -> None:
    """update WHERE created_at >= X — Gap-A comparison grammar on the update path."""
    affected = provider.update(
        _NS,
        _TABLE,
        conditions={"created_at": {"op": "gte", "value": "2026-01-04T00:00:00Z"}},
        updates={"marker": "r"},
    )
    _check(affected == 2, f"update created_at>=Jan4 affects 2 rows (got {affected})")
    rows = provider.select(_NS, _TABLE, conditions={"marker": "r"})
    _check(_ids(rows) == {"r4", "r5"}, f"marker='r' landed on {{r4,r5}} (got {_ids(rows)})")


_PARITY_CASES: tuple[dict[str, Any], ...] = (
    {"grp": "A"},
    {"grp": ["A", "B"]},
    {"grp": []},
    {"tag": {"op": "is_null"}},
    {"tag": {"op": "is_not_null"}},
    {"id": ["r1", "r3"], "is_deleted": 0},
    {"a": "scalar", "b": ["x", "y", "z"], "c": {"op": "is_null"}},
    # Gap-A comparison ops — all four operators + a combined range/scalar mix.
    {"created_at": {"op": "lt", "value": "2026-01-02T00:00:00Z"}},
    {"created_at": {"op": "lte", "value": "2026-01-02T00:00:00Z"}},
    {"seq": {"op": "gt", "value": 5}},
    {"seq": {"op": "gte", "value": 5}},
    {"called_at": {"op": "gte", "value": "2026-01-01T00:00:00Z"}, "grp": "A", "is_deleted": 0},
)


def case_rds_grammar_parity(provider: PostgresProvider) -> None:
    """Prove RDS ``_build_filter_clauses`` is byte-identical to postgres.

    Both helpers are staticmethods, so no DB is required to build the clauses;
    they are rendered to text via the live postgres connection (an AdaptContext)
    purely to compare SQL strings. Any drift between the two lockstep
    implementations fails here.
    """
    with provider.get_connection() as conn:
        for conditions in _PARITY_CASES:
            pg_clauses, pg_params = PostgresProvider._build_filter_clauses(conditions)
            rds_clauses, rds_params = RdsProvider._build_filter_clauses(conditions)
            pg_rendered = [c.as_string(conn) for c in pg_clauses]
            rds_rendered = [c.as_string(conn) for c in rds_clauses]
            _check(
                pg_rendered == rds_rendered and pg_params == rds_params,
                f"RDS parity for {conditions!r}: SQL+params identical "
                f"(pg={pg_rendered}/{pg_params} rds={rds_rendered}/{rds_params})",
            )


def _drop_schema(provider: PostgresProvider, schema_name: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        )


def main() -> int:
    if os.environ.get("FILTER_GRAMMAR_SMOKE") != "1":
        print(
            "  SKIP  FILTER_GRAMMAR_SMOKE != 1; "
            "this smoke creates and drops a sandbox schema in the live DB.",
        )
        return 0
    schema_name = f"example_test_filter_grammar_{secrets.token_hex(4)}"
    config = _load_pg_config(schema_name)
    provider = PostgresProvider(config)
    provider.initialize()  # creates the sandbox schema + trigger fn
    try:
        _create_probe_table(provider, schema_name)
        _seed_rows(provider, schema_name)
        # Read-only grammar cases first.
        case_scalar_equality(provider)
        case_list_any(provider)
        case_empty_list(provider)
        case_is_null(provider)
        case_is_not_null(provider)
        case_unsupported_op_raises(provider)
        case_comparison_ops(provider)
        case_comparison_missing_value_raises(provider)
        case_ordered_list_with_is_deleted(provider)
        case_ordered_ledger_shape(provider)
        case_ordered_comparison_range(provider)
        # RDS composition-parity (no mutation).
        case_rds_grammar_parity(provider)
        # Mutating update cases last.
        case_update_list_where(provider)
        case_update_is_null_where(provider)
        case_update_scalar_where(provider)
        case_update_comparison_where(provider)
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
