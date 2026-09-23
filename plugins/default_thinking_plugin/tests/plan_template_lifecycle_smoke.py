#!/usr/bin/env python3
"""SUB-01 (POR §4.5 GOAL) — plan-template curation-lifecycle smoke (no pytest).

Proves the verb-backed transition machine offline, driving the REAL plugin
delegates (``transition_plan_template`` / ``get_plan_template``) through the
real ``PlanTemplateLifecycle`` engine over an in-memory ``plan_templates`` KB
double. Cards are seeded by the REAL ``render_template_card`` so the draft
origin is authentic — there is NO ``register_plan_template`` verb (out of
SUB-01 scope; the joseki smoke likewise seeds via its register path, this one
seeds via the renderer).

The deliberate §4.5 asymmetry under test: state lives in the card's
front-matter, NOT a state table — so a transition is a read → parse → apply →
render → write of the card, with NO compare-and-set and NO retirement banner
(the ``Template State:`` line + ``state:`` tag ARE the searchable signal).

Covers:

* get on an unknown key (found=false); get on a draft (curation view)
* draft → active transitions and PERSISTS the new state to the card
  front-matter; the idempotent same-state no-op
* 'draft' rejected as a manual target; an unknown target state rejected
* illegal transitions (archived is terminal); transition on an unregistered
  template
* supersede guards: missing / self / unregistered / archived replacement;
  a valid supersession records superseded_by, persists it, and marks the
  card non-forkable
* archive from active; superseded → archived PRESERVES the replacement pointer
* an identity-mismatched card (body Template Key != the key operated on) is
  rejected, never silently operated on
* DONE-WHEN: a transitioned card round-trips through parse_template_card with
  the new front-matter state

Offline: no live solet, no LM Studio, no Postgres, no state table.

Run:
    .venv/bin/python3 \\
      plugins/default_thinking_plugin/tests/plan_template_lifecycle_smoke.py
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "default_thinking_plugin" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

importlib.import_module("ananta.core.config")  # pre-warm; avoids a latent cycle

from ananta.core.plans.plan_templates import (  # noqa: E402
    PlanTemplate,
    TemplateState,
    fork_template_to_instance,
    parse_template_card,
    render_template_card,
)
from ananta.error_handling import FrameworkError  # noqa: E402
from default_thinking_plugin.plan_template_lifecycle import _card_path  # noqa: E402
from default_thinking_plugin.plugin import DefaultThinkingPlugin  # noqa: E402

# Joseki-program handles (NOT process keys) — the reusable skeleton the
# template runs. Distinct namespace from ``service_interface::``/``plugin::``
# so the C3.1 negative-fixture detector has nothing to flag.
_PROGRAM = (
    "joseki::neuro_ambient::warm_dark_brief_lock",
    "joseki::neuro_ambient::expand_palette_to_section_family",
    "joseki::planning::close_phase_handoff",
)


def _template(
    template_key: str,
    state: TemplateState = TemplateState.DRAFT,
    superseded_by: str | None = None,
) -> PlanTemplate:
    return PlanTemplate(
        template_key=template_key,
        title=template_key.rsplit("::", 1)[-1].replace("_", " ").title(),
        goal=f"achieve the {template_key} outcome end-to-end",
        domain="neuro-ambient",
        outcome="a delivered artifact",
        program=_PROGRAM,
        binding_slots=("artifact_prefix", "target_lufs"),
        state=state,
        superseded_by=superseded_by,
    )


class InMemoryKb:
    """plan_templates KB double; a card survives across delegate calls."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def read(self, path: str) -> str:
        return self.files.get(path, "")

    def write(self, path: str, content: str) -> None:
        self.files[path] = content


def build_plugin() -> tuple[Any, InMemoryKb]:
    """Real plugin delegate over an in-memory plan_templates KB double."""
    kb = InMemoryKb()
    plugin: Any = DefaultThinkingPlugin.__new__(DefaultThinkingPlugin)
    plugin._read_from_plan_templates_kb = kb.read
    plugin._write_to_plan_templates_kb = kb.write
    return plugin, kb


def _seed(kb: InMemoryKb, template: PlanTemplate) -> None:
    """Author a card into the KB double via the REAL renderer."""
    kb.write(_card_path(template.template_key), render_template_card(template))


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


