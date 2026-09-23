"""Plan-template curation-lifecycle transitions (SUB-01, POR Phase-6 §4.5 GOAL).

The verb-backed transition machine for a plan TEMPLATE's curation lifecycle

    draft → active → superseded → archived

mirroring Tier-1's ``AuthoredJosekiLifecycle`` (``transition_authored_joseki``)
— but over the card's front-matter, NOT a state table. The deliberate,
documented asymmetry (POR §4.5): a template does not RUN, so it earns no run
evidence, has no ``proven`` state, and needs no queryable state row. Its
lifecycle state is pure CURATION metadata carried in the KB card's front-matter
(:func:`render_template_card` / :func:`parse_template_card`). A transition is
therefore a validated front-matter edit: read the card, parse it, apply the
pure :func:`transition_template` state machine, re-render, and persist the card
back through the knowledge service (``create_file``/``edit_file``).

Two structural consequences of "state lives in the card, not a table":

* **No predicated compare-and-set.** The joseki engine CAS-guards its
  ``thinking_authored_joseki`` row (``SET state=<to> WHERE state=<from>``);
  a template has no row, so the card rewrite is the whole persistence step.
  Concurrent curation edits are last-write-wins — acceptable for a
  low-frequency, human/agent-initiated curation action, and a CAS would
  require a row that §4.5 deliberately omits.
* **No retirement banner.** The joseki engine stamps a ``> **LIFECYCLE: …**``
  banner into the card because joseki state lived OFF the card (in a table),
  so the card needed an in-body retirement signal. A template's
  ``- Template State:`` front-matter line + the ``state:`` facet tag ARE the
  on-card, searchable signal, so no banner is needed — omitting it is the
  correct asymmetry, not a gap.

Collaborators (card reader/writer) are injected so the engine is exercisable
offline against in-memory doubles; no service is resolved inside.
"""

from __future__ import annotations

from typing import Any, Protocol

from ananta.core.plans.plan_templates import (
    TEMPLATE_LEGAL_TRANSITIONS,
    PlanTemplate,
    TemplateState,
    parse_template_card,
    render_template_card,
    transition_template,
)
from ananta.error_handling import FrameworkError

from default_thinking_plugin.constants import ErrorCode

_CARD_SUFFIX = ".md"


class PlanTemplateCardReader(Protocol):
    """Read a plan-template card from the plan_templates knowledge base."""

    def read(self, path: str) -> str:
        """Return the card markdown at *path*, or ``""`` when absent."""
        ...


class PlanTemplateCardWriter(Protocol):
    """Write a plan-template card into the plan_templates knowledge base."""

    def write(self, path: str, content: str) -> None:
        """Create or update the card at *path* (KB-relative)."""
        ...


