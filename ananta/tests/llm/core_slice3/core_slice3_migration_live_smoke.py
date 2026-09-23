#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for core execute_sql Slice-3 (SQL lockdown).

Pins the six migrated ``FlowRuntimeGraph`` sites (GAP-CORE-2) against a REAL
``PostgresProvider`` — the raw ``execute_sql`` UPDATE/SELECT/COUNT surfaces are
gone, replaced by ``update_state`` (CAS) and ``query_state`` (uncapped read +
Python fold):

* ``update_token_state``        — raw UPDATE  → ``update_state`` (affected == 1
                                  identity invariant; ``updated_at`` dropped —
                                  the universal BEFORE-UPDATE trigger owns it).
* ``get_pending_token_count``   — ``COUNT(*) … NOT IN (terminal)`` → ``query_state``
                                  ``state = ANY(non-terminal complement)`` + len.
* ``get_pending_tokens``        — unbounded ``SELECT … ORDER BY created_at ASC`` →
                                  uncapped ``query_state`` + Python sort by
                                  ``(to_naive_utc(created_at), id)`` (deliberately
                                  NOT ``query_ordered`` — that silently caps at 100;
                                  ``id`` is the deterministic tie-break the raw
                                  query lacked; ``created_at`` compares by value,
                                  never by ISO spelling).
* ``get_token_for_action``      — point ``SELECT flow_token_id`` → ``query_state``;
                                  RAISES on a DB-error envelope, None for not-found.
* ``_check_flow_completion``    — ``UPDATE … WHERE status='active'`` (genuine CAS) →
                                  ``update_state``; a 0-row miss is LEGITIMATE and
                                  the completion callbacks fire UNCONDITIONALLY
                                  after the envelope check (never gated on the win).
* ``_get_flow_id_for_token``    — point ``SELECT flow_id_trace`` → ``query_state``;
                                  RAISES on a DB-error envelope, None for not-found.

Each path is driven through the REAL production method over a faithful state
adapter wired to a live provider, asserted against the deterministic expected
result for the seeded corpus, with raw column read-backs (``_scalar``) where a
write is verified, and error/malformed-envelope stubs for the fail-fast cases.
Sandbox schema is DROPped in a ``finally``.

Env-gated behind ``CORE_SLICE3_LIVE_SMOKE=1`` (needs the live DB up; own
throwaway schema).

Run::

    CORE_SLICE3_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/core_slice3/core_slice3_migration_live_smoke.py
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

from ananta.core.state.flow_runtime_graph import (  # noqa: E402
    FlowRuntimeGraph,
    TokenState,
)
from ananta.error_handling import FrameworkError  # noqa: E402
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


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None, "timestamp": ""}


class _LiveStateAdapter:
    """Faithful StateManagementInterface stand-in mirroring the plugin facade 1:1.

    ``query_state`` → ``provider.select`` (list values fuse to ``= ANY``; datetimes
    come back as ISO strings, exactly as production does); ``update_state`` →
    ``provider.update`` (rows-affected → ``data.result.updated``).
    """

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        conds = filters.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace,
            table=str(filters["table"]),
            conditions=cast("dict[str, Any]", conds) if isinstance(conds, dict) else None,
            limit=cast("int | None", filters.get("limit")),
        )
        return _ok({"records": rows, "count": len(rows)})

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        affected = self._provider.update(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", query.get("filters") or {}),
            updates=updates,
        )
        return _ok({"namespace": namespace, "result": {"updated": affected}})


# ─── Sandbox DDL ─────────────────────────────────────────────────────────────

