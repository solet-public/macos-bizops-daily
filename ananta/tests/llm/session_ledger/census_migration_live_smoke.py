#!/usr/bin/env python3
"""Live-Postgres smoke for the census ``__event`` keyset-paging migration (GAP-1).

SQL lockdown: ``census_source_rows`` retires the multi-CTE
``bit_xor(hashtextextended(...))`` aggregate onto a Python fold over the typed
primitives (operator D1, 2026-06-20). The bounded source/session/tool_call/batch
reads + the fold are proven offline in ``census_fold_smoke.py``; this live smoke
proves the one genuinely NEW real-Postgres behavior the migration introduces —
the memory-bounded ``__event`` keyset scan (:func:`fold_census_events`) — against
the REAL provider + schema:

* the ``id`` keyset cursor (Gap-A ``id > last_id`` over the unique TEXT PK,
  ordered ``(id, created_at)`` ASC) pages every live event exactly once with NO
  skip / duplicate at a page boundary — asserted by ``page_size=2`` (multi-page)
  yielding the IDENTICAL fold to ``page_size=100`` (single page);
* the ``query_ordered`` ``is_deleted = 0`` default EXCLUDES a soft-deleted event;
* the fingerprint is DETERMINISTIC across runs against real rows;
* events are attributed to their session's source via the real ``query_state``
  reads, and the canonical/sibling split is correct.

``census_source_rows`` itself reads the WHOLE corpus (every source/session/event
row) — that is the verb's nature (the operator-accepted full-read). To stay
corpus-INDEPENDENT (a reviewer's live DB may hold ~1M events), this smoke seeds
sentinels with a HIGH-sorting id prefix and bounds every read at the SQL level:
the bounded reads scope by ``id`` / ``source_id`` equality, and the event scan's
first page floors the ``id`` cursor at the sentinel prefix (subsequent pages ride
the fold's own ``id > last_id`` cursor, which stays in sentinel range). So the
fold runs end-to-end against real Postgres while reading only the ~6 seeded rows.

There are NO DB-level foreign keys (FKs are repository-enforced per schema.py), so
sentinel ids are free-form. Env-gated behind ``LEDGER_READ_LIVE_SMOKE=1``.

Run::

    LEDGER_READ_LIVE_SMOKE=1 \\
      .venv/bin/python3 \\
      ananta/tests/llm/session_ledger/census_migration_live_smoke.py
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
from ananta.llm.session_ledger.read_support import (  # noqa: E402
    _CensusAggregator,
    _OrderedReader,
    fold_census_events,
)
from ananta.llm.session_ledger.repository import (  # noqa: E402
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_EVENT,
    TABLE_SESSION,
    TABLE_SOURCE,
)
from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
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


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None}


class _LiveStateAdapter:
    """Minimal StateManagementInterface stand-in over a real provider.

    ``query_state`` / ``query_ordered`` mirror the production
    ``plugin.query_state`` → ``read_state`` → ``provider.select`` and
    ``plugin.query_ordered`` → ``parse_ordered_query`` → ``provider.select_ordered``
    paths verbatim, so a green run discriminates the REAL read path (raw bind,
    ``_serialize_for_json``, the ``{records, count}`` envelope). ``transactional``
    is present only so the concrete ``SessionLedgerRepository`` constructs cleanly.
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

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        # The production plugin.query_ordered body verbatim: parse_ordered_query
        # (the Gap-C cap + cursor naive-UTC seam) → provider.select_ordered (the
        # one site that composes the keyset SQL).
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
        return _envelope({"records": rows, "count": len(rows)})

    @contextmanager
    def transactional(self) -> Any:
        with self._provider.get_transactional_connection() as conn:
            yield _PostgresStateTransaction(conn, self._provider)


_MARK = "__census_migration_live_smoke__"
_PREFIX = f"zzzz_census_{_MARK}_"  # sorts AFTER real ids → keyset isolates sentinels
_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
_SEEDS = (0, 527612190)
_SRC_ID = f"{_PREFIX}src"
_SESS_CANON = f"{_PREFIX}s_canon"
_SESS_SIB = f"{_PREFIX}s_sib"


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
        "root_uri": f"/census/{_MARK}",
        "enabled": True,
        "created_at": _naive(_NOW),
        "updated_at": _naive(_NOW),
        "is_deleted": 0,
    }


