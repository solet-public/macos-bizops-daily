#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the #3 conflict-predicate upsert.

Pins ``provider.upsert_conditional`` + ``_build_conflict_predicate`` +
``_build_upsert_sql(on_conflict_do_nothing=True)`` — the
``upsert_state(on_conflict="do_nothing", conflict_predicate=[...])`` path
(capability-primitive UNIT B, 2026-06-20). The target is the ledger's
two-phase canonical dispatch: ``INSERT ... ON CONFLICT (vendor,
external_session_id) WHERE canonical_external_session_id IS NULL AND
is_deleted = 0 DO NOTHING``.

**Predicate replicates the real migrated index verbatim.** The sandbox table
carries a UNIQUE index copied from the live platform's ``session_ledger__session``
partial-unique index (``CREATE UNIQUE INDEX ... (vendor, external_session_id)
WHERE ((canonical_external_session_id IS NULL) AND (is_deleted = 0))``). The
replica is asserted to MATCH the live index — introspected from ``pg_indexes``
so drift fails loudly — in ``consolidated_fix_regression_smoke.py``. This is the
case that catches the ``= $1`` vs ``= 0`` trap: Postgres' ON CONFLICT arbiter
inference matches a partial index's WHERE only against a constant-folded
predicate, so the ``eq`` op MUST emit ``sql.Literal`` (``is_deleted = 0``), not
a bind placeholder (``is_deleted = $1`` would fail inference in production).
The positive cases below would raise ``42P10 no unique or exclusion constraint
matching the ON CONFLICT specification`` if the predicate were placeholder-bound
or incomplete — the explicit negative case proves the full predicate is
load-bearing.

Coverage model (surfaced, not silent): the postgres provider gets full live
coverage; the RDS provider's predicate compiler is proved byte-identical via
``_build_conflict_predicate`` rendering (staticmethod). Live RDS *behavioral*
coverage (RDS provider/txn against LOCAL Postgres, no cloud IAM required) +
malformed-predicate facade negatives live in
``consolidated_fix_regression_smoke.py``.

Sandboxed via a temporary schema; cleanup drops it in a ``finally``. Env-gated
behind ``CONFLICT_PREDICATE_SMOKE=1``.

Run::

    CONFLICT_PREDICATE_SMOKE=1 \\
      .venv/bin/python3 \\
      plugins/postgres_state_management_plugin/tests/conflict_predicate_upsert_smoke.py
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

import psycopg  # noqa: E402
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

# Mirror the ledger session shape; namespace/table chosen so build_table_name
# yields the real physical name `session_ledger__session`.
_NS = "session_ledger"
_TABLE = "session"
_PHYSICAL = f"{_NS}__{_TABLE}"
_PLAIN_NS = "plain"
_PLAIN_TABLE = "kv"
_PLAIN_PHYSICAL = f"{_PLAIN_NS}__{_PLAIN_TABLE}"

# The structured predicate that must match the real partial-unique index.
_LEDGER_PREDICATE: list[dict[str, Any]] = [
    {"column": "canonical_external_session_id", "op": "is_null"},
    {"column": "is_deleted", "op": "eq", "value": 0},
]
_CONFLICT_COLUMNS = ["vendor", "external_session_id"]


def _load_pg_config(schema_name: str) -> PostgresConfig:
    raw = json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8"))
    config = PostgresConfig(**raw)
    config.pg_schema = schema_name
    return config


def _create_ledger_like_table(provider: PostgresProvider, schema_name: str) -> None:
    """Sandbox table + the REAL partial-unique index (verbatim predicate)."""
    idx_name = f"{_PHYSICAL}__idx_canonical_one_per_{secrets.token_hex(3)}"
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'CREATE TABLE "{schema_name}"."{_PHYSICAL}" ('
                "id text PRIMARY KEY, "
                "vendor text NOT NULL, "
                "external_session_id text NOT NULL, "
                "canonical_external_session_id text, "
                "is_deleted integer NOT NULL DEFAULT 0"
                ")",
            )
        )
        cur.execute(
            cast(
                LiteralString,
                f'CREATE UNIQUE INDEX "{idx_name}" ON '
                f'"{schema_name}"."{_PHYSICAL}" (vendor, external_session_id) '
                "WHERE ((canonical_external_session_id IS NULL) "
                "AND (is_deleted = 0))",
            )
        )


