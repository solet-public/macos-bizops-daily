#!/usr/bin/env python3
"""Durable-rollback verb smoke — ``SwapOrchestrator.rollback_release`` (no pytest).

Design ``2026-06-27_true_local_blue_green_materialized_artifacts_design.md``
§4.5 PATH A + the Architect status partition. Rollback IS a swap whose target
is the existing ``previous`` release: it reuses the shared ``SwapExecutor``
choreography verbatim, with the symlink op = ``ReleaseManager.rollback`` (not
``cutover``) and the brought-up release = the rehydrated ``previous`` (not a
freshly-built candidate).

Covers the full FAILED-vs-NEEDS_INTERVENTION partition:

- happy path → QUEUED (spawn previous as opposite color, activate, rollback()
  symlink swap, quiesce, enqueue complete_swap);
- stale expected_current_release (the concurrency CAS, ruled FIRST) →
  FAILED(stale_current_release), nothing spawned, ACTUAL current echoed for
  retry;
- no ``previous`` → FAILED(no_rollback_target), nothing spawned;
- activate refused → FAILED(activate_refused), candidate killed, current still
  active;
- rollback() failed but compensation CONFIRMED → FAILED(rollback_compensated);
- rollback target never registers (unbootable) → NEEDS_INTERVENTION(
  rollback_target_unbootable), candidate SIGKILLed + unregistered;
- rollback target not materializable (candidate_for raises) →
  NEEDS_INTERVENTION(rollback_target_unbootable), nothing spawned;
- rollback target's interpreter missing/non-exec (spawn OSError despite
  candidate_for passing dir+VERSION) → NEEDS_INTERVENTION(
  rollback_target_unbootable), nothing mutated;
- rollback target registers but never health-probes → NEEDS_INTERVENTION(
  rollback_target_unbootable), candidate SIGKILLed AND unregistered (no stale
  router binding);
- rollback() failed and compensation did NOT take → NEEDS_INTERVENTION(
  compensation_incomplete), candidate LEFT ALIVE;
- undo/redo → a second rollback_release toggles back to the original release.

Drives the REAL ``SwapOrchestrator.rollback_release`` with a recording stub
router + a fake ReleaseManager whose ``rollback`` is configurable; the
candidate is a real ``sleep`` child so kill-vs-alive is directly observable.
The TCP probe is stubbed; no real solet is launched.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/rollback_release_smoke.py
"""

from __future__ import annotations

import subprocess
import sys
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

# router.rollback (compensation) behaviour tokens.
_COMP_OK = "ok"
_COMP_REFUSED = "refused"
_COMP_RPC = "rpc"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _StubRouter:
    """Active=blue; activate + compensation-rollback behaviour configurable."""

    def __init__(self, *, activate_ok: bool = True, comp_mode: str = _COMP_OK) -> None:
        self._activate_ok = activate_ok
        self._comp_mode = comp_mode
        self.registered: dict[str, int] = {}
        self.activate_calls: list[str] = []
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
        self.activate_calls.append(instance_id)
        if not self._activate_ok:
            return {"activated": False, "reason": "injected activate refusal"}
        del color
        return {"activated": True, "previous_color": COLOR_BLUE, "drain_window_seconds": 30}

    def rollback(self, color: str, instance_id: str) -> dict[str, Any]:
        self.rollback_calls.append((color, instance_id))
        if self._comp_mode == _COMP_RPC:
            raise RouterClientError("rollback", "injected compensation RPC failure")
        if self._comp_mode == _COMP_REFUSED:
            return {"rolled_back": False, "reason": "drain_window_expired"}
        return {"rolled_back": True, "active_color": color}

    def unregister_color(self, instance_id: str) -> dict[str, Any]:
        self.unregister_calls.append(instance_id)
        return {"unregistered": True}


