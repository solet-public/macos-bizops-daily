#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the agent_messaging W-write migration (W1–W5).

Pins the SQL-lockdown thread/message-write rework against a REAL
``PostgresProvider`` — the five writes migrated off raw ``transactional()`` SQL
onto the state-management primitives — driving them through the ACTUAL production
SQL-composition + serialization path (real JSONB columns, real ``created_at`` /
``updated_at`` DB defaults, the real BEFORE-UPDATE trigger, real ``TIMESTAMP``
naive-UTC F1 columns). A planted-rows fake models none of those — exactly the
write-migration real-schema mandate the ledger ingest smoke established.

The schema is built by the REAL standardizer (``create_tables_from_schema`` from
``get_agent_messaging_schema()``) in a THROWAWAY pg-schema and DROPped in a
``finally``. That is both isolated from live ``core`` data AND faithful where the
live DB would NOT be: ``append_message`` writes the first-class ``important``
column, which the live ``core__agent_message`` only gains at the next schema
reconciliation — so a live-table harness would hit ``UndefinedColumn`` on exactly
the column W4 exercises. The throwaway schema, minted from the declaration,
carries it.

Coverage:

* **W1 ``create_thread``** → ``write_state``: every business column round-trips;
  ``metadata`` (a Python dict) lands in the JSONB column with no caller cast and
  re-reads byte-faithfully (nested dict/list/bool/int); ``last_message_cursor``
  defaults to 0; ``created_at`` / ``updated_at`` come from the DB default
  (``NOW() AT TIME ZONE 'UTC'``, statement-stable → equal) since the migrated
  write OMITS them.
* **W2 ``update_thread``** → ``update_state`` off ``_build_thread_updates``: each
  field-mapping branch (status / active-ids / backend_session_id /
  clear_active_action / set_closed_at) projects correctly; ``updated_at`` is
  maintained by the trigger (NOT written by the migration) and never regresses;
  an all-None update is a no-op that still returns the row.
* **W3 ``conditional_update_thread``** → ``update_state`` ``status = ANY`` CAS: an
  allowed status applies + returns the updated row (hit); a disallowed status
  RAISES and leaves the row UNTOUCHED — the untouched re-read is what proves the
  ``=ANY`` predicate is actually in the WHERE (a dropped filter would apply
  unconditionally yet still raise nowhere); a missing thread raises; an empty
  update fails fast.
* **W4 ``append_message``** → typed-txn ``increment_and_return`` (cursor alloc,
  status gate FUSED into its ``= ANY`` WHERE) + ``write_state`` (message) +
  ``update_state`` (optional thread update): ungated append advances the cursor
  monotonically and round-trips the row; a gated append on an ALLOWED status
  applies + bundles the thread update (status → QUEUED); a gated append on a
  DISALLOWED status RAISES **and** inserts no message **and** leaves
  ``last_message_cursor`` unchanged (the no-cursor-burn proof that ``= ANY`` is in
  the increment's WHERE); a missing thread raises.
* **W5 ``merge_message_metadata``** → typed-txn read-modify-write: the shallow
  merge overwrites patched keys, ADDS new keys, and PRESERVES untouched keys
  (the ``jsonb_set`` failure mode); the merged dict serializes to JSONB with no
  ``::jsonb`` cast; an empty patch is a no-op; a missing message raises.

Needs Postgres up; env-gated behind ``AGENT_MESSAGING_WRITE_LIVE_SMOKE=1``. Run::

    AGENT_MESSAGING_WRITE_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/agent_messaging/repository_write_migration_live_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.llm.agent_messaging.models import (  # noqa: E402
    MessageKind,
    MessageRole,
    OriginatorType,
    TextPart,
    ThreadStatus,
)
from ananta.llm.agent_messaging.repository import (  # noqa: E402
    AgentMessagingRepository,
    NewMessage,
    RepositoryError,
    ThreadStatusUpdate,
)
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    NAMESPACE,
    get_agent_messaging_schema,
)
from ananta.types.schema_standardizer import SchemaStandardizer  # noqa: E402
from postgres_state_management_plugin.plugin import (  # noqa: E402
    _PostgresStateTransaction,
)
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.ddl_renderer import (  # noqa: E402
    emit_create_table_ops,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)

_passed = 0
_failed: list[str] = []

# Fixed clock so closed_at / message created_at are deterministic across the run.
_NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)

_TITLE = "agm-write-migration-smoke"
_MESSAGE_TABLE = "core__agent_message"


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


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None}


