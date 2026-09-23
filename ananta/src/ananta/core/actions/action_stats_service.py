"""
Action Statistics Service

Responsibility: Handle all action performance tracking and statistics operations for ActionExecutionEngine
Dependencies: StateService, ActionStatus, logging
Complexity: Medium-High - focused on performance tracking, statistics computation, and database operations

Extracted from ActionExecutionEngine god class (4 methods, including C(12) complexity method)
"""

import json
import logging
from datetime import UTC, datetime

from ananta.constants import FRAMEWORK_ACTION_EXECUTIONS_TABLE, FRAMEWORK_NAMESPACE
from ananta.core.domain.status import is_status_match
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.interfaces.state_service_protocol import StateServiceProtocol

logger = logging.getLogger(__name__)


class ActionStatsService:
    """
    Service for managing action execution tracking and performance statistics.

    ARCHITECTURAL ROLE: Supporting service that extracts performance tracking logic
    from ActionExecutionEngine while maintaining execution engine integrity.

    This service handles:
    - Tracking action execution start/end events
    - Computing comprehensive performance statistics
    - Managing execution records in database
    - Providing performance analytics and reporting
    """

    def __init__(self, state_service: StateServiceProtocol | None = None) -> None:
        """Initialize ActionStatsService."""
        self.state_service = state_service

    async def track_action_execution_start(
        self,
        execution_id: str,
        action_name: str,
        action_parameters: dict[str, object],
        start_time: datetime,
        source_context: dict[str, object],
    ) -> None:
        """
        Track the start of action execution.

        EXTRACTED FROM: ActionExecutionEngine._track_action_execution_start() - A complexity

        Args:
            execution_id: Unique identifier for this execution
            action_name: Name of the action being executed
            action_parameters: Parameters passed to the action
            start_time: When execution started
            source_context: Context information about execution source
        """
        try:
            if not self.state_service:
                logger.error("State service not available for execution tracking")
                return

            execution_record = {
                "execution_id": execution_id,
                "action_name": action_name,
                "parameters": json.dumps(action_parameters),
                "status": "running",
                "start_time": start_time.isoformat(),
                "source_context": json.dumps(source_context),
            }

            result = self.state_service.write_state(
                namespace=FRAMEWORK_NAMESPACE,
                data={"table": FRAMEWORK_ACTION_EXECUTIONS_TABLE, "record": execution_record},
            )

            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                logger.error(f"Failed to track execution start for {execution_id}: {result}")

        except Exception as e:
            logger.error(f"Error tracking execution start for {execution_id}: {e}")

    async def track_action_execution_end(
        self,
        execution_id: str,
        status: ActionStatus,
        result: dict[str, object],
        error_message: str | None,
        start_time: datetime,
    ) -> None:
        """
        Track the end of action execution.

        EXTRACTED FROM: ActionExecutionEngine._track_action_execution_end() - B(6) complexity

        Args:
            execution_id: Unique identifier for this execution
            status: Final status of the action execution
            result: Result data from the action
            error_message: Error message if execution failed
            start_time: When execution started (used to compute duration)
        """
        try:
            if not self.state_service:
                return

            end_time = datetime.now(UTC)
            duration_seconds = (end_time - start_time).total_seconds()

            update_data = {
                "status": status.value,
                "end_time": end_time.isoformat(),
                "duration_seconds": duration_seconds,
                "result_summary": json.dumps({"keys": list(result.keys())} if result else {}),
            }

            if error_message:
                update_data.update({"error_message": error_message})

            update_result = self.state_service.write_state(
                namespace=FRAMEWORK_NAMESPACE,
                data={
                    "table": FRAMEWORK_ACTION_EXECUTIONS_TABLE,
                    "filters": {"execution_id": execution_id},
                    "record": update_data,
                    "upsert": True,
                },
            )

            if not is_status_match(update_result.get("action_status"), ActionStatus.COMPLETED):
                logger.error(
                    f"Failed to update execution record for {execution_id}: {update_result}"
                )

        except Exception as e:
            logger.error(f"Error tracking execution end for {execution_id}: {e}")

    def _parse_records_from_result(self, records_obj: object) -> list[dict[str, object]] | None:
        """Parse and validate records list from query result."""
        if not isinstance(records_obj, list):
            return None

        records: list[dict[str, object]] = []
        for record in records_obj:
            if isinstance(record, dict):
                records.append(record)

        return records if records else None

    async def get_action_performance_stats(
        self, action_name: str | None = None
    ) -> dict[str, object]:
        """Get performance statistics for actions."""
        try:
            if not self.state_service:
                logger.error("State service not available for performance stats")
                return {}

            query: dict[str, object] = {"table": FRAMEWORK_ACTION_EXECUTIONS_TABLE}
            if action_name:
                query["filters"] = {"action_name": action_name}

            result = self.state_service.read_state(namespace=FRAMEWORK_NAMESPACE, query=query)

            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                logger.error(f"Failed to retrieve performance stats: {result}")
                return {}

            data_obj = result.get("data")
            records_obj = data_obj.get("records", []) if isinstance(data_obj, dict) else []
            if not records_obj:
                suffix = f" for action {action_name}" if action_name else ""
                return {"message": f"No execution records found{suffix}"}

            records = self._parse_records_from_result(records_obj)
            if records is None:
                return {"error": "Invalid records format"}
            if not records:
                return {"message": "No valid records found"}

            return self.compute_action_stats(records)

        except Exception as e:
            logger.error(f"Error retrieving performance stats: {e}")
            return {"error": str(e)}

    def compute_action_stats(self, action_records: list[dict[str, object]]) -> dict[str, object]:
        """
        Compute comprehensive performance statistics from action execution records.

        EXTRACTED FROM: ActionExecutionEngine._compute_action_stats() - C(12) complexity

        Args:
            action_records: List of action execution records from database

        Returns:
            Dictionary containing computed performance statistics including:
            - Total/successful/failed execution counts
            - Success rates and timing statistics
            - Per-action breakdowns with detailed metrics
        """
        if not action_records:
            return {"message": "No action records to analyze"}

        # Process all records to collect raw metrics
        raw_metrics = self._collect_raw_metrics(action_records)

        # Build summary statistics
        stats = self._build_summary_stats(raw_metrics)

        # Process per-action statistics
        action_breakdown_obj = raw_metrics["action_breakdown"]
        if isinstance(action_breakdown_obj, dict):
            stats["action_breakdown"] = self._build_action_breakdown_stats(action_breakdown_obj)
        else:
            stats["action_breakdown"] = {}

        return stats

    def _init_action_breakdown_entry(self) -> dict[str, object]:
        """Create initial action breakdown entry."""
        return {"total": 0, "successful": 0, "failed": 0, "durations": []}

    def _increment_breakdown_counter(self, breakdown: dict[str, object], key: str) -> None:
        """Increment a counter in breakdown dict."""
        val = breakdown.get(key, 0)
        if isinstance(val, int):
            breakdown[key] = val + 1

    def _track_duration_value(
        self, duration: object, durations: list[float], breakdown: dict[str, object]
    ) -> None:
        """Track duration in global list and breakdown."""
        if duration is None or not isinstance(duration, int | float | str):
            return
        try:
            duration_float = float(duration)
            durations.append(duration_float)
            durations_list = breakdown.get("durations")
            if isinstance(durations_list, list):
                durations_list.append(duration_float)
        except (ValueError, TypeError):
            pass

    def _collect_raw_metrics(self, action_records: list[dict[str, object]]) -> dict[str, object]:
        """Collect raw metrics from action records for further processing."""
        successful_executions = 0
        failed_executions = 0
        durations: list[float] = []
        action_breakdown: dict[str, dict[str, object]] = {}

        for record in action_records:
            action_name_obj = record.get("action_name", "unknown")
            action_name = str(action_name_obj) if action_name_obj is not None else "unknown"

            if action_name not in action_breakdown:
                action_breakdown[action_name] = self._init_action_breakdown_entry()

            breakdown = action_breakdown[action_name]
            self._increment_breakdown_counter(breakdown, "total")

            status = record.get("status")
            if status == ActionStatus.COMPLETED.value:
                successful_executions += 1
                self._increment_breakdown_counter(breakdown, "successful")
            else:
                failed_executions += 1
                self._increment_breakdown_counter(breakdown, "failed")

            self._track_duration_value(record.get("duration_seconds"), durations, breakdown)

        return {
            "total_executions": len(action_records),
            "successful_executions": successful_executions,
            "failed_executions": failed_executions,
            "durations": durations,
            "action_breakdown": action_breakdown,
        }

    def _build_summary_stats(self, raw_metrics: dict[str, object]) -> dict[str, object]:
        """Build summary statistics from raw metrics."""
        # Type narrowing: extract and validate integer metrics
        total_executions_obj = raw_metrics["total_executions"]
        successful_executions_obj = raw_metrics["successful_executions"]
        failed_executions_obj = raw_metrics["failed_executions"]
        durations_obj = raw_metrics["durations"]

        # Ensure we have integer values
        if not isinstance(total_executions_obj, int):
            total_executions = 0
        else:
            total_executions = total_executions_obj

        if not isinstance(successful_executions_obj, int):
            successful_executions = 0
        else:
            successful_executions = successful_executions_obj

        if not isinstance(failed_executions_obj, int):
            failed_executions = 0
        else:
            failed_executions = failed_executions_obj

        stats: dict[str, object] = {
            "total_executions": total_executions,
            "successful_executions": successful_executions,
            "failed_executions": failed_executions,
            "success_rate": round((successful_executions / total_executions) * 100, 2)
            if total_executions > 0
            else 0,
        }

        # Add timing statistics if available
        if isinstance(durations_obj, list) and durations_obj:
            # Verify all elements are floats
            float_durations: list[float] = []
            for d in durations_obj:
                if isinstance(d, int | float):
                    float_durations.append(float(d))

            if float_durations:
                stats["average_duration_seconds"] = round(
                    sum(float_durations) / len(float_durations), 3
                )
                stats["min_duration_seconds"] = round(min(float_durations), 3)
                stats["max_duration_seconds"] = round(max(float_durations), 3)

        return stats

    def _parse_breakdown_counts(self, breakdown_obj: dict[str, object]) -> tuple[int, int, int]:
        """Parse and validate breakdown count values."""
        total_obj = breakdown_obj.get("total", 0)
        successful_obj = breakdown_obj.get("successful", 0)
        failed_obj = breakdown_obj.get("failed", 0)

        total = int(total_obj) if isinstance(total_obj, int | float) else 0
        successful = int(successful_obj) if isinstance(successful_obj, int | float) else 0
        failed = int(failed_obj) if isinstance(failed_obj, int | float) else 0

        return total, successful, failed

    def _extract_float_durations(self, durations_obj: object) -> list[float]:
        """Extract and validate float durations from durations list."""
        if not isinstance(durations_obj, list) or not durations_obj:
            return []

        float_durations: list[float] = []
        for d in durations_obj:
            if isinstance(d, int | float):
                float_durations.append(float(d))

        return float_durations

    def _add_duration_stats(self, stats: dict[str, object], float_durations: list[float]) -> None:
        """Add duration statistics to stats dict if durations available."""
        if not float_durations:
            return

        stats["average_duration_seconds"] = round(sum(float_durations) / len(float_durations), 3)
        stats["min_duration_seconds"] = round(min(float_durations), 3)
        stats["max_duration_seconds"] = round(max(float_durations), 3)

    def _build_action_breakdown_stats(
        self, action_breakdown: dict[str, object]
    ) -> dict[str, dict[str, object]]:
        """Build per-action statistics from action breakdown data."""
        result: dict[str, dict[str, object]] = {}

        for action_name, breakdown_obj in action_breakdown.items():
            if not isinstance(breakdown_obj, dict):
                continue

            total, successful, failed = self._parse_breakdown_counts(breakdown_obj)

            action_stats: dict[str, object] = {
                "total": total,
                "successful": successful,
                "failed": failed,
                "success_rate": round((successful / total) * 100, 2) if total > 0 else 0,
            }

            float_durations = self._extract_float_durations(breakdown_obj.get("durations", []))
            self._add_duration_stats(action_stats, float_durations)

            result[action_name] = action_stats

        return result
