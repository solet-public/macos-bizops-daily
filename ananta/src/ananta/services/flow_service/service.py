"""Flow Service Implementation.

Provides flow management and status tracking operations.
"""

import logging
from typing import Any, Protocol, runtime_checkable

from ananta.core.domain.constants import (
    KEY_ACTION_STATUS,
    KEY_DATA,
    KEY_RESULT,
    STATUS_COMPLETED,
    STATUS_ERROR,
)
from ananta.interfaces.state_service_protocol import StateServiceProtocol

logger = logging.getLogger(__name__)


@runtime_checkable
class FlowManagerProtocol(Protocol):
    """Protocol for FlowManager interface used by FlowService."""

    def create_flow(
        self,
        session_id: str,
        trigger_type: str,
        trigger_data: dict[str, object],
        priority: int = 5,
    ) -> str: ...

    def get_flow_trigger_data(self, flow_id: str) -> dict[str, object]: ...


class FlowService:
    """Flow service for creating and tracking flows."""

    def __init__(self, flow_manager: FlowManagerProtocol, state_service: StateServiceProtocol):
        """Initialize flow service.

        Args:
            flow_manager: FlowManager instance for flow creation
            state_service: State service instance for database access
        """
        self.flow_manager = flow_manager
        self.state_service = state_service
        self.namespace = "core"

    def create_flow(
        self,
        session_id: str,
        trigger_type: str,
        trigger_data: dict[str, object],
        priority: int = 5,
    ) -> dict[str, Any]:
        """Create a new flow record.

        Service interface methods receive individual kwargs (action_processor pattern).
        Delegates to FlowManager for flow creation.

        Args:
            session_id: Session ID this flow belongs to
            trigger_type: Type of trigger (e.g., user_input, scheduled_task)
            trigger_data: Trigger context data
            priority: Flow priority (1-10)

        Returns:
            ActionResult dict with flow_id
        """
        try:
            # Delegate to FlowManager
            flow_id = self.flow_manager.create_flow(
                session_id=session_id,
                trigger_type=trigger_type,
                trigger_data=trigger_data,
                priority=priority,
            )

            # Return standardized ActionResult
            return {
                KEY_ACTION_STATUS: STATUS_COMPLETED,
                KEY_DATA: {
                    KEY_RESULT: {
                        "flow_id": flow_id,
                    }
                },
                "actions": [],
            }

        except Exception as e:
            logger.error(f"Failed to create flow: {e}", exc_info=True)
            return {
                KEY_ACTION_STATUS: STATUS_ERROR,
                KEY_DATA: {
                    KEY_RESULT: {
                        "error": str(e),
                        "flow_id": None,
                    }
                },
                "actions": [],
            }

    def get_flow_status(
        self,
        flow_id: str,
    ) -> dict[str, Any]:
        """Retrieve flow status by ID.

        Service interface methods receive individual kwargs (action_processor pattern).
        Queries core__flows table for flow details.

        Args:
            flow_id: Flow ID to query

        Returns:
            ActionResult dict with flow record or None if not found
        """
        try:
            # Query StateService for flow
            result = self.state_service.read_state(
                namespace=self.namespace,
                query={
                    "table": "flows",
                    "filters": {"id": flow_id},
                },
            )

            # Extract flow record from result
            flow = None
            if result.get(KEY_ACTION_STATUS) == STATUS_COMPLETED:
                data_obj = result.get(KEY_DATA)
                if isinstance(data_obj, dict):
                    # StateService.read_state returns single record directly
                    flow = data_obj

            # Return standardized ActionResult
            return {
                KEY_ACTION_STATUS: STATUS_COMPLETED,
                KEY_DATA: {
                    KEY_RESULT: {
                        "flow": flow,
                    }
                },
                "actions": [],
            }

        except Exception as e:
            logger.error(f"Failed to retrieve flow status: {e}", exc_info=True)
            return {
                KEY_ACTION_STATUS: STATUS_ERROR,
                KEY_DATA: {
                    KEY_RESULT: {
                        "error": str(e),
                        "flow": None,
                    }
                },
                "actions": [],
            }

    def get_flow_input(
        self,
        flow_id: str,
    ) -> dict[str, Any]:
        """Extract original user input from flow's trigger_data.

        Handles both console and JSON-RPC trigger_data formats:
        - Console uses "user_input" key
        - JSON-RPC uses "input" key

        Args:
            flow_id: Flow ID to extract input from

        Returns:
            ActionResult dict with original_input string
        """
        try:
            # Use FlowManager's method which handles all StateService response formats
            trigger_data = self.flow_manager.get_flow_trigger_data(flow_id)

            if not trigger_data:
                return {
                    KEY_ACTION_STATUS: STATUS_COMPLETED,
                    KEY_DATA: {
                        KEY_RESULT: {
                            "original_input": "",
                            "flow_id": flow_id,
                            "message": "Flow not found or no trigger data",
                        }
                    },
                    "actions": [],
                }

            # Handle both formats: console uses "user_input", jsonrpc uses "input"
            original_input = str(trigger_data.get("user_input") or trigger_data.get("input") or "")

            # Include attachments if present (for file-based operations)
            attachments = trigger_data.get("attachments", [])

            # IO source metadata for plugin-addressed communication
            source_namespace = str(trigger_data.get("source_namespace", ""))
            source = str(trigger_data.get("source", ""))
            sender_name = str(trigger_data.get("sender_name", ""))
            session_id = str(trigger_data.get("session_id", ""))
            # P1-A 2026-06-16: `kind` discriminates between user-driven flows
            # and system-owned periodic crons. Consumed by
            # ``inference_service.inference_transaction._resolve_io_process_key``
            # to emit a structured FrameworkError citing the design contract
            # when a ``system_owned_periodic_cron`` flow reaches that path
            # (system-owned crons should be terminal/headless; reaching the
            # IO-process-key resolver indicates drift).
            kind = str(trigger_data.get("kind", ""))

            return {
                KEY_ACTION_STATUS: STATUS_COMPLETED,
                KEY_DATA: {
                    KEY_RESULT: {
                        "original_input": original_input,
                        "attachments": attachments,
                        "flow_id": flow_id,
                        "source_namespace": source_namespace,
                        "source": source,
                        "sender_name": sender_name,
                        "session_id": session_id,
                        "kind": kind,
                    }
                },
                "actions": [],
            }

        except Exception as e:
            logger.error(f"Failed to extract flow input: {e}", exc_info=True)
            return {
                KEY_ACTION_STATUS: STATUS_ERROR,
                KEY_DATA: {
                    KEY_RESULT: {
                        "original_input": "",
                        "error": str(e),
                    }
                },
                "actions": [],
            }

    def get_flow_input_for_presentation(
        self,
        flow_id: str,
    ) -> dict[str, Any]:
        """Extract flow input for result/error presentation (no attachments).

        Same as get_flow_input but excludes attachments to prevent confusion
        between input attachments and output attachment refs in presentation prompts.
        """
        result = self.get_flow_input(flow_id)
        # Strip attachments from the result to avoid LLM confusion
        if result.get(KEY_ACTION_STATUS) == STATUS_COMPLETED:
            result_data = result.get(KEY_DATA, {}).get(KEY_RESULT, {})
            result_data.pop("attachments", None)
        return result
