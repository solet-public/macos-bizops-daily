"""Closed contract and target-evidence composition for existing-Solet diagnostics."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from . import existing_solet_diagnostics as base
from .models import JsonValue

_REQUIRED_GENESIS_STEPS: tuple[str, ...] = (
    "validate_name",
    "resolve_target",
    "materialize_configs",
    "seed_root_manifest",
    "materialize_kb_symlinks",
    "write_manifest_marker",
)
_SOLET_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
APPLY_MANIFEST_BINDING_RULE: dict[str, JsonValue] = {
    "version": 1,
    "service_bindings": {
        "input_path": "new_manifest.service_bindings",
        "provided_input": "merge_over_current_bindings",
        "omitted_input": "copy_current_bindings",
        "materialized_path": "profile/config/service_bindings.json",
    },
    "receipt": {
        "path": ".solet/genesis.json",
        "required_completed_steps": list(_REQUIRED_GENESIS_STEPS),
    },
}


def inspect_process_input_projection(
    schema: Path | Mapping[str, object],
    *,
    required_public_inputs: Mapping[str, object],
    source: str,
) -> base.DiagnosticCheck:
    """Verify one closed process schema against release-owned public inputs."""
    check_id = "inspect::process_input_projection_v1"
    reason_code = "process_input_projection_mismatch"
    repair_code = "repair_refused_product_schema_owner"
    expected_inputs = base.validated_public_input_contract(required_public_inputs)
    expected_names = sorted(expected_inputs)
    expected_names_json = [cast(JsonValue, name) for name in expected_names]
    loaded = base.load_json_object(schema) if isinstance(schema, Path) else dict(schema)
    if isinstance(loaded, base._StaticRead):
        return base.artifact_check(
            check_id=check_id,
            loaded=loaded,
            reason_code=reason_code,
            repair_code=repair_code,
            expected=expected_names_json,
            source=source,
        )
    if not base._is_json_object(loaded):
        return base.failed_check(
            check_id,
            "Process schema must contain only finite JSON values.",
            reason_code,
            repair_code,
            observed=None,
            expected=expected_names_json,
            source=source,
        )
    arguments = loaded.get("arguments")
    if not isinstance(arguments, dict):
        return base.failed_check(
            check_id,
            "Process schema lacks a closed arguments object.",
            reason_code,
            repair_code,
            observed=None,
            expected=expected_names_json,
            source=source,
        )
    properties = arguments.get("properties")
    if not isinstance(properties, dict):
        return base.failed_check(
            check_id,
            "Process schema lacks declared argument properties.",
            reason_code,
            repair_code,
            observed=None,
            expected=expected_names_json,
            source=source,
        )
    observed, expected, valid = base.projection_evidence(
        cast(dict[str, object], arguments),
        cast(dict[str, object], properties),
        expected_inputs,
        expected_names,
        expected_names_json,
    )
    if not valid:
        return base.failed_check(
            check_id,
            "The public process projection does not match the closed released input contract.",
            reason_code,
            repair_code,
            observed=observed,
            expected=expected,
            source=source,
        )
    return base.verified_check(
        check_id,
        "The closed process schema matches every release-owned Manager input definition.",
        observed=observed,
        expected=expected,
        source=source,
    )


def inspect_declared_platform_probe_ref(
    setup_contract_path: Path,
    *,
    probe_name: str,
) -> str | base.DiagnosticCheck:
    """Read one uniquely named platform-process ref from a target setup contract."""
    loaded = base.load_json_object(setup_contract_path)
    if isinstance(loaded, base._StaticRead):
        return base.artifact_check(
            check_id="inspect::process_input_projection_v1",
            loaded=loaded,
            reason_code="process_input_projection_mismatch",
            repair_code="repair_refused_product_schema_owner",
            expected={"probe_name": probe_name, "runner": "platform_process"},
            source="target_setup_contract",
        )
    matches = [
        item for item in _nested_objects(cast(JsonValue, loaded)) if item.get("name") == probe_name
    ]
    if len(matches) != 1:
        return _projection_contract_failure(
            "The target setup contract does not declare exactly one expected platform probe.",
            {"probe_name": probe_name, "matches": len(matches)},
            {"probe_name": probe_name, "matches": 1},
        )
    match = matches[0]
    probe_ref = match.get("probe_ref")
    if match.get("runner") != "platform_process" or not isinstance(probe_ref, str):
        return _projection_contract_failure(
            "The target setup probe is not a transportable platform-process declaration.",
            {"probe_name": probe_name, "runner": match.get("runner"), "probe_ref": probe_ref},
            {"runner": "platform_process", "probe_ref": "transportable string"},
        )
    if re.fullmatch(r"[a-z][a-z0-9_]*::[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", probe_ref) is None:
        return _projection_contract_failure(
            "The target setup probe reference is malformed.",
            {"probe_name": probe_name, "probe_ref": probe_ref},
            "<provider_type>::<provider>.<function_name>",
        )
    return probe_ref


def _projection_contract_failure(
    summary: str,
    observed: JsonValue,
    expected: JsonValue,
) -> base.DiagnosticCheck:
    return base.failed_check(
        "inspect::process_input_projection_v1",
        summary,
        "process_input_projection_mismatch",
        "repair_refused_product_schema_owner",
        observed=observed,
        expected=expected,
        source="target_setup_contract",
    )


def _nested_objects(value: JsonValue) -> list[dict[str, JsonValue]]:
    if isinstance(value, list):
        return [item for child in value for item in _nested_objects(child)]
    if not isinstance(value, dict):
        return []
    return [value, *(item for child in value.values() for item in _nested_objects(child))]


def inspect_apply_manifest_binding_contract(
    schema: Path | Mapping[str, object],
) -> base.DiagnosticCheck:
    """Verify the selected release publishes the exact closed binding rule."""
    check_id = "inspect::apply_manifest_binding_contract_v1"
    reason_code = "apply_manifest_binding_contract_conflict"
    repair_code = "repair_refused_contract_metadata_owner"
    loaded = base.load_json_object(schema) if isinstance(schema, Path) else dict(schema)
    if isinstance(loaded, base._StaticRead):
        return base.artifact_check(
            check_id=check_id,
            loaded=loaded,
            reason_code=reason_code,
            repair_code=repair_code,
            expected="released machine binding rule plus canonical nested payload",
            source="released_apply_manifest_contract",
        )
    if not base._is_json_object(loaded):
        return base.failed_check(
            check_id,
            "Released apply_manifest metadata must contain only finite JSON values.",
            reason_code,
            repair_code,
            observed={"contract_valid": False, "metadata": "invalid_json_values"},
            expected="finite JSON object",
            source="released_apply_manifest_contract",
        )
    payload_error = base.binding_payload_error(loaded)
    rule_valid = loaded.get("binding_rule") == APPLY_MANIFEST_BINDING_RULE
    if payload_error is not None or not rule_valid:
        return base.failed_check(
            check_id,
            payload_error or "Released apply_manifest metadata lacks the required binding rule.",
            reason_code,
            repair_code,
            observed={
                "contract_valid": False,
                "machine_rule": "valid" if rule_valid else "invalid",
                "nested_payload": "invalid" if payload_error else "valid",
                "next_action": "Publish the closed apply_manifest arguments and binding_rule.",
            },
            expected={"machine_rule": APPLY_MANIFEST_BINDING_RULE, "nested_payload": "valid"},
            source="released_apply_manifest_contract",
        )
    evidence: dict[str, JsonValue] = {
        "contract_valid": True,
        "machine_rule": "valid",
        "nested_payload": "valid",
    }
    return base.verified_check(
        check_id,
        "Released apply_manifest metadata publishes a closed binding rule.",
        observed=evidence,
        expected=evidence,
        source="released_apply_manifest_contract",
    )


def inspect_target_apply_manifest_binding(
    schema: Path | Mapping[str, object],
    receipt_path: Path,
    *,
    expected_solet_name: str | None,
) -> base.DiagnosticCheck:
    """Combine static binding validity with the target's immutable receipt."""
    contract = inspect_apply_manifest_binding_contract(schema)
    receipt = base.load_json_object(receipt_path)
    if isinstance(receipt, base._StaticRead):
        receipt_error, receipt_observed = _static_receipt_evidence(receipt)
        if contract.status is base.DiagnosticStatus.VERIFIED:
            return _valid_contract_static_receipt(
                contract,
                receipt,
                receipt_observed=receipt_observed,
            )
    else:
        receipt_error, receipt_observed = _receipt_evidence(
            receipt,
            expected_solet_name=expected_solet_name,
        )
    if contract.status is not base.DiagnosticStatus.VERIFIED:
        return _invalid_contract_with_receipt_evidence(
            contract,
            receipt_error=receipt_error,
            receipt_observed=receipt_observed,
        )
    if receipt_error is not None:
        return base.failed_check(
            contract.check_id,
            receipt_error,
            "apply_manifest_binding_contract_conflict",
            "repair_refused_contract_metadata_owner",
            observed={
                **cast(dict[str, JsonValue], contract.observed),
                "target_receipt": receipt_observed,
            },
            expected=_receipt_expected(contract),
            source="released_contract_and_target_genesis_receipt",
        )
    return base.verified_check(
        contract.check_id,
        "The released binding rule is valid and the target records its materialization.",
        observed={
            **cast(dict[str, JsonValue], contract.observed),
            "target_receipt": receipt_observed,
        },
        expected={
            **cast(dict[str, JsonValue], contract.expected),
            "target_receipt": receipt_observed,
        },
        source="released_contract_and_target_genesis_receipt",
    )


