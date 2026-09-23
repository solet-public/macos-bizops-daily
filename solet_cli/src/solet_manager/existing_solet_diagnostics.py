"""Passive diagnostics for existing Solet product-contract failures.

Security and authority invariants:

* target-local Python is parsed as data and is never imported or executed;
* every filesystem component is opened descriptor-relatively without following
  symbolic links, and remains pinned until its stability check completes;
* static reads consume no more than one MiB plus one oversize sentinel byte;
* a file, parent, directory entry, or relevant metadata change fails closed;
* process inputs are compared with release-supplied property definitions rather
  than inferred from names or descriptions;
* binding prose cannot establish a machine contract;
* every direct dependency row must parse completely as PEP 508 before any
  normalized distribution name can satisfy the declaration check;
* ``not_applicable`` may satisfy a proven prerequisite but never increments the
  count whose public name is ``verified_count``.

The module remains lower-level and non-persisting. It exposes no target repair,
rollback, public CLI registration, package installation, or lifecycle mutation.
"""

from __future__ import annotations

import ast
import json
import math
import os
import re
import stat
import tomllib
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from packaging.requirements import InvalidRequirement, Requirement

from .models import ExitCode, JsonValue

_MAX_STATIC_ARTIFACT_BYTES = 1_048_576
_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

# The diagnostic boundary is deliberately narrower than the Manager's eventual
# public doctor surface. Expected input definitions arrive from a selected
# release contract; this module never guesses missing product schema.
# Binding prose is likewise non-authoritative: until released metadata carries
# a closed machine rule, the binding check can only fail closed.


class DiagnosticStatus(StrEnum):
    """Closed passive-check status vocabulary."""

    VERIFIED = "verified"
    MISSING = "missing"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class DiagnosticCheck:
    """Public, secret-safe result of one passive existing-install check."""

    check_id: str
    status: DiagnosticStatus
    summary: str
    reason_code: str | None
    repair_code: str | None
    observed: JsonValue
    expected: JsonValue
    source: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "summary": self.summary,
            "reason_code": self.reason_code,
            "repair_code": self.repair_code,
            "observed": self.observed,
            "expected": self.expected,
            "source": self.source,
        }


@dataclass(frozen=True)
class DiagnosticDoctorRollup:
    """Private rollup input for a future closed public result constructor."""

    status: str
    exit_code: ExitCode
    checks: tuple[DiagnosticCheck, ...]
    verified_count: int
    satisfied_count: int
    total_count: int
    summary: dict[str, int]


@dataclass(frozen=True)
class _StaticRead:
    status: DiagnosticStatus
    text: str | None
    summary: str


@dataclass(frozen=True)
class _PinnedEntry:
    parent_descriptor: int | None
    name: str | None
    descriptor: int
    metadata: os.stat_result


@dataclass(frozen=True)
class _ParsedRequirement:
    normalized_name: str
    specifier: str
    marker: str | None
    extras: tuple[str, ...]
    url: str | None


def inspect_process_input_projection(
    schema_path: Path | Mapping[str, object],
    *,
    required_public_inputs: Mapping[str, object],
    source: str = "static_process_schema",
) -> DiagnosticCheck:
    """Compatibility surface for the focused closed-contract implementation."""
    from .diagnostic_contract_checks import inspect_process_input_projection as inspect

    return inspect(
        schema_path,
        required_public_inputs=required_public_inputs,
        source=source,
    )


def inspect_declared_platform_probe_ref(
    setup_contract_path: Path,
    *,
    probe_name: str,
) -> str | DiagnosticCheck:
    """Compatibility surface for target setup-contract selection."""
    from .diagnostic_contract_checks import inspect_declared_platform_probe_ref as inspect

    return inspect(setup_contract_path, probe_name=probe_name)


def inspect_apply_manifest_binding_contract(
    schema_path: Path,
) -> DiagnosticCheck:
    """Compatibility surface for the released binding-contract verifier."""
    from .diagnostic_contract_checks import inspect_apply_manifest_binding_contract as inspect

    return inspect(schema_path)


