from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Optional, TypeVar

if TYPE_CHECKING:
    from ananta.core.domain.enums import ProcessorPolicyCategory


# TypeVar for decorated functions - bound to Callable to allow proper attribute assignment
F = TypeVar("F", bound=Callable[..., object])


class ParameterType(Enum):
    STRING = "string"
    INTEGER = "int"
    FLOAT = "float"
    BOOLEAN = "bool"
    DICT = "dict"
    LIST = "list"
    OBJECT = "object"


class ActionAvailability(Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"


_PROMPT_CONTRACT_KINDS = frozenset({"standard", "delegated_artifact_creation"})


@dataclass(frozen=True)
class PromptContract:
    """Declares how the inference pipeline should handle this process.

    Closed vocabulary:
    - ``standard`` — default; normal prompt construction.
    - ``delegated_artifact_creation`` — the main model emits a directive;
      artifact authoring is delegated to a separate thinking context.
    """

    kind: str = "standard"

    def __post_init__(self) -> None:
        if self.kind not in _PROMPT_CONTRACT_KINDS:
            msg = (
                f"Invalid prompt_contract kind {self.kind!r}; "
                f"must be one of {sorted(_PROMPT_CONTRACT_KINDS)}"
            )
            raise ValueError(msg)

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind}


@dataclass
class ReturnValueSchema:
    type: ParameterType
    description: str
    properties: dict[str, "ParameterMetadata"] = field(default_factory=dict)
    examples: list[object] = field(default_factory=list)
    error_conditions: dict[str, str] = field(default_factory=dict)
    usage_patterns: list[str] = field(default_factory=list)
    chain_compatible_processes: list[str] = field(default_factory=list)
    common_transformations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": self.type.value,
            "description": self.description,
            "properties": {name: param.to_dict() for name, param in self.properties.items()},
            "examples": self.examples,
            "error_conditions": self.error_conditions,
            "usage_patterns": self.usage_patterns,
            "chain_compatible_processes": self.chain_compatible_processes,
            "common_transformations": self.common_transformations,
        }


@dataclass
class WorkflowStep:
    process_key: str
    purpose: str
    input_mapping: dict[str, str]
    output_usage: str
    required: bool = True
    alternatives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "process_key": self.process_key,
            "purpose": self.purpose,
            "input_mapping": self.input_mapping,
            "output_usage": self.output_usage,
            "required": self.required,
            "alternatives": self.alternatives,
        }


@dataclass
class WorkflowPattern:
    """Workflow pattern definition. Note: Currently unused - no instances are created."""

    name: str
    description: str
    steps: list[WorkflowStep]
    use_cases: list[str] = field(default_factory=list)
    success_indicators: list[str] = field(default_factory=list)
    failure_patterns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "steps": [step.to_dict() for step in self.steps],
            "use_cases": self.use_cases,
            "success_indicators": self.success_indicators,
            "failure_patterns": self.failure_patterns,
        }


@dataclass
class ParameterMetadata:
    type: ParameterType
    required: bool = False
    description: str = ""
    default: object | None = None
    validation: dict[str, object] | None = None
    examples: list[object] = field(default_factory=list)
    ai_hints: list[str] = field(default_factory=list)  # Specific guidance for AI
    format_hint: str | None = None
    # Data sensitivity annotation (0.0 = public, 1.0 = restricted). The
    # exposure-filter consumer was removed 2026-07-15 (frontier-first);
    # retained as descriptive metadata.
    data_sensitivity: float = 0.0

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "type": self.type.value,
            "required": self.required,
            "description": self.description,
        }
        if self.default is not None:
            result["default"] = self.default
        if self.validation:
            result["validation"] = self.validation
        if self.examples:
            result["examples"] = self.examples
        if self.ai_hints:
            result["ai_hints"] = self.ai_hints
        if self.format_hint:
            result["format_hint"] = self.format_hint
        # Always include data_sensitivity to allow explicit public (0.0) marking
        # This enables distinguishing "explicitly public" from "not set"
        result["data_sensitivity"] = self.data_sensitivity
        return result


