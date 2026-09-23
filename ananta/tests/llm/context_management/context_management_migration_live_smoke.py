#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the context_management migration (SQL lockdown #0, core Slice-1).

Pins the 27 raw ``execute_sql`` sites across ``context_event_store`` /
``context_snapshot_store`` / ``context_sessions`` migrated onto the
``StateManagementInterface`` primitives (``query_state`` / ``query_ordered`` /
``update_state`` / ``delete_records``) against a REAL ``PostgresProvider`` — the
migration's real-schema mandate (the thin in-memory fake does NOT model
filter/order/cap/JSONB-parse/trigger semantics).

Why a live harness (build-map §6): the migrated methods order on ``created_at``,
read JSONB ``metadata`` back as a dict, paginate by a ``(created_at, id)``
row-value cursor, and lean on the table's BEFORE-UPDATE ``updated_at`` trigger.
Each is invisible to a fake-state unit.

The load-bearing assertions:

* **No silent 100-cap truncation.** ``list_all_events`` / ``list_events_since``
  read >100 rows via uncapped ``query_state`` + Python sort — ``query_ordered``
  (cap 100) would have silently truncated. Seeds 150 events and proves all 150
  come back in order.
* **Cursor pagination** reproduces the ``(created_at, id) > (last, last_id)``
  row-value comparison the equality grammar cannot express.
* **JSONB ``metadata``** comes back as a dict (``contains_process_keys`` extracted).
* **``updated_at`` moves** on the migrated ``update_state`` (trigger), since the
  explicit ``updated_at = now`` set was dropped.
* Each migrated read/delete equals a ground-truth raw query over the same rows.

WHY A SANDBOX SCHEMA: the delete/update verbs mutate rows; the smoke builds
``core__context_{events,snapshots,sessions}``-shaped tables (TIMESTAMP cols,
integer ``is_deleted``, the standard ``updated_at`` trigger) in a throwaway
schema, seeds the exact corpus each verb targets, drives the MIGRATED store
methods through a faithful adapter over a real provider, and DROPs the schema
in a ``finally``.

Env-gated behind ``CONTEXT_MGMT_LIVE_SMOKE=1`` (needs the live DB up; writes
only to its own throwaway schema).

Run::

    CONTEXT_MGMT_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/context_management/context_management_migration_live_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.services.context_management.context_event_store import (  # noqa: E402
    ContextEventStore,
)
from ananta.services.context_management.context_sessions import (  # noqa: E402
    ContextSessionRegistry,
)
from ananta.services.context_management.context_snapshot_store import (  # noqa: E402
    ContextSnapshotStore,
)
from ananta.services.state_service.ordered_query import (  # noqa: E402
    parse_ordered_query,
)
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


def _envelope(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "action_status": "completed",
        "data": {"records": rows, "count": len(rows)},
        "actions": [],
        "error": None,
        "timestamp": "",
    }


class _LiveStateAdapter:
    """Faithful StateManagementInterface stand-in mirroring the plugin facade 1:1.

    ``read_state`` / ``query_state`` → ``provider.select`` (the autocommit
    equality / ``= ANY`` / ``is_null`` grammar); ``query_ordered`` → the real
    ``parse_ordered_query`` hardening + ``provider.select_ordered``;
    ``update_state`` → ``provider.update`` (rows-affected); ``delete_records`` →
    ``provider.delete`` (soft by default). The migrated stores exercise the
    actual SQL-composition + cap + JSONB-parse path, not a reimplementation.
    """

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else None,
            limit=cast("int | None", query.get("limit")),
        )
        return _envelope(rows)

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        # StateService.query_state's second positional/keyword is named ``filters``
        # but carries the same {table, filters, limit?} query dict read_state takes.
        return self.read_state(namespace, filters)

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        spec = parse_ordered_query(data)
        rows = self._provider.select_ordered(
            namespace=namespace,
            table=spec.table,
            conditions=spec.filters,
            order_columns=spec.order_columns,
            direction=spec.direction,
            limit=spec.limit,
            after=spec.after,
            include_deleted=spec.include_deleted,
        )
        return _envelope(rows)

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        filters = query.get("filters") or {}
        affected = self._provider.update(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters),
            updates=updates,
        )
        return {
            "action_status": "completed",
            "data": {"namespace": namespace, "result": {"updated": affected}},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        deleted = self._provider.delete(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters),
            soft_delete=bool(query.get("soft_delete", True)),
        )
        return {
            "action_status": "completed",
            "data": {"namespace": namespace, "result": {"deleted": deleted}},
            "actions": [],
            "error": None,
            "timestamp": "",
        }


