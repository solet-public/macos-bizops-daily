#!/usr/bin/env python3
"""Phase 2 — assemble_agent_context end-to-end via a fake factory (offline, no pytest).

The done-when ("an agent calls ONE process and receives the grounding classes")
is exercised here without a live pipeline / model / orchestrator: a fake pipeline
factory returns a canned ``PromptContext`` (message_blocks + output_schema), and
we assert the verb returns the FULL briefing envelope, threads flow/session into
``pipeline.execute``, passes ``budget`` through to the grouping, applies the
``requested_bundles`` filter, and FAILS LOUD on an unknown bundle name (no
defensive silent-drop).

Run:
    .venv/bin/python3 ananta/tests/core/context_service/assemble_agent_context_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.prompts.context import MessageBlock, SourceReference  # noqa: E402
from ananta.services.context_service.service import ContextService  # noqa: E402

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


def _blocks() -> list[MessageBlock]:
    """Three valid blocks spanning three bundles (frame / process_catalog / guidance)."""
    return [
        MessageBlock(
            block_id="b-frame", context_layer="ossified_context",
            reasoning_slot="static_frame", ephemeral=False, history_kind="none",
            source_kind="system_template", subtype="system",
            source_reference=SourceReference(kind="template", ref="system.md"),
            transition_behavior="stable", content="frame", sequence=0, prompt_role="system",
        ),
        MessageBlock(
            block_id="b-cat", context_layer="ossified_context",
            reasoning_slot="static_frame", ephemeral=False, history_kind="none",
            source_kind="process_catalog", subtype="catalog",
            source_reference=SourceReference(
                kind="process_key", ref="service_interface::knowledge_service::search",
            ),
            transition_behavior="stable", content="catalog", sequence=1, prompt_role="system",
        ),
        MessageBlock(
            block_id="b-guid", context_layer="ossified_context",
            reasoning_slot="working_evidence", ephemeral=False, history_kind="none",
            source_kind="prompt_asset", subtype="article",
            source_reference=SourceReference(kind="article", ref="guidance/x.md"),
            transition_behavior="stable", content="guidance", sequence=2, prompt_role="system",
        ),
    ]


class _FakeCtx:
    def __init__(self, blocks: list[MessageBlock], output_schema: dict[str, object] | None) -> None:
        self.message_blocks = blocks
        self.output_schema = output_schema


class _FakePipeline:
    def __init__(self, ctx: _FakeCtx) -> None:
        self._ctx = ctx
        self.captured: dict[str, Any] = {}

    def execute(self, **kwargs: Any) -> tuple[_FakeCtx, None]:
        self.captured = kwargs
        return self._ctx, None


class _FakeFactory:
    def __init__(self, ctx: _FakeCtx) -> None:
        self.pipeline = _FakePipeline(ctx)
        self.captured_profile: Any = None

    def create_pipeline(self, profile: Any) -> _FakePipeline:
        self.captured_profile = profile
        return self.pipeline


def _service_with_fake(output_schema: dict[str, object] | None) -> tuple[ContextService, _FakeFactory]:
    svc = ContextService(
        app_home="/unused", context_config=None,  # type: ignore[arg-type]
        orchestrator=object(), state_service=None,
    )
    factory = _FakeFactory(_FakeCtx(_blocks(), output_schema))
    svc._pipeline_factory = factory  # type: ignore[assignment]  # bypass live factory build
    return svc, factory


def test_returns_full_envelope() -> None:
    svc, _ = _service_with_fake({"type": "object"})
    out = svc.assemble_agent_context(session_id="s1", flow_id="f1")
    _check(
        {"profile", "bundles", "answer_contract", "available_contracts", "provenance", "manifest"}
        <= set(out),
        f"verb returns the full briefing envelope (got keys {sorted(out)})",
    )
    _check(out["answer_contract"] == {"type": "object"}, "answer_contract = the ctx output_schema")
    _check(
        "service_interface::knowledge_service::search" in out["available_contracts"],  # type: ignore[operator]
        "available_contracts lists the process_key from the catalog block",
    )


def test_threads_flow_and_session_into_execute() -> None:
    svc, factory = _service_with_fake(None)
    svc.assemble_agent_context(session_id="s2", flow_id="f2", context_id="c2")
    cap = factory.pipeline.captured
    _check(
        cap.get("flow_id") == "f2" and cap.get("session_id") == "s2"
        and cap.get("context_id") == "c2"
        and cap.get("action_name") == "assemble_agent_context",
        f"flow/session/context + action_name threaded into pipeline.execute (got {cap})",
    )


def test_budget_passes_through() -> None:
    svc, _ = _service_with_fake(None)
    out = svc.assemble_agent_context(session_id="s", flow_id="f", budget=1)
    manifest = out["manifest"]
    assert isinstance(manifest, dict)
    _check(manifest["block_count"] == 1, "budget=1 threads through to the grouping (1 block kept)")
    _check(manifest["dropped"] == 2 and manifest["budget_applied"] is True, "budget drop recorded")


def test_requested_bundles_filter() -> None:
    svc, _ = _service_with_fake(None)
    out = svc.assemble_agent_context(
        session_id="s", flow_id="f", requested_bundles=["process_catalog"],
    )
    bundles = out["bundles"]
    assert isinstance(bundles, dict)
    _check(
        set(bundles) == {"process_catalog"} and len(bundles["process_catalog"]) == 1,
        f"requested_bundles restricts the bundles map (got {sorted(bundles)})",
    )


def test_unknown_bundle_fails_loud() -> None:
    svc, _ = _service_with_fake(None)
    try:
        svc.assemble_agent_context(
            session_id="s", flow_id="f", requested_bundles=["not_a_bundle"],
        )
    except ValueError as exc:
        _check(
            "not_a_bundle" in str(exc),
            "unknown requested_bundles raises ValueError naming the unknown (fail-fast)",
        )
    else:
        _check(False, "unknown requested_bundles did NOT raise (defensive silent-drop regression)")


def main() -> int:
    print("=== assemble_agent_context end-to-end (fake factory) — Phase 2 ===")
    test_returns_full_envelope()
    test_threads_flow_and_session_into_execute()
    test_budget_passes_through()
    test_requested_bundles_filter()
    test_unknown_bundle_fails_loud()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
