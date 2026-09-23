"""Canonical plan data types.

``ParsedPlanStep`` and ``ParsedPlan`` are immutable value objects that
represent a parsed plan.  The ``marker`` field is the single source of
truth for step status; boolean helpers are computed properties.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ananta.core.result_processing.enums import ResultProcessorKind

# Actions that record progress or communicate — never justify a step alone.
COMPANION_SUFFIXES: tuple[str, ...] = ("::upsert_plan", "::post_message")

type StepMarker = Literal["X", ">", " ", "-"]


@dataclass(frozen=True, slots=True)
class ComposedReference:
    """A cross-step argument reference in a WBS bound sub-step.

    Resolves at execution time by reading the source argument from
    another step's bound arguments and applying an optional suffix.

    Example WBS syntax::

        Composed: input_midi_file = output_phrase_id from step 1 + "_mid"

    When ``source_steps`` has multiple entries, the resolved values
    are collected into a list (used for ``input_audio_files`` in
    concatenation steps).
    """

    target_arg: str
    source_arg: str
    source_steps: tuple[int, ...]
    suffix: str


@dataclass(frozen=True, slots=True)
class BoundSubStep:
    """A WBS sub-step with its process key and parsed bound arguments."""

    label: str
    process_key: str
    arguments: dict[str, Any] | None = None
    composed_references: tuple[ComposedReference, ...] = ()


@dataclass(frozen=True, slots=True)
class LayerPolicy:
    """Parsed ``LAYER_POLICY:`` annotation on a plan step.

    Mirrors the four search-API knobs in
    ``knowledge_service::search``: an exact set, an inclusive range, and
    an unlayered-inclusion flag. Either ``knowledge_layers`` is set, or
    one/both of ``min_knowledge_layer`` / ``max_knowledge_layer`` —
    never both. ``include_unlayered`` defaults to ``None`` (caller
    should treat as "unset", letting the search default apply).
    """

    knowledge_layers: tuple[int, ...] | None = None
    min_knowledge_layer: int | None = None
    max_knowledge_layer: int | None = None
    include_unlayered: bool | None = None

    @property
    def is_empty(self) -> bool:
        return (
            self.knowledge_layers is None
            and self.min_knowledge_layer is None
            and self.max_knowledge_layer is None
            and self.include_unlayered is None
        )

    def as_arguments(self) -> dict[str, Any]:
        """Render as a flat ``{arg_name: value}`` dict suitable for
        injection into a search action's arguments. Skips unset fields."""
        args: dict[str, Any] = {}
        if self.knowledge_layers is not None:
            args["knowledge_layers"] = list(self.knowledge_layers)
        if self.min_knowledge_layer is not None:
            args["min_knowledge_layer"] = self.min_knowledge_layer
        if self.max_knowledge_layer is not None:
            args["max_knowledge_layer"] = self.max_knowledge_layer
        if self.include_unlayered is not None:
            args["include_unlayered"] = self.include_unlayered
        return args


@dataclass(frozen=True, slots=True)
class ParsedPlanStep:
    """A single parsed plan step."""

    marker: StepMarker
    number: int
    lines: tuple[str, ...]
    process_keys: tuple[str, ...]
    playbook_id: str | None = None
    playbook_section_id: str | None = None
    guidance_article: str | None = None
    guidance_section_id: str | None = None
    support_articles: tuple[str, ...] = ()
    bound_sub_steps: tuple[BoundSubStep, ...] = ()
    min_actions: int | None = None
    layer_policy: LayerPolicy | None = None
    result_processor_kind: ResultProcessorKind | None = None
    # ``AUTO_SAFE: true`` step annotation (Phase 4 / Q15): the author's
    # EXPLICIT opt-in for pull-mode auto-submission. Honored only when the
    # step also declares deterministic continuation AND passes the full
    # 18-invariant validation — the flag alone never auto-submits.
    auto_safe: bool = False

    @property
    def knowledge_layers(self) -> tuple[int, ...] | None:
        """Back-compat shortcut to ``layer_policy.knowledge_layers``."""
        return self.layer_policy.knowledge_layers if self.layer_policy else None

    # -- Status (derived from marker) --

    @property
    def is_completed(self) -> bool:
        return self.marker == "X"

    @property
    def is_active(self) -> bool:
        return self.marker == ">"

    @property
    def is_pending(self) -> bool:
        return self.marker == " "

    @property
    def is_skipped(self) -> bool:
        return self.marker == "-"

    # -- Action categories --

    @property
    def continuation_keys(self) -> tuple[str, ...]:
        """Non-companion process keys — these justify a step continuing."""
        return tuple(
            k for k in self.process_keys
            if not any(k.endswith(s) for s in COMPANION_SUFFIXES)
        )

    @property
    def companion_keys(self) -> tuple[str, ...]:
        """Companion process keys — bookkeeping and communication."""
        return tuple(
            k for k in self.process_keys
            if any(k.endswith(s) for s in COMPANION_SUFFIXES)
        )

    @property
    def has_planning_extension(self) -> bool:
        """True when upsert_plan carries model-authored future-tail content.

        Detected by the sub-step text containing "extend" or "replace"
        alongside the upsert_plan process key.  When ``True``, the schema
        must include ``upsert_plan`` so the model provides the extended or
        replacement plan content.  When ``False``, ``upsert_plan`` is
        bookkeeping (progress-only) and the platform auto-injects it.

        The "replace" keyword covers the WBS transition case where the
        planning meta-plan is replaced by a projected active plan.
        """
        _EXTENSION_KEYWORDS = ("extend", "replace")
        for line in self.lines:
            lower = line.lower()
            if "upsert_plan" in lower and any(
                kw in lower for kw in _EXTENSION_KEYWORDS
            ):
                return True
        return False

    @property
    def is_checkpoint(self) -> bool:
        """True when the step is companion-only (pause after completion)."""
        return len(self.continuation_keys) == 0 and len(self.process_keys) > 0

    @property
    def is_continuation(self) -> bool:
        """True when the step has at least one continuation action."""
        return len(self.continuation_keys) > 0

    def summary(self) -> str:
        """First line only — for collapsed sections."""
        return self.lines[0] if self.lines else ""

    def full_text(self) -> str:
        """All lines joined — for the ACTIVE_PLAN section."""
        return "\n".join(self.lines)


@dataclass(frozen=True, slots=True)
class ParsedPlan:
    """A fully parsed plan with header and steps."""

    header_lines: tuple[str, ...]
    steps: tuple[ParsedPlanStep, ...]
    plan_guidance_article: str | None = None
    plan_guidance_section_id: str | None = None

    # -- Plan-level status (derived from steps) --

    @property
    def current_step_number(self) -> int | None:
        """Step number of the active ``[>]`` step, or ``None``."""
        for step in self.steps:
            if step.is_active:
                return step.number
        return None

    @property
    def current_step(self) -> ParsedPlanStep | None:
        """The active ``[>]`` step object, or ``None``."""
        for step in self.steps:
            if step.is_active:
                return step
        return None

    @property
    def first_executable_step_number(self) -> int | None:
        """First step that is not completed and not skipped."""
        for step in self.steps:
            if not step.is_completed and not step.is_skipped:
                return step.number
        return None

    @property
    def is_complete(self) -> bool:
        """True when every step is either completed or skipped."""
        return bool(self.steps) and all(
            s.is_completed or s.is_skipped for s in self.steps
        )

    def step_by_number(self, number: int) -> ParsedPlanStep | None:
        """Look up a step by its number."""
        for step in self.steps:
            if step.number == number:
                return step
        return None
