"""Prompt assembly service contract — typed request/result for pipeline execution.

Promotes prompt assembly from an internal plugin helper to a first-class
inference service capability.  Callers (inference plugin, thinking plugin)
build a ``PromptAssemblyRequest`` with identity fields and raw parameters;
the service runs the pipeline through a named profile and returns a
``PromptAssemblyResult`` with serialized messages, semantic blocks, and
an output schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class WorkProductLookup(Protocol):
    """Narrow protocol for work-product resolution during prompt assembly.

    Keeps work-product provenance ownership outside the inference
    service contract.  The concrete ``WorkProductRegister`` lives in
    ``ananta.core.plans.work_products``.
    """

    def lookup_by_step_and_slot(
        self, step_number: int, output_slot: str,
    ) -> Any: ...

    def lookup_composed_sources(self, ref: Any) -> Any: ...


@dataclass(frozen=True)
class SerializationSpec:
    """Model/provider-specific serialization constraints.

    Parameterizes the serializer so adding a new model with different
    API constraints means adding a new spec, not changing stage code.
    """

    supports_multiple_system_messages: bool
    requires_role_alternation: bool
    system_message_position: str  # "first_only" | "anywhere"
    supports_assistant_prefill: bool


@dataclass(frozen=True)
class PromptAssemblyRequest:
    """Input contract for prompt assembly.

    Includes all identity and parameter fields that ``PromptContext``
    requires, because the assembly service constructs a valid
    ``PromptContext`` internally.
    """

    profile_name: str  # "inference", "thinking", "text_completion"

    # Pipeline identity — required by PromptContext
    flow_id: str
    action_name: str
    session_id: str
    context_id: str | None = None

    # Action parameters — raw, pre-template-resolution.
    # TemplateStage resolves these into resolved_action_params on PromptContext.
    raw_action_params: dict[str, Any] = field(default_factory=dict)

    # Pre-resolved runtime state — callers compute these before assembly
    io_namespace: str | None = None

    # Optional overrides
    serialization_spec: SerializationSpec | None = None

    # Work-product lookup — narrow protocol, not concrete register
    work_product_lookup: WorkProductLookup | None = None

    # Pre-built messages — bypass the pipeline and apply only serialization.
    # Used by callers (like the thinking plugin) that hand-build domain-specific
    # message arrays.  When set, the pipeline is NOT executed; the assembly
    # function applies the spec's serialization rules (role merge, system
    # consolidation) and returns the result.
    pre_built_messages: tuple[dict[str, str], ...] | None = None


@dataclass(frozen=True)
class PromptAssemblyResult:
    """Output contract from prompt assembly."""

    messages: tuple[dict[str, str], ...]  # serialized message array for the provider
    output_schema: dict[str, Any] | None  # decode contract schema
    semantic_blocks: tuple[Any, ...]  # MessageBlock tuple (inspection/debugging)
    profile_name: str
    assembly_manifest: dict[str, Any]  # stage timing, block counts, decisions
    prompt_context: Any | None = None  # PromptContext for callers that need post-pipeline access


# ── Standard serialization specs ─────────────────────────────────────

LM_STUDIO_SPEC = SerializationSpec(
    supports_multiple_system_messages=True,
    requires_role_alternation=False,
    system_message_position="first_only",
    supports_assistant_prefill=True,
)
