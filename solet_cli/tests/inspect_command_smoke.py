#!/usr/bin/env python3
"""Offline smoke for the path-addressed active inspection command.

The suite treats ``inspect --target --json`` as the public boundary.  Its
fixtures deliberately carry four independent subject-local discriminators:
the genesis receipt, target interpreter availability, target registry schema,
and service-binding coverage.  Every directional control changes exactly one
of those inputs and asserts that unrelated diagnostic rows remain stable.

The first three pairs preserve the discrimination classes frozen before the
repair existed.  Independent fixture oracles parse or execute the target input
directly instead of calling production diagnostic helpers.  The public
composition controls cover two evidence-preservation defects found during
independent review: wrong-subject receipts and an invalid released rule beside
an independently valid target receipt.

The fourth pair reproduces the field-observed half-wired provider state.  One
service face keeps the provider active while a second advertised face has
registered processes but no target binding.  The negative assertion requires
the exact stranded process keys.  A wholly unbound provider is kept separate:
current profile materialization has no machine-readable intentional-unbinding
declaration, so that state remains intent-indeterminate instead of being called
an accidental defect.

Finally, two hermetic mutation controls prove the instrument is sensitive: one
swaps independent oracle labels and the other replaces the process verifier
with a fixed sentinel.  Both mutated suites must fail before the ordinary suite
may report green.  All target mutations occur only in disposable directories.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

_PACKAGE_ROOT = Path(
    os.environ.get(
        "SOLET_MANAGER_TEST_PACKAGE_ROOT",
        str(Path(__file__).resolve().parents[1] / "src"),
    )
)
sys.path.insert(0, str(_PACKAGE_ROOT))

from solet_manager.cli import build_parser  # noqa: E402
from solet_manager.existing_solet_diagnostics import (  # noqa: E402
    DiagnosticCheck,
    DiagnosticStatus,
    roll_up_active_checks,
)
from solet_manager.inspect import inspect_target  # noqa: E402
from solet_manager.models import ExitCode  # noqa: E402

_CHECKS = 0
_CONTROLS = 0
_MANAGER_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
_PROCESS_CHECK_ID = "inspect::process_input_projection_v1"
_DEPENDENCY_CHECK_ID = "doctor::seat_rotation_dependency_closure_v1"
_BINDING_CHECK_ID = "inspect::apply_manifest_binding_contract_v1"
_SERVICE_BINDING_CHECK_ID = "inspect::registered_service_binding_coverage_v1"
_FIXTURE_PROVIDER = "fixture_deployment_plugin"
_PRIMARY_SERVICE = "self_deployment_service"
_LOCAL_SERVICE = "local_self_deployment_service"
_LOCAL_PROCESSES = (
    "complete_swap",
    "install_autostart",
    "rollback_release",
    "status_autostart",
    "swap_rollback",
    "swap_status",
    "uninstall_autostart",
)


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _control(condition: object, label: str) -> None:
    global _CONTROLS
    _CONTROLS += 1
    if not condition:
        raise AssertionError(label)


def _write_root_manifest(root: Path) -> None:
    (root / "root_manifest.yaml").write_text(
        f"schema_version: 1\nsolet_name: {root.name}\n",
        encoding="utf-8",
    )


def _write_target(root: Path, *, helper_source: str | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _write_root_manifest(root)
    schema = root / "ananta/knowledge_base/processes/lifecycle_management_service"
    schema.mkdir(parents=True)
    (schema / "apply_manifest.json").write_text(
        json.dumps(
            {
                "process_key": (
                    "service_interface::lifecycle_management_service::apply_manifest"
                )
            }
        ),
        encoding="utf-8",
    )
    package = root / "plugins/agent_messaging_plugin"
    source = package / "src/agent_messaging_plugin"
    source.mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        "[project]\nname = 'agent_messaging_plugin'\ndependencies = []\n",
        encoding="utf-8",
    )
    (source / "seat_rotation_helper.py").write_text(
        helper_source or "from __future__ import annotations\nimport iterm2\n",
        encoding="utf-8",
    )
    setup_flow = root / "plugins/github_midwife_plugin/knowledge_base"
    setup_flow.mkdir(parents=True)
    (setup_flow / "macos_setup_flow.json").write_text(
        json.dumps(
            {
                "probes": {
                    "embedding_request_succeeds": {
                        "name": "Embedding request succeeds",
                        "probe_ref": (
                            "service_interface::embedding_service.get_embedding_dimension"
                        ),
                        "runner": "platform_process",
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    venv_bin = root / ".venv/bin"
    venv_bin.mkdir(parents=True)
    solet_probe = venv_bin / "solet-bridge"
    solet_probe.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"invocation_schema\":{\"type\":\"object\","
        "\"required\":[\"process\",\"reason\",\"arguments\"],"
        "\"additionalProperties\":false,\"properties\":{\"arguments\":{"
        "\"type\":\"object\",\"required\":[],\"additionalProperties\":false,"
        "\"properties\":{\"model\":{\"description\":\"Embedding model name\","
        "\"type\":\"string\"}}}}}}'\n",
        encoding="utf-8",
    )
    solet_probe.chmod(0o755)
    _write_binding_coverage_inputs(root, provider_selected=True, bound=True)


def _write_binding_coverage_inputs(
    root: Path,
    *,
    provider_selected: bool,
    bound: bool,
) -> None:
    config = root / "profile/config"
    config.mkdir(parents=True, exist_ok=True)
    selected = [_FIXTURE_PROVIDER] if provider_selected else []
    (config / "manifest.yaml").write_text(
        "profile_name: fixture\nplugins:\n"
        + "".join(f"- {plugin}\n" for plugin in selected),
        encoding="utf-8",
    )
    bindings = (
        {_PRIMARY_SERVICE: _FIXTURE_PROVIDER} if provider_selected else {}
    )
    if provider_selected and bound:
        bindings[_LOCAL_SERVICE] = _FIXTURE_PROVIDER
    (config / "service_bindings.json").write_text(
        json.dumps(bindings, sort_keys=True),
        encoding="utf-8",
    )
    plugin = root / "plugins" / _FIXTURE_PROVIDER
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "plugin.yaml").write_text(
        f"name: {_FIXTURE_PROVIDER}\n"
        "implements:\n"
        "  - interface: SelfDeploymentServiceInterface\n"
        "  - interface: LocalSelfDeploymentServiceInterface\n",
        encoding="utf-8",
    )
    process_root = root / "ananta/knowledge_base/processes" / _LOCAL_SERVICE
    process_root.mkdir(parents=True, exist_ok=True)
    for process_name in _LOCAL_PROCESSES:
        (process_root / f"{process_name}.json").write_text(
            json.dumps(
                {
                    "process_key": (
                        f"service_interface::{_LOCAL_SERVICE}::{process_name}"
                    )
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )


def _write_discrimination_target(
    root: Path,
    *,
    process_resolves: bool,
    dependency_available: bool,
    receipt_complete: bool,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _write_root_manifest(root)
    schema = root / "ananta/knowledge_base/processes/lifecycle_management_service"
    schema.mkdir(parents=True)
    (schema / "apply_manifest.json").write_text(
        json.dumps(
            {
                "process_key": (
                    "service_interface::lifecycle_management_service::apply_manifest"
                ),
                "arguments": {
                    "type": "object",
                    "required": ["new_manifest"],
                    "additionalProperties": False,
                    "properties": {
                        "new_manifest": {
                            "type": "object",
                            "required": ["plugins"],
                            "additionalProperties": False,
                            "properties": {
                                "plugins": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "profile_name": {"type": "string"},
                                "service_bindings": {
                                    "type": "object",
                                    "additionalProperties": {"type": "string"},
                                },
                            },
                        }
                    },
                },
                "binding_rule": {
                    "version": 1,
                    "service_bindings": {
                        "input_path": "new_manifest.service_bindings",
                        "provided_input": "merge_over_current_bindings",
                        "omitted_input": "copy_current_bindings",
                        "materialized_path": "profile/config/service_bindings.json",
                    },
                    "receipt": {
                        "path": ".solet/genesis.json",
                        "required_completed_steps": [
                            "validate_name",
                            "resolve_target",
                            "materialize_configs",
                            "seed_root_manifest",
                            "materialize_kb_symlinks",
                            "write_manifest_marker",
                        ],
                    },
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    package = root / "plugins/agent_messaging_plugin"
    source = package / "src/agent_messaging_plugin"
    source.mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        "[project]\nname = 'agent_messaging_plugin'\ndependencies = []\n",
        encoding="utf-8",
    )
    (source / "seat_rotation_helper.py").write_text(
        "try:\n    import iterm2\nexcept ModuleNotFoundError:\n    iterm2 = None\n",
        encoding="utf-8",
    )
    setup_flow = root / "plugins/github_midwife_plugin/knowledge_base"
    setup_flow.mkdir(parents=True)
    process_ref = (
        "service_interface::embedding_service.get_embedding_dimension"
        if process_resolves
        else "plugin::openai_embeddings_plugin.embed_text"
    )
    (setup_flow / "macos_setup_flow.json").write_text(
        json.dumps(
            {
                "probes": {
                    "embedding_request_succeeds": {
                        "name": "Embedding request succeeds",
                        "probe_ref": process_ref,
                        "runner": "platform_process",
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    venv_bin = root / ".venv/bin"
    venv_bin.mkdir(parents=True)
    python_probe = venv_bin / "python3"
    python_probe.write_text(
        "#!/bin/sh\nexit " + ("0" if dependency_available else "1") + "\n",
        encoding="utf-8",
    )
    python_probe.chmod(0o755)
    solet_probe = venv_bin / "solet-bridge"
    if process_resolves:
        solet_probe.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' '{\"invocation_schema\":{\"type\":\"object\","
            "\"required\":[\"process\",\"reason\",\"arguments\"],"
            "\"additionalProperties\":false,\"properties\":{\"arguments\":{"
            "\"type\":\"object\",\"required\":[],\"additionalProperties\":false,"
            "\"properties\":{\"model\":{\"description\":\"Embedding model name\","
            "\"type\":\"string\"}}}}}}'\n",
            encoding="utf-8",
        )
    else:
        solet_probe.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'Process not found' >&2\nexit 12\n",
            encoding="utf-8",
        )
    solet_probe.chmod(0o755)
    if receipt_complete:
        receipt = root / ".solet/genesis.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps(
                {
                    "solet_name": root.name,
                    "completed_at": "2026-08-25T05:21:24+00:00",
                    "steps": [
                        {"step_name": name, "status": "completed"}
                        for name in (
                            "validate_name",
                            "resolve_target",
                            "materialize_configs",
                            "seed_root_manifest",
                            "materialize_kb_symlinks",
                            "write_manifest_marker",
                        )
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    _write_binding_coverage_inputs(root, provider_selected=True, bound=True)


def _set_dependency_discriminator(root: Path, *, available: bool) -> None:
    probe = root / ".venv/bin/python3"
    probe.write_text(
        "#!/bin/sh\nexit " + ("0" if available else "1") + "\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)


def _set_process_discriminator(
    root: Path,
    *,
    state: str,
) -> None:
    setup_contract = (
        root / "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json"
    )
    declared = (
        "plugin::openai_embeddings_plugin.embed_text"
        if state == "missing"
        else "service_interface::embedding_service.get_embedding_dimension"
    )
    setup_contract.write_text(
        json.dumps(
            {
                "probes": {
                    "embedding_request_succeeds": {
                        "name": "Embedding request succeeds",
                        "probe_ref": declared,
                        "runner": "platform_process",
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    probe = root / ".venv/bin/solet-bridge"
    if state == "missing":
        payload = "#!/bin/sh\nprintf '%s\\n' 'Process not found' >&2\nexit 12\n"
    else:
        schema_payload = json.dumps(
            {
                "invocation_schema": {
                    "type": "object",
                    "required": ["process", "reason", "arguments"],
                    "additionalProperties": False,
                    "properties": {
                        "arguments": {
                            "type": "object",
                            "required": [],
                            "additionalProperties": state == "schema_mismatch",
                            "properties": {
                                "model": {
                                    "description": "Embedding model name",
                                    "type": "string",
                                }
                            },
                        }
                    },
                }
            },
            sort_keys=True,
        )
        payload = f"#!/bin/sh\nprintf '%s\\n' '{schema_payload}'\n"
    probe.write_text(payload, encoding="utf-8")
    probe.chmod(0o755)


def _set_receipt_discriminator(root: Path, *, complete: bool) -> None:
    receipt = root / ".solet/genesis.json"
    if not complete:
        receipt.unlink(missing_ok=True)
        return
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(
            {
                "solet_name": root.name,
                "completed_at": "2026-08-25T05:21:24+00:00",
                "steps": [
                    {"step_name": name, "status": "completed"}
                    for name in (
                        "validate_name",
                        "resolve_target",
                        "materialize_configs",
                        "seed_root_manifest",
                        "materialize_kb_symlinks",
                        "write_manifest_marker",
                    )
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _set_receipt_name(root: Path, name: str) -> None:
    receipt = root / ".solet/genesis.json"
    parsed = json.loads(receipt.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError("receipt fixture must remain an object")
    parsed["solet_name"] = name
    receipt.write_text(json.dumps(parsed, sort_keys=True), encoding="utf-8")


def _set_service_binding_discriminator(root: Path, *, bound: bool) -> None:
    bindings = {_PRIMARY_SERVICE: _FIXTURE_PROVIDER}
    if bound:
        bindings[_LOCAL_SERVICE] = _FIXTURE_PROVIDER
    (root / "profile/config/service_bindings.json").write_text(
        json.dumps(bindings, sort_keys=True),
        encoding="utf-8",
    )


def _set_discriminator(root: Path, check_id: str, *, positive: bool) -> None:
    if check_id == _PROCESS_CHECK_ID:
        _set_process_discriminator(root, state="valid" if positive else "missing")
    elif check_id == _DEPENDENCY_CHECK_ID:
        _set_dependency_discriminator(root, available=positive)
    elif check_id == _BINDING_CHECK_ID:
        _set_receipt_discriminator(root, complete=positive)
    elif check_id == _SERVICE_BINDING_CHECK_ID:
        _set_service_binding_discriminator(root, bound=positive)
    else:
        raise AssertionError(f"unsupported discriminator: {check_id}")


def _independent_oracle(root: Path, check_id: str) -> bool:
    if check_id == _BINDING_CHECK_ID:
        return _receipt_oracle(root)
    if check_id == _DEPENDENCY_CHECK_ID:
        return _dependency_oracle(root)
    if check_id == _SERVICE_BINDING_CHECK_ID:
        return _service_binding_oracle(root)
    return _process_oracle(root)


def _service_binding_oracle(root: Path) -> bool:
    parsed = json.loads(
        (root / "profile/config/service_bindings.json").read_text(encoding="utf-8")
    )
    return isinstance(parsed, dict) and parsed.get(_LOCAL_SERVICE) == _FIXTURE_PROVIDER


def _receipt_oracle(root: Path) -> bool:
    receipt = root / ".solet/genesis.json"
    if not receipt.is_file():
        return False
    parsed = json.loads(receipt.read_text(encoding="utf-8"))
    completed = {
        row.get("step_name")
        for row in parsed.get("steps", [])
        if isinstance(row, dict) and row.get("status") == "completed"
    }
    return parsed.get("solet_name") == root.name and completed == {
        "validate_name",
        "resolve_target",
        "materialize_configs",
        "seed_root_manifest",
        "materialize_kb_symlinks",
        "write_manifest_marker",
    }


def _dependency_oracle(root: Path) -> bool:
    completed = subprocess.run(
        [str(root / ".venv/bin/python3"), "-I", "-c", "import iterm2"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.returncode == 0


def _process_oracle(root: Path) -> bool:
    setup = json.loads(
        (
            root / "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json"
        ).read_text(encoding="utf-8")
    )
    declared = setup["probes"]["embedding_request_succeeds"]["probe_ref"]
    provider, function_name = declared.rsplit(".", maxsplit=1)
    completed = subprocess.run(
        [str(root / ".venv/bin/solet-bridge"), "schema", f"{provider}::{function_name}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0:
        return False
    parsed = json.loads(completed.stdout)
    arguments = parsed["invocation_schema"]["properties"]["arguments"]
    return (
        arguments.get("type") == "object"
        and arguments.get("required") == []
        and arguments.get("additionalProperties") is False
        and isinstance(arguments.get("properties"), dict)
    )


def _run_inspect_argv(
    target: Path,
    *,
    package_root: Path = _PACKAGE_ROOT,
) -> dict[str, object]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(package_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "solet_manager",
            "inspect",
            "--target",
            str(target),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    if not completed.stdout:
        raise AssertionError(
            f"inspect argv emitted no JSON: exit={completed.returncode}, "
            f"stderr={completed.stderr!r}"
        )
    parsed = json.loads(completed.stdout)
    if not isinstance(parsed, dict):
        raise AssertionError(f"inspect argv emitted non-object JSON: {parsed!r}")
    return parsed


def _check_apply_manifest_public_composition_controls() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        wrong_name_target = root / "receipt-identity-fixture"
        _write_discrimination_target(
            wrong_name_target,
            process_resolves=True,
            dependency_available=True,
            receipt_complete=True,
        )
        positive = _run_inspect_argv(wrong_name_target)
        _set_receipt_name(wrong_name_target, "different-subject")
        wrong_name = _run_inspect_argv(wrong_name_target)
        wrong_row = _check_row(wrong_name, _BINDING_CHECK_ID)
        _control(
            _check_status(positive, _BINDING_CHECK_ID) == "verified"
            and _check_status(wrong_name, _BINDING_CHECK_ID) == "failed"
            and wrong_row.get("reason_code")
            == "apply_manifest_binding_contract_conflict"
            and isinstance(wrong_row.get("observed"), dict)
            and wrong_row["observed"].get("target_receipt", {}).get("solet_name")
            == "different-subject",
            "public inspect rejects a completed receipt for a different subject identity",
        )
        _unrelated_row_controls(
            (_PROCESS_CHECK_ID, _DEPENDENCY_CHECK_ID, _BINDING_CHECK_ID),
            _BINDING_CHECK_ID,
            positive,
            wrong_name,
            direction="wrong-name receipt mutation",
            collect_red=False,
        )

        _check_invalid_release_rule_public_control(root)


def _check_invalid_release_rule_public_control(root: Path) -> None:
    mutation_root = root / "invalid-release-package/src"
    package = mutation_root / "solet_manager"
    mutation_root.mkdir(parents=True)
    shutil.copytree(_PACKAGE_ROOT / "solet_manager", package)
    mutated_release = package / "released_metadata/apply_manifest.json"
    release_payload = json.loads(mutated_release.read_text(encoding="utf-8"))
    if not isinstance(release_payload, dict):
        raise AssertionError("released apply_manifest fixture must be an object")
    release_payload.pop("binding_rule", None)
    mutated_release.write_text(
        json.dumps(release_payload, sort_keys=True),
        encoding="utf-8",
    )
    valid_receipt_target = root / "valid-receipt-fixture"
    _write_discrimination_target(
        valid_receipt_target,
        process_resolves=True,
        dependency_available=True,
        receipt_complete=True,
    )
    invalid_release = _run_inspect_argv(
        valid_receipt_target,
        package_root=mutation_root,
    )
    invalid_row = _check_row(invalid_release, _BINDING_CHECK_ID)
    invalid_observed = invalid_row.get("observed")
    released_contract = (
        invalid_observed.get("released_contract")
        if isinstance(invalid_observed, dict)
        else None
    )
    _control(
        _check_status(invalid_release, _BINDING_CHECK_ID) == "failed"
        and isinstance(invalid_observed, dict)
        and isinstance(released_contract, dict)
        and released_contract.get("contract_valid") is False
        and invalid_observed.get("target_receipt_valid") is True
        and isinstance(invalid_observed.get("target_receipt"), dict),
        "public inspect preserves valid receipt evidence beside an invalid released rule; "
        f"observed={invalid_release!r}",
    )


def _check_registered_service_binding_field_control() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        target = root / "selected-provider-binding-fixture"
        _write_discrimination_target(
            target,
            process_resolves=True,
            dependency_available=True,
            receipt_complete=True,
        )
        positive = _run_inspect_argv(target)
        _set_service_binding_discriminator(target, bound=False)
        negative = _run_inspect_argv(target)
        negative_row = _check_row(negative, _SERVICE_BINDING_CHECK_ID)
        observed = negative_row.get("observed")
        unbound = (
            observed.get("unbound_selected_provider_services")
            if isinstance(observed, dict)
            else None
        )
        expected_processes = [
            f"service_interface::{_LOCAL_SERVICE}::{process_name}"
            for process_name in _LOCAL_PROCESSES
        ]
        _control(
            _check_status(positive, _SERVICE_BINDING_CHECK_ID) == "verified"
            and _check_status(negative, _SERVICE_BINDING_CHECK_ID) == "failed"
            and negative_row.get("reason_code")
            == "registered_service_provider_unbound"
            and unbound
            == [
                {
                    "service": _LOCAL_SERVICE,
                    "active_selected_providers": [_FIXTURE_PROVIDER],
                    "stranded_processes": expected_processes,
                    "intent_classification": (
                        "half_wired_provider_intent_indeterminate"
                    ),
                }
            ],
            "public inspect names the unbound service and all seven stranded processes; "
            f"observed={negative_row!r}",
        )
        _unrelated_row_controls(
            (
                _PROCESS_CHECK_ID,
                _DEPENDENCY_CHECK_ID,
                _BINDING_CHECK_ID,
                _SERVICE_BINDING_CHECK_ID,
            ),
            _SERVICE_BINDING_CHECK_ID,
            positive,
            negative,
            direction="service binding removal",
            collect_red=False,
        )

        intentionally_absent = root / "unselected-provider-fixture"
        _write_discrimination_target(
            intentionally_absent,
            process_resolves=True,
            dependency_available=True,
            receipt_complete=True,
        )
        _write_binding_coverage_inputs(
            intentionally_absent,
            provider_selected=False,
            bound=False,
        )
        absent_payload = _run_inspect_argv(intentionally_absent)
        absent_row = _check_row(absent_payload, _SERVICE_BINDING_CHECK_ID)
        absent_observed = absent_row.get("observed")
        _control(
            _check_status(absent_payload, _SERVICE_BINDING_CHECK_ID) == "verified"
            and isinstance(absent_observed, dict)
            and absent_observed.get("intent_declaration_supported") is False
            and absent_observed.get("unbound_selected_provider_services") == [],
            "registered processes without a selected provider are not misclassified as "
            "a binding defect, and the absent intent declaration is explicit",
        )


def _check_status(payload: dict[str, object], check_id: str) -> str:
    return str(_check_row(payload, check_id).get("status"))


def _check_row(payload: dict[str, object], check_id: str) -> dict[str, object]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AssertionError(f"inspect payload lacks data object: {payload!r}")
    checks = data.get("checks")
    if not isinstance(checks, list):
        raise AssertionError(f"inspect payload lacks checks: {payload!r}")
    for row in checks:
        if isinstance(row, dict) and row.get("check_id") == check_id:
            return row
    raise AssertionError(f"inspect payload lacks {check_id}: {payload!r}")


def _landing_control(condition: object, label: str, *, collect_red: bool) -> None:
    if not collect_red:
        _control(condition, label)


def _unrelated_row_controls(
    check_ids: tuple[str, ...],
    selected: str,
    left: dict[str, object],
    right: dict[str, object],
    *,
    direction: str,
    collect_red: bool,
) -> None:
    for unrelated_id in check_ids:
        if unrelated_id == selected:
            continue
        _landing_control(
            _check_status(left, unrelated_id) == _check_status(right, unrelated_id),
            f"{selected} {direction} does not flip {unrelated_id}",
            collect_red=collect_red,
        )


def _directional_pair_controls(
    root: Path,
    index: int,
    check_id: str,
    check_ids: tuple[str, ...],
    *,
    collect_red: bool,
) -> tuple[str, str]:
    target = root / f"directional-{index}"
    _write_discrimination_target(
        target,
        process_resolves=True,
        dependency_available=True,
        receipt_complete=True,
    )
    positive_payload = _run_inspect_argv(target)
    positive_oracle = _independent_oracle(target, check_id)
    _set_discriminator(target, check_id, positive=False)
    negative_payload = _run_inspect_argv(target)
    negative_oracle = _independent_oracle(target, check_id)
    if os.environ.get("SOLET_MANAGER_SWAP_ORACLES") == "1":
        positive_oracle, negative_oracle = negative_oracle, positive_oracle
    observed = (
        _check_status(positive_payload, check_id),
        _check_status(negative_payload, check_id),
    )
    _landing_control(
        observed[0] == "verified" and observed[1] in {"missing", "failed"},
        f"{check_id} positive-to-negative direction follows the frozen class",
        collect_red=collect_red,
    )
    _landing_control(
        positive_oracle and not negative_oracle,
        f"{check_id} independent oracle direction is positive then negative",
        collect_red=collect_red,
    )
    _unrelated_row_controls(
        check_ids,
        check_id,
        positive_payload,
        negative_payload,
        direction="negative mutation",
        collect_red=collect_red,
    )
    negative_row = _check_row(negative_payload, check_id)
    _landing_control(
        negative_row.get("reason_code") is not None
        and negative_row.get("repair_code") is not None,
        f"{check_id} negative class retains actionable reason and repair codes",
        collect_red=collect_red,
    )
    _set_discriminator(target, check_id, positive=True)
    recovered_payload = _run_inspect_argv(target)
    _landing_control(
        _check_status(recovered_payload, check_id) == "verified"
        and _independent_oracle(target, check_id),
        f"{check_id} negative-to-positive direction restores eligibility",
        collect_red=collect_red,
    )
    _unrelated_row_controls(
        check_ids,
        check_id,
        negative_payload,
        recovered_payload,
        direction="positive restoration",
        collect_red=collect_red,
    )
    return observed


def _check_frozen_discrimination_pairs() -> None:
    collect_red = os.environ.get("SOLET_MANAGER_COLLECT_RED") == "1"
    check_ids = (
        _PROCESS_CHECK_ID,
        _DEPENDENCY_CHECK_ID,
        _BINDING_CHECK_ID,
        _SERVICE_BINDING_CHECK_ID,
    )
    observed: dict[str, tuple[str, str]] = {}
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        for index, check_id in enumerate(check_ids):
            observed[check_id] = _directional_pair_controls(
                root,
                index,
                check_id,
                check_ids,
                collect_red=collect_red,
            )
        schema_target = root / "process-schema-sensitivity"
        _write_discrimination_target(
            schema_target,
            process_resolves=True,
            dependency_available=True,
            receipt_complete=True,
        )
        _set_process_discriminator(schema_target, state="schema_mismatch")
        schema_payload = _run_inspect_argv(schema_target)
        _landing_control(
            _check_status(schema_payload, _PROCESS_CHECK_ID) == "failed"
            and not _independent_oracle(schema_target, _PROCESS_CHECK_ID),
            "process projection propagates a live-schema verifier mismatch",
            collect_red=collect_red,
        )
    _check(
        all(positive != negative for positive, negative in observed.values()),
        "the same inspect argv discriminates every frozen positive/negative dimension; "
        f"observed={observed!r}",
    )


def _run_mutation_child(environment_updates: dict[str, str]) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(environment_updates)
    environment["SOLET_MANAGER_SKIP_MUTATION_CONTROLS"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, __file__],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )


def _mutating_probe_source(probe: Path, sentinel: Path) -> str:
    source = probe.read_text(encoding="utf-8")
    marker = "#!/bin/sh\n"
    if source.count(marker) != 1:
        raise AssertionError("target probe fixture must have one shell marker")
    return source.replace(
        marker,
        marker + f": > {shlex.quote(str(sentinel))}\n",
        1,
    )


def _check_active_probe_contract() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        active_target = root / "active-probe-target"
        _write_discrimination_target(
            active_target,
            process_resolves=True,
            dependency_available=True,
            receipt_complete=True,
        )
        active_sentinel = root / "active-probe-ran"
        active_probe = active_target / ".venv/bin/solet-bridge"
        active_probe.write_text(
            _mutating_probe_source(active_probe, active_sentinel),
            encoding="utf-8",
        )
        active_payload = _run_inspect_argv(active_target)
        active_data = active_payload.get("data")
        active_contract = (
            active_data.get("probe_contract") if isinstance(active_data, dict) else None
        )
        _control(
            active_sentinel.is_file()
            and "Active target inspection" in str(active_payload.get("message"))
            and isinstance(active_contract, dict)
            and active_contract.get("mode") == "active"
            and active_contract.get("executes_target_binaries") is True
            and active_contract.get("target_mutation_prevention") == "not_enforced",
            "active inspection executes the identity-valid target probe and truthfully warns "
            f"that mutation prevention is not enforced; payload={active_payload!r}",
        )

        symlink_target = root / "symlink-probe-target"
        _write_discrimination_target(
            symlink_target,
            process_resolves=True,
            dependency_available=True,
            receipt_complete=True,
        )
        substituted_sentinel = root / "substituted-probe-ran"
        target_probe = symlink_target / ".venv/bin/solet-bridge"
        substituted_probe = root / "substituted-solet-bridge"
        substituted_probe.write_text(
            _mutating_probe_source(target_probe, substituted_sentinel),
            encoding="utf-8",
        )
        substituted_probe.chmod(0o755)
        target_probe.unlink()
        target_probe.symlink_to(substituted_probe)
        refused_payload = _run_inspect_argv(symlink_target)
        refused_row = _check_row(refused_payload, _PROCESS_CHECK_ID)
        _control(
            not substituted_sentinel.exists()
            and refused_row.get("status") == "unknown"
            and refused_row.get("reason_code") == "target_runtime_probe_unavailable",
            "active inspection refuses a symlink-substituted target executable before it can "
            f"mutate outside the target; row={refused_row!r}",
        )


def _check_mutation_controls() -> None:
    swapped = _run_mutation_child({"SOLET_MANAGER_SWAP_ORACLES": "1"})
    swapped_output = swapped.stdout + swapped.stderr
    _control(
        swapped.returncode != 0 and "independent oracle direction" in swapped_output,
        "swapping positive and negative oracle labels makes the landing suite fail",
    )
    with tempfile.TemporaryDirectory() as raw:
        mutation_root = Path(raw).resolve() / "package/src"
        package = mutation_root / "solet_manager"
        mutation_root.mkdir(parents=True)
        shutil.copytree(_PACKAGE_ROOT / "solet_manager", package)
        inspect_path = package / "inspect.py"
        source = inspect_path.read_text(encoding="utf-8")
        marker = "_PROBE_TIMEOUT_SECONDS: Final[float] = 10.0\n"
        sentinel = '''

def inspect_process_input_projection(*args: object, **kwargs: object) -> DiagnosticCheck:
    return DiagnosticCheck(
        check_id="inspect::process_input_projection_v1",
        status=DiagnosticStatus.VERIFIED,
        summary="fixed hermetic mutation sentinel",
        reason_code=None,
        repair_code=None,
        observed="fixed_sentinel",
        expected="fixed_sentinel",
        source="hermetic_mutation",
    )
'''
        if source.count(marker) != 1:
            raise AssertionError("sentinel mutation marker is not unique")
        inspect_path.write_text(
            source.replace(marker, marker + sentinel, 1),
            encoding="utf-8",
        )
        sentinel_result = _run_mutation_child(
            {"SOLET_MANAGER_TEST_PACKAGE_ROOT": str(mutation_root)}
        )
    sentinel_output = sentinel_result.stdout + sentinel_result.stderr
    _control(
        sentinel_result.returncode != 0
        and "live-schema verifier mismatch" in sentinel_output,
        "replacing the process verifier result with a fixed sentinel makes the suite fail; "
        f"exit={sentinel_result.returncode}, output={sentinel_output[-2000:]!r}",
    )


def _check_parser() -> None:
    parsed = build_parser().parse_args(["inspect", "--target", "/fixture", "--json"])
    _check(parsed.command == "inspect", "inspect parser registers a dedicated command")
    _check(parsed.target == Path("/fixture"), "inspect parser retains the explicit target path")
    _check(parsed.json is True, "inspect parser retains the local JSON switch")


def _check_manager_runtime_dependencies() -> None:
    metadata = tomllib.loads(_MANAGER_PYPROJECT.read_text(encoding="utf-8"))
    dependencies = metadata["project"]["dependencies"]
    _check(
        dependencies == ["packaging==26.2", "solet-setup-contracts"],
        "manager metadata declares its verified packaging wheel and shared setup contracts",
    )


def _check_installed_wheel_release_metadata() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        source = root / "wheel-source"
        source.mkdir()
        shutil.copy2(_MANAGER_PYPROJECT, source / "pyproject.toml")
        shutil.copytree(_PACKAGE_ROOT, source / "src")
        wheelhouse = root / "wheelhouse"
        wheelhouse.mkdir()
        built = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(wheelhouse),
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        _check(
            built.returncode == 0,
            f"standalone manager wheel builds from copied source; stderr={built.stderr!r}",
        )
        wheels = tuple(wheelhouse.glob("*.whl"))
        _check(len(wheels) == 1, f"wheel build emits one artifact; wheels={wheels!r}")
        installed = root / "installed"
        installed_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-index",
                "--target",
                str(installed),
                str(wheels[0]),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        _check(
            installed_result.returncode == 0,
            "built wheel installs into an external target; "
            f"stderr={installed_result.stderr!r}",
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(installed)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        probed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json; from importlib import resources; "
                    "import solet_manager.inspect as module; "
                    "resource = resources.files('solet_manager').joinpath("
                    "'released_metadata/apply_manifest.json'); "
                    "payload = module._released_apply_manifest_contract(); "
                    "print(json.dumps({'module': module.__file__, 'path': str(resource), "
                    "'binding_rule': payload.get('binding_rule'), "
                    "'arguments': payload.get('arguments')}))"
                ),
            ],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )
        _check(
            probed.returncode == 0,
            "installed wheel resolves its packaged apply_manifest contract without the source "
            f"tree; stderr={probed.stderr!r}",
        )
        payload = json.loads(probed.stdout)
        _check(
            isinstance(payload, dict)
            and Path(str(payload.get("module"))).is_relative_to(installed)
            and Path(str(payload.get("path"))).is_relative_to(installed)
            and isinstance(payload.get("binding_rule"), dict)
            and isinstance(payload.get("arguments"), dict),
            "wheel proof reads module and released metadata from install root; "
            f"payload={payload!r}",
        )


def _check_populated_target() -> None:
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "target"
        _write_target(target)
        result = inspect_target(target)
    details = result.data
    checks = details["checks"]
    _check(result.kind == "existing_solet_inspection", "inspection has a typed result kind")
    _check(result.status == "incomplete", "missing target receipt remains incomplete")
    _check(
        result.exit_code is ExitCode.HUMAN_ACTION,
        "missing target receipt controls the command exit",
    )
    _check(
        [row["path"] for row in details["artifacts"]]
        == [
            "ananta/knowledge_base/processes/lifecycle_management_service/apply_manifest.json",
            "plugins/agent_messaging_plugin/pyproject.toml",
            "plugins/agent_messaging_plugin/src/agent_messaging_plugin/seat_rotation_helper.py",
            "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json",
        ],
        "artifact catalog exposes the complete declared path table",
    )
    _check(
        all(
            set(row) == {"path", "check", "reason_generalizes"}
            for row in details["artifacts"]
        ),
        "each declared artifact exposes its check and seed-generalization reason",
    )
    dependency = next(row for row in checks if row["check_id"].startswith("doctor::"))
    _check(
        dependency["status"] == "missing"
        and dependency["repair_code"] == "repair_requires_declared_dependency_plan",
        "undeclared imported dependency is a repairable packaging finding",
    )
    projection = next(
        row for row in checks if row["check_id"] == "inspect::process_input_projection_v1"
    )
    _check(
        projection["status"] == "verified" and projection["reason_code"] is None,
        "process projection resolves the target-declared live schema",
    )


def _check_guarded_dependency_is_indeterminate() -> None:
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw).resolve() / "target"
        _write_target(
            target,
            helper_source=(
                "try:\n"
                "    import iterm2\n"
                "except ModuleNotFoundError:\n"
                "    iterm2 = None\n"
            ),
        )
        result = inspect_target(target)
    dependency = next(
        row for row in result.data["checks"] if row["check_id"].startswith("doctor::")
    )
    _check(
        dependency["status"] == "unknown"
        and dependency["reason_code"] == "dependency_requirement_indeterminate",
        "guarded import is indeterminate rather than a missing dependency",
    )


def _check_absent_artifacts() -> None:
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "target"
        target.mkdir()
        result = inspect_target(target)
    checks = result.data["checks"]
    not_applicable = [row for row in checks if row["status"] == "not_applicable"]
    _check(len(not_applicable) == 1, "absent selected artifacts become not applicable")
    _check(
        all("lacks declared inspection artifact" in row["summary"] for row in not_applicable),
        "not-applicable artifact summaries identify the absent target input",
    )
    _check(result.status == "incomplete", "absent artifacts do not erase the blocked release check")
    _check(result.exit_code is ExitCode.HUMAN_ACTION, "blocked release proof remains nonzero")
    _check(
        result.data["verified_count"] == 0,
        "not-applicable artifacts are never counted verified",
    )


def _check_invalid_target() -> None:
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "not-a-directory"
        result = inspect_target(target)
    _check(result.status == "incomplete", "missing target root is incomplete rather than a crash")
    _check(result.exit_code is ExitCode.HUMAN_ACTION, "missing target root has a nonzero exit")
    _check(
        result.data["checks"][0]["reason_code"] == "target_root_unavailable",
        "missing target root is self-diagnosing",
    )


def _check_not_applicable_closure() -> None:
    valid = DiagnosticCheck(
        check_id="doctor::seat_rotation_dependency_closure_v1",
        status=DiagnosticStatus.NOT_APPLICABLE,
        summary="closed not-applicable fixture",
        reason_code=None,
        repair_code=None,
        observed={"imported": False, "dependency": "iterm2"},
        expected="iterm2",
        source="static_python_source",
    )
    rollup = roll_up_active_checks((valid,))
    _check(
        rollup.verified_count == 0 and rollup.satisfied_count == 1,
        "closed not-applicable stays unverified",
    )
    malformed = DiagnosticCheck(
        check_id="doctor::seat_rotation_dependency_closure_v1",
        status=DiagnosticStatus.NOT_APPLICABLE,
        summary="malformed target-artifact fixture",
        reason_code=None,
        repair_code=None,
        observed={"imported": False, "dependency": "different"},
        expected="iterm2",
        source="static_python_source",
    )
    try:
        roll_up_active_checks((malformed,))
    except ValueError:
        _check(True, "malformed not-applicable evidence is rejected before rollup")
    else:
        _check(False, "malformed not-applicable evidence must be rejected")


def main() -> int:
    _check_parser()
    _check_manager_runtime_dependencies()
    _check_installed_wheel_release_metadata()
    _check_frozen_discrimination_pairs()
    _check_apply_manifest_public_composition_controls()
    _check_registered_service_binding_field_control()
    _check_populated_target()
    _check_guarded_dependency_is_indeterminate()
    _check_absent_artifacts()
    _check_invalid_target()
    _check_not_applicable_closure()
    if os.environ.get("SOLET_MANAGER_SKIP_MUTATION_CONTROLS") != "1":
        _check_active_probe_contract()
        _check_mutation_controls()
    print(
        json.dumps(
            {"checks": _CHECKS, "controls": _CONTROLS, "status": "passed"},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
