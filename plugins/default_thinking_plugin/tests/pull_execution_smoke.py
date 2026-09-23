#!/usr/bin/env python3
"""Phase 4 Seam C — pull-based step engine done-when smoke (no pytest).

Proves the POR Phase 4 done-when clauses offline:

* a small WBS runs in PULL MODE end-to-end (start → get_next → execute
  externally → record_wbs_step_observation → advance … → complete);
* INVALID observations do not advance state — wrong order, undeclared
  process key, and non-success results are rejected with structured
  errors and the durable record stays byte-identical;
* the agent gets enough context to execute/repair/stop (resolved
  arguments incl. Composed references, support articles, result
  contract, completion criteria, await-user control envelopes);
* Q15 — ``advance_wbs_execution`` offers auto-submission ONLY for
  validated deterministic steps explicitly marked ``AUTO_SAFE: true``;
* ◆R2 DISCONNECT/RESUME IS EXERCISED EXPLICITLY — the first driver
  "session" dies mid-WBS; a SECOND service instance over the same
  durable stores resumes from the next unexecuted step with every prior
  observation intact.

Offline: no live solet, no LM Studio, no Postgres. The durable substrates
(knowledge store + thinking_wbs row) are in-memory doubles; the engine,
parser, validators, and lifecycle recorder are the real production code.

Run:
    .venv/bin/python3 plugins/default_thinking_plugin/tests/pull_execution_smoke.py
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "default_thinking_plugin" / "src"))

importlib.import_module("ananta.core.config")  # pre-warm; avoids a latent cycle

from ananta.core.plans.parser import normalize_content, parse  # noqa: E402
from default_thinking_plugin.pull_execution_service import (  # noqa: E402
    PullExecutionService,
)

# ---------------------------------------------------------------------------
# Fixture WBS — 3 executable steps + 1 control step
# ---------------------------------------------------------------------------

WBS_ID = "wbs-pull-fixture"
SEARCH_KEY = "service_interface::knowledge_service::search"
RECORD_KEY = (
    "service_interface::thinking_service::record_work_breakdown_structure_step_state"
)

WBS_DOC = f"""# Work Breakdown Structure

WBS ID: {WBS_ID}
Phase: 2

## Phase 2. Pull-mode fixture chain

### Work Item 2.1: Search, chain, and record

[ ] 1. Search for release guidance
    RESULT_PROCESSOR_KIND: inference
    SUPPORT_ARTICLES: guide_a.md, guide_b.md
    a) Search the knowledge base ({SEARCH_KEY})
        Arguments:
        {{"query": "release guidance", "top_k": 5}}

[ ] 2. Record the search outcome
    RESULT_PROCESSOR_KIND: deterministic_continuation
    AUTO_SAFE: true
    a) Record step state ({RECORD_KEY})
        Arguments:
        {{"wbs_id": "{WBS_ID}", "step_number": 2, "status": "completed"}}
        Composed: state_summary = query from step 1 + " searched"

[-] 3. Await USER message

[ ] 4. Record the terminal work-item state
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Record step state ({RECORD_KEY})
        Arguments:
        {{"wbs_id": "{WBS_ID}", "step_number": 4, "status": "completed"}}
"""


# ── N3(b) multi-sub-step fixture (Claude-C 2026-07-02) ──────────────────────
# Step 2 carries TWO executable sub-steps, each binding a DIFFERENT required arg
# via its OWN Composed reference from step 1. Stresses _sub_step_views'
# per-sub-step Composed isolation (Phase-4 attack #4): sub-step a)'s Composed
# target must resolve into ONLY a)'s arguments and b)'s into ONLY b)'s — the
# opposite of the B1 document-wide-union masking class.
WBS_MULTISUBSTEP_ID = "wbs-pull-multisubstep"

WBS_MULTISUBSTEP = f"""# Work Breakdown Structure

WBS ID: {WBS_MULTISUBSTEP_ID}
Phase: 2

## Phase 2. Two sub-steps, isolated Composed bindings

### Work Item 2.1: Chain two sub-steps

[ ] 1. Produce a seed query
    RESULT_PROCESSOR_KIND: inference
    a) Search the knowledge base ({SEARCH_KEY})
        Arguments:
        {{"query": "seed", "top_k": 5}}

