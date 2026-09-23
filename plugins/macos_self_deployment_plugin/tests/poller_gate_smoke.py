#!/usr/bin/env python3
"""C2 poller-gate smoke (no pytest).

Design ``2026-06-27_true_local_blue_green_materialized_artifacts_design.md``,
Phase-2 review finding C2 (Codex), ruled in-campaign foundation correctness:
the platform action-queue poller's color-active gate
(``EventOrchestrator.is_active_color``) was wired (the poller consults it)
but NEVER flipped ``False`` anywhere — so a draining (blue) process's poller
kept polling and could claim its OWN ``complete_swap`` finisher and SIGTERM
itself, skipping its unregister and leaving the action stuck 'processing'.

The fix flips the gate ``False`` at quiesce, BEFORE the ``complete_swap`` row
is enqueued, so only the new color's poller ever sees it; ``swap_rollback``
restores it ``True`` on reactivation.

This asserts the unit-testable half:
  Part 1 — on a successful swap-cutover, the color-active gate is flipped
    ``False`` exactly once, and that flip happens BEFORE ``complete_swap`` is
    enqueued (captured by the enqueue-count observed at flip time).
  Part 2 — ``swap_rollback`` restores the gate to ``True`` on reactivation.

The remaining C2 acceptance — post-cutover NO stale router binding for the
dead color + NO stuck 'processing' ``complete_swap`` — needs live pollers and
is part of the operator-coordinated proving run (same proxy boundary as
§8.1).

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/poller_gate_smoke.py
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_PLUGIN_SRC), str(_REPO_ROOT / "ananta" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from macos_self_deployment_plugin import green_candidate, heartbeat_lifecycle  # noqa: E402
from macos_self_deployment_plugin.constants import (  # noqa: E402
    COLOR_BLUE,
)
from macos_self_deployment_plugin.plugin import MacosSelfDeploymentPlugin  # noqa: E402
from macos_self_deployment_plugin.preflight_probe_runner import ProbeOutcome  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CandidatePaths,
    GcResult,
    SwapResult,
)
from macos_self_deployment_plugin.router_client import RouterClient  # noqa: E402
from macos_self_deployment_plugin.schema_preflight import PreflightVerdict  # noqa: E402
from macos_self_deployment_plugin.swap_orchestrator import SwapOrchestrator  # noqa: E402


def _smoke_green_probe(*, candidate: CandidatePaths, app_home: Path) -> ProbeOutcome:
    """GTE-06 seam: a GREEN probe so this smoke's pre-existing flow is unchanged."""
    del app_home
    return ProbeOutcome(
        ok=True,
        payload={"ok": True, "duration_ms": 0, "release_id": candidate.release_id},
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


_GREEN_PORT = 50066


class _SuccessRouter:
    """Happy-path stub: blue active, green registers, activate succeeds."""

    def __init__(self) -> None:
        self.registered: dict[str, int] = {}

    def status(self) -> dict[str, Any]:
        colors = [{"instance_id": iid, "port": p} for iid, p in self.registered.items()]
        return {"active_color": COLOR_BLUE, "active_instance_id": "blue-id", "colors": colors}

    def register_color(
        self, port: int, color: str, instance_id: str,
        *, streamable_port: int | None = None,
    ) -> dict[str, Any]:
        del color, streamable_port
        self.registered[instance_id] = port
        return {"accepted": True}

    def activate(self, color: str, instance_id: str) -> dict[str, Any]:
        del color, instance_id
        return {"activated": True, "previous_color": COLOR_BLUE, "drain_window_seconds": 30}


class _SuccessReleaseManager:
    def build_candidate(
        self, *, manifest_etag: str = "",
        manifest_plugins: tuple[str, ...] | None = None,
        schema_snapshot_fn: object = None,
    ) -> CandidatePaths:
        del manifest_etag, manifest_plugins, schema_snapshot_fn
        base = Path("/nonexistent/rel-ok")
        return CandidatePaths(
            release_id="rel-ok", release_dir=base, code_root=base / "code",
            venv_python=base / "venv" / "bin" / "python3", version_file=base / "VERSION",
            missing_pth_targets=(), schema_snapshot=None,
        )

    def cutover(self, candidate: CandidatePaths) -> SwapResult:
        return SwapResult(current=candidate.release_id, previous="rel-prev")

    def gc(self, *, keep: int | None = None) -> GcResult:
        del keep
        return GcResult(deleted=(), retained=())

    @property
    def current_release(self) -> str | None:
        return None

    @property
    def previous_release(self) -> str | None:
        return None

    def current_schema_snapshot(self) -> dict[str, object] | None:
        return None

    def candidate_for(self, release_id: str) -> CandidatePaths:
        del release_id
        raise NotImplementedError("forward-path smoke: candidate_for unused")

    def rollback(self) -> SwapResult:
        raise NotImplementedError("forward-path smoke: rollback unused")


class _RecordingActionFactory:
    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []

    def submit_action_definition(
        self, action_definition: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> str:
        del context
        self.submissions.append(action_definition)
        return "ae-complete-swap"


def _additive(
    candidate: CandidatePaths, *,
    current_snapshot: dict[str, object] | None,
    current_release_exists: bool,
) -> PreflightVerdict:
    del candidate, current_snapshot, current_release_exists
    return PreflightVerdict(is_additive=True, breaking_changes=())


def _part1_flip_before_enqueue() -> None:
    stub = _SuccessRouter()
    action_factory = _RecordingActionFactory()
    # Each flip records (active, #complete_swaps enqueued so far) — proving order.
    flip_log: list[tuple[bool, int]] = []

    def rec_set_color_active(active: bool) -> None:
        flip_log.append((active, len(action_factory.submissions)))

    def fake_spawn(
        app_home: Path, next_color: str, next_instance_id: str,
        solet_name: str, candidate: CandidatePaths,
    ) -> int:
        del app_home, solet_name, candidate
        stub.register_color(_GREEN_PORT, next_color, next_instance_id)
        return 4242

    def _always_reachable(*_a: object, **_k: object) -> bool:
        return True

    original_probe = green_candidate._probe_port_reachable  # noqa: SLF001
    green_candidate._probe_port_reachable = _always_reachable  # type: ignore[assignment]  # noqa: SLF001
    try:
        orch = SwapOrchestrator(
            router_client=cast("RouterClient", stub),
            action_factory=action_factory,
            session_factory=lambda: "sess",
            solet_name="smoke",
            release_manager=_SuccessReleaseManager(),
            schema_preflight=_additive,
            preflight_probe=_smoke_green_probe,
            set_color_active=rec_set_color_active,
            spawn_fn=fake_spawn,
            ready_timeout_seconds=2,
            ready_poll_interval_seconds=0.01,
        )
        result = orch.restart(
            reason="poller-gate-smoke",
            expected_etag="etag-x",
            dry_run=False,
            app_home=Path("/nonexistent/profile"),
            self_instance_id="blue-id",
            self_color=COLOR_BLUE,
            set_active_targets=[],
        )
    finally:
        green_candidate._probe_port_reachable = original_probe  # noqa: SLF001

    _check(result.status.value == "queued", f"swap succeeded (QUEUED): {result.status}")
    _check(
        flip_log == [(False, 0)],
        f"color-active gate flipped False exactly once, BEFORE complete_swap "
        f"was enqueued (enqueued-count at flip = 0): {flip_log}",
    )
    _check(
        len(action_factory.submissions) == 1,
        f"complete_swap WAS enqueued (after the flip): {len(action_factory.submissions)}",
    )


class _RollbackRouter:
    """A live blue drain target is reactivated by a green rollback action."""

    def __init__(self) -> None:
        self.active_instance_id = "green-active"

    def status(self) -> dict[str, Any]:
        return {
            "active_color": (
                COLOR_BLUE
                if self.active_instance_id == "blue-draining"
                else "green"
            ),
            "active_instance_id": self.active_instance_id,
            "drain_entries": [{"color": COLOR_BLUE, "instance_id": "blue-draining"}],
        }

    def rollback(self, color: str, instance_id: str) -> dict[str, Any]:
        if color != COLOR_BLUE or instance_id != "blue-draining":
            return {"rolled_back": False, "reason": "wrong_target"}
        self.active_instance_id = instance_id
        return {"rolled_back": True, "active_color": color}


class _HeartbeatRollbackRouter(_RollbackRouter):
    """Drive green → blue between real heartbeat-loop iterations."""

    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__()
        self._stop_event = stop_event
        self._heartbeat_count = 0
        self._status_count = 0
        self.executing_plugin: MacosSelfDeploymentPlugin | None = None

    def heartbeat(self, _instance_id: str) -> dict[str, Any]:
        self._heartbeat_count += 1
        if self._heartbeat_count == 2:
            if self.executing_plugin is None:
                raise RuntimeError("test router has no executing plugin")
            self.executing_plugin.swap_rollback("operator-rollback")
        return {"alive": True}

    def status(self) -> dict[str, Any]:
        self._status_count += 1
        snapshot = super().status()
        if self.active_instance_id == "blue-draining" and self._status_count >= 5:
            self._stop_event.set()
        return snapshot


def _part2_rollback_restores_gate() -> None:
    stop_event = threading.Event()
    router = _HeartbeatRollbackRouter(stop_event)
    executing_plugin = MacosSelfDeploymentPlugin()
    executing_plugin._router_client = cast("RouterClient", router)  # noqa: SLF001
    executing_plugin.orchestrator_ref = SimpleNamespace(is_active_color=True)  # type: ignore[attr-defined]
    reactivated_plugin = MacosSelfDeploymentPlugin()
    reactivated_plugin.orchestrator_ref = SimpleNamespace(is_active_color=False)  # type: ignore[attr-defined]
    executing_restore_calls: list[bool] = []
    executing_plugin._set_color_active = executing_restore_calls.append  # type: ignore[method-assign]
    router.executing_plugin = executing_plugin
    original_interval = heartbeat_lifecycle.DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    heartbeat_lifecycle.DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 0.01
    try:
        heartbeat_lifecycle._run_steady_state_heartbeat(
            client=cast("RouterClient", router),
            port=0,
            self_color=COLOR_BLUE,
            self_instance_id="blue-draining",
            stop_event=stop_event,
            pending_finisher_file=None,
            current_release_lookup=None,
            logger=reactivated_plugin.logger,
            set_color_active=reactivated_plugin._set_color_active,
        )
    finally:
        heartbeat_lifecycle.DEFAULT_HEARTBEAT_INTERVAL_SECONDS = original_interval
    _check(
        executing_restore_calls == [],
        "rollback action did NOT flip the executing process's already-active gate",
    )
    _check(
        reactivated_plugin.orchestrator_ref.is_active_color is True,  # type: ignore[attr-defined]
        "reactivated process restored its OWN poller gate after router transition",
    )


def run_smoke() -> int:
    print("=== poller_gate_smoke (C2: gate draining poller OFF before complete_swap) ===")
    _part1_flip_before_enqueue()
    _part2_rollback_restores_gate()
    print(f"\npoller_gate_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
