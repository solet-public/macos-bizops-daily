"""Target-local construction for the manager reconciliation cutover entry.

The setup adapter runs from the refreshed target tree, but it is not a
platform process and must never manufacture one.  This module constructs the
same :class:`SwapOrchestrator` used by the service entry from target-local
facts, then binds it to the controller's narrow observe/execute contracts.
The running, router-served process is observed through its existing T1
attestation verb; it is never asked to expose a new reconciliation verb.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ananta.core.root_manifest.classifier import load_manifest
from ananta.core.runtime import get_runtime_dir

from macos_self_deployment_plugin.constants import (
    DEFAULT_PREFLIGHT_PROBE_TIMEOUT_SECONDS,
    ENV_SOLET_NAME,
    ROUTER_SOCKET_SUFFIX,
)
from macos_self_deployment_plugin.cutover_journal_binding import ReceiptJournalStore
from macos_self_deployment_plugin.plugin import (
    classify_cutover_schema_preflight,
    run_cutover_preflight_probe,
)
from macos_self_deployment_plugin.reconciliation_cutover import (
    CutoverProvenance,
    ReconciliationCutoverController,
    ReconciliationCutoverRequest,
    SwapEvidence,
    observation_from_attestation,
    swap_evidence_from_restart_result,
)
from macos_self_deployment_plugin.release_manager import CandidatePaths, ReleaseManager
from macos_self_deployment_plugin.router_client import RouterClient
from macos_self_deployment_plugin.swap_executor import (
    TargetLocalUnavailableActionFactory,
    target_local_unavailable_session,
)
from macos_self_deployment_plugin.swap_orchestrator import SwapOrchestrator

__all__ = [
    "TargetLocalConstructionError",
    "build_target_local_controller",
    "run_target_local_cutover",
]

_ATTEST_PROCESS_KEY: Final[str] = (
    "service_interface::local_self_deployment_service::attest_runtime_code"
)
_ATTEST_TIMEOUT_SECONDS: Final[float] = 30.0


class TargetLocalConstructionError(RuntimeError):
    """The refreshed target lacks a fact required for a safe cutover."""


@dataclass(frozen=True, slots=True)
class _TargetLocalDependencies:
    """Concrete target-local collaborators, isolated as the smoke seam."""

    target: Path
    solet_name: str
    app_home: Path
    runtime_dir: Path
    router_client: RouterClient
    release_manager: ReleaseManager
    attest: Callable[[], dict[str, object]]
    logger: logging.Logger


def run_target_local_cutover(request: object) -> dict[str, object]:
    """Translate the adapter's closed request into the controller's contract.

    The adapter validates installation identity before it calls here.  This
    boundary validates the separate controller request shape before a journal
    intent or a shared swap is reachable.
    """
    from github_midwife_plugin.target_reconciliation import (  # noqa: PLC0415
        TargetReconciliationRequest,
    )

    if not isinstance(request, TargetReconciliationRequest):
        raise TypeError("target-local cutover requires TargetReconciliationRequest")
    target = request_target_root()
    dependencies = _build_target_local_dependencies(target)
    if request.name != dependencies.solet_name:
        raise TargetLocalConstructionError(
            "request name does not match the refreshed target root manifest: "
            f"request={request.name!r}, target={dependencies.solet_name!r}",
        )
    controller = _controller_from_dependencies(dependencies)
    outcome = controller.cutover(
        ReconciliationCutoverRequest(
            reconciliation_id=request.reconciliation_id,
            expected_source_surface_sha256=request.expected_source_surface_sha256,
            expected_release_surface_sha256=request.expected_release_surface_sha256,
            expected_manifest_etag=request.expected_manifest_etag,
            expected_current_release_id=request.expected_current_release_id,
            expected_active_instance_id=request.expected_active_instance_id,
            expected_active_start_token=request.expected_active_start_token,
        ),
        recover=request.phase == "recover",
    )
    return outcome.to_dict()


def request_target_root() -> Path:
    """Derive the target from the refreshed adapter's own installed module."""
    from github_midwife_plugin.target_reconciliation import installed_target_root  # noqa: PLC0415

    return installed_target_root()


def build_target_local_controller(*, target: Path) -> ReconciliationCutoverController:
    """Build the one shared swap state machine from refreshed target bytes."""
    return _controller_from_dependencies(_build_target_local_dependencies(target))