_DDL: tuple[tuple[str, str], ...] = (
    (
        "core__flow_tokens",
        "id text PRIMARY KEY, core__flows_id text, flow_id_trace text NOT NULL, "
        "owner_type text NOT NULL, owner_ref text NOT NULL, state text NOT NULL, "
        "process_key text, result_summary text, completed_at timestamp, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "core__flows",
        "id text PRIMARY KEY, status text NOT NULL, completed_at timestamp, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "core__action_events",
        "id text PRIMARY KEY, flow_token_id text, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
)

_SEED_AT = "2026-06-01T00:00:00"


def _create_trigger_function(provider: PostgresProvider, schema: str) -> None:
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
                f'CREATE TRIGGER "{table}_upd" BEFORE UPDATE ON "{schema}"."{table}" '
                f'FOR EACH ROW EXECUTE FUNCTION "{schema}".update_updated_at_column();',
            ))


def _insert(provider: PostgresProvider, schema: str, table: str, row: dict[str, object]) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    col_csv = ", ".join(f'"{c}"' for c in cols)
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'INSERT INTO "{schema}"."{table}" ({col_csv}) VALUES ({placeholders})'),
            tuple(row[c] for c in cols),
        )


def _scalar(provider: PostgresProvider, schema: str, table: str, col: str, row_id: str) -> object:
    rows = provider.execute_query(f'SELECT "{col}" FROM "{schema}"."{table}" WHERE id = %s', (row_id,))
    return rows[0][0] if rows else "<<absent>>"


def _seed_token(
    provider: PostgresProvider, schema: str, *, tid: str, flow: str, state: str,
    owner_ref: str = "act-x", process_key: str | None = "p.k", created_at: str = _SEED_AT,
) -> None:
    _insert(provider, schema, "core__flow_tokens", {
        "id": tid, "core__flows_id": flow, "flow_id_trace": flow,
        "owner_type": "process", "owner_ref": owner_ref, "state": state,
        "process_key": process_key, "result_summary": "{}", "completed_at": None,
        "is_deleted": 0, "created_at": created_at, "updated_at": _SEED_AT,
    })


# ─── Cases ───────────────────────────────────────────────────────────────────


def test_update_token_state_terminal(provider: PostgresProvider, schema: str) -> None:
    """Terminal transition sets state + completed_at + result_summary; trigger bumps updated_at."""
    _seed_token(provider, schema, tid="ft-term", flow="flow-A", state="dispatched")
    frg = FlowRuntimeGraph(state_service=cast("Any", _LiveStateAdapter(provider)))
    frg.update_token_state("ft-term", TokenState.COMPLETED, {"ok": True})
    _check(_scalar(provider, schema, "core__flow_tokens", "state", "ft-term") == "completed",
           "update_token_state wrote state=completed via update_state")
    _check(_scalar(provider, schema, "core__flow_tokens", "completed_at", "ft-term") is not None,
           "terminal transition set completed_at")
    _check(_scalar(provider, schema, "core__flow_tokens", "result_summary", "ft-term") == '{"ok": true}',
           "result_summary persisted as JSON")
    _check(str(_scalar(provider, schema, "core__flow_tokens", "updated_at", "ft-term")) > _SEED_AT,
           "updated_at advanced via trigger (explicit updated_at write was dropped)")


def test_update_token_state_nonterminal_keeps_completed_at_null(provider: PostgresProvider, schema: str) -> None:
    """A non-terminal transition leaves completed_at NULL (behavior preserved)."""
    _seed_token(provider, schema, tid="ft-wait", flow="flow-A", state="pending")
    frg = FlowRuntimeGraph(state_service=cast("Any", _LiveStateAdapter(provider)))
    frg.update_token_state("ft-wait", TokenState.WAITING_JOB)
    _check(_scalar(provider, schema, "core__flow_tokens", "state", "ft-wait") == "waiting_job",
           "non-terminal transition wrote state=waiting_job")
    _check(_scalar(provider, schema, "core__flow_tokens", "completed_at", "ft-wait") is None,
           "non-terminal transition left completed_at NULL")