def _static_receipt_evidence(
    receipt: base._StaticRead,
) -> tuple[str, JsonValue]:
    missing = receipt.status is base.DiagnosticStatus.MISSING
    return (
        (
            "The released binding rule is valid, but the target genesis receipt is missing."
            if missing
            else f"The released binding rule is valid, but {receipt.summary}"
        ),
        {
            "state": "missing" if missing else "invalid",
            "detail": receipt.summary,
            "next_action": "Complete genesis so the Manager writes its immutable receipt.",
        },
    )


def _valid_contract_static_receipt(
    contract: base.DiagnosticCheck,
    receipt: base._StaticRead,
    *,
    receipt_observed: JsonValue,
) -> base.DiagnosticCheck:
    return base.DiagnosticCheck(
        check_id=contract.check_id,
        status=receipt.status,
        summary=(
            "The released binding rule is valid, but the target genesis receipt "
            f"is not usable: {receipt.summary}"
        ),
        reason_code="apply_manifest_binding_contract_conflict",
        repair_code="repair_refused_contract_metadata_owner",
        observed={
            **cast(dict[str, JsonValue], contract.observed),
            "target_receipt": receipt_observed,
        },
        expected=_receipt_expected(contract),
        source="released_contract_and_target_genesis_receipt",
    )


