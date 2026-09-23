#!/usr/bin/env python3
"""§4.7 cutover-failure-compensation smoke (no pytest).

Design ``2026-06-27_true_local_blue_green_materialized_artifacts_design.md``
§4.7, hardened after the Phase-2 adversarial review (Codex + Reviewer-C).
Covers the two defects in the after-``activate``-before-``complete_swap``
window, each by injecting the EXACT failure mode:

F1 (defense-in-depth): ``cutover``'s ledger I/O brackets its inner symlink
``try``, so a RAW ``OSError`` / ``JSONDecodeError`` / ``KeyError`` can
escape its ReleaseManagerError contract. The orchestrator's broadened
``except`` must still run the compensation for EVERY such raw type — not
let it escape past ``_handle_cutover_failure`` (router cut over, no
rollback). Asserted: FAILED via compensation (router rollback + candidate
killed + NO complete_swap) for each of {ReleaseManagerError, OSError,
JSONDecodeError, KeyError}.

F2 (state-worsening): the candidate kill is GATED on a CONFIRMED router
rollback. If rollback does NOT take — RPC error OR an explicit refusal
(``rolled_back=False`` / drain expired) — the router may still route to
the candidate, so killing it would route live traffic to a DEAD color.
Asserted: candidate LEFT ALIVE, NOT unregistered, status
``NEEDS_INTERVENTION`` with reason_code ``cutover_router_rollback_failed``
(the F2-iv typed-escalation retrofit), NO complete_swap. The CONFIRMED-
rollback F1 cases carry status ``FAILED`` + reason_code
``cutover_compensated``.

Drives the real ``SwapOrchestrator.restart`` with a recording stub router
and a fake ``ReleaseManager`` whose ``cutover`` raises; the candidate is a
real ``sleep`` child so its kill-vs-alive is directly observable. The TCP
probe is stubbed; no real solet is launched.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/cutover_failure_smoke.py
"""

from __future__ import annotations

import subprocess
import sys
from json import JSONDecodeError
from pathlib import Path
from typing import Any, cast

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_PLUGIN_SRC), str(_REPO_ROOT / "ananta" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from macos_self_deployment_plugin import green_candidate  # noqa: E402
from macos_self_deployment_plugin.constants import COLOR_BLUE  # noqa: E402
from macos_self_deployment_plugin.preflight_probe_runner import ProbeOutcome  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CandidatePaths,
    GcResult,
    ReleaseManagerError,
    SwapResult,
)
from macos_self_deployment_plugin.router_client import (  # noqa: E402
    RouterClient,
    RouterClientError,
)
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

# rollback-mode tokens.
_ROLLBACK_OK = "ok"
_ROLLBACK_RPC_ERROR = "rpc_error"
_ROLLBACK_REFUSED = "refused"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _StubRouter:
    """Records compensation calls; ``rollback`` behaviour is configurable."""

    def __init__(self, rollback_mode: str) -> None:
        self._rollback_mode = rollback_mode
        self.registered: dict[str, int] = {}
        self.rollback_calls: list[tuple[str, str]] = []
        self.unregister_calls: list[str] = []

    def status(self) -> dict[str, Any]:
        colors = [{"instance_id": iid, "port": p} for iid, p in self.registered.items()]
        return {
            "active_color": COLOR_BLUE,
            "active_instance_id": "blue-id",
            "colors": colors,
        }

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

    def rollback(self, color: str, instance_id: str) -> dict[str, Any]:
        self.rollback_calls.append((color, instance_id))
        if self._rollback_mode == _ROLLBACK_RPC_ERROR:
            raise RouterClientError("rollback", "injected rollback RPC failure")
        if self._rollback_mode == _ROLLBACK_REFUSED:
            return {"rolled_back": False, "reason": "drain_window_expired"}
        return {"rolled_back": True, "active_color": color}

    def unregister_color(self, instance_id: str) -> dict[str, Any]:
        self.unregister_calls.append(instance_id)
        return {"unregistered": True}