# ─── Sandbox DDL ─────────────────────────────────────────────────────────────

_DDL: tuple[tuple[str, str], ...] = (
    (
        "core__context_events",
        "id text PRIMARY KEY, context_id text NOT NULL, event_type text NOT NULL, "
        "actor_type text, actor_id text, content_path text, "
        "content_char_count integer, metadata jsonb, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "core__context_snapshots",
        "id text PRIMARY KEY, context_id text NOT NULL, start_event_id text, "
        "end_event_id text, summary_path text, summary_char_count integer, "
        "original_char_count integer, cache_key text, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "core__context_sessions",
        "id text PRIMARY KEY, context_id text NOT NULL, provider text, "
        "context_mode text, last_event_id text, last_event_created_at text, "
        "cache_state text, is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
)


def _create_trigger_function(provider: PostgresProvider, schema: str) -> None:
    """Replicate the platform updated_at trigger function in the sandbox schema."""
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(
            LiteralString,
            f'CREATE OR REPLACE FUNCTION "{schema}".update_updated_at_column() '
            "RETURNS TRIGGER AS $$ BEGIN "
            "NEW.updated_at = (NOW() AT TIME ZONE 'UTC'); RETURN NEW; "
            "END; $$ LANGUAGE plpgsql;",
        ))


def _create_tables(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        for table, body in _DDL:
            cur.execute(cast(LiteralString, f'CREATE TABLE "{schema}"."{table}" ({body})'))
            cur.execute(cast(
                LiteralString,
                f'CREATE TRIGGER "{table}_update_updated_at" BEFORE UPDATE ON '
                f'"{schema}"."{table}" FOR EACH ROW '
                f'EXECUTE FUNCTION "{schema}".update_updated_at_column();',
            ))


def _insert(provider: PostgresProvider, schema: str, table: str, row: dict[str, object]) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    col_csv = ", ".join(f'"{c}"' for c in cols)
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{table}" ({col_csv}) VALUES ({placeholders})',
            ),
            tuple(row[c] for c in cols),
        )


def _raw(provider: PostgresProvider, sql: str, params: tuple[object, ...] = ()) -> list[list[Any]]:
    return provider.execute_query(sql, params)


def _scalar(provider: PostgresProvider, schema: str, table: str, col: str, row_id: str) -> object:
    rows = _raw(provider, f'SELECT "{col}" FROM "{schema}"."{table}" WHERE id = %s', (row_id,))
    return rows[0][0] if rows else "<<absent>>"


_SEED_AT = "2026-06-01T00:00:00"


def _seed_event(
    provider: PostgresProvider,
    schema: str,
    *,
    event_id: str,
    context_id: str,
    created_at: str,
    event_type: str = "input",
    is_deleted: int = 0,
    metadata: dict[str, object] | None = None,
) -> None:
    _insert(provider, schema, "core__context_events", {
        "id": event_id,
        "context_id": context_id,
        "event_type": event_type,
        "actor_type": "agent",
        "content_path": f"events/{event_id}.txt",
        "content_char_count": 1,
        "metadata": json.dumps(metadata) if metadata is not None else None,
        "is_deleted": is_deleted,
        "created_at": created_at,
        "updated_at": _SEED_AT,
    })


# ─── Cases ───────────────────────────────────────────────────────────────────


def test_no_silent_cap_truncation(store: ContextEventStore, provider: PostgresProvider, schema: str) -> None:
    """list_all_events / list_events_since read >100 rows (no query_ordered cap)."""
    ctx = "ctx_bulk"
    for i in range(150):
        _seed_event(
            provider, schema,
            event_id=f"ctxe-{i:04d}", context_id=ctx,
            created_at=f"2026-06-01T00:{i // 60:02d}:{i % 60:02d}",
        )
    raw_ids = [str(r[0]) for r in _raw(
        provider,
        f'SELECT id FROM "{schema}".core__context_events '
        "WHERE context_id = %s AND is_deleted = 0 ORDER BY created_at ASC, id ASC",
        (ctx,),
    )]
    all_events = store.list_all_events(ctx)
    _check(len(all_events) == 150, f"list_all_events returns all 150 (NOT capped at 100); got {len(all_events)}")
    _check([str(e["id"]) for e in all_events] == raw_ids, "list_all_events order matches raw ORDER BY created_at, id")

    since_all = store.list_events_since(ctx, limit=1000)
    _check(len(since_all) == 150, f"list_events_since(limit=1000) returns all 150 (NOT capped); got {len(since_all)}")
    _check([str(e["id"]) for e in since_all] == raw_ids, "list_events_since(no cursor) order matches raw")


