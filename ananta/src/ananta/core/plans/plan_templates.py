"""Plan template lifecycle — separate the reusable planning skeleton
(template) from a concrete run (instance), enabling fork-by-intent.

POR §4.5. Today a plan *instance* is durable (``pln-…``) but there is no
template lifecycle and no fork-by-intent. This module adds the missing
half: a plan *template* is a reusable joseki-program skeleton with
discovery-by-intent metadata (goal, domain, outcome, program) — authored
and searched like a joseki card — and a pure transform that forks a
template into a concrete *instance* by binding its open slots.

Deliberate asymmetry vs the joseki lifecycle table (Phase 6 Tier 1's
``thinking_authored_joseki``). A template does **not run**, so it earns
no run evidence and therefore has no ``proven`` state and no queryable
run-evidence table. Run evidence lives on plan *instances* (the
``thinking_wbs`` step-state records). So the Q14 dual — a searchable
card *and* a run-evidence state table — applies to joseki and instances;
a template's lifecycle state is pure **curation** metadata (is this the
canonical skeleton to fork, or a superseded/archived one?). Curation
metadata is exactly what KB-card front-matter represents and what
semantic retrieval surfaces, so a template needs only the card — a
second state table would add a queryable surface for evidence that
templates never generate. State-as-front-matter is the correct
representation on the merits, chosen (not missed) for this asymmetry.

Every type here is a frozen value object: forking a template into an
instance cannot mutate the template. That immutability is the structural
guarantee behind the §4.5 done-when — "a template forks to an instance
by intent without mutating the template."

Persisted-card contract (POR §4.6, no silent loss). Card-bound strings
render on a single physical line and parse by reading that line, so
every scalar field (the identity fields, the intent axes, each program
entry, each binding slot) is constrained to a SINGLE LINE at construction
time — a newline or control char is rejected loudly rather than silently
truncated on round-trip; intent axes are short retrieval hooks by design,
so this is a real constraint, not one worth escaping around. A template's
``program`` must be non-empty (a template with no executable skeleton is
not a template); its ``binding_slots`` may be empty (a fully-bound
template with zero open slots is coherent and forks with an empty map).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from ananta.error_handling import FrameworkError

_ERROR_PARAMETER = "core.plans.template_parameter_error"
_ERROR_BINDING = "core.plans.template_binding_error"
_ERROR_STATE = "core.plans.template_state_conflict"
_ERROR_CARD = "core.plans.template_card_error"
_ERROR_FIELD = "core.plans.template_field_error"


def _reject_control_chars(value: str, field: str) -> None:
    """Reject a card-bound string that is not a single clean line.

    Card scalars render as one physical line and parse by reading that
    line, so a newline or control character would SILENTLY TRUNCATE on
    round-trip (``goal='a\\nb'`` → ``'a'``). Rather than build escaping or
    structured multiline, we fail loud at construction: intent axes are
    short retrieval hooks by design, so single-line is a real constraint,
    not a limitation worth engineering around (POR §4.6 persisted-card
    contract: no silent loss). Rejects newlines and every other C0 control
    plus DEL.
    """
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise FrameworkError(
            message=(
                f"plan-template field {field!r} must be a single line with no "
                f"control characters (got {value!r})"
            ),
            error_code=_ERROR_FIELD,
        )

# KB article-class identity for a plan template (POR §4.5 / §4.6). A
# template is a reusable Layer-2 skeleton, searched like a joseki card.
# The role + tags land in the standard labeled front-matter lines the
# knowledge indexer reads (``Article Role:``/``Article Layer:``/
# ``Article Tags:``); the intent axes land in the body so retrieval
# embeds them. This keeps the plan-template card a first-class KB
# article class without the generic KB plugin needing to know about it.
PLAN_TEMPLATE_ARTICLE_ROLE = "plan_template"
PLAN_TEMPLATE_ARTICLE_LAYER = 2
_SUPERSEDED_NONE = "(none)"
_LIST_ITEM_RE = re.compile(r"^(?:\d+\.|-)\s+(.*)$")


class TemplateState(StrEnum):
    """Plan-template lifecycle states — pure curation metadata.

    Contrast the joseki lifecycle (Tier 1)
    ``draft → candidate → proven → superseded → archived``, whose
    ``proven`` is *earned* by a recorded successful run. A template does
    not run, so it has no earned state:

    - ``draft``      — authored, not yet endorsed for forking
    - ``active``     — endorsed; the canonical skeleton to fork by intent
    - ``superseded`` — replaced by another template (``superseded_by``)
    - ``archived``   — retired; kept for provenance, not offered for forking

    These states live in KB-card front-matter, not a state table (see the
    module docstring for the deliberate asymmetry).
    """

    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"

    @property
    def is_forkable(self) -> bool:
        """Only ``draft``/``active`` templates may be forked."""
        return self in (TemplateState.DRAFT, TemplateState.ACTIVE)


@dataclass(frozen=True, slots=True)
class PlanDiscoveryMetadata:
    """Discovery-by-intent metadata (POR §4.5).

    The four axes a searcher supplies to locate a reusable skeleton to
    fork: what it achieves (``goal``), the problem ``domain``, the kind
    of result (``outcome``), and the ordered joseki ``program`` the
    skeleton runs. Rendered into a KB card's front-matter so both
    templates and forked instances are findable by intent.
    """

    goal: str
    domain: str
    outcome: str
    program: tuple[str, ...]

    def __post_init__(self) -> None:
        _reject_control_chars(self.goal, "goal")
        _reject_control_chars(self.domain, "domain")
        _reject_control_chars(self.outcome, "outcome")
        for entry in self.program:
            _reject_control_chars(entry, "program entry")

    def as_front_matter(self) -> dict[str, Any]:
        """Render the discovery axes for a KB card's front-matter."""
        return {
            "goal": self.goal,
            "domain": self.domain,
            "outcome": self.outcome,
            "program": list(self.program),
        }