def _invalid_contract_with_receipt_evidence(
    contract: base.DiagnosticCheck,
    *,
    receipt_error: str | None,
    receipt_observed: JsonValue,
) -> base.DiagnosticCheck:
    return base.DiagnosticCheck(
        check_id=contract.check_id,
        status=contract.status,
        summary=contract.summary,
        reason_code=contract.reason_code,
        repair_code=contract.repair_code,
        observed={
            "released_contract": contract.observed,
            "target_receipt": receipt_observed,
            "target_receipt_valid": receipt_error is None,
        },
        expected={
            "released_contract": contract.expected,
            "target_receipt": "completed for the declared target identity",
        },
        source="released_contract_and_target_genesis_receipt",
    )


def _receipt_expected(contract: base.DiagnosticCheck) -> dict[str, JsonValue]:
    return {
        **cast(dict[str, JsonValue], contract.expected),
        "target_receipt": "completed",
        "required_completed_steps": list(_REQUIRED_GENESIS_STEPS),
    }


def _receipt_evidence(
    receipt: dict[str, object],
    *,
    expected_solet_name: str | None,
) -> tuple[str | None, dict[str, JsonValue]]:
    completed, invalid = _receipt_steps(receipt.get("steps"))
    missing = sorted(set(_REQUIRED_GENESIS_STEPS) - set(completed))
    solet_name = receipt.get("solet_name")
    completed_at = receipt.get("completed_at")
    observed: dict[str, JsonValue] = {
        "solet_name": solet_name if isinstance(solet_name, str) else None,
        "expected_solet_name": expected_solet_name,
        "completed_at": completed_at if isinstance(completed_at, str) else None,
        "completed_steps": [cast(JsonValue, item) for item in sorted(completed)],
        "missing_required_steps": [cast(JsonValue, item) for item in missing],
        "invalid_steps": [cast(JsonValue, item) for item in sorted(invalid)],
    }
    return (
        _receipt_error(
            solet_name,
            expected_solet_name,
            completed_at,
            missing,
            invalid,
        ),
        observed,
    )


