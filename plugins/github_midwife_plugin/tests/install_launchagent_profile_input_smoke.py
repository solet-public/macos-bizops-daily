"""Prove launchagent receives the selected profile and rejects its absence."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "github_midwife_plugin" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "solet_cli" / "src"))

from github_midwife_plugin import setup_adapter, setup_operations  # noqa: E402
from github_midwife_plugin.setup_adapter import _ALLOWED_PUBLIC_INPUTS  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome  # noqa: E402
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.operation_records import operation_request  # noqa: E402
from solet_manager.plan_builder import build_setup_plan  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_KB_ROOT = _REPO_ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
_CHECKS: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised when this focused launchagent contract smoke fails."""


class MissingProfileRuntime:
    """Prove the guard returns before reaching the subprocess boundary."""

    home = Path("/tmp/install-launchagent-profile-guard-home")

    def run(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("missing setup_profile must block before subprocess execution")


class LaunchagentRuntime:
    """Hermetic launchctl seam for LaunchAgent preview health tests."""

    def __init__(self, *, home: Path, outcome: CommandOutcome) -> None:
        self.home = home
        self.outcome = outcome
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandOutcome:
        del kwargs
        self.calls.append(argv)
        return self.outcome


def _check(condition: bool, label: str) -> None:
    _CHECKS.append(label)
    if not condition:
        raise SmokeFailureError(label)


def _action_ids(actions: object) -> list[object]:
    if not isinstance(actions, list):
        return []
    return [cast(dict[str, object], action).get("id") for action in actions if isinstance(action, dict)]


def _is_launchagent_repair(
    response: dict[str, object], runtime: LaunchagentRuntime, launchctl_command: tuple[str, ...]
) -> bool:
    return (
        response.get("checkpoint_status") == "pending"
        and _action_ids(response.get("planned_actions")) == ["genesis.install_launchagent"]
        and runtime.calls == [launchctl_command]
    )


def _is_verified_launchagent(
    response: dict[str, object], runtime: LaunchagentRuntime, launchctl_command: tuple[str, ...]
) -> bool:
    return (
        response.get("checkpoint_status") == "verified"
        and response.get("planned_actions") == []
        and runtime.calls == [launchctl_command]
    )


def _launchagent_request() -> AdapterRequest:
    revision = "a" * 40
    target = Path("/tmp/install-launchagent-profile-contract")
    bundle = ContractBundle.load(source_revision=revision, directory=_KB_ROOT)
    seed = SeedLock(
        repository="example/seed",
        release_tag="v1.0.0",
        commit=revision,
        tree_hash="b" * 40,
        archive_sha256="c" * 64,
        profile="macos-bizops",
    )
    plan = build_setup_plan(
        bundle=bundle,
        config=CreateConfig(name="launchagent-contract", target=target, autostart=True),
        seed=seed,
        journal_path=Path("/tmp/install-launchagent-profile-contract.json"),
        prospective_consents=True,
        operation_stage_ids={"models"},
    )
    operation = next(
        item
        for item in plan.operations
        if item.operation_ref == "genesis::autostart.install"
    )
    transaction = Transaction.create(
        name="launchagent-contract",
        target=target,
        input_fingerprint=canonical_sha256({"fixture": "launchagent-profile-contract"}),
        answers=plan.answers,
        seed=seed,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=tuple(bundle.stages),
        completion_probe_ids=(),
    )
    request = operation_request(
        transaction,
        bundle,
        operation,
        phase="probe",
        probe_purpose="preview",
        approval=None,
        attempt=1,
    )
    return AdapterRequest.from_dict(cast(dict[str, object], request.to_dict()))


def _check_profile_projection_and_guard() -> None:
    request = _launchagent_request()
    _check(
        request.public_inputs == {"setup_profile": "macos-bizops", "autostart": "enabled"},
        "launchagent request projects selected setup_profile and autostart",
    )
    adapter_response = setup_adapter.dispatch_request(request, MissingProfileRuntime())
    adapter_actions = adapter_response.get("planned_actions")
    _check(
        adapter_response.get("checkpoint_status") == "pending"
        and isinstance(adapter_actions, list)
        and any(action.get("id") == "genesis.install_launchagent" for action in adapter_actions),
        "flow-declared launchagent inputs pass adapter validation "
        f"[{adapter_response}]",
    )
    missing_preview_payload = dict(cast(dict[str, object], request_to_dict(request)))
    missing_preview_payload["public_inputs"] = {"autostart": "enabled"}
    preview_response = setup_adapter.dispatch_request(
        AdapterRequest.from_dict(missing_preview_payload),
        MissingProfileRuntime(),
    )
    preview_actions = preview_response.get("planned_actions")
    _check(
        preview_response.get("checkpoint_status") == "blocked"
        and preview_response.get("error_kind") == "adapter_protocol_error"
        and preview_actions == []
        and preview_response.get("repair")
        == "Use only the exact flow-declared public inputs for this callable.",
        "preview without setup_profile blocks before subprocess execution",
    )
    missing_payload = dict(cast(dict[str, object], request_to_dict(request)))
    missing_payload.update(
        {
            "phase": "apply",
            "probe_purpose": None,
            "approval_fingerprint": "sha256:" + "c" * 64,
            "dry_run": False,
            "target": str(Path(sys.executable).parents[2]),
            "public_inputs": {"autostart": "enabled"},
        }
    )
    response = setup_adapter.dispatch_request(
        AdapterRequest.from_dict(missing_payload),
        MissingProfileRuntime(),
    )
    _check(
        response.get("checkpoint_status") == "blocked"
        and response.get("error_kind") == "adapter_protocol_error"
        and response.get("repair")
        == "Use only the exact flow-declared public inputs for this callable.",
        "apply without setup_profile blocks before subprocess execution",
    )


def _check_preview_requires_launchd_health() -> None:
    request = _launchagent_request()
    launchctl_command = (
        "/bin/launchctl",
        "print",
        f"gui/{os.getuid()}/local.solet.{request.name}",
    )
    broken_runtime = LaunchagentRuntime(
        home=Path("/tmp/install-launchagent-preview-broken-home"),
        outcome=CommandOutcome(113, False, 0, "", "Could not find service"),
    )
    with patch.object(setup_operations, "genesis_artifacts_valid", return_value=True):
        broken_response = setup_adapter.dispatch_request(request, broken_runtime)
    _check(
        _is_launchagent_repair(broken_response, broken_runtime, launchctl_command),
        "broken launchd service remains an actionable LaunchAgent repair preview",
    )
    healthy_outcome = CommandOutcome(
        0,
        False,
        0,
        "state = running\nlast exit code = 0\nrun count = 1\n",
        "",
    )
    with TemporaryDirectory(prefix="install-launchagent-preview-") as temporary_home:
        home = Path(temporary_home)
        missing_plist_runtime = LaunchagentRuntime(home=home, outcome=healthy_outcome)
        with patch.object(setup_operations, "genesis_artifacts_valid", return_value=True):
            missing_plist_response = setup_adapter.dispatch_request(request, missing_plist_runtime)
        _check(
            _is_launchagent_repair(
                missing_plist_response, missing_plist_runtime, launchctl_command
            ),
            "running launchd service without a persistent plist remains an actionable repair preview",
        )
        plist = home / "Library" / "LaunchAgents" / f"local.solet.{request.name}.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("<plist />", encoding="utf-8")
        healthy_runtime = LaunchagentRuntime(home=home, outcome=healthy_outcome)
        with patch.object(setup_operations, "genesis_artifacts_valid", return_value=True):
            healthy_response = setup_adapter.dispatch_request(request, healthy_runtime)
        _check(
            _is_verified_launchagent(healthy_response, healthy_runtime, launchctl_command),
            "healthy launchd service with a persistent plist verifies without a redundant repair",
        )


def _check_flow_parameter_allowlist_drift() -> None:
    """Keep adapter acceptance aligned with every flow-declared parameter name."""

    flow = json.loads((_KB_ROOT / "macos_setup_flow.json").read_text(encoding="utf-8"))
    operations = flow["operations"]
    declared_by_reference: dict[str, set[str]] = {}
    for operation in operations.values():
        reference = operation["operation_ref"]
        declared_by_reference.setdefault(reference, set()).update(operation.get("parameters", {}))
    for reference, declared in declared_by_reference.items():
        allowed = _ALLOWED_PUBLIC_INPUTS.get(reference, frozenset())
        _check(
            declared <= allowed,
            "adapter allowlist covers every flow-declared parameter "
            f"[{reference}: declared={sorted(declared)}, allowed={sorted(allowed)}]",
        )


def request_to_dict(request: AdapterRequest) -> dict[str, object]:
    """Build the closed wire request without reaching a production adapter."""

    return {
        "protocol_version": 1,
        "kind": "operation_request",
        "request_id": request.request_id,
        "operation_id": request.operation_id,
        "operation_ref": request.operation_ref,
        "phase": request.phase,
        "probe_purpose": request.probe_purpose,
        "attempt": request.attempt,
        "name": request.name,
        "target": str(request.target),
        "flow_id": "macos.repository_setup",
        "flow_source_revision": request.flow_source_revision,
        "answers_fingerprint": request.answers_fingerprint,
        "approval_fingerprint": request.approval_fingerprint,
        "dry_run": request.dry_run,
        "timeout_seconds": request.timeout_seconds,
        "public_inputs": request.public_inputs,
    }


def main() -> int:
    try:
        _check_profile_projection_and_guard()
        _check_preview_requires_launchd_health()
        _check_flow_parameter_allowlist_drift()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"install_launchagent_profile_input_smoke OK: {len(_CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
