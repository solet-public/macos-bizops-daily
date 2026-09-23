"""Bounded active inspection of a seed-built Solet at an explicit target."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Final, cast

from .diagnostic_contract_checks import (
    inspect_target_apply_manifest_binding,
    read_target_solet_name,
)
from .existing_solet_diagnostics import (
    DiagnosticCheck,
    DiagnosticStatus,
    inspect_declared_platform_probe_ref,
    inspect_declared_python_dependency,
    inspect_process_input_projection,
    roll_up_active_checks,
)
from .lm_studio_diagnostics import inspect_lm_studio
from .models import CommandResult, JsonValue


class InspectionCheck(StrEnum):
    """The lower-level diagnostic selected for a declared target artifact."""

    APPLY_MANIFEST_BINDING = "apply_manifest_binding"
    DECLARED_PYTHON_DEPENDENCY = "declared_python_dependency"
    PROCESS_INPUT_PROJECTION = "process_input_projection"


@dataclass(frozen=True)
class InspectionArtifact:
    """One seed-relative artifact selected as diagnostic input data."""

    relative_path: PurePosixPath
    check: InspectionCheck
    reason_generalizes: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "path": self.relative_path.as_posix(),
            "check": self.check.value,
            "reason_generalizes": self.reason_generalizes,
        }


_ARTIFACTS: Final[tuple[InspectionArtifact, ...]] = (
    InspectionArtifact(
        relative_path=PurePosixPath(
            "ananta/knowledge_base/processes/lifecycle_management_service/"
            "apply_manifest.json"
        ),
        check=InspectionCheck.APPLY_MANIFEST_BINDING,
        reason_generalizes=(
            "The seed manifest copies the platform knowledge-base tree, so this "
            "published lifecycle process contract is present in every seed-built Solet."
        ),
    ),
    InspectionArtifact(
        relative_path=PurePosixPath("plugins/agent_messaging_plugin/pyproject.toml"),
        check=InspectionCheck.DECLARED_PYTHON_DEPENDENCY,
        reason_generalizes=(
            "Every seed profile selects agent_messaging_plugin, and seed assembly archives "
            "the selected plugin subtree including its package metadata."
        ),
    ),
    InspectionArtifact(
        relative_path=PurePosixPath(
            "plugins/agent_messaging_plugin/src/agent_messaging_plugin/"
            "seat_rotation_helper.py"
        ),
        check=InspectionCheck.DECLARED_PYTHON_DEPENDENCY,
        reason_generalizes=(
            "Every seed profile selects agent_messaging_plugin, and its selected subtree "
            "ships runtime source under src rather than the excluded tools directory."
        ),
    ),
    InspectionArtifact(
        relative_path=PurePosixPath(
            "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json"
        ),
        check=InspectionCheck.PROCESS_INPUT_PROJECTION,
        reason_generalizes=(
            "Every macOS seed publishes the setup probe declarations that the Manager "
            "must reconcile with that target's live process registry."
        ),
    ),
)

_DEPENDENCY: Final[str] = "iterm2"
_PROJECTION_BLOCKER_REPAIR: Final[str] = "repair_refused_product_schema_owner"
_PROJECTION_PROBE_NAME: Final[str] = "Embedding request succeeds"
_PROJECTION_PUBLIC_INPUTS: Final[dict[str, object]] = {}
_PROBE_TIMEOUT_SECONDS: Final[float] = 10.0


def inspect_target(target: Path) -> CommandResult:
    """Inspect static artifacts and invoke identity-validated target-local probes."""
    resolved_target = target.expanduser().resolve(strict=False)
    catalog: list[JsonValue] = [
        cast(JsonValue, artifact.to_dict()) for artifact in _ARTIFACTS
    ]
    if not resolved_target.is_dir():
        checks = (_target_root_check(resolved_target),)
    else:
        missing = _missing_artifacts(resolved_target)
        checks = (
            (_not_applicable_artifact_check(missing), _process_projection_check(resolved_target))
            if missing
            else (
                _apply_manifest_binding_check(resolved_target),
                _declared_dependency_check(resolved_target),
                _process_projection_check(resolved_target),
                _registered_service_binding_coverage_check(resolved_target),
            )
        )
    if resolved_target.is_dir():
        checks += inspect_lm_studio(resolved_target)
    rollup = roll_up_active_checks(checks)
    data: dict[str, JsonValue] = {
        "target": str(resolved_target),
        "artifacts": catalog,
        "checks": [cast(JsonValue, check.to_dict()) for check in rollup.checks],
        "verified_count": rollup.verified_count,
        "satisfied_count": rollup.satisfied_count,
        "total_count": rollup.total_count,
        "summary": cast(JsonValue, rollup.summary),
        "probe_contract": {
            "mode": "active",
            "executes_target_binaries": True,
            "executable_identity_validation": (
                "absolute nonsymlink target and nonsymlink contained executable chain"
            ),
            "target_mutation_prevention": "not_enforced",
            "side_effect_warning": (
                "Identity-validated target-local probes may still have side effects."
            ),
        },
    }
    return CommandResult(
        kind="existing_solet_inspection",
        status=rollup.status,
        message=(
            "Active target inspection completed. Identity-validated target-local probes may "
            "have side effects."
        ),
        exit_code=rollup.exit_code,
        error_kind=(None if rollup.status == "verified" else "inspection_incomplete"),
        repair=(
            None
            if rollup.status == "verified"
            else "Review each diagnostic finding and its repair code before changing the target."
        ),
        data=data,
    )


def _target_root_check(target: Path) -> DiagnosticCheck:
    return DiagnosticCheck(
        check_id="inspect::target_root_v1",
        status=DiagnosticStatus.UNKNOWN,
        summary="The requested inspection target is not an existing directory.",
        reason_code="target_root_unavailable",
        repair_code="repair_requires_existing_target_root",
        observed=str(target),
        expected="existing directory",
        source="target_argument",
    )


def _apply_manifest_binding_check(target: Path) -> DiagnosticCheck:
    return inspect_target_apply_manifest_binding(
        _released_apply_manifest_contract(),
        target / ".solet/genesis.json",
        expected_solet_name=read_target_solet_name(target / "root_manifest.yaml"),
    )


def _registered_service_binding_coverage_check(target: Path) -> DiagnosticCheck:
    check_id = "inspect::registered_service_binding_coverage_v1"
    repair_code = "repair_requires_service_binding_or_explicit_unbound_intent"
    manifest_path = target / "profile/config/manifest.yaml"
    bindings_path = target / "profile/config/service_bindings.json"
    process_root = target / "ananta/knowledge_base/processes"
    try:
        selected_plugins = _selected_plugins(manifest_path)
        bindings = _service_bindings(bindings_path)
        registered_processes = _registered_service_processes(process_root)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return DiagnosticCheck(
            check_id=check_id,
            status=DiagnosticStatus.UNKNOWN,
            summary="The target's service-binding coverage inputs are unavailable or invalid.",
            reason_code="service_binding_coverage_inputs_invalid",
            repair_code=repair_code,
            observed={
                "manifest_path": str(manifest_path),
                "service_bindings_path": str(bindings_path),
                "process_root": str(process_root),
                "error": str(exc),
                "next_action": (
                    "Restore the target manifest, service bindings, and process contracts, "
                    "then retry active inspection."
                ),
            },
            expected="readable target-local manifest, bindings, and process contracts",
            source="target_manifest_bindings_and_process_contracts",
        )
    provider_services, provider_manifest_gaps = _selected_provider_services(
        target,
        selected_plugins,
    )
    active_provider_plugins = set(bindings.values())
    unbound: list[JsonValue] = []
    covered: list[JsonValue] = []
    for service_name, providers in sorted(provider_services.items()):
        processes = registered_processes.get(service_name)
        if not processes:
            continue
        active_providers = sorted(active_provider_plugins.intersection(providers))
        if not active_providers:
            continue
        if service_name in bindings:
            covered.append(service_name)
            continue
        unbound.append(
            {
                "service": service_name,
                "active_selected_providers": cast(list[JsonValue], active_providers),
                "stranded_processes": cast(list[JsonValue], list(processes)),
                "intent_classification": "half_wired_provider_intent_indeterminate",
            }
        )
    evidence: dict[str, JsonValue] = {
        "manifest_path": str(manifest_path),
        "service_bindings_path": str(bindings_path),
        "process_root": str(process_root),
        "selected_plugin_count": len(selected_plugins),
        "registered_service_count": len(registered_processes),
        "bound_service_count": len(bindings),
        "active_service_provider_plugins": cast(
            list[JsonValue],
            sorted(active_provider_plugins),
        ),
        "covered_selected_provider_services": covered,
        "unbound_selected_provider_services": unbound,
        "selected_plugins_without_readable_provider_manifest": provider_manifest_gaps,
        "intent_declaration_supported": False,
        "intent_limitation": (
            "The materialized profile has no machine-readable intentionally-unbound "
            "service declaration; template comments do not survive materialization. "
            "This check therefore fails only a half-wired provider that is selected and "
            "active through another binding while registered processes on one of its "
            "advertised service faces remain unbound. A selected but wholly unbound "
            "provider remains intent-indeterminate."
        ),
    }
    if unbound:
        evidence["next_action"] = (
            "Bind each named service to one of its selected providers, or extend the "
            "profile format with an explicit intentionally-unbound declaration."
        )
        return DiagnosticCheck(
            check_id=check_id,
            status=DiagnosticStatus.FAILED,
            summary=(
                "Selected provider plugins expose registered service processes that the "
                "target has not bound."
            ),
            reason_code="registered_service_provider_unbound",
            repair_code=repair_code,
            observed=evidence,
            expected="every registered service exposed by a selected provider is bound",
            source="target_manifest_bindings_and_process_contracts",
        )
    evidence["next_action"] = None
    return DiagnosticCheck(
        check_id=check_id,
        status=DiagnosticStatus.VERIFIED,
        summary="Every registered service exposed by a selected provider is bound.",
        reason_code=None,
        repair_code=None,
        observed=evidence,
        expected="every registered service exposed by a selected provider is bound",
        source="target_manifest_bindings_and_process_contracts",
    )


def _selected_plugins(path: Path) -> tuple[str, ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    plugins: list[str] = []
    in_plugins = False
    for line in lines:
        if line == "plugins:":
            in_plugins = True
            continue
        if not in_plugins:
            continue
        if line.startswith("- "):
            plugin = line.removeprefix("- ").strip()
            if not plugin:
                raise ValueError("profile manifest contains an empty plugin name")
            plugins.append(plugin)
            continue
        if line and not line.startswith((" ", "#")):
            break
    if len(plugins) != len(set(plugins)):
        raise ValueError("profile manifest contains duplicate plugin names")
    return tuple(plugins)


def _service_bindings(path: Path) -> dict[str, str]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in loaded.items()
    ):
        raise ValueError("service_bindings.json must map service names to plugin names")
    return cast(dict[str, str], loaded)


def _registered_service_processes(root: Path) -> dict[str, tuple[str, ...]]:
    if not root.is_dir():
        raise ValueError("target process-contract root is unavailable")
    registered: dict[str, tuple[str, ...]] = {}
    for service_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        process_keys: list[str] = []
        for process_path in sorted(service_dir.glob("*.json")):
            loaded: object = json.loads(process_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or not isinstance(
                loaded.get("process_key"),
                str,
            ):
                raise ValueError(f"process contract lacks process_key: {process_path}")
            process_keys.append(cast(str, loaded["process_key"]))
        if process_keys:
            registered[service_dir.name] = tuple(process_keys)
    return registered


def _selected_provider_services(
    target: Path,
    selected_plugins: tuple[str, ...],
) -> tuple[dict[str, tuple[str, ...]], list[JsonValue]]:
    providers: dict[str, list[str]] = {}
    gaps: list[JsonValue] = []
    for plugin_name in selected_plugins:
        plugin_manifest = target / "plugins" / plugin_name / "plugin.yaml"
        try:
            lines = plugin_manifest.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            gaps.append(
                {
                    "plugin": plugin_name,
                    "path": str(plugin_manifest),
                    "error": type(exc).__name__,
                }
            )
            continue
        for line in lines:
            match = re.fullmatch(r"\s*- interface:\s*([A-Za-z][A-Za-z0-9]*)\s*", line)
            if match is None or not match.group(1).endswith("ServiceInterface"):
                continue
            service_name = _service_name_from_interface(match.group(1))
            providers.setdefault(service_name, []).append(plugin_name)
    return (
        {
            service: tuple(sorted(set(plugin_names)))
            for service, plugin_names in providers.items()
        },
        gaps,
    )


def _service_name_from_interface(interface_name: str) -> str:
    stem = interface_name.removesuffix("Interface")
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", stem)
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", words)
    return words.lower()


def _released_apply_manifest_contract() -> dict[str, object]:
    resource = resources.files("solet_manager").joinpath(
        "released_metadata/apply_manifest.json"
    )
    try:
        loaded: object = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot read packaged apply_manifest binding contract") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("packaged apply_manifest binding contract must be an object")
    return cast(dict[str, object], loaded)


def _declared_dependency_check(target: Path) -> DiagnosticCheck:
    artifacts = _artifacts_for(InspectionCheck.DECLARED_PYTHON_DEPENDENCY)
    metadata = _artifact_path(target, artifacts[0])
    source = _artifact_path(target, artifacts[1])
    completed = _run_target_probe(
        target,
        executable=PurePosixPath(".venv/bin/python3"),
        arguments=("-I", "-c", f"import {_DEPENDENCY}"),
    )
    availability_evidence: JsonValue = (
        {
            "probe": f".venv/bin/python3 -I -c import {_DEPENDENCY}",
            "exit_code": completed.returncode,
            "stderr": completed.stderr.strip()[-2_000:],
        }
        if completed is not None
        else {
            "probe": f".venv/bin/python3 -I -c import {_DEPENDENCY}",
            "result": "unavailable_or_timed_out",
        }
    )
    return inspect_declared_python_dependency(
        metadata,
        source,
        dependency=_DEPENDENCY,
        target_available=(None if completed is None else completed.returncode == 0),
        availability_evidence=availability_evidence,
    )


def _missing_artifacts(target: Path) -> tuple[str, ...]:
    return tuple(
        artifact.relative_path.as_posix()
        for artifact in _ARTIFACTS
        if _is_absent(_artifact_path(target, artifact))
    )


def _not_applicable_artifact_check(missing: tuple[str, ...]) -> DiagnosticCheck:
    if not missing:
        raise ValueError("not-applicable artifact check requires a missing artifact")
    return DiagnosticCheck(
        check_id="doctor::seat_rotation_dependency_closure_v1",
        status=DiagnosticStatus.NOT_APPLICABLE,
        summary=(
            "The selected target lacks declared inspection artifact(s): "
            f"{', '.join(missing)}."
        ),
        reason_code=None,
        repair_code=None,
        observed={"imported": False, "dependency": _DEPENDENCY},
        expected=_DEPENDENCY,
        source="static_python_source",
    )


def _process_projection_check(target: Path) -> DiagnosticCheck:
    artifact = _single_artifact(InspectionCheck.PROCESS_INPUT_PROJECTION)
    declared = inspect_declared_platform_probe_ref(
        _artifact_path(target, artifact),
        probe_name=_PROJECTION_PROBE_NAME,
    )
    if isinstance(declared, DiagnosticCheck):
        return declared
    registry_key = _registry_process_key(declared)
    completed = _run_target_probe(
        target,
        executable=PurePosixPath(".venv/bin/solet-bridge"),
        arguments=("schema", registry_key),
    )
    if completed is None:
        return _target_probe_unavailable_check(
            check_id="inspect::process_input_projection_v1",
            summary="The target-local process registry probe could not be executed.",
            observed={
                "declared_probe_ref": declared,
                "registry_process_key": registry_key,
                "probe": ".venv/bin/solet-bridge schema",
                "next_action": "Restore the target-local CLI and retry active inspection.",
            },
            expected={"declared_probe_ref": declared, "registry_resolution": "available"},
            source="target_process_registry",
            repair_code=_PROJECTION_BLOCKER_REPAIR,
        )
    if completed.returncode != 0:
        return DiagnosticCheck(
            check_id="inspect::process_input_projection_v1",
            status=DiagnosticStatus.MISSING,
            summary="The target's declared setup process is absent from its live registry.",
            reason_code="process_input_projection_mismatch",
            repair_code=_PROJECTION_BLOCKER_REPAIR,
            observed={
                "declared_probe_ref": declared,
                "registry_process_key": registry_key,
                "registry_exit_code": completed.returncode,
                "registry_stderr": completed.stderr.strip(),
                "next_action": (
                    "Publish a callable setup probe_ref and activate the provider that owns it."
                ),
            },
            expected={
                "declared_probe_ref": declared,
                "registry_process_key": registry_key,
                "registry_exit_code": 0,
                "released_public_inputs": {},
            },
            source="target_setup_contract_and_live_registry",
        )
    try:
        payload: object = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return _malformed_registry_schema_check(declared, f"invalid JSON: {exc.msg}")
    arguments = _registry_arguments_schema(payload)
    if arguments is None:
        return _malformed_registry_schema_check(
            declared,
            "invocation_schema.properties.arguments is absent or malformed",
        )
    projection = inspect_process_input_projection(
        {"arguments": arguments},
        required_public_inputs=_PROJECTION_PUBLIC_INPUTS,
        source="target_process_registry",
    )
    return DiagnosticCheck(
        check_id=projection.check_id,
        status=projection.status,
        summary=(
            "The target's declared setup process resolves and its invocation schema "
            "accepts the release-owned public-input projection."
            if projection.status is DiagnosticStatus.VERIFIED
            else projection.summary
        ),
        reason_code=projection.reason_code,
        repair_code=projection.repair_code,
        observed={
            "declared_probe_ref": declared,
            "registry_process_key": registry_key,
            "registry_exit_code": completed.returncode,
            "projection": projection.observed,
            "next_action": (
                None
                if projection.status is DiagnosticStatus.VERIFIED
                else "Align the declared probe's invocation schema with released Manager inputs."
            ),
        },
        expected={
            "registry_exit_code": 0,
            "released_public_inputs": {},
            "projection": projection.expected,
        },
        source="target_setup_contract_and_live_registry",
    )


def _run_target_probe(
    target: Path,
    *,
    executable: PurePosixPath,
    arguments: tuple[str, ...],
) -> subprocess.CompletedProcess[str] | None:
    path = _validated_probe_executable(target, executable)
    if path is None:
        return None
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        return subprocess.run(
            [str(path), *arguments],
            check=False,
            capture_output=True,
            text=True,
            cwd=target,
            env=env,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _validated_probe_executable(target: Path, executable: PurePosixPath) -> Path | None:
    if executable.is_absolute() or ".." in executable.parts:
        return None
    path = target.joinpath(*executable.parts)
    try:
        resolved_target = target.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except OSError:
        return None
    parents = tuple(
        target.joinpath(*executable.parts[:index])
        for index in range(1, len(executable.parts))
    )
    if not _probe_target_identity_valid(target):
        return None
    if not _probe_path_identity_valid(
        path=path,
        parents=parents,
        resolved_target=resolved_target,
        resolved_path=resolved_path,
    ):
        return None
    return path


def _probe_target_identity_valid(target: Path) -> bool:
    return target.is_absolute() and target.is_dir() and not target.is_symlink()


def _probe_path_identity_valid(
    *,
    path: Path,
    parents: tuple[Path, ...],
    resolved_target: Path,
    resolved_path: Path,
) -> bool:
    return (
        path.is_file()
        and not path.is_symlink()
        and not any(parent.is_symlink() for parent in parents)
        and resolved_path.is_relative_to(resolved_target)
        and os.access(path, os.X_OK)
    )


def _registry_process_key(declared_probe_ref: str) -> str:
    provider, function_name = declared_probe_ref.rsplit(".", maxsplit=1)
    return f"{provider}::{function_name}"


def _registry_arguments_schema(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    invocation = payload.get("invocation_schema")
    if not isinstance(invocation, dict):
        return None
    properties = invocation.get("properties")
    if not isinstance(properties, dict):
        return None
    arguments = properties.get("arguments")
    if not isinstance(arguments, dict):
        return None
    return cast(dict[str, object], arguments)


def _malformed_registry_schema_check(declared: str, detail: str) -> DiagnosticCheck:
    return DiagnosticCheck(
        check_id="inspect::process_input_projection_v1",
        status=DiagnosticStatus.FAILED,
        summary="The target registry returned a malformed process invocation schema.",
        reason_code="process_input_projection_mismatch",
        repair_code=_PROJECTION_BLOCKER_REPAIR,
        observed={
            "declared_probe_ref": declared,
            "schema_error": detail,
            "next_action": "Repair the target registry schema response and retry inspection.",
        },
        expected="closed invocation_schema.properties.arguments object",
        source="target_process_registry",
    )


def _target_probe_unavailable_check(
    *,
    check_id: str,
    summary: str,
    observed: JsonValue,
    expected: JsonValue,
    source: str,
    repair_code: str,
) -> DiagnosticCheck:
    return DiagnosticCheck(
        check_id=check_id,
        status=DiagnosticStatus.UNKNOWN,
        summary=summary,
        reason_code="target_runtime_probe_unavailable",
        repair_code=repair_code,
        observed=observed,
        expected=expected,
        source=source,
    )


def _single_artifact(check: InspectionCheck) -> InspectionArtifact:
    artifacts = _artifacts_for(check)
    if len(artifacts) != 1:
        raise AssertionError(f"{check.value} requires exactly one declared artifact")
    return artifacts[0]


def _artifacts_for(check: InspectionCheck) -> tuple[InspectionArtifact, ...]:
    return tuple(artifact for artifact in _ARTIFACTS if artifact.check is check)


def _artifact_path(target: Path, artifact: InspectionArtifact) -> Path:
    return target.joinpath(*artifact.relative_path.parts)


def _is_absent(path: Path) -> bool:
    return not os.path.lexists(path)