def test_update_token_state_missing_raises(provider: PostgresProvider) -> None:
    """Updating a non-existent token RAISES (affected == 0 ≠ 1 identity invariant)."""
    frg = FlowRuntimeGraph(state_service=cast("Any", _LiveStateAdapter(provider)))
    raised = False
    try:
        frg.update_token_state("ft-nope", TokenState.COMPLETED)
    except FrameworkError:
        raised = True
    _check(raised, "update_token_state on a missing token RAISES (0 affected, not a silent no-op)")


def test_get_pending_token_count(provider: PostgresProvider, schema: str) -> None:
    """Counts ONLY non-terminal tokens (the = ANY complement) for the given flow."""
    states = ["pending", "dispatched", "waiting_job", "completed", "failed", "cancelled", "aborted"]
    for i, st in enumerate(states):
        _seed_token(provider, schema, tid=f"ftc-{i}", flow="flow-C", state=st)
    _seed_token(provider, schema, tid="ftc-other", flow="flow-OTHER", state="pending")  # different flow
    frg = FlowRuntimeGraph(state_service=cast("Any", _LiveStateAdapter(provider)))
    _check(frg.get_pending_token_count("flow-C") == 3,
           "count == 3 (pending+dispatched+waiting_job; 4 terminal + other-flow excluded)")
    _check(frg.get_pending_token_count("flow-NONE") == 0, "unknown flow → 0")


def test_get_pending_tokens_shape_and_order(provider: PostgresProvider, schema: str) -> None:
    """Returns the 6-key shape, non-terminal only, ordered by created_at ascending."""
    _seed_token(provider, schema, tid="ftp-late", flow="flow-P", state="dispatched",
                owner_ref="o-late", process_key="late", created_at="2026-06-05T00:00:03")
    _seed_token(provider, schema, tid="ftp-early", flow="flow-P", state="pending",
                owner_ref="o-early", process_key="early", created_at="2026-06-05T00:00:01")
    _seed_token(provider, schema, tid="ftp-done", flow="flow-P", state="completed",
                owner_ref="o-done", process_key="done", created_at="2026-06-05T00:00:02")
    frg = FlowRuntimeGraph(state_service=cast("Any", _LiveStateAdapter(provider)))
    tokens = frg.get_pending_tokens("flow-P")
    _check([t["id"] for t in tokens] == ["ftp-early", "ftp-late"],
           f"non-terminal only, ordered by created_at asc (early, late); got {[t['id'] for t in tokens]}")
    _check(tokens and set(tokens[0].keys()) == {"id", "owner_type", "owner_ref", "state", "process_key", "created_at"},
           f"exactly the 6 observability keys; got {sorted(tokens[0].keys()) if tokens else None}")
    _check(tokens[0]["owner_ref"] == "o-early" and tokens[0]["process_key"] == "early",
           "row fields marshalled by dict key (not positional)")


def test_get_pending_tokens_tie_break(provider: PostgresProvider, schema: str) -> None:
    """Equal created_at tokens tie-break deterministically on id (raw query had none)."""
    _seed_token(provider, schema, tid="ftt-b", flow="flow-T", state="pending", created_at="2026-06-06T00:00:00")
    _seed_token(provider, schema, tid="ftt-a", flow="flow-T", state="pending", created_at="2026-06-06T00:00:00")
    frg = FlowRuntimeGraph(state_service=cast("Any", _LiveStateAdapter(provider)))
    tokens = frg.get_pending_tokens("flow-T")
    _check([t["id"] for t in tokens] == ["ftt-a", "ftt-b"],
           f"equal created_at → ascending id (ftt-a before ftt-b); got {[t['id'] for t in tokens]}")


