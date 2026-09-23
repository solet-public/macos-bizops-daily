#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the summarize-mixin migration (SQL-lockdown).

Pins that ``SessionLedgerSummarizeMixin`` — migrated off raw ``transactional()`` /
``execute_sql`` SQL onto the state-interface primitives — drives the summary
write/read paths correctly against the running solet's REAL ledger schema (real
``timestamp`` (naive-UTC F1) columns, the real ``(session_id, chunk_index)``
UNIQUE index that ignores ``is_deleted``, the real BEFORE-UPDATE triggers). The
thin planted-rows stub cannot model the ``persist_summary`` denorm guard (its
in-txn chunk read returns ``[]`` and its synthetic ``__session`` row has no
``summary_text`` key, so it structurally always fires the update) — exactly the
migration's real-schema test mandate.

Coverage:

* ``persist_summary`` — all THREE denorm-guard branches on one session:
  (a) first chunk when ``summary_text`` is NULL → fires (NULL arm);
  (b) a HIGHER chunk → fires (this-is-the-latest arm);
  (c) a LOWER chunk arriving when a higher one exists AND ``summary_text`` is
      already set → does NOT clobber (the stale-chunk no-overwrite case the
      original ``COALESCE((SELECT MAX(chunk_index)))`` subquery guarded).
* ``overwrite_summary_text_for_codex_stage1`` — replaces ``__session.summary_text``
  AND HARD-deletes chunk0 (the row is GONE, not soft-deleted — a soft-delete
  would leave a zombie that blocks the caller's chunk0 re-insert because the
  UNIQUE index ignores ``is_deleted``).
* ``list_summaries_by_external_ids`` — the ``= ANY`` read returns only live rows
  whose ``embedding_vector_id`` matches; a soft-deleted row is excluded
  (``is_deleted: 0``) and a non-matching id contributes nothing.
* ``mark_session_summary_text`` — the ``{"op": "is_null"}`` compare-and-set sets
  ``summary_text`` when NULL and is a no-op (no clobber) once it is set.

Writes only sentinel rows (tracked by id) and hard-deletes them in a ``finally``.
There are NO DB-level foreign keys (FKs are repository-enforced). Env-gated behind
``LEDGER_SUMMARIZE_LIVE_SMOKE=1`` (needs the live solet DB up).

Run::

    LEDGER_SUMMARIZE_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/summarize_migration_live_smoke.py
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
    IngestSourceKind,
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
    ``query_ordered`` / ``delete_records`` delegate to the provider's
    ``insert`` / ``update`` / ``select`` / ``select_ordered`` / ``delete``;
    ``transactional()`` yields the PRODUCTION ``_PostgresStateTransaction`` so
    ``persist_summary`` runs the actual SQL-composition + serialization path.
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

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        deleted = self._provider.delete(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else {},
            soft_delete=bool(query.get("soft_delete", True)),
        )
        return _envelope({"result": {"deleted": deleted, "soft_delete": bool(query.get("soft_delete", True))}})

    @contextmanager
    def transactional(self) -> Any:
        with self._provider.get_transactional_connection() as conn:
            yield _PostgresStateTransaction(conn, self._provider)


_MARK = "__summarize_migration_live_smoke__"
_SCHEMA = os.environ[SOLET_NAME_ENV_VAR]
_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_NOW = datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC)


def _row_or_none(
    provider: PostgresProvider, table: str, conditions: dict[str, Any]
) -> dict[str, Any] | None:
    rows = provider.select(namespace="session_ledger", table=table, conditions=conditions)
    return rows[0] if rows else None


def _session_summary_text(provider: PostgresProvider, session_id: str) -> object:
    row = _row_or_none(provider, "session", {"id": session_id})
    assert row is not None, f"session {session_id} vanished"
    return row.get("summary_text")


def _hard_delete(provider: PostgresProvider, table: str, row_id: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'DELETE FROM "{_SCHEMA}"."session_ledger__{table}" WHERE id = %s'),
            (row_id,),
        )


def _delete_summaries_for_session(provider: PostgresProvider, session_id: str) -> None:
    delete_sql: LiteralString = (
        f'DELETE FROM "{_SCHEMA}"."session_ledger__summary" WHERE session_id = %s'
    )
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(delete_sql, (session_id,))


def _make_session(
    repo: SessionLedgerRepository, source_id: str, external_session_id: str
) -> str:
    """Insert a fresh session (summary_text starts NULL) via the INSERT branch."""
    return repo.upsert_session(
        source_id=source_id,
        external_session_id=external_session_id,
        vendor=SourceVendor.CLAUDE_CODE,
        vendor_session_label="label",
        project_path="/proj",
        first_event_at=_T0,
        last_event_at=_T0,
    )


def _persist(
    repo: SessionLedgerRepository,
    *,
    session_id: str,
    chunk_index: int,
    summary_text: str,
    embedding_vector_id: str,
) -> str:
    return repo.persist_summary(
        session_id=session_id,
        chunk_index=chunk_index,
        summary_text=summary_text,
        embedding_vector_id=embedding_vector_id,
        generated_by_client_id="internal:summarize_live_smoke",
        generated_at=_T0,
    )


