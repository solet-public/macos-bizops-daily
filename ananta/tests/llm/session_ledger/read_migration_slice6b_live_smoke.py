#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the Slice-6b read migration (SQL lockdown).

Pins that the two ``read.py`` reads migrated off raw ``execute_sql`` onto the
``query_ordered`` state primitive (via the Gap-A comparison-op grammar) return
results IDENTICAL to the raw SQL they replaced — exercised against the **real**
running solet's ledger schema with **real** production rows (read-only, non-destructive):

* ``list_tool_calls``      — equality filters + ``called_at >= since`` (Gap-A gte)
                              + newest-first ``(called_at, id)`` + ≤ 100 cap.
* ``get_session_timeline`` — ``sequence > after_sequence`` cursor (Gap-A gt)
                              + ``(sequence, id)`` asc + ≤ 100 cap + content_json.

Method: run the migrated read through a real ``SessionLedgerRepository`` wired to
a faithful state adapter (the real ``parse_ordered_query`` hardening +
``provider.select_ordered`` — the SAME composition path the live plugin facade
uses, so the REAL comparison-op SQL is exercised, not a reimplementation), then
compare against a ground-truth raw query over the same rows. The boundary split
exercises the gte/gt comparison; one assertion targets an event carrying
non-null ``content_json`` to prove no str<->dict shape flip across the seam.

Env-gated behind ``LEDGER_READ_LIVE_SMOKE=1`` (shared with the Slice-1 read live
smoke). Writes nothing, so it is safe to run any time the DB is reachable.