@dataclass
class ActionExample:
    description: str
    parameters: dict[str, object]
    expected_output: str
    scenario: str = ""  # When/why to use this example
    success_indicators: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "description": self.description,
            "parameters": self.parameters,
            "expected_output": self.expected_output,
        }
        if self.scenario:
            result["scenario"] = self.scenario
        if self.success_indicators:
            result["success_indicators"] = self.success_indicators
        return result


@dataclass
class UsageGuidance:
    """Complete usage guidance for a process."""

    when_to_use: list[str] = field(default_factory=list)
    when_not_to_use: list[str] = field(default_factory=list)
    best_practices: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "best_practices": self.best_practices,
        }


@dataclass
class ContextHandling:
    """Documentation for how a process handles context and session state.

    Use predefined class constants for common patterns:
        - ContextHandling.NONE: Stateless action with no context requirements
        - ContextHandling.SESSION_AWARE: Action that uses session_id for routing
        - ContextHandling.CONVERSATION_AWARE: Action that tracks conversation history

    For custom documentation, instantiate with explicit parameters:
        ContextHandling(
            session_awareness="Description of session behavior",
            conversation_history="Description of history handling",
            context_passing="Description of context flow",
        )
    """

    # Class-level constants (defined after class body)
    NONE: ClassVar["ContextHandling"]
    SESSION_AWARE: ClassVar["ContextHandling"]
    CONVERSATION_AWARE: ClassVar["ContextHandling"]

    session_awareness: str = ""
    conversation_history: str = ""
    context_passing: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "session_awareness": self.session_awareness,
            "conversation_history": self.conversation_history,
            "context_passing": self.context_passing,
        }


# Predefined constants for common context handling patterns
# These MUST be defined after the class to avoid forward reference issues
ContextHandling.NONE = ContextHandling(
    session_awareness="Stateless - no session tracking required",
    conversation_history="Does not use or maintain conversation history",
    context_passing="No context dependencies or outputs",
)

ContextHandling.SESSION_AWARE = ContextHandling(
    session_awareness="Session-aware - uses session_id for message routing",
    conversation_history="May access conversation history via session context",
    context_passing="Routes responses to the originating session",
)

ContextHandling.CONVERSATION_AWARE = ContextHandling(
    session_awareness="Session-aware with full conversation tracking",
    conversation_history="Maintains and references conversation history for continuity",
    context_passing="Preserves context across conversation turns",
)


@dataclass
class InvocationExample:
    """Complete example showing invocation syntax and expected response."""

    description: str
    invocation: dict[str, object]
    response: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "description": self.description,
            "invocation": self.invocation,
            "response": self.response,
        }


@dataclass
class ErrorCase:
    """Documentation for a specific error condition."""

    condition: str
    error_response: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "condition": self.condition,
            "error_response": self.error_response,
        }


@dataclass
class TypicalWorkflow:
    """Documentation for a complete workflow using this process."""

    scenario: str
    steps: list[str]
    example: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "steps": self.steps,
            "example": self.example,
        }