def test_get_token_for_action(provider: PostgresProvider, schema: str) -> None:
    """Returns flow_token_id for an action; None for absent / null flow_token_id."""
    _insert(provider, schema, "core__action_events", {
        "id": "act-1", "flow_token_id": "ft-99", "is_deleted": 0,
        "created_at": _SEED_AT, "updated_at": _SEED_AT,
    })
    _insert(provider, schema, "core__action_events", {
        "id": "act-null", "flow_token_id": None, "is_deleted": 0,
        "created_at": _SEED_AT, "updated_at": _SEED_AT,
    })
    frg = FlowRuntimeGraph(state_service=cast("Any", _LiveStateAdapter(provider)))
    _check(frg.get_token_for_action("act-1") == "ft-99", "action → flow_token_id")
    _check(frg.get_token_for_action("act-null") is None, "action with NULL flow_token_id → None")
    _check(frg.get_token_for_action("act-absent") is None, "absent action → None")


def test_complete_token_completes_flow_cas(provider: PostgresProvider, schema: str) -> None:
    """Last pending token completing flips flows active→completed (CAS win) + fires callback once."""
    _insert(provider, schema, "core__flows", {
        "id": "flow-W", "status": "active", "completed_at": None,
        "is_deleted": 0, "created_at": _SEED_AT, "updated_at": _SEED_AT,
    })
    _seed_token(provider, schema, tid="ftw-1", flow="flow-W", state="dispatched")
    fired: list[str] = []
    frg = FlowRuntimeGraph(state_service=cast("Any", _LiveStateAdapter(provider)))
    frg.register_completion_callback(fired.append)
    frg.complete_token("ftw-1", success=True)
    _check(_scalar(provider, schema, "core__flows", "status", "flow-W") == "completed",
           "complete_token zero-pending → flows CAS active→completed")
    _check(_scalar(provider, schema, "core__flows", "completed_at", "flow-W") is not None,
           "flow completion set completed_at")
    _check(fired == ["flow-W"], f"completion callback fired exactly once with flow id; got {fired}")


def test_check_flow_completion_cas_miss_still_fires_callback(provider: PostgresProvider, schema: str) -> None:
    """A CAS miss (flow already completed, updated == 0) does NOT raise and STILL fires callbacks.

    This is the advisor-corrected invariant: callbacks are gated on the envelope
    check, never on the affected count — a flow that left 'active' via another
    path must not skip the (idempotent) cleanup callbacks.
    """
    _insert(provider, schema, "core__flows", {
        "id": "flow-M", "status": "completed", "completed_at": _SEED_AT,
        "is_deleted": 0, "created_at": _SEED_AT, "updated_at": _SEED_AT,
    })
    # No pending tokens for flow-M → _check_flow_completion proceeds to the CAS.
    fired: list[str] = []
    frg = FlowRuntimeGraph(state_service=cast("Any", _LiveStateAdapter(provider)))
    frg.register_completion_callback(fired.append)
    raised = False
    try:
        frg._check_flow_completion("flow-M")  # noqa: SLF001 — exercising the real CAS-miss path
    except FrameworkError:
        raised = True
    _check(not raised, "CAS miss (updated == 0, already-completed flow) does NOT raise")
    _check(fired == ["flow-M"], f"callbacks fire UNCONDITIONALLY after the envelope check on a CAS miss; got {fired}")


def test_complete_token_pending_remains_no_completion(provider: PostgresProvider, schema: str) -> None:
    """With pending tokens remaining, completing one does NOT complete the flow or fire callbacks."""
    _insert(provider, schema, "core__flows", {
        "id": "flow-R", "status": "active", "completed_at": None,
        "is_deleted": 0, "created_at": _SEED_AT, "updated_at": _SEED_AT,
    })
    _seed_token(provider, schema, tid="ftr-1", flow="flow-R", state="dispatched")
    _seed_token(provider, schema, tid="ftr-2", flow="flow-R", state="pending")
    fired: list[str] = []
    frg = FlowRuntimeGraph(state_service=cast("Any", _LiveStateAdapter(provider)))
    frg.register_completion_callback(fired.append)
    frg.complete_token("ftr-1", success=True)
    _check(_scalar(provider, schema, "core__flows", "status", "flow-R") == "active",
           "one pending token remains → flow stays active")
    _check(fired == [], f"no completion callback fired while work remains; got {fired}")