[ ] 2. Two sub-steps each Composed from step 1 with a different target
    RESULT_PROCESSOR_KIND: inference
    a) Search again ({SEARCH_KEY})
        Arguments:
        {{"top_k": 3}}
        Composed: query = query from step 1 + "_a"
    b) Record step state ({RECORD_KEY})
        Arguments:
        {{"wbs_id": "{WBS_MULTISUBSTEP_ID}", "step_number": 2, "status": "completed"}}
        Composed: state_summary = query from step 1 + "_b"

[ ] 3. Record the terminal work-item state
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Record step state ({RECORD_KEY})
        Arguments:
        {{"wbs_id": "{WBS_MULTISUBSTEP_ID}", "step_number": 3, "status": "completed"}}
"""


# ---------------------------------------------------------------------------
# In-memory durable substrates (shared across "sessions")
# ---------------------------------------------------------------------------


class InMemoryKnowledgeStore:
    """Stateful KB double — the durable document survives 'sessions'."""

    def __init__(self, initial: dict[str, str]) -> None:
        self.files = dict(initial)

    def read(self, path: str) -> str:
        return self.files.get(path, "")

    def write(self, path: str, content: str) -> None:
        self.files[path] = content


class InMemoryWbsState:
    """thinking_wbs row double with read/write/update primitives."""

    def __init__(self, record: dict[str, Any]) -> None:
        self.record = dict(record)
        self.register_json: str | None = None
        self.update_calls: list[tuple[dict[str, object], dict[str, object]]] = []

    # WbsStateService (keyword) --------------------------------------
    def read_state(
        self, namespace: str, query: dict[str, object],
    ) -> dict[str, Any]:
        row = dict(self.record)
        if self.register_json is not None:
            row["work_products_data"] = self.register_json
        return {"action_status": "completed", "data": {"records": [row]}}

    def write_state(self, namespace: str, data: dict[str, object]) -> Any:
        return {"action_status": "completed"}

    # WorkProductStateService (positional) ---------------------------
    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
    ) -> dict[str, Any]:
        self.update_calls.append((query, updates))
        if "status" in updates:
            self.record["status"] = updates["status"]
        if "work_products_data" in updates:
            self.register_json = str(updates["work_products_data"])
        # The compare-and-set count lives at data.result.updated
        # (work_product_store._affected_count).
        return {
            "action_status": "completed",
            "data": {"result": {"updated": 1}},
        }


class RecordingFocus:
    def __init__(self) -> None:
        self.upserts: list[str] = []

    def upsert(self, content: str, *, doc_tag: str, label: str) -> str:
        self.upserts.append(doc_tag)
        return "mem-pull-1"

    def get_focused(self) -> list[dict[str, Any]]:
        return []


def build_session(
    knowledge: InMemoryKnowledgeStore, state: InMemoryWbsState,
) -> PullExecutionService:
    """One driver 'session' — a fresh service over the SAME durable stores."""
    return PullExecutionService(
        state_service=state,
        work_product_state_service=state,
        knowledge_store=knowledge,
        focus_manager=RecordingFocus(),
        namespace="default_thinking_plugin",
    )


def fresh_stores() -> tuple[InMemoryKnowledgeStore, InMemoryWbsState]:
    knowledge = InMemoryKnowledgeStore({f"wbs/{WBS_ID}.md": WBS_DOC})
    state = InMemoryWbsState(
        {
            "id": WBS_ID,
            "status": "ready",
            "knowledge_base_path": f"wbs/{WBS_ID}.md",
        },
    )
    return knowledge, state


def fresh_multisubstep_stores() -> tuple[InMemoryKnowledgeStore, InMemoryWbsState]:
    knowledge = InMemoryKnowledgeStore(
        {f"wbs/{WBS_MULTISUBSTEP_ID}.md": WBS_MULTISUBSTEP},
    )
    state = InMemoryWbsState(
        {
            "id": WBS_MULTISUBSTEP_ID,
            "status": "ready",
            "knowledge_base_path": f"wbs/{WBS_MULTISUBSTEP_ID}.md",
        },
    )
    return knowledge, state


class Checker:
    """Minimal pass/fail accumulator (project policy: no pytest)."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: object, label: str) -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL  {label}")

    def summary(self) -> int:
        print(f"\n{self.passed} passed, {len(self.failed)} failed")
        for label in self.failed:
            print(f"  FAILED: {label}")
        return 1 if self.failed else 0


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_start_fresh_session(c: Checker) -> None:
    knowledge, state = fresh_stores()
    session = build_session(knowledge, state)
    started = session.start_wbs_execution(WBS_ID)
    c.check(
        started["status"] == "in_progress" and not started["resumed"],
        f"fresh start marks in_progress, resumed=False ({started['status']})",
    )
    c.check(
        state.record["status"] == "in_progress",
        "status transition lands on the durable row",
    )
    c.check(
        started["next"]["kind"] == "execute"
        and started["next"]["step_number"] == 1,
        f"next envelope points at step 1 ({started['next']['kind']})",
    )
    c.check(
        state.register_json is not None,
        "work-product register initialized durably",
    )


