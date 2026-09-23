#!/usr/bin/env python3
"""INF-06 reliability carve — forwarded-vertex sweep / drain / GC / RESUBMIT smoke.

Plugin-side half of the holder-forward recoverability slice (the core-side mint +
queue-helpers + plan-[>]-unmoved matrix lives in
``ananta/src/ananta/services/inference_service/tests/forward_vertex_queue_smoke.py``).
Drives the REAL ``AutonomicAssignment`` riders + the REAL
``AgentMessagingPlugin._resubmit_vertex`` primitive against the shared REAL-SHAPE
state fake:

  S2  SWEEP serve-timeout: a ``forwarded`` row past the serve window increments
      the monotone ``attempts`` AND re-drives (RESUBMIT), WITHOUT hard-deleting —
      the re-mint would reset a deleted row's attempts and defeat the cap; a row
      still inside the window is untouched; at the cap the row flips to terminal
      ``failed`` (durable stall record) and is NOT re-driven.
  S3  RESUBMIT fresh-decode + method-agnostic (the load-bearing §6-bis property):
      the REAL ``_resubmit_vertex`` on a ``process_error`` row builds a fresh
      ``process_results`` initial vertex (observation REMOVED, instructions
      emptied) and submits THAT — never the recorded process_error decode
      (replay is forbidden). Same fresh vertex regardless of the recorded method.
  S6  RESUBMIT fault isolation: an unknown flow (no owning session) or a missing
      collaborator returns False and NEVER raises (per-row sweep/drain isolation).
  S4  DRAIN holder-transition: on a dark→lit claim the drain re-drives a
      ``forwarded`` row (dead prior holder) and HARD-deletes it (new occupancy);
      a terminal ``failed`` row is SKIPPED.
  S5  DRAIN plain-deferral: a ``deferred`` row re-drives via the SAME primitive
      (the pre-existing INF-01 §D.9 inert-drain gap this slice closes).
  GC  terminal-row GC: an AGED ``failed`` row is reaped; a fresh ``failed`` row
      and any non-failed row are kept.

Offline: the shared REAL-SHAPE state fake (no schema enforcement on this table).
Needs SOLET_NAME set (agent_messaging_plugin package init resolves
vault-scoped constants eagerly) — no default, raises if unset. No live platform.

Run from repo root:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/forward_vertex_redrive_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import SYS_AUTONOMIC_SLOT  # noqa: E402
from ananta.services.inference_service.deferred_vertex_queue import (  # noqa: E402
    increment_attempts,
    mark_terminal_failed,
    record_deferred_vertex,
    record_forwarded_vertex,
)
from ananta.services.inference_service.schema import (  # noqa: E402
    COL_ATTEMPTS,
    COL_FLOW_ID,
    COL_STATE,
    INFERENCE_DEFERRED_VERTEX_NAMESPACE,
    STATE_FAILED,
    STATE_FORWARDED,
    TABLE_INFERENCE_DEFERRED_VERTEX,
)
from ananta.services.inference_service.vertex_resolver import (  # noqa: E402
    VertexResolution,
    VertexRouting,
)

from agent_messaging_plugin.autonomic_assignment import AutonomicAssignment  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

_PAST = "2020-01-01T00:00:00+00:00"
_FUTURE = "2099-01-01T00:00:00+00:00"

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


class _ResubmitSpy:
    """Records (flow_id, method) re-drive calls; returns a controlled verdict."""

    def __init__(self, *, verdict: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self._verdict = verdict

    def __call__(self, flow_id: str, method: str) -> bool:
        self.calls.append((flow_id, method))
        return self._verdict


def _assignment(
    state: RealShapeState,
    *,
    resubmit_vertex: Any,
    forward_serve_window_seconds: int = 0,
    forward_attempts_cap: int = 5,
    terminal_gc_after_seconds: int = 0,
) -> AutonomicAssignment:
    """A real AutonomicAssignment wired only for the sweep/drain/GC riders.

    The bridge/registry collaborators are inert lambdas — the forwarded riders
    read only the state service + the injected re-drive primitive + the 3 knobs.
    """
    return AutonomicAssignment(
        state_service=lambda: state,
        list_active_bridges=lambda: [],
        bindings_for_bridge=lambda _bid: [],
        live_binding_for_session=lambda _sid: None,
        has_live_provider=lambda _agi: False,
        send_notice=lambda **_kw: True,
        grace_seconds=30,
        forward_completion=lambda _holder, _row: None,
        serve_window_seconds=900,
        resubmit_vertex=resubmit_vertex,
        forward_serve_window_seconds=forward_serve_window_seconds,
        forward_attempts_cap=forward_attempts_cap,
        terminal_gc_after_seconds=terminal_gc_after_seconds,
    )


def _rows(state: RealShapeState) -> list[dict[str, Any]]:
    return state.rows(
        INFERENCE_DEFERRED_VERTEX_NAMESPACE, TABLE_INFERENCE_DEFERRED_VERTEX,
    )


def _row(state: RealShapeState, flow_id: str) -> dict[str, Any] | None:
    return next((r for r in _rows(state) if r.get(COL_FLOW_ID) == flow_id), None)


def _seed_forwarded(
    state: RealShapeState, flow_id: str, *, stamp: str, is_error: bool = True,
) -> None:
    record_forwarded_vertex(
        state, is_error=is_error, role=SYS_AUTONOMIC_SLOT,
        holder_agent_instance_id="agi-dead", flow_id=flow_id, now_iso=stamp,
    )


# --------------------------------------------------------------------------
# S2 — SWEEP serve-timeout: re-drive past-window, cap→terminal, no hard-delete
# --------------------------------------------------------------------------
def test_sweep() -> None:
    print("S2 — forwarded serve-timeout sweep:")

    state = RealShapeState()
    _seed_forwarded(state, "flow-old", stamp=_PAST)     # past the window → re-drive
    _seed_forwarded(state, "flow-fresh", stamp=_FUTURE)  # inside the window → skip
    spy = _ResubmitSpy(verdict=True)
    a = _assignment(state, resubmit_vertex=spy, forward_serve_window_seconds=0)

    re_driven, terminal = a.forwarded.sweep_serve_timeouts()
    _check(
        (re_driven, terminal) == (1, 0) and spy.calls == [("flow-old", "process_error")],
        "S2a past-window row re-driven (method carried), fresh row skipped",
    )
    old = _row(state, "flow-old")
    _check(
        old is not None and old.get(COL_ATTEMPTS) == 1 and old.get(COL_STATE) == STATE_FORWARDED,
        "S2b re-driven row NOT hard-deleted; attempts bumped to 1 (monotone preserved)",
    )
    fresh = _row(state, "flow-fresh")
    _check(
        fresh is not None and fresh.get(COL_ATTEMPTS) is None,
        "S2c inside-window row untouched (no attempt spent)",
    )

    # cap: a row already at attempts=cap-1 flips terminal, is NOT re-driven.
    cap_state = RealShapeState()
    _seed_forwarded(cap_state, "flow-cap", stamp=_PAST)
    increment_attempts(cap_state, flow_id="flow-cap")  # attempts=1; cap=2 → next=2 hits cap
    cap_spy = _ResubmitSpy(verdict=True)
    cap_a = _assignment(
        cap_state, resubmit_vertex=cap_spy, forward_serve_window_seconds=0,
        forward_attempts_cap=2,
    )
    re2, term2 = cap_a.forwarded.sweep_serve_timeouts()
    capped = _row(cap_state, "flow-cap")
    _check(
        (re2, term2) == (0, 1) and cap_spy.calls == []
        and capped is not None and capped.get(COL_STATE) == STATE_FAILED
        and capped.get(COL_ATTEMPTS) == 2,
        "S2d at cap → terminal 'failed' (attempts++), NOT re-driven",
    )


# --------------------------------------------------------------------------
# S3 / S6 — RESUBMIT fresh-decode (method-agnostic, not replay) + fault isolation
# --------------------------------------------------------------------------
_PROCESS_RESULTS_TEMPLATE: dict[str, Any] = {
    "name": "process_results_template",
    "process_key": "service_interface::inference_service::process_results",
    "arguments": {
        "params": {
            "prompt": {
                # the recorded (process_error) decode shape carries an observation —
                # the fresh re-entry MUST strip it (fresh input, not replay).
                "observation": {"result": "the recorded failed action result"},
                "user": {"instructions": ["stale recorded instructions"]},
            },
        },
    },
}


class _Registry:
    def get_process_registry(self) -> dict[str, Any]:
        return {
            "processes": {
                "service_interface::inference_service::process_results": {
                    "action_definition_template": _PROCESS_RESULTS_TEMPLATE,
                },
            },
        }


class _SubmitSpy:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.contexts: list[object] = []

    def submit_action_definition(
        self, *, action_definition: dict[str, Any], context: object,
    ) -> None:
        self.submitted.append(action_definition)
        self.contexts.append(context)


class _ContextBuilder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def build_context(self, *, session_id: str, flow_id: str) -> object:
        self.calls.append((session_id, flow_id))
        return SimpleNamespace(session_id=session_id, flow_id=flow_id)


def _fake_plugin(
    *, session_for_flow: str | None, submit: _SubmitSpy, builder: _ContextBuilder,
) -> SimpleNamespace:
    return SimpleNamespace(
        _flow_manager=SimpleNamespace(
            get_flow_session_id=lambda _flow: session_for_flow,
        ),
        orchestrator_ref=_Registry(),
        action_factory=submit,
        _compilation_context_builder=builder,
    )


def test_resubmit_fresh_decode() -> None:
    print("S3/S6 — RESUBMIT fresh-decode (method-agnostic), fault isolation:")

    submit = _SubmitSpy()
    builder = _ContextBuilder()
    fake = _fake_plugin(session_for_flow="sess-1", submit=submit, builder=builder)

    # method='process_error' → the re-entry is STILL a fresh process_results
    # initial vertex with the observation stripped (never the recorded decode).
    ok = AgentMessagingPlugin._resubmit_vertex(fake, "flow-x", "process_error")  # noqa: SLF001
    submitted = submit.submitted[0] if submit.submitted else {}
    prompt = submitted.get("arguments", {}).get("params", {}).get("prompt", {})
    _check(
        ok is True
        and len(submit.submitted) == 1
        and submitted.get("name") == "initial_vertex"
        and "observation" not in prompt
        and prompt.get("user", {}).get("instructions") == []
        and submitted.get("session_id") == "sess-1"
        and submitted.get("flow_id") == "flow-x",
        "S3a process_error row → fresh process_results initial_vertex (observation stripped)",
    )
    _check(
        builder.calls == [("sess-1", "flow-x")],
        "S3b compilation context built for the SAME (session, flow) — "
        "fresh decode of current state",
    )

    # the deep-copy did not mutate the shared template (no replay leak).
    _check(
        "observation" in _PROCESS_RESULTS_TEMPLATE["arguments"]["params"]["prompt"],
        "S3c the shared template is untouched (fresh action is a deep copy)",
    )

    # S6 fault isolation — unknown flow (no owning session): False, never raises.
    submit2 = _SubmitSpy()
    fake_unknown = _fake_plugin(
        session_for_flow=None, submit=submit2, builder=_ContextBuilder(),
    )
    unknown_ok = AgentMessagingPlugin._resubmit_vertex(  # noqa: SLF001
        fake_unknown, "flow-ghost", "process_results",
    )
    _check(
        unknown_ok is False and submit2.submitted == [],
        "S6a unknown flow → False, no submit, no raise",
    )

    # S6 fault isolation — a missing collaborator: False, never raises.
    fake_missing = SimpleNamespace(
        _flow_manager=None, orchestrator_ref=_Registry(),
        action_factory=_SubmitSpy(), _compilation_context_builder=_ContextBuilder(),
    )
    missing_ok = AgentMessagingPlugin._resubmit_vertex(  # noqa: SLF001
        fake_missing, "flow-x", "process_results",
    )
    _check(missing_ok is False, "S6b missing collaborator → False, no raise")


# --------------------------------------------------------------------------
# S4 / S5 — DRAIN: holder-transition (forwarded) + plain-deferral, skip failed
# --------------------------------------------------------------------------
def test_drain() -> None:
    print("S4/S5 — first-claim drain (holder-transition + plain-deferral):")

    state = RealShapeState()
    # a forwarded row (dead prior holder → dark-lane invariant), a deferred row,
    # and a terminal failed row (must be skipped, never re-driven).
    _seed_forwarded(state, "flow-fwd", stamp=_PAST)
    record_deferred_vertex(
        state, is_error=False,
        resolution=VertexResolution(VertexRouting.DEFER, None, SYS_AUTONOMIC_SLOT, None),
        flow_id="flow-def",
    )
    _seed_forwarded(state, "flow-terminal", stamp=_PAST)
    mark_terminal_failed(state, flow_id="flow-terminal")

    spy = _ResubmitSpy(verdict=True)
    a = _assignment(state, resubmit_vertex=spy)
    drained, remaining = a._drain_deferred("smoke")  # noqa: SLF001 — drive the drain directly

    redriven_flows = {c[0] for c in spy.calls}
    _check(
        drained == 2 and remaining == 0
        and redriven_flows == {"flow-fwd", "flow-def"},
        "S4/S5a drain re-drives BOTH forwarded (holder-transition) + deferred (vacancy-fill)",
    )
    _check(
        ("flow-terminal", "process_error") not in spy.calls
        and _row(state, "flow-terminal") is not None,
        "S4b terminal 'failed' row SKIPPED (not re-driven, left for GC)",
    )
    _check(
        _row(state, "flow-fwd") is None and _row(state, "flow-def") is None,
        "S5b re-driven rows HARD-deleted (unique flow_id slots freed)",
    )


# --------------------------------------------------------------------------
# GC — aged terminal 'failed' rows reaped; fresh + non-failed kept
# --------------------------------------------------------------------------
def test_gc() -> None:
    print("GC — terminal-row garbage collection:")

    state = RealShapeState()
    _seed_forwarded(state, "flow-aged", stamp=_PAST)
    mark_terminal_failed(state, flow_id="flow-aged")
    _row(state, "flow-aged")["updated_at"] = _PAST  # type: ignore[index]  # aged

    _seed_forwarded(state, "flow-recent", stamp=_PAST)
    mark_terminal_failed(state, flow_id="flow-recent")
    _row(state, "flow-recent")["updated_at"] = _FUTURE  # type: ignore[index]  # fresh

    _seed_forwarded(state, "flow-live", stamp=_PAST)  # non-failed — never GC'd

    a = _assignment(
        state, resubmit_vertex=_ResubmitSpy(), terminal_gc_after_seconds=0,
    )
    reaped = a.forwarded.gc_terminal_rows()
    _check(
        reaped == 1 and _row(state, "flow-aged") is None,
        "GC1 aged 'failed' row reaped",
    )
    _check(
        _row(state, "flow-recent") is not None and _row(state, "flow-live") is not None
        and _row(state, "flow-live").get(COL_STATE) == STATE_FORWARDED,  # type: ignore[union-attr]
        "GC2 fresh 'failed' row + non-failed forwarded row KEPT",
    )


def main() -> int:
    test_sweep()
    test_resubmit_fresh_decode()
    test_drain()
    test_gc()
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
