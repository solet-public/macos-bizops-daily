"""Service Interface Process Decorator System.

This module provides the @service_interface_process decorator for marking service
interface methods with complete metadata. This eliminates the need for manual
dict-based registration in SERVICE_INTERFACE_PROCESSES.

All service interface methods MUST be decorated with @service_interface_process
to provide explicit, validated metadata at definition time.

Result/error processor customizations are OPTIONAL since the 2026-07-15
frontier-first relax. When present they are merged with the base templates
from the inference interface (action_definition_template on process_results
and process_error processes) to provide action-specific context. One caveat:
verbs submitted as deterministic_continuation steps need error customizations
PRESENT (even an empty block) — ActionFactory's §16 check refuses the
submission otherwise.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    PromptContract,
    ReturnValueSchema,
)

if TYPE_CHECKING:
    from ananta.core.domain.enums import ProcessorPolicyCategory


# TypeVar for decorated service interface methods
ServiceInterfaceMethod = TypeVar("ServiceInterfaceMethod", bound=Callable[..., object])


@dataclass(frozen=True)
class ServiceInterfaceActionMetadata:
    """Metadata for service interface action methods.

    This metadata is attached to interface methods via the @service_interface_process decorator.
    It contains all information needed to register the service interface process in the registry.

    Result/error processor customizations are optional (2026-07-15 relax);
    when present they define how results/errors are presented to users.
    """

    name: str
    display_name: str
    description: str  # FOR LLM (350-800 chars) - technical explanation for correct usage
    embedding_description: str  # FOR SEARCH (200-400 chars) - keyword-dense for semantic matching
    is_discoverable: bool  # Whether process appears in discovery results (default False for services)
    provider: str  # e.g., "state_service", "inference_service"
    function_name: str
    parameters: dict[str, ParameterMetadata]
    return_value_schema: ReturnValueSchema
    is_inference_capable: bool  # True for inference/GENERATE processes
    processor_policy_category: "ProcessorPolicyCategory | None"
    is_enabled: bool
    is_long_running: bool  # If True, user should be notified before invocation
    version: str
    chaining_guidance: tuple[str, ...]  # Guidance for chaining with other processes
    action_definition_template: (
        dict[str, object] | None
    )  # Complete action definition for VERTEX processes
    work_count_impact: int  # Impact on flow's work_count when action completes. Default +1 (edge), 0 for inference/status/post_message.
    result_processor_customizations: (
        MergeResultProcessorCustomizations | None
    )  # optional since the 2026-07-15 relax
    error_processor_customizations: (
        MergeErrorProcessorCustomizations | None
    )  # optional — §16 requires presence for deterministic_continuation verbs
    requires_result_processor: bool  # Auto-inferred from result_processor_customizations
    prompt_contract: "PromptContract | None"
    # W-VAULT-INTERFACE-EXTEND (P0 Tier 1): when True, ActionProcessor builds
    # a server-side `CallContext` from the queued action's `source_plugin`
    # (and any authenticated_principal injected via state) and passes it as
    # the `call_context` keyword. Caller-supplied `call_context` values are
    # dropped — the field is never inferred from the process key's provider.
    # See workbench/2026-06-07_state_service_consolidation_master_plan.md §3.3.3.
    requires_call_context: bool

    def to_process_dict(self) -> dict[str, object]:
        """Convert metadata to process registry dict format."""
        from ananta.constants import ProviderType

        result: dict[str, object] = {
            "provider_type": ProviderType.SERVICE_INTERFACE.value,
            "provider": self.provider,
            "function_name": self.function_name,
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "embedding_description": self.embedding_description,
            "is_discoverable": self.is_discoverable,
            "parameters": {
                param_name: param.to_dict() for param_name, param in self.parameters.items()
            },
            "return_value_schema": self.return_value_schema.to_dict(),
            "is_inference_capable": self.is_inference_capable,
            "processor_policy_category": (
                self.processor_policy_category.value if self.processor_policy_category else None
            ),
            "is_enabled": self.is_enabled,
            "is_long_running": self.is_long_running,
            "version": self.version,
            "requires_result_processor": self.requires_result_processor,
            "requires_call_context": self.requires_call_context,
        }
        if self.chaining_guidance:
            result["chaining_guidance"] = list(self.chaining_guidance)
        if self.action_definition_template:
            result["action_definition_template"] = self.action_definition_template
        result["work_count_impact"] = self.work_count_impact
        if self.result_processor_customizations:
            result["result_processor_customizations"] = (
                self.result_processor_customizations.to_dict()
            )
        if self.error_processor_customizations:
            result["error_processor_customizations"] = self.error_processor_customizations.to_dict()
        if self.prompt_contract is not None:
            result["prompt_contract"] = self.prompt_contract.to_dict()
        return result


def is_service_interface_process_enabled(
    metadata: ServiceInterfaceActionMetadata | bool,
) -> bool:
    """Return whether a decorated service-interface process is registrable."""
    return metadata if isinstance(metadata, bool) else metadata.is_enabled


def service_interface_process(
    *,
    name: str,
    display_name: str = "",  # Now loaded from knowledge base JSON
    description: str = "",  # Now loaded from knowledge base JSON
    embedding_description: str = "",  # Now loaded from knowledge base JSON
    is_discoverable: bool = False,  # Service methods default to not discoverable
    provider: str,
    parameters: dict[str, ParameterMetadata],
    return_value_schema: ReturnValueSchema,
    is_inference_capable: bool = False,
    processor_policy_category: "ProcessorPolicyCategory | None" = None,
    is_enabled: bool = True,
    is_long_running: bool = False,
    version: str = "1.0.0",
    chaining_guidance: list[str] | None = None,
    action_definition_template: dict[str, object] | None = None,
    work_count_impact: int = 1,
    result_processor_customizations: MergeResultProcessorCustomizations | None = None,
    error_processor_customizations: MergeErrorProcessorCustomizations | None = None,
    requires_result_processor: bool | None = None,
    prompt_contract: PromptContract | None = None,
    requires_call_context: bool = False,
) -> Callable[[ServiceInterfaceMethod], ServiceInterfaceMethod]:
    """Decorator for service interface process methods.

    This decorator marks service interface methods with complete metadata, enabling
    automatic registration in the process registry without manual dict construction.

    Result/error processor customizations are OPTIONAL (2026-07-15 relax). When
    present they are merged with base templates from the inference interface to
    provide action-specific context for result/error handling; error
    customizations must be PRESENT on verbs driven as deterministic_continuation
    steps (§16).

    Args:
        name: Action name (e.g., "read_state")
        display_name: Human-readable name (e.g., "Read State")
        description: Complete description of what this action does
        provider: Service provider name (e.g., "state_service")
        parameters: Dict of parameter name -> ParameterMetadata
        return_value_schema: Schema describing return value structure
        is_inference_capable: True for inference/GENERATE processes (default False)
        processor_policy_category: Optional ProcessorPolicyCategory
        is_enabled: Whether this action is enabled (default True)
        version: Semantic version string (default "1.0.0")
        chaining_guidance: List of guidance strings for chaining with other processes
        action_definition_template: Complete action definition template for VERTEX processes.
            When an edge completes, it can fetch this template from the registry and
            substitute result values to create the next action.
        work_count_impact: Impact on flow's work_count when action completes. Default +1
            (edge creates work). Use 0 for inference/status/post_message (neutral).
        result_processor_customizations: Optional customizations merged into the result
            processor template (action_definition_template from process_results).
        error_processor_customizations: Optional customizations merged into the error
            processor template (action_definition_template from process_error); keep at
            least an empty block on deterministic_continuation verbs (§16).

    Returns:
        Decorated method with _service_interface_metadata attribute

    Example:
        @service_interface_process(
            name="read_state",
            display_name="Read State",
            description="Read data from state management system",
            provider="state_service",
            parameters={
                "namespace": ParameterMetadata(
                    description="Database namespace",
                    required=True,
                    type=ParameterType.STRING
                ),
            },
            return_value_schema=ReturnValueSchema(...),
            result_processor_customizations=MergeResultProcessorCustomizations(
                action_label="Read State",
                result_type="query_results",
                result_description="Records retrieved from the database",
                presentation_guidance="",
            ),
            error_processor_customizations=MergeErrorProcessorCustomizations(
                action_context="Reading records from state database",
                error_interpretation="QueryError: invalid query structure",
                recovery_guidance="Verify query format and retry",
            ),
        )
        @abstractmethod
        def read_state(self, namespace: str, query: dict) -> ActionResult:
            pass
    """

    def decorator(func: ServiceInterfaceMethod) -> ServiceInterfaceMethod:
        """Attach metadata to the service interface method."""
        # Auto-infer requires_result_processor from customizations ONLY when
        # the caller did not pass an explicit value (None sentinel).
        # Explicit False is respected even when result_processor_customizations is present.
        effective_requires_result_processor: bool
        if requires_result_processor is not None:
            effective_requires_result_processor = requires_result_processor
        elif result_processor_customizations is not None:
            effective_requires_result_processor = True
        else:
            effective_requires_result_processor = False

        metadata = ServiceInterfaceActionMetadata(
            name=name,
            display_name=display_name,
            description=description,
            embedding_description=embedding_description,
            is_discoverable=is_discoverable,
            provider=provider,
            function_name=func.__name__,
            parameters=parameters,
            return_value_schema=return_value_schema,
            is_inference_capable=is_inference_capable,
            processor_policy_category=processor_policy_category,
            is_enabled=is_enabled,
            is_long_running=is_long_running,
            version=version,
            chaining_guidance=tuple(chaining_guidance) if chaining_guidance else (),
            action_definition_template=action_definition_template,
            work_count_impact=work_count_impact,
            result_processor_customizations=result_processor_customizations,
            error_processor_customizations=error_processor_customizations,
            requires_result_processor=effective_requires_result_processor,
            prompt_contract=prompt_contract,
            requires_call_context=requires_call_context,
        )

        # Attach metadata to function
        func._service_interface_metadata = metadata  # type: ignore[attr-defined]

        return func

    return decorator
