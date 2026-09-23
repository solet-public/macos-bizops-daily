"""Universal job tracking for cross-system job monitoring.

This module manages job entries in the core.asynchronous_jobs table, providing
centralized tracking of scheduled job execution status across the system.
"""

from __future__ import annotations

import logging
from typing import Any

from ananta.interfaces.state_service_protocol import StateServiceProtocol

from ..utils.logging_utils import safe_log_error

RELOAD_SAFE = True


class UniversalJobTracker:
    """Manages universal job tracking in core.asynchronous_jobs table.

    Provides centralized tracking of scheduled jobs across the system, allowing
    cross-plugin monitoring and status updates.
    """

    def __init__(
        self,
        state_service: StateServiceProtocol | None,
        plugin_name: str,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize the universal job tracker.

        Args:
            state_service: State service for database operations (can be None)
            plugin_name: Name of the plugin creating jobs
            logger: Optional logger for operation tracking
        """
        self.state_service = state_service
        self.plugin_name = plugin_name
        self.logger = logger

    def create_job(
        self,
        schedule_id: str,
        action_name: str,
        schedule_data: dict[str, Any],
    ) -> bool:
        """Create a schedule job entry in the universal asynchronous_jobs table.

        Args:
            schedule_id: Unique identifier for the schedule
            action_name: Name of the action being scheduled
            schedule_data: Complete schedule data for tracking

        Returns:
            bool: True if job was created successfully, False otherwise
        """
        try:
            if not self.state_service:
                safe_log_error(
                    self.logger,
                    "Cannot create universal job: StateService not available",
                )
                return False

            result = self.state_service.write_state(
                namespace="core",
                data={
                    "table": "asynchronous_jobs",
                    "record": {
                        "external_id": schedule_id,
                        "plugin_name": self.plugin_name,
                        "action_name": action_name,
                        "status": "pending",
                        "priority": 200,
                        "request_data": schedule_data,
                        "result_data": None,
                        "error_message": None,
                    },
                },
            )

            if result.get("action_status") == "completed":
                return True
            else:
                safe_log_error(
                    self.logger,
                    f"Failed to create universal schedule job: {result}",
                )
                return False

        except Exception as e:
            safe_log_error(
                self.logger,
                f"Error creating universal schedule job {schedule_id}: {e}",
            )
            return False

    def update_job(
        self,
        schedule_id: str,
        updates: dict[str, Any],
    ) -> bool:
        """Update a schedule job in the universal asynchronous_jobs table.

        Args:
            schedule_id: Unique identifier for the schedule
            updates: Dictionary of fields to update (supports 'status', 'result', 'error')

        Returns:
            bool: True if job was updated successfully, False otherwise

        Note:
            The 'result' key in updates is mapped to 'result_data' field.
            The 'error' key in updates is mapped to 'error_message' field.
        """
        try:
            if not self.state_service:
                safe_log_error(
                    self.logger,
                    "Cannot update universal job: StateService not available",
                )
                return False

            # Map update fields to database schema
            update_data = dict(updates)
            if "result" in updates:
                update_data["result_data"] = updates["result"]
                del update_data["result"]
            if "error" in updates:
                update_data["error_message"] = updates["error"]
                del update_data["error"]

            result = self.state_service.update_state(
                namespace="core",
                query={
                    "table": "asynchronous_jobs",
                    "filters": {"external_id": schedule_id},
                },
                updates=update_data,
            )

            if result.get("action_status") == "completed":
                return True
            else:
                safe_log_error(
                    self.logger,
                    f"Failed to update universal schedule job: {result}",
                )
                return False

        except Exception as e:
            safe_log_error(
                self.logger,
                f"Error updating universal schedule job {schedule_id}: {e}",
            )
            return False
