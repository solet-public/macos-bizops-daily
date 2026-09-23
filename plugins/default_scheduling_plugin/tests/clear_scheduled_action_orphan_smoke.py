#!/usr/bin/env python3
"""Smoke — `default_scheduling_plugin.clear_scheduled_action` kills orphaned jobs.

Background: a recurring schedule whose DB/metadata row was hard-deleted while its
in-memory APScheduler job stayed alive was UNKILLABLE by any verb —
`clear_scheduled_action` gated `remove_job` behind `schedule_id in
_load_schedules()`, so once the row was gone the live job kept firing until a
solet restart (draining tokens by waking dead sessions; the operator's "scheduled alerts
we cannot turn off"). The fix makes the verb ALWAYS attempt `remove_job`
(exception-safe) and report `cancelled` if EITHER the DB row was deleted OR a live
job was removed — so an orphan is now killable by id without a restart.

Proves the verb's branch logic against a fake scheduler-manager + fake
`_load_schedules` (the established fake-source / real-logic boundary; the LIVE
end-to-end is the post-restart `process_call` path).

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


class _FakeScheduler:
    """Records remove_job calls; get_job reports a live job for known ids."""

    def __init__(self, live_job_ids: set[str]) -> None:
        self._live = set(live_job_ids)
        self.removed: list[str] = []

    def get_job(self, job_id: str) -> object | None:
        return object() if job_id in self._live else None

    def remove_job(self, job_id: str) -> None:
        self.removed.append(job_id)
        self._live.discard(job_id)


# DB-backed rows visible to _load_schedules(). The orphan ("orphan-1") is
# deliberately ABSENT here but LIVE in the scheduler — the unkillable case.
_DB_ROWS: dict[str, Any] = {
    "scheduled_actions": {
        "1000": {
            "type": "recurring", "status": "scheduled", "label": "normal cron",
            "cron_expression": "*/5 * * * *", "run_at": None,
            "tags": ["t"], "session_id": None, "flow_id": "flow-1000",
        },
    },
}


def _build_plugin(live_job_ids: set[str]) -> tuple[SchedulingPlugin, _FakeScheduler]:
    plugin = SchedulingPlugin()
    plugin.logger = logging.getLogger("clear_orphan_smoke")
    sched = _FakeScheduler(live_job_ids)
    plugin._scheduler_manager = sched  # type: ignore[assignment]
    plugin._load_schedules = lambda: _DB_ROWS  # type: ignore[method-assign]
    # DB delete succeeds only for rows present in _DB_ROWS (the real
    # _delete_schedule needs a repository/_memory_schedules; stub the boundary).
    plugin._delete_schedule = (  # type: ignore[method-assign]
        lambda sid: sid in _DB_ROWS["scheduled_actions"]
    )
    return plugin, sched


def main() -> int:
    print("clear_scheduled_action orphan-recovery smoke")
    print("============================================")

    # (1) ORPHAN: row gone, job live → must remove the job and report cancelled.
    plugin, sched = _build_plugin(live_job_ids={"orphan-1"})
    res = plugin.clear_scheduled_action({"schedule_id": "orphan-1"}, {})
    _check(res.get("action_status") == "completed", "orphan clear completes")
    _check(
        res.get("data", {}).get("cancelled") is True,
        "orphan reported cancelled=True (the fix: was False when row-gated)",
    )
    _check("orphan-1" in sched.removed, "orphan's live in-memory job was removed")

    # (2) NORMAL: row present + job live → unchanged behaviour, still cancels.
    plugin, sched = _build_plugin(live_job_ids={"1000"})
    res = plugin.clear_scheduled_action({"schedule_id": "1000"}, {})
    _check(res.get("data", {}).get("cancelled") is True, "normal schedule still cancels")
    _check("1000" in sched.removed, "normal schedule's job removed")

    # (3) TRULY GONE: no row, no live job → cancelled=False, "not found".
    plugin, sched = _build_plugin(live_job_ids=set())
    res = plugin.clear_scheduled_action({"schedule_id": "ghost"}, {})
    _check(
        res.get("data", {}).get("cancelled") is False,
        "absent schedule reports cancelled=False",
    )
    _check(
        "not found" in res.get("data", {}).get("message", ""),
        "absent schedule message says 'not found'",
    )

    print("\n--------------------------------------------")
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
