"""Tool use memory types for memory-centric tool selection.

This module defines the types used for recording and querying tool use memories.
Tool uses are recorded automatically by ActionQueuePoller after action completion.

Types:
    ToolResultStatus: Success/failure status of tool execution
    ToolUseDomain: Domain classification based on process_key
    ToolUseTag: Standard tags for tool use memories
    ToolUseRecord: Structured record for memory storage
"""

from dataclasses import dataclass
from enum import StrEnum


class ToolResultStatus(StrEnum):
    """Status of a tool use execution."""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"


class ToolUseDomain(StrEnum):
    """Domain classification for tool uses.

    Each domain represents a category of functionality.
    Process keys are classified into domains based on prefix patterns.
    """

    FILE_SYSTEM = "file_system"
    NETWORK = "network"
    DATABASE = "database"
    INFERENCE = "inference"
    IO = "io"
    MEMORY = "memory"
    DISCOVERY = "discovery"
    AUDIO = "audio"
    STATE = "state"
    SCHEDULING = "scheduling"
    BLOB_STORAGE = "blob_storage"
    OTHER = "other"


class ToolUseTag(StrEnum):
    """Standard tags for tool use memories.

    These tags enable filtering of tool use memories:
    - TOOL_USE: All tool use memories have this tag
    - SUCCESS/FAILURE: Outcome-based filtering
    - DOMAIN_PREFIX: Combined with domain value for domain-based filtering
    """

    TOOL_USE = "tool_use"
    SUCCESS = "tool_success"
    FAILURE = "tool_failure"
    DOMAIN_PREFIX = "domain"


# Maximum lengths for truncated content
_MAX_ARGS_LENGTH: int = 200
_MAX_RESULT_LENGTH: int = 300


@dataclass(frozen=True, slots=True)
class ToolUseRecord:
    """Structured record of a tool use for memory storage.

    Attributes:
        process_key: Full process key (e.g., service_interface::state_service::read_state)
        arguments_summary: Truncated string representation of arguments
        result_status: SUCCESS, FAILURE, or PARTIAL
        result_summary: Truncated output or error message
        domain: Classified domain based on process_key
        session_id: Session context (may be None)
        flow_id: Flow context (may be None)
    """

    process_key: str
    arguments_summary: str
    result_status: ToolResultStatus
    result_summary: str
    domain: ToolUseDomain
    session_id: str | None
    flow_id: str | None

    def to_memory_content(self) -> str:
        """Format the record for memory storage.

        Returns:
            Formatted string suitable for remember() content parameter.
        """
        return (
            f"Tool: {self.process_key}\n"
            f"Args: {self.arguments_summary}\n"
            f"Status: {self.result_status.value}\n"
            f"Result: {self.result_summary}"
        )

    def get_tags(self) -> list[str]:
        """Generate tags for memory storage.

        Returns:
            List of tags including: tool_use, status tag, domain tag.
        """
        tags = [ToolUseTag.TOOL_USE.value]

        # Add status tag
        if self.result_status == ToolResultStatus.SUCCESS:
            tags.append(ToolUseTag.SUCCESS.value)
        else:
            tags.append(ToolUseTag.FAILURE.value)

        # Add domain tag (e.g., "domain:state")
        tags.append(f"{ToolUseTag.DOMAIN_PREFIX.value}:{self.domain.value}")

        return tags


def create_tool_use_record(
    process_key: str,
    arguments: dict[str, object],
    result_status: ToolResultStatus,
    result_data: object,
    domain: ToolUseDomain,
    session_id: str | None = None,
    flow_id: str | None = None,
) -> ToolUseRecord:
    """Factory function to create a ToolUseRecord with proper truncation.

    Args:
        process_key: Full process key
        arguments: Raw arguments dict (will be truncated)
        result_status: Execution outcome
        result_data: Raw result data (will be truncated)
        domain: Classified domain
        session_id: Optional session context
        flow_id: Optional flow context

    Returns:
        ToolUseRecord with truncated content.
    """
    # Truncate arguments
    args_str = str(arguments)
    if len(args_str) > _MAX_ARGS_LENGTH:
        args_str = args_str[:_MAX_ARGS_LENGTH] + "..."

    # Truncate result
    result_str = str(result_data)
    if len(result_str) > _MAX_RESULT_LENGTH:
        result_str = result_str[:_MAX_RESULT_LENGTH] + "..."

    return ToolUseRecord(
        process_key=process_key,
        arguments_summary=args_str,
        result_status=result_status,
        result_summary=result_str,
        domain=domain,
        session_id=session_id,
        flow_id=flow_id,
    )
