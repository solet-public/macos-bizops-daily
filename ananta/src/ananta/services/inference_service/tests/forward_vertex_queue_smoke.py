#!/usr/bin/env python3
"""INF-06 reliability carve — forwarded-vertex durable queue + freshness smoke.

Core-side half of the holder-forward recoverability slice (the plugin-side
sweep/drain/GC/RESUBMIT matrix lives in
``plugins/agent_messaging_plugin/tests/forward_vertex_redrive_smoke.py``). Proves
the durable ``core__inference_deferred_vertex`` dual-state mechanics and the
Architect §6-bis fresh-decode-not-blind property:

  S1  MINT durability-first + fail-loud (``record_forwarded_vertex``): a happy
      mint upserts ``state=forwarded`` + ``forwarded_at`` (attempts OMITTED so a
      re-forward preserves the monotone counter), method discriminates
      error/results; a ``flow_id=None`` forward RAISES (anchorless-forward
      refusal, N2 analog); a non-completed upsert RAISES (require_completed =
      durability-first, never a swallowed mint then a fire-and-forget forward).
  S1d record_deferred_vertex now stamps ``state=deferred`` + ``forwarded_at=None``
      (attempts OMITTED) — the vacancy row shape.
  HLP forwarded_before / live_rows_in_state / increment_attempts (monotone) /
      mark_terminal_failed / attempts_of — the sweep/drain read+write helpers.
  S7  ★ Architect §6-bis plan-[>]-unmoved: a forwarded ``process_error`` turn
      does NOT advance the plan (``should_skip_advancement`` guard), so the
      failed step keeps its ``[>]`` current marker — the re-driven fresh decode
      reads a plan NOT moved past the failure (failure-awareness durable in plan
      state). RED-FIRST control: a ``process_results`` turn is NOT skipped.
  WIRE _dispatch_to_provider mints BEFORE ``provider.process_*`` and degrades a
      mint failure to a durable DEFER (source-order pin — never fire-and-forget).

Offline: the shared REAL-SHAPE state fake (no schema enforcement on this table;
``fail_next`` injects provider-error envelopes). No live solet / LM Studio / Postgres.

Run from repo root:
    .venv/bin/python3 \
        ananta/src/ananta/services/inference_service/tests/forward_vertex_queue_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# RealShapeState's schema-enforcement pulls in agent_messaging_plugin, whose
# package init resolves vault-scoped constants eagerly (same class as
# hmac_bearer_token_smoke). SOLET_NAME must already be set in the
# environment (platform contract: no silent default) for this import chain
# to succeed.

REPO_ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "tests"))

from _real_state_fake import RealShapeState  # noqa: E402  # pyright: ignore[reportMissingImports]

from ananta.core.plans.advancement import (  # noqa: E402
    maybe_advance_plan,
    should_skip_advancement,
)
from ananta.core.plans.parser import parse  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.llm.agent_messaging.state_results import StateOperationError  # noqa: E402
from ananta.services.inference_service.deferred_vertex_queue import (  # noqa: E402
    attempts_of,
    deferred_vertices_snapshot,
    forward_with_serve_anchor,
    forwarded_before,
    increment_attempts,
    live_rows_in_state,
    mark_terminal_failed,
    mint_forwarded_or_degrade,
    record_deferred_vertex,
    record_forwarded_vertex,
)
from ananta.services.inference_service.schema import (  # noqa: E402
    COL_ATTEMPTS,
    COL_FLOW_ID,
    COL_FORWARDED_AT,
    COL_METHOD,
    COL_STATE,
    INFERENCE_DEFERRED_VERTEX_NAMESPACE,
    METHOD_PROCESS_ERROR,
    METHOD_PROCESS_RESULTS,
    STATE_DEFERRED,
    STATE_FAILED,
    STATE_FORWARDED,
    TABLE_INFERENCE_DEFERRED_VERTEX,
)
from ananta.services.inference_service.vertex_resolver import (  # noqa: E402
    VertexResolution,
    VertexRouting,
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


def _rows(state: RealShapeState) -> list[dict[str, Any]]:
    return state.rows(
        INFERENCE_DEFERRED_VERTEX_NAMESPACE, TABLE_INFERENCE_DEFERRED_VERTEX,
    )


def _only(state: RealShapeState) -> dict[str, Any]:
    rows = _rows(state)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    return rows[0]


# --------------------------------------------------------------------------
# S1 — MINT durability-first + fail-loud
# --------------------------------------------------------------------------
def test_mint_forwarded() -> None:
    print("S1 — record_forwarded_vertex durability-first + fail-loud:")

    # happy path (results forward): state=forwarded, forwarded_at set, method,
    # attempts OMITTED (so a later re-forward upsert preserves the counter).
    state = RealShapeState()
    record_forwarded_vertex(
        state, is_error=False, role="sys:autonomic",
        holder_agent_instance_id="agi-h", flow_id="flow-1",
        now_iso="2026-07-18T00:00:00+00:00",
    )
    row = _only(state)
    _check(
        row.get(COL_STATE) == STATE_FORWARDED
        and row.get(COL_FORWARDED_AT) == "2026-07-18T00:00:00+00:00"
        and row.get(COL_METHOD) == METHOD_PROCESS_RESULTS
        and COL_ATTEMPTS not in row,
        "S1a results forward → forwarded+stamp+process_results, attempts omitted",
    )

    # error forward → method discriminates.
    state2 = RealShapeState()
    record_forwarded_vertex(
        state2, is_error=True, role="sys:autonomic",
        holder_agent_instance_id="agi-h", flow_id="flow-e",
        now_iso="2026-07-18T00:00:01+00:00",
    )
    _check(
        _only(state2).get(COL_METHOD) == METHOD_PROCESS_ERROR,
        "S1b error forward → method=process_error",
    )

    # flow_id=None → anchorless forward refused (raises; no row).
    state3 = RealShapeState()
    raised = False
    try:
        record_forwarded_vertex(
            state3, is_error=False, role="sys:autonomic",
            holder_agent_instance_id="agi-h", flow_id=None,
            now_iso="2026-07-18T00:00:02+00:00",
        )
    except FrameworkError:
        raised = True
    _check(
        raised and not _rows(state3),
        "S1c flow_id=None → RAISES (anchorless forward refused), no row",
    )

    # durability-first: a non-completed upsert RAISES (never a swallowed mint
    # followed by a fire-and-forget forward). require_completed raises
    # StateOperationError — a store-broken signal that propagates loud (the mint's
    # ``except FrameworkError`` degrade covers only the recoverable flow_id-absent
    # case; a broken store can't be degraded to the SAME store's DEFER write).
    state4 = RealShapeState()
    state4.fail_next("upsert")
    raised2 = False
    try:
        record_forwarded_vertex(
            state4, is_error=False, role="sys:autonomic",
            holder_agent_instance_id="agi-h", flow_id="flow-f",
            now_iso="2026-07-18T00:00:03+00:00",
        )
    except StateOperationError:
        raised2 = True
    _check(raised2, "S1d non-completed upsert → RAISES (require_completed fail-loud)")

    # record_deferred_vertex vacancy shape: state=deferred, forwarded_at=None.
    state5 = RealShapeState()
    resolution = VertexResolution(
        VertexRouting.DEFER, None, "sys:autonomic", "agi-absent",
    )
    record_deferred_vertex(state5, is_error=True, resolution=resolution, flow_id="flow-d")
    drow = _only(state5)
    _check(
        drow.get(COL_STATE) == STATE_DEFERRED
        and drow.get(COL_FORWARDED_AT) is None
        and drow.get(COL_METHOD) == METHOD_PROCESS_ERROR
        and COL_ATTEMPTS not in drow,
        "S1e record_deferred_vertex → deferred + forwarded_at=None + attempts omitted",
    )


# --------------------------------------------------------------------------
# HLP — sweep/drain read+write helpers
# --------------------------------------------------------------------------
def _hlp_forwarded_before() -> None:
    cutoff = "2026-07-18T12:00:00+00:00"
    past = forwarded_before({COL_FORWARDED_AT: "2026-07-18T11:00:00+00:00"}, cutoff_iso=cutoff)
    future = forwarded_before({COL_FORWARDED_AT: "2026-07-18T13:00:00+00:00"}, cutoff_iso=cutoff)
    absent = forwarded_before({}, cutoff_iso=cutoff)
    # no-stamp→True is the surface-on-anomaly fix (F-AISLOP, 2026-07-20): a
    # 'forwarded' row always carries a stamp, so a missing one is an anomaly that
    # must be re-driven (bounded by the attempts cap), NOT silently pinned. The old
    # no-stamp→False assertion pinned exactly that silent-stall bug.
    _check(
        past and not future and absent,
        "HLP1 forwarded_before: past→True, future→False, no-stamp→True "
        "(surface-on-anomaly)",
    )


def _hlp_live_rows_filter() -> RealShapeState:
    state = RealShapeState()
    record_forwarded_vertex(
        state, is_error=False, role="sys:autonomic",
        holder_agent_instance_id="agi-h", flow_id="flow-1",
        now_iso="2026-07-18T00:00:00+00:00",
    )
    record_deferred_vertex(
        state, is_error=False,
        resolution=VertexResolution(VertexRouting.DEFER, None, "sys:autonomic", None),
        flow_id="flow-2",
    )
    fwd = live_rows_in_state(state, state=STATE_FORWARDED)
    dfr = live_rows_in_state(state, state=STATE_DEFERRED)
    fwd_ok = len(fwd) == 1 and fwd[0].get(COL_FLOW_ID) == "flow-1"
    dfr_ok = len(dfr) == 1 and dfr[0].get(COL_FLOW_ID) == "flow-2"
    _check(fwd_ok and dfr_ok, "HLP2 live_rows_in_state filters by state (forwarded vs deferred)")
    return state


class _OrderedQuerySpy(RealShapeState):
    """Makes the smoke prove a page boundary causes a second ordered read."""

    def __init__(self) -> None:
        super().__init__()
        self.ordered_reads = 0

    def query_ordered(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        self.ordered_reads += 1
        return super().query_ordered(namespace, query)


def _record_deferred_rows(state: RealShapeState, *, count: int) -> None:
    ticks = iter(range(count))
    state.now_iso = lambda: f"2026-07-18T00:00:00.{next(ticks):06d}+00:00"
    resolution = VertexResolution(
        VertexRouting.DEFER, None, "sys:autonomic", "agi-absent",
    )
    for index in range(count):
        record_deferred_vertex(
            state, is_error=False, resolution=resolution, flow_id=f"page-{index}",
        )


def _hlp_paged_reads() -> None:
    overflow = _OrderedQuerySpy()
    _record_deferred_rows(overflow, count=101)
    rows = live_rows_in_state(overflow, state=STATE_DEFERRED)
    _check(
        len(rows) == 101 and overflow.ordered_reads == 2,
        "HLP2a live_rows_in_state pages past 100 rows without a prefix",
    )

    boundary = _OrderedQuerySpy()
    _record_deferred_rows(boundary, count=100)
    snapshot = deferred_vertices_snapshot(boundary)
    _check(
        len(snapshot) == 100 and boundary.ordered_reads == 2,
        "HLP2b snapshot verifies an exact 100-row boundary with a terminal empty page",
    )


def _row_by_flow(state: RealShapeState, flow_id: str) -> dict[str, Any]:
    return next(r for r in _rows(state) if r.get(COL_FLOW_ID) == flow_id)


def _hlp_attempts_monotone(state: RealShapeState) -> None:
    # attempts monotone: increment twice → 2 (never reset); attempts_of reads it.
    increment_attempts(state, flow_id="flow-1")
    increment_attempts(state, flow_id="flow-1")
    _check(
        attempts_of(_row_by_flow(state, "flow-1")) == 2,
        "HLP3 increment_attempts monotone (2 bumps → 2)",
    )
    # a re-forward (upsert, attempts omitted) PRESERVES the bumped counter — the
    # occupancy-monotone property the sweep depends on (no hard-delete-then-reset).
    record_forwarded_vertex(
        state, is_error=False, role="sys:autonomic",
        holder_agent_instance_id="agi-h", flow_id="flow-1",
        now_iso="2026-07-18T00:05:00+00:00",
    )
    row2 = _row_by_flow(state, "flow-1")
    preserved = attempts_of(row2) == 2
    refreshed = row2.get(COL_FORWARDED_AT) == "2026-07-18T00:05:00+00:00"
    _check(preserved and refreshed, "HLP4 re-forward upsert PRESERVES attempts (monotone), refreshes stamp")


def _hlp_terminal_and_attempts_of(state: RealShapeState) -> None:
    mark_terminal_failed(state, flow_id="flow-1")
    failed = _row_by_flow(state, "flow-1").get(COL_STATE) == STATE_FAILED
    left_set = not live_rows_in_state(state, state=STATE_FORWARDED)
    _check(failed and left_set, "HLP5 mark_terminal_failed → state=failed (leaves the forwarded set)")

    is_int = attempts_of({COL_ATTEMPTS: 3}) == 3
    is_absent = attempts_of({}) == 0
    is_bool = attempts_of({COL_ATTEMPTS: True}) == 0
    _check(is_int and is_absent and is_bool, "HLP6 attempts_of: int→n, absent→0, bool guarded→0")


def test_helpers() -> None:
    print("HLP — forwarded_before / live_rows_in_state / attempts monotone:")
    _hlp_forwarded_before()
    state = _hlp_live_rows_filter()
    _hlp_paged_reads()
    _hlp_attempts_monotone(state)
    _hlp_terminal_and_attempts_of(state)


# --------------------------------------------------------------------------
# S7 — ★ Architect §6-bis plan-[>]-unmoved (fresh decode not blind)
# --------------------------------------------------------------------------
class _AdvanceSpy:
    def __init__(self) -> None:
        self.calls = 0

    def advance_current_plan_step(self, *, session_id: str) -> dict[str, Any] | None:
        del session_id
        self.calls += 1
        return None


def test_plan_marker_unmoved() -> None:
    print("S7 — ★ failed forward keeps the plan's [>] on the failed step:")

    plan_text = (
        "ACTIVE_PLAN: p1\nACTIVE_WBS: wbs1\n\n"
        "[X] 1. Step one — done\n"
        "[>] 2. Step two — the failed step\n"
        "[ ] 3. Step three — later\n"
    )
    _check(
        parse(plan_text).current_step_number == 2,
        "S7a the plan's current [>] step is step 2 (the failed step)",
    )

    # A forwarded process_error turn must NOT advance — the guard keeps [>] put.
    skip = should_skip_advancement(
        action_name="process_error", is_continuation=True,
        memory_provider=None, session_id="s1",
    )
    _check(
        skip is not None,
        "S7b process_error turn is SKIPPED (must not advance past incomplete step)",
    )
    # RED-FIRST control: a success (process_results) turn is NOT skipped by this
    # guard — proving the skip is specific to the failure path, not vacuous.
    no_skip = should_skip_advancement(
        action_name="process_results", is_continuation=True,
        memory_provider=None, session_id="s1",
    )
    _check(
        no_skip is None,
        "S7c RED-FIRST control: process_results turn is NOT skipped (guard is specific)",
    )

    # maybe_advance_plan for the failed turn calls NOTHING on the thinking service.
    spy = _AdvanceSpy()
    maybe_advance_plan(
        action_name="process_error", is_continuation=True,
        memory_provider=None, thinking_service=spy, session_id="s1",
    )
    _check(
        spy.calls == 0 and parse(plan_text).current_step_number == 2,
        "S7d failed turn advances nothing → fresh decode still reads [>] on step 2",
    )


# --------------------------------------------------------------------------
# WIRE — forward_with_serve_anchor: mint BEFORE forward, degrade never forwards
# --------------------------------------------------------------------------
class _ProviderSpy:
    """Asserts the serve-anchor row EXISTS at the moment the forward fires."""

    def __init__(self, state: RealShapeState) -> None:
        self._state = state
        self.row_present_at_forward: bool | None = None

    def _forward(self) -> dict[str, Any]:
        self.row_present_at_forward = bool(_rows(self._state))
        return {"action_status": "completed", "actions": [], "data": {}}

    def process_results(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        del params, state
        return self._forward()

    def process_error(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        del params, state
        return self._forward()


def _degrade_spy() -> tuple[list[dict[str, Any]], Any]:
    calls: list[dict[str, Any]] = []

    def degrade(*, is_error: bool, resolution: Any, flow_id: str | None) -> dict[str, Any]:
        calls.append({"is_error": is_error, "flow_id": flow_id, "resolution": resolution})
        return {"action_status": "completed", "vertex_deferred": True, "actions": []}

    return calls, degrade


def test_forward_helpers() -> None:
    print("WIRE — forward_with_serve_anchor mints before forward, degrade never forwards:")

    resolution = VertexResolution(
        VertexRouting.PROVIDER, None, "sys:autonomic", "agi-h",
    )

    # mint_forwarded_or_degrade: happy → None (mint landed), row present.
    state = RealShapeState()
    calls, degrade = _degrade_spy()
    out = mint_forwarded_or_degrade(
        state, resolution=resolution, is_error=False, flow_id="flow-1",
        now_iso="2026-07-18T00:00:00+00:00", degrade=degrade,
    )
    _check(
        all([out is None, len(_rows(state)) == 1, calls == []]),
        "WIRE1 mint_forwarded_or_degrade happy → None, row minted, no degrade",
    )

    # flow_id=None → the recoverable failure degrades (degrade called, its result
    # returned) — no crash, no row.
    state2 = RealShapeState()
    calls2, degrade2 = _degrade_spy()
    out2 = mint_forwarded_or_degrade(
        state2, resolution=resolution, is_error=True, flow_id=None,
        now_iso="2026-07-18T00:00:01+00:00", degrade=degrade2,
    )
    degraded = out2 is not None and out2.get("vertex_deferred") is True
    _check(
        all([degraded, len(calls2) == 1, not _rows(state2)]),
        "WIRE2 flow_id-absent mint → degrades to DEFER (no crash, no row)",
    )

    # forward_with_serve_anchor happy: the row EXISTS when the forward fires
    # (durability-first ordering), and the provider's result is returned.
    state3 = RealShapeState()
    spy = _ProviderSpy(state3)
    res3 = VertexResolution(VertexRouting.PROVIDER, spy, "sys:autonomic", "agi-h")  # type: ignore[arg-type]
    _, degrade3 = _degrade_spy()
    result = forward_with_serve_anchor(
        state3, resolution=res3, is_error=False,
        params={}, state={"flow_id": "flow-3"},
        now_iso="2026-07-18T00:00:02+00:00", degrade=degrade3,
    )
    _check(
        all([
            spy.row_present_at_forward is True,
            result.get("action_status") == "completed",
            len(_rows(state3)) == 1,
        ]),
        "WIRE3 forward_with_serve_anchor mints the row BEFORE the forward fires",
    )

    # forward_with_serve_anchor with a flow_id-absent state: degrades, and the
    # provider is NEVER called (no anchorless forward).
    state4 = RealShapeState()
    spy4 = _ProviderSpy(state4)
    res4 = VertexResolution(VertexRouting.PROVIDER, spy4, "sys:autonomic", "agi-h")  # type: ignore[arg-type]
    calls4, degrade4 = _degrade_spy()
    forward_with_serve_anchor(
        state4, resolution=res4, is_error=False,
        params={}, state={},  # no flow_id
        now_iso="2026-07-18T00:00:03+00:00", degrade=degrade4,
    )
    _check(
        all([spy4.row_present_at_forward is None, len(calls4) == 1, not _rows(state4)]),
        "WIRE4 flow_id-absent → degrades, provider NEVER called (no anchorless forward)",
    )

    # N6 invariant: a PROVIDER verdict with NO provider fails LOUD (never a silent
    # fall-through to the default model).
    _, degrade5 = _degrade_spy()
    raised = False
    try:
        forward_with_serve_anchor(
            RealShapeState(), resolution=resolution, is_error=False,
            params={}, state={"flow_id": "flow-5"},
            now_iso="2026-07-18T00:00:04+00:00", degrade=degrade5,
        )
    except FrameworkError:
        raised = True
    _check(raised, "WIRE5 PROVIDER verdict with no provider → RAISES (N6 invariant)")


def main() -> int:
    test_mint_forwarded()
    test_helpers()
    test_plan_marker_unmoved()
    test_forward_helpers()
    total = _passed + len(_failed)
    print(f"\n{_passed}/{total} checks passed")
    if _failed:
        print("FAILURES:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
