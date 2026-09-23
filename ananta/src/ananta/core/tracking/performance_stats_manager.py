"""
Performance Stats Manager Service

Responsibility: Handle all action performance statistics computation and analysis operations
Dependencies: StateService, logging, datetime utilities, statistical calculations
Complexity: High - focused on complex performance analysis with time-series data processing

Extracted from ActionManager god class (B6 + B8 complexity performance statistics methods)
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from ananta.constants import FRAMEWORK_NAMESPACE
from ananta.core.domain.status import is_status_match
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.interfaces.state_service_protocol import StateServiceProtocol

logger = logging.getLogger(__name__)


class ExecutionRecord(TypedDict, total=False):
    """Type definition for action execution records."""

    action_name: str
    started_at: str
    duration_ms: float
    status: str


class PerformanceStats(TypedDict):
    """Type definition for performance statistics."""

    total_executions: int
    successful_executions: int
    failed_executions: int
    success_rate: float
    average_execution_time_ms: float
    min_execution_time_ms: float
    max_execution_time_ms: float


class PerformanceStatsManager:
    """
    Service for managing action performance statistics computation and analysis.

    ARCHITECTURAL ROLE: Supporting service that extracts performance analytics logic
    from ActionManager while maintaining action performance monitoring integrity.

    This service handles:
    - Time-windowed performance data retrieval with flexible filtering
    - Action execution record queries with complex filter conditions
    - Performance statistics computation (success rates, timing metrics, etc.)
    - Action grouping and statistical aggregation for multi-action analysis
    - Error handling and data validation for statistical operations
    - Historical trend analysis and performance monitoring
    """

    def __init__(self, state_service: StateServiceProtocol | None = None) -> None:
        """Initialize PerformanceStatsManager with required dependencies."""
        self.state_service = state_service

    async def get_action_performance_stats(
        self, action_name: str | None = None, hours: int = 24
    ) -> dict[str, object]:
        """
        Retrieve comprehensive action performance statistics with time-window filtering.

        EXTRACTED FROM: ActionManager.get_action_performance_stats() - B(6) complexity

        This method handles performance data retrieval and analysis:
        1. Calculates time window boundaries for statistical analysis
        2. Constructs complex filter conditions for state service queries
        3. Retrieves action execution records with proper error handling
        4. Delegates statistical computation to performance calculation methods
        5. Formats comprehensive performance reports with metadata

        Args:
            action_name: Optional specific action name to filter statistics
            hours: Time window in hours for performance analysis (default 24)

        Returns:
            dict: Comprehensive performance statistics including:
                - executions: List of execution records within time window
                - stats: Calculated performance metrics by action
                - time_window_hours: Applied time window
                - query_time: Timestamp of statistical analysis

        Raises:
            Exception: Logged but not re-raised to prevent cascade failures
        """
        try:
            if not self.state_service:
                return {"error": "State service not available"}

            # Calculate time window
            since_time = datetime.now(UTC) - timedelta(hours=hours)

            filters = {"started_at >=": since_time.isoformat()}

            if action_name:
                filters["action_name"] = action_name

            result = self.state_service.read_state(
                namespace=FRAMEWORK_NAMESPACE,
                query={
                    "table": "action_executions",
                    "filters": filters,
                    "order_by": "started_at DESC",
                },
            )

            # Type narrow result to check if it has the expected structure
            action_status = result.get("action_status")
            if not is_status_match(action_status, ActionStatus.COMPLETED):
                return {"executions": [], "stats": {}}

            data_obj = result.get("data")
            if not isinstance(data_obj, dict):
                return {"executions": [], "stats": {}}

            result_obj = data_obj.get("result")
            if not isinstance(result_obj, dict):
                return {"executions": [], "stats": {}}

            records_obj = result_obj.get("records")
            if not isinstance(records_obj, list):
                return {"executions": [], "stats": {}}

            records: list[dict[str, object]] = []
            for record in records_obj:
                if isinstance(record, dict):
                    records.append(record)

            # Calculate statistics
            stats = self.calculate_performance_stats(records, action_name)

            return {
                "executions": records,
                "stats": stats,
                "time_window_hours": hours,
                "query_time": datetime.now(UTC).isoformat(),
            }

        except Exception as e:
            logger.error(f"Error retrieving action performance stats: {e}")
            return {"error": str(e)}

    def calculate_performance_stats(
        self, records: list[dict[str, object]], action_name: str | None = None
    ) -> dict[str, object]:
        """
        Calculate comprehensive performance statistics from execution records.

        EXTRACTED FROM: ActionManager._calculate_performance_stats() - B(6) complexity

        This method handles statistical aggregation and grouping:
        1. Groups execution records by action name for multi-action analysis
        2. Delegates detailed computation to action-specific statistics methods
        3. Provides flexible single-action vs multi-action statistical analysis

        Args:
            records: List of execution records to analyze
            action_name: Optional specific action name for focused analysis

        Returns:
            dict: Performance statistics grouped by action name
        """
        if not records:
            return {}

        # Group by action if not filtering by specific action
        if action_name:
            action_groups: dict[str, list[dict[str, object]]] = {action_name: records}
        else:
            action_groups = {}
            for record in records:
                name_obj = record["action_name"]
                # Type narrow to str using isinstance
                if not isinstance(name_obj, str):
                    continue
                name: str = name_obj
                if name not in action_groups:
                    action_groups[name] = []
                action_groups[name].append(record)

        stats: dict[str, object] = {}

        for name, action_records in action_groups.items():
            stats[name] = self.compute_action_stats(action_records)

        return stats

    def compute_action_stats(self, action_records: list[dict[str, object]]) -> dict[str, object]:
        """
        Compute detailed performance statistics for a single action's execution records.

        EXTRACTED FROM: ActionManager._compute_action_stats() - B(8) complexity

        This method handles comprehensive performance metric calculation:
        1. Filters execution records by completion status and error conditions
        2. Computes execution timing statistics (average, min, max duration)
        3. Calculates success/failure rates and execution counts
        4. Provides detailed performance metrics for monitoring and analysis

        Args:
            action_records: List of execution records for a single action

        Returns:
            dict: Detailed performance statistics including:
                - total_executions: Total number of execution attempts
                - successful_executions: Count of successful completions
                - failed_executions: Count of failed executions
                - success_rate: Ratio of successful to total executions
                - average_execution_time_ms: Mean execution duration
                - min_execution_time_ms: Fastest execution time
                - max_execution_time_ms: Slowest execution time
        """
        completed_records = [r for r in action_records if r.get("duration_ms") is not None]
        failed_records = [r for r in action_records if r.get("status") == "error"]

        if completed_records:
            # Type narrow duration_ms values to float using isinstance
            execution_times: list[float] = []
            for r in completed_records:
                duration_obj = r["duration_ms"]
                if isinstance(duration_obj, int | float):
                    execution_times.append(float(duration_obj))

            if execution_times:
                avg_time: float = sum(execution_times) / len(execution_times)
                min_time: float = min(execution_times)
                max_time: float = max(execution_times)
            else:
                avg_time = min_time = max_time = 0.0
        else:
            avg_time = min_time = max_time = 0.0

        return {
            "total_executions": len(action_records),
            "successful_executions": len(completed_records),
            "failed_executions": len(failed_records),
            "success_rate": (len(completed_records) / len(action_records) if action_records else 0),
            "average_execution_time_ms": avg_time,
            "min_execution_time_ms": min_time,
            "max_execution_time_ms": max_time,
        }
