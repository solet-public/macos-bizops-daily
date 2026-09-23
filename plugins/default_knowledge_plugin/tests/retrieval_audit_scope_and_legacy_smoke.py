#!/usr/bin/env python3
"""Hermetic smoke for the audit path's ratified-scope split and LEGACY_KEY finding.

Guards two additions to ``plugins/default_knowledge_plugin/src/default_knowledge_plugin/
retrieval_audit.py`` (Lane A, 2026-08-14):

1. ``run_case_under_scope_plan`` -- the one merge helper that gives the audit's
   ``_run_single_audit`` and the ``run_retrieval_companions.py`` runner the SAME
   ratified split (``RATIFIED_SCOPE_PLAN``: targets judged within the article's
   own KB, forbidden judged across every active one). Both consumers import
   ``ScopePolicy`` / ``ScopePlan`` / ``RATIFIED_SCOPE_PLAN`` / ``RETIRED_KEYS``
   from this one module -- this smoke proves that with object identity, not just
   equal values, so a future edit that forks one copy from the other cannot pass
   silently.
2. LEGACY_KEY as a first-class audit finding. ``parse_test_file`` stays tolerant
   of the retired ``non_target_queries`` key (coerces it to an empty forbidden
   set exactly as before -- never raises), but now records the fact on
   ``AuditCase.legacy_keys``, and ``aggregate_audit`` fails the article on it
   even when every live query it can still assert passes -- the defect this
   closes: five companions on the retired spelling were reporting PASSING with
   their overreach claims silently unenforced.

Offline by construction: every case runs against a temp-dir fixture or a
hand-built report payload, never a live solet. The audit verb's live half
(``self.test_retrieval``) is exercised only through the injected ``call``
stub here, same convention as ``retrieval_companion_runner_smoke.py``.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/retrieval_audit_scope_and_legacy_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "tools"))

# The companion runner is origin tooling (**/tools/ never ships in a seed), so
# a born clone legitimately lacks it. Guarded import: at origin every leg runs;
# in a clone the runner-identity checks are skipped WITH a printed reason and
# the audit-side legs (whose subject ships) still run.
try:
    import run_retrieval_companions  # noqa: E402
    runner = run_retrieval_companions
except ModuleNotFoundError:  # born clone — tools/ pruned by the seed manifest
    runner = None
    print("NOTE: run_retrieval_companions absent (origin tooling, pruned from "
          "seeds) — runner-identity checks skipped; audit-side legs still run")
from default_knowledge_plugin.retrieval_audit import (  # noqa: E402
    RATIFIED_SCOPE_PLAN,
    RETIRED_KEYS,
    AuditCase,
    ScopePlan,
    ScopePolicy,
    aggregate_audit,
    parse_test_file,
    render_markdown,
    run_case_under_scope_plan,
)


class Checker:
    """Minimal pass/fail recorder shared with the sibling knowledge-plugin smokes."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: object, label: str) -> None:
        """Record one assertion."""
        if condition:
            self.passed += 1
        else:
            self.failed.append(label)

    def report(self) -> bool:
        """Print the tally and return whether every assertion passed."""
        total = self.passed + len(self.failed)
        print(f"\n=== {self.title} ===")
        print(f"passed {self.passed}/{total}")
        for failure in self.failed:
            print(f"  FAIL: {failure}")
        return not self.failed


def _case(
    *,
    article_path: str = "knowledge_bases/demo_kb/01_demo.md",
    target_queries: list[str] | None = None,
    forbidden_queries: list[str] | None = None,
    legacy_keys: tuple[str, ...] = (),
) -> AuditCase:
    return AuditCase(
        article_path=article_path,
        target_queries=target_queries if target_queries is not None else ["t"],
        forbidden_queries=forbidden_queries if forbidden_queries is not None else [],
        min_rank=3,
        forbidden_min_rank=4,
        process_key_assertions=[],
        legacy_keys=legacy_keys,
        source_file="/dev/null",
    )


