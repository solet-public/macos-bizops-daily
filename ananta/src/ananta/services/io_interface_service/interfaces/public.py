"""IO Interface Service Public API.

AI-discoverable IO routing operations with @service_interface_process decorators.
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
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process


class IOInterfaceServiceAPI(ABC):
    """Public IO interface operations - AI-discoverable via process registry.

    This interface defines IO routing operations that can be discovered and
    invoked by the AI orchestration system and action templates.
    """

    @service_interface_process(
        name="post_message",
        provider="io_interface_service",
        is_enabled=False,  # Model addresses IO plugins directly (plugin::<ns>::post_message)
        # EDGE_SINK: Terminal — IO delivery ends the flow, no result processor
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "session_id": ParameterMetadata(
                description="Session ID to route the message to",
                required=True,
                type=ParameterType.STRING,
            ),
            "message": ParameterMetadata(
                description="Message content to send to the client",
                required=True,
                type=ParameterType.STRING,
            ),
            "attachments": ParameterMetadata(
                description=(
                    "List of attachment names to deliver with the message. "
                    "Use names from available_attachments. Empty list if no files to attach."
                ),
                required=False,
                type=ParameterType.LIST,
            ),
            "job_result_ref": ParameterMetadata(
                description=(
                    "Identifier for a completed async job whose artifacts should "
                    "be included automatically when attachments are omitted. "
                    "Typically this is the AsyncJobManager job_id."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Result of posting message to the IO interface",
            type=ParameterType.OBJECT,
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status of the action (completed, error)",
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Response data from the IO interface plugin",
                ),
                "error": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Error message if action failed",
                ),
            },
        ),
        work_count_impact=0,  # Status messages don't affect flow completion
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    @abstractmethod
    def post_message(
        self,
        session_id: str,
        message: str,
        attachments: list[dict[str, object]] | None = None,
        job_result_ref: str | None = None,
    ) -> dict[str, Any]:
        """Send status message without affecting flow completion.

        Args:
            session_id: Session ID determining which interface to route to
            message: Message content to send

        Returns:
            ActionResult dict from the underlying IO interface plugin
        """
        pass

    @service_interface_process(
        name="deliver_artifact",
        provider="io_interface_service",
        is_enabled=False,  # Internal-only — model addresses IO plugins directly
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "session_id": ParameterMetadata(
                description="Session ID to route the artifact to",
                required=True,
                type=ParameterType.STRING,
            ),
            "job_result_ref": ParameterMetadata(
                description="Reference to the async job containing the artifact(s) to deliver",
                required=True,
                type=ParameterType.STRING,
            ),
            "message": ParameterMetadata(
                description="Optional accompanying text message",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Result of delivering artifact to the IO interface",
            type=ParameterType.OBJECT,
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status of the action (completed, error)",
                ),
                "delivery_mode": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="How artifact was delivered: 'file_upload' or 'text_reference'",
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Response data from the IO interface plugin",
                ),
                "error": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Error message if action failed",
                ),
            },
        ),
        work_count_impact=0,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    @abstractmethod
    def deliver_artifact(
        self,
        session_id: str,
        job_result_ref: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Deliver artifact with capability awareness.

        Args:
            session_id: Session ID determining which interface to route to
            job_result_ref: Reference to async job containing artifact data
            message: Optional accompanying text message

        Returns:
            ActionResult dict with delivery_mode indicating how it was delivered
        """
        pass
