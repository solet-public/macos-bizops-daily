#!/usr/bin/env python3
"""REL-03 — swap-window terminated-flow guard (no pytest).

Background: during a blue-green cutover (~10s color overlap) a flow whose
``trigger_data`` namespace lived on the dying color hits
``FrameworkError: Empty source_namespace in flow trigger_data`` in
``inference_transaction._resolve_io_process_key`` when its
``process_error`` / ``process_results`` action re-dispatches on the new color.
The first such failure already terminates the flow (``_should_skip_error_routing``
returns terminal for inference_service keys). But the incident log
(``workbench/2026-07-01_incident_green_release_error_routing_storm.md``,
window 19:48:56–19:49:06) shows the flow's OTHER already-queued
``process_error`` siblings (distinct action_ids) each then execute on the new
color, each re-hitting ``_resolve_io_process_key``, each emitting a full
traceback and re-terminating the already-dead flow — the burst (~6-7 doomed
siblings per flow, one straggler ~9.4s later).

The guard: ``_terminate_flow`` tombstones the flow_id; ``_process_action`` drops
any subsequently-dequeued ``process_error`` / ``process_results`` sibling of a
tombstoned flow BEFORE execution, marking it failed WITHOUT re-routing (so no
traceback, no re-terminate). The drop is scoped to exactly those two VERTEX
inference keys — terminal EDGE_SINK deliveries, bridge deliver_error escape-valve
actions, and cleanup for a failed flow pass through untouched.

Cases (behavioral — a spy ``action_processor`` records ``execute_action``):
  A. ``_terminate_flow`` tombstones the flow (the single choke point) AND writes
     the canonical ``flows.status=failed`` record.
  B. A ``process_error`` sibling of a tombstoned flow is DROPPED — execute_action
     called ZERO times; the action row is marked failed (terminal, no dispatch).
  C. A ``process_results`` sibling of a tombstoned flow is DROPPED (the
     unevidenced-in-log key — pinned so both members of the class are covered).
  D. Tombstone MISS: a ``process_error`` action whose flow was never terminated
     executes normally (execute_action called once).
  E. EDGE_SINK pass-through: a terminal ``post_message`` delivery for a
     tombstoned flow still executes (the guard's scope does not swallow the
     signal that reports the failure).
  F. Predicate + bounded-FIFO: ``_is_terminated_flow_sibling`` truth table and
     the tombstone cap eviction (memory is bounded without a TTL).

Project policy: no pytest. Offline — no live solet / LM Studio / Postgres. Exits 0
on success, 1 on first-failed-check aggregate.

Run from repo root:
    .venv/bin/python3 ananta/tests/core/substrate_contracts/swap_window_terminated_flow_guard_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.actions.action_queue_poller import (  # noqa: E402
    _INFERENCE_PROCESS_ERROR_KEY,
    _INFERENCE_PROCESS_RESULTS_KEY,
    _TERMINATED_FLOW_TOMBSTONE_CAP,
    QueuedAction,
)

# substrate_contract_fixtures lives beside this file; import as a sibling module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from substrate_contract_fixtures import (  # noqa: E402
    Checker,
    RecordingStateService,
    build_offline_poller,
)

_EDGE_SINK_KEY = "plugin::io_console::post_message"  # wint:negative-fixture


class SpyActionProcessor:
    """Records every ``execute_action`` invocation by action id."""

    def __init__(self) -> None:
        self.execute_calls: list[str] = []

    def execute_action(self, action: QueuedAction) -> dict[str, object]:
        self.execute_calls.append(action.id)
        return {"success": True}


def _make_poller(state: RecordingStateService) -> tuple[Any, SpyActionProcessor]:
    """An offline poller with the swap-guard collaborators wired.

    Reuses ``build_offline_poller`` (object.__new__ + state_service pre-warm),
    then sets the guard field + a spy processor and stubs the post-gate
    preparation machinery so the test isolates the GATE behaviour (does a doomed
    sibling reach ``execute_action``) from the unrelated FRG-token / validation
    path. Everything the guard itself reads (``state_service``,
    ``_terminated_flow_ids``) is real.
    """
    poller: Any = build_offline_poller(state)
    poller._terminated_flow_ids = OrderedDict()
    spy = SpyActionProcessor()
    poller.action_processor = spy
    # Post-gate isolation: the gate runs BEFORE _prepare_action_for_execution,
    # so stubbing these does not weaken the drop/pass-through assertions — it
    # only removes the FRG/validation collaborators the guard does not touch.
    poller._prepare_action_for_execution = lambda *_: True
    poller._resolve_io_context = lambda *_: None
    poller._requires_main_thread = lambda *_: True
    poller._mark_action_completed = lambda *_: None
    return poller, spy


def _action(process_key: str, flow_id: str | None, action_id: str) -> QueuedAction:
    return QueuedAction(
        id=action_id,
        process_key=process_key,
        parameters="{}",
        notes="",
        created_at="2026-07-02T00:00:00+00:00",
        flow_id=flow_id,
    )


def _failed_action_ids(state: RecordingStateService) -> list[str]:
    """Action ids the poller marked failed via update_state (no dispatch)."""
    ids: list[str] = []
    for call in state.update_calls:
        query = call.get("query", {})
        updates = call.get("updates", {})
        if query.get("table") == "action_events" and updates.get("status") == "failed":
            filters = query.get("filters", {})
            ident = filters.get("id")
            if isinstance(ident, str):
                ids.append(ident)
    return ids


def _flow_failed(state: RecordingStateService, flow_id: str) -> bool:
    for call in state.update_calls:
        query = call.get("query", {})
        updates = call.get("updates", {})
        if (
            query.get("table") == "flows"
            and query.get("filters", {}).get("id") == flow_id
            and updates.get("status") == "failed"
        ):
            return True
    return False


def main() -> int:
    checker = Checker("REL-03 swap-window terminated-flow guard")

    # ── Case A: _terminate_flow is the choke point — tombstones + fails flow ──
    state_a = RecordingStateService()
    poller_a, _ = _make_poller(state_a)
    poller_a._terminate_flow("flow-doomed")
    checker.check(
        "flow-doomed" in poller_a._terminated_flow_ids,
        "A1: _terminate_flow tombstones the flow_id",
    )
    checker.check(
        _flow_failed(state_a, "flow-doomed"),
        "A2: _terminate_flow writes the canonical flows.status=failed record",
    )

    # ── Case B: process_error sibling of a tombstoned flow is DROPPED ──
    state_b = RecordingStateService()
    poller_b, spy_b = _make_poller(state_b)
    poller_b._terminate_flow("flow-doomed")
    state_b.update_calls.clear()  # isolate the sibling's writes from the terminate
    sibling_err = _action(_INFERENCE_PROCESS_ERROR_KEY, "flow-doomed", "ae-sib-err")
    asyncio.run(poller_b._process_action(sibling_err))
    checker.check(
        spy_b.execute_calls == [],
        "B1: doomed process_error sibling never reaches execute_action",
    )
    checker.check(
        "ae-sib-err" in _failed_action_ids(state_b),
        "B2: dropped sibling is marked failed (terminal, no re-poll)",
    )

    # ── Case C: process_results sibling of a tombstoned flow is DROPPED ──
    state_c = RecordingStateService()
    poller_c, spy_c = _make_poller(state_c)
    poller_c._terminate_flow("flow-doomed")
    sibling_res = _action(_INFERENCE_PROCESS_RESULTS_KEY, "flow-doomed", "ae-sib-res")
    asyncio.run(poller_c._process_action(sibling_res))
    checker.check(
        spy_c.execute_calls == [],
        "C1: doomed process_results sibling never reaches execute_action",
    )

    # ── Case D: tombstone MISS — a live flow's process_error executes ──
    state_d = RecordingStateService()
    poller_d, spy_d = _make_poller(state_d)
    poller_d._terminate_flow("flow-doomed")  # a DIFFERENT flow is tombstoned
    live = _action(_INFERENCE_PROCESS_ERROR_KEY, "flow-live", "ae-live")
    asyncio.run(poller_d._process_action(live))
    checker.check(
        spy_d.execute_calls == ["ae-live"],
        "D1: tombstone-miss process_error executes normally",
    )

    # ── Case E: EDGE_SINK delivery for a tombstoned flow still executes ──
    state_e = RecordingStateService()
    poller_e, spy_e = _make_poller(state_e)
    poller_e._terminate_flow("flow-doomed")
    edge_sink = _action(_EDGE_SINK_KEY, "flow-doomed", "ae-edge")
    asyncio.run(poller_e._process_action(edge_sink))
    checker.check(
        spy_e.execute_calls == ["ae-edge"],
        "E1: terminal EDGE_SINK delivery of a failed flow is NOT dropped",
    )

    # ── Case F: predicate truth table + bounded-FIFO cap ──
    state_f = RecordingStateService()
    poller_f, _ = _make_poller(state_f)
    poller_f._remember_terminated_flow("flow-doomed")
    checker.check(
        poller_f._is_terminated_flow_sibling(
            _action(_INFERENCE_PROCESS_ERROR_KEY, "flow-doomed", "x")
        ),
        "F1: process_error + tombstoned flow => True",
    )
    checker.check(
        poller_f._is_terminated_flow_sibling(
            _action(_INFERENCE_PROCESS_RESULTS_KEY, "flow-doomed", "x")
        ),
        "F2: process_results + tombstoned flow => True",
    )
    checker.check(
        not poller_f._is_terminated_flow_sibling(
            _action(_EDGE_SINK_KEY, "flow-doomed", "x")
        ),
        "F3: non-processing key (EDGE_SINK) + tombstoned flow => False",
    )
    checker.check(
        not poller_f._is_terminated_flow_sibling(
            _action(_INFERENCE_PROCESS_ERROR_KEY, "flow-live", "x")
        ),
        "F4: process_error + non-tombstoned flow => False",
    )
    checker.check(
        not poller_f._is_terminated_flow_sibling(
            _action(_INFERENCE_PROCESS_ERROR_KEY, None, "x")
        ),
        "F5: process_error with no flow_id => False",
    )

    # Bounded FIFO: overfill past the cap; size stays capped and the oldest is
    # evicted while the newest survives.
    state_g = RecordingStateService()
    poller_g, _ = _make_poller(state_g)
    for i in range(_TERMINATED_FLOW_TOMBSTONE_CAP + 5):
        poller_g._remember_terminated_flow(f"flow-{i}")
    checker.check(
        len(poller_g._terminated_flow_ids) == _TERMINATED_FLOW_TOMBSTONE_CAP,
        "F6: tombstone size is bounded by the cap",
    )
    checker.check(
        "flow-0" not in poller_g._terminated_flow_ids
        and f"flow-{_TERMINATED_FLOW_TOMBSTONE_CAP + 4}" in poller_g._terminated_flow_ids,
        "F7: bounded FIFO evicts oldest, keeps newest",
    )

    return checker.summary()


if __name__ == "__main__":
    sys.exit(main())