def check_shared_definition_identity(checker: Checker) -> None:
    """The runner tool imports the SAME objects, not equal-but-separate copies."""
    if runner is None:
        print("  SKIP: runner-identity checks (origin tooling absent in this tree)")
        return
    checker.check(
        runner.ScopePolicy is ScopePolicy,
        "run_retrieval_companions.ScopePolicy IS retrieval_audit.ScopePolicy (one definition)",
    )
    checker.check(
        runner.ScopePlan is ScopePlan,
        "run_retrieval_companions.ScopePlan IS retrieval_audit.ScopePlan (one definition)",
    )
    checker.check(
        runner.RATIFIED_SCOPE_PLAN is RATIFIED_SCOPE_PLAN,
        "run_retrieval_companions.RATIFIED_SCOPE_PLAN IS retrieval_audit.RATIFIED_SCOPE_PLAN "
        "(the same object, not a re-declared equal one)",
    )
    checker.check(
        runner.RETIRED_KEYS is RETIRED_KEYS,
        "run_retrieval_companions.RETIRED_KEYS IS retrieval_audit.RETIRED_KEYS (one definition)",
    )
    checker.check(
        RATIFIED_SCOPE_PLAN.target_scope is ScopePolicy.OWN_KB
        and RATIFIED_SCOPE_PLAN.forbidden_scope is ScopePolicy.ALL_ACTIVE,
        "the ratified plan is still targets=own_kb, forbidden=all_active",
    )


def check_scope_plan_merge(checker: Checker) -> None:
    """``run_case_under_scope_plan`` issues one or two calls and merges correctly."""
    calls: list[tuple[ScopePolicy, tuple[str, ...], tuple[str, ...]]] = []

    def split_call(
        scope: ScopePolicy, target_queries: list[str], forbidden_queries: list[str],
    ) -> dict[str, Any]:
        calls.append((scope, tuple(target_queries), tuple(forbidden_queries)))
        if scope is ScopePolicy.OWN_KB:
            return {
                "target_results": [{"query": "t", "observed_rank": 1}],
                "forbidden_results": [{"query": "OWN_KB-should-not-survive", "observed_rank": 1}],
                "process_key_freshness": [{"referenced_key": "svc::a", "exists_in_registry": True}],
            }
        return {
            "target_results": [{"query": "ALL_ACTIVE-should-not-survive", "observed_rank": 1}],
            "forbidden_results": [{"query": "f", "observed_rank": 9}],
            "process_key_freshness": [{"referenced_key": "svc::b", "exists_in_registry": False}],
        }

    result = run_case_under_scope_plan(RATIFIED_SCOPE_PLAN, ["t"], ["f"], split_call)
    checker.check(len(calls) == 2, "a split plan (target != forbidden scope) issues exactly two calls")
    checker.check(
        calls[0] == (ScopePolicy.OWN_KB, ("t",), ()),
        "the own-KB call carries the target queries and NO forbidden queries",
    )
    checker.check(
        calls[1] == (ScopePolicy.ALL_ACTIVE, (), ("f",)),
        "the all-active call carries the forbidden queries and NO target queries",
    )
    checker.check(
        result["target_results"] == [{"query": "t", "observed_rank": 1}],
        "merged target_results come from the OWN_KB call",
    )
    checker.check(
        result["forbidden_results"] == [{"query": "f", "observed_rank": 9}],
        "merged forbidden_results come from the ALL_ACTIVE call, overriding the OWN_KB call's placeholder",
    )
    checker.check(
        result["process_key_freshness"] == [{"referenced_key": "svc::a", "exists_in_registry": True}],
        "process_key_freshness comes from the call that carried the target queries -- "
        "never double-counted from the forbidden call",
    )

    calls.clear()
    uniform = ScopePlan(target_scope=ScopePolicy.ALL_ACTIVE, forbidden_scope=ScopePolicy.ALL_ACTIVE)

    def uniform_call(
        scope: ScopePolicy, target_queries: list[str], forbidden_queries: list[str],
    ) -> dict[str, Any]:
        calls.append((scope, tuple(target_queries), tuple(forbidden_queries)))
        return {
            "target_results": [{"query": "t", "observed_rank": 2}],
            "forbidden_results": [{"query": "f", "observed_rank": 8}],
            "process_key_freshness": [],
        }

    uniform_result = run_case_under_scope_plan(uniform, ["t"], ["f"], uniform_call)
    checker.check(len(calls) == 1, "a uniform plan (same scope both kinds) issues exactly one call")
    checker.check(
        calls[0] == (ScopePolicy.ALL_ACTIVE, ("t",), ("f",)),
        "the single call carries BOTH target and forbidden queries",
    )
    checker.check(
        uniform_result["target_results"] == [{"query": "t", "observed_rank": 2}]
        and uniform_result["forbidden_results"] == [{"query": "f", "observed_rank": 8}],
        "a uniform plan's single payload passes through untouched",
    )