@dataclass(frozen=True)
class MergeResultProcessorCustomizations:
    """Customizations merged into result processor template.

    OPTIONAL on every category since the 2026-07-15 frontier-first relax.
    When present, these are merged into the base result processor template
    to give the LLM action-specific context for presenting results.
    """

    # Text fields — now loaded from knowledge base JSON, not set in decorators
    action_label: str = ""  # e.g., "Report generated"
    result_type: str = ""  # e.g., "audio_generated", "memory_stored", "data_retrieved"
    result_description: str = ""  # e.g., "Audio blob with keys: blob_key, duration, format"
    presentation_guidance: str = ""
    # e.g., "Present blob_key, duration, format. Include playback commands:
    #        macOS: afplay <path>, Windows: start <path>"

    # Fields to hide from LLM context (structural filtering)
    # These fields are removed before the result reaches the LLM
    # Use this instead of "DO NOT show" in presentation_guidance
    hidden_fields: tuple[str, ...] = ()  # e.g., ("internal_id", "secret_value")

    # Schema table name linking result fields to schema_registry column
    # names. Descriptive metadata since the 2026-07-15 exposure-filter removal.
    table_name: str | None = None

    # Output action guidance - what should the LLM do with the result?
    # Keep general — do NOT mention specific process names (overfitting).
    # The model already knows about post_message from SYSTEM_PROMPT_PROCESS_KEYS.
    output_action_guidance: str = (
        "Determine the appropriate next action based on the context and action result."
    )

    # Optional output schema override - enforces structural constraints on LLM response
    # Use this when the default schema (allowing multiple actions) is too permissive
    # Example: discovery sets maxItems=1 to ensure exactly one action is returned
    output_schema: dict[str, object] | None = None

    # Blob field mapping for attachment extraction
    # Maps attachment fields to result data fields
    # e.g., {"blob_id": "audio_blob_key", "namespace": LiteralValue("audio_plugin"), ...}
    # Use LiteralValue(val) for constant values, bare strings for field lookups
    blob_fields: dict[str, object] | None = None

    # For processes that produce multiple attachments of the same type
    # List of blob_fields mappings, one per attachment
    blob_fields_list: list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        # Validate table_name if provided
        if self.table_name is not None and not self.table_name:
            raise ValueError("table_name must be a non-empty string")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "action_label": self.action_label,
            "result_type": self.result_type,
            "result_description": self.result_description,
            "presentation_guidance": self.presentation_guidance,
            "output_action_guidance": self.output_action_guidance,
        }
        if self.hidden_fields:
            result["hidden_fields"] = list(self.hidden_fields)
        if self.table_name:
            result["table_name"] = self.table_name
        if self.output_schema is not None:
            result["output_schema"] = self.output_schema
        if self.blob_fields is not None:
            result["blob_fields"] = self._serialize_blob_fields(self.blob_fields)
        if self.blob_fields_list is not None:
            result["blob_fields_list"] = [
                self._serialize_blob_fields(bf) for bf in self.blob_fields_list
            ]
        return result

    def _serialize_blob_fields(self, blob_fields: dict[str, object]) -> dict[str, object]:
        """Serialize blob_fields dict, converting LiteralValue to JSON-compatible format."""
        from ananta.core.tracking.blob_field_types import LiteralValue

        serialized: dict[str, object] = {}
        for key, value in blob_fields.items():
            if isinstance(value, LiteralValue):
                # Serialize as {"__literal__": value} for deserialization
                serialized[key] = {"__literal__": value.value}
            else:
                serialized[key] = value
        return serialized


@dataclass(frozen=True)
class MergeErrorProcessorCustomizations:
    """Customizations merged into error processor template.

    OPTIONAL at registration since the 2026-07-15 frontier-first relax —
    BUT presence is load-bearing for any verb submitted as a
    ``deterministic_continuation``: ActionFactory's §16 check
    (``_require_error_processor_for_deterministic``) refuses the submission
    when the registry entry carries no error customizations (an empty block
    serializes truthy and satisfies it). Keep at least an empty block on
    verbs driven by joseki/WBS deterministic steps.
    """

    # Text fields — now loaded from knowledge base JSON, not set in decorators
    action_context: str = ""
    error_interpretation: str = ""
    recovery_guidance: str = ""

    # Structural fields — set in decorators, stay in code
    retryable: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "action_context": self.action_context,
            "error_interpretation": self.error_interpretation,
            "recovery_guidance": self.recovery_guidance,
            "retryable": self.retryable,
        }


