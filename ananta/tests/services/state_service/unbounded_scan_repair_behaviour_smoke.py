#!/usr/bin/env python3
"""Smoke: the repaired whole-table scans DO THEIR JOB, not merely pass a limit.

Three sites in the 2026-08-15 SCAN_UNBOUNDED class each carried TWO defects: an
unbounded read, and a result navigated through a `data["result"]` key that
`read_state` has never returned. Fixing only the bound would have left every one
of them just as broken, with the symptom removed from view — so this smoke
asserts the OUTCOME at each site and treats the bound as incidental.

That distinction is the reason this file exists. A smoke that asserted "a limit
was passed" would go green against all three defects simultaneously.

The envelope seam being pinned, measured live on 2026-08-15:

    read_state   -> data = {records, count, namespace, table}   NO "result" key
    count        -> data = {namespace, result: {value: N}}
    update_state -> data = {namespace, result: {updated: N}}

Neither shape is guessable from the other, which is how three separate consumers
came to read a key that was never there.

What each section proves:

  A  session_manager.cleanup_expired_sessions issues ONE set-based UPDATE with
     the expiry predicate pushed into SQL — no table read, no per-row round
     trip — and returns the real count. Previously it read all of core.sessions
     (21,723 rows, over the cap), and its extractor returned [] regardless, so it
     reported 0 having expired nothing.
  B  discovery_service.get_service_health reports the ACTUAL row count via the
     `count` verb. Previously it fetched the whole usage_stats table and then
     read a missing key, so total_usage_records was structurally always 0.
  C  ProcessRegistryUtil resolves the REAL provider/function values for a row.
     Previously the filter sat at the wrong nesting level (silently dropped, full
     756-row scan) and the values were read off the envelope instead of the row —
     so it returned {"provider_type": "plugin", "provider": "", "function_name":
     ""} for every input, a well-formed answer made entirely of default
     arguments. Asserting "not None" would pass against that; these assert the
     values.

PURE UNIT: real production classes against spy state services that record every
call. No DB, no platform, no live solet.

Run:
    .venv/bin/python3 ananta/tests/services/state_service/unbounded_scan_repair_behaviour_smoke.py

Project policy: no pytest. Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.orchestration.managers.session_manager import SessionManager  # noqa: E402
from ananta.core.process_registry.util import ProcessRegistryUtil  # noqa: E402

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


def _completed(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None}


class _SpyState:
    """Records every state call and serves the REAL envelope shapes."""

    def __init__(
        self,
        *,
        records: list[dict[str, Any]] | None = None,
        count_value: int = 0,
        updated: int = 0,
    ) -> None:
        self.records = records if records is not None else []
        self.count_value = count_value
        self.updated = updated
        self.reads: list[dict[str, Any]] = []
        self.counts: list[dict[str, Any]] = []
        self.updates: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        _ = namespace
        self.reads.append(query)
        # The real read_state envelope: no "result" key.
        return _completed(
            {
                "records": self.records,
                "count": len(self.records),
                "namespace": namespace,
                "table": query.get("table"),
            }
        )

    def count(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        self.counts.append(data)
        return _completed({"namespace": namespace, "result": {"value": self.count_value}})

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        _ = namespace
        self.updates.append((query, updates))
        return _completed({"namespace": namespace, "result": {"updated": self.updated}})


def _section_a() -> None:
    print("A. cleanup_expired_sessions — expires sessions in ONE set-based UPDATE")
    spy = _SpyState(updated=21_702)
    manager = SessionManager(state_service=spy)  # type: ignore[arg-type]

    expired = manager.cleanup_expired_sessions()

    _check(expired == 21_702, "returns the REAL expired count (was hardcoded-0 in effect)")
    _check(spy.reads == [], "issues NO read at all — the table is never fetched")
    _check(len(spy.updates) == 1, "issues exactly ONE update (was one per expired row)")

    query, updates = spy.updates[0]
    filters = query.get("filters", {})
    _check(query.get("table") == "sessions", "targets core.sessions")
    _check(
        filters.get("expires_at") == {"op": "lt", "value": filters.get("expires_at", {}).get("value")}
        and filters.get("expires_at", {}).get("op") == "lt",
        "pushes the expiry predicate into SQL as {'op': 'lt', ...}",
    )
    _check(
        isinstance(filters.get("expires_at", {}).get("value"), str),
        "passes a concrete timestamp value, not a placeholder",
    )
    _check(
        filters.get("status") == "active",
        "restricts to ACTIVE rows so the sweep is idempotent",
    )
    _check(updates == {"status": "expired"}, "sets status=expired")


def _section_b() -> None:
    print("\nB. get_service_health — reports the ACTUAL usage_stats row count")
    from ananta.services.discovery_service.service import DiscoveryService

    spy = _SpyState(count_value=4_242)
    service = DiscoveryService.__new__(DiscoveryService)
    service.state_service = spy  # type: ignore[attr-defined]
    service.namespace = "core"  # type: ignore[attr-defined]
    service.processes = {}  # type: ignore[attr-defined]
    service.vector_service = None  # type: ignore[attr-defined]

    health = service.get_service_health()

    _check(
        health["total_usage_records"] == 4_242,
        "reports the real count (was structurally 0 for every table size)",
    )
    _check(spy.reads == [], "ships NO rows — the whole-table fetch is gone")
    _check(len(spy.counts) == 1, "asks the database to COUNT instead")
    _check(spy.counts[0].get("table") == "usage_stats", "counts the right table")

    # The load-bearing half: a DIFFERENT count must produce a DIFFERENT answer.
    # A site that ignores its result passes any single-value assertion.
    spy2 = _SpyState(count_value=7)
    service2 = DiscoveryService.__new__(DiscoveryService)
    service2.state_service = spy2  # type: ignore[attr-defined]
    service2.namespace = "core"  # type: ignore[attr-defined]
    service2.processes = {}  # type: ignore[attr-defined]
    service2.vector_service = None  # type: ignore[attr-defined]
    _check(
        service2.get_service_health()["total_usage_records"] == 7,
        "the reported number TRACKS the database (not a constant that happens to match)",
    )


def _section_c() -> None:
    print("\nC. ProcessRegistryUtil — resolves the REAL row, not fabricated defaults")
    row = {
        "external_id": "proc-abc123",
        "process_key": "service_interface::state_service::count",
        "provider_type": "service_interface",
        "provider": "state_service",
        "function_name": "count",
    }
    spy = _SpyState(records=[row])
    util = ProcessRegistryUtil(spy)  # type: ignore[arg-type]

    info = util.get_process_info("proc-abc123")

    # These are the assertions that "not None" would have missed: every one of
    # these values equals its DEFAULT in the broken version.
    _check(info.get("provider_type") == "service_interface", "provider_type is the ROW's value")
    _check(info.get("provider") == "state_service", "provider is the ROW's value")
    _check(info.get("function_name") == "count", "function_name is the ROW's value")
    _check(
        info != {"provider_type": "plugin", "provider": "", "function_name": ""},
        "does NOT return the all-defaults dict the broken version produced",
    )

    _check(len(spy.reads) == 1, "one read")
    query = spy.reads[0]
    _check(
        query.get("filters", {}).get("external_id") == "proc-abc123",
        "the predicate is inside 'filters' where the provider reads it",
    )
    _check(
        "external_id" not in {k for k in query if k != "filters"},
        "the predicate is NOT left at the top level, where it is silently ignored",
    )
    _check(query.get("limit") == 1, "bounded to the one row the unique constraint allows")

    print("\nC2. query_by_process_key / lookup_external_id — same envelope, same fix")
    spy2 = _SpyState(records=[row])
    util2 = ProcessRegistryUtil(spy2)  # type: ignore[arg-type]
    found = util2.query_by_process_key("service_interface::state_service::count")
    _check(found is not None, "resolves a row that exists (returned None for every key before)")
    _check(
        found is not None and found.get("function_name") == "count",
        "hands back the ROW itself",
    )

    spy3 = _SpyState(records=[row])
    util3 = ProcessRegistryUtil(spy3)  # type: ignore[arg-type]
    _check(
        util3.lookup_external_id_by_process_key("service_interface::state_service::count")
        == "proc-abc123",
        "lookup_external_id_by_process_key resolves the id (was always None)",
    )

    print("\nC3. a genuinely absent row still reports absent")
    spy4 = _SpyState(records=[])
    util4 = ProcessRegistryUtil(spy4)  # type: ignore[arg-type]
    _check(util4.get_process_info("nope") == {}, "no row -> {} (not a defaults dict)")
    _check(util4.query_by_process_key("nope") is None, "no row -> None")


def main() -> int:
    _section_a()
    _section_b()
    _section_c()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