def check_legacy_key_parsing(checker: Checker) -> None:
    """The retired key is still tolerated (never raises), but recorded."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        legacy_file = root / "legacy.retrieval_test.yaml"
        legacy_file.write_text(
            "article_path: knowledge_bases/demo_kb/01_demo.md\n"
            'target_queries:\n  - "t"\n'
            'non_target_queries:\n  - "should be ignored, not read as forbidden"\n',
            encoding="utf-8",
        )
        clean_file = root / "clean.retrieval_test.yaml"
        clean_file.write_text(
            "article_path: knowledge_bases/demo_kb/02_demo.md\n"
            'target_queries:\n  - "t"\n'
            'forbidden_queries:\n  - "f"\n',
            encoding="utf-8",
        )

        legacy_case = parse_test_file(legacy_file)
        checker.check(
            legacy_case.legacy_keys == ("non_target_queries",),
            "a companion declaring the retired key records it on legacy_keys",
        )
        checker.check(
            legacy_case.forbidden_queries == [],
            "the retired key's claims are still coerced to an empty forbidden set (tolerant, not raising)",
        )

        clean_case = parse_test_file(clean_file)
        checker.check(
            clean_case.legacy_keys == (),
            "a companion using the current spelling records no legacy keys",
        )


def check_aggregate_fails_on_legacy_key(checker: Checker) -> None:
    """A LEGACY_KEY finding fails its article even when the live report is clean."""
    clean_report = {
        "target_results": [{"query": "t", "observed_rank": 1}],
        "forbidden_results": [],
        "process_key_freshness": [],
    }

    legacy = aggregate_audit(
        cases=[_case(legacy_keys=("non_target_queries",))],
        run_single=lambda case: clean_report,
        fail_fast=False,
    )
    checker.check(legacy.failed == 1 and legacy.passed == 0, "a legacy-key companion fails, never reports PASSING")
    checker.check(
        legacy.legacy_keys
        == [
            {
                "article_path": "knowledge_bases/demo_kb/01_demo.md",
                "legacy_key": "non_target_queries",
                "replacement": "forbidden_queries",
            }
        ],
        "the LEGACY_KEY finding names the article, the retired key, and its replacement",
    )
    checker.check(legacy.passing_articles == [], "a legacy-key article is excluded from the passing list")

    clean = aggregate_audit(
        cases=[_case(legacy_keys=())],
        run_single=lambda case: clean_report,
        fail_fast=False,
    )
    checker.check(
        clean.passed == 1 and clean.failed == 0 and clean.legacy_keys == [],
        "a companion with no legacy key and a clean report still passes normally (no regression)",
    )


def check_render_markdown_legacy_section(checker: Checker) -> None:
    """The Markdown report gets a LEGACY_KEY section, present even when empty."""
    empty = aggregate_audit(cases=[], run_single=lambda case: {}, fail_fast=False)
    empty_doc = render_markdown(
        aggregate=empty, ran_at="2026-08-14T00:00:00Z", corpus_root="knowledge_bases",
        report_path="/tmp/report.md", duration_seconds=0.1, total_fixtures_discovered=0,
    )
    checker.check("## LEGACY_KEY" in empty_doc, "the report always carries a LEGACY_KEY section")
    legacy_section_index = empty_doc.index("## LEGACY_KEY")
    checker.check(
        "None." in empty_doc[legacy_section_index:legacy_section_index + 40],
        "an empty legacy-key list renders as 'None.', matching the other finding sections",
    )

    populated = aggregate_audit(
        cases=[_case(legacy_keys=("non_target_queries",))],
        run_single=lambda case: {"target_results": [], "forbidden_results": [], "process_key_freshness": []},
        fail_fast=False,
    )
    populated_doc = render_markdown(
        aggregate=populated, ran_at="2026-08-14T00:00:00Z", corpus_root="knowledge_bases",
        report_path="/tmp/report.md", duration_seconds=0.1, total_fixtures_discovered=1,
    )
    checker.check(
        "non_target_queries" in populated_doc and "forbidden_queries" in populated_doc,
        "a populated LEGACY_KEY section names both the retired key and its replacement",
    )
    checker.check(
        "knowledge_bases/demo_kb/01_demo.md" in populated_doc,
        "a populated LEGACY_KEY section names the article",
    )


def main() -> int:
    checker = Checker("retrieval audit scope split + legacy-key finding")
    check_shared_definition_identity(checker)
    check_scope_plan_merge(checker)
    check_legacy_key_parsing(checker)
    check_aggregate_fails_on_legacy_key(checker)
    check_render_markdown_legacy_section(checker)
    return 0 if checker.report() else 1


if __name__ == "__main__":
    raise SystemExit(main())