def _session_row(*, row_id: str, canonical: str | None) -> dict[str, Any]:
    return {
        "id": row_id,
        "namespace": "session_ledger",
        "source_id": _SRC_ID,
        "external_session_id": f"ext-{row_id}",
        "vendor": SourceVendor.CODEX.value,
        "first_event_at": _naive(_NOW),
        "last_event_at": _naive(_NOW),
        "event_count": 0,
        "canonical_external_session_id": canonical,
        "created_at": _naive(_NOW),
        "updated_at": _naive(_NOW),
        "is_deleted": 0,
    }


def _event_row(
    *,
    suffix: str,
    session_id: str,
    sequence: int,
    blob: str | None,
    is_deleted: int = 0,
) -> dict[str, Any]:
    return {
        "id": f"{_PREFIX}{suffix}",
        "namespace": "session_ledger",
        "session_id": session_id,
        "sequence": sequence,
        "event_type": EventType.MESSAGE.value,
        "content_blob_id": blob,
        "event_at": _naive(_NOW),
        "imported_at": _naive(_NOW),
        "batch_id": f"{_PREFIX}batch",
        "created_at": _naive(_NOW),
        "updated_at": _naive(_NOW),
        "is_deleted": is_deleted,
    }


def _seed(provider: PostgresProvider, tracker: list[tuple[str, str]]) -> None:
    _insert(provider, tracker, "source", _source_row())
    _insert(provider, tracker, "session",
            _session_row(row_id=_SESS_CANON, canonical=None))
    _insert(provider, tracker, "session",
            _session_row(row_id=_SESS_SIB, canonical="ext-shared"))
    # 5 live events (3 under canonical, 2 under sibling) + 1 soft-deleted.
    _insert(provider, tracker, "event",
            _event_row(suffix="e01", session_id=_SESS_CANON, sequence=1, blob=None))
    _insert(provider, tracker, "event",
            _event_row(suffix="e02", session_id=_SESS_CANON, sequence=2, blob="b02"))
    _insert(provider, tracker, "event",
            _event_row(suffix="e03", session_id=_SESS_CANON, sequence=3, blob="b03"))
    _insert(provider, tracker, "event",
            _event_row(suffix="e04", session_id=_SESS_SIB, sequence=1, blob=None))
    _insert(provider, tracker, "event",
            _event_row(suffix="e05", session_id=_SESS_SIB, sequence=2, blob="b05"))
    _insert(provider, tracker, "event",
            _event_row(suffix="e06", session_id=_SESS_CANON, sequence=4,
                       blob="b06", is_deleted=1))


def _floored_ordered_reader(real: _OrderedReader) -> _OrderedReader:
    """Wrap the repo's ``_query_ordered`` to floor the FIRST page at the sentinel.

    The census fold starts with no ``id`` filter (whole-table scan); flooring the
    first page at the sentinel prefix bounds the real ``provider.select_ordered``
    to the seeded rows AT THE SQL LEVEL. Later pages carry the fold's own
    ``id > last_id`` cursor (a sentinel id > prefix), so they stay bounded.
    """

    def reader(
        table: str,
        *,
        filters: dict[str, object],
        order_by: list[list[str]],
        limit: int,
    ) -> list[dict[str, object]]:
        scoped = dict(filters)
        if "id" not in scoped:
            scoped["id"] = {"op": "gt", "value": _PREFIX}
        return real(table, filters=scoped, order_by=order_by, limit=limit)

    return reader


def _census_for_sentinel(
    repo: SessionLedgerRepository,
    *,
    page_size: int,
) -> dict[str, object]:
    """Fold the sentinel census via real scoped reads + the real keyset scan."""
    aggregator = _CensusAggregator(
        sources=repo._query(TABLE_SOURCE, {"id": _SRC_ID, "is_deleted": 0}),  # noqa: SLF001
        sessions=repo._query(  # noqa: SLF001
            TABLE_SESSION, {"source_id": _SRC_ID, "is_deleted": 0},
        ),
        tool_calls=[],
        import_batches=[],
        fingerprint_seeds=_SEEDS,
    )
    fold_census_events(
        aggregator,
        query_ordered=_floored_ordered_reader(repo._query_ordered),  # noqa: SLF001
        table=TABLE_EVENT,
        page_size=page_size,
    )
    rows = aggregator.result(now=_naive(_NOW))
    return next(r for r in rows if str(r["source_id"]) == _SRC_ID)