def test_envelope_gives_execution_context(c: Checker) -> None:
    knowledge, state = fresh_stores()
    session = build_session(knowledge, state)
    envelope = session.get_next_wbs_step(WBS_ID)
    c.check(
        envelope["support_articles"] == ["guide_a.md", "guide_b.md"],
        f"support articles surface ({envelope['support_articles']})",
    )
    sub = envelope["sub_steps"][0]
    c.check(
        sub["process_key"] == SEARCH_KEY
        and sub["arguments"] == {"query": "release guidance", "top_k": 5},
        f"bound arguments resolve ({sub['arguments']})",
    )
    c.check(
        envelope["expected_result_contract"]["result_status_one_of"]
        == ["completed", "succeeded", "success"],
        "expected result contract states the success statuses",
    )
    c.check(
        bool(envelope["completion_criteria"]),
        "completion criteria included",
    )


def test_invalid_observations_change_nothing(c: Checker) -> None:
    knowledge, state = fresh_stores()
    session = build_session(knowledge, state)
    session.start_wbs_execution(WBS_ID)  # establishes a durable register + one write
    before = knowledge.files[f"wbs/{WBS_ID}.md"]
    # ◆N1 (Claude-C 2026-07-02): a rejected observation must touch NO durable
    # state, not only the KB doc. Snapshot the work-product register AND the
    # state-write call log AFTER the legitimate start, so the assertions below
    # prove the invalid branch returns before any record_step_state /
    # update_state (non-vacuous: the register is already non-None here).
    register_before = state.register_json
    update_calls_before = len(state.update_calls)

    out_of_order = session.record_wbs_step_observation(
        WBS_ID, 2, RECORD_KEY, {"action_status": "completed"},
    )
    undeclared = session.record_wbs_step_observation(
        WBS_ID, 1, "plugin::nope::x", {"action_status": "completed"},  # wint:negative-fixture
    )
    failed_result = session.record_wbs_step_observation(
        WBS_ID, 1, SEARCH_KEY, {"action_status": "error"},
    )
    c.check(
        not out_of_order["accepted"]
        and any("not the next" in e for e in out_of_order["errors"]),
        f"out-of-order observation rejected ({out_of_order['errors']})",
    )
    c.check(
        not undeclared["accepted"]
        and any("not declared" in e for e in undeclared["errors"]),
        "undeclared process key rejected",
    )
    c.check(
        not failed_result["accepted"]
        and any("status" in e for e in failed_result["errors"]),
        "non-success result rejected",
    )
    c.check(
        knowledge.files[f"wbs/{WBS_ID}.md"] == before,
        "durable document is byte-identical after every rejection",
    )
    c.check(
        state.register_json == register_before,
        f"work-product register_json unchanged after every rejection "
        f"(before={register_before!r}, after={state.register_json!r})",
    )
    c.check(
        len(state.update_calls) == update_calls_before,
        f"zero state-write (update_state) calls across the rejections "
        f"(before={update_calls_before}, after={len(state.update_calls)})",
    )