class _RollbackReleaseManager:
    """Fake whose ``rollback`` symlink op is configurable.

    A stateful ``current``/``previous`` pair so a 2nd ``rollback_release`` can
    prove undo/redo: ``rollback`` toggles them, ``previous_release`` reflects
    the toggle. ``candidate_for`` rehydrates either side (or raises when the
    target is not materializable).
    """

    def __init__(
        self, *, previous: str | None, symlink_mode: str = _COMP_OK,
        candidate_for_raises: bool = False,
    ) -> None:
        self._current = "rel-broken-current"
        self._previous = previous
        self._symlink_mode = symlink_mode
        self._candidate_for_raises = candidate_for_raises
        self.rollback_calls = 0
        self.candidate_for_calls: list[str] = []

    @property
    def current_release(self) -> str | None:
        return self._current

    @property
    def previous_release(self) -> str | None:
        return self._previous

    def candidate_for(self, release_id: str) -> CandidatePaths:
        self.candidate_for_calls.append(release_id)
        if self._candidate_for_raises:
            raise ReleaseManagerError(f"release dir gone/corrupt: {release_id}")
        base = Path("/nonexistent") / release_id
        return CandidatePaths(
            release_id=release_id, release_dir=base, code_root=base / "code",
            venv_python=base / "venv" / "bin" / "python3",
            version_file=base / "VERSION", missing_pth_targets=(), schema_snapshot=None,
        )

    def rollback(self) -> SwapResult:
        self.rollback_calls += 1
        if self._symlink_mode == "raise":
            raise ReleaseManagerError("injected rollback symlink failure")
        self._current, self._previous = self._previous, self._current
        return SwapResult(current=cast("str", self._current), previous=self._previous)

    # --- forward-cutover surface (unused by rollback_release) -------------
    def build_candidate(
        self, *, manifest_etag: str = "",
        manifest_plugins: tuple[str, ...] | None = None,
        schema_snapshot_fn: object = None,
    ) -> CandidatePaths:
        del manifest_etag, manifest_plugins, schema_snapshot_fn
        raise NotImplementedError("rollback smoke: build_candidate unused")

    def cutover(self, candidate: CandidatePaths) -> SwapResult:
        del candidate
        raise NotImplementedError("rollback smoke: cutover unused")

    def gc(self, *, keep: int | None = None) -> GcResult:
        del keep
        return GcResult(deleted=(), retained=())


