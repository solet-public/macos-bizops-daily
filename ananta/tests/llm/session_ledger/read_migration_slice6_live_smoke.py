#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the Slice-6a read migration (SQL lockdown).

Pins the two read-mixin methods migrated off raw ``execute_sql`` (``_fetch_all``)
onto the state-interface ``query_state`` primitive + a faithful Python
post-filter / post-sort. Unlike the read-only Slice-1 smoke
(``read_migration_live_smoke.py``), these two reads have order/filter logic that
the live corpus cannot reliably exercise (canonical-vs-sibling, subtype edges),
so this smoke SEEDS sentinel rows and hard-deletes them in a ``finally``:

* ``find_session_id_by_external_session_id`` — the SQL led its ``ORDER BY`` with
  a computed boolean (``canonical_external_session_id IS NOT NULL``) the flat
  ``query_state`` grammar cannot express, so the canonical-first / ``created_at``
  ASC top-1 pick now happens in Python. Verifies: single-canonical, canonical
  wins over a NEWER sibling, oldest-wins when no canonical exists, ``is_deleted``
  excluded (even when the deleted row is older), and the empty case.
* ``find_latest_away_summary_for_session`` — the SQL filtered
  ``content_json->>'subtype' = 'away_summary'`` (a JSONB path extraction the flat
  grammar cannot express) and ordered ``event_at`` DESC, ``sequence`` DESC LIMIT
  1. Verifies: most-recent away_summary wins, a different subtype / a missing
  subtype key / a NON-OBJECT content_json are all skipped WITHOUT raising (the
  ``->>'subtype'`` → NULL semantic that ``_coerce_json_dict`` would have raised
  on), ``is_deleted`` excluded, and the no-match case. Also asserts the real
  ``query_state`` path hands ``content_json`` back as a ``dict`` (the production
  JSONB-deserialization seam this read now depends on).

There are NO DB-level foreign keys (FKs are repository-enforced per schema.py),
so sentinel ``source_id`` / ``session_id`` / ``batch_id`` strings are valid.
Env-gated behind ``LEDGER_READ_LIVE_SMOKE=1``.