def inspect_declared_python_dependency(
    pyproject_path: Path,
    source_path: Path,
    *,
    dependency: str,
    target_available: bool | None = None,
    availability_evidence: JsonValue = None,
) -> DiagnosticCheck:
    """Compatibility surface for static and target-venv dependency evidence."""
    from .diagnostic_contract_checks import inspect_declared_python_dependency as inspect

    return inspect(
        pyproject_path,
        source_path,
        dependency=dependency,
        target_available=target_available,
        availability_evidence=availability_evidence,
    )


def roll_up_active_checks(
    checks: tuple[DiagnosticCheck, ...],
) -> DiagnosticDoctorRollup:
    """Roll up checks that may include bounded execution of target-local probes."""
    _validate_rollup_checks(checks)
    status, exit_code = _doctor_rollup(checks)
    verified_count = sum(
        check.status is DiagnosticStatus.VERIFIED for check in checks
    )
    satisfied_count = sum(_check_is_satisfied(check) for check in checks)
    summary = {
        status.value: sum(check.status is status for check in checks)
        for status in DiagnosticStatus
    }
    return DiagnosticDoctorRollup(
        status=status,
        exit_code=exit_code,
        checks=checks,
        verified_count=verified_count,
        satisfied_count=satisfied_count,
        total_count=len(checks),
        summary=summary,
    )


def _validate_rollup_checks(checks: tuple[DiagnosticCheck, ...]) -> None:
    if not checks:
        raise ValueError("diagnostic doctor requires at least one check")
    check_ids = [check.check_id for check in checks]
    if len(check_ids) != len(set(check_ids)):
        raise ValueError("diagnostic doctor check ids must be unique")
    if any(not _status_code_shape_is_valid(check) for check in checks):
        raise ValueError("diagnostic check has an invalid status/code shape")
    if any(
        check.status is DiagnosticStatus.NOT_APPLICABLE
        and not _not_applicable_prerequisite_is_valid(check)
        for check in checks
    ):
        raise ValueError("not_applicable check lacks its closed verified prerequisite")


def _validated_public_input_contract(
    values: Mapping[str, object],
) -> dict[str, JsonValue]:
    validated: dict[str, JsonValue] = {}
    for name, definition in values.items():
        if re.fullmatch(r"[a-z][a-z0-9_]{1,127}", name) is None:
            raise ValueError(f"invalid public input identifier: {name!r}")
        if not isinstance(definition, dict):
            raise ValueError(f"public input definition must be an object: {name!r}")
        typed_definition = cast(dict[str, object], definition)
        if not _is_json_object(typed_definition):
            raise ValueError(f"public input definition must contain JSON values: {name!r}")
        validated[name] = cast(dict[str, JsonValue], dict(typed_definition))
    return validated


def _is_json_object(value: dict[str, object]) -> bool:
    return all(_is_json_value(item) for item in value.values())