class PlanTemplateLifecycle:
    """Curation-lifecycle state machine over a plan template's card.

    Collaborators are injected so the whole engine runs offline (in-memory
    card doubles); no service is resolved inside. Every verb reads the
    canonical card, applies the pure :func:`transition_template` transform,
    and rewrites the card — front-matter IS the state (no row, no CAS).
    """

    def __init__(
        self,
        *,
        card_reader: PlanTemplateCardReader,
        card_writer: PlanTemplateCardWriter,
    ) -> None:
        self._reader = card_reader
        self._writer = card_writer

    # -- public verbs -------------------------------------------------------

    def transition(
        self,
        *,
        template_key: str,
        target_state: str,
        superseded_by: str | None = None,
    ) -> dict[str, Any]:
        """Advance a plan template to *target_state* (active/superseded/archived).

        ``draft`` is rejected — it is the authoring origin, not a manual
        target. Returns the previous + new state; ``status`` is
        ``"transitioned"`` or, when the card already held the target,
        ``"unchanged"`` (idempotent no-op).
        """
        target = _parse_target_state(target_state)
        if target not in TEMPLATE_LEGAL_TRANSITIONS:
            # Non-manual target ('draft', the authoring origin) is rejected
            # regardless of the card's current state. Hoisted ABOVE the
            # idempotent short-circuit: without this, a draft card asked to
            # transition to 'draft' would hit ``current.state is target`` and
            # return 'unchanged' instead of the promised rejection (Reviewer-B
            # LOW). transition_template keeps the same guard for direct callers.
            raise FrameworkError(
                message=(
                    f"{target} is not a manual plan-template transition target "
                    f"— 'draft' is the authoring origin; transition "
                    f"{template_key!r} to one of "
                    f"{sorted(state.value for state in TEMPLATE_LEGAL_TRANSITIONS)}"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        if target is not TemplateState.SUPERSEDED and superseded_by is not None:
            # Coherence guard hoisted ABOVE the idempotent no-op so a stray
            # superseded_by is rejected regardless of the current state (the
            # no-op path would otherwise silently swallow it). transition_template
            # keeps the same guard as defense-in-depth for direct core callers.
            raise FrameworkError(
                message=(
                    f"'superseded_by' is only meaningful when superseding — "
                    f"drop it when transitioning {template_key!r} to {target}"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        current = self._require_template(template_key)

        if current.state is target:
            return _idempotent(current, superseded_by)

        if target is TemplateState.SUPERSEDED:
            self._assert_replacement(template_key, superseded_by)

        updated = transition_template(
            current, target_state=target, superseded_by=superseded_by,
        )
        self._writer.write(_card_path(template_key), render_template_card(updated))
        return {
            "template_key": template_key,
            "previous_state": current.state.value,
            "state": updated.state.value,
            "superseded_by": updated.superseded_by,
            "status": "transitioned",
        }

    def get(self, *, template_key: str) -> dict[str, Any]:
        """Read a plan template's curation view (observability).

        Returns the front-matter lifecycle state + the discovery-by-intent
        axes (the searchable curation surface), NOT the program skeleton —
        mirroring how ``get_authored_joseki`` returns the lifecycle row, not
        the card body. ``found`` is False when no card exists.
        """
        template = self._load(template_key)
        if template is None:
            return {"found": False, "template_key": template_key}
        return {
            "found": True,
            "template_key": template.template_key,
            "title": template.title,
            "state": template.state.value,
            "goal": template.goal,
            "domain": template.domain,
            "outcome": template.outcome,
            "superseded_by": template.superseded_by,
        }

    # -- card access --------------------------------------------------------

    def _load(self, template_key: str) -> PlanTemplate | None:
        """Read + parse the card at *template_key*'s canonical path.

        Returns ``None`` when the card is absent. A card whose parsed
        ``Template Key`` disagrees with the row key it was loaded for is an
        identity mismatch and is rejected loudly (mirrors the joseki
        candidate gate's expected-key binding), never silently operated on.
        """
        content = self._reader.read(_card_path(template_key))
        if not content:
            return None
        template = parse_template_card(content)
        if template.template_key != template_key:
            raise FrameworkError(
                message=(
                    f"plan-template card at {_card_path(template_key)!r} "
                    f"declares Template Key {template.template_key!r}, not "
                    f"{template_key!r} — refusing to operate on a mismatched "
                    f"card"
                ),
                error_code=ErrorCode.PLAN_TEMPLATE_STATE_CONFLICT,
            )
        return template

    def _require_template(self, template_key: str) -> PlanTemplate:
        template = self._load(template_key)
        if template is None:
            raise FrameworkError(
                message=(
                    f"plan template {template_key!r} is not registered — no "
                    f"card exists at {_card_path(template_key)!r} in the "
                    f"plan_templates knowledge base"
                ),
                error_code=ErrorCode.PLAN_TEMPLATE_NOT_FOUND,
            )
        return template

    def _assert_replacement(
        self, template_key: str, superseded_by: str | None,
    ) -> None:
        """Supersession replacement must be a registered, non-archived template.

        ``transition_template`` already rejects an empty / self
        ``superseded_by`` with the precise message; this adds the I/O-bound
        guard (read the replacement card) the pure transform cannot do —
        mirroring the joseki supersession guard.
        """
        if not superseded_by or superseded_by == template_key:
            return  # transition_template raises with the precise message
        replacement = self._load(superseded_by)
        if replacement is None:
            raise FrameworkError(
                message=(
                    f"replacement plan template {superseded_by!r} is not "
                    f"registered — register it before superseding "
                    f"{template_key!r} to it"
                ),
                error_code=ErrorCode.PLAN_TEMPLATE_NOT_FOUND,
            )
        if replacement.state is TemplateState.ARCHIVED:
            raise FrameworkError(
                message=(
                    f"replacement plan template {superseded_by!r} is archived "
                    f"— a retired template cannot be the successor for "
                    f"{template_key!r}"
                ),
                error_code=ErrorCode.PLAN_TEMPLATE_STATE_CONFLICT,
            )


def _idempotent(
    current: PlanTemplate, superseded_by: str | None,
) -> dict[str, Any]:
    """No-op when the card already holds the target state.

    For an already-``superseded`` card, a DIFFERING ``superseded_by`` is a
    conflict, not a no-op — the replacement pointer is load-bearing (mirrors
    the joseki idempotent path). No card rewrite occurs: front-matter already
    carries the correct state, and there is no banner to re-assert.
    """
    if (
        current.state is TemplateState.SUPERSEDED
        and superseded_by is not None
        and superseded_by != current.superseded_by
    ):
        raise FrameworkError(
            message=(
                f"plan template {current.template_key!r} is already superseded "
                f"by {current.superseded_by!r}; refusing to repoint it to "
                f"{superseded_by!r}"
            ),
            error_code=ErrorCode.PLAN_TEMPLATE_STATE_CONFLICT,
        )
    return {
        "template_key": current.template_key,
        "previous_state": current.state.value,
        "state": current.state.value,
        "superseded_by": current.superseded_by,
        "status": "unchanged",
    }


def _parse_target_state(target_state: str) -> TemplateState:
    """Coerce the caller's string to a ``TemplateState`` (fail loud)."""
    try:
        return TemplateState(target_state)
    except ValueError as exc:
        legal = sorted(state.value for state in TemplateState)
        raise FrameworkError(
            message=(
                f"unknown plan-template target state {target_state!r} — "
                f"expected one of {legal}"
            ),
            error_code=ErrorCode.PARAMETER_ERROR,
        ) from exc


def _card_path(template_key: str) -> str:
    """Canonical KB-relative card path for *template_key*.

    A template key is ``::``-namespaced (``plan_template::<domain>::<name>``).
    Colons in filenames are fragile, so the namespace separators map to a
    directory hierarchy — the KB's ``**/*.md`` glob indexes the nested card.
    """
    return f"{template_key.replace('::', '/')}{_CARD_SUFFIX}"