Run::

    LEDGER_READ_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/read_migration_slice6_live_smoke.py
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
    EventType,
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
    """Minimal StateManagementInterface stand-in over a real provider.

    The two migrated reads call only ``query_state``; ``transactional`` is
    present so the concrete ``SessionLedgerRepository`` constructs cleanly.
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


_MARK = "__read_migration_slice6_live_smoke__"
_SCHEMA = os.environ[SOLET_NAME_ENV_VAR]
_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


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


def _session_row(
    *,
    row_id: str,
    source_id: str,
    external_session_id: str,
    canonical: str | None,
    created_at: datetime,
    is_deleted: int = 0,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "namespace": "session_ledger",
        "source_id": source_id,
        "external_session_id": external_session_id,
        "vendor": SourceVendor.CLAUDE_CODE.value,
        "first_event_at": _naive(_T0),
        "last_event_at": _naive(_T0),
        "event_count": 0,
        "canonical_external_session_id": canonical,
        "created_at": _naive(created_at),
        "updated_at": _naive(created_at),
        "is_deleted": is_deleted,
    }


def _event_row(
    *,
    row_id: str,
    session_id: str,
    sequence: int,
    content_json: object,
    content_text: str | None,
    event_at: datetime,
    is_deleted: int = 0,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "namespace": "session_ledger",
        "session_id": session_id,
        "sequence": sequence,
        "event_type": EventType.SYSTEM.value,
        "content_json": content_json,
        "content_text": content_text,
        "event_at": _naive(event_at),
        "imported_at": _naive(event_at),
        "batch_id": f"imb-{_MARK}",
        "created_at": _naive(_T0),
        "updated_at": _naive(_T0),
        "is_deleted": is_deleted,
    }


def test_find_session_id_by_external_session_id(
    repo: SessionLedgerRepository, provider: PostgresProvider, tracker: list[tuple[str, str]]
) -> None:
    # Case A — single canonical row.
    a1 = _insert(provider, tracker, "session", _session_row(
        row_id=f"les-A1-{_MARK}", source_id=f"srcA1-{_MARK}",
        external_session_id=f"extA-{_MARK}", canonical=None, created_at=_T0,
    ))
    _check(
        repo.find_session_id_by_external_session_id(f"extA-{_MARK}") == a1,
        "single canonical row is returned",
    )

    # Case B — canonical + sibling; canonical wins even though it is NEWER.
    b_canon = _insert(provider, tracker, "session", _session_row(
        row_id=f"les-Bc-{_MARK}", source_id=f"srcB1-{_MARK}",
        external_session_id=f"extB-{_MARK}", canonical=None,
        created_at=_T0 + timedelta(days=10),
    ))
    _insert(provider, tracker, "session", _session_row(
        row_id=f"les-Bs-{_MARK}", source_id=f"srcB2-{_MARK}",
        external_session_id=f"extB-{_MARK}", canonical=f"extB-{_MARK}",
        created_at=_T0,
    ))
    _check(
        repo.find_session_id_by_external_session_id(f"extB-{_MARK}") == b_canon,
        "canonical row wins over an older sibling (canonical-first, not created_at)",
    )

    # Case C — no canonical; oldest non-canonical row wins on created_at ASC.
    _insert(provider, tracker, "session", _session_row(
        row_id=f"les-C1-{_MARK}", source_id=f"srcC1-{_MARK}",
        external_session_id=f"extC-{_MARK}", canonical=f"extC-{_MARK}",
        created_at=_T0 + timedelta(days=5),
    ))
    c_old = _insert(provider, tracker, "session", _session_row(
        row_id=f"les-C2-{_MARK}", source_id=f"srcC2-{_MARK}",
        external_session_id=f"extC-{_MARK}", canonical=f"extC-{_MARK}",
        created_at=_T0,
    ))
    _check(
        repo.find_session_id_by_external_session_id(f"extC-{_MARK}") == c_old,
        "no canonical → oldest-created sibling wins (created_at ASC)",
    )

    # Case D — is_deleted excluded even when the deleted row is older.
    d_live = _insert(provider, tracker, "session", _session_row(
        row_id=f"les-Dl-{_MARK}", source_id=f"srcD1-{_MARK}",
        external_session_id=f"extD-{_MARK}", canonical=None, created_at=_T0,
    ))
    _insert(provider, tracker, "session", _session_row(
        row_id=f"les-Dd-{_MARK}", source_id=f"srcD2-{_MARK}",
        external_session_id=f"extD-{_MARK}", canonical=None,
        created_at=_T0 - timedelta(days=1), is_deleted=1,
    ))
    _check(
        repo.find_session_id_by_external_session_id(f"extD-{_MARK}") == d_live,
        "is_deleted=1 row excluded (even though it is older)",
    )

    # Case E — no rows.
    _check(
        repo.find_session_id_by_external_session_id(f"ext-none-{_MARK}") is None,
        "no matching external_session_id → None",
    )


def test_find_latest_away_summary_for_session(
    repo: SessionLedgerRepository, provider: PostgresProvider, tracker: list[tuple[str, str]]
) -> None:
    sess = f"les-AS-{_MARK}"

    # e1/e2: two away_summary events; the most-recent (event_at DESC) wins.
    e1 = _insert(provider, tracker, "event", _event_row(
        row_id=f"evt-AS1-{_MARK}", session_id=sess, sequence=1,
        content_json={"subtype": "away_summary"}, content_text="  recap-old  ",
        event_at=_T0,
    ))
    _insert(provider, tracker, "event", _event_row(
        row_id=f"evt-AS2-{_MARK}", session_id=sess, sequence=2,
        content_json={"subtype": "away_summary"}, content_text="recap-new",
        event_at=_T0 + timedelta(hours=1),
    ))
    # e3: different subtype → skipped.
    _insert(provider, tracker, "event", _event_row(
        row_id=f"evt-AS3-{_MARK}", session_id=sess, sequence=3,
        content_json={"subtype": "tool_use"}, content_text="nope",
        event_at=_T0 + timedelta(hours=2),
    ))
    # e4: object without a subtype key → skipped, no crash.
    _insert(provider, tracker, "event", _event_row(
        row_id=f"evt-AS4-{_MARK}", session_id=sess, sequence=4,
        content_json={"other": "x"}, content_text="nope2",
        event_at=_T0 + timedelta(hours=3),
    ))
    # e5: NON-OBJECT content_json (top-level JSON array) → skipped, no crash.
    #     This is the exact payload _coerce_json_dict would have RAISED on; the
    #     SQL ``->>'subtype'`` returned NULL for it, so the faithful Python
    #     filter must skip-not-raise.
    _insert(provider, tracker, "event", _event_row(
        row_id=f"evt-AS5-{_MARK}", session_id=sess, sequence=5,
        content_json=["not", "an", "object"], content_text="nope3",
        event_at=_T0 + timedelta(hours=4),
    ))
    # e6: is_deleted away_summary, NEWER than e2 → excluded.
    _insert(provider, tracker, "event", _event_row(
        row_id=f"evt-AS6-{_MARK}", session_id=sess, sequence=6,
        content_json={"subtype": "away_summary"}, content_text="deleted-recap",
        event_at=_T0 + timedelta(hours=5), is_deleted=1,
    ))

    _check(
        repo.find_latest_away_summary_for_session(sess) == "recap-new",
        "most-recent live away_summary wins; trimmed; deleted/other-subtype/"
        "non-object skipped without raising",
    )

    # The production query_state path must hand content_json back as a dict (the
    # JSONB-deserialization seam this read now depends on).
    e1_rows = provider.select(
        namespace="session_ledger", table="event", conditions={"id": e1},
    )
    _check(
        len(e1_rows) == 1 and isinstance(e1_rows[0].get("content_json"), dict),
        "content_json reads back as a dict from the query_state path (provider.select)",
    )

    # No away_summary anywhere → None.
    _check(
        repo.find_latest_away_summary_for_session(f"les-none-{_MARK}") is None,
        "session with no away_summary event → None",
    )

    # Sibling-aware reuse (2026-06-30 cross-source dedup fix): the recap is
    # ingested on a SIBLING source row while M6 summarizes the CANONICAL row.
    # A canonical-only lookup misses it (the 0/207 zero-reuse bug); the
    # conversation-group widening must surface it.
    canon = _insert(provider, tracker, "session", _session_row(
        row_id=f"les-ASc-{_MARK}", source_id=f"src-ASc-{_MARK}",
        external_session_id=f"extAS-{_MARK}", canonical=None, created_at=_T0,
    ))
    sib = _insert(provider, tracker, "session", _session_row(
        row_id=f"les-ASs-{_MARK}", source_id=f"src-ASs-{_MARK}",
        external_session_id=f"extAS-{_MARK}", canonical=f"extAS-{_MARK}",
        created_at=_T0,
    ))
    _insert(provider, tracker, "event", _event_row(
        row_id=f"evt-ASsib-{_MARK}", session_id=sib, sequence=1,
        content_json={"subtype": "away_summary"}, content_text="sibling-recap",
        event_at=_T0 + timedelta(hours=1),
    ))
    _check(
        repo.find_latest_away_summary_for_session(canon) == "sibling-recap",
        "canonical reuses the away_summary recap that lives on its sibling "
        "(2026-06-30 cross-source dedup fix)",
    )


def main() -> int:
    if os.environ.get("LEDGER_READ_LIVE_SMOKE") != "1":
        print("=== read_migration_slice6_live_smoke ===")
        print(
            "  SKIP  set LEDGER_READ_LIVE_SMOKE=1 to run "
            "(needs the live solet DB)"
        )
        return 0

    print("=== read_migration_slice6_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    repo = SessionLedgerRepository(state_service=cast("Any", _LiveStateAdapter(provider)))
    tracker: list[tuple[str, str]] = []
    try:
        print("test_find_session_id_by_external_session_id")
        test_find_session_id_by_external_session_id(repo, provider, tracker)
        print("test_find_latest_away_summary_for_session")
        test_find_latest_away_summary_for_session(repo, provider, tracker)
    finally:
        for table, row_id in reversed(tracker):
            _hard_delete_by_id(provider, table, row_id)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