def _is_json_value(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if value is None or isinstance(value, bool | int | str):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return _is_json_object(cast(dict[str, object], value))
    return False


def _required_names(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        return None
    names = cast(list[str], value)
    if len(names) != len(set(names)):
        return None
    return sorted(names)


def _projection_evidence(
    arguments: dict[str, object],
    properties: dict[str, object],
    expected_inputs: dict[str, JsonValue],
    expected_names: list[str],
    expected_names_json: list[JsonValue],
) -> tuple[dict[str, JsonValue], dict[str, JsonValue], bool]:
    declared = sorted(properties)
    required = _required_names(arguments.get("required"))
    missing = sorted(set(expected_names) - set(declared))
    mismatched = sorted(
        name
        for name, definition in expected_inputs.items()
        if properties.get(name) != definition
    )
    observed: dict[str, JsonValue] = {
        "arguments_type": cast(JsonValue, arguments.get("type")),
        "declared_inputs": _json_names(declared),
        "required_inputs": None if required is None else _json_names(required),
        "missing_inputs": _json_names(missing),
        "mismatched_input_definitions": _json_names(mismatched),
        "additional_properties": cast(JsonValue, arguments.get("additionalProperties")),
    }
    expected: dict[str, JsonValue] = {
        "arguments_type": "object",
        "required_inputs": expected_names_json,
        "additional_properties": False,
    }
    valid = all(
        (
            arguments.get("type") == "object",
            arguments.get("additionalProperties") is False,
            required == expected_names,
            not missing,
            not mismatched,
        )
    )
    return observed, expected, valid


def _json_names(values: list[str]) -> list[JsonValue]:
    return [cast(JsonValue, value) for value in values]


def _binding_payload_error(value: dict[str, object]) -> str | None:
    arguments = _closed_object_schema(value.get("arguments"), "arguments")
    if isinstance(arguments, str):
        return arguments
    required_error = _required_property_error(arguments, "new_manifest", "nested")
    if required_error is not None:
        return required_error
    properties = cast(dict[str, object], arguments["properties"])
    new_manifest = _closed_object_schema(properties.get("new_manifest"), "new_manifest")
    if isinstance(new_manifest, str):
        return new_manifest
    required_error = _required_property_error(new_manifest, "plugins", "nested")
    if required_error is not None:
        return required_error
    nested_properties = cast(dict[str, object], new_manifest["properties"])
    return _binding_nested_properties_error(nested_properties)


def _required_property_error(
    schema: dict[str, object],
    property_name: str,
    qualifier: str,
) -> str | None:
    required = _required_names(schema.get("required"))
    if required is None or property_name not in required:
        return f"Process schema does not require the {qualifier} {property_name} property."
    properties = cast(dict[str, object], schema["properties"])
    undeclared = sorted(set(required) - set(properties))
    if undeclared:
        names = ", ".join(undeclared)
        return f"Process schema requires undeclared {qualifier} properties: {names}."
    return None


def _binding_nested_properties_error(
    nested_properties: dict[str, object],
) -> str | None:
    if nested_properties.get("plugins") != {
        "type": "array",
        "items": {"type": "string"},
    }:
        return "Process schema has a malformed nested plugins definition."
    if (
        "profile_name" in nested_properties
        and nested_properties["profile_name"] != {"type": "string"}
    ):
        return "Process schema has a malformed nested profile_name definition."
    service_bindings = nested_properties.get("service_bindings")
    if not isinstance(service_bindings, dict):
        return "Process schema lacks the nested service_bindings object."
    typed_bindings = cast(dict[str, object], service_bindings)
    if (
        typed_bindings.get("type") != "object"
        or typed_bindings.get("additionalProperties") != {"type": "string"}
    ):
        return "Process schema has a malformed nested service_bindings definition."
    return None


def _closed_object_schema(value: object, label: str) -> dict[str, object] | str:
    if not isinstance(value, dict):
        return f"Process schema lacks the {label} object."
    typed = cast(dict[str, object], value)
    if typed.get("type") != "object" or typed.get("additionalProperties") is not False:
        return f"Process schema {label} is not a closed object."
    properties = typed.get("properties")
    if not isinstance(properties, dict):
        return f"Process schema lacks {label} properties."
    return typed


def _doctor_rollup(
    checks: tuple[DiagnosticCheck, ...],
) -> tuple[str, ExitCode]:
    statuses = {check.status for check in checks}
    if DiagnosticStatus.FAILED in statuses:
        return "failed", ExitCode.FAILED
    if statuses & {DiagnosticStatus.MISSING, DiagnosticStatus.UNKNOWN}:
        return "incomplete", ExitCode.HUMAN_ACTION
    return "verified", ExitCode.OK


def _check_is_satisfied(check: DiagnosticCheck) -> bool:
    return check.status in {
        DiagnosticStatus.VERIFIED,
        DiagnosticStatus.NOT_APPLICABLE,
    }


def _status_code_shape_is_valid(check: DiagnosticCheck) -> bool:
    if check.status in {
        DiagnosticStatus.VERIFIED,
        DiagnosticStatus.NOT_APPLICABLE,
    }:
        return check.reason_code is None and check.repair_code is None
    return (
        isinstance(check.reason_code, str)
        and bool(check.reason_code)
        and isinstance(check.repair_code, str)
        and bool(check.repair_code)
    )


def _not_applicable_prerequisite_is_valid(check: DiagnosticCheck) -> bool:
    if check.check_id != "doctor::seat_rotation_dependency_closure_v1":
        return False
    if check.source != "static_python_source" or check.reason_code is not None:
        return False
    if check.repair_code is not None or not isinstance(check.observed, dict):
        return False
    if not isinstance(check.expected, str):
        return False
    try:
        normalized_dependency = _normalize_package_name(check.expected)
    except ValueError:
        return False
    if normalized_dependency != check.expected:
        return False
    return check.observed == {
        "imported": False,
        "dependency": normalized_dependency,
    }


def _load_json_object(path: Path) -> dict[str, object] | _StaticRead:
    loaded = _read_static_text(path)
    if loaded.status is not DiagnosticStatus.VERIFIED:
        return loaded
    if loaded.text is None:
        raise AssertionError("verified static read lacks text")
    try:
        value: object = json.loads(
            loaded.text,
            parse_constant=_reject_non_finite_json_constant,
        )
    except json.JSONDecodeError as exc:
        return _StaticRead(
            DiagnosticStatus.FAILED,
            None,
            f"Static JSON artifact is malformed: {exc.msg}.",
        )
    except ValueError as exc:
        return _StaticRead(
            DiagnosticStatus.FAILED,
            None,
            f"Static JSON artifact is malformed: {exc}.",
        )
    if not isinstance(value, dict):
        return _StaticRead(
            DiagnosticStatus.FAILED,
            None,
            "Static JSON artifact must be an object.",
        )
    typed_value = cast(dict[str, object], value)
    if not _is_json_object(typed_value):
        return _StaticRead(
            DiagnosticStatus.FAILED,
            None,
            "Static JSON artifact must contain only finite JSON values.",
        )
    return typed_value


def _reject_non_finite_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _read_static_text(path: Path) -> _StaticRead:
    components = _safe_path_components(path)
    if isinstance(components, str):
        return _StaticRead(DiagnosticStatus.FAILED, None, components)
    try:
        return _read_pinned_text(path, components)
    except FileNotFoundError:
        return _StaticRead(DiagnosticStatus.MISSING, None, "Static artifact is missing.")
    except PermissionError:
        return _StaticRead(
            DiagnosticStatus.UNKNOWN,
            None,
            "Static artifact cannot be read without additional permission.",
        )
    except OSError as exc:
        return _StaticRead(
            DiagnosticStatus.FAILED,
            None,
            f"Static artifact could not be read safely: {exc.strerror or exc.__class__.__name__}.",
        )


def _read_pinned_text(path: Path, components: tuple[str, ...]) -> _StaticRead:
    # Hold every directory descriptor until the bounded read and stability pass
    # finish. Re-resolving through Path after opening would reintroduce the
    # parent-substitution race this reader exists to prevent.
    with ExitStack() as stack:
        entries: list[_PinnedEntry] = []
        anchor = os.open(
            "/" if path.is_absolute() else ".",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        stack.callback(os.close, anchor)
        entries.append(_pinned_entry(None, None, anchor))
        parent = anchor
        for component in components[:-1]:
            parent = _open_directory_component(parent, component, stack, entries)
        descriptor = _open_regular_component(parent, components[-1], stack, entries)
        content = _read_bounded(descriptor)
        if not _pinned_entries_are_stable(entries):
            return _StaticRead(
                DiagnosticStatus.FAILED,
                None,
                "Static artifact changed while it was inspected.",
            )
        if len(content) > _MAX_STATIC_ARTIFACT_BYTES:
            return _StaticRead(
                DiagnosticStatus.FAILED,
                None,
                "Static artifact exceeds the bounded inspection size.",
            )
        return _decode_static_text(content)


def _decode_static_text(content: bytes) -> _StaticRead:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return _StaticRead(
            DiagnosticStatus.FAILED,
            None,
            "Static artifact is not valid UTF-8.",
        )
    return _StaticRead(DiagnosticStatus.VERIFIED, text, "Static artifact read.")


def _safe_path_components(path: Path) -> tuple[str, ...] | str:
    parts = path.parts[1:] if path.is_absolute() else path.parts
    components = tuple(part for part in parts if part not in {"", "."})
    if not components:
        return "Static artifact path must name a file."
    if ".." in components:
        return "Static artifact path cannot contain parent traversal."
    return components


def _pinned_entry(
    parent_descriptor: int | None,
    name: str | None,
    descriptor: int,
) -> _PinnedEntry:
    return _PinnedEntry(parent_descriptor, name, descriptor, os.fstat(descriptor))


def _open_directory_component(
    parent: int,
    name: str,
    stack: ExitStack,
    entries: list[_PinnedEntry],
) -> int:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise OSError("static artifact parent is not a regular directory")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent,
    )
    stack.callback(os.close, descriptor)
    entry = _pinned_entry(parent, name, descriptor)
    if not _same_identity(before, entry.metadata):
        raise OSError("static artifact parent changed while it was opened")
    entries.append(entry)
    return descriptor


def _open_regular_component(
    parent: int,
    name: str,
    stack: ExitStack,
    entries: list[_PinnedEntry],
) -> int:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise OSError("static artifact is not a regular file")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent,
    )
    stack.callback(os.close, descriptor)
    entry = _pinned_entry(parent, name, descriptor)
    if not stat.S_ISREG(entry.metadata.st_mode) or not _same_identity(
        before,
        entry.metadata,
    ):
        raise OSError("static artifact changed while it was opened")
    entries.append(entry)
    return descriptor


def _read_bounded(descriptor: int) -> bytes:
    # Read at most limit+1. The sentinel byte distinguishes exact-limit content
    # from oversize content without trusting a racy pre-read st_size value.
    remaining = _MAX_STATIC_ARTIFACT_BYTES + 1
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _pinned_entries_are_stable(entries: list[_PinnedEntry]) -> bool:
    # Both views must agree: fstat proves the held object stayed stable, while
    # descriptor-relative lstat proves its directory entry was not substituted.
    for entry in entries:
        current = os.fstat(entry.descriptor)
        if not _stable_metadata(entry.metadata, current):
            return False
        if entry.parent_descriptor is None or entry.name is None:
            continue
        current_path = os.stat(
            entry.name,
            dir_fd=entry.parent_descriptor,
            follow_symlinks=False,
        )
        if not _stable_metadata(entry.metadata, current_path):
            return False
    return True


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _stable_metadata(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_nlink,
        left.st_uid,
        left.st_gid,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_nlink,
        right.st_uid,
        right.st_gid,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _artifact_check(
    *,
    check_id: str,
    loaded: _StaticRead,
    reason_code: str,
    repair_code: str,
    expected: JsonValue,
    source: str,
) -> DiagnosticCheck:
    if loaded.status is DiagnosticStatus.VERIFIED:
        raise AssertionError("verified artifact read cannot produce an artifact failure")
    return DiagnosticCheck(
        check_id=check_id,
        status=loaded.status,
        summary=loaded.summary,
        reason_code=reason_code,
        repair_code=repair_code,
        observed=None,
        expected=expected,
        source=source,
    )


def _failed_check(
    check_id: str,
    summary: str,
    reason_code: str,
    repair_code: str,
    *,
    observed: JsonValue,
    expected: JsonValue,
    source: str,
) -> DiagnosticCheck:
    return DiagnosticCheck(
        check_id=check_id,
        status=DiagnosticStatus.FAILED,
        summary=summary,
        reason_code=reason_code,
        repair_code=repair_code,
        observed=observed,
        expected=expected,
        source=source,
    )


def _verified_check(
    check_id: str,
    summary: str,
    *,
    observed: JsonValue,
    expected: JsonValue,
    source: str,
) -> DiagnosticCheck:
    return DiagnosticCheck(
        check_id=check_id,
        status=DiagnosticStatus.VERIFIED,
        summary=summary,
        reason_code=None,
        repair_code=None,
        observed=observed,
        expected=expected,
        source=source,
    )


def _normalize_package_name(value: str) -> str:
    match = _REQUIREMENT_NAME.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid dependency name: {value!r}")
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _parse_imported_names(
    text: str,
    filename: str,
) -> tuple[set[str], set[str]] | str:
    try:
        tree = ast.parse(text, filename=filename)
    except SyntaxError as exc:
        return f"Python source is not statically parseable: {exc.msg}."
    return _imported_top_level_names(tree), _unguarded_module_import_names(tree)


def _parse_project_dependencies(text: str) -> tuple[_ParsedRequirement, ...] | str:
    try:
        value: object = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return "Package metadata is not valid TOML."
    return _project_dependencies(value)


def _imported_top_level_names(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level = alias.name.partition(".")[0]
                if top_level != "__future__":
                    imported.add(_normalize_package_name(top_level))
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            top_level = node.module.partition(".")[0]
            if top_level != "__future__":
                imported.add(_normalize_package_name(top_level))
    return imported


def _unguarded_module_import_names(tree: ast.AST) -> set[str]:
    """Return imports directly in a module body, excluding any guarded control flow."""
    if not isinstance(tree, ast.Module):
        raise AssertionError("Python parser did not return a module tree")
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(_normalized_alias_names(node.names))
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module.partition(".")[0] != "__future__":
                imported.add(_normalized_module_name(node.module))
    return imported


def _normalized_alias_names(aliases: list[ast.alias]) -> set[str]:
    return {
        _normalized_module_name(alias.name)
        for alias in aliases
        if alias.name.partition(".")[0] != "__future__"
    }


def _normalized_module_name(module: str) -> str:
    return _normalize_package_name(module.partition(".")[0])


def _project_dependencies(value: object) -> tuple[_ParsedRequirement, ...] | str:
    if not isinstance(value, dict):
        return "Package metadata root must be a TOML table."
    project = cast(dict[str, object], value).get("project")
    if not isinstance(project, dict):
        return "Package metadata lacks a project table."
    dependencies = cast(dict[str, object], project).get("dependencies")
    if not isinstance(dependencies, list):
        return "Package metadata project.dependencies must be an array."
    parsed: list[_ParsedRequirement] = []
    for dependency in dependencies:
        # Parse every direct row before considering name membership. Keeping the
        # parsed specifier, marker, extras, and URL prevents prefix-only evidence
        # from silently discarding the controls attached to a valid requirement.
        if not isinstance(dependency, str):
            return "Package dependency entries must be strings."
        try:
            requirement = Requirement(dependency)
        except InvalidRequirement:
            return "Package dependency entry is not valid PEP 508."
        parsed.append(
            _ParsedRequirement(
                normalized_name=_normalize_package_name(requirement.name),
                specifier=str(requirement.specifier),
                marker=str(requirement.marker) if requirement.marker is not None else None,
                extras=tuple(sorted(requirement.extras)),
                url=requirement.url,
            )
        )
    return tuple(parsed)


# Internal cross-module seams used by diagnostic_contract_checks. Public aliases
# keep strict unused-function analysis honest without widening the CLI surface.
validated_public_input_contract = _validated_public_input_contract
projection_evidence = _projection_evidence
binding_payload_error = _binding_payload_error
load_json_object = _load_json_object
artifact_check = _artifact_check
failed_check = _failed_check
verified_check = _verified_check
parse_imported_names = _parse_imported_names
parse_project_dependencies = _parse_project_dependencies