def test_summarize_lifecycle(  # noqa: PLR0915 — one linear lifecycle, no branching
    repo: SessionLedgerRepository, provider: PostgresProvider
) -> None:
    sessions: list[str] = []
    sources: list[str] = []
    summaries: list[str] = []
    try:
        source_id = repo.insert_source(
            source_kind=IngestSourceKind.CODEX_PUSHED,
            root_uri=f"pushed:{_MARK}",
            account_label=_MARK,
            config={},
        )
        sources.append(source_id)

        # ── persist_summary: all three denorm-guard branches on one session ──
        s1 = _make_session(repo, source_id, f"ext-persist-{_MARK}")
        sessions.append(s1)
        _check(
            _session_summary_text(provider, s1) is None,
            "fresh session starts with summary_text NULL",
        )

        # (a) first chunk, summary_text NULL → fires (NULL arm).
        summaries.append(
            _persist(repo, session_id=s1, chunk_index=0, summary_text="c0", embedding_vector_id="ev-c0")
        )
        _check(
            _session_summary_text(provider, s1) == "c0",
            "persist chunk0 on a NULL-summary session sets summary_text=c0 (NULL arm)",
        )

        # (b) higher chunk → fires (this-is-the-latest arm).
        summaries.append(
            _persist(repo, session_id=s1, chunk_index=2, summary_text="c2", embedding_vector_id="ev-c2")
        )
        _check(
            _session_summary_text(provider, s1) == "c2",
            "persist higher chunk2 updates summary_text=c2 (latest-chunk arm)",
        )

        # (c) lower chunk, higher exists, summary_text set → does NOT clobber.
        summaries.append(
            _persist(repo, session_id=s1, chunk_index=1, summary_text="c1", embedding_vector_id="ev-c1")
        )
        _check(
            _session_summary_text(provider, s1) == "c2",
            "persist lower chunk1 does NOT clobber summary_text (stays c2; no-overwrite arm)",
        )

        # ── overwrite_summary_text_for_codex_stage1: replace text + HARD-delete chunk0 ──
        s2 = _make_session(repo, source_id, f"ext-overwrite-{_MARK}")
        sessions.append(s2)
        summaries.append(
            _persist(repo, session_id=s2, chunk_index=0, summary_text="old-chunk0", embedding_vector_id="ev-ow0")
        )
        _check(
            _row_or_none(provider, "summary", {"session_id": s2, "chunk_index": 0}) is not None,
            "overwrite fixture: chunk0 present before overwrite",
        )
        repo.overwrite_summary_text_for_codex_stage1(
            session_id=s2, new_summary_text="rewritten-by-codex-stage1",
        )
        _check(
            _session_summary_text(provider, s2) == "rewritten-by-codex-stage1",
            "overwrite replaces __session.summary_text",
        )
        # The row must be GONE entirely (select WITHOUT an is_deleted filter would
        # still surface a soft-deleted row) — proves the HARD delete.
        _check(
            _row_or_none(provider, "summary", {"session_id": s2, "chunk_index": 0}) is None,
            "overwrite HARD-deletes chunk0 (row absent, not soft-deleted)",
        )

        # ── list_summaries_by_external_ids: = ANY, is_deleted-excluded ──
        s3 = _make_session(repo, source_id, f"ext-list-{_MARK}")
        sessions.append(s3)
        live_id = _persist(
            repo, session_id=s3, chunk_index=0, summary_text="live-row", embedding_vector_id="ev-live"
        )
        summaries.append(live_id)
        deleted_id = _persist(
            repo, session_id=s3, chunk_index=1, summary_text="deleted-row", embedding_vector_id="ev-deleted"
        )
        summaries.append(deleted_id)
        # Soft-delete the second chunk so the = ANY read must exclude it.
        provider.update(
            namespace="session_ledger", table="summary",
            conditions={"id": deleted_id}, updates={"is_deleted": 1},
        )
        rows = repo.list_summaries_by_external_ids(["ev-live", "ev-deleted", "ev-missing"])
        returned_ids = {str(r.get("embedding_vector_id")) for r in rows}
        _check(
            returned_ids == {"ev-live"},
            f"list_summaries returns only the live matching row via = ANY "
            f"(soft-deleted + non-matching excluded; got {returned_ids!r})",
        )
        _check(
            repo.list_summaries_by_external_ids([]) == [],
            "list_summaries short-circuits to [] on an empty id list",
        )

        # ── mark_session_summary_text: is_null compare-and-set ──
        s4 = _make_session(repo, source_id, f"ext-mark-{_MARK}")
        sessions.append(s4)
        repo.mark_session_summary_text(session_id=s4, summary_text="(trivial)")
        _check(
            _session_summary_text(provider, s4) == "(trivial)",
            "mark sets summary_text when currently NULL (is_null arm fires)",
        )
        repo.mark_session_summary_text(session_id=s4, summary_text="SHOULD-NOT-APPLY")
        _check(
            _session_summary_text(provider, s4) == "(trivial)",
            "mark is a no-op once summary_text is set (is_null CAS misses; no clobber)",
        )
    finally:
        for session_id in sessions:
            _delete_summaries_for_session(provider, session_id)
            _hard_delete(provider, "session", session_id)
        for summary_id in summaries:
            _hard_delete(provider, "summary", summary_id)
        for source_id in sources:
            _hard_delete(provider, "source", source_id)
        leftover = [
            sid for sid in sessions
            if _row_or_none(provider, "session", {"id": sid}) is not None
        ]
        _check(not leftover, f"all sentinel sessions hard-deleted (cleanup); leftover={leftover!r}")


def main() -> int:
    if os.environ.get("LEDGER_SUMMARIZE_LIVE_SMOKE") != "1":
        print("=== summarize_migration_live_smoke ===")
        print(
            "  SKIP  set LEDGER_SUMMARIZE_LIVE_SMOKE=1 to run; "
            "needs the live solet DB."
        )
        return 0
    print("=== summarize_migration_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    repo = SessionLedgerRepository(
        state_service=cast("Any", _LiveStateAdapter(provider)),
        clock=lambda: _NOW,
    )
    test_summarize_lifecycle(repo, provider)
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
