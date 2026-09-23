#!/usr/bin/env python3
"""Live-Postgres smoke for the ``list_quiescent_sessions`` read-then-route migration.

SQL-lockdown: the M6 auto-summarize feed retires off one SQL statement (canonical
+ cutoff + ``NOT EXISTS __summary`` anti-join + ``(summary_text IS NULL OR !=
sentinel)`` + INNER JOIN ``__source`` + ORDER BY ``last_event_at`` ASC LIMIT) onto
three ``query_state`` reads + a Python fold. The fold + the filter composition are
proven offline in ``quiescent_fold_smoke.py``; this smoke runs the REAL
``repo.list_quiescent_sessions`` against the real provider + schema and asserts
the read-then-route exclusions hold end-to-end:

* an un-summarized, non-sentinel, canonical, past-cutoff session is RETURNED with
  its projected ``source_kind`` + ``summary_text``;
* a session with a live ``__summary`` row is EXCLUDED (the idempotency seam);
* a session whose ``summary_text`` is the trivial sentinel is EXCLUDED;
* a sibling (``canonical_external_session_id`` set) is EXCLUDED (canonical-only).

The verb scans ALL quiescent canonical sessions (its nature), so this DB may hold
real rows too. The sentinels are seeded with a DISTANT-PAST ``last_event_at``
(2020) so they sort oldest-first — guaranteed within the ``limit`` window
regardless of how much real data is present — and assertions filter the result to
the sentinel ids. Env-gated behind ``LEDGER_READ_LIVE_SMOKE=1``; sentinels are
hard-deleted in ``finally``.

Run::

    LEDGER_READ_LIVE_SMOKE=1 \\
      .venv/bin/python3 \\
      ananta/tests/llm/session_ledger/quiescent_migration_live_smoke.py
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


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None}


class _LiveStateAdapter:
    """Minimal stand-in over a real provider — list_quiescent uses only query_state.

    ``query_state`` is the production ``plugin.query_state`` → ``read_state`` →
    ``provider.select`` path verbatim (raw bind, ``_serialize_for_json``,
    ``{records}`` envelope). ``transactional`` is present only for repo ctor.
    """

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else None,
        )
        return _envelope({"records": rows, "count": len(rows)})

    @contextmanager
    def transactional(self) -> Any:
        with self._provider.get_transactional_connection() as conn:
            yield _PostgresStateTransaction(conn, self._provider)


_MARK = "__quiescent_migration_live_smoke__"
_SENTINEL = f"[trivial-{_MARK}]"
_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
_PAST = datetime(2020, 1, 1, 0, 0, 0)  # naive; distant past → sorts oldest-first
_SRC_ID = f"src-{_MARK}"


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _hard_delete_by_id(provider: PostgresProvider, table: str, row_id: str) -> None:
    delete_sql: LiteralString = cast(
        LiteralString,
        f'DELETE FROM "{_SCHEMA}"."session_ledger__{table}" WHERE "id" = %s',
    )
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(delete_sql, (row_id,))


def _insert(
    provider: PostgresProvider,
    tracker: list[tuple[str, str]],
    table: str,
    row: dict[str, Any],
) -> str:
    provider.insert(namespace="session_ledger", table=table, data=row)
    tracker.append((table, str(row["id"])))
    return str(row["id"])


def _source_row() -> dict[str, Any]:
    return {
        "id": _SRC_ID,
        "namespace": "session_ledger",
        "source_kind": IngestSourceKind.CODEX_LOCAL.value,
        "root_uri": f"/quiescent/{_MARK}",
        "enabled": True,
        "created_at": _naive(_NOW),
        "updated_at": _naive(_NOW),
        "is_deleted": 0,
    }


def _session_row(
    *,
    suffix: str,
    canonical: str | None,
    summary_text: str | None,
) -> dict[str, Any]:
    return {
        "id": f"{suffix}-{_MARK}",
        "namespace": "session_ledger",
        "source_id": _SRC_ID,
        "external_session_id": f"ext-{suffix}-{_MARK}",
        "vendor": SourceVendor.CODEX.value,
        "first_event_at": _PAST,
        "last_event_at": _PAST,
        "event_count": 3,
        "summary_text": summary_text,
        "canonical_external_session_id": canonical,
        "created_at": _PAST,
        "updated_at": _naive(_NOW),
        "is_deleted": 0,
    }


def _summary_row(*, session_id: str) -> dict[str, Any]:
    return {
        "id": f"sum-{session_id}",
        "namespace": "session_ledger",
        "session_id": session_id,
        "chunk_index": 0,
        "summary_text": "embedded recap",
        "generated_at": _naive(_NOW),
        "generated_by_client_id": "internal:auto_summarize:inferred",
        "created_at": _naive(_NOW),
        "updated_at": _naive(_NOW),
        "is_deleted": 0,
    }


def _seed(provider: PostgresProvider, tracker: list[tuple[str, str]]) -> dict[str, str]:
    _insert(provider, tracker, "source", _source_row())
    ids = {
        "survive": _insert(provider, tracker, "session", _session_row(
            suffix="q-survive", canonical=None, summary_text=None)),
        "summarized": _insert(provider, tracker, "session", _session_row(
            suffix="q-summarized", canonical=None, summary_text=None)),
        "sentinel": _insert(provider, tracker, "session", _session_row(
            suffix="q-sentinel", canonical=None, summary_text=_SENTINEL)),
        "sibling": _insert(provider, tracker, "session", _session_row(
            suffix="q-sibling", canonical="ext-shared", summary_text=None)),
    }
    _insert(provider, tracker, "summary", _summary_row(session_id=ids["summarized"]))
    return ids


def test_list_quiescent_read_then_route(
    repo: SessionLedgerRepository, ids: dict[str, str],
) -> None:
    rows = repo.list_quiescent_sessions(
        quiescence_minutes=1, limit=50, trivial_sentinel=_SENTINEL,
    )
    by_id = {str(r["id"]): r for r in rows}

    _check(
        ids["survive"] in by_id,
        "un-summarized, non-sentinel, canonical, past-cutoff session is RETURNED",
    )
    _check(
        ids["summarized"] not in by_id,
        "session with a live __summary row is EXCLUDED (idempotency seam)",
    )
    _check(
        ids["sentinel"] not in by_id,
        "session with trivial-sentinel summary_text is EXCLUDED",
    )
    _check(
        ids["sibling"] not in by_id,
        "sibling (canonical_external_session_id set) is EXCLUDED (canonical-only)",
    )

    survivor = by_id.get(ids["survive"])
    if survivor is not None:
        _check(
            survivor.get("source_kind") == IngestSourceKind.CODEX_LOCAL.value,
            "survivor carries source_kind projected from the real __source read",
        )
        _check(
            survivor.get("summary_text") is None
            and isinstance(survivor.get("last_event_at"), str),
            "survivor carries summary_text (None) + last_event_at as an ISO string",
        )


def main() -> int:
    if os.environ.get("LEDGER_READ_LIVE_SMOKE") != "1":
        print("=== quiescent_migration_live_smoke ===")
        print(
            "  SKIP  set LEDGER_READ_LIVE_SMOKE=1 to run "
            "(needs the live solet DB)"
        )
        return 0

    print("=== quiescent_migration_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    repo = SessionLedgerRepository(
        state_service=cast("Any", _LiveStateAdapter(provider)),
        clock=lambda: _NOW,
    )
    tracker: list[tuple[str, str]] = []
    try:
        ids = _seed(provider, tracker)
        print("test_list_quiescent_read_then_route")
        test_list_quiescent_read_then_route(repo, ids)
    finally:
        for table, row_id in reversed(tracker):
            _hard_delete_by_id(provider, table, row_id)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
