"""PromptContext - Accumulator for prompt pipeline data.

This dataclass flows through all pipeline stages, accumulating data at each step.
Stages mutate this object rather than transforming it, enabling full observability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

if TYPE_CHECKING:
    from ananta.core.prompts.plan_state import PlanState

# -- Closed structural field types (Section 5) --

ContextLayer = Literal["ossified_context", "living_context"]

ReasoningSlot = Literal[
    "static_frame",
    "settled_history",
    "working_evidence",
    "working_state",
    "dialogue_frontier",
    "synthetic_driver",
]

PromptRole = Literal["system", "assistant", "user"]

HistoryKind = Literal["none", "input_event", "output_event"]

SourceKind = Literal[
    "system_template",
    "prompt_asset",
    "focus_state",
    "human_input",
    "model_output",
    "response_processor_output",
    "runtime_instruction",
    "persisted_event",
    # Added by Unit 9 for block-producing pipeline stages:
    "process_catalog",
    "discovered_schema",
    "plugin_notice",
]

TransitionBehavior = Literal[
    "stable",
    "living_to_ossified",
    "replaced",
    "filtered",
    "promoted",
]

# -- Allowed-value sets for boundary validation --
ALLOWED_CONTEXT_LAYERS: frozenset[str] = frozenset(get_args(ContextLayer))
ALLOWED_REASONING_SLOTS: frozenset[str] = frozenset(get_args(ReasoningSlot))
ALLOWED_PROMPT_ROLES: frozenset[str] = frozenset(get_args(PromptRole))
ALLOWED_HISTORY_KINDS: frozenset[str] = frozenset(get_args(HistoryKind))
ALLOWED_TRANSITION_BEHAVIORS: frozenset[str] = frozenset(get_args(TransitionBehavior))


@dataclass(slots=True)
class SourceReference:
    """Traceability back to the originating artifact."""

    kind: str  # e.g. "event_id", "template", "focus_buffer", "process_key"
    ref: str  # The actual identifier


# -- Ordering weights for assembly (Section 7) --
_SLOT_ORDER: dict[str, int] = {
    "static_frame": 0,
    "settled_history": 1,
    "working_state": 2,       # Focus blocks (artifacts, plan view) before guidance
    "working_evidence": 3,    # Guidance articles immediately before driver
    "dialogue_frontier": 4,
    "synthetic_driver": 4,    # Same position — mutually exclusive with dialogue_frontier
}

_LAYER_ORDER: dict[str, int] = {
    "ossified_context": 0,
    "living_context": 1,
}


@dataclass(slots=True)
class MessageBlock:
    """A single prompt block with all structural fields assigned at creation.

    Construction-time invariants (Section 9) are validated in __post_init__.
    """

    block_id: str
    context_layer: ContextLayer
    reasoning_slot: ReasoningSlot
    ephemeral: bool
    history_kind: HistoryKind
    source_kind: SourceKind
    subtype: str
    source_reference: SourceReference
    transition_behavior: TransitionBehavior
    content: str
    sequence: int = 0  # Within-slot tie-breaker for assembly ordering
    prompt_role: str = ""  # Transitional: set during construction or resolved by serializer

    def __post_init__(self) -> None:
        """Validate construction-time invariants (Section 9)."""
        self._check_ephemeral_transition()
        self._check_living_context_transition()
        self._check_ossified_not_ephemeral()
        self._check_slot_ephemeral_constraints()
        self._check_slot_layer_constraints()
        self._check_history_kind_consistency()

    def _check_ephemeral_transition(self) -> None:
        """Invariant 1: ephemeral=true -> transition in {replaced, filtered}."""
        if self.ephemeral and self.transition_behavior not in ("replaced", "filtered"):
            msg = (
                f"Invariant 1: ephemeral=true requires transition_behavior "
                f"in {{replaced, filtered}}, got '{self.transition_behavior}' "
                f"(block_id={self.block_id})"
            )
            raise ValueError(msg)

    def _check_living_context_transition(self) -> None:
        """Invariants 2-3: living_context transition rules."""
        if self.ephemeral or self.context_layer != "living_context":
            return
        # Invariant 2: non-persisted -> living_to_ossified
        if self.source_kind != "persisted_event":
            if self.transition_behavior != "living_to_ossified":
                msg = (
                    f"Invariant 2: non-ephemeral living_context block from "
                    f"'{self.source_kind}' must have transition_behavior="
                    f"living_to_ossified, got '{self.transition_behavior}' "
                    f"(block_id={self.block_id})"
                )
                raise ValueError(msg)
        # Invariant 3: persisted_event -> promoted
        elif self.transition_behavior != "promoted":
            msg = (
                f"Invariant 3: non-ephemeral living_context persisted_event "
                f"must have transition_behavior=promoted, got "
                f"'{self.transition_behavior}' (block_id={self.block_id})"
            )
            raise ValueError(msg)

    def _check_ossified_not_ephemeral(self) -> None:
        """Invariant 4: ossified_context -> ephemeral=false."""
        if self.context_layer == "ossified_context" and self.ephemeral:
            msg = (
                f"Invariant 4: ossified_context blocks must have ephemeral=false "
                f"(block_id={self.block_id})"
            )
            raise ValueError(msg)

    def _check_slot_ephemeral_constraints(self) -> None:
        """Invariants 5-7: slot-specific ephemeral requirements."""
        _MUST_BE_EPHEMERAL: dict[str, int] = {
            "working_state": 5,
            "synthetic_driver": 6,
        }
        _MUST_NOT_BE_EPHEMERAL: dict[str, int] = {
            "dialogue_frontier": 7,
        }
        if self.reasoning_slot in _MUST_BE_EPHEMERAL and not self.ephemeral:
            inv = _MUST_BE_EPHEMERAL[self.reasoning_slot]
            msg = (
                f"Invariant {inv}: {self.reasoning_slot} blocks must have "
                f"ephemeral=true (block_id={self.block_id})"
            )
            raise ValueError(msg)
        if self.reasoning_slot in _MUST_NOT_BE_EPHEMERAL and self.ephemeral:
            inv = _MUST_NOT_BE_EPHEMERAL[self.reasoning_slot]
            msg = (
                f"Invariant {inv}: {self.reasoning_slot} blocks must have "
                f"ephemeral=false (block_id={self.block_id})"
            )
            raise ValueError(msg)

    def _check_slot_layer_constraints(self) -> None:
        """Invariants 8-9: static_frame/settled_history -> ossified_context."""
        _OSSIFIED_ONLY_SLOTS: dict[str, int] = {
            "static_frame": 8,
            "settled_history": 9,
        }
        if (
            self.reasoning_slot in _OSSIFIED_ONLY_SLOTS
            and self.context_layer != "ossified_context"
        ):
            inv = _OSSIFIED_ONLY_SLOTS[self.reasoning_slot]
            msg = (
                f"Invariant {inv}: {self.reasoning_slot} blocks must be in "
                f"ossified_context (block_id={self.block_id})"
            )
            raise ValueError(msg)

    def _check_history_kind_consistency(self) -> None:
        """Invariants 10-11: history_kind consistency.

        10: ephemeral=true -> history_kind="none"
            (ephemeral blocks are never persisted, so they cannot be events)
        11: transition_behavior=living_to_ossified -> history_kind != "none"
            (blocks that will be persisted must declare what kind of event they become)
        """
        if self.ephemeral and self.history_kind != "none":
            msg = (
                f"Invariant 10: ephemeral=true requires history_kind='none', "
                f"got '{self.history_kind}' (block_id={self.block_id})"
            )
            raise ValueError(msg)
        if (
            self.transition_behavior == "living_to_ossified"
            and self.history_kind == "none"
        ):
            msg = (
                f"Invariant 11: transition_behavior=living_to_ossified requires "
                f"history_kind != 'none' (block_id={self.block_id})"
            )
            raise ValueError(msg)

    @property
    def sort_key(self) -> tuple[int, int, int]:
        """Sort key for assembly ordering (Section 7).

        Returns (layer_order, slot_order, sequence). Slots determine major
        ordering. Within a slot, ``sequence`` provides tie-breaking:

        - ``settled_history``: chronological event order
        - ``dialogue_frontier``: source order (assistant before user reply)
        - ``working_evidence``: catalog before results
        - other slots: stable insertion order
        """
        return (
            _LAYER_ORDER[self.context_layer],
            _SLOT_ORDER[self.reasoning_slot],
            self.sequence,
        )


# Marker string produced by plan windowing logic in focused plan summaries.
# Always ``ACTIVE_PLAN:`` — the old ``ACTIVE_PLAN_CHECKPOINT_EXCERPT:``
# variant was removed; window type is detected by content inspection.
ACTIVE_PLAN_MARKER = "ACTIVE_PLAN"


@dataclass(slots=True)
class PromptContext:
    """Accumulates prompt data through pipeline stages.

    Each stage adds to this context rather than transforming it.
    The final assembly uses all accumulated data.

    Attributes:
        flow_id: Unique identifier for the action flow
        action_name: Name of the action being executed
        session_id: Session identifier for context retrieval
        context_id: Context stream ID for platform-managed context (optional)

        raw_action_params: Original action parameters before template resolution
        resolved_action_params: Parameters after template resolution
        template_substitutions: Map of pattern -> resolved value for debugging

        system_prompt: Formatted system prompt content
        user_prompt: Formatted user prompt content
        output_schema: JSON schema for structured output (if any)

        focused_memories: Pinned memories from focus buffer (budget-capped, highest priority)
        relevant_memories: Semantically relevant memories (ACT-R recall)
        identity_memories: Identity/personality memories (always present)
        conversation_history: Parsed conversation messages

        messages: Final message array for LLM API
        api_payload: Complete API request payload

        stage_timings: Execution time per stage in milliseconds
        stage_decisions: Log of decisions made by each stage
    """

    # Identity (set at creation, immutable)
    flow_id: str
    action_name: str
    session_id: str
    context_id: str | None = None

    # Stage 1: Template Resolution
    raw_action_params: dict[str, Any] = field(default_factory=dict)
    resolved_action_params: dict[str, Any] = field(default_factory=dict)
    template_substitutions: dict[str, str] = field(default_factory=dict)

    # Stage 2: Formatting
    system_prompt: str = ""
    user_prompt: str = ""
    user_prompt_when_observation_empty: str = ""  # Alternate instruction when observation has no results
    tool_observation: str | None = None  # Action result context (rendered as assistant message)
    raw_observation_dict: dict[str, Any] | None = None  # Pre-rendered observation for plan step matching
    observation_is_empty: bool = False  # True when action_result.data is an empty collection
    observation_source_memory_id: str = ""  # memory_id of the artifact that produced this observation
    output_schema: dict[str, Any] | None = None

    # Stage 3: Context Injection
    focused_memories: list[dict[str, Any]] = field(default_factory=list)
    relevant_memories: list[dict[str, Any]] = field(default_factory=list)
    identity_memories: list[dict[str, Any]] = field(default_factory=list)
    conversation_history: list[dict[str, str]] = field(default_factory=list)
    history_memory_ids: set[str] = field(default_factory=set)  # memory_ids already present in conversation history
    has_stored_system_events: bool = False  # True if SYSTEM events exist in context store
    has_focused_plan: bool = False  # True if any focused memory contains plan sections
    is_wbs_active_plan: bool = False  # True when plan has ACTIVE_WBS (any step in WBS regime)
    is_wbs_execution_context: bool = False  # True when current step is a projected WBS execution step
    playbook_section_content: str | None = None  # Hydrated playbook section for current step
    playbook_section_id: str | None = None  # Section ID of the hydrated playbook section
    attachment_summary: str = ""  # Recent attachments summary (appended to user message)
    generated_files_summary: str = ""  # Generated files summary (appended to user message)
    current_step_process_keys: list[str] = field(default_factory=list)  # Resolved keys for active plan step
    model_visible_process_keys: list[str] = field(default_factory=list)  # Keys the model must emit (excludes auto-injected companions)

    # Pre-resolved runtime state
    io_namespace: str | None = None  # IO namespace resolved by plugin before pipeline execution

    # Structured plan state (computed by PlanStateStage, consumed by downstream stages)
    plan_state: PlanState | None = None

    # Profile context flags (set by assembly caller, consumed by ContextStage)
    profile_include_conversation_history: bool = True
    profile_include_focused_memories: bool = True
    profile_include_semantic_recall: bool = True
    profile_max_context_messages: int | None = None

    # Stage outputs (populated by CatalogStage/GuidanceStage, consumed by APIStage)
    discovered_schema_text: str | None = None  # Discovered process schema (CatalogStage)
    step_guidance_messages: list[dict[str, str]] = field(default_factory=list)  # Guidance articles + drivers (GuidanceStage)

    # Stage 4: API Payload
    message_blocks: list[MessageBlock] = field(default_factory=list)  # Block-based assembly
    messages: list[dict[str, str]] = field(default_factory=list)
    api_payload: dict[str, Any] = field(default_factory=dict)
    has_current_user_turn: bool = False  # True when a real user message is appended

    # Manifest data
    stage_timings: dict[str, float] = field(default_factory=dict)
    stage_decisions: dict[str, list[str]] = field(default_factory=dict)

    def add_decision(self, stage_name: str, decision: str) -> None:
        """Add a decision log entry for a stage.

        Args:
            stage_name: Name of the stage making the decision
            decision: Description of what was decided/done
        """
        if stage_name not in self.stage_decisions:
            self.stage_decisions[stage_name] = []
        self.stage_decisions[stage_name].append(decision)

    def get_total_memory_count(self) -> int:
        """Get total count of memory items (focused + relevant + identity)."""
        return len(self.focused_memories) + len(self.relevant_memories) + len(self.identity_memories)
