#!/usr/bin/env python3
"""Live-Postgres smoke for the session-upsert canonical-dispatch migration (SQL-lockdown Slice 6 keystone).

Pins the M18 two-phase ``upsert_session`` canonical dispatch — migrated off raw
``transactional()`` ``INSERT … ON CONFLICT`` SQL onto the autocommit
state-interface primitives (Architect ruling 2026-06-21 = Option B):

* Phase 1 = ``upsert_state`` DO-NOTHING with the structured ``conflict_predicate``
  mirroring the M18 partial-unique index ``idx_session_canonical_one_per_vendor_pair``
  (``ON CONFLICT (vendor, external_session_id) WHERE
  canonical_external_session_id IS NULL AND is_deleted = 0``). Its ``inserted``
  bool IS the landed signal (replaces the SELECT-back fetch_one).
* The canonical resolve = ``query_state`` ``is_null`` filter.
* Phase 2 demotion = ``write_state`` with the canonical pointer populated.
* The existing-session UPDATE path = autocommit ``update_state`` (the
  LEAST/GREATEST/COALESCE merge in Python).

Exercised against the running solet's REAL schema so the partial-unique index
actually fires the conflict (the thin stub cannot model it — the migration's
real-schema test mandate). Coverage:

1. Phase 1 — a fresh (vendor, external_session_id) lands the canonical row
   (canonical_external_session_id IS NULL).
2. UPDATE path — re-upsert the SAME (source_id, external_session_id) widens
   last_event_at (GREATEST) + backfills a NULL snapshot column (COALESCE-keep),
   returning the SAME id with NO new row.
3. Phase 2 — a SECOND source with the SAME (vendor, external_session_id) hits
   the partial-unique conflict, resolves the canonical, and INSERTs a sibling
   with canonical_external_session_id populated; the original stays canonical.

Writes only sentinel rows and hard-deletes them in a ``finally``. Env-gated
behind ``LEDGER_INGEST_KEYSTONE_LIVE_SMOKE=1``.

Run::

    LEDGER_INGEST_KEYSTONE_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/ingest_canonical_dispatch_migration_live_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
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

    Carries the surface ``upsert_session`` exercises end-to-end: ``query_state``
    (existing-row read + canonical resolve), ``upsert_state`` DO-NOTHING
    (Phase 1), ``write_state`` (Phase 2 + insert_source), ``update_state``
    (the UPDATE path).
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

    def upsert_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        inserted, record_id = self._provider.upsert_conditional(
            namespace=namespace,
            table=str(data["table"]),
            data=cast("dict[str, Any]", data["record"]),
            conflict_columns=cast("list[str]", data["conflict_columns"]),
            conflict_predicate=data.get("conflict_predicate"),
        )
        return _envelope({"result": {"inserted": inserted, "id": record_id}})

    @contextmanager
    def transactional(self) -> Any:
        with self._provider.get_transactional_connection() as conn:
            yield _PostgresStateTransaction(conn, self._provider)


_MARK = "__ingest_keystone_live_smoke__"
_SCHEMA = os.environ[SOLET_NAME_ENV_VAR]
_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _row(provider: PostgresProvider, table: str, row_id: str) -> dict[str, Any]:
    rows = provider.select(namespace="session_ledger", table=table, conditions={"id": row_id})
    assert len(rows) == 1, f"expected 1 {table} row for {row_id}, got {len(rows)}"
    return rows[0]


def _as_dt(value: object) -> datetime:
    """Coerce a stored timestamp cell (naive datetime OR isoformat str) to datetime.

    provider.select serializes datetimes to ``isoformat()`` (a ``T``-separated
    str) — so a raw ``str()`` comparison against ``str(naive_dt)`` (space sep)
    spuriously fails; parse both sides instead.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise AssertionError(f"expected datetime/ISO cell, got {type(value).__name__}")


def _hard_delete_by_id(provider: PostgresProvider, table: str, row_id: str) -> None:
    delete_sql: LiteralString = cast(
        LiteralString,
        f'DELETE FROM "{_SCHEMA}"."session_ledger__{table}" WHERE "id" = %s',
    )
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(delete_sql, (row_id,))