def test_pull_run_with_disconnect_resume(c: Checker) -> None:
    """The ◆R2 acceptance criterion, exercised explicitly."""
    knowledge, state = fresh_stores()

    # --- session 1: start, execute step 1, record, then DIE ----------
    session_one = build_session(knowledge, state)
    session_one.start_wbs_execution(WBS_ID)
    accepted = session_one.record_wbs_step_observation(
        WBS_ID,
        1,
        SEARCH_KEY,
        {"action_status": "completed"},
        state_summary="found guidance",
        output_artifacts=["guidance_notes.md"],
    )
    c.check(
        accepted["accepted"] and accepted["next"]["step_number"] == 2,
        f"session 1 records step 1 and sees step 2 next ({accepted['next']['step_number']})",
    )
    del session_one  # driver disconnects (bridge death) — nothing else persists

    # --- session 2: a FRESH service over the same durable stores -----
    session_two = build_session(knowledge, state)
    resumed = session_two.start_wbs_execution(WBS_ID)
    c.check(
        resumed["resumed"] and resumed["executed_step_numbers"] == [1, 3],
        f"session 2 resumes with step 1 intact (3 is the pre-authored "
        f"skip marker) ({resumed['executed_step_numbers']})",
    )
    c.check(
        resumed["next"]["step_number"] == 2,
        "session 2 resumes from the next unexecuted step",
    )
    c.check(
        "found guidance" in knowledge.files[f"wbs/{WBS_ID}.md"],
        "no lost observations — session 1's summary survives in the durable record",
    )

    # --- Q15: step 2 is AUTO_SAFE + deterministic + closed-world -----
    evaluation = session_two.advance_wbs_execution(WBS_ID)
    c.check(
        evaluation["mode"] == "auto_safe",
        f"auto-safe step offers auto-submission ({evaluation['mode']})",
    )
    c.check(
        evaluation["action_definition"]["arguments"]["state_summary"]
        == "release guidance searched",
        "Composed reference resolves across the resume boundary",
    )

    # --- drive to completion -----------------------------------------
    session_two.record_wbs_step_observation(
        WBS_ID, 2, RECORD_KEY, {"action_status": "completed"},
    )
    after_two = session_two.get_next_wbs_step(WBS_ID)
    c.check(
        after_two["kind"] == "execute" and after_two["step_number"] == 4,
        f"skipped control step [-] 3 is not offered ({after_two['step_number']})",
    )
    evaluation_four = session_two.advance_wbs_execution(WBS_ID)
    c.check(
        evaluation_four["mode"] == "agent_review"
        and any("AUTO_SAFE" in r for r in evaluation_four["reasons"]),
        f"unmarked deterministic step returns to the agent (Q15) "
        f"({evaluation_four['reasons']})",
    )
    session_two.record_wbs_step_observation(
        WBS_ID, 4, RECORD_KEY, {"action_status": "completed"},
    )
    final = session_two.advance_wbs_execution(WBS_ID)
    c.check(final["mode"] == "complete", f"run completes ({final['mode']})")
    c.check(
        state.record["status"] == "completed",
        "completion lands on the durable row",
    )


def test_await_user_control_step_stops_pull(c: Checker) -> None:
    doc = WBS_DOC.replace("[-] 3. Await USER message", "[ ] 3. Await USER message")
    knowledge = InMemoryKnowledgeStore({f"wbs/{WBS_ID}.md": doc})
    state = InMemoryWbsState(
        {
            "id": WBS_ID,
            "status": "ready",
            "knowledge_base_path": f"wbs/{WBS_ID}.md",
        },
    )
    session = build_session(knowledge, state)
    annotated = doc + (
        "\n<!-- Step 1: status=completed -->\n"
        "<!-- Step 2: status=completed -->\n"
    )
    knowledge.files[f"wbs/{WBS_ID}.md"] = annotated
    envelope = session.get_next_wbs_step(WBS_ID)
    c.check(
        envelope["kind"] == "await_user" and envelope["step_number"] == 3,
        f"control step yields await_user ({envelope['kind']})",
    )


def test_classic_markers_count_as_executed(c: Checker) -> None:
    """A WBS that rode the projected-plan path resumes correctly."""
    doc = WBS_DOC.replace("[ ] 1. Search", "[X] 1. Search")
    knowledge = InMemoryKnowledgeStore({f"wbs/{WBS_ID}.md": doc})
    state = InMemoryWbsState(
        {
            "id": WBS_ID,
            "status": "ready",
            "knowledge_base_path": f"wbs/{WBS_ID}.md",
        },
    )
    session = build_session(knowledge, state)
    envelope = session.get_next_wbs_step(WBS_ID)
    c.check(
        envelope["step_number"] == 2,
        f"[X]-marked step counts as executed ({envelope['step_number']})",
    )


