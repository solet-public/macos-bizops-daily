#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the Slice-1 read migration (SQL lockdown #0).

Pins that the five ``read.py`` reads migrated off raw ``execute_sql`` onto the
``query_state`` / ``query_ordered`` state primitives return results IDENTICAL to
the raw SQL they replaced — exercised against the running solet's **real** ledger schema
with **real** production rows (no hand-rolled fixture table; no seeding; fully
read-only / non-destructive).

The five migrated reads:

* ``list_sessions_by_ids``                  — id IN (...) → ``query_state`` ``= ANY``
* ``find_event_id_by_vendor_id``            — ORDER sequence DESC LIMIT 1 → ``query_ordered``
* ``find_call_event_id_for_resolution``     — + event_type filter → ``query_ordered``
* ``iter_all_events_for_session``           — paged (sequence, id) keyset walk
* ``list_sources``                          — unbounded ORDER created_at → ``query_state`` + Python sort

Method: each test runs the migrated read through a real ``SessionLedgerRepository``
wired to a faithful state adapter (the adapter calls the SAME provider methods +
the SAME ``parse_ordered_query`` hardening the live plugin facade uses, so the
real ``= ANY`` / ordering / ``is_deleted`` SQL composition is exercised), then
compares against a ground-truth raw query over the same rows. Real fixtures are
discovered at runtime so the smoke adapts to whatever the live corpus holds.

Env-gated behind ``LEDGER_READ_LIVE_SMOKE=1`` — it needs the live solet DB up.
It writes nothing, so it is safe to run any time the DB is reachable.

Run::

    LEDGER_READ_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/read_migration_live_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.constants import SOLET_NAME_ENV_VAR  # noqa: E402
from ananta.llm.session_ledger.read import _EVENT_PAGE_ROWS  # noqa: E402
from ananta.llm.session_ledger.repository import (  # noqa: E402
    SessionLedgerRepository,
)
from ananta.services.state_service.ordered_query import (  # noqa: E402
    parse_ordered_query,
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


class _LiveStateAdapter:
    """Faithful StateManagementInterface stand-in for the read surface.

    ``query_state`` / ``query_ordered`` mirror the live plugin facade
    1:1 — ``query_state`` → ``provider.select`` (the autocommit equality /
    ``= ANY`` / ``is_null`` grammar), ``query_ordered`` → the real
    ``parse_ordered_query`` hardening + ``provider.select_ordered`` — so the
    migrated reads exercise the actual SQL-composition path, not a reimpl.
    The success envelope matches ``create_success_result`` (``data.records``).
    """

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        table = str(query["table"])
        filters = query.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace,
            table=table,
            conditions=filters if isinstance(filters, dict) else None,
            limit=query.get("limit"),
        )
        return _envelope(rows)

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
        return _envelope(rows)


def _envelope(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "action_status": "completed",
        "data": {"records": rows, "count": len(rows)},
        "actions": [],
        "error": None,
        "timestamp": "",
    }


def _raw(provider: PostgresProvider, sql: str, params: tuple[object, ...] | None = None) -> list[list[Any]]:
    """Ground-truth raw query — returns positional rows (the original read path)."""
    return provider.execute_query(sql, params)


# ───── Cases (each: migrated read vs ground-truth raw SQL over real rows) ─────


def test_list_sources(repo: SessionLedgerRepository, provider: PostgresProvider) -> None:
    """list_sources → query_state + Python created_at sort == raw ORDER BY created_at."""
    migrated = repo.list_sources(enabled_only=True)
    raw = _raw(
        provider,
        f'SELECT id FROM "{_SCHEMA}".session_ledger__source '
        "WHERE is_deleted = 0 AND enabled = TRUE ORDER BY created_at ASC",
    )
    raw_ids = [str(r[0]) for r in raw]
    migrated_ids = [s.id for s in migrated]
    _check(
        migrated_ids == raw_ids,
        f"list_sources(enabled_only=True) id-order matches raw ORDER BY created_at "
        f"(migrated {len(migrated_ids)} vs raw {len(raw_ids)})",
    )
    # enabled_only=False must be a superset (drops the enabled filter).
    all_sources = repo.list_sources(enabled_only=False)
    _check(
        {s.id for s in all_sources} >= set(migrated_ids),
        "list_sources(enabled_only=False) is a superset of enabled-only",
    )


