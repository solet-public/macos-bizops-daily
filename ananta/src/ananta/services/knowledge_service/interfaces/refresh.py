"""Process-registry refresh service-interface verbs (W5.Q decomposition).

Reload one or all process JSON definitions from a plugin's knowledge
base and merge the changes into the live process registry without a
restart. Lifted byte-for-byte from the W5.Q-pre-decomposition
``KnowledgeServiceAPI``.
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process


class KnowledgeRefreshAPI(ABC):
    """Process-registry refresh verbs — refresh_plugin_processes / refresh_plugin_process."""

    @service_interface_process(
        name="refresh_plugin_processes",
        provider="knowledge_service",
        is_discoverable=True,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "plugin_name": ParameterMetadata(
                description="Plugin name whose processes to refresh (use 'ananta' for platform services)",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Refresh result with updated process count",
            type=ParameterType.OBJECT,
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "'success' when every merge applied cleanly, 'partial' when some "
                        "reported errors"
                    ),
                ),
                "plugin_name": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Plugin whose knowledge base the process JSONs were read from",
                ),
                "updated_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of processes updated",
                ),
                "process_keys": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "List of updated process keys. Deliberately PLURAL: at the top level "
                        "of a result envelope the singular 'process_key' names the verb that "
                        "PRODUCED the result, so a singular spelling here would collide with "
                        "the result-contract invariant"
                    ),
                ),
                "errors": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Errors reported by the registry merge; empty on a clean refresh",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
    )
    @abstractmethod
    def refresh_plugin_processes(self, plugin_name: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="refresh_plugin_process",
        provider="knowledge_service",
        is_discoverable=True,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "plugin_name": ParameterMetadata(
                description="Plugin name owning the process (use 'ananta' for platform services)",
                required=True,
                type=ParameterType.STRING,
            ),
            "process_key": ParameterMetadata(
                description="The specific process key to refresh",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Refresh result for single process",
            type=ParameterType.OBJECT,
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "'success' when the merge applied cleanly, 'error' when it reported "
                        "errors"
                    ),
                ),
                "plugin_name": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Plugin whose knowledge base the process JSON was read from",
                ),
                "refreshed_process_key": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "The process key that was refreshed — an echo of the request's "
                        "process_key argument. Deliberately NOT named 'process_key': at the "
                        "top level of a result envelope that name is reserved for the key of "
                        "the verb that PRODUCED the result, and the collision made every call "
                        "raise RESULT_CONTRACT_VIOLATION after the side-effect had landed"
                    ),
                ),
                "updated": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether the process was successfully updated",
                ),
                "errors": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Errors reported by the registry merge; empty on a clean refresh",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
    )
    @abstractmethod
    def refresh_plugin_process(
        self, plugin_name: str, process_key: str,
    ) -> dict[str, Any]: ...