@dataclass(frozen=True, slots=True)
class PlanTemplate:
    """A reusable planning skeleton, distinct from a plan instance.

    A joseki-program skeleton with open ``binding_slots`` and
    discovery-by-intent metadata, authored and searched like a joseki
    card. It carries no concrete per-run values; those are supplied when
    it is forked into an instance. Frozen — forking cannot mutate it.

    Construction-time invariants (fail loud, POR §4.6 persisted-card
    contract):

    - every card-bound string (``template_key``, ``title``, the intent
      axes, each program entry, each binding slot, ``superseded_by``) is
      single-line — see :func:`_reject_control_chars`;
    - ``program`` is NON-EMPTY: a template without an executable joseki
      skeleton is not a template;
    - ``binding_slots`` MAY be empty — a fully-bound template with zero
      open slots is coherent (it forks with an empty bindings map).
    """

    template_key: str
    title: str
    goal: str
    domain: str
    outcome: str
    program: tuple[str, ...]
    binding_slots: tuple[str, ...]
    state: TemplateState = TemplateState.DRAFT
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        _reject_control_chars(self.template_key, "template_key")
        _reject_control_chars(self.title, "title")
        _reject_control_chars(self.goal, "goal")
        _reject_control_chars(self.domain, "domain")
        _reject_control_chars(self.outcome, "outcome")
        if self.superseded_by is not None:
            _reject_control_chars(self.superseded_by, "superseded_by")
        for key in self.program:
            _reject_control_chars(key, "program entry")
        for slot in self.binding_slots:
            _reject_control_chars(slot, "binding slot")
        if not self.program:
            raise FrameworkError(
                message=(
                    f"plan template {self.template_key!r} must declare a "
                    f"non-empty joseki program"
                ),
                error_code=_ERROR_FIELD,
            )

    @property
    def discovery_metadata(self) -> PlanDiscoveryMetadata:
        return PlanDiscoveryMetadata(
            goal=self.goal,
            domain=self.domain,
            outcome=self.outcome,
            program=self.program,
        )


