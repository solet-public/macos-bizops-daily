#!/usr/bin/env python3
"""Live-Postgres smoke for the list_sessions junction migration (SQL-lockdown).

The Architect-ruled ``session_source_kind`` junction backs list_sessions'
source_kind filter (read-then-route). The read fold + window/sort + the backfill
+ the recompute are proven offline via a filter+write-honoring shim in
``list_sessions_m17_filters_smoke.py``; this live smoke proves the genuinely-new
REAL-Postgres behaviors the shim cannot — that the junction TABLE exists and the
real provider round-trips it:

* the ingest attach-path ``upsert_session`` (new canonical) writes a
  ``(canonical_session_id, source_kind)`` junction row via the real
  ``upsert_state`` DO-NOTHING, idempotently (a second DO-NOTHING is a no-op);
* the read-then-route reads it back — real ``query_state(junction, {source_kind})``
  yields the canonical id, and real ``query_state(session, {id: ANY([...])})``
  resolves the session (the ``= ANY`` list bind against the real provider);
* the recompute (``_reconcile_junction_for_demotion``) merges a demoted
  ex-canonical's kind into the survivor + hard-deletes the stale rows.

⚠ POST-ADOPTION ONLY: needs the ``session_source_kind`` table adopted on the
live DB. Before adoption it fails with "relation does not exist" — expected.
Sentinels use a high-sorting id prefix + are hard-deleted in ``finally``.
Env-gated behind ``LEDGER_READ_LIVE_SMOKE=1``.

Run::

    LEDGER_READ_LIVE_SMOKE=1 \\
      .venv/bin/python3 \\
      ananta/tests/llm/session_ledger/list_sessions_migration_live_smoke.py
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
from ananta.services.state_service.ordered_query import (  # noqa: E402
    parse_ordered_query,
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

_SCHEMA = os.environ[SOLET_NAME_ENV_VAR]

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


def _env(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None}


class _LiveStateAdapter:
    """StateManagementInterface stand-in over a real provider — prod paths verbatim."""

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace, table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else None,
        )
        return _env({"records": rows, "count": len(rows)})

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        spec = parse_ordered_query(data)
        rows = self._provider.select_ordered(
            namespace=namespace, table=spec.table, conditions=spec.filters,
            order_columns=spec.order_columns, direction=spec.direction,
            limit=spec.limit, after=spec.after, include_deleted=spec.include_deleted,
        )
        return _env({"records": rows, "count": len(rows)})

    def upsert_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        inserted, row_id = self._provider.upsert_conditional(
            namespace=namespace, table=str(data["table"]),
            data=cast("dict[str, Any]", data["record"]),
            conflict_columns=cast("list[str]", data["conflict_columns"]),
            conflict_predicate=cast("list[dict[str, Any]]", data.get("conflict_predicate")),
        )
        return _env({"result": {"inserted": inserted, "id": row_id}})

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any],
    ) -> dict[str, Any]:
        updated = self._provider.update(
            namespace=namespace, table=str(query["table"]),
            conditions=cast("dict[str, Any]", query.get("filters") or {}), updates=updates,
        )
        return _env({"result": {"updated": updated}})

    def write_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        record = cast("dict[str, Any]", query["record"])
        self._provider.insert(namespace=namespace, table=str(query["table"]), data=record)
        return _env({"result": {"generated_id": str(record.get("id", ""))}})

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        deleted = self._provider.delete(
            namespace=namespace, table=str(query["table"]),
            conditions=cast("dict[str, Any]", query.get("filters") or {}),
            soft_delete=bool(query.get("soft_delete", True)),
        )
        return _env({"result": {"deleted": deleted}})

    @contextmanager
    def transactional(self) -> Any:
        with self._provider.get_transactional_connection() as conn:
            yield _PostgresStateTransaction(conn, self._provider)


_MARK = "__list_sessions_migration_live_smoke__"
_PREFIX = f"zzzz_lsj_{_MARK}_"
_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)
_SRC_ID = f"{_PREFIX}src"
_EXT = f"ext-{_MARK}"
_KIND = IngestSourceKind.CODEX_LOCAL


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _hard_delete(provider: PostgresProvider, table: str, col: str, value: str) -> None:
    sql: LiteralString = cast(
        LiteralString,
        f'DELETE FROM "{_SCHEMA}"."session_ledger__{table}" WHERE "{col}" = %s',
    )
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (value,))


def _seed_source(provider: PostgresProvider) -> None:
    provider.insert(namespace="session_ledger", table="source", data={
        "id": _SRC_ID, "namespace": "session_ledger", "source_kind": _KIND.value,
        "root_uri": f"/lsj/{_MARK}", "enabled": True,
        "created_at": _naive(_NOW), "updated_at": _naive(_NOW), "is_deleted": 0,
    })


def test_real_ingest_attach_and_read_route(repo: SessionLedgerRepository) -> None:
    sid = repo.upsert_session(
        source_id=_SRC_ID, external_session_id=_EXT, vendor=SourceVendor.CODEX,
        source_kind=_KIND, vendor_session_label="canon", project_path=None,
        first_event_at=_NOW, last_event_at=_NOW,
    )
    junction = repo._query(  # noqa: SLF001
        "session_source_kind", {"canonical_session_id": sid, "is_deleted": 0},
    )
    _check(
        len(junction) == 1 and str(junction[0]["source_kind"]) == _KIND.value,
        "real ingest-attach: upsert_session wrote one (canonical_id, source_kind) "
        "junction row via real upsert_state DO-NOTHING",
    )
    # Idempotency: a second UPDATE-path upsert does NOT add a junction row.
    repo.upsert_session(
        source_id=_SRC_ID, external_session_id=_EXT, vendor=SourceVendor.CODEX,
        source_kind=_KIND, vendor_session_label="canon-v2", project_path=None,
        first_event_at=_NOW, last_event_at=_NOW,
    )
    junction2 = repo._query(  # noqa: SLF001
        "session_source_kind", {"canonical_session_id": sid, "is_deleted": 0},
    )
    _check(len(junction2) == 1, "real ingest-attach: re-upsert (UPDATE path) is junction-idempotent")

    # Read-then-route: junction by source_kind → canonical id → session by id=ANY.
    by_kind = repo._query(  # noqa: SLF001
        "session_source_kind", {"source_kind": _KIND.value, "is_deleted": 0},
    )
    canonical_ids = [str(r["canonical_session_id"]) for r in by_kind]
    _check(sid in canonical_ids, "real read-route: junction(source_kind=K) yields the sentinel canonical id")
    sessions = repo._query(  # noqa: SLF001
        "session", {"id": [sid], "is_deleted": 0},
    )
    _check(
        len(sessions) == 1 and str(sessions[0]["id"]) == sid,
        "real read-route: query_state(session, {id: ANY([canonical_id])}) resolves the session",
    )


def test_real_recompute_reconcile(repo: SessionLedgerRepository, provider: PostgresProvider) -> None:
    survivor = f"{_PREFIX}survivor"
    demoted = f"{_PREFIX}demoted"
    # Seed a stale (demoted, kind) junction row directly.
    provider.insert(namespace="session_ledger", table="session_source_kind", data={
        "id": f"{_PREFIX}j1", "namespace": "session_ledger",
        "canonical_session_id": demoted, "source_kind": _KIND.value,
        "created_at": _naive(_NOW), "updated_at": _naive(_NOW), "is_deleted": 0,
    })
    repo._reconcile_junction_for_demotion(  # noqa: SLF001
        survivor_id=survivor, demoted_id=demoted, now=_naive(_NOW),
    )
    on_survivor = repo._query(  # noqa: SLF001
        "session_source_kind", {"canonical_session_id": survivor, "is_deleted": 0},
    )
    on_demoted = repo._query(  # noqa: SLF001
        "session_source_kind", {"canonical_session_id": demoted, "is_deleted": 0},
    )
    _check(
        len(on_survivor) == 1 and str(on_survivor[0]["source_kind"]) == _KIND.value,
        "real recompute: demoted kind merged under the survivor (real upsert)",
    )
    _check(on_demoted == [], "real recompute: stale (demoted, *) junction rows hard-deleted")


def main() -> int:
    if os.environ.get("LEDGER_READ_LIVE_SMOKE") != "1":
        print("=== list_sessions_migration_live_smoke ===")
        print(
            "  SKIP  set LEDGER_READ_LIVE_SMOKE=1 to run "
            "(needs the live solet DB with the session_source_kind junction adopted)"
        )
        return 0

    print("=== list_sessions_migration_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    repo = SessionLedgerRepository(
        state_service=cast("Any", _LiveStateAdapter(provider)), clock=lambda: _NOW,
    )
    try:
        _seed_source(provider)
        print("test_real_ingest_attach_and_read_route")
        test_real_ingest_attach_and_read_route(repo)
        print("test_real_recompute_reconcile")
        test_real_recompute_reconcile(repo, provider)
    finally:
        _hard_delete(provider, "session_source_kind", "canonical_session_id", f"{_PREFIX}survivor")
        for r in provider.select(namespace="session_ledger", table="session",
                                 conditions={"external_session_id": _EXT}):
            _hard_delete(provider, "session_source_kind", "canonical_session_id", str(r["id"]))
            _hard_delete(provider, "session", "id", str(r["id"]))
        _hard_delete(provider, "source", "id", _SRC_ID)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
