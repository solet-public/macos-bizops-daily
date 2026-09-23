#!/usr/bin/env python3
"""Smoke — default-scope exclusion + explicit-name bypass (W5, Architect Q1).

An excluded KB (workbench / thinking_plans / thinking_playbooks) is dropped from
DEFAULT semantic + diversity search but reachable with explicit ``name=`` scoping.
``collect_tiered_results`` takes ``honor_exclusions`` as a REQUIRED keyword-only
parameter (condition C2) that gates the ``searchable`` list feeding the semantic
tier and diversity fill; the process-key (Tier 1) and tag (Tier 2) tiers run on
the unfiltered ``active_names`` and so always include excluded KBs. The behavior
change is narrow: an untagged semantic explicit-name search of an excluded KB
goes from empty-by-construction to returning results.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/search_exclusion_bypass_smoke.py
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

from default_knowledge_plugin.constants import (  # noqa: E402
    SEARCH_EXCLUDED_KB_NAMES,
    TAG_DOMAIN_OFFICIAL,
    TAG_PREFIX_SCOPE,
    Scope,
    document_tag,
    kb_id_tag,
)
from default_knowledge_plugin.kb_search import collect_tiered_results  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _mem(kb_name: str, doc: str, score: float) -> dict[str, Any]:
    return {
        "id": f"mem-{kb_name}",
        "content": f"Source: {kb_name} — {doc}\n# {doc}\nbody",
        "similarity": score,
        "tags": [
            TAG_DOMAIN_OFFICIAL,
            kb_id_tag(kb_name),
            f"{TAG_PREFIX_SCOPE}{Scope.WORKSPACE.value}",
            document_tag(f"{doc}.md"),
        ],
    }


class _FakeMemory:
    """Returns one workbench + one non-excluded memory for every recall call."""

    def __init__(self) -> None:
        self.candidates = [_mem("workbench", "Foo", 0.9), _mem("ananta_platform", "Bar", 0.8)]

    def recall(
        self, query: str, top_k: int, tags: list[str] | None = None,
        exclude_ids: list[str] | None = None, score_by_similarity: bool = True,
    ) -> dict[str, Any]:
        del query, top_k, tags, score_by_similarity
        excluded = set(exclude_ids or [])
        return {"memories": [m for m in self.candidates if m["id"] not in excluded]}


_ACTIVE = ["workbench", "ananta_platform"]


def _kb_names(results: list[dict[str, Any]]) -> set[str]:
    return {r.get("knowledge_base", "") for r in results}


def test_workbench_is_excluded() -> None:
    _check("workbench" in SEARCH_EXCLUDED_KB_NAMES, "workbench registered in SEARCH_EXCLUDED_KB_NAMES")


def test_default_semantic_excludes_workbench() -> None:
    results = collect_tiered_results(
        "q", _ACTIVE, 12, None, None, _FakeMemory(), honor_exclusions=True,
    )
    names = _kb_names(results)
    _check("workbench" not in names, "default semantic search EXCLUDES workbench")
    _check("ananta_platform" in names, "default semantic search still returns non-excluded KBs")


def test_explicit_name_bypasses_exclusion() -> None:
    results = collect_tiered_results(
        "q", _ACTIVE, 12, None, None, _FakeMemory(), honor_exclusions=False,
    )
    _check("workbench" in _kb_names(results), "explicit name= scope INCLUDES workbench (bypass)")


def test_tag_tier_includes_excluded() -> None:
    results = collect_tiered_results(
        "q", _ACTIVE, 12, None, ["knowledge:tag:whatever"], _FakeMemory(), honor_exclusions=True,
    )
    _check(
        "workbench" in _kb_names(results),
        "Tier-2 tag search includes workbench even under honor_exclusions (runs on active_names)",
    )


def test_honor_exclusions_is_required_keyword_only() -> None:
    sig = inspect.signature(collect_tiered_results)
    param = sig.parameters.get("honor_exclusions")
    _check(param is not None, "collect_tiered_results has honor_exclusions parameter")
    _check(
        param is not None
        and param.kind == inspect.Parameter.KEYWORD_ONLY
        and param.default is inspect.Parameter.empty,
        "honor_exclusions is keyword-only with NO default (condition C2)",
    )


def main() -> int:
    print("search exclusion + explicit-name bypass smoke (W5)")
    print("==================================================")
    test_workbench_is_excluded()
    test_default_semantic_excludes_workbench()
    test_explicit_name_bypasses_exclusion()
    test_tag_tier_includes_excluded()
    test_honor_exclusions_is_required_keyword_only()
    print(f"\nPASSED: {_passed}\nFAILED: {len(_failed)}")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