@dataclass(frozen=True, slots=True)
class PlanInstance:
    """A concrete fork of a plan template.

    Binds the template's open slots to per-run values and records
    provenance (``template_key``). Carries the template's discovery
    metadata forward so instances are themselves findable/forkable by
    intent (POR §4.5 "so old plans are forkable").
    """

    instance_id: str
    template_key: str
    goal: str
    domain: str
    outcome: str
    program: tuple[str, ...]
    bindings: tuple[tuple[str, str], ...]

    @property
    def bindings_map(self) -> dict[str, str]:
        return dict(self.bindings)

    @property
    def discovery_metadata(self) -> PlanDiscoveryMetadata:
        return PlanDiscoveryMetadata(
            goal=self.goal,
            domain=self.domain,
            outcome=self.outcome,
            program=self.program,
        )


def derive_discovery_metadata(
    source: PlanTemplate | PlanInstance,
) -> PlanDiscoveryMetadata:
    """Project a template's or instance's discovery-by-intent metadata.

    A single accessor over both artifact kinds so the KB-card
    front-matter and retrieval index are populated identically whether
    the source is a template being registered or an instance being made
    findable.
    """
    return source.discovery_metadata


def fork_template_to_instance(
    template: PlanTemplate,
    instance_id: str,
    bindings: Mapping[str, str],
) -> PlanInstance:
    """Fork a template into a concrete instance by intent.

    Pure transform: the ``template`` is never mutated (frozen). The
    returned instance binds exactly the template's ``binding_slots`` to
    the supplied ``bindings`` (in slot order) and records
    ``template_key`` provenance.

    Fast-fail (no defaults, no fallback):

    - the template state must be forkable (``draft``/``active``);
    - ``instance_id`` must be non-empty;
    - ``bindings`` must cover EXACTLY ``binding_slots`` — a missing slot
      or an unknown binding is an error (mirrors the joseki validator's
      unbound-slot / out-of-schema-arg rejection).
    """
    if not instance_id:
        raise FrameworkError(
            message="instance_id is required",
            error_code=_ERROR_PARAMETER,
        )

    if not template.state.is_forkable:
        detail = (
            f" (superseded by {template.superseded_by})"
            if template.state is TemplateState.SUPERSEDED and template.superseded_by
            else ""
        )
        raise FrameworkError(
            message=(
                f"Plan template {template.template_key!r} is {template.state} "
                f"and cannot be forked{detail}"
            ),
            error_code=_ERROR_STATE,
        )

    required = set(template.binding_slots)
    supplied = set(bindings)
    missing = required - supplied
    unknown = supplied - required
    if missing or unknown:
        raise FrameworkError(
            message=(
                f"Fork of {template.template_key!r} has invalid bindings: "
                f"missing={sorted(missing)} unknown={sorted(unknown)}"
            ),
            error_code=_ERROR_BINDING,
        )

    ordered = tuple((slot, bindings[slot]) for slot in template.binding_slots)
    return PlanInstance(
        instance_id=instance_id,
        template_key=template.template_key,
        goal=template.goal,
        domain=template.domain,
        outcome=template.outcome,
        program=template.program,
        bindings=ordered,
    )


# ── Curation-lifecycle transitions (SUB-01, POR §4.5 GOAL) ───────────
#
# The pure state machine behind the plan-template transition verb. It
# mirrors the joseki lifecycle (Phase 6 Tier 1) with two documented
# asymmetries, both consequences of "a template does not run":
#
#   * NO ``proven`` state and NO ``candidate`` re-validation gate — those
#     are joseki concepts earned/checked by execution. A template's states
#     are pure CURATION metadata: ``draft`` (authored), ``active``
#     (endorsed as the canonical skeleton to fork), ``superseded``
#     (replaced), ``archived`` (retired).
#   * NO state table and therefore NO predicated compare-and-set. The
#     returned frozen template IS the whole of the new state; persistence
#     is re-rendering its card (:func:`render_template_card`). ``draft`` is
#     the authoring origin, never a manual transition target — exactly as
#     the joseki machine excludes ``draft`` from its manual targets.

