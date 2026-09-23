#!/usr/bin/env python3
"""Smoke — `default_scheduling_plugin.list_schedules` enumerate-all introspection verb.

Background: the scheduler had `get_schedules_by_tag` (tag-scoped) and the clear
verbs, but NO enumerate-all verb — so a schedule whose tag/id you do not already
know was unfindable (operator could not locate a stale 'OVERNIGHT sweep' cron to
clear it). `list_schedules` is the tag-agnostic companion: it returns ALL
schedules (optionally narrowed by status), read-only.

This smoke proves the NEW code's responsibility — the projection + optional
status-filter + count over `_load_schedules()`'s output, and the dual-form
(plugin:: + service_interface::) registration. `_load_schedules` is overridden
with a realistic fixture (the dict shape `ScheduleData.model_dump()` produces),
the established fake-source/real-logic unit boundary — `_load_schedules` itself
is exercised elsewhere. The LIVE end-to-end (real schedules table + the stale
cron surfacing) is verified post-restart via `process_call` per the
create-process skill Step 5; this smoke is the slice-ready, restart-free proof.

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src"))

from default_scheduling_plugin.plugin import SchedulingPlugin  # noqa: E402

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


# A realistic `_load_schedules()` output: three schedules, one mimicking the
# stale 'OVERNIGHT sweep' cron the operator needs to find by enumeration (it
# carries a tag/id the caller does not already know).
_FIXTURE: dict[str, Any] = {
    "next_id": 1003,
    "scheduled_actions": {
        "1000": {
            "type": "recurring",
            "status": "scheduled",
            "label": "Coordinator-Dusk OVERNIGHT autonomous sweep",
            "cron_expression": "0 * * * *",
            "run_at": None,
            "tags": ["coordinator:dusk:overnight_sweep"],
            "session_id": None,
            "flow_id": "flow-dusk-sweep",
        },
        "1001": {
            "type": "recurring",
            "status": "scheduled",
            "label": "Ledger periodic poll",
            "cron_expression": "*/5 * * * *",
            "run_at": None,
            "tags": ["ledger:periodic_poll"],
            "session_id": None,
            "flow_id": "flow-ledger-poll",
        },
        "1002": {
            "type": "one_time",
            "status": "completed",
            "label": "one-shot check-in",
            "cron_expression": None,
            "run_at": "2026-06-21T00:00:00+00:00",
            "tags": ["followup:job-x"],
            "session_id": "session-abc",
            "flow_id": "flow-x",
        },
    },
}

_PROJECTED_KEYS = {
    "schedule_id", "type", "status", "label",
    "cron_expression", "run_at", "tags", "session_id", "flow_id",
}


def _build_plugin() -> SchedulingPlugin:
    plugin = SchedulingPlugin()
    plugin.logger = logging.getLogger("list_schedules_smoke")
    # Target the projection/filter logic, not the loader: _load_schedules is
    # existing, separately-exercised code. (fake-source / real-logic boundary)
    plugin._load_schedules = lambda: _FIXTURE  # type: ignore[method-assign]
    return plugin


def main() -> int:
    print("list_schedules enumerate-all introspection smoke")
    print("================================================")

    plugin = _build_plugin()

    # (1) Unfiltered: returns EVERY schedule with the canonical 9-field shape.
    res_all = plugin.list_schedules({}, {})
    _check(res_all.get("action_status") == "completed", "unfiltered call completes")
    data_all = res_all.get("data", {})
    _check(data_all.get("count") == 3, f"count == 3 (got {data_all.get('count')})")
    _check(len(data_all.get("schedules", [])) == 3, "returns all 3 schedules")
    _check(data_all.get("status_filter") is None, "status_filter is None when unfiltered")
    entry = next(
        (e for e in data_all.get("schedules", []) if e.get("schedule_id") == "1000"),
        None,
    )
    _check(entry is not None, "the stale OVERNIGHT-sweep schedule is enumerated")
    if entry is not None:
        _check(set(entry.keys()) == _PROJECTED_KEYS, f"entry has exactly the 9 projected keys (got {sorted(entry.keys())})")
        _check(
            entry.get("tags") == ["coordinator:dusk:overnight_sweep"],
            "stale schedule surfaces its real tag (the unblock — caller now has the tag/id to clear)",
        )
        _check(entry.get("label") == "Coordinator-Dusk OVERNIGHT autonomous sweep", "label projected")
        _check(entry.get("cron_expression") == "0 * * * *", "cron_expression projected")

    # (2) Status filter narrows to matching schedules and echoes the filter.
    res_sched = plugin.list_schedules({"status": "scheduled"}, {})
    data_sched = res_sched.get("data", {})
    _check(data_sched.get("count") == 2, f"status='scheduled' returns 2 (got {data_sched.get('count')})")
    _check(data_sched.get("status_filter") == "scheduled", "status_filter echoed")
    _check(
        all(e.get("status") == "scheduled" for e in data_sched.get("schedules", [])),
        "every returned entry matches the status filter",
    )

    res_done = plugin.list_schedules({"status": "completed"}, {})
    _check(res_done.get("data", {}).get("count") == 1, "status='completed' returns 1")

    # (3) Dual-form registration: the service_interface:: form rides an
    # EdgeProcessDefinition (decorated<->declared parity — customizations are
    # optional since the 2026-07-15 frontier-first relax).
    edge_defs = plugin.get_edge_process_definitions()
    _check("list_schedules" in edge_defs, "list_schedules registered as an EdgeProcessDefinition (service_interface:: form)")

    print("\n------------------------------------------------")
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    if _failed:
        print("\nFailures:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
