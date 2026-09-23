"""
Execution Tracking Manager Service

Responsibility: Handle all action execution tracking and performance monitoring operations
Dependencies: StateService, ActionExecutionRecord, logging, JSON serialization, datetime utilities
Complexity: Medium-High - focused on complex execution lifecycle tracking with state persistence

Extracted from ActionManager god class (B6 + B8 complexity execution tracking methods)
"""

import json
import logging
from datetime import UTC, datetime

from ananta.constants import FRAMEWORK_ACTION_EXECUTIONS_TABLE, FRAMEWORK_NAMESPACE
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.interfaces.state_provider_interface import ActionExecutionRecord

logger = logging.getLogger(__name__)


class ExecutionTrackingManager:
    """
    Service for managing action execution tracking and lifecycle monitoring.

    ARCHITECTURAL ROLE: Supporting service that extracts execution tracking logic
    from ActionManager while maintaining action lifecycle integrity.

    This service handles:
        pass
    - Action execution record creation and initialization
    - Execution start time tracking with parameter persistence
    - Provider information updates for execution context
    - Execution completion tracking with duration calculation
    - State service integration for persistent execution logging
    - Error handling and recovery for tracking operations
    - Performance timing and status management
    """

    def __init__(self, state_service=None) -> None:  # type: ignore[no-untyped-def]
        """Initialize ExecutionTrackingManager with required dependencies.

        SAFE: Optional dependency injected at runtime, explicit type would require import risking circular dependency.
        """
        self.state_service = state_service

    async def track_action_execution_start(
        self,
        execution_id: str,
        action_name: str,
        parameters: dict[str, object],
        start_time: datetime,
        source_context: dict[str, object] | None = None,
    ) -> None:
        """
        Track the start of an action execution with complete initialization.

        EXTRACTED FROM: ActionManager._track_action_execution_start() - B(6) complexity

        This method handles initial execution record creation and persistence:
            pass
        1. Creates ActionExecutionRecord with standardized schema
        2. Serializes parameters and source context for storage
        3. Writes initial execution state to persistent storage
        4. Provides comprehensive error handling and logging

        Args:
            execution_id: Unique identifier for the execution
            action_name: Name of the action being executed
            parameters: Action parameters to be stored
            start_time: Execution start timestamp
            source_context: Optional context information for the execution

        Returns:
            None: Logs success/failure, does not raise exceptions to prevent cascade failures
        """
        try:
            if not self.state_service:
                return

            # Create ActionExecutionRecord with standardized schema
            execution_record = ActionExecutionRecord(
                id=execution_id,
                action_name=action_name,
                provider_type="framework",
                provider="framework",
                status="processing",
                parameters=json.dumps(parameters) if parameters else None,
                started_at=start_time,
                source_context=json.dumps(source_context) if source_context else None,
            )

            # Use the proper state interface
            result = self.state_service.write_state(
                namespace=FRAMEWORK_NAMESPACE,
                data={
                    "table": FRAMEWORK_ACTION_EXECUTIONS_TABLE,
                    "records": [execution_record.__dict__],
                },
            )

            if result.get("action_status") == ActionStatus.COMPLETED.value:
                pass
            else:
                logger.error(f"Failed to start execution tracking for {execution_id}: {result}")

        except Exception as e:
            logger.error(f"Error starting execution tracking for {execution_id}: {e}")

    async def update_execution_provider(
        self, execution_id: str, provider_type: str, provider: str
    ) -> None:
        """
        Update provider information for an execution record.

        EXTRACTED FROM: ActionManager._update_execution_provider() - A complexity helper

        Args:
            execution_id: Unique identifier for the execution
            provider_type: Type of provider executing the action
            provider: Specific provider instance name

        Returns:
            None: Logs success/failure, does not raise exceptions
        """
        try:
            if not self.state_service:
                return

            # Use the proper state interface
            result = self.state_service.update_state(
                namespace=FRAMEWORK_NAMESPACE,
                query={"table": FRAMEWORK_ACTION_EXECUTIONS_TABLE, "execution_id": execution_id},
                updates={"provider_type": provider_type, "provider": provider},
            )

            if result.get("action_status") != ActionStatus.COMPLETED.value:
                logger.error(f"Failed to update provider for execution {execution_id}: {result}")

        except Exception as e:
            logger.error(f"Error updating provider for execution {execution_id}: {e}")

    async def track_action_execution_end(
        self,
        execution_id: str,
        start_time: datetime,
        success: bool,
        result: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> None:
        """
        Track the completion of an action execution with full performance data.

        EXTRACTED FROM: ActionManager._track_action_execution_end() - B(8) complexity

        This method handles execution completion and performance tracking:
            pass
        1. Calculates execution duration with millisecond precision
        2. Determines execution status based on success/failure
        3. Serializes result and error data for persistent storage
        4. Updates execution record with completion information
        5. Provides comprehensive error handling and logging

        Args:
            execution_id: Unique identifier for the execution
            start_time: Original execution start timestamp
            success: Whether the execution completed successfully
            result: Optional result data from the execution
            error: Optional error information if execution failed

        Returns:
            None: Logs success/failure, does not raise exceptions to prevent cascade failures
        """
        try:
            if not self.state_service:
                return

            end_time = datetime.now(UTC)
            execution_time_ms = int((end_time - start_time).total_seconds() * 1000)

            update_data = {
                "completed_at": end_time,
                "duration_ms": execution_time_ms,
                "status": "completed" if success else "error",
            }

            if result:
                update_data["result"] = json.dumps(result)
            if error:
                update_data["error"] = json.dumps(error)

            # Use the proper state interface
            update_result = self.state_service.update_state(
                namespace=FRAMEWORK_NAMESPACE,
                query={"table": FRAMEWORK_ACTION_EXECUTIONS_TABLE, "execution_id": execution_id},
                updates=update_data,
            )

            if update_result and update_result.get("action_status") == ActionStatus.COMPLETED.value:
                pass
            else:
                logger.error(
                    f"Failed to complete execution tracking for {execution_id}: {update_result}"
                )

        except Exception as e:
            logger.error(f"Error completing execution tracking for {execution_id}: {e}")