# Legal source states per manual transition target. ``draft`` is absent as
# a target (it is where a template is authored, not transitioned to).
TEMPLATE_LEGAL_TRANSITIONS: Mapping[TemplateState, frozenset[TemplateState]] = {
    TemplateState.ACTIVE: frozenset({TemplateState.DRAFT}),
    TemplateState.SUPERSEDED: frozenset(
        {TemplateState.DRAFT, TemplateState.ACTIVE},
    ),
    TemplateState.ARCHIVED: frozenset(
        {TemplateState.DRAFT, TemplateState.ACTIVE, TemplateState.SUPERSEDED},
    ),
}
_MANUAL_TEMPLATE_TARGETS = frozenset(TEMPLATE_LEGAL_TRANSITIONS)


def transition_template(
    template: PlanTemplate,
    *,
    target_state: TemplateState,
    superseded_by: str | None = None,
) -> PlanTemplate:
    """Return a new ``PlanTemplate`` advanced to *target_state* (pure).

    Fast-fail (no defaults, no fallback):

    - *target_state* must be a manual target (``active``/``superseded``/
      ``archived``); ``draft`` is the authoring origin, not a target;
    - the transition must be legal from ``template.state`` (see
      :data:`TEMPLATE_LEGAL_TRANSITIONS`);
    - ``superseded`` requires a real, non-self ``superseded_by``; any other
      target rejects a ``superseded_by`` as meaningless. Archiving a card
      that is already ``superseded`` PRESERVES its replacement pointer.

    The ``template`` is never mutated (frozen); a new value object is
    returned via :func:`dataclasses.replace`, so its construction-time
    invariants (single-line scalars, non-empty program) are re-checked.
    Same-state idempotency is the CALLER's concern — this transform assumes
    a real state change and rejects a no-op as an illegal self-transition.
    """
    if target_state not in _MANUAL_TEMPLATE_TARGETS:
        raise FrameworkError(
            message=(
                f"{target_state} is not a manual plan-template transition "
                f"target — author a draft, then transition to one of "
                f"{sorted(state.value for state in _MANUAL_TEMPLATE_TARGETS)}"
            ),
            error_code=_ERROR_STATE,
        )
    legal_from = TEMPLATE_LEGAL_TRANSITIONS[target_state]
    if template.state not in legal_from:
        raise FrameworkError(
            message=(
                f"plan template {template.template_key!r} cannot transition "
                f"{template.state} → {target_state} — legal source states "
                f"for {target_state} are "
                f"{sorted(state.value for state in legal_from)}"
            ),
            error_code=_ERROR_STATE,
        )
    if target_state is TemplateState.SUPERSEDED:
        new_superseded_by: str | None = _require_supersession(
            template, superseded_by,
        )
    else:
        if superseded_by is not None:
            raise FrameworkError(
                message=(
                    f"'superseded_by' is only meaningful when superseding — "
                    f"drop it when transitioning {template.template_key!r} to "
                    f"{target_state}"
                ),
                error_code=_ERROR_PARAMETER,
            )
        # Preserve an existing pointer (e.g. superseded → archived).
        new_superseded_by = template.superseded_by
    return replace(template, state=target_state, superseded_by=new_superseded_by)


def _require_supersession(template: PlanTemplate, superseded_by: str | None) -> str:
    """Validate the replacement key for a supersession (real, non-self)."""
    if not superseded_by:
        raise FrameworkError(
            message=(
                f"superseding plan template {template.template_key!r} requires "
                f"'superseded_by' naming the replacement template"
            ),
            error_code=_ERROR_PARAMETER,
        )
    if superseded_by == template.template_key:
        raise FrameworkError(
            message=f"plan template {template.template_key!r} cannot supersede itself",
            error_code=_ERROR_PARAMETER,
        )
    return superseded_by


# ── KB-card serialization (searchable representation) ────────────────
#
# A plan template is made searchable by rendering it to a KB card whose
# front-matter follows the platform's labeled-line convention and whose
# body carries the discovery-by-intent language for the embedder. The
# inverse parse reads a stored card back into a ``PlanTemplate`` so an
# old template can be forked by intent ("so old plans are forkable").