def test_canonical_dispatch_lifecycle(
    repo: SessionLedgerRepository, provider: PostgresProvider,
    sessions: list[str], sources: list[str],
) -> None:
    ext = f"ext-{_MARK}"
    src_a = repo.insert_source(
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
        root_uri=f"localA:{_MARK}", account_label=_MARK, config={},
    )
    sources.append(src_a)
    src_b = repo.insert_source(
        source_kind=IngestSourceKind.CLAUDE_CODE_HISTORY,
        root_uri=f"historyB:{_MARK}", account_label=_MARK, config={},
    )
    sources.append(src_b)

    # ── Phase 1: fresh (vendor, ext) lands the canonical row ──
    id1 = repo.upsert_session(
        source_id=src_a, external_session_id=ext, vendor=SourceVendor.CLAUDE_CODE,
        vendor_session_label=None, project_path=None,
        first_event_at=_T0, last_event_at=_T0,
    )
    sessions.append(id1)
    r1 = _row(provider, "session", id1)
    _check(r1["canonical_external_session_id"] is None, "Phase 1 row is canonical (pointer NULL)")
    _check(str(r1["source_id"]) == src_a, "Phase 1 row bound to source A")

    # ── UPDATE path: same (source_id, ext) re-upsert → SAME id, widened bound,
    #    backfilled snapshot column, NO new row ──
    id1_again = repo.upsert_session(
        source_id=src_a, external_session_id=ext, vendor=SourceVendor.CLAUDE_CODE,
        vendor_session_label="backfilled-label", project_path=None,
        first_event_at=_T0, last_event_at=_T0 + timedelta(hours=3),
    )
    _check(id1_again == id1, "UPDATE path returns the SAME id (existing-row hit, no new row)")
    r1b = _row(provider, "session", id1)
    _check(
        _as_dt(r1b["last_event_at"]) == (_T0 + timedelta(hours=3)).replace(tzinfo=None),
        "UPDATE path widened last_event_at (GREATEST)",
    )
    _check(
        str(r1b["vendor_session_label"]) == "backfilled-label",
        "UPDATE path backfilled the NULL vendor_session_label (COALESCE-new-wins)",
    )

    # ── Phase 2: SECOND source, SAME (vendor, ext) → partial-unique conflict →
    #    resolve canonical → INSERT sibling with the pointer populated ──
    id2 = repo.upsert_session(
        source_id=src_b, external_session_id=ext, vendor=SourceVendor.CLAUDE_CODE,
        vendor_session_label="sibling", project_path=None,
        first_event_at=_T0, last_event_at=_T0,
    )
    sessions.append(id2)
    _check(id2 != id1, "Phase 2 minted a distinct sibling row id")
    r2 = _row(provider, "session", id2)
    _check(
        str(r2["canonical_external_session_id"]) == ext,
        "Phase 2 sibling carries canonical_external_session_id pointer = ext",
    )
    _check(str(r2["source_id"]) == src_b, "Phase 2 sibling bound to source B")
    # The original stays canonical (the partial-unique winner).
    r1c = _row(provider, "session", id1)
    _check(
        r1c["canonical_external_session_id"] is None,
        "original row stays canonical after the sibling demotion",
    )


def main() -> int:
    if os.environ.get("LEDGER_INGEST_KEYSTONE_LIVE_SMOKE") != "1":
        print("=== ingest_canonical_dispatch_migration_live_smoke ===")
        print(
            "  SKIP  set LEDGER_INGEST_KEYSTONE_LIVE_SMOKE=1 to run "
            "(needs the live solet DB)"
        )
        return 0

    print("=== ingest_canonical_dispatch_migration_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    repo = SessionLedgerRepository(state_service=cast("Any", _LiveStateAdapter(provider)))
    sessions: list[str] = []
    sources: list[str] = []
    try:
        test_canonical_dispatch_lifecycle(repo, provider, sessions, sources)
    finally:
        for session_id in sessions:
            _hard_delete_by_id(provider, "session", session_id)
        for source_id in sources:
            _hard_delete_by_id(provider, "source", source_id)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
