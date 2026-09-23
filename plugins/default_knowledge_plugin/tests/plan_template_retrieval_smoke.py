#!/usr/bin/env python3
"""Phase 6 §4.5 — plan-template KB article class retrieval smoke (no pytest).

Proves a rendered plan-template card is a first-class, SEARCHABLE KB
article by driving it through default_knowledge_plugin's REAL indexing
pipeline (the same functions the live ingest path uses):

* ``extract_article_metadata`` recognizes the ``plan_template`` article
  role, the Layer-2 classification, and the domain/state facet tags —
  so the plan-template card is a distinguishable article CLASS.
* ``chunk_by_headers`` + ``build_chunk_preamble`` produce the exact
  embedded surface a real embedder sees; that surface carries the
  discovery-by-intent language (goal/domain/outcome) and the article
  role — i.e. the card is searchable BY CONSTRUCTION.
* ``strip_article_metadata_preamble`` removes the front-matter labels
  from chunk bodies (the indexer re-adds its own preamble) while
  preserving the title and the intent body.

Honest scope: this proves the card is STRUCTURED to rank for intent
queries (searchable-by-construction) — the platform's ``test_retrieval``
target/forbidden-rank semantics are a STRONGER check this deliberately
does not attempt offline (no embedder). Live cosine ranking is NOT an
existing post-deploy check either: it first requires standing up a
plan-template KB + an example card to ingest — that ingestion + a
``test_retrieval``-style ranking assertion is §4.6 (per-type KB
lifecycles) work, not something deployable today.

Offline: pure rendering + the plugin's real indexing functions. No DB.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/plan_template_retrieval_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

from ananta.core.plans.plan_templates import (  # noqa: E402
    PLAN_TEMPLATE_ARTICLE_ROLE,
    PlanTemplate,
    TemplateState,
    render_template_card,
)
from default_knowledge_plugin.chunking import (  # noqa: E402
    chunk_by_headers,
    strip_article_metadata_preamble,
)
from default_knowledge_plugin.kb_indexing import (  # noqa: E402
    build_chunk_preamble,
    build_source_line,
    extract_article_metadata,
    extract_article_title,
)

_TEMPLATE = PlanTemplate(
    template_key="plan_template::neuro_ambient::warm_dark_full_piece",
    title="Warm-Dark Full Piece",
    goal="produce a finished warm-dark neuro-ambient piece end-to-end",
    domain="neuro-ambient",
    outcome="delivered composition (M4A + FLAC archive)",
    program=(
        "joseki::neuro_ambient::warm_dark_brief_lock",
        "expand_palette_to_section_family",
        "close_phase_handoff",
    ),
    binding_slots=("artifact_prefix", "target_lufs"),
    state=TemplateState.ACTIVE,
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

    def report(self) -> bool:
        total = self.passed + len(self.failed)
        print(f"\n=== {self.title} ===")
        print(f"passed {self.passed}/{total}")
        for f in self.failed:
            print(f"  FAIL: {f}")
        return not self.failed


def _embedded_chunks(card: str) -> list[str]:
    """Reproduce the exact per-chunk embedded surface the indexer builds."""
    title = extract_article_title(card)
    meta = extract_article_metadata(card)
    source_line = build_source_line("plan_templates", title)
    preamble = build_chunk_preamble(
        source_line, meta.role, meta.tags, meta.knowledge_layer
    )
    return [f"{preamble}\n\n{chunk}" for chunk in chunk_by_headers(card)]


def main() -> int:
    c = Checker("Phase 6 §4.5 plan-template KB article class retrieval")
    card = render_template_card(_TEMPLATE)

    # (1) The indexer recognizes the plan-template article CLASS.
    meta = extract_article_metadata(card)
    c.check(meta.role == PLAN_TEMPLATE_ARTICLE_ROLE, "article role is plan_template")
    c.check(meta.knowledge_layer == 2, "article layer is 2")
    c.check("plan-template" in meta.tags, "plan-template facet tag present")
    c.check("domain:neuro-ambient" in meta.tags, "domain facet tag present")
    c.check("state:active" in meta.tags, "state facet tag present")
    c.check(extract_article_title(card) == _TEMPLATE.title, "title extracted")

    # (2) Front-matter is stripped from chunk bodies; intent body survives.
    stripped = strip_article_metadata_preamble(card)
    c.check("Article Role:" not in stripped, "front-matter role line stripped from body")
    c.check("## Intent" in stripped, "intent section survives stripping")
    c.check(_TEMPLATE.goal in stripped, "goal text survives stripping")

    # (3) The embedded surface carries the intent + class → searchable.
    embedded = _embedded_chunks(card)
    c.check(len(embedded) >= 1, "card produces at least one indexable chunk")
    surface = "\n".join(embedded)
    c.check(_TEMPLATE.goal in surface, "embedded surface carries the goal (intent)")
    c.check(_TEMPLATE.domain in surface, "embedded surface carries the domain")
    c.check(_TEMPLATE.outcome in surface, "embedded surface carries the outcome")
    c.check(
        all(f"Article Role: {PLAN_TEMPLATE_ARTICLE_ROLE}" in e for e in embedded),
        "every chunk's preamble embeds the article class",
    )

    return 0 if c.report() else 1


if __name__ == "__main__":
    raise SystemExit(main())