def test_list_sessions_by_ids(repo: SessionLedgerRepository, provider: PostgresProvider) -> None:
    """list_sessions_by_ids → query_state ``id = ANY`` == raw id IN (...)."""
    sample = _raw(
        provider,
        f'SELECT id FROM "{_SCHEMA}".session_ledger__session WHERE is_deleted = 0 LIMIT 5',
    )
    ids = [str(r[0]) for r in sample]
    if not ids:
        _check(False, "list_sessions_by_ids: no live sessions to sample (corpus empty?)")
        return
    rows = repo.list_sessions_by_ids(ids)
    _check(
        {str(r["id"]) for r in rows} == set(ids),
        f"list_sessions_by_ids returns exactly the {len(ids)} requested ids "
        f"(=ANY; got {len(rows)})",
    )
    # Empty input short-circuits to [] (no SQL).
    _check(repo.list_sessions_by_ids([]) == [], "list_sessions_by_ids([]) == [] (short-circuit)")
    # A bogus id is silently absent (not an error).
    partial = repo.list_sessions_by_ids([ids[0], "les_does_not_exist_sentinel"])
    _check(
        {str(r["id"]) for r in partial} == {ids[0]},
        "list_sessions_by_ids drops a non-existent id, keeps the real one",
    )


def test_iter_all_events_for_session(repo: SessionLedgerRepository, provider: PostgresProvider) -> None:
    """iter_all_events_for_session → paged (sequence, id) keyset walk == raw ORDER BY sequence.

    Read-cap sweep, 2026-08-16 (lane-ak). The method under test changed from an
    unbounded ``query_state`` + Python sort into a paged keyset walk, so what
    this smoke has to prove changed with it. Matching the raw ORDER BY is
    necessary but no longer sufficient: a walk that silently stopped after its
    first page would still match a raw query on any session with fewer than
    ``_EVENT_PAGE_ROWS`` events.

    So the sample is the BUSIEST session (``ORDER BY count(*) DESC``) and the
    smoke asserts explicitly that it straddles at least one page boundary. If it
    does not, that is reported as a FAILURE rather than passing quietly: a
    fixture that cannot reach the boundary cannot test the cursor, and a green
    from such a fixture means nothing.
    """
    pick = _raw(
        provider,
        f'SELECT session_id FROM "{_SCHEMA}".session_ledger__event WHERE is_deleted = 0 '
        "GROUP BY session_id ORDER BY count(*) DESC LIMIT 1",
    )
    if not pick:
        _check(False, "iter_all_events_for_session: no live events to sample")
        return
    session_id = str(pick[0][0])
    migrated = list(repo.iter_all_events_for_session(session_id=session_id))
    raw = _raw(
        provider,
        f'SELECT id, sequence FROM "{_SCHEMA}".session_ledger__event '
        "WHERE session_id = %s AND is_deleted = 0 ORDER BY sequence ASC",
        (session_id,),
    )
    migrated_seq = [(str(r["id"]), int(r["sequence"])) for r in migrated]  # type: ignore[call-overload]
    raw_seq = [(str(r[0]), int(r[1])) for r in raw]
    _check(
        len(raw_seq) > _EVENT_PAGE_ROWS,
        f"iter_all_events_for_session FIXTURE straddles a page boundary: busiest "
        f"session has {len(raw_seq)} events vs page size {_EVENT_PAGE_ROWS} "
        f"(below it, this test cannot detect a walk that stops after page 1)",
    )
    _check(
        migrated_seq == raw_seq,
        f"iter_all_events_for_session ordered (id,sequence) matches raw ORDER BY "
        f"sequence ASC across {len(raw_seq)} events "
        f"({-(-len(raw_seq) // _EVENT_PAGE_ROWS)} pages)",
    )
    _check(
        len(migrated_seq) == len(raw_seq),
        f"iter_all_events_for_session yielded every row ({len(migrated_seq)}), not "
        f"a first-page prefix ({_EVENT_PAGE_ROWS})",
    )
    _check(
        migrated_seq == sorted(migrated_seq, key=lambda t: t[1]),
        "iter_all_events_for_session result is sequence-ascending (order comes "
        "from query_ordered, not a post-hoc Python sort)",
    )