def _create_plain_unique_table(provider: PostgresProvider, schema_name: str) -> None:
    """A plain (non-partial) UNIQUE table for the no-predicate DO NOTHING path."""
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'CREATE TABLE "{schema_name}"."{_PLAIN_PHYSICAL}" ('
                "id text PRIMARY KEY, "
                "k text NOT NULL UNIQUE, "
                "v text"
                ")",
            )
        )


def case_first_insert_returns_inserted(provider: PostgresProvider) -> None:
    inserted, row_id = provider.upsert_conditional(
        _NS,
        _TABLE,
        data={
            "id": "s1",
            "vendor": "v1",
            "external_session_id": "e1",
            "canonical_external_session_id": None,
            "is_deleted": 0,
        },
        conflict_columns=_CONFLICT_COLUMNS,
        conflict_predicate=_LEDGER_PREDICATE,
    )
    _check(
        inserted is True and row_id == "s1",
        f"first insert -> (inserted=True, id='s1') (got {inserted!r}, {row_id!r}); "
        "ON CONFLICT inference matched the real partial-unique index",
    )


def case_conflicting_insert_skipped(provider: PostgresProvider) -> None:
    inserted, row_id = provider.upsert_conditional(
        _NS,
        _TABLE,
        data={
            "id": "s1_dup",
            "vendor": "v1",
            "external_session_id": "e1",
            "canonical_external_session_id": None,
            "is_deleted": 0,
        },
        conflict_columns=_CONFLICT_COLUMNS,
        conflict_predicate=_LEDGER_PREDICATE,
    )
    _check(
        inserted is False and row_id is None,
        f"conflicting insert -> (inserted=False, id=None) (got {inserted!r}, "
        f"{row_id!r}); DO NOTHING skipped the duplicate",
    )


def case_non_conflicting_insert(provider: PostgresProvider) -> None:
    inserted, row_id = provider.upsert_conditional(
        _NS,
        _TABLE,
        data={
            "id": "s2",
            "vendor": "v1",
            "external_session_id": "e2",
            "canonical_external_session_id": None,
            "is_deleted": 0,
        },
        conflict_columns=_CONFLICT_COLUMNS,
        conflict_predicate=_LEDGER_PREDICATE,
    )
    _check(
        inserted is True and row_id == "s2",
        f"distinct external_session_id -> (inserted=True, id='s2') "
        f"(got {inserted!r}, {row_id!r})",
    )


def case_incomplete_predicate_fails_inference(provider: PostgresProvider) -> None:
    """A predicate that doesn't imply the index's full WHERE must fail inference.

    Proves the complete predicate is load-bearing: ``canonical IS NULL`` alone
    does not imply ``canonical IS NULL AND is_deleted = 0``, so Postgres cannot
    select the partial index as arbiter (SQLSTATE 42P10).
    """
    try:
        provider.upsert_conditional(
            _NS,
            _TABLE,
            data={
                "id": "s3",
                "vendor": "v9",
                "external_session_id": "e9",
                "canonical_external_session_id": None,
                "is_deleted": 0,
            },
            conflict_columns=_CONFLICT_COLUMNS,
            conflict_predicate=[
                {"column": "canonical_external_session_id", "op": "is_null"},
            ],
        )
    except psycopg.Error:
        _check(True, "incomplete predicate -> ON CONFLICT inference fails (42P10)")
        return
    _check(
        False,
        "incomplete predicate did NOT raise (expected no-matching-constraint error)",
    )