def test_census_keyset_paging(repo: SessionLedgerRepository) -> None:
    paged = _census_for_sentinel(repo, page_size=2)   # multi-page keyset
    single = _census_for_sentinel(repo, page_size=100)  # one page

    _check(
        paged["event_count"] == 5,
        "5 live events folded; the soft-deleted e06 is excluded (is_deleted "
        "default of query_ordered)",
    )
    _check(
        paged["session_count"] == 2
        and paged["canonical_count"] == 1
        and paged["sibling_count"] == 1,
        "session counts from real query_state: 2 total, 1 canonical, 1 sibling",
    )
    _check(
        isinstance(paged["fingerprint_a"], int)
        and isinstance(paged["fingerprint_b"], int),
        "present two-seed fingerprints over the real event rows",
    )
    _check(
        paged["event_count"] == single["event_count"]
        and paged["fingerprint_a"] == single["fingerprint_a"]
        and paged["fingerprint_b"] == single["fingerprint_b"],
        "page_size=2 (multi-page) == page_size=100 (single page): the real "
        "(id,created_at) keyset cursor pages every event exactly once",
    )

    # Determinism across runs against real rows (blake2b, not salted hash()).
    again = _census_for_sentinel(repo, page_size=2)
    _check(
        again["fingerprint_a"] == paged["fingerprint_a"]
        and again["fingerprint_b"] == paged["fingerprint_b"],
        "fingerprint is deterministic across runs on the real corpus",
    )


def test_event_keyset_read_shape(repo: SessionLedgerRepository) -> None:
    # Direct keyset read of the sentinel events — proves the real query_ordered
    # returns them id-ascending, excludes the soft-deleted row, and yields the
    # field shapes the fold reads.
    rows = repo._query_ordered(  # noqa: SLF001
        TABLE_EVENT,
        filters={"id": {"op": "gt", "value": _PREFIX}},
        order_by=[["id", "asc"], ["created_at", "asc"]],
        limit=100,
    )
    ids = [str(r["id"]) for r in rows if str(r["id"]).startswith(_PREFIX)]
    _check(
        ids == [f"{_PREFIX}e0{n}" for n in (1, 2, 3, 4, 5)],
        "real query_ordered returns the 5 live sentinel events in id-ascending "
        "order; the soft-deleted e06 is excluded",
    )
    if rows:
        _check(
            all(isinstance(r["id"], str) for r in rows)
            and all(
                r.get("content_blob_id") is None
                or isinstance(r.get("content_blob_id"), str)
                for r in rows
            ),
            "event rows carry str id + str|None content_blob_id (the fold inputs)",
        )

    # Production's FIRST census page is filters={} (whole-table scan). Prove that
    # exact shape runs against real Postgres (the floored reader never hits it).
    first_page = repo._query_ordered(  # noqa: SLF001
        TABLE_EVENT,
        filters={},
        order_by=[["id", "asc"], ["created_at", "asc"]],
        limit=1,
    )
    _check(
        len(first_page) <= 1,
        "census first-page shape (empty filters, (id,created_at) ASC, limit=1) "
        "runs on real Postgres and honors the limit",
    )


def main() -> int:
    if os.environ.get("LEDGER_READ_LIVE_SMOKE") != "1":
        print("=== census_migration_live_smoke ===")
        print(
            "  SKIP  set LEDGER_READ_LIVE_SMOKE=1 to run "
            "(needs the live solet DB)"
        )
        return 0

    print("=== census_migration_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    repo = SessionLedgerRepository(
        state_service=cast("Any", _LiveStateAdapter(provider)),
        clock=lambda: _NOW,
    )
    tracker: list[tuple[str, str]] = []
    try:
        _seed(provider, tracker)
        print("test_census_keyset_paging")
        test_census_keyset_paging(repo)
        print("test_event_keyset_read_shape")
        test_event_keyset_read_shape(repo)
    finally:
        for table, row_id in reversed(tracker):
            _hard_delete_by_id(provider, table, row_id)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
