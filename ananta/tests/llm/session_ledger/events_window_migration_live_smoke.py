#!/usr/bin/env python3
"""Live-Postgres smoke for the ``list_events_by_source_window`` denormalize migration.

SQL-lockdown Slice 7 (Architect-ruled denormalize): the verb's 3-table JOIN
retires onto a single-table ``query_ordered`` over ``__event`` using the
``session_vendor`` + ``source_kind`` columns denormalized at append time; pre-
migration rows are filled once by ``backfill_event_source_denormalization``.

The read shape + the window post-filter + the global backfill DRIVER are proven
offline in ``events_window_fold_smoke.py``. This live smoke proves the two real-
Postgres behaviors the offline shim cannot:

* the REAL ``query_ordered`` read (``parse_ordered_query`` → ``provider.select_ordered``)
  over the new ``source_kind`` / ``session_vendor`` columns + the ``event_at <=
  until`` keyset window returns the seeded events DESC, with the projected
  envelope — i.e. the new columns actually EXIST and read back;
* the REAL per-session backfill WRITE (``_update`` → ``provider.update``) fills a
  sentinel session's NULL ``session_vendor`` / ``source_kind`` and is idempotent.

The global ``backfill_event_source_denormalization`` driver is NOT run here — it
scans EVERY session and would mutate the whole real corpus; its session-keyset
paging is covered offline. This smoke calls the per-session helper on a sentinel
ONLY, so it touches nothing but the seeded rows.

⚠ POST-ADOPTION ONLY: needs the Slice-7 schema (``session_vendor`` + ``source_kind``
on ``__event``) to have been adopted on the live DB. Before adoption it fails
with "column does not exist" — that is expected, not a regression.

Read-isolation: sentinels are seeded at a DISTANT-FUTURE ``event_at`` (2099) and
the read window is ``[2098, 2100]``, so no real event shares the window — the
result is the sentinels alone, regardless of corpus size. Env-gated behind
``LEDGER_READ_LIVE_SMOKE=1``; sentinels are hard-deleted in ``finally``.

Run::

    LEDGER_READ_LIVE_SMOKE=1 \\
      .venv/bin/python3 \\
      ananta/tests/llm/session_ledger/events_window_migration_live_smoke.py
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
    """StateManagementInterface stand-in over a real provider — prod paths verbatim.

    ``query_state`` → ``provider.select``; ``query_ordered`` →
    ``parse_ordered_query`` → ``provider.select_ordered``; ``update_state`` →
    ``provider.update`` wrapped in the ``data.result.updated`` envelope (the exact
    ``plugin.update_state`` shape ``base._update`` parses). ``transactional`` is
    present only so the repository constructs cleanly.
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

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any],
    ) -> dict[str, Any]:
        updated = self._provider.update(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", query.get("filters") or {}),
            updates=updates,
        )
        return _envelope({"namespace": namespace, "result": {"updated": updated}})

    @contextmanager
    def transactional(self) -> Any:
        with self._provider.get_transactional_connection() as conn:
            yield _PostgresStateTransaction(conn, self._provider)


_MARK = "__events_window_migration_live_smoke__"
_PREFIX = f"zzzz_window_{_MARK}_"
_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)
# Distant-future window so only the sentinels occupy [since, until].
_SINCE = datetime(2098, 1, 1, 0, 0, 0, tzinfo=UTC)
_UNTIL = datetime(2100, 1, 1, 0, 0, 0, tzinfo=UTC)
_SRC_ID = f"{_PREFIX}src"
_SESS_READ = f"{_PREFIX}s_read"
_SESS_BACKFILL = f"{_PREFIX}s_backfill"


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _at(*, year: int = 2099, month: int = 6, day: int = 15, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, 0, 0)


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
        "root_uri": f"/window/{_MARK}",
        "enabled": True,
        "created_at": _naive(_NOW),
        "updated_at": _naive(_NOW),
        "is_deleted": 0,
    }


def _session_row(row_id: str) -> dict[str, Any]:
    return {
        "id": row_id,
        "namespace": "session_ledger",
        "source_id": _SRC_ID,
        "external_session_id": f"ext-{row_id}",
        "vendor": SourceVendor.CODEX.value,
        "first_event_at": _naive(_NOW),
        "last_event_at": _naive(_NOW),
        "event_count": 0,
        "canonical_external_session_id": None,
        "created_at": _naive(_NOW),
        "updated_at": _naive(_NOW),
        "is_deleted": 0,
    }


def _event_row(
    *,
    suffix: str,
    session_id: str,
    sequence: int,
    event_at: datetime,
    session_vendor: str | None,
    source_kind: str | None,
) -> dict[str, Any]:
    return {
        "id": f"{_PREFIX}{suffix}",
        "namespace": "session_ledger",
        "session_id": session_id,
        "sequence": sequence,
        "event_type": EventType.MESSAGE.value,
        "role": "user",
        "content_text": f"body-{suffix}",
        "event_at": event_at,
        "imported_at": _naive(_NOW),
        "batch_id": f"{_PREFIX}batch",
        "session_vendor": session_vendor,
        "source_kind": source_kind,
        "created_at": _naive(_NOW),
        "updated_at": _naive(_NOW),
        "is_deleted": 0,
    }