class _LiveStateAdapter:
    """Full StateManagementInterface stand-in over a real provider.

    Autocommit ``write_state`` / ``update_state`` / ``query_state`` /
    ``query_ordered`` delegate to ``provider.insert`` / ``update`` / ``select`` /
    ``select_ordered``; ``transactional()`` yields the PRODUCTION
    ``_PostgresStateTransaction`` over a real non-autocommit connection — so the
    migrated writes (incl. ``append_message``'s ``increment_and_return`` +
    ``write_state``) run the actual SQL-composition + serialization path
    end-to-end.
    """

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        row_id = self._provider.insert(
            namespace=namespace,
            table=str(data["table"]),
            data=cast("dict[str, Any]", data.get("record")),
        )
        return _envelope({"result": {"generated_id": row_id, "inserted": 1}})

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        filters = query.get("filters") or {}
        affected = self._provider.update(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else {},
            updates=updates,
        )
        return _envelope({"result": {"updated": affected}})

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else None,
        )
        return _envelope({"records": rows, "count": len(rows)})

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        filters = data.get("filters") or {}
        order_by = cast("list[list[str]]", data.get("order_by") or [])
        rows = self._provider.select_ordered(
            namespace=namespace,
            table=str(data["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else {},
            order_columns=tuple(str(pair[0]) for pair in order_by),
            direction=str(order_by[0][1]) if order_by else "asc",
            limit=int(cast("int", data["limit"])),
        )
        return _envelope({"records": rows, "count": len(rows)})

    @contextmanager
    def transactional(self) -> Any:
        with self._provider.get_transactional_connection() as conn:
            yield _PostgresStateTransaction(conn, self._provider)


def _make_repo(provider: PostgresProvider) -> AgentMessagingRepository:
    return AgentMessagingRepository(
        cast("Any", _LiveStateAdapter(provider)),
        clock=lambda: _NOW,
    )


def _create_schema_tables(provider: PostgresProvider) -> None:
    """Build the real agent_messaging tables in the throwaway pg-schema.

    Uses the PRODUCTION DDL renderer (``emit_create_table_ops`` — the same path
    the plugin-schema lifecycle drives) applied through the provider's
    ``apply_schema_change_ops`` chokepoint, so the throwaway schema carries the
    real column types, the real ``created_at`` / ``updated_at`` defaults +
    BEFORE-UPDATE trigger, AND the ``important`` column the live
    ``core__agent_message`` lacks until its next reconciliation. (The legacy
    ``create_table`` path mis-renders a non-audit ``DATETIME`` ColumnDefinition
    such as ``closed_at``; the lifecycle renderer does not.)
    """
    # Standardize first (adds id / namespace / created_at / updated_at /
    # is_deleted / … — the canonical columns the trigger + indexes reference),
    # exactly as the lifecycle does before rendering.
    schema = SchemaStandardizer().standardize_schema(get_agent_messaging_schema())
    schema_name = provider.config.schema_name
    ops = [
        op
        for table in schema.tables.values()
        for op in emit_create_table_ops(NAMESPACE, table, schema_name)
    ]
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        provider.apply_schema_change_ops(cur, schema, ops)


def _drop_schema(provider: PostgresProvider, schema_name: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))


def _new_thread(repo: AgentMessagingRepository, *, status: ThreadStatus, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "originator_type": OriginatorType.MCP_BRIDGE,
        "target_backend": "peer:claude_code",
        "target_plugin_name": "agent_messaging_plugin",
        "status": status,
        "title": _TITLE,
    }
    kwargs.update(overrides)
    return repo.create_thread(**kwargs)


def test_w1_create_thread(repo: AgentMessagingRepository) -> str:
    metadata = {"flag": True, "count": 3, "nested": {"a": [1, 2]}, "label": "x"}
    thread = _new_thread(
        repo,
        status=ThreadStatus.OPEN,
        originator_id="orig-1",
        originator_session_id="sess-1",
        originator_bridge_id="bridge-1",
        working_directory="/tmp/wd",
        metadata=metadata,
        recipient_agent_instance_id="agi-recip-1",
        originator_session_label="Coordinator",
        originator_agent_instance_id="agi-orig-1",
        recipient_session_label="Architect",
    )
    _check(thread.id.startswith("agt-_"), f"W1: minted thread id (got {thread.id!r})")

    fetched = repo.get_thread(thread.id)
    assert fetched is not None, "W1: thread must be visible after insert"
    _check(fetched.originator_type == OriginatorType.MCP_BRIDGE, "W1: originator_type round-trips")
    _check(fetched.target_backend == "peer:claude_code", "W1: target_backend round-trips")
    _check(fetched.target_plugin_name == "agent_messaging_plugin", "W1: target_plugin_name round-trips")
    _check(fetched.status == ThreadStatus.OPEN, "W1: status round-trips")
    _check(fetched.originator_id == "orig-1", "W1: originator_id round-trips")
    _check(fetched.originator_session_id == "sess-1", "W1: originator_session_id round-trips")
    _check(fetched.originator_bridge_id == "bridge-1", "W1: originator_bridge_id round-trips")
    _check(fetched.working_directory == "/tmp/wd", "W1: working_directory round-trips")
    _check(fetched.recipient_agent_instance_id == "agi-recip-1", "W1: recipient_agent_instance_id round-trips")
    _check(fetched.originator_session_label == "Coordinator", "W1: originator_session_label round-trips")
    _check(fetched.originator_agent_instance_id == "agi-orig-1", "W1: originator_agent_instance_id round-trips")
    _check(fetched.recipient_session_label == "Architect", "W1: recipient_session_label round-trips")
    _check(
        fetched.metadata == metadata,
        f"W1: metadata dict round-trips through JSONB (no caller cast); got {fetched.metadata!r}",
    )
    _check(fetched.last_message_cursor == 0, "W1: last_message_cursor defaults to 0")
    _check(
        fetched.created_at == fetched.updated_at,
        f"W1: created_at == updated_at from the DB default (statement-stable "
        f"NOW(), omitted by the migration); got {fetched.created_at} vs {fetched.updated_at}",
    )
    _check(fetched.closed_at is None, "W1: closed_at is NULL on a fresh thread")
    return thread.id


def test_w2_update_thread(repo: AgentMessagingRepository, thread_id: str) -> None:
    created_at = repo.get_thread(thread_id).created_at  # type: ignore[union-attr]

    # (a) status + active-ids + backend_session_id
    repo.update_thread(
        thread_id,
        ThreadStatusUpdate(
            status=ThreadStatus.RUNNING,
            active_action_id="act-1",
            active_flow_id="flow-1",
            backend_session_id="bsess-1",
        ),
    )
    row = repo.get_thread(thread_id)
    assert row is not None
    _check(row.status == ThreadStatus.RUNNING, "W2a: status -> RUNNING")
    _check(row.active_action_id == "act-1", "W2a: active_action_id set")
    _check(row.active_flow_id == "flow-1", "W2a: active_flow_id set")
    _check(row.backend_session_id == "bsess-1", "W2a: backend_session_id set")
    _check(
        row.updated_at >= created_at,
        f"W2a: trigger maintains updated_at (>= created_at), not the migration; "
        f"got {row.updated_at} vs {created_at}",
    )

    # (b) clear_active_action -> both NULL, other fields untouched
    repo.update_thread(thread_id, ThreadStatusUpdate(clear_active_action=True))
    row = repo.get_thread(thread_id)
    assert row is not None
    _check(row.active_action_id is None, "W2b: clear_active_action nulls active_action_id")
    _check(row.active_flow_id is None, "W2b: clear_active_action nulls active_flow_id")
    _check(row.status == ThreadStatus.RUNNING, "W2b: status untouched by clear")
    _check(row.backend_session_id == "bsess-1", "W2b: backend_session_id untouched by clear")

    # (c) status CLOSED + set_closed_at
    repo.update_thread(
        thread_id, ThreadStatusUpdate(status=ThreadStatus.CLOSED, set_closed_at=True),
    )
    row = repo.get_thread(thread_id)
    assert row is not None
    _check(row.status == ThreadStatus.CLOSED, "W2c: status -> CLOSED")
    _check(isinstance(row.closed_at, datetime), f"W2c: closed_at set (got {row.closed_at!r})")

    # (d) empty (all-None) update is a no-op that still returns the row
    returned = repo.update_thread(thread_id, ThreadStatusUpdate())
    _check(returned.id == thread_id, "W2d: empty update returns the row (no-op)")
    _check(returned.status == ThreadStatus.CLOSED, "W2d: empty update leaves status CLOSED")
    _check(returned.closed_at is not None, "W2d: empty update leaves closed_at set")


def test_w3_conditional_update_thread(
    repo: AgentMessagingRepository, thread_id: str,
) -> None:
    # HIT: thread is OPEN, which is in the allowed set -> CAS applies.
    hit = repo.conditional_update_thread(
        thread_id,
        ThreadStatusUpdate(status=ThreadStatus.RUNNING, active_action_id="act-w3"),
        require_status_in=(ThreadStatus.OPEN, ThreadStatus.IDLE),
    )
    _check(hit.status == ThreadStatus.RUNNING, "W3 hit: allowed status -> CAS applied (RUNNING)")
    _check(hit.active_action_id == "act-w3", "W3 hit: returns the updated row")
    reread = repo.get_thread(thread_id)
    assert reread is not None
    _check(reread.status == ThreadStatus.RUNNING, "W3 hit: re-read confirms RUNNING")

    # MISS on wrong status: RUNNING is NOT in {OPEN, IDLE} -> raise AND row UNTOUCHED.
    # The untouched re-read is the assertion that actually proves status=ANY is in
    # the WHERE (a dropped filter would apply unconditionally).
    raised = False
    try:
        repo.conditional_update_thread(
            thread_id,
            ThreadStatusUpdate(status=ThreadStatus.CLOSED, set_closed_at=True),
            require_status_in=(ThreadStatus.OPEN, ThreadStatus.IDLE),
        )
    except RepositoryError:
        raised = True
    _check(raised, "W3 miss: disallowed status raises RepositoryError")
    after = repo.get_thread(thread_id)
    assert after is not None
    _check(
        after.status == ThreadStatus.RUNNING and after.closed_at is None,
        f"W3 miss: row UNTOUCHED by the missed CAS (=ANY predicate held); "
        f"status={after.status.value!r} closed_at={after.closed_at!r}",
    )

    # NOT-FOUND: a missing thread -> raise (0-row CAS, re-read finds nothing).
    raised = False
    try:
        repo.conditional_update_thread(
            "agt-_does_not_exist",
            ThreadStatusUpdate(status=ThreadStatus.CLOSED, set_closed_at=True),
            require_status_in=(ThreadStatus.OPEN,),
        )
    except RepositoryError:
        raised = True
    _check(raised, "W3 not-found: missing thread raises RepositoryError")

    # EMPTY UPDATE: explicit fail-fast (a non-empty update is required).
    raised = False
    try:
        repo.conditional_update_thread(
            thread_id, ThreadStatusUpdate(), require_status_in=(ThreadStatus.RUNNING,),
        )
    except RepositoryError:
        raised = True
    _check(raised, "W3 empty update: raises (non-empty update required)")


def _text_message(text: str, **kw: Any) -> NewMessage:
    return NewMessage(
        role=kw.pop("role", MessageRole.AGENT),
        kind=MessageKind.MESSAGE,
        content=[TextPart(type="text", text=text)],
        **kw,
    )


def test_w4_append_message(repo: AgentMessagingRepository) -> None:
    # Ungated append: cursor advances monotonically; the row round-trips.
    t = _new_thread(repo, status=ThreadStatus.OPEN).id
    m1 = repo.append_message(thread_id=t, message=_text_message("one"))
    m2 = repo.append_message(thread_id=t, message=_text_message("two"))
    _check(m1.id.startswith("agm-_"), "W4 ungated: minted message id")
    _check(
        m1.cursor == 1 and m2.cursor == 2,
        f"W4 ungated: cursor advances monotonically (got {m1.cursor}, {m2.cursor})",
    )
    back = _find_message(repo, t, m2.id)
    _check(
        bool(back.content) and back.content[0].text == "two",
        "W4 ungated: content round-trips through JSONB",
    )
    t_after = repo.get_thread(t)
    assert t_after is not None
    _check(t_after.last_message_cursor == 2, "W4 ungated: thread last_message_cursor advanced to 2")

    # Gated-allowed: OPEN is in the allowed set -> applies + bundles the thread
    # update (status -> QUEUED) atomically with the append.
    g = _new_thread(repo, status=ThreadStatus.OPEN).id
    gm = repo.append_message(
        thread_id=g,
        message=_text_message("go", role=MessageRole.ORIGINATOR, metadata={"important": True}),
        require_status_in=(ThreadStatus.OPEN, ThreadStatus.IDLE),
        update=ThreadStatusUpdate(status=ThreadStatus.QUEUED),
    )
    _check(gm.cursor == 1, "W4 gated-allowed: cursor allocated under the fused status gate")
    g_after = repo.get_thread(g)
    assert g_after is not None
    _check(g_after.status == ThreadStatus.QUEUED, "W4 gated-allowed: bundled thread update applied (->QUEUED)")
    _check(g_after.last_message_cursor == 1, "W4 gated-allowed: cursor persisted")

    # Gated-miss: g is now QUEUED, NOT in {OPEN, IDLE} -> raise AND no message
    # inserted AND last_message_cursor unchanged. The no-cursor-burn + no-row
    # proof is what shows status=ANY is actually in the increment's WHERE.
    before_cursor = g_after.last_message_cursor
    before_count = len(repo.recent_messages(g, limit=50))
    raised = False
    try:
        repo.append_message(
            thread_id=g,
            message=_text_message("nope", role=MessageRole.ORIGINATOR),
            require_status_in=(ThreadStatus.OPEN, ThreadStatus.IDLE),
        )
    except RepositoryError:
        raised = True
    _check(raised, "W4 gated-miss: disallowed status raises RepositoryError")
    g_miss = repo.get_thread(g)
    assert g_miss is not None
    after_count = len(repo.recent_messages(g, limit=50))
    _check(
        g_miss.last_message_cursor == before_cursor and after_count == before_count,
        f"W4 gated-miss: NO cursor burn + NO message inserted (=ANY held); "
        f"cursor {before_cursor}->{g_miss.last_message_cursor}, msgs {before_count}->{after_count}",
    )

    # Not-found: missing thread -> raise (0-row increment).
    raised = False
    try:
        repo.append_message(thread_id="agt-_does_not_exist", message=_text_message("x"))
    except RepositoryError:
        raised = True
    _check(raised, "W4 not-found: missing thread raises RepositoryError")


def _seed_message(
    provider: PostgresProvider,
    schema: str,
    *,
    thread_id: str,
    message_id: str,
    metadata: dict[str, object],
) -> None:
    """Insert a minimal agent_message fixture row via raw SQL.

    W5's subject (``merge_message_metadata``) touches ONLY the ``metadata``
    column, so the fixture seeds just the existing NOT-NULL columns directly —
    independent of ``append_message`` (W4-pending raw SQL) and of the
    ``important`` column the live solet DB only gains at the next schema
    reconciliation (restart). ``created_at`` / ``is_deleted`` come from DB
    defaults.
    """
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{_MESSAGE_TABLE}" '
                "(id, namespace, thread_id, cursor, role, kind, content, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)",
            ),
            (
                message_id,
                "core",
                thread_id,
                0,
                "originator",
                "message",
                json.dumps([{"type": "text", "text": "hello"}]),
                json.dumps(metadata),
            ),
        )


