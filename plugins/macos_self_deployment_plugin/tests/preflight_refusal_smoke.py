#!/usr/bin/env python3
"""§3 / §8.7 preflight-REFUSAL smoke (no pytest).

Design ``2026-06-27_true_local_blue_green_materialized_artifacts_design.md``
§3 / §8.7 — the literal criterion is "a deploy with a non-additive
SchemaDefinition diff is **refused**/flagged." ``schema_preflight_smoke``
proves the *classifier* (the "flagged" verdict); this proves the
*orchestrator gate* (the "refused" deploy): when the §3 preflight returns
a non-additive verdict, ``SwapOrchestrator.restart`` must return
``FAILED`` **before any spawn**, and must NOT cut over.

This matters precisely because the production preflight ships in DEFER
mode this cycle (no snapshot producer yet → always additive), so the
refusal branch is dead-until-producer. Exercising it with an injected
non-additive preflight now guarantees the gate glue is wired before a
future agent lands the producer.

The preflight runs after ``build_candidate`` but before spawn, so only
``router.status()`` is reached on the router — a minimal recording stub
suffices (no activate/register/rollback).

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/preflight_refusal_smoke.py
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
from macos_self_deployment_plugin.schema_preflight import (  # noqa: E402
    PreflightVerdict,
    SchemaChange,
)
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


class _StatusOnlyRouter:
    """Only ``status()`` is reached before the preflight refuses."""

    def status(self) -> dict[str, Any]:
        return {
            "active_color": COLOR_BLUE,
            "active_instance_id": "blue-id",
            "colors": [],
        }


class _RecordingReleaseManager:
    """build_candidate records; cutover MUST NOT be called on the refusal path."""

    def __init__(self) -> None:
        self.build_count = 0
        self.cutover_count = 0
        self.gc_count = 0

    def gc(self, *, keep: int | None = None) -> GcResult:
        del keep
        self.gc_count += 1
        return GcResult(deleted=(), retained=())

    def build_candidate(
        self, *, manifest_etag: str = "",
        manifest_plugins: tuple[str, ...] | None = None,
        schema_snapshot_fn: object = None,
    ) -> CandidatePaths:
        del manifest_etag, manifest_plugins, schema_snapshot_fn
        self.build_count += 1
        base = Path("/nonexistent/rel-refused")
        return CandidatePaths(
            release_id="rel-refused",
            release_dir=base,
            code_root=base / "code",
            venv_python=base / "venv" / "bin" / "python3",
            version_file=base / "VERSION",
            missing_pth_targets=(),
            schema_snapshot=None,
        )

    def cutover(self, candidate: CandidatePaths) -> SwapResult:
        del candidate
        self.cutover_count += 1
        return SwapResult(current="rel-refused", previous=None)

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


def _non_additive_preflight(
    candidate: CandidatePaths, *,
    current_snapshot: dict[str, object] | None,
    current_release_exists: bool,
) -> PreflightVerdict:
    """Inject a non-additive verdict (a dropped column) for ``candidate``."""
    del candidate, current_snapshot, current_release_exists
    return PreflightVerdict(
        is_additive=False,
        breaking_changes=(
            SchemaChange(
                kind="column_removed", namespace="plugin", table="thing",
                column="gone", detail="column dropped",
            ),
        ),
    )


def run_smoke() -> int:
    print("=== preflight_refusal_smoke (§3/§8.7: non-additive schema → deploy REFUSED) ===")
    release_mgr = _RecordingReleaseManager()
    spawn_calls: list[str] = []

    def fake_spawn(
        app_home: Path, next_color: str, next_instance_id: str,
        solet_name: str, candidate: CandidatePaths,
    ) -> int:
        del app_home, next_color, solet_name, candidate
        spawn_calls.append(next_instance_id)
        return 4242

    def fake_action_factory_submit(
        action_definition: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> str:
        del action_definition, context
        return "ae-should-not-happen"

    action_factory = cast("Any", type("_AF", (), {"submit_action_definition": staticmethod(fake_action_factory_submit)})())

    orch = SwapOrchestrator(
        router_client=cast("RouterClient", _StatusOnlyRouter()),
        action_factory=action_factory,
        session_factory=lambda: "sess-refusal",
        solet_name="smoke",
        release_manager=release_mgr,
        schema_preflight=_non_additive_preflight,
        preflight_probe=_smoke_green_probe,
        set_color_active=lambda _active: None,
        spawn_fn=fake_spawn,
        ready_timeout_seconds=2,
        ready_poll_interval_seconds=0.01,
    )
    result = orch.restart(
        reason="refusal-smoke",
        expected_etag="etag-x",
        dry_run=False,
        app_home=Path("/nonexistent/profile"),
        self_instance_id="blue-id",
        self_color=COLOR_BLUE,
        set_active_targets=[],
    )

    _check(result.status.value == "failed", f"restart returned FAILED ({result.status})")
    _check(
        "schema preflight refused deploy" in result.message,
        f"failure message names the schema-preflight refusal: {result.message[:120]!r}",
    )
    _check(
        release_mgr.build_count == 1,
        f"candidate WAS built before the gate ran: build_count={release_mgr.build_count}",
    )
    _check(
        spawn_calls == [],
        f"NO spawn happened — refused before spawning green: {spawn_calls}",
    )
    _check(
        release_mgr.cutover_count == 0,
        f"NO cutover happened on the refusal path: cutover_count={release_mgr.cutover_count}",
    )
    _check(
        release_mgr.gc_count >= 1,
        f"GC ran even on the REFUSAL path (rejected candidates don't leak): {release_mgr.gc_count}",
    )

    print(f"\npreflight_refusal_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