def case_unsupported_op_raises(provider: PostgresProvider) -> None:
    try:
        provider.upsert_conditional(
            _NS,
            _TABLE,
            data={"id": "s4", "vendor": "v8", "external_session_id": "e8"},
            conflict_columns=_CONFLICT_COLUMNS,
            conflict_predicate=[{"column": "is_deleted", "op": "lt", "value": 1}],
        )
    except ValueError:
        _check(True, "unsupported predicate op 'lt' raises ValueError (fast-fail)")
        return
    _check(False, "unsupported predicate op 'lt' did NOT raise (expected ValueError)")


def case_no_predicate_do_nothing(provider: PostgresProvider) -> None:
    """No-predicate DO NOTHING against a plain UNIQUE constraint."""
    first = provider.upsert_conditional(
        _PLAIN_NS,
        _PLAIN_TABLE,
        data={"id": "p1", "k": "k1", "v": "a"},
        conflict_columns=["k"],
    )
    dup = provider.upsert_conditional(
        _PLAIN_NS,
        _PLAIN_TABLE,
        data={"id": "p2", "k": "k1", "v": "b"},
        conflict_columns=["k"],
    )
    _check(
        first == (True, "p1") and dup == (False, None),
        f"plain DO NOTHING: first {first!r} then conflict {dup!r}",
    )


_EXPECTED_PREDICATE_SQL = (
    '"canonical_external_session_id" IS NULL AND "is_deleted" = 0'
)


def case_predicate_sql_uses_literal(provider: PostgresProvider) -> None:
    """The compiled predicate is constant-folded (literal 0, not a placeholder)."""
    with provider.get_connection() as conn:
        rendered = PostgresProvider._build_conflict_predicate(
            _LEDGER_PREDICATE
        ).as_string(conn)
    _check(
        rendered == _EXPECTED_PREDICATE_SQL,
        f"predicate compiles to literal form {_EXPECTED_PREDICATE_SQL!r} "
        f"(got {rendered!r})",
    )


def case_rds_predicate_parity(provider: PostgresProvider) -> None:
    """RDS ``_build_conflict_predicate`` is byte-identical to postgres."""
    cases: tuple[list[dict[str, Any]], ...] = (
        _LEDGER_PREDICATE,
        [{"column": "x", "op": "is_not_null"}],
        [{"column": "a", "op": "eq", "value": "lit"}],
    )
    with provider.get_connection() as conn:
        for predicate in cases:
            pg = PostgresProvider._build_conflict_predicate(predicate).as_string(conn)
            rds = RdsProvider._build_conflict_predicate(predicate).as_string(conn)
            _check(
                pg == rds,
                f"RDS predicate parity for {predicate!r}: {pg!r} == {rds!r}",
            )


def _drop_schema(provider: PostgresProvider, schema_name: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        )


def main() -> int:
    if os.environ.get("CONFLICT_PREDICATE_SMOKE") != "1":
        print(
            "  SKIP  CONFLICT_PREDICATE_SMOKE != 1; "
            "this smoke creates and drops a sandbox schema in the live DB.",
        )
        return 0
    schema_name = f"example_test_conflict_predicate_{secrets.token_hex(4)}"
    config = _load_pg_config(schema_name)
    provider = PostgresProvider(config)
    provider.initialize()  # creates the sandbox schema + trigger fn
    try:
        _create_ledger_like_table(provider, schema_name)
        _create_plain_unique_table(provider, schema_name)
        case_first_insert_returns_inserted(provider)
        case_conflicting_insert_skipped(provider)
        case_non_conflicting_insert(provider)
        case_incomplete_predicate_fails_inference(provider)
        case_unsupported_op_raises(provider)
        case_no_predicate_do_nothing(provider)
        case_predicate_sql_uses_literal(provider)
        case_rds_predicate_parity(provider)
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