Run::

    LEDGER_READ_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/read_migration_slice6b_live_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.constants import SOLET_NAME_ENV_VAR  # noqa: E402
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
    """Faithful StateManagementInterface stand-in — ``query_ordered`` → real SQL.

    ``query_ordered`` mirrors the live plugin facade 1:1: the real
    ``parse_ordered_query`` hardening + ``provider.select_ordered``, so the
    migrated reads exercise the actual comparison-op SQL composition (Gap-A
    ``gte``/``gt`` + Gap-C cap), not a reimplementation.
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


def _raw(
    provider: PostgresProvider, sql: str, params: tuple[object, ...] | None = None,
) -> list[list[Any]]:
    """Ground-truth raw query — returns positional rows (the original read path)."""
    return provider.execute_query(sql, params)


# ───── Cases (each: migrated read vs ground-truth raw SQL over real rows) ─────


def test_list_tool_calls_range_order_cap(
    repo: SessionLedgerRepository, provider: PostgresProvider,
) -> None:
    """list_tool_calls → query_ordered: newest-first order + called_at>=since (gte) + cap."""
    migrated = repo.list_tool_calls(limit=100)
    raw = _raw(
        provider,
        f'SELECT id, called_at FROM "{_SCHEMA}".session_ledger__tool_call '
        "WHERE is_deleted = 0 ORDER BY called_at DESC, id DESC LIMIT 100",
    )
    if not raw:
        _check(True, "list_tool_calls: no live tool_calls (skip — vacuously OK)")
        return
    migrated_ids = [str(r["id"]) for r in migrated]  # type: ignore[call-overload]
    raw_ids = [str(r[0]) for r in raw]
    _check(
        migrated_ids == raw_ids,
        f"list_tool_calls newest-first(called_at,id) matches raw "
        f"(migrated {len(migrated_ids)} vs raw {len(raw_ids)})",
    )
    _check(len(migrated_ids) <= 100, "list_tool_calls caps at 100 rows")
    if len(raw) < 2:
        _check(True, "list_tool_calls range: <2 rows, gte split skipped (vacuously OK)")
        return
    # gte range parity (inclusive): split on the median row's called_at.
    boundary = datetime.fromisoformat(str(raw[len(raw) // 2][1]))
    migrated_range = repo.list_tool_calls(since=boundary, limit=100)
    raw_range = _raw(
        provider,
        f'SELECT id FROM "{_SCHEMA}".session_ledger__tool_call '
        "WHERE is_deleted = 0 AND called_at >= %s "
        "ORDER BY called_at DESC, id DESC LIMIT 100",
        (boundary,),
    )
    _check(
        [str(r["id"]) for r in migrated_range] == [str(r[0]) for r in raw_range],  # type: ignore[call-overload]
        f"list_tool_calls(since=boundary) gte-range matches raw called_at>=%s "
        f"(inclusive; {len(raw_range)} rows at/after the median)",
    )


def test_get_session_timeline_cursor_content_json_cap(
    repo: SessionLedgerRepository, provider: PostgresProvider,
) -> None:
    """get_session_timeline → query_ordered: gt cursor + (sequence,id) asc + cap + content_json shape."""
    pick = _raw(
        provider,
        f'SELECT session_id FROM "{_SCHEMA}".session_ledger__event WHERE is_deleted = 0 '
        "GROUP BY session_id ORDER BY count(*) DESC LIMIT 1",
    )
    if not pick:
        _check(True, "get_session_timeline: no live events (skip — vacuously OK)")
        return
    session_id = str(pick[0][0])
    migrated = repo.get_session_timeline(session_id=session_id, after_sequence=0, limit=100)
    raw = _raw(
        provider,
        f'SELECT id, sequence FROM "{_SCHEMA}".session_ledger__event '
        "WHERE session_id = %s AND sequence > 0 AND is_deleted = 0 "
        "ORDER BY sequence ASC, id ASC LIMIT 100",
        (session_id,),
    )
    migrated_seq = [(str(r["id"]), int(r["sequence"])) for r in migrated]  # type: ignore[call-overload]
    raw_seq = [(str(r[0]), int(r[1])) for r in raw]
    _check(
        migrated_seq == raw_seq,
        f"get_session_timeline (sequence,id) asc matches raw first page "
        f"(session with {len(raw_seq)} events ≤ cap)",
    )
    _check(len(migrated_seq) <= 100, "get_session_timeline caps at 100 rows")
    # gt cursor parity (strict): split on a median sequence.
    if len(raw_seq) >= 2:
        boundary_seq = raw_seq[len(raw_seq) // 2][1]
        migrated_after = repo.get_session_timeline(
            session_id=session_id, after_sequence=boundary_seq, limit=100,
        )
        raw_after = _raw(
            provider,
            f'SELECT id FROM "{_SCHEMA}".session_ledger__event '
            "WHERE session_id = %s AND sequence > %s AND is_deleted = 0 "
            "ORDER BY sequence ASC, id ASC LIMIT 100",
            (session_id, boundary_seq),
        )
        _check(
            [str(r["id"]) for r in migrated_after] == [str(r[0]) for r in raw_after],  # type: ignore[call-overload]
            "get_session_timeline(after_sequence) gt-cursor matches raw sequence>%s "
            "(strict; excludes the boundary sequence)",
        )
    # content_json shape discriminator: an event carrying a non-null content_json
    # must surface with content_json present + the SAME type the raw read yields
    # (proves no str<->dict flip across the _fetch_all → query_ordered seam).
    cj = _raw(
        provider,
        "SELECT session_id, sequence, id, content_json "
        f'FROM "{_SCHEMA}".session_ledger__event '
        "WHERE content_json IS NOT NULL AND is_deleted = 0 LIMIT 1",
    )
    if not cj:
        _check(True, "get_session_timeline content_json: no event carries one (skip)")
        return
    cj_session = str(cj[0][0])
    cj_seq = int(cj[0][1])
    cj_id = str(cj[0][2])
    cj_raw_value = cj[0][3]
    page = repo.get_session_timeline(
        session_id=cj_session, after_sequence=cj_seq - 1, limit=100,
    )
    match = next((r for r in page if str(r["id"]) == cj_id), None)  # type: ignore[call-overload]
    _check(match is not None, "get_session_timeline surfaces the content_json-bearing event")
    if match is not None:
        cj_migrated_value = match["content_json"]
        _check(
            cj_migrated_value is not None,
            "migrated event carries a non-null content_json field",
        )
        _check(
            type(cj_migrated_value) is type(cj_raw_value)
            and cj_migrated_value == cj_raw_value,
            f"content_json type+value matches the raw read — no str<->dict flip "
            f"(both {type(cj_raw_value).__name__})",
        )


def main() -> int:
    if os.environ.get("LEDGER_READ_LIVE_SMOKE") != "1":
        print("=== read_migration_slice6b_live_smoke ===")
        print(
            "  SKIP  set LEDGER_READ_LIVE_SMOKE=1 to run; "
            "needs the live solet DB "
            "(read-only, non-destructive).",
        )
        return 0
    print("=== read_migration_slice6b_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    adapter = _LiveStateAdapter(provider)
    repo = SessionLedgerRepository(state_service=adapter)  # type: ignore[arg-type]
    test_list_tool_calls_range_order_cap(repo, provider)
    test_get_session_timeline_cursor_content_json_cap(repo, provider)
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