def _controller_from_dependencies(
    dependencies: _TargetLocalDependencies,
) -> ReconciliationCutoverController:
    def preflight_probe(*, candidate: CandidatePaths, app_home: Path):
        return run_cutover_preflight_probe(
            candidate=candidate,
            app_home=app_home,
            solet_name=dependencies.solet_name,
            runtime_dir=dependencies.runtime_dir,
            timeout_seconds=DEFAULT_PREFLIGHT_PROBE_TIMEOUT_SECONDS,
            logger=dependencies.logger,
        )

    def schema_preflight(
        candidate: CandidatePaths,
        *,
        current_snapshot: dict[str, object] | None,
        current_release_exists: bool,
    ):
        return classify_cutover_schema_preflight(
            candidate,
            current_snapshot=current_snapshot,
            current_release_exists=current_release_exists,
            logger=dependencies.logger,
        )

    orchestrator = SwapOrchestrator(
        router_client=dependencies.router_client,
        action_factory=TargetLocalUnavailableActionFactory(),
        session_factory=target_local_unavailable_session,
        solet_name=dependencies.solet_name,
        release_manager=dependencies.release_manager,
        schema_preflight=schema_preflight,
        preflight_probe=preflight_probe,
        # The adapter is not the router-served old process.  Its poller is
        # unreachable by construction, so it has nothing it may quiesce.
        set_color_active=lambda _active: None,
        logger=dependencies.logger,
        runtime_dir=dependencies.runtime_dir,
    )

    def observe():
        return observation_from_attestation(dependencies.attest())

    def observe_journal(_journal):
        from solet_manager.cutover_receipts import CutoverRuntimeObservation  # noqa: PLC0415

        payload = dependencies.attest()
        observation = observation_from_attestation(payload)
        release_id = _required_text(payload, "release_id")
        served_by_self = payload.get("served_by_self")
        if served_by_self is not True:
            raise TargetLocalConstructionError(
                "router attestation did not come from the active served process",
            )
        return CutoverRuntimeObservation(
            reachable=True,
            release_id=release_id,
            active_instance_id=observation.active_instance_id,
            source_surface_sha256=observation.source_surface_sha256,
            release_surface_sha256=observation.release_surface_sha256,
        )

    journal = ReceiptJournalStore(dependencies.target, observe_runtime=observe_journal)

    def execute_swap(
        *,
        reason: str,
        expected_etag: str,
        dry_run: bool,
        provenance: CutoverProvenance,
        prior_pid: int,
        prior_instance_id: str,
        prior_color: str,
        prior_start_token: str,
    ) -> SwapEvidence:
        result = orchestrator.restart(
            reason=reason,
            expected_etag=expected_etag,
            dry_run=dry_run,
            app_home=dependencies.app_home,
            self_instance_id=prior_instance_id,
            self_color=prior_color,
            prior_pid=prior_pid,
            prior_start_token=prior_start_token,
            poller_gate="unreachable_from_target_local",
            set_active_targets=(),
            reconciliation_provenance=provenance.to_dict(),
        )
        return swap_evidence_from_restart_result(result)

    return ReconciliationCutoverController(
        observe=observe,
        execute_swap=execute_swap,
        journal=journal,
    )


def _build_target_local_dependencies(target: Path) -> _TargetLocalDependencies:
    """Resolve every operational input from the refreshed target itself."""
    resolved_target = target.resolve(strict=True)
    if not resolved_target.is_dir():
        raise TargetLocalConstructionError(
            f"target-local cutover target is not a directory: {resolved_target}",
        )
    manifest, manifest_error = load_manifest(resolved_target / "root_manifest.yaml")
    if manifest is None:
        raise TargetLocalConstructionError(
            f"cannot resolve target solet_name: {manifest_error}",
        )
    solet_name = manifest.solet_name
    if not solet_name:
        raise TargetLocalConstructionError("target root manifest has an empty solet_name")
    runtime_dir = get_runtime_dir(solet_name)
    logger = logging.getLogger("macos_self_deployment_plugin.target_local_cutover")
    return _TargetLocalDependencies(
        target=resolved_target,
        solet_name=solet_name,
        app_home=resolved_target / "profile",
        runtime_dir=runtime_dir,
        router_client=RouterClient(runtime_dir / f"{solet_name}{ROUTER_SOCKET_SUFFIX}"),
        release_manager=ReleaseManager(
            solet_name=solet_name,
            source_root=resolved_target,
            logger=logger,
        ),
        attest=lambda: _attest_runtime(resolved_target, solet_name),
        logger=logger,
    )


def _attest_runtime(target: Path, solet_name: str) -> dict[str, object]:
    """Read the active process through the target's own bridge executable."""
    return _attestation_data(_run_target_attestation(target, solet_name))


def _run_target_attestation(target: Path, solet_name: str) -> subprocess.CompletedProcess[str]:
    bridge = _target_bridge(target)
    environment = dict(os.environ)
    environment[ENV_SOLET_NAME] = solet_name
    payload = json.dumps(
        {"reconciliation_id": "", "verification_modules": []},
        separators=(",", ":"),
    )
    try:
        completed = subprocess.run(
            [str(bridge), "call", _ATTEST_PROCESS_KEY, payload],
            cwd=target,
            env=environment,
            capture_output=True,
            text=True,
            timeout=_ATTEST_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError as exc:
        raise TargetLocalConstructionError(f"target-local attestation could not start: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TargetLocalConstructionError(
            f"target-local attestation exceeded {_ATTEST_TIMEOUT_SECONDS}s",
        ) from exc
    if completed.returncode != 0:
        raise TargetLocalConstructionError(
            "target-local attestation bridge exited non-zero: "
            f"code={completed.returncode}, stderr={completed.stderr[-512:]!r}",
        )
    return completed


def _target_bridge(target: Path) -> Path:
    bridge = target / ".venv" / "bin" / "solet-bridge"
    if not bridge.is_file() or not os.access(bridge, os.X_OK):
        raise TargetLocalConstructionError(
            f"target-local bridge is unavailable: {bridge}",
        )
    return bridge


def _attestation_data(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """Decode the bridge's success envelope without treating errors as data."""
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise TargetLocalConstructionError("target-local attestation returned invalid JSON") from exc
    if not isinstance(raw, dict) or raw.get("status") != "success":
        raise TargetLocalConstructionError(
            f"target-local attestation was not successful: {completed.stdout[-512:]!r}",
        )
    result = raw.get("result")
    if not isinstance(result, dict):
        raise TargetLocalConstructionError("target-local attestation has no result object")
    data = result.get("data")
    if not isinstance(data, dict):
        raise TargetLocalConstructionError("target-local attestation has no data object")
    return data


def _required_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise TargetLocalConstructionError(
            f"target-local attestation field {key!r} must be a non-empty string",
        )
    return value