class _RecordingActionFactory:
    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []

    def submit_action_definition(
        self, action_definition: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> str:
        del context
        self.submissions.append(action_definition)
        return "ae-rollback"


def _additive(
    candidate: CandidatePaths, *,
    current_snapshot: dict[str, object] | None,
    current_release_exists: bool,
) -> PreflightVerdict:
    del candidate, current_snapshot, current_release_exists
    return PreflightVerdict(is_additive=True, breaking_changes=())


def _spawn_sleep_child() -> subprocess.Popen[bytes]:
    return subprocess.Popen(["/bin/sleep", "30"])


def _run_case(
    *, previous: str | None, expected_current_release: str = "rel-broken-current",
    register_succeeds: bool = True,
    activate_ok: bool = True, symlink_mode: str = _COMP_OK,
    comp_mode: str = _COMP_OK, candidate_for_raises: bool = False,
    spawn_raises: bool = False, health_probe_ok: bool = True,
    reuse: tuple[_StubRouter, _RollbackReleaseManager] | None = None,
) -> dict[str, Any]:
    """Drive rollback_release once; return observations for assertions.

    ``expected_current_release`` defaults to the fake's live current
    (``rel-broken-current``) so the concurrency CAS passes; pass a different
    value to exercise the stale-current-release refusal.
    """
    if reuse is not None:
        stub, release_mgr = reuse
    else:
        stub = _StubRouter(activate_ok=activate_ok, comp_mode=comp_mode)
        release_mgr = _RollbackReleaseManager(
            previous=previous, symlink_mode=symlink_mode,
            candidate_for_raises=candidate_for_raises,
        )
    action_factory = _RecordingActionFactory()
    spawn_calls: list[str] = []
    children: list[subprocess.Popen[bytes]] = []

    def fake_spawn(
        app_home: Path, next_color: str, next_instance_id: str,
        solet_name: str, candidate: CandidatePaths,
    ) -> int:
        del app_home, solet_name, candidate
        if spawn_raises:
            # Models default_spawn's Popen raising on a missing/non-exec
            # <previous>/venv/bin/python3 (candidate_for passed dir+VERSION but
            # the executable layer is corrupt) — OSError, not a clean failure.
            raise FileNotFoundError("rollback target interpreter missing/non-exec")
        spawn_calls.append(next_instance_id)
        proc = _spawn_sleep_child()
        children.append(proc)
        if register_succeeds:
            stub.register_color(50066, next_color, next_instance_id)
        return proc.pid

    def _probe(*_a: object, **_k: object) -> bool:
        return health_probe_ok

    original_probe = green_candidate._probe_port_reachable  # noqa: SLF001
    green_candidate._probe_port_reachable = _probe  # type: ignore[assignment]  # noqa: SLF001
    try:
        orch = SwapOrchestrator(
            router_client=cast("RouterClient", stub),
            action_factory=action_factory,
            session_factory=lambda: "sess-rollback",
            solet_name="smoke",
            release_manager=release_mgr,
            schema_preflight=_additive,
            preflight_probe=_smoke_green_probe,
            set_color_active=lambda _active: None,
            spawn_fn=fake_spawn,
            ready_timeout_seconds=2,
            ready_poll_interval_seconds=0.01,
        )
        result = orch.rollback_release(
            reason="rollback-smoke",
            expected_etag="etag-x",
            expected_current_release=expected_current_release,
            app_home=Path("/nonexistent/profile"),
            self_instance_id="blue-id",
            self_color=COLOR_BLUE,
            set_active_targets=[],
        )
    finally:
        green_candidate._probe_port_reachable = original_probe  # noqa: SLF001
        # B2: the rollback drives the shared executor spine, which writes a
        # pending-finisher record under the runtime dir (solet "smoke", not
        # the live solet's real name). Clear the smoke-scoped record so nothing lingers in ~/.ananta.
        (Path.home() / ".ananta" / "runtime" / "smoke.pending_finisher.json").unlink(
            missing_ok=True,
        )

    candidate_alive = False
    for proc in children:
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            candidate_alive = True
            proc.kill()
            proc.wait()
    return {
        "status": result.status.value,
        "reason_code": result.reason_code,
        "message": result.message,
        "spawn_calls": spawn_calls,
        "rollback_symlink_calls": release_mgr.rollback_calls,
        "candidate_for_calls": list(release_mgr.candidate_for_calls),
        "router_rollback_calls": stub.rollback_calls,
        "unregister_calls": stub.unregister_calls,
        "submissions": action_factory.submissions,
        "candidate_alive": candidate_alive,
        "_stub": stub,
        "_rm": release_mgr,
    }


def _happy_path() -> None:
    obs = _run_case(previous="rel-prev")
    _check(
        obs["status"] == "queued" and obs["reason_code"] == "",
        f"happy: QUEUED, no reason_code (got {obs['status']}/{obs['reason_code']!r})",
    )
    _check(
        obs["candidate_for_calls"] == ["rel-prev"],
        f"happy: rehydrated the PREVIOUS release (got {obs['candidate_for_calls']})",
    )
    _check(
        len(obs["spawn_calls"]) == 1
        and obs["spawn_calls"][0].startswith("solet-green-"),
        f"happy: spawned previous as the OPPOSITE color green (got {obs['spawn_calls']})",
    )
    _check(
        obs["rollback_symlink_calls"] == 1,
        f"happy: ReleaseManager.rollback() symlink swap fired once "
        f"(got {obs['rollback_symlink_calls']})",
    )
    _check(
        len(obs["submissions"]) == 1
        and obs["submissions"][0].get("name") == "complete_swap",
        "happy: complete_swap enqueued for the reactivated poller",
    )
    _check(
        obs["router_rollback_calls"] == [],
        "happy: no router-rollback (compensation) on the success path",
    )


def _no_previous() -> None:
    # CAS passes (default expected matches the fake's current), THEN the
    # no-previous check fires → FAILED(no_rollback_target).
    obs = _run_case(previous=None)
    _check(
        obs["status"] == "failed" and obs["reason_code"] == "no_rollback_target",
        f"no-previous: FAILED(no_rollback_target) "
        f"(got {obs['status']}/{obs['reason_code']!r})",
    )
    _check(
        obs["spawn_calls"] == [] and obs["candidate_for_calls"] == [],
        "no-previous: nothing spawned, no rehydrate",
    )


def _cas_stale_current() -> None:
    # Concurrency CAS (Architect ruling c): the caller's asserted current !=
    # the live current (rel-broken-current) → FAILED BEFORE any spawn, system
    # unchanged, actual echoed for retry. CAS runs FIRST, so previous_release
    # is never even consulted.
    obs = _run_case(
        previous="rel-prev", expected_current_release="rel-someone-else-deployed",
    )
    _check(
        obs["status"] == "failed" and obs["reason_code"] == "stale_current_release",
        f"cas-stale: FAILED(stale_current_release) "
        f"(got {obs['status']}/{obs['reason_code']!r})",
    )
    _check(
        "rel-broken-current" in obs["message"]
        and "rel-someone-else-deployed" in obs["message"],
        f"cas-stale: echoes asserted + ACTUAL current for retry "
        f"(msg={obs['message'][:130]!r})",
    )
    _check(
        obs["spawn_calls"] == [] and obs["candidate_for_calls"] == [],
        "cas-stale: refused BEFORE any spawn/rehydrate (CAS is first)",
    )


def _activate_refused() -> None:
    obs = _run_case(previous="rel-prev", activate_ok=False)
    _check(
        obs["status"] == "failed" and obs["reason_code"] == "activate_refused",
        f"activate-refused: FAILED(activate_refused) "
        f"(got {obs['status']}/{obs['reason_code']!r})",
    )
    _check(
        obs["rollback_symlink_calls"] == 0,
        "activate-refused: no symlink rollback (current still authoritative)",
    )
    _check(
        not obs["candidate_alive"],
        "activate-refused: candidate SIGKILLed",
    )


def _rollback_fail_compensation_clean() -> None:
    obs = _run_case(previous="rel-prev", symlink_mode="raise", comp_mode=_COMP_OK)
    _check(
        obs["status"] == "failed" and obs["reason_code"] == "rollback_compensated",
        f"rollback-fail/comp-clean: FAILED(rollback_compensated) "
        f"(got {obs['status']}/{obs['reason_code']!r})",
    )
    _check(
        obs["router_rollback_calls"] == [(COLOR_BLUE, "blue-id")]
        and not obs["candidate_alive"],
        "rollback-fail/comp-clean: router rolled back to blue + candidate killed",
    )


def _target_unbootable_register_timeout() -> None:
    obs = _run_case(previous="rel-prev", register_succeeds=False)
    _check(
        obs["status"] == "needs_intervention"
        and obs["reason_code"] == "rollback_target_unbootable",
        f"unbootable(register): NEEDS_INTERVENTION(rollback_target_unbootable) "
        f"(got {obs['status']}/{obs['reason_code']!r})",
    )
    _check(
        len(obs["spawn_calls"]) == 1 and not obs["candidate_alive"],
        "unbootable(register): spawned then SIGKILLed (safety net void)",
    )


def _target_unbootable_candidate_for_raises() -> None:
    obs = _run_case(previous="rel-prev", candidate_for_raises=True)
    _check(
        obs["status"] == "needs_intervention"
        and obs["reason_code"] == "rollback_target_unbootable",
        f"unbootable(rehydrate): NEEDS_INTERVENTION(rollback_target_unbootable) "
        f"(got {obs['status']}/{obs['reason_code']!r})",
    )
    _check(
        obs["spawn_calls"] == [],
        "unbootable(rehydrate): nothing spawned — target not materializable",
    )


def _target_unbootable_spawn_raises() -> None:
    # #2 (Codex): the previous passes candidate_for (dir + VERSION exist) but its
    # interpreter is missing/non-exec, so default_spawn's Popen raises OSError.
    # The safety net is non-functional → NEEDS_INTERVENTION, NOT a retryable
    # FAILED(spawn_failed) (which would mislabel it + hide that the operator must
    # rebuild a good previous).
    obs = _run_case(previous="rel-prev", spawn_raises=True)
    _check(
        obs["status"] == "needs_intervention"
        and obs["reason_code"] == "rollback_target_unbootable",
        f"unbootable(spawn-OSError): NEEDS_INTERVENTION(rollback_target_unbootable) "
        f"(got {obs['status']}/{obs['reason_code']!r})",
    )
    _check(
        obs["candidate_for_calls"] == ["rel-prev"]
        and obs["spawn_calls"] == []
        and obs["rollback_symlink_calls"] == 0
        and obs["submissions"] == [],
        "unbootable(spawn-OSError): rehydrated but NO successful spawn / symlink / "
        f"complete_swap — system unmutated (got {obs['candidate_for_calls']})",
    )


def _target_unbootable_unhealthy() -> None:
    # #3 (Codex): the previous registers with the router but never passes the TCP
    # health probe → wait_until_registered times out. The candidate IS in the
    # registry, so it must be killed AND unregistered (no stale binding).
    obs = _run_case(
        previous="rel-prev", register_succeeds=True, health_probe_ok=False,
    )
    _check(
        obs["status"] == "needs_intervention"
        and obs["reason_code"] == "rollback_target_unbootable",
        f"unbootable(unhealthy): NEEDS_INTERVENTION(rollback_target_unbootable) "
        f"(got {obs['status']}/{obs['reason_code']!r})",
    )
    _check(
        len(obs["spawn_calls"]) == 1
        and not obs["candidate_alive"]
        and len(obs["unregister_calls"]) == 1
        and obs["unregister_calls"][0].startswith("solet-green-"),
        "unbootable(unhealthy): spawned + registered, then SIGKILLed AND "
        f"unregistered (no stale binding) (unreg={obs['unregister_calls']})",
    )


def _compensation_incomplete() -> None:
    for label, mode in (("rpc", _COMP_RPC), ("refused", _COMP_REFUSED)):
        obs = _run_case(previous="rel-prev", symlink_mode="raise", comp_mode=mode)
        _check(
            obs["status"] == "needs_intervention"
            and obs["reason_code"] == "compensation_incomplete"
            and "LEFT ALIVE" in obs["message"],
            f"comp-incomplete[{label}]: NEEDS_INTERVENTION(compensation_incomplete) "
            f"+ LEFT ALIVE (got {obs['status']}/{obs['reason_code']!r})",
        )
        _check(
            obs["candidate_alive"] and obs["unregister_calls"] == [],
            f"comp-incomplete[{label}]: candidate LEFT ALIVE, NOT unregistered",
        )


def _undo_redo() -> None:
    stub = _StubRouter()
    rm = _RollbackReleaseManager(previous="rel-prev")
    # 1st rollback: current is rel-broken-current (the CAS default).
    obs1 = _run_case(previous="rel-prev", reuse=(stub, rm))
    # The 1st rollback TOGGLED the pair, so current is now rel-prev — the caller
    # must re-read it and assert it for the redo (the CAS enforces exactly
    # this: you cannot redo against a stale current view).
    obs2 = _run_case(
        previous="rel-prev", expected_current_release="rel-prev", reuse=(stub, rm),
    )
    _check(
        obs1["status"] == "queued" and obs2["status"] == "queued",
        f"undo/redo: both rollbacks QUEUED (got {obs1['status']}/{obs2['status']})",
    )
    # candidate_for_calls accumulates on the shared release manager: after the
    # 1st rollback it rehydrated rel-prev; the 2nd toggled and rehydrated the
    # original (broken) release — undo/redo.
    _check(
        obs1["candidate_for_calls"] == ["rel-prev"]
        and obs2["candidate_for_calls"] == ["rel-prev", "rel-broken-current"],
        "undo/redo: 2nd rollback toggles back to the original (broken) release — "
        f"redo (got 1st={obs1['candidate_for_calls']} 2nd={obs2['candidate_for_calls']})",
    )


def run_smoke() -> int:
    print("=== rollback_release_smoke (§4.5 PATH A durable rollback) ===")
    _happy_path()
    _no_previous()
    _cas_stale_current()
    _activate_refused()
    _rollback_fail_compensation_clean()
    _target_unbootable_register_timeout()
    _target_unbootable_candidate_for_raises()
    _target_unbootable_spawn_raises()
    _target_unbootable_unhealthy()
    _compensation_incomplete()
    _undo_redo()
    print(f"\nrollback_release_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
