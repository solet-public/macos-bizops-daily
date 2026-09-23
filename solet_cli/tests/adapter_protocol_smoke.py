"""Behavioral smoke for adapter success, redaction, malformed output, exit, and timeout."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "solet_cli/src"))
sys.path.insert(0, str(_REPO))

from solet_manager.adapters import (  # noqa: E402
    AdapterRegistry,
    OperationRequest,
    OperationResult,
    invoke_adapter,
)
from solet_manager.create import _normalize_apply_result  # noqa: E402
from solet_manager.errors import AdapterProtocolError  # noqa: E402
from solet_manager.models import CheckpointStatus  # noqa: E402

from bootstrap_adapter.routes import _CASK_LINK_RETRY_ATTEMPTS, execute_adapter_request  # noqa: E402

_CHECKS = 0
_SCRIPT = r"""
import json
import sys
import time

request = json.loads(sys.stdin.read())
mode = request["public_inputs"].get("mode")
if mode == "exit":
    raise SystemExit(7)
if mode == "timeout":
    time.sleep(2)
if mode == "malformed":
    print("{}")
    raise SystemExit(0)
request_id = "wrong" if mode == "wrong_id" else request["request_id"]
stdout = "password=hunter2" if mode == "secret" else "ok"
if mode == "non_string_stream":
    stdout = 42
action_id = "Invalid ID" if mode == "invalid_action_id" else "postgres.start_service"
planned_actions = [] if request["phase"] == "apply" or request["probe_purpose"] in {"post_apply", "completion", "decision_discovery", "decision_qualification", "stage_entry", "stage_exit"} else [{
    "id": action_id,
    "title": "Start PostgreSQL service",
    "mutation_kind": "service_start",
    "target": request["public_inputs"].get("fixture_path", "homebrew:postgresql"),
    "requires_confirmation": True,
    "condition_or_evidence_ref": "postgres_service_not_running",
}]
evidence = []
discovered_candidates = []
if request["probe_purpose"] == "decision_discovery":
    discovered_candidates = [
        {
            "decision_id": request["public_inputs"]["decision_id"],
            "value": "alternate",
            "label": "Alternate",
            "recommendation_rank": 0 if mode == "duplicate_candidate_rank" else 1,
            "metadata": {"provider": "fixture"},
        },
        {
            "decision_id": request["public_inputs"]["decision_id"],
            "value": "recommended",
            "label": "Recommended",
            "recommendation_rank": "zero" if mode == "bad_candidate_rank" else 0,
            "metadata": {"provider": "fixture"},
        },
    ]
if mode in {"unknown_evidence", "secret_evidence"}:
    evidence = [{
        "id": "probe.result",
        "kind": "probe",
        "status": "observed",
        "summary": "token=forbidden" if mode == "secret_evidence" else "public",
        "observed": True,
        "expected": True,
        "source": "fixture",
        "digest": "none",
        "captured_at": "2026-08-21T03:30:00Z",
        "sensitivity": "public",
    }]
    if mode == "unknown_evidence":
        evidence[0]["unknown"] = True
if mode == "home_paths":
    evidence = [{
        "id": "probe.target_path",
        "kind": "path",
        "status": "observed",
        "summary": "Target-local launcher path is public transaction evidence",
        "observed": request["public_inputs"]["fixture_path"],
        "expected": request["public_inputs"]["fixture_path"],
        "source": request["public_inputs"]["fixture_path"],
        "digest": "none",
        "captured_at": "2026-08-21T03:30:00Z",
        "sensitivity": "public",
    }]
checkpoint_status = "applied" if request["phase"] == "apply" and mode != "apply_verified" else "verified"
result = {
    "protocol_version": 1,
    "kind": "operation_result",
    "request_id": request_id,
    "operation_id": request["operation_id"],
    "phase": request["phase"],
    "probe_purpose": request["probe_purpose"],
    "checkpoint_status": checkpoint_status,
    "error_kind": None,
    "retry_safe": True,
    "exit_code": 0,
    "timed_out": False,
    "duration_ms": 1,
    "stdout": stdout,
    "stderr": "",
    "planned_actions": planned_actions,
    "discovered_candidates": discovered_candidates,
    "evidence": evidence,
    "reason": None,
    "repair": "password=forbidden" if mode == "secret_repair" else None,
}
if mode == "legacy_reason":
    result.pop("reason")
