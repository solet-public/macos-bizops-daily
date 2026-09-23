#!/usr/bin/env python3
"""Unit smoke for the M2.2 memory-curation pure functions
(``memory_curation_verbs.py`` — slug/slot-tag resolution and the
activation-ranked curation report). Covers the accepted design's two
load-bearing properties directly:

1. **MAX aggregation per line** — a line survives while ANY backing fact is
   strong (the conservative direction for traps).
2. **Pin = exemption from demotion candidacy, never reinforcement** — a
   line referencing ANY pinned fact is excluded from the candidate list
   entirely, regardless of its measured activation (the coordinator seat's sharpened
   rationale, encoded in the design note: routing pins through the
   spaced-repetition queue would corrupt the very signal this ranking
   depends on, so pinning must never touch ``strength``/``retrieval_count``
   — this smoke asserts the EXCLUSION property the source of truth for
   that guarantee).

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/memory_curation_verbs_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.memory_curation_verbs import (  # noqa: E402
    build_curation_report,
    build_fact_index,
    origin_tag,
    resolve_memory_id_by_slug,
    slug_to_slot_tag,
)
from agent_messaging_plugin.session_lifecycle_verbs import VerbError  # noqa: E402

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


def test_slug_to_slot_tag_matches_the_hydrate_convention() -> None:
    _check(
        slug_to_slot_tag("testhom", "feedback_example") == "agent_memory:slot:claude_code.testhom:feedback_example",
        "slug_to_slot_tag produces the exact tag shape "
        ".claude/hooks/memory_passthrough already writes/reads",
    )


def test_origin_tag_matches_the_export_filter() -> None:
    _check(
        origin_tag("testhom") == "agent_memory:origin:claude_code.testhom",
        "origin_tag matches export_memories's own origin-scope tag",
    )


def test_resolve_memory_id_by_slug_zero_matches_raises() -> None:
    try:
        resolve_memory_id_by_slug([], "missing-slug")
        _check(False, "zero matches raises slug_not_found")
    except VerbError as exc:
        _check(exc.code == "slug_not_found", "zero matches raises slug_not_found")


def test_resolve_memory_id_by_slug_multiple_matches_raises() -> None:
    try:
        resolve_memory_id_by_slug([{"id": "mem-1"}, {"id": "mem-2"}], "dup-slug")
        _check(False, "multiple matches raises slug_ambiguous")
    except VerbError as exc:
        _check(exc.code == "slug_ambiguous", "multiple matches raises slug_ambiguous")


def test_resolve_memory_id_by_slug_one_match_returns_id() -> None:
    result = resolve_memory_id_by_slug([{"id": "mem-abc"}], "some-slug")
    _check(result == "mem-abc", "exactly one match returns its id")


def test_build_fact_index_extracts_slug_strength_and_pin() -> None:
    records = [
        {
            "id": "mem-1", "strength": 3.5,
            "tags": ["agent_memory:slot:claude_code.testhom:trap_a", "agent_memory"],
        },
        {
            "id": "mem-2", "strength": 0.1,
            "tags": [
                "agent_memory:slot:claude_code.testhom:trap_pinned", "agent_memory:pinned",
            ],
        },
        # No matching slot tag for this origin -- must be skipped, not crash.
        {"id": "mem-3", "strength": 9.0, "tags": ["agent_memory:slot:claude_code.other:x"]},
        # Malformed tags field -- must be skipped, not crash.
        {"id": "mem-4", "strength": 1.0, "tags": "not-a-list"},
    ]
    index = build_fact_index(records, "testhom")
    _check(
        index.get("trap_a") == {"strength": 3.5, "pinned": False, "memory_id": "mem-1"},
        "an unpinned record's slug resolves to its strength/pinned/memory_id",
    )
    _check(
        index.get("trap_pinned") == {"strength": 0.1, "pinned": True, "memory_id": "mem-2"},
        "agent_memory:pinned is detected independently of the slot tag",
    )
    _check(
        "x" not in index and len(index) == 2,
        "a record with no slot tag for THIS origin, and a record with malformed "
        "tags, are both skipped rather than crashing the whole index build",
    )


_FACT_INDEX = {
    "strong": {"strength": 10.0, "pinned": False, "memory_id": "mem-strong"},
    "weak": {"strength": 0.5, "pinned": False, "memory_id": "mem-weak"},
    "weakest": {"strength": 0.1, "pinned": False, "memory_id": "mem-weakest"},
    "pinned_weak": {"strength": 0.05, "pinned": True, "memory_id": "mem-pinned"},
}


def test_build_curation_report_max_aggregates_bundled_lines() -> None:
    head_lines = ["[Strong](strong.md) · [Weak](weak.md)"]
    report = build_curation_report(
        head_lines, _FACT_INDEX, bottom_n=10, byte_budget=17_000, line_budget=132,
    )
    _check(
        len(report["demotion_candidates"]) == 1
        and report["demotion_candidates"][0]["max_strength"] == 10.0,
        "a bundled line's score is the MAX of its facts, not the min or mean -- "
        "a line survives while ANY backing fact is strong",
    )


def test_build_curation_report_excludes_any_pinned_line_from_candidacy() -> None:
    """The mutation-proof target: a line referencing a pinned fact must NEVER
    appear in demotion_candidates, even though its own (non-pinned) sibling
    fact on the same line is also weak."""
    head_lines = ["[Weakest](weakest.md)", "[Weak](weak.md) · [Pinned](pinned_weak.md)"]
    report = build_curation_report(
        head_lines, _FACT_INDEX, bottom_n=10, byte_budget=17_000, line_budget=132,
    )
    candidate_lines = [c["line"] for c in report["demotion_candidates"]]
    _check(
        "[Weakest](weakest.md)" in candidate_lines,
        "an unpinned weak line IS a demotion candidate",
    )
    _check(
        "[Weak](weak.md) · [Pinned](pinned_weak.md)" not in candidate_lines,
        "a line referencing ANY pinned fact is EXCLUDED from candidacy entirely, "
        "even though its other fact is weak and even though the pinned fact's "
        "own strength is the lowest of all -- pin means exemption from "
        "candidacy, never reinforcement, and must never be 'corrected' by "
        "letting it surface as a candidate anyway",
    )


def test_build_curation_report_reports_unresolved_lines_separately() -> None:
    head_lines = ["[Ghost](does-not-exist.md)"]
    report = build_curation_report(
        head_lines, _FACT_INDEX, bottom_n=10, byte_budget=17_000, line_budget=132,
    )
    _check(
        report["unresolved_lines"] == ["[Ghost](does-not-exist.md)"]
        and report["demotion_candidates"] == [],
        "a line whose slug resolves to nothing is reported as unresolved, "
        "never silently dropped and never scored as a candidate",
    )


def test_build_curation_report_bottom_n_truncates_ascending() -> None:
    head_lines = ["[S](strong.md)", "[Wk](weak.md)", "[Wt](weakest.md)"]
    report = build_curation_report(
        head_lines, _FACT_INDEX, bottom_n=2, byte_budget=17_000, line_budget=132,
    )
    strengths = [c["max_strength"] for c in report["demotion_candidates"]]
    _check(
        strengths == [0.1, 0.5],
        "bottom_n truncates to the N lowest-activation candidates, ascending",
    )


def test_build_curation_report_over_budget_flag() -> None:
    under = build_curation_report(["x"], {}, bottom_n=10, byte_budget=17_000, line_budget=132)
    over_bytes = build_curation_report(["x" * 100], {}, bottom_n=10, byte_budget=10, line_budget=132)
    over_lines = build_curation_report(
        ["a", "b", "c"], {}, bottom_n=10, byte_budget=17_000, line_budget=2,
    )
    _check(under["over_budget"] is False, "within both budgets -> over_budget False")
    _check(over_bytes["over_budget"] is True, "byte budget alone exceeded -> over_budget True")
    _check(over_lines["over_budget"] is True, "line budget alone exceeded -> over_budget True")


def main() -> int:
    test_slug_to_slot_tag_matches_the_hydrate_convention()
    test_origin_tag_matches_the_export_filter()
    test_resolve_memory_id_by_slug_zero_matches_raises()
    test_resolve_memory_id_by_slug_multiple_matches_raises()
    test_resolve_memory_id_by_slug_one_match_returns_id()
    test_build_fact_index_extracts_slug_strength_and_pin()
    test_build_curation_report_max_aggregates_bundled_lines()
    test_build_curation_report_excludes_any_pinned_line_from_candidacy()
    test_build_curation_report_reports_unresolved_lines_separately()
    test_build_curation_report_bottom_n_truncates_ascending()
    test_build_curation_report_over_budget_flag()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
