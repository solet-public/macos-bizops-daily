#!/usr/bin/env python3
"""Red-first smoke — cron triggers must evaluate in UTC, not host local time.

Background (2026-08-16 lane-utc-scheduler measurement): `SchedulerManager`
builds its `BackgroundScheduler` with `timezone=datetime.UTC`
(scheduler_manager.py:52), but `add_cron_job` calls
`CronTrigger.from_crontab(cron_expression)` with no `timezone` kwarg
(scheduler_manager.py:135). APScheduler's `CronTrigger.__init__` resolves an
unset `timezone` to `get_localzone()` **at trigger-construction time** —
independent of the scheduler's own configured timezone, despite
`from_crontab`'s docstring claiming it "defaults to scheduler timezone".
Reproduced directly against the installed apscheduler: on this host,
`CronTrigger.from_crontab('30 7 * * *').timezone` resolves to
`America/Vancouver`, a −25200s offset from UTC — the same magnitude already
measured independently in the `peer_list.updated_at` UTC-naive bug.

`default_scheduling_plugin`'s own KB reference doc states "All cron
expressions use UTC timezone" — the doc is part of the defect. This smoke
pins a fixed, deliberately non-UTC `get_localzone()` return value (NOT the
host's real timezone) so the assertion is portable: it must fail the same
way on a UTC-local CI host as it does here, or it silently stops covering
the defect the moment it runs somewhere whose local tz happens to be UTC.

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import datetime
import sys
import zoneinfo
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src"))

import apscheduler.triggers.cron as apscheduler_cron_module  # noqa: E402
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


# Deliberately NOT the host's real local zone and NOT UTC — a fixed marker
# zone so the test is meaningful regardless of what machine runs it.
_FIXED_NON_UTC_ZONE = zoneinfo.ZoneInfo("America/Vancouver")


def _pinned_get_localzone() -> zoneinfo.ZoneInfo:
    return _FIXED_NON_UTC_ZONE


def _utc_offset_seconds(tz: datetime.tzinfo) -> float:
    offset = tz.utcoffset(datetime.datetime(2026, 8, 16, 12, 0, 0))
    assert offset is not None
    return offset.total_seconds()


# ----- Case 1: live add_cron_job must register a UTC trigger -----------------
print("Case 1: SchedulerManager.add_cron_job registers a UTC-evaluated trigger")

original_get_localzone = apscheduler_cron_module.get_localzone
apscheduler_cron_module.get_localzone = _pinned_get_localzone
try:
    manager = SchedulerManager()
    manager.initialize()
    manager.add_cron_job(lambda: None, "30 7 * * *", "smoke-job-live")

    job = manager.get_job("smoke-job-live")
    _check(job is not None, "job was registered")

    if job is not None:
        trigger_tz = job.trigger.timezone
        _check(
            _utc_offset_seconds(trigger_tz) == 0.0,
            f"live add_cron_job trigger timezone is UTC (got {trigger_tz}, "
            f"offset={_utc_offset_seconds(trigger_tz)}s)",
        )
finally:
    apscheduler_cron_module.get_localzone = original_get_localzone


# ----- Case 2: the restore path (same add_cron_job call site) must also be UTC
print("\nCase 2: restore_schedules' recurring-schedule path is also UTC-evaluated")

apscheduler_cron_module.get_localzone = _pinned_get_localzone
try:
    manager2 = SchedulerManager()
    manager2.initialize()

    restored_calls: list[tuple[str, dict[str, object]]] = []

    def _execution_callback(schedule_id: str, data: dict[str, object]) -> None:
        restored_calls.append((schedule_id, data))

    loaded, skipped = manager2.restore_schedules(
        {
            "smoke-restore-job": {
                "type": "recurring",
                "status": "scheduled",
                "cron_expression": "30 7 * * *",
                "actions": [],
            }
        },
        _execution_callback,
    )
    _check(loaded == 1 and skipped == 0, f"restore loaded the schedule (loaded={loaded}, skipped={skipped})")

    job2 = manager2.get_job("smoke-restore-job")
    _check(job2 is not None, "restored job was registered")
    if job2 is not None:
        trigger_tz2 = job2.trigger.timezone
        _check(
            _utc_offset_seconds(trigger_tz2) == 0.0,
            f"restored trigger timezone is UTC (got {trigger_tz2}, "
            f"offset={_utc_offset_seconds(trigger_tz2)}s)",
        )
finally:
    apscheduler_cron_module.get_localzone = original_get_localzone


# ----- Report ------------------------------------------------------------
print()
print(f"Passed: {_passed}")
print(f"Failed: {len(_failed)}")
if _failed:
    for label in _failed:
        print(f"  - {label}")
    sys.exit(1)
sys.exit(0)
