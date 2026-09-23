#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the ingest-mixin write migration (Slice 5).

Pins that ``SessionLedgerIngestMixin`` + ``base._increment_batch_counters`` —
migrated off raw ``transactional()`` SQL onto the state-interface primitives
(SQL-lockdown #0, Slice 5) — drive the ingest write path correctly against the
running solet's REAL ledger schema (real JSONB columns, real BEFORE-UPDATE
triggers, real unique indexes, real ``timestamp`` (naive-UTC F1) columns). The thin planted-rows
stub cannot model any of those, which is exactly the migration's real-schema test
mandate.

Single-statement writes ride the AUTOCOMMIT seams (``_write`` / ``_update`` /
``_query`` / ``_query_ordered``); the atomic multi-write ``append_event`` and the
``upsert_session`` UPDATE branch ride the TYPED-TXN ops (``txn.write_state`` /
``txn.update_state`` / ``txn.query_state`` / ``txn.increment_and_return``) inside
a real ``transactional()`` — both exercised here through the actual production
SQL-composition + serialization code.

Coverage:

* ``insert_source`` → ``config_json`` Python dict round-trips through the real
  JSONB column (no caller ``json.dumps`` / ``::jsonb`` cast — the unknown-OID
  text→jsonb coercion).
* ``get_source`` / ``find_source_id_by_kind_and_root_uri`` read it back.
* ``upsert_session`` INSERT branch (the unchanged Slice-6 canonical dispatch),
  then the MIGRATED UPDATE branch: ``first_event_at`` narrows (LEAST), it
  widens ``last_event_at`` (GREATEST), and the seven COALESCE merges honour
  their asymmetric directions (new-wins vendor_session_label/project_path;
  existing-wins originator snapshot).
* ``append_event`` → event row with ``content_json`` dict → JSONB; the
  per-session ``sequence`` allocated monotonically via ``increment_and_return``;
  the batch ``event_count`` bumped via ``increment_and_return``; the session
  ``last_event_at`` advanced (GREATEST) by an out-of-order pair; the
  ``content_blob_id`` offload path NULLs ``content_text``.
* ``record_tool_call`` → ``resolve_tool_call`` (status CAS).
* ``record_attachment``.
* ``repoint_source_root_uri`` (rows-affected → True).

Writes only sentinel rows (tracked by id) and hard-deletes them in a ``finally``.
There are NO DB-level foreign keys (FKs are repository-enforced), so delete order
is irrelevant. Env-gated behind ``LEDGER_INGEST_LIVE_SMOKE=1`` (needs the live
solet DB up).

Run::

    LEDGER_INGEST_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/ingest_migration_live_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.constants import SOLET_NAME_ENV_VAR  # noqa: E402