_KEY = "plan_template::neuro_ambient::warm_dark_full_piece"


def _stored_state(kb: InMemoryKb, template_key: str) -> TemplateState:
    """Re-parse the persisted card and return its front-matter state."""
    return parse_template_card(kb.read(_card_path(template_key))).state


def run() -> bool:  # noqa: PLR0915 — a flat, readable case list
    c = Checker("plan_template_lifecycle_smoke")
    plugin, kb = build_plugin()

    # get on an unknown key
    c.check(
        plugin.get_plan_template("nope")["found"] is False,
        "get on unknown key returns found=false",
    )

    # seed a draft + read the curation view
    _seed(kb, _template(_KEY))
    got = plugin.get_plan_template(_KEY)
    c.check(got["found"] and got["state"] == "draft", "get reads the draft curation view")
    c.check(got["goal"].startswith("achieve"), "get surfaces the discovery axis (goal)")
    c.check("program" not in got, "get returns the curation view, not the program body")

    # draft -> active persists to front-matter
    trans = plugin.transition_plan_template(_KEY, "active")
    c.check(
        trans["previous_state"] == "draft" and trans["state"] == "active",
        "draft -> active transitions",
    )
    c.check(trans["status"] == "transitioned", "transition reports 'transitioned'")
    c.check(_stored_state(kb, _KEY) is TemplateState.ACTIVE, "active persisted to the card")

    # idempotent no-op
    again = plugin.transition_plan_template(_KEY, "active")
    c.check(again["status"] == "unchanged", "active -> active is an idempotent no-op")

    # 'draft' is not a manual target; unknown target rejected
    c.raises(
        lambda: plugin.transition_plan_template(_KEY, "draft"),
        "draft rejected as a manual target",
        contains="not a manual",
    )
    # Reviewer-B LOW regression: a card ALREADY at draft, asked to transition
    # to 'draft', must be REJECTED — not swallowed by the idempotent no-op
    # short-circuit (which pre-fix returned status='unchanged').
    _seed(kb, _template("plan_template::na::still_draft"))
    c.raises(
        lambda: plugin.transition_plan_template("plan_template::na::still_draft", "draft"),
        "draft-card -> draft is rejected (not an idempotent no-op)",
        contains="not a manual",
    )
    c.raises(
        lambda: plugin.transition_plan_template(_KEY, "bogus"),
        "unknown target state rejected",
        contains="unknown",
    )

    # transition on an unregistered template
    c.raises(
        lambda: plugin.transition_plan_template("plan_template::na::ghost", "active"),
        "transition on unregistered template",
        contains="not registered",
    )

    _run_supersession_cases(c, plugin, kb)
    _run_archive_cases(c, plugin, kb)
    _run_identity_case(c, plugin, kb)
    _run_done_when_case(c, plugin, kb)

    return c.report()