class _CutoverRaisingReleaseManager:
    """build_candidate succeeds; cutover raises the injected exception."""

    def __init__(self, cutover_exc: Exception) -> None:
        self._cutover_exc = cutover_exc

    def gc(self, *, keep: int | None = None) -> GcResult:
        del keep
        return GcResult(deleted=(), retained=())

    def build_candidate(
        self, *, manifest_etag: str = "",
        manifest_plugins: tuple[str, ...] | None = None,
        schema_snapshot_fn: object = None,
    ) -> CandidatePaths:
        del manifest_etag, manifest_plugins, schema_snapshot_fn
        base = Path("/nonexistent/rel-cutover-fail")
        return CandidatePaths(
            release_id="rel-cutover-fail",
            release_dir=base,
            code_root=base / "code",
            venv_python=base / "venv" / "bin" / "python3",
            version_file=base / "VERSION",
            missing_pth_targets=(),
            schema_snapshot=None,
        )

    def cutover(self, candidate: CandidatePaths) -> SwapResult:
        del candidate
        raise self._cutover_exc

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
    """Records complete_swap submissions — must stay empty on every failure path."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []

    def submit_action_definition(
        self, action_definition: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> str:
        del context
        self.submissions.append(action_definition)
        return "ae-should-not-happen"


def _additive(
    candidate: CandidatePaths, *,
    current_snapshot: dict[str, object] | None,
    current_release_exists: bool,
) -> PreflightVerdict:
    del candidate, current_snapshot, current_release_exists
    return PreflightVerdict(is_additive=True, breaking_changes=())


def _spawn_sleep_child() -> subprocess.Popen[bytes]:
    """A real, signalable stand-in for the candidate green process."""
    return subprocess.Popen(["/bin/sleep", "30"])


def _run_case(*, cutover_exc: Exception, rollback_mode: str) -> dict[str, Any]:
    """Drive restart() once; return observations for assertions."""
    stub = _StubRouter(rollback_mode)
    action_factory = _RecordingActionFactory()
    release_mgr = _CutoverRaisingReleaseManager(cutover_exc)
    child = _spawn_sleep_child()

    def fake_spawn(
        app_home: Path, next_color: str, next_instance_id: str,
        solet_name: str, candidate: CandidatePaths,
    ) -> int:
        del app_home, solet_name, candidate
        stub.register_color(50055, next_color, next_instance_id)
        return child.pid

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
            release_manager=release_mgr,
            schema_preflight=_additive,
            preflight_probe=_smoke_green_probe,
            set_color_active=lambda _active: None,
            spawn_fn=fake_spawn,
            ready_timeout_seconds=2,
            ready_poll_interval_seconds=0.01,
        )
        result = orch.restart(
            reason="cutover-fail-smoke",
            expected_etag="etag-x",
            dry_run=False,
            app_home=Path("/nonexistent/profile"),
            self_instance_id="blue-id",
            self_color=COLOR_BLUE,
            set_active_targets=[],
        )
    finally:
        green_candidate._probe_port_reachable = original_probe  # noqa: SLF001

    # Liveness via wait() (reaps a SIGKILLed child — avoids the zombie-looks-
    # alive trap of os.kill(pid, 0)). A killed candidate returns promptly; a
    # left-alive one times out (then we reap it for cleanup).
    try:
        child.wait(timeout=2.0)
        candidate_alive = False
    except subprocess.TimeoutExpired:
        candidate_alive = True
        child.kill()
        child.wait()
    return {
        "status": result.status.value,
        "reason_code": result.reason_code,
        "message": result.message,
        "rollback_calls": stub.rollback_calls,
        "unregister_calls": stub.unregister_calls,
        "submissions": action_factory.submissions,
        "candidate_alive": candidate_alive,
    }


def _f1_cases() -> None:
    """F1: every raw exception type out of cutover reaches the compensation."""
    exceptions: list[tuple[str, Exception]] = [
        ("ReleaseManagerError", ReleaseManagerError("boom")),
        ("raw OSError", OSError("ledger write failed")),
        ("raw JSONDecodeError", JSONDecodeError("bad ledger", "", 0)),
        ("raw KeyError", KeyError("new_rel")),
    ]
    for label, exc in exceptions:
        obs = _run_case(cutover_exc=exc, rollback_mode=_ROLLBACK_OK)
        unreg = cast("list[str]", obs["unregister_calls"])
        _check(
            obs["status"] == "failed"
            and obs["reason_code"] == "cutover_compensated"
            and obs["rollback_calls"] == [(COLOR_BLUE, "blue-id")]
            and obs["candidate_alive"] is False
            and len(unreg) == 1
            and unreg[0].startswith("solet-green-"),
            f"F1 {label}: compensation ran (rollback→blue + candidate killed "
            "+ unregistered + FAILED[cutover_compensated])",
        )
        _check(
            obs["submissions"] == [],
            f"F1 {label}: complete_swap NOT enqueued",
        )


def _f2_cases() -> None:
    """F2: rollback that does NOT take leaves the candidate ALIVE (no dead-color route)."""
    for label, mode in (("rollback RPC error", _ROLLBACK_RPC_ERROR),
                         ("rollback refused (drain expired)", _ROLLBACK_REFUSED)):
        obs = _run_case(cutover_exc=ReleaseManagerError("boom"), rollback_mode=mode)
        _check(
            obs["candidate_alive"],
            f"F2 {label}: candidate LEFT ALIVE (router may still route to it)",
        )
        _check(
            obs["unregister_calls"] == [],
            f"F2 {label}: candidate NOT unregistered",
        )
        _check(
            obs["status"] == "needs_intervention"
            and obs["reason_code"] == "cutover_router_rollback_failed"
            and "LEFT ALIVE" in obs["message"],
            f"F2 {label}: NEEDS_INTERVENTION + cutover_router_rollback_failed "
            "reason_code, does NOT claim 'restored'",
        )
        _check(
            obs["submissions"] == [],
            f"F2 {label}: complete_swap NOT enqueued",
        )


def run_smoke() -> int:
    print("=== cutover_failure_smoke (§4.7: F1 raw-exception + F2 rollback-fail) ===")
    _f1_cases()
    _f2_cases()
    print(f"\ncutover_failure_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