def test_auto_safe_annotation_parses_strictly(c: Checker) -> None:
    parsed = parse(WBS_DOC)
    flags = {s.number: s.auto_safe for s in parsed.steps}
    c.check(
        flags == {1: False, 2: True, 3: False, 4: False},
        f"AUTO_SAFE parses per step ({flags})",
    )
    for bad, token in (
        (WBS_DOC.replace("AUTO_SAFE: true", "AUTO_SAFE: yes"), "AUTO_SAFE_INVALID"),
        (
            WBS_DOC.replace(
                "AUTO_SAFE: true", "AUTO_SAFE: true\n    AUTO_SAFE: true",
            ),
            "AUTO_SAFE_DUPLICATE",
        ),
    ):
        try:
            parse(bad)
            c.check(False, f"malformed AUTO_SAFE must raise ({token})")
        except ValueError as exc:
            c.check(token in str(exc), f"malformed AUTO_SAFE raises {token}")


def test_auto_safe_survives_inline_rehydrate_round_trip(c: Checker) -> None:
    """◆N3 (Claude-C 2026-07-02): AUTO_SAFE is wired into _INLINE_METADATA_RE, so
    an inlined WBS (metadata collapsed onto the prior line) rehydrates through
    the public normalize_content and re-parses with the flag intact — the
    round-trip the Phase-4 rehydration wiring added but left uncovered."""
    # Collapse the AUTO_SAFE metadata onto the preceding RPK line (drop the
    # newline + indent) — the exact inlined shape _INLINE_METADATA_RE targets.
    inlined = WBS_DOC.replace(
        "deterministic_continuation\n    AUTO_SAFE: true",
        "deterministic_continuation    AUTO_SAFE: true",
    )
    c.check(
        "\n    AUTO_SAFE: true" not in inlined,
        "precondition: the inlined fixture carries NO standalone AUTO_SAFE line",
    )
    parsed = parse(normalize_content(inlined))
    flags = {s.number: s.auto_safe for s in parsed.steps}
    c.check(
        flags.get(2) is True,
        f"AUTO_SAFE on step 2 survives inline→rehydrate→parse ({flags})",
    )
    c.check(
        flags == {1: False, 2: True, 3: False, 4: False},
        f"no other step gains or loses AUTO_SAFE through the round-trip ({flags})",
    )


def test_multi_sub_step_composed_isolation(c: Checker) -> None:
    """◆N3 (Claude-C 2026-07-02): _sub_step_views resolves each sub-step's
    Composed reference into ONLY that sub-step's arguments — no cross-sub-step
    bleed within one step (the opposite of the B1 document-wide masking class).
    Step 2 has two sub-steps binding different targets from step 1."""
    knowledge, state = fresh_multisubstep_stores()
    session = build_session(knowledge, state)
    session.start_wbs_execution(WBS_MULTISUBSTEP_ID)
    # Execute step 1 so step 2 (the multi-sub-step step) becomes next.
    session.record_wbs_step_observation(
        WBS_MULTISUBSTEP_ID, 1, SEARCH_KEY, {"action_status": "completed"},
    )
    envelope = session.get_next_wbs_step(WBS_MULTISUBSTEP_ID)
    c.check(
        envelope["step_number"] == 2,
        f"step 2 (the multi-sub-step step) is next ({envelope.get('step_number')})",
    )
    subs = {s["label"]: s for s in envelope["sub_steps"]}
    a_args = subs.get("a", {}).get("arguments", {})
    b_args = subs.get("b", {}).get("arguments", {})
    c.check(
        a_args.get("query") == "seed_a" and "state_summary" not in a_args,
        f"sub-step a) resolves ONLY its own Composed target 'query' ({a_args})",
    )
    c.check(
        b_args.get("state_summary") == "seed_b" and "query" not in b_args,
        f"sub-step b) resolves ONLY its own Composed target 'state_summary' ({b_args})",
    )


def main() -> int:
    c = Checker("pull-based step engine")
    cases: list[Callable[[Checker], None]] = [
        test_start_fresh_session,
        test_envelope_gives_execution_context,
        test_invalid_observations_change_nothing,
        test_pull_run_with_disconnect_resume,
        test_await_user_control_step_stops_pull,
        test_classic_markers_count_as_executed,
        test_auto_safe_annotation_parses_strictly,
        test_auto_safe_survives_inline_rehydrate_round_trip,
        test_multi_sub_step_composed_isolation,
    ]
    for case in cases:
        print(f"\n{case.__name__}")
        case(c)
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
