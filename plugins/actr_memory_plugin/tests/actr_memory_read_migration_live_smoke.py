#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the actr_memory G2 SQL-lockdown READ migration.

Pins the three migrated ``core__memory_events`` READ sites against a REAL
``PostgresProvider`` driven through the REAL ``PostgresStatePlugin`` (partial-
constructed with only its ``_provider`` set, so ``query_ordered`` /
``query_state`` / ``transactional()`` all run the genuine verb→provider path —
no hand-mirrored adapter that could drift from the plugin):

* SITE 1 — ``backend.get_recent_memory_structured`` :: the raw
  ``SELECT ... FROM core__memory_events ... ORDER BY timestamp DESC LIMIT`` is now
  ``query_ordered(core, memory_events, {filters}, order_by=[[timestamp,desc],
  [id,desc]], limit, unbounded=limit>100, include_deleted=True)``. Pins
  ordering (DESC then reversed → chronological ASC), ``limit`` (keeps the
  NEWEST), the ``max_age_hours`` half-open range op, ``namespace_filter`` /
  ``session_id`` equality filters, ``include_deleted=True`` (the original raw
  SQL applied no ``is_deleted`` filter), and the ``convert_memory_record``
  dict-branch ``metadata`` JSON parse (the equivalence fix — query_ordered
  returns dict rows, and the old dict branch returned RAW metadata).

* SITE 3 — ``session_query.query_event_summary`` :: the raw
  ``SELECT COUNT(*), MIN(timestamp), MAX(timestamp) WHERE session_id`` is now the
  transaction-scoped aggregates ``txn.count`` / ``txn.min_value`` /
  ``txn.max_value``. Pins the totals + the empty-session ``{0, None, None}``.

* SITE 4 — ``session_query.query_namespace_breakdown`` :: the raw
  ``SELECT source_namespace, COUNT(*) ... GROUP BY`` is now
  ``query_state(core, memory_events, {session_id})`` + a Python per-namespace
  fold. Pins the per-namespace counts AND the real ``session_id`` filtering
  (the breakdown of one session excludes another session's events).

Env-gated behind ``ACTR_READ_MIGRATION_LIVE_SMOKE=1`` (needs the live DB up; own
throwaway schema, dropped on exit).

Run::

    ACTR_READ_MIGRATION_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/actr_memory_plugin/tests/actr_memory_read_migration_live_smoke.py
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"),
)

# Pre-load config_manager (via importlib so the import sorter can't reorder it
# after the backend) to cache the deep plugin_contracts chain before
# ``ananta.utils`` initializes — avoids the utils↔config circular import when the
# backend / state plugin are imported standalone.
importlib.import_module("ananta.core.config.config_manager")
from actr_memory_plugin.backend import ACTRMemoryBackend  # noqa: E402
from actr_memory_plugin.session_query import (  # noqa: E402
    query_event_summary,
    query_namespace_breakdown,
)
from postgres_state_management_plugin.plugin import PostgresStatePlugin  # noqa: E402
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
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
    REPO_ROOT / "profile" / "config" / "plugins"
    / "postgres_state_management_plugin.json"
)


def _load_pg_config(schema_name: str) -> PostgresConfig:
    config = PostgresConfig(**json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8")))
    config.pg_schema = schema_name
    return config


def _state_service(provider: PostgresProvider) -> PostgresStatePlugin:
    """Partial-construct the REAL state plugin with only ``_provider`` set.

    ``_get_provider()`` returns a non-falsy ``_provider`` verbatim (no lazy
    config/keychain init), so ``query_ordered`` / ``query_state`` /
    ``transactional()`` run their genuine provider path against the throwaway
    schema.
    """
    plugin = object.__new__(PostgresStatePlugin)
    plugin._provider = provider  # noqa: SLF001 — partial-construct for the smoke
    return plugin


def _backend(provider: PostgresProvider) -> ACTRMemoryBackend:
    """Partial-construct the backend with only the ``state_service`` attribute
    the migrated read sites touch."""
    backend = object.__new__(ACTRMemoryBackend)
    backend.state_service = cast("Any", _state_service(provider))
    return backend


# Mirrors ananta/src/ananta/config/core_schemas.py get_memory_events_schema
# (business columns) + the auto-injected standard fields.
_DDL = (
    "id text PRIMARY KEY, session_id text NOT NULL, source_namespace text NOT NULL, "
    "event_type text NOT NULL, content text NOT NULL, metadata text, "
    "timestamp timestamp NOT NULL, "
    "is_deleted integer NOT NULL DEFAULT 0, "
    "created_at timestamp NOT NULL, updated_at timestamp NOT NULL"
)
_TABLE = "core__memory_events"
_BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None)