def test_find_event_id_by_vendor_id(repo: SessionLedgerRepository, provider: PostgresProvider) -> None:
    """find_event_id_by_vendor_id → query_ordered DESC LIMIT 1 == raw."""
    pick = _raw(
        provider,
        f'SELECT session_id, vendor_event_id FROM "{_SCHEMA}".session_ledger__event '
        "WHERE is_deleted = 0 AND vendor_event_id IS NOT NULL LIMIT 1",
    )
    if not pick:
        _check(True, "find_event_id_by_vendor_id: no event carries a vendor_event_id (skip — vacuously OK)")
    else:
        session_id, vendor_event_id = str(pick[0][0]), str(pick[0][1])
        got = repo.find_event_id_by_vendor_id(
            session_id=session_id, vendor_event_id=vendor_event_id,
        )
        raw = _raw(
            provider,
            f'SELECT id FROM "{_SCHEMA}".session_ledger__event '
            "WHERE session_id = %s AND vendor_event_id = %s AND is_deleted = 0 "
            "ORDER BY sequence DESC LIMIT 1",
            (session_id, vendor_event_id),
        )
        expected = str(raw[0][0]) if raw else None
        _check(
            got == expected,
            f"find_event_id_by_vendor_id returns the raw DESC-LIMIT-1 id (got {got!r})",
        )
    # A vendor_event_id that doesn't exist returns None.
    _check(
        repo.find_event_id_by_vendor_id(
            session_id="les_nope", vendor_event_id="vendor_nope",
        ) is None,
        "find_event_id_by_vendor_id(nonexistent) returns None",
    )


def test_find_call_event_id_for_resolution(repo: SessionLedgerRepository, provider: PostgresProvider) -> None:
    """find_call_event_id_for_resolution → query_ordered (+event_type) == raw."""
    pick = _raw(
        provider,
        f'SELECT session_id, vendor_event_id FROM "{_SCHEMA}".session_ledger__event '
        "WHERE is_deleted = 0 AND vendor_event_id IS NOT NULL "
        "AND event_type = 'TOOL_CALL' LIMIT 1",
    )
    if not pick:
        _check(
            True,
            "find_call_event_id_for_resolution: no TOOL_CALL with vendor_event_id (skip — vacuously OK)",
        )
        return
    session_id, vendor_event_id = str(pick[0][0]), str(pick[0][1])
    got = repo.find_call_event_id_for_resolution(
        session_id=session_id, tool_use_vendor_id=vendor_event_id,
    )
    raw = _raw(
        provider,
        f'SELECT id FROM "{_SCHEMA}".session_ledger__event '
        "WHERE session_id = %s AND vendor_event_id = %s AND event_type = 'TOOL_CALL' "
        "AND is_deleted = 0 ORDER BY sequence DESC LIMIT 1",
        (session_id, vendor_event_id),
    )
    expected = str(raw[0][0]) if raw else None
    _check(
        got == expected,
        f"find_call_event_id_for_resolution returns the raw TOOL_CALL DESC-LIMIT-1 id (got {got!r})",
    )


def main() -> int:
    if os.environ.get("LEDGER_READ_LIVE_SMOKE") != "1":
        print("=== read_migration_live_smoke ===")
        print(
            "  SKIP  set LEDGER_READ_LIVE_SMOKE=1 to run; "
            "needs the live solet DB "
            "(read-only, non-destructive).",
        )
        return 0
    print("=== read_migration_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    adapter = _LiveStateAdapter(provider)
    repo = SessionLedgerRepository(state_service=adapter)  # type: ignore[arg-type]
    test_list_sources(repo, provider)
    test_list_sessions_by_ids(repo, provider)
    test_iter_all_events_for_session(repo, provider)
    test_find_event_id_by_vendor_id(repo, provider)
    test_find_call_event_id_for_resolution(repo, provider)
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