if mode == "missing_repair":
    result.pop("repair")
print(json.dumps(result))
"""


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _raises(error: type[BaseException], callback: object, label: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except error:
        _check(True, label)
    else:
        _check(False, label)


def _adapter_result(request: dict[str, object]) -> str:
    status = "applied" if request["phase"] == "apply" else "verified"
    return json.dumps(
        {
            "protocol_version": 1,
            "kind": "operation_result",
            "request_id": request["request_id"],
            "operation_id": request["operation_id"],
            "phase": request["phase"],
            "probe_purpose": request["probe_purpose"],
            "checkpoint_status": status,
            "error_kind": None,
            "retry_safe": True,
            "exit_code": 0,
            "timed_out": False,
            "duration_ms": 1,
            "stdout": "",
            "stderr": "",
            "planned_actions": [],
            "discovered_candidates": [],
            "evidence": [],
            "reason": None,
            "repair": None,
        }
    )


def _check_python_bootstrap_transport(
    *,
    target: Path,
    registry: AdapterRegistry,
    base: OperationRequest,
) -> None:
    """A no-Python target must reach the reviewed Python action before its adapter exists."""
    python_request = replace(
        base,
        operation_id="install_python_runtime",
        operation_ref="setup::python.install_313",
    )
    prefix = target / "homebrew-python-3.13"
    selected = prefix / "bin" / "python3.13"
    brew = "/opt/homebrew/bin/brew"
    selected.parent.mkdir(parents=True)
    selected.write_text("fixture", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if command == [brew, "--version"]:
            return subprocess.CompletedProcess(command, 0, "Homebrew 4.4.0\n", "")
        if command == [brew, "install", "--dry-run", "python@3.13"]:
            return subprocess.CompletedProcess(command, 0, "Would install 1 formula:\npython@3.13\n", "")
        if command == [brew, "install", "python@3.13"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command == [brew, "--prefix", "python@3.13"]:
            return subprocess.CompletedProcess(command, 0, f"{prefix}\n", "")
        if command == [str(selected), "--version"]:
            return subprocess.CompletedProcess(command, 0, "Python 3.13.7\n", "")
        request = json.loads(str(kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, _adapter_result(request), "")

    with (
        patch(
            "solet_manager.adapters.resolve_long_lived_python",
            side_effect=[None, None],
        ),
        patch("solet_manager.adapters.shutil.which", return_value=None),
        patch("solet_manager.adapters.subprocess.run", side_effect=runner),
    ):
        preview = invoke_adapter(registry, runner="bootstrap", request=python_request)
        _check(
            preview.checkpoint_status is CheckpointStatus.PENDING and preview.error_kind is None and [item.id for item in preview.planned_actions] == ["python.install_homebrew_formula"],
            "no-Python bootstrap probe presents the reviewed Homebrew action instead of adapter_missing",
        )
        _check(
            calls == [[brew, "--version"]],
            "Python bootstrap probe only validates the resolved Homebrew executable",
        )
        applied = invoke_adapter(
            registry,
            runner="bootstrap",
            request=replace(
                python_request,
                phase="apply",
                probe_purpose=None,
                approval_fingerprint="sha256:" + "2" * 64,
                dry_run=False,
            ),
        )
        _check(
            applied.checkpoint_status is CheckpointStatus.APPLIED,
            "approved Python bootstrap applies through the exact formula-selected interpreter",
        )
        _check(
            registry.refresh_base_python() == selected,
            "registry retains the exact formula-selected interpreter without generic fallback",
        )
        dependency = invoke_adapter(
            registry,
            runner="bootstrap",
            request=replace(
                base,
                operation_id="build_instance_environment",
                operation_ref="bootstrap::environment.ensure_dependency_closure",
            ),
        )
    _check(
        dependency.checkpoint_status is CheckpointStatus.VERIFIED
        and [call for call in calls if call[:1] == [brew]]
        == [
            [brew, "--version"],
            [brew, "--version"],
            [brew, "install", "--dry-run", "python@3.13"],
            [brew, "install", "python@3.13"],
            [brew, "--prefix", "python@3.13"],
        ],
        "approved Python bootstrap validates then uses only the resolved reviewed formula path",
    )
    adapter_calls = [call for call in calls if call[:1] == [str(selected)]]
    _check(
        adapter_calls
        == [
            [str(selected), "--version"],
            [str(selected), str(target / "bootstrap.py"), "--operation-adapter"],
            [str(selected), str(target / "bootstrap.py"), "--operation-adapter"],
        ],
        "formula-selected Python is revalidated and then binds both adapter calls",
    )
    with (
        patch("solet_manager.adapters.resolve_long_lived_python", return_value=None),
        patch("solet_manager.adapters._is_python_runtime_bootstrap", return_value=False),
    ):
        removed_transport = invoke_adapter(
            AdapterRegistry(target=target),
            runner="bootstrap",
            request=python_request,
        )
    _check(
        removed_transport.error_kind == "adapter_missing",
        "removing the pre-interpreter transport fails closed at adapter_missing",
    )


def _check_coding_tool_pre_venv_transport(base: OperationRequest) -> None:
    """The three early coding-tool routes must not require the target venv."""

    flow = json.loads((_REPO / "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json").read_text())
    specifications = (
        ("install_codex_cli", "setup::coding_agents.install_codex", "codex", "cask", "codex"),
        ("install_claude_cli", "setup::coding_agents.install_claude", "claude", "cask", "claude-code"),
        ("install_node", "setup::coding_agents.install_node", "node", "formula", "node"),
    )
    _check(
        all(flow["operations"][operation_id]["runner"] == "bootstrap" for operation_id, *_ in specifications),
        "the three coding-tool operations use the pre-venv bootstrap runner",
    )
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw)
        adapter_module = target / "plugins/github_midwife_plugin/src/github_midwife_plugin/setup_adapter.py"
        adapter_module.parent.mkdir(parents=True)
        adapter_module.write_text("# target hydration adapter fixture\n")
        requests = [replace(base, operation_id=operation_id, operation_ref=operation_ref, public_inputs={}) for operation_id, operation_ref, _executable, _kind, _package in specifications]
        legacy = [invoke_adapter(AdapterRegistry(target=target), runner="hydration", request=request) for request in requests]
        _check(
            [(request.operation_id, result.checkpoint_status.value, result.error_kind) for request, result in zip(requests, legacy, strict=True)] == [("install_codex_cli", "blocked", "adapter_missing"), ("install_claude_cli", "blocked", "adapter_missing"), ("install_node", "blocked", "adapter_missing")],
            "pre-fix fresh-target hydration is exactly the reported three blocked adapter_missing rows",
        )
        _check(
            invoke_adapter(AdapterRegistry(target=target), runner="hydration", request=replace(requests[0], operation_id="install_tmux", operation_ref="setup::tmux.install")).error_kind == "adapter_missing",
            "target_python guard remains unchanged for other hydration operations",
        )
        _check_coding_tool_bootstrap_results(requests, specifications)
        unknown = execute_adapter_request(replace(requests[0], operation_id="unregistered_coding_tool", operation_ref="setup::coding_agents.unregistered").to_dict(), runner=_coding_tool_absent_runner, which=_coding_tool_brew)
        _check(unknown["checkpoint_status"] == "blocked" and unknown["error_kind"] == "adapter_missing", "unknown bootstrap operation remains fail-closed")


def _coding_tool_brew(name: str) -> str | None:
    return "/fixture/brew" if name == "brew" else None


def _coding_tool_absent_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    if command == ["/fixture/brew", "--version"]:
        return subprocess.CompletedProcess(command, 0, "Homebrew 4.4.0\n", "")
    raise AssertionError(f"unexpected absent coding-tool command: {command!r}")


def _check_coding_tool_homebrew_path_fallback(
    requests: list[OperationRequest],
    specifications: tuple[tuple[str, str, str, str, str], ...],
) -> None:
    for homebrew_bin in ("/opt/homebrew/bin", "/usr/local/bin"):
        for request, (_operation_id, _operation_ref, executable, _kind, _package) in zip(requests, specifications, strict=True):
            resolved_path = f"{homebrew_bin}/{executable}"

            def restricted_path_which(
                name: str,
                *,
                resolved_path: str = resolved_path,
            ) -> str | None:
                return resolved_path if name == resolved_path else None

            resolved = execute_adapter_request(
                replace(request, probe_purpose="post_apply").to_dict(),
                runner=_coding_tool_absent_runner,
                which=restricted_path_which,
            )
            _check(
                resolved["checkpoint_status"] == "verified" and resolved["evidence"][0]["observed"] == resolved_path,
                f"restricted PATH resolves {executable} through {homebrew_bin} after install",
            )


def _check_coding_tool_bootstrap_results(requests: list[OperationRequest], specifications: tuple[tuple[str, str, str, str, str], ...]) -> None:
    absent = [execute_adapter_request(request.to_dict(), runner=_coding_tool_absent_runner, which=_coding_tool_brew) for request in requests]
    _check(all(result["checkpoint_status"] == "pending" for result in absent), "post-fix fresh-target preview reaches pending rather than adapter_missing for all three")
    _check_coding_tool_homebrew_path_fallback(requests, specifications)
    for request, (_operation_id, _operation_ref, executable, kind, package) in zip(requests, specifications, strict=True):
        calls: list[list[str]] = []

        def present_runner(
            command: list[str],
            *,
            calls: list[list[str]] = calls,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        present = execute_adapter_request(request.to_dict(), runner=present_runner, which=lambda name, executable=executable: f"/fixture/{executable}" if name == executable else None)
        _check(present["checkpoint_status"] == "verified" and not calls, f"present {executable} verifies without an install or reinstall side effect")
        package_args = ["--cask", package] if kind == "cask" else [package]

        def apply_runner(
            command: list[str],
            *,
            calls: list[list[str]] = calls,
            kind: str = kind,
            package: str = package,
            package_args: list[str] = package_args,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command == ["/fixture/brew", "--version"]:
                return subprocess.CompletedProcess(command, 0, "Homebrew 4.4.0\n", "")
            if command == ["/fixture/brew", "install", "--dry-run", *package_args]:
                return subprocess.CompletedProcess(command, 0, f"Would install 1 {kind}:\n{package}\n", "")
            if command == ["/fixture/brew", "install", *package_args]:
                return subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(f"unexpected coding-tool apply command: {command!r}")

        applied = execute_adapter_request(replace(request, phase="apply", probe_purpose=None, approval_fingerprint="sha256:" + "c" * 64, dry_run=False).to_dict(), runner=apply_runner, which=_coding_tool_brew, sleep=_no_op_sleep)
        _check(
            applied["checkpoint_status"] == "applied" and calls[-3:] == [["/fixture/brew", "--version"], ["/fixture/brew", "install", "--dry-run", *package_args], ["/fixture/brew", "install", *package_args]],
            f"absent {executable} applies only its exact reviewed Homebrew package action",
        )
    _check_coding_tool_cask_apply_retry(requests, specifications)


def _no_op_sleep(_seconds: float) -> None:
    pass


def _cask_apply_runner(
    command: list[str],
    *,
    kind: str,
    package: str,
    package_args: list[str],
    **_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    if command == ["/fixture/brew", "--version"]:
        return subprocess.CompletedProcess(command, 0, "Homebrew 4.4.0\n", "")
    if command == ["/fixture/brew", "install", "--dry-run", *package_args]:
        return subprocess.CompletedProcess(command, 0, f"Would install 1 {kind}:\n{package}\n", "")
    if command == ["/fixture/brew", "install", *package_args]:
        return subprocess.CompletedProcess(command, 0, "", "")
    raise AssertionError(f"unexpected coding-tool apply command: {command!r}")


def _check_cask_apply_retry_succeeds(apply_request: dict[str, object], executable: str, kind: str, apply_runner: object) -> None:
    sleeps: list[float] = []
    calls_before_resolved = 2
    which_calls = 0

    def eventually_resolves(name: str) -> str | None:
        nonlocal which_calls
        if name == "brew":
            return "/fixture/brew"
        if name != executable:
            return None
        which_calls += 1
        return f"/fixture/{executable}" if which_calls > calls_before_resolved else None

    applied = execute_adapter_request(apply_request, runner=apply_runner, which=eventually_resolves, sleep=sleeps.append)
    if kind == "cask":
        _check(
            applied["checkpoint_status"] == "applied" and applied["evidence"][0]["status"] == "verified" and applied["evidence"][0]["observed"] == f"/fixture/{executable}",
            f"cask {executable} apply retries resolution until the linked executable is stat-visible",
        )
        # -1: the route's own pre-apply resolution check consumes the first which() call.
        _check(len(sleeps) == calls_before_resolved - 1, f"cask {executable} apply stops retrying as soon as resolution succeeds")
    else:
        _check(
            applied["checkpoint_status"] == "applied" and not sleeps,
            f"formula {executable} apply never retries resolution -- only kind='cask' hits the new path",
        )


def _check_cask_apply_retry_exhausts(apply_request: dict[str, object], executable: str, kind: str, apply_runner: object) -> None:
    exhausted_sleeps: list[float] = []
    exhausted = execute_adapter_request(
        apply_request,
        runner=apply_runner,
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        sleep=exhausted_sleeps.append,
    )
    if kind == "cask":
        _check(
            exhausted["checkpoint_status"] == "applied" and exhausted["evidence"][0]["status"] == "blocked" and len(exhausted_sleeps) == _CASK_LINK_RETRY_ATTEMPTS - 1,
            f"cask {executable} apply retry is bounded and still reports blocked evidence when the executable never resolves",
        )
    else:
        _check(not exhausted_sleeps, f"formula {executable} apply retry stays unbounded-irrelevant -- it never enters the retry loop")


def _check_coding_tool_cask_apply_retry(
    requests: list[OperationRequest],
    specifications: tuple[tuple[str, str, str, str, str], ...],
) -> None:
    """A cask apply retries resolution briefly before trusting an unresolved check (iss_9d12ff3f)."""

    for request, (_operation_id, _operation_ref, executable, kind, package) in zip(requests, specifications, strict=True):
        package_args = ["--cask", package] if kind == "cask" else [package]

        def apply_runner(
            command: list[str],
            *,
            kind: str = kind,
            package: str = package,
            package_args: list[str] = package_args,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            return _cask_apply_runner(command, kind=kind, package=package, package_args=package_args)

        apply_request = replace(request, phase="apply", probe_purpose=None, approval_fingerprint="sha256:" + "c" * 64, dry_run=False).to_dict()
        _check_cask_apply_retry_succeeds(apply_request, executable, kind, apply_runner)
        _check_cask_apply_retry_exhausts(apply_request, executable, kind, apply_runner)


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw)
        (target / "bootstrap.py").write_text(_SCRIPT, encoding="utf-8")
        registry = AdapterRegistry(target=target, base_python=Path(sys.executable))
        base = OperationRequest(
            request_id="8f2f3ed3-03fc-4f58-915e-eb400a172a67",
            operation_id="fixture",
            operation_ref="setup::fixture",
            phase="probe",
            probe_purpose="preview",
            attempt=1,
            name="bizops",
            target=str(target),
            flow_id="macos.repository_setup",
            flow_source_revision="a" * 40,
            answers_fingerprint="sha256:" + "1" * 64,
            approval_fingerprint=None,
            dry_run=True,
            timeout_seconds=5,
            public_inputs={"mode": "valid"},
        )
        no_python_registry = AdapterRegistry(target=target)
        _check_python_bootstrap_transport(
            target=target,
            registry=no_python_registry,
            base=base,
        )
        _check_coding_tool_pre_venv_transport(base)
        valid = invoke_adapter(registry, runner="bootstrap", request=base)
        _check(valid.checkpoint_status is CheckpointStatus.VERIFIED, "valid adapter verified")
        _check(valid.stdout == "ok", "valid adapter stdout")
        _check(valid.probe_purpose == "preview", "probe purpose echoed")
        _check(valid.planned_actions[0].id == "postgres.start_service", "planned action parsed")
        legacy_reason = invoke_adapter(
            registry,
            runner="bootstrap",
            request=replace(base, public_inputs={"mode": "legacy_reason"}),
        )
        _check(
            legacy_reason.reason is None,
            "legacy result without reason is normalized to None",
        )
        _raises(
            AdapterProtocolError,
            lambda: invoke_adapter(
                registry,
                runner="bootstrap",
                request=replace(base, public_inputs={"mode": "missing_repair"}),
            ),
            "result missing any field other than legacy reason is refused",
        )
        stage_request = replace(
            base,
            operation_id="homebrew_available",
            operation_ref="setup::homebrew.available",
            probe_purpose="stage_entry",
        )
        stage_raw = {
            **valid.to_dict(),
            "request_id": stage_request.request_id,
            "operation_id": stage_request.operation_id,
            "probe_purpose": "stage_entry",
            "planned_actions": [],
            "discovered_candidates": [],
        }
        stage_result = OperationResult.from_dict(stage_raw, stage_request)
        _check(
            stage_result.checkpoint_status is CheckpointStatus.VERIFIED,
            "empty stage-boundary result passes runtime validation",
        )
        _raises(
            AdapterProtocolError,
            lambda: OperationResult.from_dict(
                {**stage_raw, "planned_actions": [valid.planned_actions[0].to_dict()]},
                stage_request,
            ),
            "stage-boundary planned actions fail runtime validation",
        )
        candidate = {
            "decision_id": "embedding_model",
            "value": "fixture",
            "label": "Fixture",
            "recommendation_rank": 0,
            "metadata": {"provider": "fixture"},
        }
        _raises(
            AdapterProtocolError,
            lambda: OperationResult.from_dict(
                {**stage_raw, "discovered_candidates": [candidate]},
                stage_request,
            ),
            "stage-boundary candidates fail runtime validation",
        )
        home_path = str(Path.home() / "Solets" / "bizops" / "client" / "bin" / "bizops")
        home_paths = invoke_adapter(
            registry,
            runner="bootstrap",
            request=replace(
                base,
                public_inputs={"mode": "home_paths", "fixture_path": home_path},
            ),
        )
        _check(
            home_paths.planned_actions[0].target == home_path and home_paths.evidence[0]["observed"] == home_path,
            "approved target-local home paths remain available to adapters and evidence",
        )
        apply_request = replace(
            base,
            phase="apply",
            probe_purpose=None,
            approval_fingerprint="sha256:" + "2" * 64,
            dry_run=False,
        )
        applied = invoke_adapter(registry, runner="bootstrap", request=apply_request)
        _check(
            applied.checkpoint_status is CheckpointStatus.APPLIED,
            "apply reports applied without owning verification",
        )
        apply_exit = _normalize_apply_result(
            invoke_adapter(
                registry,
                runner="bootstrap",
                request=replace(apply_request, public_inputs={"mode": "exit"}),
            )
        )
        apply_timeout = _normalize_apply_result(
            invoke_adapter(
                registry,
                runner="bootstrap",
                request=replace(
                    apply_request,
                    timeout_seconds=1,
                    public_inputs={"mode": "timeout"},
                ),
            )
        )
        apply_missing = _normalize_apply_result(invoke_adapter(registry, runner="unknown", request=apply_request))
        _check(
            apply_exit.error_kind == "adapter_exit_error" and apply_timeout.error_kind == "adapter_timeout" and apply_missing.error_kind == "adapter_missing",
            "apply transport exit, timeout, and missing errors remain stable",
        )
        _raises(
            AdapterProtocolError,
            lambda: invoke_adapter(
                registry,
                runner="bootstrap",
                request=replace(apply_request, public_inputs={"mode": "apply_verified"}),
            ),
            "apply result claiming verified is refused",
        )
        secret = invoke_adapter(
            registry,
            runner="bootstrap",
            request=replace(base, public_inputs={"mode": "secret"}),
        )
        _check("hunter2" not in secret.stdout and "[REDACTED]" in secret.stdout, "output redacted")
        _raises(
            AdapterProtocolError,
            lambda: invoke_adapter(
                registry,
                runner="bootstrap",
                request=replace(base, public_inputs={"mode": "malformed"}),
            ),
            "malformed result refused",
        )
        _raises(
            AdapterProtocolError,
            lambda: invoke_adapter(
                registry,
                runner="bootstrap",
                request=replace(base, public_inputs={"mode": "wrong_id"}),
            ),
            "echo mismatch refused",
        )
        exited = invoke_adapter(
            registry,
            runner="bootstrap",
            request=replace(base, public_inputs={"mode": "exit"}),
        )
        _check(
            exited.error_kind == "adapter_exit_error" and exited.exit_code == 7,
            "exit failure stable",
        )
        timed_out = invoke_adapter(
            registry,
            runner="bootstrap",
            request=replace(base, timeout_seconds=1, public_inputs={"mode": "timeout"}),
        )
        _check(timed_out.timed_out and timed_out.error_kind == "adapter_timeout", "timeout stable")
        missing = invoke_adapter(registry, runner="unknown", request=base)
        _check(missing.checkpoint_status is CheckpointStatus.BLOCKED, "unknown runner blocked")
        discovery = invoke_adapter(
            registry,
            runner="bootstrap",
            request=replace(
                base,
                operation_id="decision.embedding_model.discover",
                probe_purpose="decision_discovery",
                public_inputs={"mode": "valid", "decision_id": "embedding_model"},
            ),
        )
        _check(
            [candidate.value for candidate in discovery.discovered_candidates] == ["recommended", "alternate"],
            "candidates canonicalize recommended rank first",
        )
        _raises(
            AdapterProtocolError,
            lambda: invoke_adapter(
                registry,
                runner="bootstrap",
                request=replace(
                    base,
                    operation_id="decision.embedding_model.discover",
                    probe_purpose="decision_discovery",
                    public_inputs={
                        "mode": "bad_candidate_rank",
                        "decision_id": "embedding_model",
                    },
                ),
            ),
            "invalid candidate recommendation rank refused",
        )
        _raises(
            AdapterProtocolError,
            lambda: invoke_adapter(
                registry,
                runner="bootstrap",
                request=replace(
                    base,
                    operation_id="decision.embedding_model.discover",
                    probe_purpose="decision_discovery",
                    public_inputs={
                        "mode": "duplicate_candidate_rank",
                        "decision_id": "embedding_model",
                    },
                ),
            ),
            "duplicate candidate recommendation ranks refused",
        )
        for mode, label in (
            ("invalid_action_id", "invalid planned-action id refused"),
            ("unknown_evidence", "unknown evidence field refused"),
            ("secret_evidence", "secret evidence refused"),
            ("non_string_stream", "non-string stream refused"),
            ("secret_repair", "secret repair refused"),
        ):
            _raises(
                AdapterProtocolError,
                lambda mode=mode: invoke_adapter(
                    registry,
                    runner="bootstrap",
                    request=replace(base, public_inputs={"mode": mode}),
                ),
                label,
            )

    print(f"adapter_protocol_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
