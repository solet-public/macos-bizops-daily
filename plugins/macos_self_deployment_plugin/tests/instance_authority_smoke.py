#!/usr/bin/env python3
"""C3 instance-authority smoke (no pytest).

Design ``2026-06-27_true_local_blue_green_materialized_artifacts_design.md``,
Phase-2 review finding C3 (Codex): ``_derive_next_color`` previously read
``active_instance_id`` only for type-validation, then accepted ANY process
whose ``active_color`` matched — so a STALE same-color process (not the
instance the router currently routes to) could drive a swap and enqueue a
``complete_swap`` for its own stale id. The fix threads ``self_instance_id``
through and requires ``active_instance_id == self_instance_id`` AND
``active_color == self_color``.

This injects the EXACT failure mode: the router reports the SAME color but
a DIFFERENT active instance id than the caller. ``restart`` must refuse
BEFORE building/spawning/cutting-over/enqueuing anything. A matching
control case confirms the authoritative instance still proceeds to build.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/instance_authority_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_PLUGIN_SRC), str(_REPO_ROOT / "ananta" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from macos_self_deployment_plugin.constants import COLOR_BLUE  # noqa: E402
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


class _StatusRouter:
    """status() reports a configurable active instance id (same color)."""

    def __init__(self, active_instance_id: str) -> None:
        self._active_instance_id = active_instance_id

    def status(self) -> dict[str, Any]:
        return {
            "active_color": COLOR_BLUE,
            "active_instance_id": self._active_instance_id,
            "colors": [],
        }

    def unregister_color(self, instance_id: str) -> dict[str, Any]:
        # The authoritative-instance case reaches the register-timeout path,
        # which now SIGKILLs AND unregisters the never-registered candidate
        # (best-effort, no stale binding).
        del instance_id
        return {"unregistered": True}


class _RecordingReleaseManager:
    """Records build/cutover — both MUST stay at 0 when the swap is refused pre-build."""

    def __init__(self) -> None:
        self.build_count = 0
        self.cutover_count = 0

    def gc(self, *, keep: int | None = None) -> GcResult:
        del keep
        return GcResult(deleted=(), retained=())

    def build_candidate(
        self, *, manifest_etag: str = "",
        manifest_plugins: tuple[str, ...] | None = None,
        schema_snapshot_fn: object = None,
    ) -> CandidatePaths:
        del manifest_etag, manifest_plugins, schema_snapshot_fn
        self.build_count += 1
        base = Path("/nonexistent/rel-x")
        return CandidatePaths(
            release_id="rel-x", release_dir=base, code_root=base / "code",
            venv_python=base / "venv" / "bin" / "python3", version_file=base / "VERSION",
            missing_pth_targets=(), schema_snapshot=None,
        )

    def cutover(self, candidate: CandidatePaths) -> SwapResult:
        del candidate
        self.cutover_count += 1
        return SwapResult(current="rel-x", previous=None)

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
        return "ae-x"


def _additive(
    candidate: CandidatePaths, *,
    current_snapshot: dict[str, object] | None,
    current_release_exists: bool,
) -> PreflightVerdict:
    del candidate, current_snapshot, current_release_exists
    return PreflightVerdict(is_additive=True, breaking_changes=())


def _run(active_instance_id: str) -> dict[str, Any]:
    """restart() with the router reporting ``active_instance_id`` (self is 'blue-id')."""
    release_mgr = _RecordingReleaseManager()
    action_factory = _RecordingActionFactory()
    spawn_calls: list[str] = []

    def fake_spawn(
        app_home: Path, next_color: str, next_instance_id: str,
        solet_name: str, candidate: CandidatePaths,
    ) -> int:
        del app_home, next_color, solet_name, candidate
        spawn_calls.append(next_instance_id)
        return 4242

    orch = SwapOrchestrator(
        router_client=cast("RouterClient", _StatusRouter(active_instance_id)),
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
        reason="instance-authority-smoke",
        expected_etag="etag-x",
        dry_run=False,
        app_home=Path("/nonexistent/profile"),
        self_instance_id="blue-id",
        self_color=COLOR_BLUE,
        set_active_targets=[],
    )
    return {
        "status": result.status.value,
        "build_count": release_mgr.build_count,
        "cutover_count": release_mgr.cutover_count,
        "spawn_calls": spawn_calls,
        "submissions": action_factory.submissions,
    }


def run_smoke() -> int:
    print("=== instance_authority_smoke (C3: stale same-color instance cannot swap) ===")

    # MISMATCH: router-active instance differs from self → refuse pre-build.
    mismatch = _run("a-different-active-id")
    _check(mismatch["status"] == "failed", f"mismatch → FAILED ({mismatch['status']})")
    _check(mismatch["build_count"] == 0, "mismatch → NO build_candidate")
    _check(mismatch["spawn_calls"] == [], "mismatch → NO spawn")
    _check(mismatch["cutover_count"] == 0, "mismatch → NO cutover")
    _check(mismatch["submissions"] == [], "mismatch → NO complete_swap enqueued")

    # CONTROL: the authoritative instance (id matches) proceeds to build.
    match = _run("blue-id")
    _check(
        match["build_count"] == 1,
        f"authoritative instance proceeds to build: {match['build_count']}",
    )

    print(f"\ninstance_authority_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