def render_template_card(template: PlanTemplate) -> str:
    """Render a plan template as a searchable KB card.

    Front-matter carries the article class + state; the ``## Intent``
    body carries goal/domain/outcome so retrieval embeds the intent
    language. Round-trips through :func:`parse_template_card`.
    """
    tags = [
        "plan-template",
        f"domain:{template.domain}",
        f"state:{template.state}",
    ]
    program_lines = "\n".join(
        f"{index}. {key}" for index, key in enumerate(template.program, start=1)
    )
    slot_lines = "\n".join(f"- {slot}" for slot in template.binding_slots)
    superseded_line = (
        f"- Superseded By: {template.superseded_by}\n" if template.superseded_by else ""
    )
    # Article-class identity lives in the front-matter labeled lines the
    # indexer reads; the template's own identity + intent live in the
    # body so they survive preamble-stripping and are embedded for search.
    return (
        f"# {template.title}\n\n"
        f"Article Role: {PLAN_TEMPLATE_ARTICLE_ROLE}\n"
        f"Article Layer: {PLAN_TEMPLATE_ARTICLE_LAYER}\n"
        f"Article Tags: {', '.join(tags)}\n\n"
        "## Identity\n\n"
        f"- Template Key: {template.template_key}\n"
        f"- Template State: {template.state}\n"
        f"{superseded_line}\n"
        "## Intent\n\n"
        f"- Goal: {template.goal}\n"
        f"- Domain: {template.domain}\n"
        f"- Outcome: {template.outcome}\n\n"
        "## Joseki Program\n\n"
        f"{program_lines}\n\n"
        "## Binding Slots\n\n"
        f"{slot_lines}\n"
    )


def parse_template_card(content: str) -> PlanTemplate:
    """Parse a rendered plan-template card back into a ``PlanTemplate``.

    Fast-fail: missing required fields or an unknown state raise a
    ``FrameworkError`` (no silent defaults).
    """
    title = _require_card_field(_first_header(content), "title")
    template_key = _require_card_field(
        _labeled_value(content, "- Template Key"), "Template Key"
    )
    state_raw = _require_card_field(
        _labeled_value(content, "- Template State"), "Template State"
    )
    goal = _require_card_field(_labeled_value(content, "- Goal"), "Goal")
    domain = _require_card_field(_labeled_value(content, "- Domain"), "Domain")
    outcome = _require_card_field(_labeled_value(content, "- Outcome"), "Outcome")

    try:
        state = TemplateState(state_raw)
    except ValueError as exc:
        raise FrameworkError(
            message=f"unknown plan-template state {state_raw!r}",
            error_code=_ERROR_CARD,
        ) from exc

    superseded = _labeled_value(content, "- Superseded By")
    superseded_by = None if superseded in (None, _SUPERSEDED_NONE) else superseded

    program = tuple(_section_items(content, "Joseki Program"))
    if not program:
        # A card without an executable joseki skeleton is not a template.
        # Fail loud at parse (PlanTemplate construction also rejects this,
        # so both ends of the round-trip are guarded).
        raise FrameworkError(
            message="plan-template card has an empty or missing '## Joseki Program'",
            error_code=_ERROR_CARD,
        )

    return PlanTemplate(
        template_key=template_key,
        title=title,
        goal=goal,
        domain=domain,
        outcome=outcome,
        program=program,
        binding_slots=tuple(_section_items(content, "Binding Slots")),
        state=state,
        superseded_by=superseded_by,
    )


def _first_header(content: str) -> str | None:
    """Return the first markdown ``# `` header text, if any."""
    for line in content.splitlines()[:5]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return None


def _labeled_value(content: str, label: str) -> str | None:
    """Return the text after ``<label>:`` on the first matching line."""
    prefix = f"{label}:"
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return None


def _section_items(content: str, header: str) -> list[str]:
    """Return the list-item texts under a ``## <header>`` section.

    Handles both numbered (``1. x``) and bullet (``- x``) list items and
    stops at the next ``## `` header.
    """
    items: list[str] = []
    in_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped[3:].strip() == header
            continue
        if not in_section or not stripped:
            continue
        match = _LIST_ITEM_RE.match(stripped)
        if match:
            items.append(match.group(1).strip())
    return items


def _require_card_field(value: str | None, label: str) -> str:
    """Return a non-empty card field or fail loudly."""
    if not value:
        raise FrameworkError(
            message=f"plan-template card missing required field: {label}",
            error_code=_ERROR_CARD,
        )
    return value
