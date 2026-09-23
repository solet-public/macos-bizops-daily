"""Disposable adversarial fixtures for passive existing-Solet diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from itertools import product
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_PACKAGE_ROOT))

import solet_manager.existing_solet_diagnostics as diagnostics  # noqa: E402
from solet_manager.diagnostic_contract_checks import (  # noqa: E402
    inspect_target_apply_manifest_binding,
)
from solet_manager.existing_solet_diagnostics import (  # noqa: E402
    DiagnosticCheck,
    DiagnosticStatus,
    inspect_apply_manifest_binding_contract,
    inspect_declared_python_dependency,
    inspect_process_input_projection,
    roll_up_active_checks,
)
from solet_manager.models import ExitCode, JsonValue  # noqa: E402

_CHECKS = 0
_CONTROLS = 0
_FAILURES: list[str] = []
_LIMIT = 1_048_576
_PUBLIC_INPUT_CONTRACT: dict[str, object] = {
    "service_bindings": {
        "type": "object",
        "description": "Manager-required service-name to provider-name bindings.",
        "additionalProperties": {"type": "string"},
    }
}
_PLUGINS_SCHEMA: dict[str, object] = {
    "type": "array",
    "items": {"type": "string"},
}
_PROFILE_NAME_SCHEMA: dict[str, object] = {"type": "string"}
_BINDING_RULE: dict[str, object] = {
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
}
_LEGAL_ROLLUP_SHAPES: tuple[
    tuple[DiagnosticStatus, str | None, str | None, str, ExitCode, int, int], ...
] = (
    (DiagnosticStatus.VERIFIED, None, None, "verified", ExitCode.OK, 1, 1),
    (
        DiagnosticStatus.MISSING,
        "fixture_reason",
        "fixture_repair",
        "incomplete",
        ExitCode.HUMAN_ACTION,
        0,
        0,
    ),
    (
        DiagnosticStatus.FAILED,
        "fixture_reason",
        "fixture_repair",
        "failed",
        ExitCode.FAILED,
        0,
        0,
    ),
    (
        DiagnosticStatus.UNKNOWN,
        "fixture_reason",
        "fixture_repair",
        "incomplete",
        ExitCode.HUMAN_ACTION,
        0,
        0,
    ),
    (DiagnosticStatus.NOT_APPLICABLE, None, None, "verified", ExitCode.OK, 0, 1),
)


def _check(condition: object, label: str, *, observed: object = None) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        _FAILURES.append(f"{label}; observed={observed!r}")


def _control(condition: object, label: str, *, observed: object = None) -> None:
    global _CONTROLS
    _CONTROLS += 1
    if not condition:
        _FAILURES.append(f"{label}; observed={observed!r}")


def _snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        snapshot[relative] = (
            "directory"
            if path.is_dir()
            else hashlib.sha256(path.read_bytes()).hexdigest()
        )
    return snapshot


def _projection_document(
    *,
    arguments_type: object = "object",
    properties: object = _PUBLIC_INPUT_CONTRACT,
    required: object = ("service_bindings",),
    additional_properties: object = False,
    description: str = "Supplied service bindings are merged.",
) -> dict[str, object]:
    return {
        "description": description,
        "arguments": {
            "type": arguments_type,
            "required": list(required) if isinstance(required, tuple) else required,
            "properties": properties,
            "additionalProperties": additional_properties,
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _binding_document(
    *,
    outer_description: str = "Supplied service bindings are merged.",
    nested_description: str = "Nested service bindings are merged.",
    new_manifest_type: object = "object",
    service_bindings_schema: object = _PUBLIC_INPUT_CONTRACT["service_bindings"],
    include_service_bindings: bool = True,
    plugins_schema: object = _PLUGINS_SCHEMA,
    include_plugins: bool = True,
    profile_name_schema: object = _PROFILE_NAME_SCHEMA,
    include_profile_name: bool = True,
    outer_required: tuple[str, ...] = ("new_manifest",),
    nested_required: tuple[str, ...] = ("plugins",),
    include_machine_looking_field: bool = False,
) -> dict[str, object]:
    nested_properties: dict[str, object] = {}
    if include_profile_name:
        nested_properties["profile_name"] = profile_name_schema
    if include_plugins:
        nested_properties["plugins"] = plugins_schema
    if include_service_bindings:
        nested_properties["service_bindings"] = service_bindings_schema
    document: dict[str, object] = {
        "description": outer_description,
        "arguments": {
            "type": "object",
            "required": list(outer_required),
            "additionalProperties": False,
            "properties": {
                "new_manifest": {
                    "type": new_manifest_type,
                    "description": nested_description,
                    "required": list(nested_required),
                    "properties": nested_properties,
                    "additionalProperties": False,
                }
            },
        },
    }
    if include_machine_looking_field:
        document["binding_rule"] = {
            "version": 1,
            "service_bindings": {"accepted": True, "merge": "replace"},
        }
    return document


def _inspect_projection(path: Path) -> DiagnosticCheck:
    return inspect_process_input_projection(
        path,
        required_public_inputs=_PUBLIC_INPUT_CONTRACT,
    )


def _with_regular_fstat_trigger(
    inspect: Callable[[], DiagnosticCheck],
    trigger: Callable[[], None],
) -> tuple[DiagnosticCheck, bool]:
    original_fstat = diagnostics.os.fstat
    triggered = False

    def triggering_fstat(descriptor: int) -> os.stat_result:
        nonlocal triggered
        metadata = original_fstat(descriptor)
        if not triggered and stat.S_ISREG(metadata.st_mode):
            triggered = True
            trigger()
        return metadata

    diagnostics.os.fstat = triggering_fstat
    try:
        return inspect(), triggered
    finally:
        diagnostics.os.fstat = original_fstat


def _static_reader_checks() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        stable_root = root / "stable"
        stable_root.mkdir()
        stable = stable_root / "schema.json"
        _write_json(stable, _projection_document())
        before = _snapshot(stable_root)
        stable_result = _inspect_projection(stable)
        after = _snapshot(stable_root)
        _check(
            stable_result.status is DiagnosticStatus.VERIFIED,
            "stable ordinary static artifact verifies",
            observed=stable_result.status,
        )
        _check(before == after, "stable passive read changes no target byte", observed=after)

        final_link = root / "final-link.json"
        final_link.symlink_to(stable)
        final_link_result = _inspect_projection(final_link)
        _check(
            final_link_result.status is DiagnosticStatus.FAILED,
            "final-component symlink fails closed",
            observed=final_link_result.status,
        )

        outside = root / "outside"
        outside.mkdir()
        _write_json(outside / "schema.json", _projection_document())
        parent_link = root / "parent-link"
        parent_link.symlink_to(outside, target_is_directory=True)
        parent_link_result = _inspect_projection(parent_link / "schema.json")
        _check(
            parent_link_result.status is DiagnosticStatus.FAILED,
            "parent-component symlink fails closed",
            observed=parent_link_result.status,
        )

        fifo = root / "schema.fifo"
        os.mkfifo(fifo)
        try:
            fifo_probe = subprocess.run(
                [sys.executable, __file__, "--probe-process-input", str(fifo)],
                capture_output=True,
                check=False,
                text=True,
                timeout=1.0,
            )
            fifo_observed: object = (fifo_probe.returncode, fifo_probe.stdout.strip())
            fifo_failed_closed = (
                fifo_probe.returncode == 0
                and fifo_probe.stdout.strip() == DiagnosticStatus.FAILED.value
            )
        except subprocess.TimeoutExpired:
            fifo_observed = "timed_out_after_1.0_seconds"
            fifo_failed_closed = False
        _check(
            fifo_failed_closed,
            "FIFO inspection returns failed within the finite deadline",
            observed=fifo_observed,
        )

        oversize = root / "oversize.json"
        oversize.write_bytes(b" " * (_LIMIT + 1))
        oversize_result = _inspect_projection(oversize)
        _check(
            oversize_result.status is DiagnosticStatus.FAILED,
            "pre-existing oversize artifact fails closed",
            observed=oversize_result.status,
        )

        growing = root / "growing.json"
        _write_json(growing, _projection_document())

        def grow_after_check() -> None:
            with growing.open("ab") as handle:
                handle.write(b" " * (_LIMIT + 249))

        growing_result, growth_triggered = _with_regular_fstat_trigger(
            lambda: _inspect_projection(growing),
            grow_after_check,
        )
        _check(growth_triggered, "post-check growth trigger executed")
        _check(
            growing_result.status is DiagnosticStatus.FAILED,
            "post-check growth beyond one MiB fails closed",
            observed=(growing_result.status, growing.stat().st_size),
        )

        raced = root / "raced.json"
        replacement = root / "replacement.json"
        _write_json(raced, _projection_document())
        _write_json(replacement, _projection_document(description="Replacement schema."))

        def replace_after_check() -> None:
            os.replace(replacement, raced)

        raced_result, replacement_triggered = _with_regular_fstat_trigger(
            lambda: _inspect_projection(raced),
            replace_after_check,
        )
        _check(replacement_triggered, "descriptor replacement trigger executed")
        _check(
            raced_result.status is DiagnosticStatus.FAILED,
            "final-component replacement during read fails closed",
            observed=raced_result.status,
        )

        non_utf8 = root / "non-utf8.json"
        non_utf8.write_bytes(b"\xff\xfe")
        non_utf8_result = _inspect_projection(non_utf8)
        _check(
            non_utf8_result.status is DiagnosticStatus.FAILED,
            "non-UTF-8 artifact fails closed",
            observed=non_utf8_result.status,
        )


def _process_projection_checks() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        cases: tuple[tuple[str, object, DiagnosticStatus], ...] = (
            ("nominal", _projection_document(), DiagnosticStatus.VERIFIED),
            (
                "wrong-arguments-type",
                _projection_document(arguments_type="string"),
                DiagnosticStatus.FAILED,
            ),
            (
                "missing-properties",
                _projection_document(properties=None),
                DiagnosticStatus.FAILED,
            ),
            (
                "open-arguments",
                _projection_document(additional_properties=True),
                DiagnosticStatus.FAILED,
            ),
            (
                "null-required-property",
                _projection_document(properties={"service_bindings": None}),
                DiagnosticStatus.FAILED,
            ),
            (
                "malformed-required-property",
                _projection_document(
                    properties={"service_bindings": {"type": "array"}}
                ),
                DiagnosticStatus.FAILED,
            ),
            (
                "extra-required-name",
                _projection_document(required=("service_bindings", "ghost")),
                DiagnosticStatus.FAILED,
            ),
        )
        for name, document, expected_status in cases:
            path = root / f"{name}.json"
            _write_json(path, document)
            result = _inspect_projection(path)
            _check(
                result.status is expected_status,
                f"Dax #36 {name} projection has the closed-contract status",
                observed=result.status,
            )
            if expected_status is DiagnosticStatus.FAILED:
                _check(
                    result.reason_code == "process_input_projection_mismatch"
                    and result.repair_code == "repair_refused_product_schema_owner",
                    f"Dax #36 {name} preserves public reason and repair codes",
                    observed=(result.reason_code, result.repair_code),
                )

        invalid = root / "invalid.json"
        invalid.write_text("{", encoding="utf-8")
        invalid_result = _inspect_projection(invalid)
        _check(
            invalid_result.status is DiagnosticStatus.FAILED,
            "Dax #36 invalid JSON fails closed",
            observed=invalid_result.status,
        )

        for name, token in (
            ("nan", "NaN"),
            ("positive-infinity", "Infinity"),
            ("negative-infinity", "-Infinity"),
        ):
            path = root / f"non-finite-{name}.json"
            nominal = json.dumps(_projection_document(), sort_keys=True)
            path.write_text(f'{nominal[:-1]}, "unused": {token}}}', encoding="utf-8")
            result = _inspect_projection(path)
            _check(
                result.status is DiagnosticStatus.FAILED,
                f"Dax #36 unused {token} JSON constant fails closed",
                observed=(result.status, result.reason_code, result.repair_code),
            )

        in_memory_schema = root / "in-memory-contract.json"
        _write_json(in_memory_schema, _projection_document())
        for name, value in (
            ("nan", float("nan")),
            ("positive-infinity", float("inf")),
            ("negative-infinity", float("-inf")),
        ):
            expected_contract: dict[str, object] = {
                "service_bindings": {
                    **_PUBLIC_INPUT_CONTRACT["service_bindings"],
                    "unused": {"nested": [value]},
                }
            }
            try:
                observed: object = inspect_process_input_projection(
                    in_memory_schema,
                    required_public_inputs=expected_contract,
                )
            except ValueError as exc:
                rejected = True
                observed = str(exc)
            else:
                rejected = False
            _check(
                rejected,
                f"Dax #36 caller-supplied nested {name} value is rejected",
                observed=observed,
            )


def _binding_contract_case_checks(
    cases: tuple[tuple[str, dict[str, object], str], ...],
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        for name, document, expected_nested_payload in cases:
            path = root / f"{name}.json"
            _write_json(path, document)
            result = inspect_apply_manifest_binding_contract(path)
            _check(
                result.status is DiagnosticStatus.FAILED,
                f"Dax #37 {name} cannot verify without a released machine rule",
                observed=result.status,
            )
            _check(
                result.reason_code == "apply_manifest_binding_contract_conflict"
                and result.repair_code == "repair_refused_contract_metadata_owner",
                f"Dax #37 {name} preserves public reason and repair codes",
                observed=(result.reason_code, result.repair_code),
            )
            nested_payload = (
                result.observed.get("nested_payload")
                if isinstance(result.observed, dict)
                else None
            )
            _check(
                nested_payload == expected_nested_payload,
                f"Dax #37 {name} exposes the non-authoritative nested classification",
                observed=(
                    result.status.value,
                    result.reason_code,
                    result.repair_code,
                    nested_payload,
                ),
            )


def _binding_contract_checks() -> None:
    _binding_contract_case_checks(
        (
            (
                "negative-prose",
                _binding_document(
                    outer_description="Bindings are not merged.",
                    nested_description="Bindings are never merged.",
                ),
                "valid",
            ),
            (
                "wrong-new-manifest-type",
                _binding_document(new_manifest_type="string"),
                "invalid",
            ),
            (
                "missing-nested-service-bindings",
                _binding_document(include_service_bindings=False),
                "invalid",
            ),
            (
                "malformed-binding-schema",
                _binding_document(service_bindings_schema=None),
                "invalid",
            ),
            ("prose-only-rule", _binding_document(), "valid"),
            (
                "missing-plugins",
                _binding_document(include_plugins=False),
                "invalid",
            ),
            (
                "wrong-plugins-definition",
                _binding_document(plugins_schema={"type": "string"}),
                "invalid",
            ),
            (
                "plugins-not-required",
                _binding_document(nested_required=()),
                "invalid",
            ),
            (
                "new-manifest-not-required",
                _binding_document(outer_required=()),
                "invalid",
            ),
            (
                "wrong-profile-name-definition",
                _binding_document(profile_name_schema={"type": "array"}),
                "invalid",
            ),
            (
                "absent-optional-profile-name",
                _binding_document(include_profile_name=False),
                "valid",
            ),
            (
                "null-profile-name-definition",
                _binding_document(profile_name_schema=None),
                "invalid",
            ),
            (
                "undeclared-outer-required-property",
                _binding_document(outer_required=("new_manifest", "ghost")),
                "invalid",
            ),
            (
                "undeclared-nested-required-property",
                _binding_document(nested_required=("plugins", "ghost")),
                "invalid",
            ),
            ("nominal-synthetic-shape", _binding_document(), "valid"),
            (
                "adversarial-machine-looking-field",
                _binding_document(include_machine_looking_field=True),
                "valid",
            ),
        )
    )


def _target_binding_evidence_controls() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        schema = root / "apply_manifest.json"
        receipt = root / "genesis.json"
        contract = _binding_document()
        contract["binding_rule"] = _BINDING_RULE
        _write_json(schema, contract)
        required_steps = (
            "validate_name",
            "resolve_target",
            "materialize_configs",
            "seed_root_manifest",
            "materialize_kb_symlinks",
            "write_manifest_marker",
        )
        valid_receipt: dict[str, object] = {
            "solet_name": "fixture-solet",
            "completed_at": "2026-08-25T05:21:24+00:00",
            "steps": [
                {"step_name": name, "status": "completed"}
                for name in required_steps
            ],
        }
        _write_json(receipt, valid_receipt)
        valid = inspect_target_apply_manifest_binding(
            schema,
            receipt,
            expected_solet_name="fixture-solet",
        )
        _control(
            valid.status is DiagnosticStatus.VERIFIED,
            "target binding requires a valid released rule and matching completed receipt",
            observed=valid,
        )

        wrong_name = dict(valid_receipt)
        wrong_name["solet_name"] = "different-solet"
        _write_json(receipt, wrong_name)
        mismatch = inspect_target_apply_manifest_binding(
            schema,
            receipt,
            expected_solet_name="fixture-solet",
        )
        _control(
            mismatch.status is DiagnosticStatus.FAILED
            and isinstance(mismatch.observed, dict)
            and mismatch.observed["target_receipt"]["solet_name"] == "different-solet",
            "wrong receipt identity is negative and remains visible",
            observed=mismatch,
        )

        incomplete = dict(valid_receipt)
        incomplete["steps"] = valid_receipt["steps"][:-1]
        _write_json(receipt, incomplete)
        incomplete_result = inspect_target_apply_manifest_binding(
            schema,
            receipt,
            expected_solet_name="fixture-solet",
        )
        _control(
            incomplete_result.status is DiagnosticStatus.FAILED,
            "incomplete receipt steps cannot verify materialization",
            observed=incomplete_result,
        )

        _write_json(receipt, valid_receipt)
        invalid_contract = _binding_document()
        _write_json(schema, invalid_contract)
        invalid = inspect_target_apply_manifest_binding(
            schema,
            receipt,
            expected_solet_name="fixture-solet",
        )
        _control(
            invalid.status is DiagnosticStatus.FAILED
            and isinstance(invalid.observed, dict)
            and invalid.observed.get("target_receipt_valid") is True
            and isinstance(invalid.observed.get("target_receipt"), dict),
            "invalid release metadata cannot be hidden by or erase valid receipt evidence",
            observed=invalid,
        )
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw).resolve() / "released-rule.json"
        document = _binding_document()
        document["binding_rule"] = _BINDING_RULE
        _write_json(path, document)
        result = inspect_apply_manifest_binding_contract(path)
        _check(
            result.status is DiagnosticStatus.VERIFIED,
            "Dax #37 exact released machine rule reaches the verified branch",
            observed=result,
        )


def _binding_null_profile_name_checks() -> None:
    _binding_contract_case_checks(
        (("null-profile-name-definition", _binding_document(profile_name_schema=None), "invalid"),)
    )


def _binding_undeclared_outer_required_checks() -> None:
    _binding_contract_case_checks(
        (
            (
                "undeclared-outer-required-property",
                _binding_document(outer_required=("new_manifest", "ghost")),
                "invalid",
            ),
        )
    )


def _binding_undeclared_nested_required_checks() -> None:
    _binding_contract_case_checks(
        (
            (
                "undeclared-nested-required-property",
                _binding_document(nested_required=("plugins", "ghost")),
                "invalid",
            ),
        )
    )


def _write_pyproject(
    path: Path,
    *,
    dependencies: tuple[str, ...],
    optional_dependencies: tuple[str, ...] = (),
) -> None:
    lines = [
        "[project]",
        'name = "agent-messaging-plugin"',
        'version = "1.0.0"',
        f"dependencies = {json.dumps(dependencies)}",
    ]
    if optional_dependencies:
        lines.extend(
            (
                "",
                "[project.optional-dependencies]",
                f"seat-rotation = {json.dumps(optional_dependencies)}",
            )
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _dependency_checks() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        sentinel = root / "TARGET_CODE_EXECUTED"
        source = root / "seat_rotation_helper.py"
        source.write_text(
            "\n".join(
                (
                    "import iterm2",
                    "from pathlib import Path",
                    f"Path({str(sentinel)!r}).write_text('unsafe')",
                    "",
                )
            ),
            encoding="utf-8",
        )
        cases: tuple[tuple[str, tuple[str, ...], tuple[str, ...], DiagnosticStatus], ...] = (
            ("missing", (), (), DiagnosticStatus.MISSING),
            ("malformed", ("iterm2 !!!",), (), DiagnosticStatus.FAILED),
            (
                "malformed-after-valid",
                ("iterm2>=2.9", "broken !!!"),
                (),
                DiagnosticStatus.FAILED,
            ),
            (
                "valid-version-marker",
                ('iTerm2>=2.9; python_version >= "3.13"',),
                (),
                DiagnosticStatus.VERIFIED,
            ),
            (
                "selected-extra-only",
                (),
                ("iterm2>=2.9",),
                DiagnosticStatus.MISSING,
            ),
        )
        before = _snapshot(root)
        for name, dependencies, optional, expected_status in cases:
            metadata = root / f"{name}.toml"
            _write_pyproject(
                metadata,
                dependencies=dependencies,
                optional_dependencies=optional,
            )
            result = inspect_declared_python_dependency(
                metadata,
                source,
                dependency="iterm2",
            )
            _check(
                result.status is expected_status,
                f"Dax #39 {name} dependency evidence has the strict PEP 508 status",
                observed=result.status,
            )
        after = _snapshot(root)
        expected_after = dict(after)
        for name, _, _, _ in cases:
            expected_after.pop(f"{name}.toml")
        _check(
            before == expected_after,
            "dependency diagnostics change no pre-existing target byte",
            observed=expected_after,
        )
        _check(not sentinel.exists(), "dependency inspection executes no target code")


def _diagnostic(check_id: str, status: DiagnosticStatus) -> DiagnosticCheck:
    shape = next(item for item in _LEGAL_ROLLUP_SHAPES if item[0] is status)
    _, reason_code, repair_code, _, _, _, _ = shape
    if status is DiagnosticStatus.NOT_APPLICABLE:
        return DiagnosticCheck(
            check_id="doctor::seat_rotation_dependency_closure_v1",
            status=status,
            summary="static import prerequisite is absent",
            reason_code=None,
            repair_code=None,
            observed={"imported": False, "dependency": "iterm2"},
            expected="iterm2",
            source="static_python_source",
        )
    return DiagnosticCheck(
        check_id=check_id,
        status=status,
        summary=f"{status.value} fixture",
        reason_code=reason_code,
        repair_code=repair_code,
        observed=status.value,
        expected=DiagnosticStatus.VERIFIED.value,
        source="fixture",
    )


def _expected_rollup_outcome(
    case: tuple[DiagnosticStatus, ...],
) -> tuple[str, ExitCode]:
    if DiagnosticStatus.FAILED in case:
        return "failed", ExitCode.FAILED
    if set(case) & {DiagnosticStatus.MISSING, DiagnosticStatus.UNKNOWN}:
        return "incomplete", ExitCode.HUMAN_ACTION
    return "verified", ExitCode.OK


def _matrix_case_matches(
    case: tuple[DiagnosticStatus, ...],
    rollup: diagnostics.DiagnosticDoctorRollup,
) -> bool:
    expected_status, expected_exit = _expected_rollup_outcome(case)
    expected_summary = {
        status.value: case.count(status) for status in DiagnosticStatus
    }
    return (
        rollup.status == expected_status
        and rollup.exit_code is expected_exit
        and rollup.verified_count == case.count(DiagnosticStatus.VERIFIED)
        and rollup.satisfied_count
        == case.count(DiagnosticStatus.VERIFIED)
        + case.count(DiagnosticStatus.NOT_APPLICABLE)
        and rollup.total_count == len(case)
        and rollup.summary == expected_summary
        and rollup == roll_up_active_checks(rollup.checks)
    )


def _rollup_matrix_checks() -> None:
    statuses = tuple(shape[0] for shape in _LEGAL_ROLLUP_SHAPES)
    matrix_cases = 0
    for width in (1, 2, 3):
        for case_index, case in enumerate(product(statuses, repeat=width)):
            if case.count(DiagnosticStatus.NOT_APPLICABLE) > 1:
                continue
            matrix_cases += 1
            checks = tuple(
                _diagnostic(f"matrix-{width}-{case_index}-{index}", status)
                for index, status in enumerate(case)
            )
            rollup = roll_up_active_checks(checks)
            _check(
                _matrix_case_matches(case, rollup),
                f"rollup legal matrix case {width}/{case_index} preserves union semantics",
                observed=rollup,
            )
    _check(matrix_cases == 141, "rollup matrix enumerates exactly 141 legal cases")


def _assert_rollup_rejected(check: DiagnosticCheck, label: str) -> None:
    try:
        observed: object = roll_up_active_checks((check,))
    except ValueError as exc:
        rejected = True
        observed = str(exc)
    else:
        rejected = False
    _check(rejected, label, observed=observed)


def _not_applicable_fixture(
    *,
    check_id: str = "doctor::seat_rotation_dependency_closure_v1",
    observed: JsonValue,
    expected: JsonValue,
    source: str = "static_python_source",
) -> DiagnosticCheck:
    return DiagnosticCheck(
        check_id=check_id,
        status=DiagnosticStatus.NOT_APPLICABLE,
        summary="adversarial prerequisite fixture",
        reason_code=None,
        repair_code=None,
        observed=observed,
        expected=expected,
        source=source,
    )


def _rollup_rejection_checks() -> None:
    for dependency in (None, {"name": "iterm2"}, "", "!!!"):
        malformed_dependency: JsonValue = dependency
        _assert_rollup_rejected(
            _not_applicable_fixture(
                observed={"imported": False, "dependency": malformed_dependency},
                expected=malformed_dependency,
            ),
            f"not_applicable malformed dependency {dependency!r} is rejected",
        )

    _assert_rollup_rejected(
        DiagnosticCheck(
            check_id="verified-with-codes",
            status=DiagnosticStatus.VERIFIED,
            summary="invalid verified union",
            reason_code="unexpected_reason",
            repair_code="unexpected_repair",
            observed="verified",
            expected="verified",
            source="fixture",
        ),
        "verified row carrying failure codes is rejected",
    )
    _assert_rollup_rejected(
        DiagnosticCheck(
            check_id="failed-without-codes",
            status=DiagnosticStatus.FAILED,
            summary="invalid failed union",
            reason_code=None,
            repair_code=None,
            observed="failed",
            expected="verified",
            source="fixture",
        ),
        "failed row omitting required codes is rejected",
    )

    near_valid_mutations: tuple[tuple[str, str, JsonValue, JsonValue, str], ...] = (
        (
            "wrong-check-id",
            "doctor::other_dependency_closure_v1",
            {"imported": False, "dependency": "iterm2"},
            "iterm2",
            "static_python_source",
        ),
        (
            "wrong-source",
            "doctor::seat_rotation_dependency_closure_v1",
            {"imported": False, "dependency": "iterm2"},
            "iterm2",
            "static_package_metadata",
        ),
        (
            "observed-expected-cross-pair",
            "doctor::seat_rotation_dependency_closure_v1",
            {"imported": False, "dependency": "requests"},
            "iterm2",
            "static_python_source",
        ),
        (
            "imported-flag-cross-pair",
            "doctor::seat_rotation_dependency_closure_v1",
            {"imported": True, "dependency": "iterm2"},
            "iterm2",
            "static_python_source",
        ),
    )
    for label, check_id, observed, expected, source in near_valid_mutations:
        _assert_rollup_rejected(
            _not_applicable_fixture(
                check_id=check_id,
                observed=observed,
                expected=expected,
                source=source,
            ),
            f"not_applicable near-valid mutation {label} is rejected",
        )


def _rollup_checks() -> None:
    for index, shape in enumerate(_LEGAL_ROLLUP_SHAPES):
        status, _, _, expected_status, expected_exit, verified, satisfied = shape
        rollup = roll_up_active_checks((_diagnostic(f"isolated-{index}", status),))
        _check(
            (
                rollup.status,
                rollup.exit_code,
                rollup.verified_count,
                getattr(rollup, "satisfied_count", None),
            )
            == (expected_status, expected_exit, verified, satisfied),
            f"isolated {status.value} rollup preserves status/count semantics",
            observed=rollup,
        )
        _check(
            getattr(rollup, "total_count", None) == 1,
            f"isolated {status.value} rollup exposes its total",
            observed=rollup,
        )

    _rollup_matrix_checks()

    mixed = roll_up_active_checks(
        (
            _diagnostic("mixed-verified", DiagnosticStatus.VERIFIED),
            _diagnostic("mixed-na", DiagnosticStatus.NOT_APPLICABLE),
        )
    )
    _check(
        (
            mixed.status,
            mixed.exit_code,
            mixed.verified_count,
            getattr(mixed, "satisfied_count", None),
            getattr(mixed, "total_count", None),
        )
        == ("verified", ExitCode.OK, 1, 2, 2),
        "mixed verified/not-applicable rollup separates verified and satisfied",
        observed=mixed,
    )
    _check(
        getattr(mixed, "summary", None)
        == {
            "verified": 1,
            "missing": 0,
            "failed": 0,
            "unknown": 0,
            "not_applicable": 1,
        },
        "rollup summary exposes exact five-bucket totals",
        observed=getattr(mixed, "summary", None),
    )

    incomplete = roll_up_active_checks(
        (
            _diagnostic("incomplete-verified", DiagnosticStatus.VERIFIED),
            _diagnostic("incomplete-missing", DiagnosticStatus.MISSING),
            _diagnostic("incomplete-unknown", DiagnosticStatus.UNKNOWN),
        )
    )
    _check(
        incomplete.status == "incomplete"
        and incomplete.exit_code is ExitCode.HUMAN_ACTION,
        "missing/unknown take precedence over satisfied checks",
        observed=incomplete,
    )
    failed = roll_up_active_checks(
        (
            _diagnostic("failed-missing", DiagnosticStatus.MISSING),
            _diagnostic("failed-failed", DiagnosticStatus.FAILED),
            _diagnostic("failed-unknown", DiagnosticStatus.UNKNOWN),
        )
    )
    _check(
        failed.status == "failed" and failed.exit_code is ExitCode.FAILED,
        "failed takes precedence over missing and unknown",
        observed=failed,
    )
    _check(
        mixed == roll_up_active_checks(mixed.checks),
        "rollup is idempotent on unchanged checks",
        observed=mixed,
    )
    duplicate = _diagnostic("duplicate", DiagnosticStatus.VERIFIED)
    try:
        roll_up_active_checks((duplicate, duplicate))
    except ValueError as exc:
        duplicate_observed: object = str(exc)
        duplicate_rejected = str(exc) == "diagnostic doctor check ids must be unique"
    else:
        duplicate_observed = "accepted"
        duplicate_rejected = False
    _check(
        duplicate_rejected,
        "duplicate diagnostic IDs are rejected with the stable error",
        observed=duplicate_observed,
    )
    _rollup_rejection_checks()


def _run_probe(path: Path) -> int:
    print(_inspect_projection(path).status.value)
    return 0


def main(selected: str | None = None) -> int:
    suites: dict[str, Callable[[], None]] = {
        "static-reader": _static_reader_checks,
        "dax-36": _process_projection_checks,
        "dax-37": _binding_contract_checks,
        "dax-37-null-profile-name": _binding_null_profile_name_checks,
        "dax-37-undeclared-outer-required": _binding_undeclared_outer_required_checks,
        "dax-37-undeclared-nested-required": _binding_undeclared_nested_required_checks,
        "dax-39": _dependency_checks,
        "rollup": _rollup_checks,
    }
    if selected is None:
        for suite_name in ("static-reader", "dax-36", "dax-37", "dax-39", "rollup"):
            suites[suite_name]()
        _target_binding_evidence_controls()
    else:
        suite = suites.get(selected)
        if suite is None:
            raise ValueError(f"unknown finding suite: {selected}")
        suite()
    if _FAILURES:
        print(f"existing_solet_diagnostic_smoke FAILED: {len(_FAILURES)}/{_CHECKS}")
        for failure in _FAILURES:
            print(f"- {failure}")
        return 1
    print(f"existing_solet_diagnostic_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe-process-input":
        sys.exit(_run_probe(Path(sys.argv[2])))
    if len(sys.argv) == 3 and sys.argv[1] == "--finding":
        sys.exit(main(sys.argv[2]))
    if len(sys.argv) != 1:
        raise SystemExit("usage: existing_solet_diagnostic_smoke.py [--finding NAME]")
    sys.exit(main())