def _receipt_error(
    solet_name: object,
    expected_solet_name: str | None,
    completed_at: object,
    missing: list[str],
    invalid: list[str],
) -> str | None:
    if expected_solet_name is None:
        return "Target root manifest lacks one unambiguous valid solet_name."
    if not isinstance(solet_name, str) or not solet_name:
        return "Target genesis receipt lacks a non-empty solet_name."
    if solet_name != expected_solet_name:
        return "Target genesis receipt solet_name does not match the inspected target identity."
    if not isinstance(completed_at, str) or not completed_at:
        return "Target genesis receipt lacks a completion timestamp."
    if missing or invalid:
        return "Target genesis receipt does not record every required step completed."
    return None


def read_target_solet_name(root_manifest_path: Path) -> str | None:
    """Read one unquoted top-level Solet identity from a static root manifest."""
    loaded = base._read_static_text(root_manifest_path)
    if loaded.status is not base.DiagnosticStatus.VERIFIED or loaded.text is None:
        return None
    candidates = [
        line.removeprefix("solet_name:").strip()
        for line in loaded.text.splitlines()
        if line.startswith("solet_name:")
    ]
    if len(candidates) != 1 or _SOLET_NAME_PATTERN.fullmatch(candidates[0]) is None:
        return None
    return candidates[0]


def _receipt_steps(value: object) -> tuple[list[str], list[str]]:
    if not isinstance(value, list):
        return [], ["steps_not_array"]
    completed: list[str] = []
    invalid: list[str] = []
    for raw_step in value:
        if not isinstance(raw_step, dict) or not isinstance(raw_step.get("step_name"), str):
            invalid.append("malformed_step")
        elif raw_step.get("status") == "completed":
            completed.append(cast(str, raw_step["step_name"]))
        else:
            invalid.append(cast(str, raw_step["step_name"]))
    return completed, invalid


def inspect_declared_python_dependency(
    pyproject_path: Path,
    source_path: Path,
    *,
    dependency: str,
    target_available: bool | None,
    availability_evidence: JsonValue,
) -> base.DiagnosticCheck:
    """Expose guardedness, declaration, and target availability independently."""
    check_id = "doctor::seat_rotation_dependency_closure_v1"
    reason_code = "seat_rotation_dependency_missing"
    repair_code = "repair_requires_declared_dependency_plan"
    normalized = base._normalize_package_name(dependency)
    source = base._read_static_text(source_path)
    if source.status is not base.DiagnosticStatus.VERIFIED:
        return base.artifact_check(
            check_id=check_id,
            loaded=source,
            reason_code=reason_code,
            repair_code=repair_code,
            expected=dependency,
            source="static_python_source",
        )
    metadata = base._read_static_text(pyproject_path)
    if metadata.status is not base.DiagnosticStatus.VERIFIED:
        return base.artifact_check(
            check_id=check_id,
            loaded=metadata,
            reason_code=reason_code,
            repair_code=repair_code,
            expected=dependency,
            source="static_package_metadata",
        )
    if source.text is None or metadata.text is None:
        raise AssertionError("verified static read lacks text")
    imports = base.parse_imported_names(source.text, source_path.name)
    if isinstance(imports, str):
        return base.failed_check(
            check_id,
            imports,
            reason_code,
            repair_code,
            observed="invalid_python_source",
            expected=dependency,
            source="static_python_source",
        )
    all_imports, unguarded_imports = imports
    if normalized not in all_imports:
        return base.DiagnosticCheck(
            check_id=check_id,
            status=base.DiagnosticStatus.NOT_APPLICABLE,
            summary=f"The inspected helper does not import {dependency!r}.",
            reason_code=None,
            repair_code=None,
            observed={"imported": False, "dependency": dependency},
            expected=dependency,
            source="static_python_source",
        )
    requirements = base.parse_project_dependencies(metadata.text)
    if isinstance(requirements, str):
        return base.failed_check(
            check_id,
            requirements,
            reason_code,
            repair_code,
            observed="invalid_dependency_table",
            expected=dependency,
            source="static_package_metadata",
        )
    facts: dict[str, JsonValue] = {
        "imported": True,
        "import_guarded": normalized not in unguarded_imports,
        "declared_direct_dependency": normalized
        in {requirement.normalized_name for requirement in requirements},
        "dependency": dependency,
    }
    return _dependency_verdict(
        facts,
        target_available=target_available,
        availability_evidence=availability_evidence,
    )


