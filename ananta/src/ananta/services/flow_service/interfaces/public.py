"""Flow Service Public API.

AI-discoverable flow management operations with @service_interface_process decorators.
All methods in this interface are indexed for process discovery.
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.services.service_interface_decorator import service_interface_process


class FlowServiceAPI(ABC):
    """Public flow service operations - AI-discoverable via process registry.

    This interface defines flow management operations that can be discovered
    and invoked by the AI orchestration system:

    1. create_flow - Create a new flow record for tracking actions
    2. get_flow_status - Retrieve flow execution status

    Each method is decorated with complete metadata for process registry.
    """

    @service_interface_process(
        name="create_flow",
        provider="flow_service",
        parameters={
            "session_id": ParameterMetadata(
                description="Session ID this flow belongs to",
                required=True,
                type=ParameterType.STRING,
            ),
            "trigger_type": ParameterMetadata(
                description=(
                    "Type of trigger that initiated this flow "
                    "(e.g., user_input, scheduled_task, webhook)"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "trigger_data": ParameterMetadata(
                description="JSON data describing the trigger context",
                required=True,
                type=ParameterType.OBJECT,
            ),
            "priority": ParameterMetadata(
                description="Flow priority (1-10, default 5)",
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Created flow ID",
            type=ParameterType.OBJECT,
            properties={
                "flow_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Generated flow ID for action correlation",
                    required=True,
                ),
            },
            usage_patterns=[
                "Create flow before submitting actions to correlate them",
                "Track workflow execution by flow_id",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def create_flow(
        self, session_id: str, trigger_type: str, trigger_data: dict[str, object], priority: int = 5
    ) -> dict[str, Any]:
        """Create a new flow record.

        Service interface receives individual kwargs (action_processor pattern).
        Creates a flow record in core__flows table.
        """
        ...

    @service_interface_process(
        name="get_flow_status",
        provider="flow_service",
        parameters={
            "flow_id": ParameterMetadata(
                description="Flow ID to query", required=True, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Flow status information",
            type=ParameterType.OBJECT,
            properties={
                "flow": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Flow record from core__flows table",
                    required=False,
                ),
            },
            usage_patterns=[
                "Check if flow completed successfully",
                "Retrieve flow trigger data for debugging",
                "Monitor flow execution progress",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def get_flow_status(self, flow_id: str) -> dict[str, Any]:
        """Retrieve flow status by ID.

        Service interface receives individual kwargs (action_processor pattern).
        Queries core__flows table for flow details.
        """
        ...

    @service_interface_process(
        name="get_flow_input",
        provider="flow_service",
        parameters={
            "flow_id": ParameterMetadata(
                description="Flow ID to extract input from",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Original user input and flow context",
            type=ParameterType.OBJECT,
            properties={
                "original_input": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The original user input that triggered the flow",
                    required=True,
                ),
                "flow_id": ParameterMetadata(
                    type=ParameterType.STRING, description="The flow ID queried", required=False
                ),
            },
            usage_patterns=[
                "Include original query in result notifications",
                "Provide context for downstream actions in a flow",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def get_flow_input(self, flow_id: str) -> dict[str, Any]:
        """Extract original user input from flow's trigger_data.

        Service interface receives individual kwargs (action_processor pattern).
        Handles both console and JSON-RPC trigger_data formats.
        """
        ...

    @service_interface_process(
        name="get_flow_input_for_presentation",
        provider="flow_service",
        parameters={
            "flow_id": ParameterMetadata(
                description="Flow ID to extract input from",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Original user input and flow context (no attachments)",
            type=ParameterType.OBJECT,
            properties={
                "original_input": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The original user input that triggered the flow",
                    required=True,
                ),
                "flow_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The flow ID queried",
                    required=False,
                ),
            },
            usage_patterns=[
                "Include original query in result notifications",
                "Provide context for result/error presentation without input attachments",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def get_flow_input_for_presentation(self, flow_id: str) -> dict[str, Any]:
        """Extract original user input for presentation prompts (no attachments).

        Used by process_results and process_error to get flow context without
        including input attachments that could be confused with output attachment refs.
        """
        ...