def test_cursor_pagination(store: ContextEventStore) -> None:
    """list_events_since reproduces the (created_at, id) row-value cursor."""
    ctx = "ctx_bulk"  # reuse the 150-event corpus seeded by the cap-truncation test
    full = store.list_all_events(ctx)
    cursor_row = full[49]  # 50th event
    after = store.list_events_since(
        ctx,
        last_created_at=str(cursor_row["created_at"]),
        last_id=str(cursor_row["id"]),
        limit=1000,
    )
    expected_ids = [str(e["id"]) for e in full[50:]]
    _check([str(e["id"]) for e in after] == expected_ids, f"cursor after 50th yields events 51..150 in order ({len(after)} rows)")

    # A bounded page honors the limit and starts strictly after the cursor.
    page = store.list_events_since(
        ctx,
        last_created_at=str(cursor_row["created_at"]),
        last_id=str(cursor_row["id"]),
        limit=10,
    )
    _check([str(e["id"]) for e in page] == expected_ids[:10], "bounded page (limit=10) == first 10 after cursor")


def test_cursor_equivalent_spelling_regression(store: ContextEventStore, provider: PostgresProvider, schema: str) -> None:
    """Codex MAJOR-1: a cursor whose ISO SPELLING differs from the stored cell's
    spelling but denotes the SAME instant must NOT drop equal-instant rows.

    Two rows at the same instant 2026-06-08T00:00:00 (the provider serializes
    both as '...T00:00:00'). A caller-supplied cursor can carry an equivalent but
    DIFFERENT spelling — '...T00:00:00.000000' (microsecond-padded) or
    '...T00:00:00+00:00' (tz-aware). The old lexical string compare treated those
    as greater than '...T00:00:00' and silently dropped sp2; coerce-to-value keeps
    it.
    """
    ctx = "ctx_spell"
    _seed_event(provider, schema, event_id="ctxe-sp1", context_id=ctx, created_at="2026-06-08T00:00:00")
    _seed_event(provider, schema, event_id="ctxe-sp2", context_id=ctx, created_at="2026-06-08T00:00:00")
    # Ground truth: raw SQL coerces both spellings to one TIMESTAMP value.
    raw = [str(r[0]) for r in _raw(
        provider,
        f'SELECT id FROM "{schema}".core__context_events '
        "WHERE context_id = %s AND is_deleted = 0 "
        "AND (created_at > %s OR (created_at = %s AND id > %s)) "
        "ORDER BY created_at ASC, id ASC",
        (ctx, "2026-06-08T00:00:00", "2026-06-08T00:00:00", "ctxe-sp1"),
    )]
    _check(raw == ["ctxe-sp2"], f"raw SQL (value semantics) returns [ctxe-sp2] after the equal-instant cursor; got {raw}")
    for label, spelling in (("microsecond-padded", "2026-06-08T00:00:00.000000"),
                            ("tz-aware +00:00", "2026-06-08T00:00:00+00:00")):
        got = [str(e["id"]) for e in store.list_events_since(ctx, last_created_at=spelling, last_id="ctxe-sp1", limit=100)]
        _check(got == ["ctxe-sp2"],
               f"divergent cursor spelling ({label}) does NOT drop the equal-instant row; got {got}")


def test_tie_break_and_precision(store: ContextEventStore, provider: PostgresProvider, schema: str) -> None:
    """Same-microsecond events tie-break on id; variable-precision timestamps order by value."""
    ctx = "ctx_tie"
    _seed_event(provider, schema, event_id="ctxe-tieB", context_id=ctx, created_at="2026-06-02T00:00:00.500000")
    _seed_event(provider, schema, event_id="ctxe-tieA", context_id=ctx, created_at="2026-06-02T00:00:00.500000")
    _seed_event(provider, schema, event_id="ctxe-zero", context_id=ctx, created_at="2026-06-02T00:00:00")
    raw_ids = [str(r[0]) for r in _raw(
        provider,
        f'SELECT id FROM "{schema}".core__context_events '
        "WHERE context_id = %s AND is_deleted = 0 ORDER BY created_at ASC, id ASC",
        (ctx,),
    )]
    got = [str(e["id"]) for e in store.list_all_events(ctx)]
    _check(got == raw_ids, f"tie-break + zero-microsecond order matches raw (got {got})")
    _check(got[0] == "ctxe-zero", "zero-microsecond timestamp sorts before the .500000 pair")
    _check(got[1:] == ["ctxe-tieA", "ctxe-tieB"], "same-microsecond pair tie-breaks A before B by id")


