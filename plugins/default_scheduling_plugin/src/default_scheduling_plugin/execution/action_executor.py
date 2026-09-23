"""Action execution module for scheduled actions.

This module handles the execution of scheduled actions, managing context
preservation and ActionFactory interaction.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from ananta.core.domain.enums import ActionStatus

from ..models import ActionData, ScheduleData
from ..protocols import ActionFactoryProtocol
from ..utils.logging_utils import safe_log_error

RELOAD_SAFE = True


class ActionExecutor:
    """Executes scheduled actions with proper context preservation.

    Handles:
    - Action definition creation with session/flow context
    - Submission via ActionFactory
    - Error handling during execution
    - Logging of execution lifecycle
    """

    def __init__(
        self,
        action_factory: ActionFactoryProtocol | None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize the action executor.

        Args:
            action_factory: Factory for submitting actions (can be None)
            logger: Optional logger for operation tracking
        """
        self.action_factory = action_factory
        self.logger = logger

    def execute_scheduled_actions(
        self,
        schedule: ScheduleData,
    ) -> tuple[bool, str | None]:
        """Execute all actions defined in a schedule.

        Args:
            schedule: Schedule containing actions to execute

        Returns:
            Tuple of (success: bool, error_message: str | None)
        """
        if not self.action_factory:
            error_msg = "Cannot execute actions - ActionFactory not available"
            safe_log_error(self.logger, error_msg)
            return False, error_msg

        try:
            # Execute all actions in the schedule
            actions = schedule.actions
            if not actions:
                # Fallback to legacy single action format for backward compatibility
                if schedule.action_name:
                    actions = [
                        ActionData(
                            name=schedule.action_name,
                            parameters=schedule.action_parameters,
                            result_processor=None,
                            result_processor_kind=None,
                        )
                    ]
                else:
                    error_msg = f"No actions defined in schedule {schedule.id}"
                    safe_log_error(self.logger, error_msg)
                    return False, error_msg

            # Execute each action
            for i, action in enumerate(actions, 1):
                success, error = self._execute_single_action(
                    action=action,
                    schedule_id=schedule.id or "unknown",
                    session_id=schedule.session_id,
                    flow_id=schedule.flow_id,
                    action_index=i,
                    total_actions=len(actions),
                )
                if not success:
                    return False, error

            return True, None

        except Exception as e:
            error_msg = f"Error executing schedule {schedule.id}: {e}"
            safe_log_error(self.logger, error_msg, exc_info=True)
            return False, error_msg

    def _execute_single_action(
        self,
        action: ActionData,
        schedule_id: str,
        session_id: str | None,
        flow_id: str | None,
        action_index: int,
        total_actions: int,
    ) -> tuple[bool, str | None]:
        """Execute a single action with context preservation.

        Args:
            action: Action to execute
            schedule_id: ID of the schedule
            session_id: Session context to preserve
            flow_id: Flow context to preserve
            action_index: Index of this action (1-based)
            total_actions: Total number of actions in schedule

        Returns:
            Tuple of (success: bool, error_message: str | None)
        """
        try:
            action_definition = self._build_action_definition(action, session_id, flow_id)
            template_context = self._build_template_context(session_id, flow_id)

            self._submit_action(
                action_definition,
                template_context,
                action,
                action_index,
                total_actions,
                schedule_id,
            )

            return True, None

        except Exception as e:
            error_msg = f"Error executing action {action.name} (index {action_index}): {e}"
            safe_log_error(self.logger, error_msg, exc_info=True)
            return False, error_msg

    def _build_action_definition(
        self,
        action: ActionData,
        session_id: str | None,
        flow_id: str | None,
    ) -> dict[str, Any]:
        """Build action definition with context."""
        action_arguments = dict(action.parameters)
        action_definition: dict[str, Any] = {
            "name": action.name,
            "process_key": action.name,
            "arguments": action_arguments,
            "action_status": ActionStatus.QUEUED,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        }

        if action.result_processor:
            action_definition["result_processor"] = action.result_processor

        if action.result_processor_kind is not None:
            action_definition["result_processor_kind"] = action.result_processor_kind

        self._apply_context_to_definition(action_definition, action_arguments, session_id, flow_id)

        return action_definition

    def _apply_context_to_definition(
        self,
        action_definition: dict[str, Any],
        action_arguments: dict[str, Any],
        session_id: str | None,
        flow_id: str | None,
    ) -> None:
        """Apply session and flow context to action definition."""
        if session_id:
            action_definition["session_id"] = session_id
        if flow_id:
            action_definition["flow_id"] = flow_id

    def _build_template_context(
        self,
        session_id: str | None,
        flow_id: str | None,
    ) -> dict[str, Any]:
        """Build template context for action execution."""
        template_context: dict[str, Any] = {}
        runtime_args: dict[str, Any] = {}

        if session_id:
            template_context["session_id"] = session_id
            runtime_args["session_id"] = session_id
        else:
            safe_log_error(
                self.logger,
                "SCHEDULER-CALLBACK-CONTEXT-ERROR: Missing session_id; template functions may fail",
            )

        if flow_id:
            template_context["flow_id"] = flow_id
            runtime_args["flow_id"] = flow_id

        if runtime_args:
            template_context["runtime_args"] = runtime_args

        return template_context

    def _submit_action(
        self,
        action_definition: dict[str, Any],
        template_context: dict[str, Any],
        action: ActionData,
        action_index: int,
        total_actions: int,
        schedule_id: str,
    ) -> None:
        """Submit action to action factory."""
        if not self.action_factory:
            raise RuntimeError("ActionFactory became unavailable during execution")

        self.action_factory.submit_action_definition(action_definition, template_context)
