"""Scheduler management for APScheduler lifecycle and job operations.

This module encapsulates all APScheduler-related functionality including:
- Scheduler initialization and configuration
- Event listener registration and handling
- Job management (add, remove, get)
- Scheduler lifecycle (start, stop)
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from typing import Any

from apscheduler.events import (  # type: ignore[import-not-found]
    JobEvent,
    JobExecutionEvent,
)
from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-not-found]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-not-found]
from apscheduler.triggers.date import DateTrigger  # type: ignore[import-not-found]

from ..validation import validate_persisted_cron_action_def

RELOAD_SAFE = True


class SchedulerManager:
    """Manages APScheduler instance and job lifecycle.

    Handles:
    - BackgroundScheduler creation and configuration
    - Event listener registration for job monitoring
    - Job addition and removal
    - Scheduler start/stop operations
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """Initialize the scheduler manager.

        Args:
            logger: Optional logger for operation tracking
        """
        self.logger = logger
        self.scheduler: BackgroundScheduler | None = None

    def initialize(self) -> None:
        """Initialize and configure APScheduler instance."""
        if not self.scheduler:
            self.scheduler = BackgroundScheduler(timezone=datetime.UTC)
            if self.logger:
                self.logger.debug("BackgroundScheduler created")

    def register_listeners(self) -> None:
        """Register all APScheduler event listeners."""
        if not self.scheduler:
            return

        from apscheduler import events  # type: ignore[import-not-found]

        self.scheduler.add_listener(self._on_job_executed, events.EVENT_JOB_EXECUTED)
        self.scheduler.add_listener(self._on_job_error, events.EVENT_JOB_ERROR)
        self.scheduler.add_listener(self._on_job_missed, events.EVENT_JOB_MISSED)
        self.scheduler.add_listener(self._on_job_added, events.EVENT_JOB_ADDED)
        self.scheduler.add_listener(self._on_job_removed, events.EVENT_JOB_REMOVED)

    def start(self) -> None:
        """Start the scheduler."""
        if self.scheduler:
            self.scheduler.start()
            if self.logger:
                self.logger.debug("BackgroundScheduler started successfully")
                self.logger.debug(
                    f"SCHEDULER-STATE: scheduler.running={self.scheduler.running}, "
                    f"scheduler.state={self.scheduler.state}"
                )

    def stop(self, wait: bool = True) -> None:
        """Stop the scheduler gracefully.

        Args:
            wait: Whether to wait for running jobs to complete
        """
        if self.scheduler:
            self.scheduler.shutdown(wait=wait)
            if self.logger:
                self.logger.debug("BackgroundScheduler stopped successfully")
            self.scheduler = None

    def pause(self) -> None:
        """Pause job dispatch via APScheduler's native pause primitive.

        Per L3 blue-green Slice D: when the plugin becomes the inactive color,
        pause the scheduler so no new jobs fire. Re-entering an already-paused
        state is a no-op (APScheduler tolerates it). Returns immediately —
        the scheduler thread keeps running but won't dispatch jobs.
        """
        if self.scheduler and self.scheduler.running:
            self.scheduler.pause()
            if self.logger:
                self.logger.debug("BackgroundScheduler paused")

    def resume(self) -> None:
        """Resume job dispatch via APScheduler's native resume primitive.

        Symmetric to ``pause``. No-op when the scheduler isn't initialized
        (the lifecycle ``start`` path hasn't run yet) or is already running.
        """
        if self.scheduler and self.scheduler.state == 2:
            # state 2 == STATE_PAUSED in APScheduler's int-enum constants.
            self.scheduler.resume()
            if self.logger:
                self.logger.debug("BackgroundScheduler resumed")

    def add_cron_job(
        self,
        func: Callable[[], None],
        cron_expression: str,
        job_id: str,
    ) -> None:
        """Add a cron-based recurring job to the scheduler.

        Args:
            func: Callable to execute on schedule
            cron_expression: Cron expression defining schedule
            job_id: Unique identifier for the job
        """
        if not self.scheduler:
            return

        # `CronTrigger.from_crontab`'s docstring claims an unset `timezone`
        # "defaults to scheduler timezone" — that is false at construction
        # time: `CronTrigger.__init__` resolves `timezone=None` to
        # `get_localzone()` (the HOST's local tz) immediately, never
        # consulting this `BackgroundScheduler`'s own `timezone=UTC`
        # config. Passing UTC explicitly here is the fix, not a style
        # preference — measured 2026-08-16, see
        # plugins/default_scheduling_plugin/tests/cron_utc_timezone_smoke.py.
        self.scheduler.add_job(
            func,
            CronTrigger.from_crontab(cron_expression, timezone=datetime.UTC),
            id=job_id,
        )

    def add_one_time_job(
        self,
        func: Callable[[], None],
        run_at: datetime.datetime,
        job_id: str,
    ) -> None:
        """Add a one-time job to the scheduler.

        Args:
            func: Callable to execute at specified time
            run_at: When to execute the job
            job_id: Unique identifier for the job
        """
        if not self.scheduler:
            return

        self.scheduler.add_job(
            func,
            DateTrigger(run_date=run_at),
            id=job_id,
        )

    def remove_job(self, job_id: str) -> None:
        """Remove a job from the scheduler.

        Args:
            job_id: ID of the job to remove
        """
        if self.scheduler:
            try:
                self.scheduler.remove_job(job_id)
                if self.logger:
                    self.logger.debug(f"Removed job {job_id} from scheduler")
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Job {job_id} not found in scheduler: {e}")

    def get_job(self, job_id: str) -> Any | None:
        """Get a job from the scheduler.

        Args:
            job_id: ID of the job to retrieve

        Returns:
            Job instance if found, None otherwise
        """
        if self.scheduler:
            return self.scheduler.get_job(job_id)
        return None

    def get_jobs(self) -> list[Any]:
        """Get all jobs from the scheduler.

        Returns:
            List of all job instances
        """
        if self.scheduler:
            return self.scheduler.get_jobs()  # type: ignore[no-any-return]
        return []

    def restore_schedules(
        self,
        schedules: dict[str, dict[str, Any]],
        execution_callback: Callable[[str, dict[str, Any]], None],
        scheduled_status: str = "scheduled",
    ) -> tuple[int, int]:
        """Restore persisted schedules to the scheduler.

        Args:
            schedules: Dictionary mapping schedule IDs to schedule data
            execution_callback: Callback to execute when job triggers (receives schedule_id, data)
            scheduled_status: Status value indicating schedule should be restored (default: "scheduled")

        Returns:
            Tuple of (loaded_count, skipped_count)
        """
        loaded_count = 0
        skipped_count = 0

        if self.logger:
            self.logger.debug(f"Loading persisted schedules: {len(schedules)} found")

        for schedule_id, data in schedules.items():
            if data.get("status") != scheduled_status:
                skipped_count += 1
                continue

            result = self._restore_single_schedule(schedule_id, data, execution_callback)
            if result:
                loaded_count += 1
            else:
                skipped_count += 1

        if self.logger:
            self.logger.debug(f"Loaded {loaded_count} schedules, skipped {skipped_count}")

        return loaded_count, skipped_count

    def _restore_single_schedule(
        self,
        schedule_id: str,
        data: dict[str, Any],
        execution_callback: Callable[[str, dict[str, Any]], None],
    ) -> bool:
        """Restore a single schedule. Returns True if loaded, False if skipped."""
        try:
            if data["type"] == "recurring":
                return self._restore_recurring_schedule(schedule_id, data, execution_callback)
            elif data["type"] == "one_time":
                return self._restore_one_time_schedule(schedule_id, data, execution_callback)
            return False
        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to restore schedule {schedule_id}: {e}", exc_info=True)
            return False

    def _restore_recurring_schedule(
        self,
        schedule_id: str,
        data: dict[str, Any],
        execution_callback: Callable[[str, dict[str, Any]], None],
    ) -> bool:
        """Restore a recurring schedule."""
        if not self._cron_action_contract_passes(schedule_id, data):
            return False

        def make_callback(sid: str, d: dict[str, Any]) -> Callable[[], None]:
            return lambda: execution_callback(sid, d)

        self.add_cron_job(
            make_callback(schedule_id, data),
            data["cron_expression"],
            schedule_id,
        )
        return True

    def _restore_one_time_schedule(
        self,
        schedule_id: str,
        data: dict[str, Any],
        execution_callback: Callable[[str, dict[str, Any]], None],
    ) -> bool:
        """Restore a one-time schedule if not expired."""
        run_at = datetime.datetime.fromisoformat(data["run_at"])
        if run_at <= datetime.datetime.now(datetime.UTC):
            return False

        if not self._cron_action_contract_passes(schedule_id, data):
            return False

        def make_callback(sid: str, d: dict[str, Any]) -> Callable[[], None]:
            return lambda: execution_callback(sid, d)

        self.add_one_time_job(
            make_callback(schedule_id, data),
            run_at,
            schedule_id,
        )
        return True

    def _cron_action_contract_passes(
        self,
        schedule_id: str,
        data: dict[str, Any],
    ) -> bool:
        """Validate every persisted action_def in `data` against the cron contract.

        Symmetric coverage for the recurring + one-time restoration paths: a
        persisted schedule whose action_def declares a session-context-requiring
        `result_processor_kind` would re-bind to APScheduler and fail at fire
        time with `Empty source_namespace in flow trigger_data`. Reject at
        restoration; log the schedule_id so operators can `clear_scheduled_action`.
        """
        actions = data.get("actions", []) or []
        if not isinstance(actions, list):
            return True
        for action in actions:
            if not isinstance(action, dict):
                continue
            try:
                validate_persisted_cron_action_def(action)
            except ValueError as exc:
                if self.logger:
                    self.logger.warning(
                        "Skipping persisted schedule %s during restoration: %s",
                        schedule_id,
                        exc,
                    )
                return False
        return True

    def cleanup_completed_jobs(
        self,
        schedules: dict[str, dict[str, Any]],
        completed_status: str = "completed",
    ) -> int:
        """Remove completed jobs from the scheduler.

        Args:
            schedules: Dictionary mapping schedule IDs to schedule data
            completed_status: Status value indicating completed jobs (default: "completed")

        Returns:
            Number of jobs removed
        """
        removed_count = 0
        job_ids = {job.id for job in self.get_jobs()}

        for schedule_id, data in list(schedules.items()):
            if data.get("status") == completed_status and schedule_id in job_ids:
                self.remove_job(schedule_id)
                removed_count += 1
                if self.logger:
                    self.logger.debug(f"Removed completed job: {schedule_id}")

        return removed_count

    # Event listener methods
    def _on_job_executed(self, event: JobExecutionEvent) -> None:
        """Handle successful job execution."""
        if self.logger:
            self.logger.debug(f"SCHEDULER-EVENT-EXECUTED: Job {event.job_id} executed successfully")

    def _on_job_error(self, event: JobExecutionEvent) -> None:
        """Handle job execution errors."""
        if self.logger:
            self.logger.error(
                f"SCHEDULER-EVENT-ERROR: Job {event.job_id} raised exception: {event.exception}",
                exc_info=event.exception,
            )

    def _on_job_missed(self, event: JobExecutionEvent) -> None:
        """Handle missed job executions."""
        if self.logger:
            self.logger.error(f"SCHEDULER-EVENT-MISSED: Job {event.job_id} execution was missed")

    def _on_job_added(self, event: JobEvent) -> None:
        """Handle job additions to scheduler."""
        pass

    def _on_job_removed(self, event: JobEvent) -> None:
        """Handle job removals from scheduler."""
        pass