def _dependency_verdict(
    facts: dict[str, JsonValue],
    *,
    target_available: bool | None,
    availability_evidence: JsonValue,
) -> base.DiagnosticCheck:
    check_id = "doctor::seat_rotation_dependency_closure_v1"
    dependency = cast(str, facts["dependency"])
    guarded = facts["import_guarded"] is True
    declared = facts["declared_direct_dependency"] is True
    if not guarded and not declared:
        return base.DiagnosticCheck(
            check_id=check_id,
            status=base.DiagnosticStatus.MISSING,
            summary=f"The unguarded helper import {dependency!r} is not declared directly.",
            reason_code="seat_rotation_dependency_missing",
            repair_code="repair_requires_declared_dependency_plan",
            observed={**facts, "target_available": target_available},
            expected={**facts, "declared_direct_dependency": True, "target_available": True},
            source="static_package_metadata",
        )
    if target_available is None:
        return _dependency_unprobed(facts, availability_evidence)
    if not target_available:
        return base.DiagnosticCheck(
            check_id=check_id,
            status=base.DiagnosticStatus.MISSING,
            summary=f"The selected target cannot import runtime dependency {dependency!r}.",
            reason_code="seat_rotation_dependency_missing",
            repair_code="repair_requires_declared_dependency_plan",
            observed={
                **facts,
                "target_available": False,
                "availability_evidence": availability_evidence,
                "next_action": f"Install {dependency!r} into the selected target venv.",
            },
            expected={**facts, "target_available": True},
            source="static_contract_and_target_venv",
        )
    return base.verified_check(
        check_id,
        f"The selected target can import runtime dependency {dependency!r}.",
        observed={
            **facts,
            "target_available": True,
            "availability_evidence": availability_evidence,
        },
        expected={**facts, "target_available": True},
        source="static_contract_and_target_venv",
    )


def _dependency_unprobed(
    facts: dict[str, JsonValue], availability_evidence: JsonValue
) -> base.DiagnosticCheck:
    dependency = cast(str, facts["dependency"])
    if facts["import_guarded"] is not True:
        return base.verified_check(
            "doctor::seat_rotation_dependency_closure_v1",
            f"The owning package declares imported dependency {dependency!r}.",
            observed={**facts, "target_available": None},
            expected={**facts, "target_available": None},
            source="static_package_metadata",
        )
    return base.DiagnosticCheck(
        check_id="doctor::seat_rotation_dependency_closure_v1",
        status=base.DiagnosticStatus.UNKNOWN,
        summary=f"The helper guards {dependency!r}, but target availability was not probed.",
        reason_code="dependency_requirement_indeterminate",
        repair_code="repair_requires_dependency_requirement_evidence",
        observed={
            **facts,
            "target_available": None,
            "availability_evidence": availability_evidence,
        },
        expected={**facts, "target_available": True},
        source="static_python_source",
    )
