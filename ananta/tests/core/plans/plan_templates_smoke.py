#!/usr/bin/env python3
"""Phase 6 §4.5 — plan template / instance fork-by-intent smoke (no pytest).

Proves the in-lane §4.5 core offline:

* ``PlanTemplate`` / ``PlanInstance`` are distinct frozen value objects;
  a template's lifecycle ``state`` is pure curation metadata (no run
  evidence — templates don't run).
* ``fork_template_to_instance`` is a PURE transform: it binds the
  template's open slots to per-run values, records ``template_key``
  provenance, and NEVER mutates the template (the §4.5 done-when).
* Fast-fail bindings contract: missing slot, unknown binding, empty
  instance_id, and forking a superseded/archived template all raise.
* ``derive_discovery_metadata`` projects the four discovery-by-intent
  axes identically from a template and from a forked instance, and
  ``as_front_matter`` renders the KB-card shape.

Offline: pure value objects, no live solet, no DB.

Run:
    .venv/bin/python3 ananta/tests/core/plans/plan_templates_smoke.py
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.plans.plan_templates import (  # noqa: E402
    PLAN_TEMPLATE_ARTICLE_ROLE,
    TEMPLATE_LEGAL_TRANSITIONS,
    PlanDiscoveryMetadata,
    PlanTemplate,
    TemplateState,
    derive_discovery_metadata,
    fork_template_to_instance,
    parse_template_card,
    render_template_card,
    transition_template,
)
from ananta.error_handling import FrameworkError  # noqa: E402

# Joseki-program handles (NOT process keys) — the reusable skeleton the
# template runs. Distinct namespace from ``service_interface::``/``plugin::``
# so the C3.1 negative-fixture detector has nothing to flag.
_PROGRAM = (
    "joseki::neuro_ambient::warm_dark_brief_lock",
    "expand_palette_to_section_family",
    "close_phase_handoff",
)


def _template(state: TemplateState, superseded_by: str | None = None) -> PlanTemplate:
    return PlanTemplate(
        template_key="plan_template::neuro_ambient::warm_dark_full_piece",
        title="Warm-Dark Full Piece",
        goal="produce a finished warm-dark neuro-ambient piece end-to-end",
        domain="neuro-ambient",
        outcome="delivered composition (M4A + FLAC archive)",
        program=_PROGRAM,
        binding_slots=("artifact_prefix", "target_lufs"),
        state=state,
        superseded_by=superseded_by,
    )


class Checker:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: object, label: str) -> None:
        if condition:
            self.passed += 1
        else:
            self.failed.append(label)

    def raises(self, fn: Any, label: str, *, contains: str = "") -> None:
        try:
            fn()
        except FrameworkError as exc:
            if contains and contains.lower() not in str(exc).lower():
                self.failed.append(f"{label}: message missing {contains!r} ({exc})")
            else:
                self.passed += 1
        except Exception as exc:  # noqa: BLE001
            self.failed.append(f"{label}: wrong exception type {type(exc).__name__}")
        else:
            self.failed.append(f"{label}: expected FrameworkError, none raised")

    def report(self) -> bool:
        total = self.passed + len(self.failed)
        print(f"\n=== {self.title} ===")
        print(f"passed {self.passed}/{total}")
        for f in self.failed:
            print(f"  FAIL: {f}")
        return not self.failed


def _run_state_cases(c: Checker) -> None:
    c.check(TemplateState.DRAFT.is_forkable, "draft is forkable")
    c.check(TemplateState.ACTIVE.is_forkable, "active is forkable")
    c.check(not TemplateState.SUPERSEDED.is_forkable, "superseded is not forkable")
    c.check(not TemplateState.ARCHIVED.is_forkable, "archived is not forkable")


def _run_happy_fork(c: Checker) -> None:
    template = _template(TemplateState.ACTIVE)
    snapshot = _template(TemplateState.ACTIVE)  # value-equal reference
    bindings = {"artifact_prefix": "warm_dark_001", "target_lufs": "-14"}

    instance = fork_template_to_instance(template, "pln-warmdark-001", bindings)

    # Instance and template are genuinely distinct types (slots=True): an
    # instance carries bindings/provenance a template lacks and vice versa.
    c.check(
        not hasattr(instance, "binding_slots") and not hasattr(instance, "state"),
        "instance is a distinct type from template (no template-only fields)",
    )
    c.check(instance.instance_id == "pln-warmdark-001", "instance carries the supplied id")
    c.check(instance.template_key == template.template_key, "instance records template provenance")
    c.check(instance.bindings_map == bindings, "instance binds exactly the supplied values")
    c.check(instance.program == _PROGRAM, "instance carries the joseki program forward")
    c.check(
        instance.bindings == (("artifact_prefix", "warm_dark_001"), ("target_lufs", "-14")),
        "bindings preserve slot order",
    )

    # Template is untouched by the fork (the §4.5 done-when).
    c.check(template == snapshot, "template is unchanged (value-equal) after fork")
    c.check(template.binding_slots == ("artifact_prefix", "target_lufs"), "template slots intact")

    # draft is forkable too
    draft_instance = fork_template_to_instance(_template(TemplateState.DRAFT), "pln-x", bindings)
    c.check(draft_instance.template_key == template.template_key, "draft template forks")


def _run_fastfail_cases(c: Checker) -> None:
    good = {"artifact_prefix": "p", "target_lufs": "-14"}
    active = _template(TemplateState.ACTIVE)

    c.raises(
        lambda: fork_template_to_instance(active, "", good),
        "empty instance_id raises",
        contains="instance_id",
    )
    c.raises(
        lambda: fork_template_to_instance(active, "pln-1", {"artifact_prefix": "p"}),
        "missing required slot raises",
        contains="missing",
    )
    c.raises(
        lambda: fork_template_to_instance(active, "pln-1", {**good, "bogus": "x"}),
        "unknown binding raises",
        contains="unknown",
    )
    c.raises(
        lambda: fork_template_to_instance(
            _template(TemplateState.SUPERSEDED, superseded_by="plan_template::na::v2"),
            "pln-1",
            good,
        ),
        "forking a superseded template raises",
        contains="superseded",
    )
    c.raises(
        lambda: fork_template_to_instance(_template(TemplateState.ARCHIVED), "pln-1", good),
        "forking an archived template raises",
        contains="archived",
    )


def _run_discovery_cases(c: Checker) -> None:
    template = _template(TemplateState.ACTIVE)
    instance = fork_template_to_instance(
        template, "pln-1", {"artifact_prefix": "p", "target_lufs": "-14"}
    )

    expected = PlanDiscoveryMetadata(
        goal=template.goal,
        domain=template.domain,
        outcome=template.outcome,
        program=_PROGRAM,
    )
    c.check(derive_discovery_metadata(template) == expected, "derive from template projects the axes")
    c.check(
        derive_discovery_metadata(instance) == expected,
        "derive from instance projects the same axes (metadata travels through fork)",
    )

    front = expected.as_front_matter()
    c.check(
        front == {
            "goal": template.goal,
            "domain": template.domain,
            "outcome": template.outcome,
            "program": list(_PROGRAM),
        },
        "as_front_matter renders the KB-card shape",
    )
    c.check(isinstance(front["program"], list), "front-matter program is a plain list")


def _run_immutability_cases(c: Checker) -> None:
    template = _template(TemplateState.ACTIVE)
    try:
        template.state = TemplateState.ARCHIVED  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        c.check(True, "PlanTemplate is frozen (attribute assignment raises)")
    except Exception as exc:  # noqa: BLE001
        c.check(False, f"unexpected exception on frozen set: {type(exc).__name__}")
    else:
        c.check(False, "expected FrozenInstanceError on frozen PlanTemplate set")

    c.check(hash(template) == hash(_template(TemplateState.ACTIVE)), "PlanTemplate is hashable + stable")


def _run_card_cases(c: Checker) -> None:
    # Active template round-trips through render → parse unchanged.
    template = _template(TemplateState.ACTIVE)
    card = render_template_card(template)
    c.check(f"Article Role: {PLAN_TEMPLATE_ARTICLE_ROLE}" in card, "card declares the article role")
    c.check("Article Layer: 2" in card, "card declares the layer")
    c.check(template.goal in card, "card body carries the goal (searchable intent)")
    c.check(template.outcome in card, "card body carries the outcome")
    c.check(all(key in card for key in _PROGRAM), "card lists the joseki program")
    c.check(all(slot in card for slot in template.binding_slots), "card lists binding slots")
    c.check(parse_template_card(card) == template, "render → parse round-trips (active)")

    # Superseded template round-trips WITH its replacement pointer.
    superseded = _template(TemplateState.SUPERSEDED, superseded_by="plan_template::na::v2")
    c.check(
        parse_template_card(render_template_card(superseded)) == superseded,
        "render → parse round-trips (superseded_by preserved)",
    )

    # A parsed card can be forked (the "old plans are forkable" path).
    reparsed = parse_template_card(card)
    forked = fork_template_to_instance(
        reparsed, "pln-reparsed", {"artifact_prefix": "p", "target_lufs": "-14"}
    )
    c.check(forked.template_key == template.template_key, "a parsed card forks by intent")

    # POR §4.5 "so old plans are forkable" — the provenance chain, closed
    # visibly: an instance's template_key is the handle → resolve the template
    # (here: the card we just parsed; in production: fetch the persisted card
    # by key = §4.6 KB wiring) → re-fork. Same key + program + bindings ⇒ the
    # old plan re-forks without touching the template.
    c.check(forked.template_key == reparsed.template_key, "instance provenance points back to its template")
    refork = fork_template_to_instance(reparsed, "pln-refork", forked.bindings_map)
    c.check(
        refork.template_key == forked.template_key
        and refork.program == forked.program
        and refork.bindings == forked.bindings,
        "re-forking the template resolved by template_key reproduces the instance shape",
    )

    # Fast-fail parses.
    c.raises(
        lambda: parse_template_card("# Title only\n\nArticle Role: plan_template\n"),
        "card missing required fields raises",
    )
    bad_state = card.replace("Template State: active", "Template State: bogus")
    c.raises(
        lambda: parse_template_card(bad_state),
        "card with unknown state raises",
        contains="state",
    )


def _run_construction_invariant_cases(c: Checker) -> None:
    """Fix-round (Codex blocker + Day rulings): single-line scalars, non-empty
    program at construction AND parse, empty binding_slots valid."""

    def _tpl(**over: object) -> PlanTemplate:
        base: dict[str, object] = {
            "template_key": "plan_template::na::x",
            "title": "X",
            "goal": "g",
            "domain": "d",
            "outcome": "o",
            "program": _PROGRAM,
            "binding_slots": ("a",),
            "state": TemplateState.ACTIVE,
        }
        base.update(over)
        return PlanTemplate(**base)  # type: ignore[arg-type]

    # Colon-in-value survives round-trip (the value's colon is not a delimiter).
    colon = _template(TemplateState.ACTIVE)
    colon = PlanTemplate(
        template_key=colon.template_key,
        title=colon.title,
        goal="produce X: a warm, evolving piece",
        domain=colon.domain,
        outcome=colon.outcome,
        program=colon.program,
        binding_slots=colon.binding_slots,
        state=colon.state,
    )
    c.check(
        parse_template_card(render_template_card(colon)).goal == "produce X: a warm, evolving piece",
        "colon-in-value round-trips intact",
    )

    # Newline/control-char scalars REJECTED at construction, error names the field.
    c.raises(lambda: _tpl(goal="line one\nline two"), "newline goal rejected", contains="goal")
    c.raises(lambda: _tpl(title="a\tb"), "control-char title rejected", contains="title")
    c.raises(
        lambda: _tpl(program=("ok", "bad\nkey")), "newline program entry rejected", contains="program"
    )
    c.raises(
        lambda: PlanDiscoveryMetadata(goal="a\nb", domain="d", outcome="o", program=_PROGRAM),
        "PlanDiscoveryMetadata rejects a multiline axis",
        contains="goal",
    )

    # Non-empty program required at BOTH ends.
    c.raises(lambda: _tpl(program=()), "empty program rejected at construction", contains="program")
    empty_prog_card = render_template_card(_template(TemplateState.ACTIVE)).replace(
        "## Joseki Program\n\n1. joseki::neuro_ambient::warm_dark_brief_lock\n"
        "2. expand_palette_to_section_family\n3. close_phase_handoff",
        "## Joseki Program\n",
    )
    c.raises(
        lambda: parse_template_card(empty_prog_card),
        "empty joseki program rejected at parse",
        contains="program",
    )

    # Empty binding_slots is VALID (ruling c): constructs, forks with an empty map, round-trips.
    no_slots = _tpl(binding_slots=())
    c.check(no_slots.binding_slots == (), "empty binding_slots constructs")
    inst = fork_template_to_instance(no_slots, "pln-noslots", {})
    c.check(inst.bindings == (), "fully-bound template forks with an empty bindings map")
    c.check(
        parse_template_card(render_template_card(no_slots)) == no_slots,
        "empty-binding-slots template round-trips",
    )


def _run_transition_cases(c: Checker) -> None:
    """SUB-01 pure ``transition_template`` state machine (no I/O, no state table).

    Curation lifecycle ``draft → active → superseded → archived`` with the
    documented asymmetries vs joseki: no ``proven``, no CAS, ``draft`` is the
    origin (never a manual target). The transform is pure — it returns a new
    frozen template and never mutates the source.
    """
    # Legal-transition map sanity: draft is the origin (never a target); the
    # three manual targets each declare their legal source states.
    c.check(TemplateState.DRAFT not in TEMPLATE_LEGAL_TRANSITIONS, "draft is not a manual target")
    c.check(
        TEMPLATE_LEGAL_TRANSITIONS[TemplateState.ACTIVE] == frozenset({TemplateState.DRAFT}),
        "active is reachable only from draft",
    )
    c.check(
        TemplateState.SUPERSEDED in TEMPLATE_LEGAL_TRANSITIONS[TemplateState.ARCHIVED],
        "archived is reachable from superseded",
    )

    draft = _template(TemplateState.DRAFT)
    snapshot = _template(TemplateState.DRAFT)  # value-equal reference

    # draft -> active is pure: new value object, source untouched.
    active = transition_template(draft, target_state=TemplateState.ACTIVE)
    c.check(active.state is TemplateState.ACTIVE, "draft -> active advances the state")
    c.check(draft == snapshot, "transition_template never mutates the source (frozen)")
    c.check(active.template_key == draft.template_key, "identity + program carry through")

    # active -> superseded requires a real, non-self replacement.
    c.raises(
        lambda: transition_template(active, target_state=TemplateState.SUPERSEDED),
        "supersede without superseded_by raises",
        contains="superseded_by",
    )
    c.raises(
        lambda: transition_template(
            active, target_state=TemplateState.SUPERSEDED, superseded_by=active.template_key,
        ),
        "self-supersede raises",
        contains="itself",
    )
    superseded = transition_template(
        active, target_state=TemplateState.SUPERSEDED, superseded_by="plan_template::na::v2",
    )
    c.check(
        superseded.state is TemplateState.SUPERSEDED
        and superseded.superseded_by == "plan_template::na::v2",
        "supersede records the replacement pointer",
    )

    # superseded -> archived PRESERVES the replacement pointer.
    archived = transition_template(superseded, target_state=TemplateState.ARCHIVED)
    c.check(
        archived.state is TemplateState.ARCHIVED
        and archived.superseded_by == "plan_template::na::v2",
        "superseded -> archived preserves superseded_by",
    )

    # 'draft' is not a manual target; illegal source rejected; stray superseded_by rejected.
    c.raises(
        lambda: transition_template(active, target_state=TemplateState.DRAFT),
        "draft rejected as a manual target",
        contains="not a manual",
    )
    c.raises(
        lambda: transition_template(archived, target_state=TemplateState.ACTIVE),
        "archived -> active is illegal",
        contains="cannot transition",
    )
    c.raises(
        lambda: transition_template(
            draft, target_state=TemplateState.ACTIVE, superseded_by="plan_template::na::v2",
        ),
        "superseded_by on a non-supersession target rejected",
        contains="only meaningful",
    )


def main() -> int:
    c = Checker("Phase 6 §4.5 plan template / instance fork-by-intent")
    _run_state_cases(c)
    _run_happy_fork(c)
    _run_fastfail_cases(c)
    _run_discovery_cases(c)
    _run_immutability_cases(c)
    _run_card_cases(c)
    _run_construction_invariant_cases(c)
    _run_transition_cases(c)
    return 0 if c.report() else 1


if __name__ == "__main__":
    raise SystemExit(main())