def test_has_system_events(store: ContextEventStore, provider: PostgresProvider, schema: str) -> None:
    """has_system_events == raw COUNT(*) > 0."""
    ctx = "ctx_sys"
    _seed_event(provider, schema, event_id="ctxe-in1", context_id=ctx, created_at=_SEED_AT, event_type="input")
    _check(store.has_system_events(ctx) is False, "no system events yet → False")
    _seed_event(provider, schema, event_id="ctxe-sys1", context_id=ctx, created_at=_SEED_AT, event_type="system")
    _check(store.has_system_events(ctx) is True, "after a system event → True")
    _check(store.has_system_events("ctx_absent") is False, "unknown context → False")


def test_soft_delete_event(store: ContextEventStore, provider: PostgresProvider, schema: str) -> None:
    """soft_delete_event flips is_deleted=1 and drops the row from live reads."""
    ctx = "ctx_del"
    _seed_event(provider, schema, event_id="ctxe-d1", context_id=ctx, created_at="2026-06-03T00:00:01")
    _seed_event(provider, schema, event_id="ctxe-d2", context_id=ctx, created_at="2026-06-03T00:00:02")
    store.soft_delete_event("ctxe-d1")
    _check(_scalar(provider, schema, "core__context_events", "is_deleted", "ctxe-d1") == 1, "soft_delete_event set is_deleted=1")
    live_ids = [str(e["id"]) for e in store.list_all_events(ctx)]
    _check(live_ids == ["ctxe-d2"], f"deleted event excluded from live reads; got {live_ids}")


def test_soft_delete_events_before(store: ContextEventStore, provider: PostgresProvider, schema: str) -> None:
    """soft_delete_events_before deletes (created_at, id) <= end inclusive; returns count."""
    ctx = "ctx_before"
    for i in range(1, 6):
        _seed_event(provider, schema, event_id=f"ctxe-b{i}", context_id=ctx, created_at=f"2026-06-04T00:00:0{i}")
    deleted = store.soft_delete_events_before(ctx, "ctxe-b3")  # inclusive of b3
    _check(deleted == 3, f"soft_delete_events_before(b3) returns 3 (b1,b2,b3 inclusive); got {deleted}")
    remaining = [str(e["id"]) for e in store.list_all_events(ctx)]
    _check(remaining == ["ctxe-b4", "ctxe-b5"], f"only b4,b5 remain live; got {remaining}")
    _check(store.soft_delete_events_before(ctx, "ctxe-b3") == 0, "re-run is idempotent (already deleted) → 0")


def test_get_process_keys_jsonb(store: ContextEventStore, provider: PostgresProvider, schema: str) -> None:
    """get_process_keys_in_events_before reads JSONB metadata back as a dict.

    The method treats contains_process_keys as opaque strings (collect + dedup),
    so the fixture uses synthetic tokens — real process-key literals here would
    trip the whole_tree_integration call-site gate (no such plugin exists), and
    the literal format is irrelevant to what this exercises.
    """
    ctx = "ctx_keys"
    _seed_event(provider, schema, event_id="ctxe-k1", context_id=ctx, created_at="2026-06-05T00:00:01",
                metadata={"contains_process_keys": ["pkey-alpha", "pkey-beta"]})
    _seed_event(provider, schema, event_id="ctxe-k2", context_id=ctx, created_at="2026-06-05T00:00:02",
                metadata={"contains_process_keys": ["pkey-beta", "pkey-gamma"]})
    _seed_event(provider, schema, event_id="ctxe-k3", context_id=ctx, created_at="2026-06-05T00:00:03",
                metadata={"contains_process_keys": ["pkey-after"]})  # after the cutoff
    keys = set(store.get_process_keys_in_events_before(ctx, "ctxe-k2"))
    _check(keys == {"pkey-alpha", "pkey-beta", "pkey-gamma"},
           f"JSONB metadata parsed to dict; keys deduped up to cutoff (k3 excluded); got {sorted(keys)}")