def _seed(provider: PostgresProvider, tracker: list[tuple[str, str]]) -> None:
    _insert(provider, tracker, "source", _source_row())
    _insert(provider, tracker, "session", _session_row(_SESS_READ))
    _insert(provider, tracker, "session", _session_row(_SESS_BACKFILL))
    cx, ck = SourceVendor.CODEX.value, IngestSourceKind.CODEX_LOCAL.value
    # READ session: denormalized events spanning the [2098, 2100] window.
    _insert(provider, tracker, "event", _event_row(
        suffix="r_after", session_id=_SESS_READ, sequence=1,
        event_at=_at(year=2100, month=6), session_vendor=cx, source_kind=ck))  # > until → excluded
    _insert(provider, tracker, "event", _event_row(
        suffix="r_atuntil", session_id=_SESS_READ, sequence=2,
        event_at=_naive(_UNTIL), session_vendor=cx, source_kind=ck))  # == until → in
    _insert(provider, tracker, "event", _event_row(
        suffix="r_mid", session_id=_SESS_READ, sequence=3,
        event_at=_at(year=2099, month=6), session_vendor=cx, source_kind=ck))  # in
    _insert(provider, tracker, "event", _event_row(
        suffix="r_atsince", session_id=_SESS_READ, sequence=4,
        event_at=_naive(_SINCE), session_vendor=cx, source_kind=ck))  # == since → in
    _insert(provider, tracker, "event", _event_row(
        suffix="r_before", session_id=_SESS_READ, sequence=5,
        event_at=_at(year=2097, month=6), session_vendor=cx, source_kind=ck))  # < since → excluded
    # BACKFILL session: NULL denorm columns (pre-migration shape).
    _insert(provider, tracker, "event", _event_row(
        suffix="b_01", session_id=_SESS_BACKFILL, sequence=1,
        event_at=_at(year=2099, month=7), session_vendor=None, source_kind=None))
    _insert(provider, tracker, "event", _event_row(
        suffix="b_02", session_id=_SESS_BACKFILL, sequence=2,
        event_at=_at(year=2099, month=8), session_vendor=None, source_kind=None))


def test_real_read_window(repo: SessionLedgerRepository) -> None:
    rows = repo.list_events_by_source_window(
        source_kind=IngestSourceKind.CODEX_LOCAL.value,
        vendor=SourceVendor.CODEX.value,
        since=_SINCE,
        until=_UNTIL,
        limit=50,
    )
    sentinel = [r for r in rows if str(r["event_id"]).startswith(_PREFIX)]
    ids = [str(r["event_id"]) for r in sentinel]
    _check(
        ids == [f"{_PREFIX}r_atuntil", f"{_PREFIX}r_mid", f"{_PREFIX}r_atsince"],
        "real query_ordered: [since, until] inclusive both ends, > until + < since "
        "excluded, DESC by event_at (the new columns exist + read back)",
    )
    if sentinel:
        top = sentinel[0]
        _check(
            set(top) == {"event_id", "session_id", "sequence", "event_at", "role",
                         "content_text", "session_vendor", "source_kind"}
            and top["session_vendor"] == SourceVendor.CODEX.value
            and top["source_kind"] == IngestSourceKind.CODEX_LOCAL.value
            and isinstance(top["event_at"], str),
            "real read: projected 8-key envelope; denormalized vendor/source_kind; "
            "event_at as an ISO string",
        )


def _read_backfill_events(repo: SessionLedgerRepository) -> list[dict[str, object]]:
    return repo._query(  # noqa: SLF001
        "event", {"session_id": _SESS_BACKFILL, "is_deleted": 0},
    )


def test_real_per_session_backfill_update(repo: SessionLedgerRepository) -> None:
    session = next(
        s for s in repo._query("session", {"id": _SESS_BACKFILL, "is_deleted": 0})  # noqa: SLF001
    )
    source_kind_by_id = {_SRC_ID: IngestSourceKind.CODEX_LOCAL.value}
    filled = repo._denormalize_session_events(  # noqa: SLF001
        session, source_kind_by_id=source_kind_by_id, now=_naive(_NOW),
    )
    _check(
        filled == 2,
        "real update_state (provider.update): the 2 NULL sentinel events were filled",
    )
    after = {str(e["id"]): e for e in _read_backfill_events(repo)}
    _check(
        all(
            after[f"{_PREFIX}{s}"]["session_vendor"] == SourceVendor.CODEX.value
            and after[f"{_PREFIX}{s}"]["source_kind"] == IngestSourceKind.CODEX_LOCAL.value
            for s in ("b_01", "b_02")
        ),
        "real read-back: both events now carry session.vendor + source.source_kind",
    )
    again = repo._denormalize_session_events(  # noqa: SLF001
        session, source_kind_by_id=source_kind_by_id, now=_naive(_NOW),
    )
    _check(
        again == 0,
        "real backfill idempotent: a second per-session pass fills nothing "
        "(session_vendor IS NULL now matches no row)",
    )


def main() -> int:
    if os.environ.get("LEDGER_READ_LIVE_SMOKE") != "1":
        print("=== events_window_migration_live_smoke ===")
        print(
            "  SKIP  set LEDGER_READ_LIVE_SMOKE=1 to run "
            "(needs the live solet DB with the Slice-7 schema adopted)"
        )
        return 0

    print("=== events_window_migration_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    repo = SessionLedgerRepository(
        state_service=cast("Any", _LiveStateAdapter(provider)),
        clock=lambda: _NOW,
    )
    tracker: list[tuple[str, str]] = []
    try:
        _seed(provider, tracker)
        print("test_real_read_window")
        test_real_read_window(repo)
        print("test_real_per_session_backfill_update")
        test_real_per_session_backfill_update(repo)
    finally:
        for table, row_id in reversed(tracker):
            _hard_delete_by_id(provider, table, row_id)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