def test_w5_merge_message_metadata(
    repo: AgentMessagingRepository,
    provider: PostgresProvider,
    schema: str,
    thread_id: str,
) -> None:
    message_id = f"agm-_{uuid.uuid4().hex}"
    _seed_message(
        provider, schema, thread_id=thread_id, message_id=message_id,
        metadata={"important": True, "timeout_seconds": 30},
    )

    # shallow merge: overwrite timeout_seconds, ADD assembled_prompt, PRESERVE important
    repo.merge_message_metadata(
        message_id, {"assembled_prompt": "PROMPT", "timeout_seconds": 45},
    )
    merged = _find_message(repo, thread_id, message_id)
    _check(
        merged.metadata == {"important": True, "timeout_seconds": 45, "assembled_prompt": "PROMPT"},
        f"W5: merge overwrites + adds + preserves keys (jsonb_set failure mode); got {merged.metadata!r}",
    )

    # empty patch is a no-op
    repo.merge_message_metadata(message_id, {})
    after_noop = _find_message(repo, thread_id, message_id)
    _check(after_noop.metadata == merged.metadata, "W5: empty patch is a no-op")

    # missing message raises
    raised = False
    try:
        repo.merge_message_metadata("agm-_does_not_exist", {"x": 1})
    except RepositoryError:
        raised = True
    _check(raised, "W5: merge on a missing message raises RepositoryError")


def _find_message(repo: AgentMessagingRepository, thread_id: str, message_id: str) -> Any:
    for row in repo.recent_messages(thread_id, limit=50):
        if row.id == message_id:
            return row
    raise AssertionError(f"message {message_id} not found in recent_messages({thread_id})")


def main() -> int:
    if os.environ.get("AGENT_MESSAGING_WRITE_LIVE_SMOKE") != "1":
        print("=== repository_write_migration_live_smoke ===")
        print("  SKIP  set AGENT_MESSAGING_WRITE_LIVE_SMOKE=1 to run; needs Postgres up.")
        return 0
    print("=== repository_write_migration_live_smoke ===")
    schema_name = f"example_test_agm_write_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_schema_tables(provider)
        repo = _make_repo(provider)

        t1 = test_w1_create_thread(repo)
        test_w2_update_thread(repo, t1)

        t3 = _new_thread(repo, status=ThreadStatus.OPEN).id
        test_w3_conditional_update_thread(repo, t3)

        test_w4_append_message(repo)

        t2 = _new_thread(repo, status=ThreadStatus.OPEN).id
        test_w5_merge_message_metadata(repo, provider, schema_name, t2)
    finally:
        _drop_schema(provider, schema_name)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