# ─── Fail-fast adapters ──────────────────────────────────────────────────────


class _ErrorStateAdapter:
    """Returns a non-completed envelope for every read/write — exercises fail-fast."""

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        _ = namespace, filters
        return {"action_status": "error", "data": None, "actions": [], "error": "simulated DB failure"}

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        _ = namespace, query, updates
        return {"action_status": "error", "data": None, "actions": [], "error": "simulated DB failure"}


class _MalformedRecordsAdapter:
    """Returns a COMPLETED envelope whose ``records`` is not a list — must RAISE."""

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        _ = namespace, filters
        return _ok({"records": "not-a-list", "count": 0})


def _raises(fn: Any) -> bool:
    try:
        fn()
    except FrameworkError:
        return True
    return False


def test_fail_fast_point_reads_db_error() -> None:
    """A DB-error envelope makes every migrated read RAISE — never a silent None/0."""
    frg = FlowRuntimeGraph(state_service=cast("Any", _ErrorStateAdapter()))
    _check(_raises(lambda: frg.get_pending_token_count("flow-X")),
           "get_pending_token_count RAISES on a DB-error envelope (not 0)")
    _check(_raises(lambda: frg.get_pending_tokens("flow-X")),
           "get_pending_tokens RAISES on a DB-error envelope (not [])")
    _check(_raises(lambda: frg.get_token_for_action("act-X")),
           "get_token_for_action RAISES on a DB-error envelope (not None)")
    _check(_raises(lambda: frg._get_flow_id_for_token("ft-X")),  # noqa: SLF001
           "_get_flow_id_for_token RAISES on a DB-error envelope (not None)")


def test_fail_fast_update_db_error() -> None:
    """A DB-error update envelope makes update_token_state RAISE (not a silent success)."""
    frg = FlowRuntimeGraph(state_service=cast("Any", _ErrorStateAdapter()))
    _check(_raises(lambda: frg.update_token_state("ft-X", TokenState.COMPLETED)),
           "update_token_state RAISES on a DB-error update envelope")


def test_fail_fast_malformed_records() -> None:
    """A completed envelope with a non-list ``records`` RAISES (malformed, not empty)."""
    frg = FlowRuntimeGraph(state_service=cast("Any", _MalformedRecordsAdapter()))
    _check(_raises(lambda: frg.get_pending_token_count("flow-X")),
           "get_pending_token_count RAISES on malformed (non-list) records")


def main() -> int:
    if os.environ.get("CORE_SLICE3_LIVE_SMOKE") != "1":
        print("=== core_slice3_migration_live_smoke ===")
        print(
            "  SKIP  set CORE_SLICE3_LIVE_SMOKE=1 to run; needs the live "
            "solet DB (own throwaway schema)."
        )
        return 0
    print("=== core_slice3_migration_live_smoke ===")
    schema_name = f"example_test_slice3_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_trigger_function(provider, schema_name)
        _create_tables(provider, schema_name)
        test_update_token_state_terminal(provider, schema_name)
        test_update_token_state_nonterminal_keeps_completed_at_null(provider, schema_name)
        test_update_token_state_missing_raises(provider)
        test_get_pending_token_count(provider, schema_name)
        test_get_pending_tokens_shape_and_order(provider, schema_name)
        test_get_pending_tokens_tie_break(provider, schema_name)
        test_get_token_for_action(provider, schema_name)
        test_complete_token_completes_flow_cas(provider, schema_name)
        test_check_flow_completion_cas_miss_still_fires_callback(provider, schema_name)
        test_complete_token_pending_remains_no_completion(provider, schema_name)
        test_fail_fast_point_reads_db_error()
        test_fail_fast_update_db_error()
        test_fail_fast_malformed_records()
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