def _create_table(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'CREATE TABLE "{schema}"."{_TABLE}" ({_DDL})'))


def _seed_event(
    provider: PostgresProvider,
    schema: str,
    *,
    eid: str,
    session_id: str,
    source_namespace: str,
    ts: datetime,
    metadata: str | None = None,
    is_deleted: int = 0,
) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{_TABLE}" '
                "(id, session_id, source_namespace, event_type, content, metadata, "
                "timestamp, is_deleted, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ),
            (eid, session_id, source_namespace, "user_input", "hi", metadata, ts, is_deleted, ts, ts),
        )


def test_recent_memory_ordered_and_filtered(provider: PostgresProvider, schema: str) -> None:
    """SITE 1: query_ordered ordering / limit / range / equality filters / metadata-parse.

    Seeds are NOW-relative because ``build_memory_query``'s ``max_age_hours``
    cutoff is ``now - hours`` — a 1h cutoff lands between r2 (now-90m) and r3
    (now-30m).
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    sess = "sess-recent"
    _seed_event(provider, schema, eid="r1", session_id=sess, source_namespace="ns_a",
                ts=now - timedelta(hours=3), metadata='{"k": "v"}')
    _seed_event(provider, schema, eid="r2", session_id=sess, source_namespace="ns_a",
                ts=now - timedelta(hours=1, minutes=30), metadata='{"n": 2}')
    _seed_event(provider, schema, eid="r3", session_id=sess, source_namespace="ns_b",
                ts=now - timedelta(minutes=30), metadata=None)
    _seed_event(provider, schema, eid="r-other", session_id="sess-other",
                source_namespace="ns_a", ts=now - timedelta(minutes=10))

    recs = _backend(provider).get_recent_memory_structured(session_id=sess, max_events=20)
    ids = [r["id"] for r in recs]
    _check(ids == ["r1", "r2", "r3"],
           f"DESC query reversed → chronological ASC, session-scoped (got {ids})")
    _check(recs[0]["metadata"] == {"k": "v"} and recs[1]["metadata"] == {"n": 2},
           "metadata JSON PARSED per-row in the dict branch (the equivalence fix)")

    lim = _backend(provider).get_recent_memory_structured(session_id=sess, max_events=2)
    _check([r["id"] for r in lim] == ["r2", "r3"],
           f"limit=2 keeps the NEWEST two (DESC then reversed); got {[r['id'] for r in lim]}")

    aged = _backend(provider).get_recent_memory_structured(
        session_id=sess, max_events=20, max_age_hours=1,
    )
    _check({r["id"] for r in aged} == {"r3"},
           f"max_age_hours range op keeps only events newer than cutoff (got {sorted(r['id'] for r in aged)})")

    nsf = _backend(provider).get_recent_memory_structured(
        session_id=sess, max_events=20, namespace_filter="ns_b",
    )
    _check({r["id"] for r in nsf} == {"r3"},
           f"namespace_filter equality applied (got {sorted(r['id'] for r in nsf)})")


def test_recent_memory_include_deleted(provider: PostgresProvider, schema: str) -> None:
    """SITE 1: include_deleted=True preserves the original no-is_deleted-filter
    semantics — a soft-deleted event is still returned."""
    sess = "sess-incldel"
    _seed_event(provider, schema, eid="d1", session_id=sess, source_namespace="ns_a", ts=_BASE)
    _seed_event(provider, schema, eid="d2", session_id=sess, source_namespace="ns_a",
                ts=_BASE + timedelta(hours=1), is_deleted=1)
    recs = _backend(provider).get_recent_memory_structured(session_id=sess, max_events=20)
    _check({r["id"] for r in recs} == {"d1", "d2"},
           f"include_deleted=True returns the is_deleted=1 row too (got {sorted(r['id'] for r in recs)})")


def test_session_event_summary_aggregates(provider: PostgresProvider, schema: str) -> None:
    """SITE 3: txn count / min_value / max_value over a session."""
    sess = "sess-stats"
    _seed_event(provider, schema, eid="s1", session_id=sess, source_namespace="ns_a", ts=_BASE)
    _seed_event(provider, schema, eid="s2", session_id=sess, source_namespace="ns_b",
                ts=_BASE + timedelta(hours=5))
    _seed_event(provider, schema, eid="s3", session_id=sess, source_namespace="ns_a",
                ts=_BASE + timedelta(hours=2))
    _seed_event(provider, schema, eid="s-other", session_id="sess-x",
                source_namespace="ns_a", ts=_BASE + timedelta(hours=9))

    summary = query_event_summary(_state_service(provider), sess)
    _check(summary["total_events"] == 3,
           f"count scoped to the session (got {summary['total_events']})")
    _check(summary["oldest_event"] == _BASE,
           f"min_value(timestamp) = oldest (got {summary['oldest_event']})")
    _check(summary["newest_event"] == _BASE + timedelta(hours=5),
           f"max_value(timestamp) = newest (got {summary['newest_event']})")

    empty = query_event_summary(_state_service(provider), "no-such-session")
    _check(empty == {"total_events": 0, "oldest_event": None, "newest_event": None},
           f"empty session → count 0, min/max None (got {empty})")


def test_namespace_breakdown_group_count(provider: PostgresProvider, schema: str) -> None:
    """SITE 4: query_state + Python per-namespace fold, with real session filtering."""
    sess = "sess-bd"
    _seed_event(provider, schema, eid="b1", session_id=sess, source_namespace="ns_a", ts=_BASE)
    _seed_event(provider, schema, eid="b2", session_id=sess, source_namespace="ns_a",
                ts=_BASE + timedelta(minutes=1))
    _seed_event(provider, schema, eid="b3", session_id=sess, source_namespace="ns_b",
                ts=_BASE + timedelta(minutes=2))
    _seed_event(provider, schema, eid="b-other", session_id="sess-y",
                source_namespace="ns_c", ts=_BASE + timedelta(minutes=3))

    breakdown = query_namespace_breakdown(_state_service(provider), sess)
    _check(breakdown == {"ns_a": {"events": 2}, "ns_b": {"events": 1}},
           f"per-namespace counts correct AND other session's ns_c excluded (got {breakdown})")


def main() -> int:
    if os.environ.get("ACTR_READ_MIGRATION_LIVE_SMOKE") != "1":
        print("=== actr_memory_read_migration_live_smoke ===")
        print("  SKIP  set ACTR_READ_MIGRATION_LIVE_SMOKE=1 to run; needs the live solet DB.")
        return 0
    print("=== actr_memory_read_migration_live_smoke ===")
    schema_name = f"example_test_actrread_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_table(provider, schema_name)
        test_recent_memory_ordered_and_filtered(provider, schema_name)
        test_recent_memory_include_deleted(provider, schema_name)
        test_session_event_summary_aggregates(provider, schema_name)
        test_namespace_breakdown_group_count(provider, schema_name)
    finally:
        with provider.get_transactional_connection() as conn, conn.cursor() as cur:
            cur.execute(cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