from ananta.llm.session_ledger.repository import (  # noqa: E402
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
    SourceVendor,
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


def _load_pg_config() -> PostgresConfig:
    return PostgresConfig(**json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8")))


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None}


class _LiveStateAdapter:
    """Full StateManagementInterface stand-in over a real provider.

    Autocommit ``write_state`` / ``update_state`` / ``query_state`` /
    ``query_ordered`` delegate to ``provider.insert`` / ``update`` / ``select`` /
    ``select_ordered``; ``transactional()`` yields the PRODUCTION
    ``_PostgresStateTransaction`` (the typed-txn ops + the raw ``execute`` /
    ``fetch_one`` the unchanged canonical dispatch still uses) over a real
    non-autocommit connection — so the migrated writes run the actual
    SQL-composition + serialization path end-to-end.
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
            conditions=filters if isinstance(filters, dict) else {},
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

    def upsert_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        # SQL-lockdown Slice 6: upsert_session's Phase 1 canonical dispatch
        # rides upsert_state DO-NOTHING — the live adapter delegates to the real
        # partial-unique conditional INSERT path.
        inserted, record_id = self._provider.upsert_conditional(
            namespace=namespace,
            table=str(data["table"]),
            data=cast("dict[str, Any]", data["record"]),
            conflict_columns=cast("list[str]", data["conflict_columns"]),
            conflict_predicate=data.get("conflict_predicate"),
        )
        return _envelope({"result": {"inserted": inserted, "id": record_id}})

    def max_value(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        # GAP-5: append_event's _next_sequence reads MAX(sequence) via the
        # autocommit aggregate primitive — delegate to the real provider.aggregate.
        value = self._provider.aggregate(
            namespace=namespace,
            table=str(data["table"]),
            op="max",
            column=str(data["column"]),
            filters=cast("dict[str, Any]", data.get("filters") or {}),
        )
        return _envelope({"result": {"value": value}})

    @contextmanager
    def transactional(self) -> Any:
        with self._provider.get_transactional_connection() as conn:
            yield _PostgresStateTransaction(conn, self._provider)


_MARK = "__ingest_migration_live_smoke__"
_SCHEMA = os.environ[SOLET_NAME_ENV_VAR]

# Fixed clock so the COALESCE / LEAST / GREATEST assertions are deterministic.
_T1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)  # earliest
_T2 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # first session insert bound
_T3 = datetime(2026, 6, 1, 14, 0, 0, tzinfo=UTC)  # latest upsert bound
_EVT_LATE = datetime(2026, 6, 1, 15, 0, 0, tzinfo=UTC)  # > _T3, advances last_event_at
_EVT_EARLY = datetime(2026, 6, 1, 13, 0, 0, tzinfo=UTC)  # < running last, must NOT regress
_NOW = datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC)


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _row(provider: PostgresProvider, table: str, row_id: str) -> dict[str, Any]:
    rows = provider.select(namespace="session_ledger", table=table, conditions={"id": row_id})
    assert len(rows) == 1, f"expected 1 {table} row for {row_id}, got {len(rows)}"
    return rows[0]


def _as_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise AssertionError(f"expected datetime/ISO cell, got {type(value).__name__}")


def _hard_delete(provider: PostgresProvider, table: str, row_id: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'DELETE FROM "{_SCHEMA}"."session_ledger__{table}" WHERE id = %s'),
            (row_id,),
        )


def _make_event(
    *,
    external_session_id: str,
    event_type: EventType,
    role: MessageRole | None,
    content_text: str | None,
    content_json: dict[str, object] | None,
    event_at: datetime,
    vendor_event_id: str | None,
) -> NormalizedSessionEvent:
    return NormalizedSessionEvent(
        external_session_id=external_session_id,
        event_type=event_type,
        role=role,
        content_text=content_text,
        content_json=content_json,
        event_at=event_at,
        vendor_event_id=vendor_event_id,
        vendor_parent_event_id=None,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
        actor_session_label="actor-A",
        actor_agent_instance_id="agi-actor-A",
    )


def test_full_ingest_lifecycle(  # noqa: PLR0915 — one linear lifecycle, no branching
    repo: SessionLedgerRepository, provider: PostgresProvider
) -> None:
    created: dict[str, list[str]] = {
        t: [] for t in ("source", "session", "event", "tool_call", "attachment", "import_batch")
    }
    ext = f"ext-{_MARK}"
    try:
        # 1. insert_source — config_json dict → real JSONB column.
        config = {"flavor": "primary", "nested": {"k": [1, 2, 3]}}
        source_id = repo.insert_source(
            source_kind=IngestSourceKind.CODEX_PUSHED,
            root_uri=f"pushed:{_MARK}",
            account_label=_MARK,
            config=config,
        )
        created["source"].append(source_id)
        _check(source_id.startswith("src_"), "insert_source returns a src_ id")
        src = repo.get_source(source_id)
        _check(src is not None, "get_source reads the inserted row back")
        _check(
            src is not None and src.config_json == config,
            f"config_json dict round-trips through JSONB (got {src.config_json if src else None!r})",
        )
        found = repo.find_source_id_by_kind_and_root_uri(
            source_kind=IngestSourceKind.CODEX_PUSHED, root_uri=f"pushed:{_MARK}",
        )
        _check(found == source_id, "find_source_id_by_kind_and_root_uri resolves the source")

        # 2. upsert_session — INSERT branch (unchanged Slice-6 canonical dispatch).
        session_id = repo.upsert_session(
            source_id=source_id,
            external_session_id=ext,
            vendor=SourceVendor.CLAUDE_CODE,
            source_kind=IngestSourceKind.CODEX_PUSHED,
            vendor_session_label="label-v1",
            project_path="/proj/keep",
            first_event_at=_T2,
            last_event_at=_T2,
        )
        created["session"].append(session_id)
        _check(session_id.startswith("les_"), "upsert_session INSERT branch mints a les_ id")

        # 3. upsert_session — MIGRATED UPDATE branch (read-compute-write).
        same_id = repo.upsert_session(
            source_id=source_id,
            external_session_id=ext,
            vendor=SourceVendor.CLAUDE_CODE,
            source_kind=IngestSourceKind.CODEX_PUSHED,
            vendor_session_label="label-v2-NEW",  # new-wins → overwrites
            project_path=None,  # new is None → keep existing "/proj/keep"
            first_event_at=_T1,  # earlier → LEAST
            last_event_at=_T3,  # later → GREATEST
            originator_session_label="orig-snap",  # existing is NULL → backfill
        )
        _check(same_id == session_id, "UPDATE branch returns the same session id")
        sess = _row(provider, "session", session_id)
        _check(_as_dt(sess["first_event_at"]) == _naive(_T1), "first_event_at narrowed (LEAST)")
        _check(_as_dt(sess["last_event_at"]) == _naive(_T3), "last_event_at widened (GREATEST)")
        _check(sess["vendor_session_label"] == "label-v2-NEW", "vendor_session_label new-wins")
        _check(sess["project_path"] == "/proj/keep", "project_path keeps existing when new is None")
        _check(sess["originator_session_label"] == "orig-snap", "originator_session_label backfilled")
        _check(int(sess["event_count"]) == 0, "event_count still 0 before any append")

        # 4. start_batch (unchanged polling_driver path) — needed for append_event.
        batch_id = repo.start_batch(source_id, polling_lease_token="plt-smoke")
        created["import_batch"].append(batch_id)
        batch_updated_before = _as_dt(_row(provider, "import_batch", batch_id)["updated_at"])

        # 5a. append_event #1 — content_json dict → JSONB; event_at advances last_event_at.
        content_json: dict[str, object] = {"tool": "grep", "args": {"q": "EDGE_SINK"}}
        r1 = repo.append_event(
            session_id=session_id,
            external_id=f"vev-{_MARK}-1",
            normalized=_make_event(
                external_session_id=ext,
                event_type=EventType.TOOL_CALL,
                role=None,
                content_text=None,
                content_json=content_json,
                event_at=_EVT_LATE,
                vendor_event_id=f"vev-{_MARK}-1",
            ),
            batch_id=batch_id,
            content_blob_id=None,
            session_vendor=SourceVendor.CLAUDE_CODE,
            source_kind=IngestSourceKind.CODEX_PUSHED,
        )
        created["event"].append(r1.event_id)
        _check(r1.sequence == 1, f"first append allocates sequence 1 (got {r1.sequence})")
        evt = _row(provider, "event", r1.event_id)
        _check(evt["content_json"] == content_json, f"content_json round-trips through JSONB (got {evt['content_json']!r})")
        sess = _row(provider, "session", session_id)
        _check(int(sess["event_count"]) == 1, "session.event_count incremented to 1")
        _check(_as_dt(sess["last_event_at"]) == _naive(_EVT_LATE), "last_event_at advanced to the later event_at (GREATEST)")
        batch = _row(provider, "import_batch", batch_id)
        _check(int(batch["event_count"]) == 1, "batch.event_count incremented to 1 (increment_and_return)")
        # increment_and_return writes no updated_at — the BEFORE-UPDATE trigger must
        # maintain it (else batch updated_at silently goes stale post-migration).
        _check(
            _as_dt(batch["updated_at"]) > batch_updated_before,
            "batch.updated_at advanced via the trigger on increment_and_return "
            "(increment writes no updated_at itself)",
        )

        # 5b. append_event #2 — OUT-OF-ORDER earlier event_at must NOT regress last_event_at.
        r2 = repo.append_event(
            session_id=session_id,
            external_id=f"vev-{_MARK}-2",
            normalized=_make_event(
                external_session_id=ext,
                event_type=EventType.MESSAGE,
                role=MessageRole.USER,
                content_text="plain message body",
                content_json=None,
                event_at=_EVT_EARLY,
                vendor_event_id=f"vev-{_MARK}-2",
            ),
            batch_id=batch_id,
            content_blob_id=None,
            session_vendor=SourceVendor.CLAUDE_CODE,
            source_kind=IngestSourceKind.CODEX_PUSHED,
        )
        created["event"].append(r2.event_id)
        _check(r2.sequence == 2, f"second append allocates sequence 2 monotonically (got {r2.sequence})")
        sess = _row(provider, "session", session_id)
        _check(int(sess["event_count"]) == 2, "session.event_count incremented to 2")
        _check(
            _as_dt(sess["last_event_at"]) == _naive(_EVT_LATE),
            "out-of-order earlier event does NOT regress last_event_at (GREATEST holds)",
        )
        batch = _row(provider, "import_batch", batch_id)
        _check(int(batch["event_count"]) == 2, "batch.event_count incremented to 2")

        # 5c. append_event #3 — content_blob_id set → content_text NULLed.
        r3 = repo.append_event(
            session_id=session_id,
            external_id=f"vev-{_MARK}-3",
            normalized=_make_event(
                external_session_id=ext,
                event_type=EventType.MESSAGE,
                role=MessageRole.USER,
                content_text="this should be dropped because a blob_id is supplied",
                content_json=None,
                event_at=_EVT_EARLY,
                vendor_event_id=f"vev-{_MARK}-3",
            ),
            batch_id=batch_id,
            content_blob_id="bmd_offloaded",
            session_vendor=SourceVendor.CLAUDE_CODE,
            source_kind=IngestSourceKind.CODEX_PUSHED,
        )
        created["event"].append(r3.event_id)
        evt3 = _row(provider, "event", r3.event_id)
        _check(evt3["content_text"] is None, "content_blob_id set → content_text NULLed on the row")
        _check(evt3["content_blob_id"] == "bmd_offloaded", "content_blob_id pointer persisted")

        # 6. record_tool_call → resolve_tool_call (status CAS).
        tool_call_id = repo.record_tool_call(
            session_id=session_id,
            call_event_id=r1.event_id,
            tool_name="grep",
            called_at=_EVT_LATE,
        )
        created["tool_call"].append(tool_call_id)
        tc = _row(provider, "tool_call", tool_call_id)
        _check(tc["status"] == "pending", "record_tool_call lands status=pending")
        repo.resolve_tool_call(
            call_event_id=r1.event_id,
            result_event_id=r2.event_id,
            status="succeeded",
            resolved_at=_EVT_LATE,
        )
        tc = _row(provider, "tool_call", tool_call_id)
        _check(tc["status"] == "succeeded", "resolve_tool_call updates status → succeeded")
        _check(tc["result_event_id"] == r2.event_id, "resolve_tool_call binds the result_event_id")

        # 7. record_attachment.
        attachment_id = repo.record_attachment(
            event_id=r1.event_id,
            blob_id="bmd_att",
            mime_type="image/png",
            filename="cap.png",
            size_bytes=2048,
        )
        created["attachment"].append(attachment_id)
        att = _row(provider, "attachment", attachment_id)
        _check(att["blob_id"] == "bmd_att" and int(att["size_bytes"]) == 2048, "record_attachment row lands")

        # 8. repoint_source_root_uri (rows-affected → True).
        changed = repo.repoint_source_root_uri(source_id, f"pushed:{_MARK}-repointed")
        _check(changed is True, "repoint_source_root_uri returns True (row changed)")
        src = repo.get_source(source_id)
        _check(
            src is not None and src.root_uri == f"pushed:{_MARK}-repointed",
            "repoint persisted the new root_uri",
        )
    finally:
        for table, ids in created.items():
            for rid in ids:
                _hard_delete(provider, table, rid)
        _check(
            all(
                not provider.select(namespace="session_ledger", table=t, conditions={"id": rid})
                for t, ids in created.items()
                for rid in ids
            ),
            "all sentinel rows hard-deleted (cleanup)",
        )


def main() -> int:
    if os.environ.get("LEDGER_INGEST_LIVE_SMOKE") != "1":
        print("=== ingest_migration_live_smoke ===")
        print(
            "  SKIP  set LEDGER_INGEST_LIVE_SMOKE=1 to run; "
            "needs the live solet DB."
        )
        return 0
    print("=== ingest_migration_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    repo = SessionLedgerRepository(
        state_service=cast("Any", _LiveStateAdapter(provider)),
        clock=lambda: _NOW,
    )
    test_full_ingest_lifecycle(repo, provider)
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