def _run_supersession_cases(c: Checker, plugin: Any, kb: InMemoryKb) -> None:
    # active _KEY is the card being superseded; seed a draft replacement
    replacement = "plan_template::neuro_ambient::warm_dark_full_piece_v2"
    _seed(kb, _template(replacement))

    c.raises(
        lambda: plugin.transition_plan_template(_KEY, "superseded"),
        "supersede without superseded_by",
        contains="superseded_by",
    )
    c.raises(
        lambda: plugin.transition_plan_template(_KEY, "superseded", superseded_by=_KEY),
        "supersede by self",
        contains="itself",
    )
    c.raises(
        lambda: plugin.transition_plan_template(
            _KEY, "superseded", superseded_by="plan_template::na::missing",
        ),
        "supersede by unregistered replacement",
        contains="not registered",
    )
    c.raises(
        lambda: plugin.transition_plan_template(
            _KEY, "active", superseded_by=replacement,
        ),
        "superseded_by supplied for a non-supersession target is rejected",
        contains="only meaningful",
    )

    # archived replacement is rejected
    _seed(kb, _template("plan_template::na::dead", state=TemplateState.ARCHIVED))
    c.raises(
        lambda: plugin.transition_plan_template(
            _KEY, "superseded", superseded_by="plan_template::na::dead",
        ),
        "supersede by an archived replacement",
        contains="archived",
    )

    # valid supersession
    sup = plugin.transition_plan_template(_KEY, "superseded", superseded_by=replacement)
    c.check(
        sup["state"] == "superseded" and sup["superseded_by"] == replacement,
        "active -> superseded records the replacement",
    )
    card = kb.read(_card_path(_KEY))
    c.check("Template State: superseded" in card, "card front-matter shows superseded state")
    c.check(f"Superseded By: {replacement}" in card, "card records the replacement pointer")
    c.check("state:superseded" in card, "card carries the searchable state facet tag")
    # a superseded template is no longer forkable
    reparsed = parse_template_card(card)
    c.raises(
        lambda: fork_template_to_instance(
            reparsed, "pln-x", {"artifact_prefix": "p", "target_lufs": "-14"},
        ),
        "a superseded template is not forkable",
        contains="superseded",
    )

    # differing superseded_by on an already-superseded card is a conflict
    c.raises(
        lambda: plugin.transition_plan_template(
            _KEY, "superseded", superseded_by="plan_template::na::other",
        ),
        "repointing an already-superseded card is a conflict",
        contains="already superseded",
    )
    # same superseded_by is a benign no-op
    noop = plugin.transition_plan_template(_KEY, "superseded", superseded_by=replacement)
    c.check(noop["status"] == "unchanged", "re-superseding by the same key is a no-op")


def _run_archive_cases(c: Checker, plugin: Any, kb: InMemoryKb) -> None:
    # archive from active
    live = "plan_template::na::epsilon"
    _seed(kb, _template(live, state=TemplateState.ACTIVE))
    arch = plugin.transition_plan_template(live, "archived")
    c.check(arch["state"] == "archived", "active -> archived transitions")
    c.check(_stored_state(kb, live) is TemplateState.ARCHIVED, "archived persisted")
    # archived is terminal
    c.raises(
        lambda: plugin.transition_plan_template(live, "active"),
        "archived -> active is illegal (terminal)",
        contains="cannot transition",
    )
    # superseded -> archived preserves the replacement pointer
    archived_super = parse_template_card(kb.read(_card_path(_KEY)))
    plugin.transition_plan_template(_KEY, "archived")
    final = parse_template_card(kb.read(_card_path(_KEY)))
    c.check(
        final.state is TemplateState.ARCHIVED
        and final.superseded_by == archived_super.superseded_by,
        "superseded -> archived preserves the superseded_by pointer",
    )


def _run_identity_case(c: Checker, plugin: Any, kb: InMemoryKb) -> None:
    """A card whose body Template Key != the key operated on is rejected."""
    # write a card at key-A's path whose BODY declares key-B
    key_a = "plan_template::na::mismatch_row"
    key_b = "plan_template::na::other_key"
    kb.write(_card_path(key_a), render_template_card(_template(key_b)))
    c.raises(
        lambda: plugin.transition_plan_template(key_a, "active"),
        "a mismatched card (body key != row key) is rejected",
        contains="mismatched",
    )
    c.raises(
        lambda: plugin.get_plan_template(key_a),
        "get also rejects a mismatched card",
        contains="mismatched",
    )


def _run_done_when_case(c: Checker, plugin: Any, kb: InMemoryKb) -> None:
    """DONE-WHEN: a transition is a durable front-matter edit that round-trips.

    Author a fresh draft, activate it, and prove the persisted card re-parses
    to an ACTIVE template value-equal to the pre-transition one with only the
    state advanced — i.e. state is representable + searchable in the card, the
    §4.5 done-when for the verb-backed machine.
    """
    key = "plan_template::na::done_when"
    draft = _template(key)
    _seed(kb, draft)
    plugin.transition_plan_template(key, "active")
    persisted = parse_template_card(kb.read(_card_path(key)))
    c.check(persisted.state is TemplateState.ACTIVE, "persisted card parses as active")
    c.check(
        persisted.template_key == draft.template_key
        and persisted.program == draft.program
        and persisted.binding_slots == draft.binding_slots
        and persisted.goal == draft.goal,
        "only the state advanced — the rest of the card round-trips intact",
    )


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
