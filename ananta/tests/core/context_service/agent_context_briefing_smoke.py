#!/usr/bin/env python3
"""Phase 2 — agent-context briefing shape + provenance (offline, no pytest).

Protects the Phase 2 done-when: an agent gets the grounding classes a Qwen
prompt would have gotten (process catalog, plan state, guidance, answer
contract), structured with provenance, **no local model required**. This smoke
exercises the PURE boundary function
``core.prompts.agent_context_briefing.group_blocks_into_briefing`` with
hand-built ``MessageBlock``s — zero pipeline, zero orchestrator, zero inference —
so the deterministic briefing shape + provenance + the budget contract are
frozen offline.

Run:
    .venv/bin/python3 ananta/tests/core/context_service/agent_context_briefing_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.prompts.agent_context_briefing import (  # noqa: E402
    BUNDLE_ANSWER_CONTRACT,
    BUNDLE_CONVERSATION,
    BUNDLE_FRAME,
    BUNDLE_GUIDANCE,
    BUNDLE_NAMES,
    BUNDLE_OTHER,
    BUNDLE_PLAN_STATE,
    BUNDLE_PROCESS_CATALOG,
    group_blocks_into_briefing,
)
from ananta.core.prompts.context import (  # noqa: E402
    ContextLayer,
    HistoryKind,
    MessageBlock,
    ReasoningSlot,
    SourceKind,
    SourceReference,
    TransitionBehavior,
)

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


# Valid MessageBlock combos (satisfy context.py construction invariants), one per bundle.
_SPECS: tuple[
    tuple[SourceKind, ReasoningSlot, ContextLayer, TransitionBehavior, bool, HistoryKind, str, str, str],
    ...,
] = (
    # source_kind, slot, layer, transition, ephemeral, history_kind, prov_kind, prov_ref, expect_bundle
    ("system_template", "static_frame", "ossified_context", "stable", False, "none",
     "template", "system.md", BUNDLE_FRAME),
    ("persisted_event", "settled_history", "ossified_context", "stable", False, "input_event",
     "event_id", "evt-1", BUNDLE_CONVERSATION),
    ("focus_state", "working_state", "living_context", "replaced", True, "none",
     "focus_buffer", "plan-view", BUNDLE_PLAN_STATE),
    ("prompt_asset", "working_evidence", "ossified_context", "stable", False, "none",
     "article", "guidance/x.md", BUNDLE_GUIDANCE),
    ("process_catalog", "static_frame", "ossified_context", "stable", False, "none",
     "process_key", "service_interface::knowledge_service::search", BUNDLE_PROCESS_CATALOG),
    ("discovered_schema", "static_frame", "ossified_context", "stable", False, "none",
     "process_key", "service_interface::thinking_service::upsert_plan", BUNDLE_ANSWER_CONTRACT),
)


def _build_blocks() -> tuple[tuple[MessageBlock, ...], dict[str, str]]:
    blocks: list[MessageBlock] = []
    expected: dict[str, str] = {}
    for i, spec in enumerate(_SPECS):
        sk, slot, layer, transition, ephemeral, hist, pkind, pref, bundle = spec
        bid = f"blk-{i}"
        blocks.append(MessageBlock(
            block_id=bid, context_layer=layer, reasoning_slot=slot, ephemeral=ephemeral,
            history_kind=hist, source_kind=sk, subtype=sk,
            source_reference=SourceReference(kind=pkind, ref=pref),
            transition_behavior=transition, content=f"content-{sk}", sequence=i,
            prompt_role="system",
        ))
        expected[bid] = bundle
    return tuple(blocks), expected


def test_every_named_bundle_present() -> None:
    blocks, _ = _build_blocks()
    briefing = group_blocks_into_briefing(blocks, output_schema=None)
    bundles = briefing["bundles"]
    assert isinstance(bundles, dict)
    _check(
        set(bundles.keys()) == set(BUNDLE_NAMES),
        f"briefing exposes exactly the closed bundle set (got {sorted(bundles)})",
    )


def test_each_source_kind_routes_to_its_bundle() -> None:
    blocks, _ = _build_blocks()
    briefing = group_blocks_into_briefing(blocks, output_schema=None)
    bundles = briefing["bundles"]
    assert isinstance(bundles, dict)
    located: dict[str, str] = {}
    for name, items in bundles.items():
        assert isinstance(items, list)
        for item in items:
            assert isinstance(item, dict)
            located[str(item["provenance"]["ref"])] = name  # type: ignore[index]
    ok = all(
        located.get(spec[7]) == spec[8] for spec in _SPECS
    )
    _check(ok, f"every source_kind routes to its bundle (got {located})")


def test_provenance_attached_per_block() -> None:
    blocks, _ = _build_blocks()
    briefing = group_blocks_into_briefing(blocks, output_schema=None)
    provenance = briefing["provenance"]
    assert isinstance(provenance, list)
    _check(len(provenance) == len(blocks), "one provenance row per block")
    complete = all(
        {"block_id", "source_kind", "kind", "ref"} <= set(row) for row in provenance
    )
    _check(complete, "each provenance row carries block_id + source_kind + kind + ref")


def test_answer_contract_is_output_schema_as_data() -> None:
    blocks, _ = _build_blocks()
    schema = {"type": "object", "properties": {"process": {"type": "string"}}}
    briefing = group_blocks_into_briefing(blocks, output_schema=schema)
    _check(briefing["answer_contract"] == schema, "answer_contract surfaces output_schema as data")
    manifest = briefing["manifest"]
    assert isinstance(manifest, dict)
    _check(manifest["has_answer_contract"] is True, "manifest flags the answer contract present")
    briefing_none = group_blocks_into_briefing(blocks, output_schema=None)
    _check(
        briefing_none["answer_contract"] is None,
        "answer_contract is None when no decode schema was produced",
    )


def test_available_contracts_lists_process_keys() -> None:
    blocks, _ = _build_blocks()
    briefing = group_blocks_into_briefing(blocks, output_schema=None)
    available = briefing["available_contracts"]
    assert isinstance(available, list)
    _check(
        "service_interface::knowledge_service::search" in available
        and "service_interface::thinking_service::upsert_plan" in available,
        f"available_contracts lists process_key-provenanced refs (got {available})",
    )


def test_no_budget_includes_everything() -> None:
    blocks, _ = _build_blocks()
    briefing = group_blocks_into_briefing(blocks, output_schema=None, budget=None)
    manifest = briefing["manifest"]
    assert isinstance(manifest, dict)
    _check(manifest["block_count"] == len(blocks), "no budget -> every block included")
    _check(manifest["budget_applied"] is False, "no budget -> budget_applied False")
    _check(manifest["dropped"] == 0, "no budget -> nothing dropped")


def test_budget_caps_and_records_drop() -> None:
    blocks, _ = _build_blocks()
    briefing = group_blocks_into_briefing(blocks, output_schema=None, budget=2)
    manifest = briefing["manifest"]
    assert isinstance(manifest, dict)
    _check(manifest["block_count"] == 2, "budget=2 retains 2 blocks")
    _check(manifest["dropped"] == len(blocks) - 2, "budget records the dropped count (no silent trim)")
    _check(manifest["budget_applied"] is True, "budget=2 flags budget_applied")


def test_unknown_source_kind_lands_in_other_not_dropped() -> None:
    block = MessageBlock(
        block_id="mystery", context_layer="ossified_context", reasoning_slot="static_frame",
        ephemeral=False, history_kind="none", source_kind="plugin_notice", subtype="notice",
        source_reference=SourceReference(kind="plugin", ref="some_plugin"),
        transition_behavior="stable", content="notice", sequence=0, prompt_role="system",
    )
    briefing = group_blocks_into_briefing((block,), output_schema=None)
    bundles = briefing["bundles"]
    assert isinstance(bundles, dict)
    _check(len(bundles[BUNDLE_OTHER]) == 1, "an unmapped source_kind lands in 'other' (never dropped)")


def main() -> int:
    print("=== agent-context briefing shape + provenance (Phase 2) ===")
    test_every_named_bundle_present()
    test_each_source_kind_routes_to_its_bundle()
    test_provenance_attached_per_block()
    test_answer_contract_is_output_schema_as_data()
    test_available_contracts_lists_process_keys()
    test_no_budget_includes_everything()
    test_budget_caps_and_records_drop()
    test_unknown_source_kind_lands_in_other_not_dropped()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
