"""EdgeProcessProvider - Protocol for plugins that provide edge processes.

Edge processes are actions whose results feed back to inference (VERTEX).
They require result/error processor customizations to tell the inference
how to present results or handle errors.

ARCHITECTURE:
- Process metadata (name, description, parameters) comes from @platform_process decorator
- EdgeProcessDefinition provides ONLY the customizations for result/error handling
- This separation avoids duplication: describe the process once in @platform_process

Usage:
    class Plugin(ServicePlugin, EdgeProcessProvider):
        def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
            return {
                "write_file": EdgeProcessDefinition(
                    name="write_file",  # Must match @platform_process name
                    result_processor_template_customizations=MergeResultProcessorCustomizations(
                        action_label="File written",
                        result_type="file_written",
                        result_description="File was written to disk",
                    ),
                    error_processor_template_customizations=MergeErrorProcessorCustomizations(
                        action_context="Writes content to a file",
                        error_interpretation="IOError: Disk full or permission denied",
                        recovery_guidance="Check disk space and permissions",
                    ),
                ),
            }
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
)


@dataclass(frozen=True)
class EdgeProcessDefinition:
    """Customizations for an EDGE process.

    EdgeProcessDefinition provides the result/error processor customizations
    that tell inference how to present results and handle errors. The actual
    process metadata (description, parameters, etc.) comes from @platform_process.

    This is a VALIDATION structure - it ensures EDGE processes have required
    customizations. The customizations are used at runtime for result processing.

    Note: The name must match the corresponding @platform_process name.
    """

    # Name - must match @platform_process name for validation
    name: str

    # Result/error processor customizations
    # result_processor is optional — EDGE_SINK processes (post_message, upsert_plan) don't need one
    result_processor_template_customizations: MergeResultProcessorCustomizations | None = None
    error_processor_template_customizations: MergeErrorProcessorCustomizations | None = None


@runtime_checkable
class EdgeProcessProvider(Protocol):
    """Protocol for plugins that surface edge processes.

    Edge processes are actions in the EDGE processor policy category.
    Their results feed back to inference (VERTEX) for presentation.

    Plugins implementing this protocol declare their edge processes
    with the required customizations upfront, enabling:
    - Type-safe registration
    - Clear documentation of what's required
    - Consistent behavior across all edge plugins

    Example:
        class MyPlugin(ServicePlugin, EdgeProcessProvider):
            def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
                return {
                    "my_action": EdgeProcessDefinition(
                        name="my_action",  # Must match @platform_process name
                        result_processor_template_customizations=MergeResultProcessorCustomizations(
                            action_label="Action completed",
                            result_type="my_result",
                            result_description="The result of my action",
                        ),
                        error_processor_template_customizations=MergeErrorProcessorCustomizations(
                            action_context="Performs my action",
                            error_interpretation="Common errors and their meanings",
                            recovery_guidance="How to recover from errors",
                            retryable=True,
                        ),
                    ),
                }
    """

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """Return all edge process definitions this plugin provides.

        Returns:
            Dictionary mapping process names to their EdgeProcessDefinition.
            Keys should match the @platform_process name parameter.
        """
        ...
