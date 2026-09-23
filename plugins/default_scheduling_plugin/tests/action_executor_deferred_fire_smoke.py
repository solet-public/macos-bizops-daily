#!/usr/bin/env python3
"""Red-first smoke — a fire landing before ActionFactory injection must defer,
never permanently kill the schedule.

Background (2026-08-16 lane-utc-scheduler measurement, defect A):
`profile/data/logs/2026-08-15_profile.log` shows `sch-2n0h8hh6jp9te` (daily
self-vet) and three other schedules firing at 07:30:00 and 09:20:00 during
two separate blue-green swap startup windows, before
`SchedulingPlugin.set_action_factory` had run in the candidate process.
`_execute_action` (plugin.py) hit `_action_executor is None` and called
`_mark_schedule_failed`, which persists `schedules.status=error`. The actual
permanent-death mechanism is NOT an ongoing firing loop — it is that status
value being read by `SchedulerManager.restore_schedules`
(scheduler_manager.py:222: `if data.get("status") != scheduled_status: skip`),
which drops the schedule on the very next process restart's restore pass.
`sch-2n0h8hh6jp9te` never fired again on 08-16 for exactly this reason.

Fix under test: on `_action_executor is None`, `_execute_action` must NOT
call `_mark_schedule_failed` — `schedules.status` stays untouched so the
schedule survives `restore_schedules` on the next deploy, and the universal
job tracker records `"pending"` (an allowed value per `core_schemas.py`'s
`asynchronous_jobs.status` CHECK constraint — `"deferred"` is not, and this
fix deliberately avoids a schema change) rather than `"failed"`.

Per the dispatch rider: this smoke pins the RESTORE assertion specifically —
a schedule that missed a fire in the gap must survive
`SchedulerManager.restore_schedules`, not just avoid a fire-time
`status=error` write in isolation. A regression that reintroduces
`_mark_schedule_failed` on this path must go red at the restore assertion
even if a shallower smoke would only check the fire-time write.

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src"))

from default_scheduling_plugin.plugin import SchedulingPlugin  # noqa: E402
from default_scheduling_plugin.scheduling.scheduler_manager import SchedulerManager  # noqa: E402

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


class _FakeRepository:
    """Records status writes; starts a schedule at status='scheduled'."""

    def __init__(self, schedule_id: str) -> None:
        self.status_by_id: dict[str, str] = {schedule_id: "scheduled"}
        self.error_message_by_id: dict[str, str | None] = {schedule_id: None}
        self.update_calls: list[tuple[str, str, str | None]] = []

    def update_schedule_status(
        self, schedule_id: str, status: str, error_message: str | None = None
    ) -> bool:
        self.update_calls.append((schedule_id, status, error_message))
        self.status_by_id[schedule_id] = status
        self.error_message_by_id[schedule_id] = error_message
        return True


class _FakeJobTracker:
    """Records universal job tracker updates."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def update_job(self, schedule_id: str, updates: dict[str, Any]) -> bool:
        self.updates.append((schedule_id, dict(updates)))
        return True


SCHEDULE_ID = "sch-smoke-defect-a"
SCHEDULE_DATA = {
    "id": SCHEDULE_ID,
    "label": "smoke deferred-fire target",
    "tags": [],
    "type": "recurring",
    "actions": [],
    "action_name": "",
    "action_parameters": {},
    "cron_expression": "*/5 * * * *",
    "status": "scheduled",
    "session_id": "sess-smoke",
    "flow_id": "flow-smoke",
    "error_message": None,
}

# ----- Case 1: fire-time write must not set status=error ---------------------
print("Case 1: executor-None fire does not persist schedules.status=error")

plugin = SchedulingPlugin()
plugin.logger = __import__("logging").getLogger("smoke-defect-a")
fake_repo = _FakeRepository(SCHEDULE_ID)
fake_tracker = _FakeJobTracker()
plugin._repository = fake_repo  # type: ignore[assignment]
plugin._job_tracker = fake_tracker  # type: ignore[assignment]

_check(plugin._action_executor is None, "fresh plugin has _action_executor=None (pre-injection state)")

plugin._execute_action(SCHEDULE_ID, dict(SCHEDULE_DATA))

_check(
    fake_repo.status_by_id[SCHEDULE_ID] == "scheduled",
    f"schedules.status stays 'scheduled' after the deferred fire (got {fake_repo.status_by_id[SCHEDULE_ID]!r})",
)
_check(
    not any(status == "error" for (_sid, status, _err) in fake_repo.update_calls),
    "no update_schedule_status call ever set status='error'",
)

tracker_statuses = [u["status"] for (_sid, u) in fake_tracker.updates if "status" in u]
_check(
    "pending" in tracker_statuses,
    f"universal job tracker recorded 'pending' (got statuses={tracker_statuses})",
)
_check(
    "failed" not in tracker_statuses,
    f"universal job tracker never recorded 'failed' for this deferred fire (got statuses={tracker_statuses})",
)

# ----- Case 2: the schedule must survive restore_schedules -------------------
print("\nCase 2: a schedule left status='scheduled' after a deferred fire SURVIVES restore")

manager = SchedulerManager()
manager.initialize()

restored: list[tuple[str, dict[str, Any]]] = []


def _execution_callback(schedule_id: str, data: dict[str, Any]) -> None:
    restored.append((schedule_id, data))


persisted_after_defer = dict(SCHEDULE_DATA)
persisted_after_defer["status"] = fake_repo.status_by_id[SCHEDULE_ID]

loaded, skipped = manager.restore_schedules(
    {SCHEDULE_ID: persisted_after_defer},
    _execution_callback,
)

_check(
    loaded == 1 and skipped == 0,
    f"the deferred-fire schedule is RESTORED, not skipped (loaded={loaded}, skipped={skipped})",
)
_check(
    manager.get_job(SCHEDULE_ID) is not None,
    "the deferred-fire schedule is re-armed in APScheduler after restore",
)

# ----- Report ------------------------------------------------------------
print()
print(f"Passed: {_passed}")
print(f"Failed: {len(_failed)}")
if _failed:
    for label in _failed:
        print(f"  - {label}")
    sys.exit(1)
sys.exit(0)