@dataclass
class ActionMetadata:
    name: str
    display_name: str
    description: str  # FOR LLM (350-800 chars) - technical explanation for correct usage
    plugin: str
    function: str
    embedding_description: str = ""  # FOR SEARCH (200-400 chars) - keyword-dense for semantic matching
    is_discoverable: bool = True  # Whether process appears in discovery results
    parameters: dict[str, ParameterMetadata] = field(default_factory=dict)
    output_type: str = "object"
    output_description: str = ""
    examples: list[ActionExample] = field(default_factory=list)
    workflow_patterns: list[WorkflowPattern] = field(default_factory=list)
    ai_guidance: Optional["AIGuidance"] = None
    return_value_schema: ReturnValueSchema | None = None
    default_result_processor: dict[str, object] | None = (
        None  # Default template for user-facing result presentation
    )
    # Customizations for result/error processing - merged into inference VERTEX's action_definition_template at runtime
    result_processor_customizations: MergeResultProcessorCustomizations | None = (
        None  # optional since the 2026-07-15 relax
    )
    error_processor_customizations: MergeErrorProcessorCustomizations | None = (
        None  # optional — but §16 requires presence for deterministic_continuation verbs
    )
    requires_result_processor: bool = (
        False  # If True, a result_processor must be attached to the action
    )
    processor_policy_category: Optional["ProcessorPolicyCategory"] = (
        None  # Category for processor policy enforcement
    )
    # Complete action definition template for VERTEX processes (e.g., process_results, process_error)
    # When an EDGE completes, this template is fetched and customized with result/error data
    action_definition_template: dict[str, object] | None = None
    prerequisites: list[str] = field(default_factory=list)
    is_inference_capable: bool = False  # True for inference/GENERATE processes
    is_async: bool = False
    is_long_running: bool = False  # If True, user should be notified before invocation
    estimated_duration: str | None = None
    version: str = "1.0.0"
    chaining_guidance: list[str] = field(default_factory=list)
    runtime_validation: bool = False
    # New comprehensive documentation fields
    summary: str = ""  # One-line summary
    usage: UsageGuidance | None = None
    context_handling: ContextHandling | None = None
    typical_workflows: list[TypicalWorkflow] = field(default_factory=list)
    complete_examples: list[InvocationExample] = field(default_factory=list)
    error_cases: list[ErrorCase] = field(default_factory=list)
    # Flow work count tracking: +1 = creates work needing response (default for edges), 0 = neutral (inference, status, post_message)
    work_count_impact: int = 1

    def to_dict(self) -> dict[str, object]:
        result = self._build_base_dict()
        self._add_optional_fields(result)
        result["work_count_impact"] = self.work_count_impact
        return result

    def _build_base_dict(self) -> dict[str, object]:
        """Build base dictionary with required fields."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "embedding_description": self.embedding_description,
            "is_discoverable": self.is_discoverable,
            "plugin": self.plugin,
            "function": self.function,
            "parameters": {name: param.to_dict() for name, param in self.parameters.items()},
            "output_type": self.output_type,
            "output_description": self.output_description,
            "examples": [example.to_dict() for example in self.examples],
            "workflow_patterns": [pattern.to_dict() for pattern in self.workflow_patterns],
            "prerequisites": self.prerequisites,
            "is_inference_capable": self.is_inference_capable,
            "is_async": self.is_async,
            "is_long_running": self.is_long_running,
            "estimated_duration": self.estimated_duration,
            "version": self.version,
            "chaining_guidance": self.chaining_guidance,
            "runtime_validation": self.runtime_validation,
            "requires_result_processor": self.requires_result_processor,
        }

    def _add_optional_fields(self, result: dict[str, object]) -> None:
        """Add optional fields if present."""
        self._add_processor_fields(result)
        self._add_template_fields(result)
        self._add_documentation_fields(result)

    def _add_processor_fields(self, result: dict[str, object]) -> None:
        """Add processor-related optional fields."""
        if self.return_value_schema:
            result["return_value_schema"] = self.return_value_schema.to_dict()
        if self.default_result_processor:
            result["default_result_processor"] = self.default_result_processor
        if self.result_processor_customizations:
            result["result_processor_customizations"] = (
                self.result_processor_customizations.to_dict()
            )
        if self.error_processor_customizations:
            result["error_processor_customizations"] = self.error_processor_customizations.to_dict()
        if self.processor_policy_category:
            result["processor_policy_category"] = self.processor_policy_category.value

    def _add_template_fields(self, result: dict[str, object]) -> None:
        """Add template-related optional fields."""
        if self.action_definition_template:
            result["action_definition_template"] = self.action_definition_template
        if self.ai_guidance:
            result["ai_guidance"] = self.ai_guidance

    def _add_documentation_fields(self, result: dict[str, object]) -> None:
        """Add documentation-related optional fields."""
        if self.summary:
            result["summary"] = self.summary
        if self.usage:
            result["usage"] = self.usage.to_dict()
        if self.context_handling:
            result["context_handling"] = self.context_handling.to_dict()
        if self.typical_workflows:
            result["typical_workflows"] = [w.to_dict() for w in self.typical_workflows]
        if self.complete_examples:
            result["complete_examples"] = [e.to_dict() for e in self.complete_examples]
        if self.error_cases:
            result["error_cases"] = [e.to_dict() for e in self.error_cases]

    def to_planning_dict(self) -> dict[str, object]:
        """Return only metadata needed for action planning (Context 1).

        Used when constructing actions - needs to know HOW to invoke and WHEN to use.
        Does NOT need error handling or response schemas.
        """
        parameters_schema = self._build_parameters_schema()
        result = self._build_planning_base_dict(parameters_schema)
        self._add_planning_optional_fields(result)
        return result

    def _build_parameters_schema(self) -> dict[str, object]:
        """Build parameter schema in proper JSON Schema format."""
        properties: dict[str, object] = {}
        for name, param in self.parameters.items():
            properties[name] = self._build_parameter_property(param)

        return {
            "type": "object",
            "required": [name for name, param in self.parameters.items() if param.required],
            "properties": properties,
        }

    def _build_parameter_property(self, param: ParameterMetadata) -> dict[str, object]:
        """Build a single parameter property dictionary."""
        prop: dict[str, object] = {
            "type": param.type.value,
            "description": param.description,
        }
        if param.default is not None:
            prop["default"] = param.default
        if param.validation:
            prop.update(param.validation)
        if param.examples:
            prop["examples"] = param.examples
        if param.ai_hints:
            prop["ai_hints"] = param.ai_hints
        return prop

    def _build_planning_base_dict(self, parameters_schema: dict[str, object]) -> dict[str, object]:
        """Build base dictionary for planning context."""
        return {
            "process_key": f"plugin::{self.plugin}::{self.name}" if self.plugin else self.name,
            "summary": self.summary,
            "description": self.description,
            "parameters": parameters_schema,
            "is_inference_capable": self.is_inference_capable,
        }

    def _add_planning_optional_fields(self, result: dict[str, object]) -> None:
        """Add optional fields for planning context."""
        if self.usage:
            result["usage"] = self.usage.to_dict()
        if self.context_handling:
            result["context_handling"] = self.context_handling.to_dict()
        if self.typical_workflows:
            result["typical_workflows"] = [wf.to_dict() for wf in self.typical_workflows]

    def to_error_handling_dict(self) -> dict[str, object]:
        """Return only metadata needed for error response handling (Context 2).

        Used when a process returns an error - needs to know what went wrong
        and how to recover or inform the user.
        """
        result: dict[str, object] = {
            "process_key": f"plugin::{self.plugin}::{self.name}" if self.plugin else self.name,
            "summary": self.summary,
        }

        if self.error_cases:
            result["error_cases"] = [error.to_dict() for error in self.error_cases]

        # Include return schema error_conditions if available
        if self.return_value_schema and self.return_value_schema.error_conditions:
            result["error_conditions"] = self.return_value_schema.error_conditions

        return result

    def to_response_handling_dict(self) -> dict[str, object]:
        """Return only metadata needed for success response handling (Context 3).

        Used when a process succeeds - needs to know what fields are in the response,
        how to use them, and what to do next.
        """
        result: dict[str, object] = {
            "process_key": f"plugin::{self.plugin}::{self.name}" if self.plugin else self.name,
            "summary": self.summary,
        }

        if self.return_value_schema:
            result["return_value_schema"] = self.return_value_schema.to_dict()

        if self.chaining_guidance:
            result["chaining_guidance"] = self.chaining_guidance

        # Include chain-compatible processes from return schema if available
        if self.return_value_schema and self.return_value_schema.chain_compatible_processes:
            result["chain_compatible_processes"] = (
                self.return_value_schema.chain_compatible_processes
            )

        # Include usage patterns from return schema if available
        if self.return_value_schema and self.return_value_schema.usage_patterns:
            result["usage_patterns"] = self.return_value_schema.usage_patterns

        return result


@dataclass
class AIGuidance:
    pass


def platform_process(
    name: str,
    display_name: str = "",  # Now loaded from knowledge base JSON
    description: str = "",  # Now loaded from knowledge base JSON
    embedding_description: str = "",  # Now loaded from knowledge base JSON
    is_discoverable: bool = True,  # Whether process appears in discovery results
    parameters: dict[str, ParameterMetadata] | None = None,
    output_type: str = "object",
    output_description: str = "Action execution result",
    examples: list[ActionExample] | None = None,
    prerequisites: list[str] | None = None,
    is_inference_capable: bool = False,
    ai_guidance: AIGuidance | None = None,
    is_async: bool = False,
    is_long_running: bool = False,
    estimated_duration: str | None = None,
    version: str = "1.0.0",
    return_value_schema: ReturnValueSchema | None = None,
    default_result_processor: dict[str, object] | None = None,
    # Customizations for result/error processing - merged into inference VERTEX's action_definition_template
    result_processor_customizations: MergeResultProcessorCustomizations | None = None,
    error_processor_customizations: MergeErrorProcessorCustomizations | None = None,
    requires_result_processor: bool | None = None,
    processor_policy_category: Optional["ProcessorPolicyCategory"] = None,
    # Complete action definition template for VERTEX processes (e.g., process_results, process_error)
    action_definition_template: dict[str, object] | None = None,
    workflow_patterns: list[WorkflowPattern] | None = None,
    chaining_guidance: list[str] | None = None,
    runtime_validation: bool = False,
    # New comprehensive documentation parameters
    summary: str = "",
    usage: UsageGuidance | None = None,
    context_handling: ContextHandling | None = None,
    typical_workflows: list[TypicalWorkflow] | None = None,
    complete_examples: list[InvocationExample] | None = None,
    error_cases: list[ErrorCase] | None = None,
    work_count_impact: int = 1,
) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        # Auto-infer requires_result_processor from customizations ONLY when
        # the caller did not pass an explicit value (None sentinel).
        # Explicit False (e.g., post_message) is respected even when
        # result_processor_customizations is present.
        effective_requires_result_processor: bool
        if requires_result_processor is not None:
            effective_requires_result_processor = requires_result_processor
        elif result_processor_customizations is not None:
            effective_requires_result_processor = True
        else:
            effective_requires_result_processor = False

        # Dynamically add metadata attribute to function
        metadata = ActionMetadata(
            name=name,
            display_name=display_name,
            description=description,
            plugin="",
            function=func.__name__,
            embedding_description=embedding_description,
            is_discoverable=is_discoverable,
            parameters=parameters or {},
            output_type=output_type,
            output_description=output_description,
            examples=examples or [],
            prerequisites=prerequisites or [],
            is_inference_capable=is_inference_capable,
            ai_guidance=ai_guidance,
            is_async=is_async,
            is_long_running=is_long_running,
            estimated_duration=estimated_duration,
            version=version,
            return_value_schema=return_value_schema,
            default_result_processor=default_result_processor,
            result_processor_customizations=result_processor_customizations,
            error_processor_customizations=error_processor_customizations,
            requires_result_processor=effective_requires_result_processor,
            processor_policy_category=processor_policy_category,
            action_definition_template=action_definition_template,
            workflow_patterns=workflow_patterns or [],
            chaining_guidance=chaining_guidance or [],
            runtime_validation=runtime_validation,
            summary=summary,
            usage=usage,
            context_handling=context_handling,
            typical_workflows=typical_workflows or [],
            complete_examples=complete_examples or [],
            error_cases=error_cases or [],
            work_count_impact=work_count_impact,
        )
        func._platform_process_metadata = metadata  # type: ignore[attr-defined]
        return func

    return decorator
