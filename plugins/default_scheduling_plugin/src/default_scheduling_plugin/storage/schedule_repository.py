"""Schedule persistence repository for database operations.

This module encapsulates all state service interactions for schedule storage,
providing a clean interface with centralized error handling.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.services.state_service.bounded_read import assert_within_ceiling

from ..constants import SchedulerJobStatus
from ..models import ScheduleData
from ..utils.logging_utils import safe_log_error

# `schedules` is a REGISTRY of configured jobs: one row per schedule a caller
# deliberately created, removed when the schedule is deleted. It is bounded by
# how many recurring jobs a deployment configures — nothing appends to it per
# fire, per run, or per request (execution history is not kept here).
#
# If this trips, the assumption that broke is "one row per configured schedule",
# which most likely means schedules are being created and never cleaned up.
# Raising the number would hide that; the loud failure is the useful outcome.
# MEASURED 2026-08-15: default_scheduling_plugin.schedules holds 6 rows. 100
# deliberately matches the platform's default row bound rather than exceeding it,
# so this read needs no `unbounded` opt-in and stays correct when the default
# drops to 100 — a deployment with more than 100 distinct configured jobs is a
# different system than this one, and should say so out loud.
_SCHEDULES_TABLE_CEILING = 100
_SCHEDULES_TABLE_CEILING_REASON = (
    "schedules holds one row per configured recurring job, deliberately created "
    "and deleted, with no per-execution rows appended (measured: 6 rows)."
)

RELOAD_SAFE = True


class ScheduleRepository:
    """Repository for schedule persistence operations.

    Centralizes all database operations for schedules, providing:
    - Type-safe storage using Pydantic models
    - Consistent error handling
    - Logging of all operations
    - Clean separation from business logic
    """

    def __init__(
        self,
        state_service: StateServiceProtocol | None,
        namespace: str,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize the schedule repository.

        Args:
            state_service: State service for database operations (can be None)
            namespace: Plugin namespace for database queries
            logger: Optional logger for operation tracking
        """
        self.state_service = state_service
        self.namespace = namespace
        self.logger = logger

    def _deserialize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Parse JSON-string fields from a DB record before constructing ScheduleData.

        PostgreSQL stores list/dict fields as JSON text strings. Pydantic expects
        native Python types, so we must parse them before model construction.
        """
        result = dict(record)
        for field in ("tags", "actions", "action_parameters"):
            value = result.get(field)
            if isinstance(value, str) and value.strip():
                try:
                    result[field] = json.loads(value)
                except json.JSONDecodeError:
                    safe_log_error(
                        self.logger,
                        f"Invalid JSON in '{field}' field of schedule record — using empty default",
                    )
                    result[field] = [] if field != "action_parameters" else {}
        return result

    def save_schedule(self, schedule: ScheduleData) -> str | None:
        """Save a schedule to the database and return the generated ID.

        Args:
            schedule: Schedule data to persist

        Returns:
            Generated ID if successful, None otherwise
        """
        if not self.state_service:
            safe_log_error(self.logger, "Cannot save schedule - state service not available")
            return None

        try:
            result = self.state_service.write_state(
                namespace=self.namespace,
                data={
                    "table": "schedules",
                    "record": schedule.model_dump(),
                },
            )

            if result.get("action_status") == "completed":
                # Extract auto-generated ID from response
                data = result.get("data", {})
                if isinstance(data, dict):  # type: ignore[reportUnnecessaryIsInstance]
                    result_data = data.get("result", {})
                    if isinstance(result_data, dict):  # type: ignore[reportUnnecessaryIsInstance]
                        generated_id = result_data.get("generated_id")
                        if generated_id:
                            return str(generated_id)
                safe_log_error(self.logger, "No generated_id in response")
                return None
            else:
                safe_log_error(self.logger, f"Failed to save schedule: {result}")
                return None

        except Exception as e:
            safe_log_error(self.logger, f"Error saving schedule: {e}", exc_info=True)
            return None

    def load_schedule(self, schedule_id: str) -> ScheduleData | None:
        """Load a specific schedule from the database.

        Args:
            schedule_id: ID of the schedule to load

        Returns:
            Schedule data if found, None otherwise
        """
        if not self.state_service:
            safe_log_error(self.logger, "Cannot load schedule - state service not available")
            return None

        try:
            result = self.state_service.read_state(
                namespace=self.namespace,
                query={"table": "schedules", "filters": {"id": schedule_id}},
            )

            if result.get("action_status") == "completed":
                data = result.get("data", {})
                if isinstance(data, dict):  # type: ignore[reportUnnecessaryIsInstance]
                    records_value = data.get("records", [])
                    if isinstance(records_value, list) and records_value:  # type: ignore[reportUnnecessaryIsInstance]
                        first_record = records_value[0]
                        if isinstance(first_record, dict):
                            return ScheduleData(**self._deserialize_record(first_record))

                return None
            else:
                safe_log_error(self.logger, f"Failed to load schedule {schedule_id}: {result}")
                return None

        except Exception as e:
            safe_log_error(self.logger, f"Error loading schedule {schedule_id}: {e}", exc_info=True)
            return None

    def load_all_schedules(self) -> dict[str, ScheduleData]:
        """Load all schedules from the database.

        Returns:
            Dictionary mapping schedule ID to ScheduleData
        """
        if not self.state_service:
            safe_log_error(self.logger, "Cannot load schedules - state service not available")
            return {}

        try:
            result = self.state_service.read_state(
                namespace=self.namespace,
                query={
                    "table": "schedules",
                    "filters": {},
                    "limit": _SCHEDULES_TABLE_CEILING,
                },
            )

            if result.get("action_status") == "completed":
                data = result.get("data", {})
                if isinstance(data, dict):  # type: ignore[reportUnnecessaryIsInstance]
                    records_value = data.get("records", [])
                    schedules = {}
                    if isinstance(records_value, list):  # type: ignore[reportUnnecessaryIsInstance]
                        # A partial read here means schedules that exist in the
                        # database silently never fire, with nothing anywhere
                        # reporting a missing one — the failure would show up as
                        # "that job stopped running" weeks later.
                        records_value = assert_within_ceiling(
                            records_value,
                            table="schedules",
                            ceiling=_SCHEDULES_TABLE_CEILING,
                            reason=_SCHEDULES_TABLE_CEILING_REASON,
                        )
                        for record in records_value:
                            if isinstance(record, dict):
                                schedule = ScheduleData(**self._deserialize_record(record))
                                if schedule.id:  # Use StateService-generated ID
                                    schedules[schedule.id] = schedule

                    return schedules
                return {}  # data is not a dict
            else:
                safe_log_error(self.logger, f"Failed to load schedules: {result}")
                return {}

        except Exception as e:
            safe_log_error(self.logger, f"Error loading schedules: {e}", exc_info=True)
            return {}

    def update_schedule_status(
        self,
        schedule_id: str,
        status: str,
        error_message: str | None = None,
    ) -> bool:
        """Update the status of a schedule.

        Args:
            schedule_id: ID of the schedule to update
            status: New status value
            error_message: Optional error message if status is ERROR

        Returns:
            True if update successful, False otherwise
        """
        if not self.state_service:
            safe_log_error(self.logger, "Cannot update status - state service not available")
            return False

        try:
            updates: dict[str, Any] = {"status": status}
            if error_message:
                updates["error_message"] = error_message

            result = self.state_service.update_state(
                namespace=self.namespace,
                query={"table": "schedules", "filters": {"id": schedule_id}},
                updates=updates,
            )

            if result.get("action_status") == "completed":
                return True
            else:
                safe_log_error(
                    self.logger,
                    f"Failed to update schedule {schedule_id} status: {result}",
                )
                return False

        except Exception as e:
            safe_log_error(
                self.logger,
                f"Error updating schedule {schedule_id} status: {e}",
                exc_info=True,
            )
            return False

    def delete_schedule(self, schedule_id: str) -> bool:
        """Delete a schedule from the database.

        Args:
            schedule_id: ID of the schedule to delete

        Returns:
            True if deletion successful, False otherwise
        """
        if not self.state_service:
            safe_log_error(self.logger, "Cannot delete schedule - state service not available")
            return False

        try:
            result = self.state_service.delete_records(
                namespace=self.namespace,
                query={"table": "schedules", "filters": {"id": schedule_id}, "soft_delete": False},
            )

            if result.get("action_status") == "completed":
                return True
            else:
                safe_log_error(self.logger, f"Failed to delete schedule {schedule_id}: {result}")
                return False

        except Exception as e:
            safe_log_error(
                self.logger,
                f"Error deleting schedule {schedule_id}: {e}",
                exc_info=True,
            )
            return False

    def delete_schedules_by_tag(self, tag: str) -> int:
        """Delete all schedules with a specific tag.

        Args:
            tag: Tag to match for deletion

        Returns:
            Number of schedules deleted
        """
        if not self.state_service:
            safe_log_error(self.logger, "Cannot delete schedules - state service not available")
            return 0

        try:
            # First, find all schedules with the tag
            schedules = self.load_all_schedules()
            matching_ids = [sid for sid, schedule in schedules.items() if tag in schedule.tags]

            # Delete each matching schedule
            deleted_count = 0
            for schedule_id in matching_ids:
                if self.delete_schedule(schedule_id):
                    deleted_count += 1

            return deleted_count

        except Exception as e:
            safe_log_error(
                self.logger,
                f"Error deleting schedules by tag '{tag}': {e}",
                exc_info=True,
            )
            return 0

    def cleanup_completed_one_time_schedules(self) -> int:
        """Remove completed one-time schedules from the database.

        Returns:
            Number of schedules cleaned up
        """
        if not self.state_service:
            safe_log_error(self.logger, "Cannot cleanup schedules - state service not available")
            return 0

        try:
            schedules = self.load_all_schedules()
            cleanup_count = 0

            for schedule_id, schedule in schedules.items():
                if schedule.type == "one_time" and schedule.status == SchedulerJobStatus.COMPLETED:
                    if self.delete_schedule(schedule_id):
                        cleanup_count += 1

            return cleanup_count

        except Exception as e:
            safe_log_error(self.logger, f"Error during schedule cleanup: {e}", exc_info=True)
            return 0