def test_snapshots(provider: PostgresProvider, schema: str) -> None:
    """get_latest_snapshot / list_snapshots / soft_delete_older_snapshots."""
    store = ContextSnapshotStore(cast("Any", _LiveStateAdapter(provider)))
    ctx = "ctx_snap"

    def _seed_snap(snap_id: str, created_at: str) -> None:
        _insert(provider, schema, "core__context_snapshots", {
            "id": snap_id, "context_id": ctx, "start_event_id": "s", "end_event_id": "e",
            "summary_path": f"snap/{snap_id}.txt", "summary_char_count": 1,
            "original_char_count": 2, "cache_key": None, "is_deleted": 0,
            "created_at": created_at, "updated_at": _SEED_AT,
        })

    for i in range(1, 4):
        _seed_snap(f"cxs-{i}", f"2026-06-06T00:00:0{i}")

    latest = store.get_latest_snapshot(ctx)
    _check(latest is not None and str(latest["id"]) == "cxs-3", "get_latest_snapshot → newest (cxs-3)")
    listed = [str(s["id"]) for s in store.list_snapshots(ctx)]
    _check(listed == ["cxs-1", "cxs-2", "cxs-3"], f"list_snapshots ascending by created_at; got {listed}")

    deleted = store.soft_delete_older_snapshots(ctx, "cxs-3")  # delete strictly older than cxs-3
    _check(set(deleted) == {"cxs-1", "cxs-2"}, f"soft_delete_older_snapshots(keep=cxs-3) returns older ids; got {deleted}")
    _check([str(s["id"]) for s in store.list_snapshots(ctx)] == ["cxs-3"], "only kept snapshot remains live")

    store.soft_delete_snapshot("cxs-3")
    _check(_scalar(provider, schema, "core__context_snapshots", "is_deleted", "cxs-3") == 1, "soft_delete_snapshot set is_deleted=1")
    _check(store.get_latest_snapshot(ctx) is None, "no live snapshots after deleting the last")


def test_update_cursor_trigger(provider: PostgresProvider, schema: str) -> None:
    """update_cursor writes the cursor AND the BEFORE-UPDATE trigger bumps updated_at."""
    registry = ContextSessionRegistry(cast("Any", _LiveStateAdapter(provider)))
    ctx = "ctx_cursor"
    _insert(provider, schema, "core__context_sessions", {
        "id": "cs-1", "context_id": ctx, "provider": "platform", "context_mode": "platform",
        "cache_state": "cold", "is_deleted": 0,
        "created_at": _SEED_AT, "updated_at": _SEED_AT,
    })
    before_updated = str(_scalar(provider, schema, "core__context_sessions", "updated_at", "cs-1"))
    registry.update_cursor(ctx, last_event_id="ctxe-cur", last_event_created_at="2026-06-07T12:00:00")
    _check(_scalar(provider, schema, "core__context_sessions", "last_event_id", "cs-1") == "ctxe-cur", "update_cursor wrote last_event_id")
    _check(str(_scalar(provider, schema, "core__context_sessions", "last_event_created_at", "cs-1")) == "2026-06-07T12:00:00",
           "update_cursor wrote last_event_created_at")
    after_updated = str(_scalar(provider, schema, "core__context_sessions", "updated_at", "cs-1"))
    _check(after_updated > before_updated, f"updated_at advanced via trigger (dropped explicit set); {before_updated} -> {after_updated}")


def main() -> int:
    if os.environ.get("CONTEXT_MGMT_LIVE_SMOKE") != "1":
        print("=== context_management_migration_live_smoke ===")
        print(
            "  SKIP  set CONTEXT_MGMT_LIVE_SMOKE=1 to run; needs the live "
            "solet DB (own throwaway schema)."
        )
        return 0
    print("=== context_management_migration_live_smoke ===")
    schema_name = f"example_test_ctxmgmt_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_trigger_function(provider, schema_name)
        _create_tables(provider, schema_name)
        event_store = ContextEventStore(cast("Any", _LiveStateAdapter(provider)))
        test_no_silent_cap_truncation(event_store, provider, schema_name)
        test_cursor_pagination(event_store)
        test_cursor_equivalent_spelling_regression(event_store, provider, schema_name)
        test_tie_break_and_precision(event_store, provider, schema_name)
        test_has_system_events(event_store, provider, schema_name)
        test_soft_delete_event(event_store, provider, schema_name)
        test_soft_delete_events_before(event_store, provider, schema_name)
        test_get_process_keys_jsonb(event_store, provider, schema_name)
        test_snapshots(provider, schema_name)
        test_update_cursor_trigger(provider, schema_name)
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
