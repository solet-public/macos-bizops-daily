"""Closed-world and top-level validation for shared setup contracts."""

from __future__ import annotations

import re
from typing import Protocol

from .contract_decision_validation import validate_decision_contract
from .errors import ContractError
from .models import CheckpointStatus, JsonValue

_EXPECTED_STATUSES = tuple(status.value for status in CheckpointStatus)
_CALLABLE_REF_PATTERN = re.compile(r"^[a-z][a-z0-9_]*::[a-z][a-z0-9_.]*$")
_RUNNER_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")


class ContractView(Protocol):
    @property
    def flow(self) -> dict[str, JsonValue]: ...

    @property
    def flow_schema(self) -> dict[str, JsonValue]: ...

    @property
    def answers_schema(self) -> dict[str, JsonValue]: ...

    @property
    def journal_schema(self) -> dict[str, JsonValue]: ...

    @property
    def adapter_schema(self) -> dict[str, JsonValue]: ...

    @property
    def stages(self) -> dict[str, dict[str, JsonValue]]: ...

    @property
    def operations(self) -> dict[str, dict[str, JsonValue]]: ...

    @property
    def probes(self) -> dict[str, dict[str, JsonValue]]: ...

    @property
    def decisions(self) -> dict[str, dict[str, JsonValue]]: ...

    @property
    def consents(self) -> dict[str, dict[str, JsonValue]]: ...

    @property
    def start_command(self) -> dict[str, JsonValue]: ...


def validate_contract_bundle(bundle: ContractView) -> None:
    """Apply dependency-free closed-world and cross-reference invariants."""

    _validate_flow_shape(bundle.flow, bundle.flow_schema)
    _validate_state_policy(bundle.flow)
    _validate_completion_policy(bundle.flow)
    validate_start_command(bundle.flow)
    _validate_contract_schema_identity(
        bundle.answers_schema,
        "https://solet.ai/schemas/setup-answers-v1.json",
    )
    _validate_contract_schema_identity(
        bundle.journal_schema,
        "https://solet.ai/schemas/setup-journal-v1.json",
    )
    _validate_contract_schema_identity(
        bundle.adapter_schema,
        "https://solet.ai/schemas/setup-adapter-envelope-v1.json",
    )
    _check_references(bundle.flow)
    validate_decision_contract(bundle)


def _validate_flow_shape(
    flow: dict[str, JsonValue],
    schema: dict[str, JsonValue],
) -> None:
    required = schema.get("required")
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ContractError("setup flow schema required list is invalid")
    required_names = {str(item) for item in required}
    missing = sorted(required_names - set(flow))
    if missing:
        raise ContractError(f"setup flow lacks required top-level fields: {missing}")
    if flow.get("schema_version") != 1:
        raise ContractError("setup flow identity must be schema v1 macos.repository_setup")
    if flow.get("flow_id") != "macos.repository_setup":
        raise ContractError("setup flow identity must be schema v1 macos.repository_setup")


def _validate_state_policy(flow: dict[str, JsonValue]) -> None:
    policy = _object(flow, "state_policy")
    statuses = policy.get("statuses")
    if statuses != list(_EXPECTED_STATUSES):
        raise ContractError(
            "setup flow status vocabulary/order drifted: "
            f"expected={list(_EXPECTED_STATUSES)}, got={statuses}"
        )
    if policy.get("record_secret_values") is not False:
        raise ContractError("setup flow must explicitly forbid secret journal values")


def _validate_completion_policy(flow: dict[str, JsonValue]) -> None:
    completion = _object(flow, "completion")
    if completion.get("allow_health_only") is not False:
        raise ContractError("setup completion may not be health-only")


def _validate_contract_schema_identity(
    schema: dict[str, JsonValue],
    expected_id: str,
) -> None:
    if schema.get("$schema") != "http://json-schema.org/draft-07/schema#":
        raise ContractError(f"{expected_id} must use Draft-07")
    if schema.get("$id") != expected_id:
        raise ContractError(f"{expected_id} identity or closed-object rule is invalid")
    if schema.get("additionalProperties") is not False:
        raise ContractError(f"{expected_id} identity or closed-object rule is invalid")


def _check_references(flow: dict[str, JsonValue]) -> None:
    registries = {key: _registry(flow, key) for key in ("operations", "probes", "stages")}
    _validate_postcondition_remediation_order(registries)
    for stage_id, stage in registries["stages"].items():
        _check_stage_references(stage_id, stage, registries)
    completion = _object(flow, "completion")
    _require_known_refs(
        "completion",
        "required_probe_refs",
        _string_tuple(
            completion.get("required_probe_refs"),
            "completion.required_probe_refs",
        ),
        registries["probes"],
    )
    start = _json_object(
        _object(flow, "executor_contracts").get("start_command"),
        "executor_contracts.start_command",
    )
    _require_known_refs(
        "lifecycle start",
        "postcondition probes",
        _string_tuple(
            start.get("postcondition_probe_refs"),
            "executor_contracts.start_command.postcondition_probe_refs",
        ),
        registries["probes"],
    )
    readiness = _json_object(
        start.get("startup_readiness"),
        "executor_contracts.start_command.startup_readiness",
    )
    _require_known_refs(
        "startup readiness",
        "consumer probes",
        _string_tuple(
            readiness.get("consumer_probe_refs"),
            "executor_contracts.start_command.startup_readiness.consumer_probe_refs",
        ),
        registries["probes"],
    )


def _validate_postcondition_remediation_order(
    registries: dict[str, dict[str, dict[str, JsonValue]]],
) -> None:
    """Reject postconditions that require a later operation to remediate."""

    positions = _operation_positions(registries["stages"])
    operations = registries["operations"]
    probes = registries["probes"]
    for operation_id, definition in operations.items():
        idempotency = _json_object(
            definition.get("idempotency"), f"operations.{operation_id}.idempotency"
        )
        postconditions = _string_tuple(
            idempotency.get("postcondition_probe_refs"),
            f"operations.{operation_id}.postcondition_probe_refs",
        )
        owner_position = positions.get(operation_id, (-1, -1))
        for probe_id in postconditions:
            probe = probes.get(probe_id)
            if probe is None:
                continue
            remediation = _string_tuple(
                probe.get("remediation_operation_refs", []),
                f"probes.{probe_id}.remediation_operation_refs",
            )
            later = [
                remediation_id
                for remediation_id in remediation
                if positions.get(remediation_id, (-1, -1)) > owner_position
            ]
            if later:
                raise ContractError(
                    f"operation {operation_id!r} declares postcondition probe "
                    f"{probe_id!r} remediated by later operations: {later}"
                )


def _operation_positions(
    stages: dict[str, dict[str, JsonValue]],
) -> dict[str, tuple[int, int]]:
    ordered_stages = sorted(
        stages.items(), key=lambda item: _stage_sequence(item[0], item[1])
    )
    positions: dict[str, tuple[int, int]] = {}
    for stage_id, definition in ordered_stages:
        sequence = _stage_sequence(stage_id, definition)
        for index, operation_id in enumerate(
            _string_tuple(definition.get("operation_refs"), f"stages.{stage_id}.operation_refs")
        ):
            positions[operation_id] = (sequence, index)
    return positions


def _stage_sequence(stage_id: str, definition: dict[str, JsonValue]) -> int:
    sequence = definition.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise ContractError(f"stage {stage_id!r} sequence must be an integer")
    return sequence


def _check_stage_references(
    stage_id: str,
    stage: dict[str, JsonValue],
    registries: dict[str, dict[str, dict[str, JsonValue]]],
) -> None:
    declarations = (
        ("operation_refs", "operations", False),
        ("entry_probe_refs", "probes", True),
        ("exit_probe_refs", "probes", False),
        ("depends_on_stage_refs", "stages", True),
    )
    for key, registry_name, optional in declarations:
        raw = stage.get(key)
        refs = () if optional and raw is None else _string_tuple(raw, f"stages.{stage_id}.{key}")
        _require_known_refs(
            f"stage {stage_id!r}",
            key,
            refs,
            registries[registry_name],
        )


def _require_known_refs(
    owner: str,
    label: str,
    refs: tuple[str, ...],
    registry: dict[str, dict[str, JsonValue]],
) -> None:
    missing = sorted(set(refs) - set(registry))
    if not missing:
        return
    if owner == "completion":
        raise ContractError(f"completion has unresolved probes: {missing}")
    if owner == "lifecycle start":
        raise ContractError(f"lifecycle start has unresolved postcondition probes: {missing}")
    if owner == "startup readiness":
        raise ContractError(f"startup readiness has unresolved consumer probes: {missing}")
    raise ContractError(f"{owner} has unresolved {label}: {missing}")


def validate_start_command(flow: dict[str, JsonValue]) -> None:
    start = _json_object(
        _object(flow, "executor_contracts").get("start_command"),
        "executor_contracts.start_command",
    )
    required = {
        "operation_ref",
        "runner",
        "timeout_seconds",
        "startup_readiness",
        "postcondition_probe_refs",
    }
    if set(start) != required:
        raise ContractError("executor_contracts.start_command differs from the closed v1 shape")
    _validate_start_runner(start)
    _validate_start_operation_ref(start)
    _validate_start_timeout(start)
    _validate_startup_readiness(start)
    _validate_start_postconditions(start)


def _validate_start_runner(start: dict[str, JsonValue]) -> None:
    value = start.get("runner")
    if not isinstance(value, str) or _RUNNER_PATTERN.fullmatch(value) is None:
        raise ContractError("executor_contracts.start_command.runner has an invalid id")


def _validate_start_operation_ref(start: dict[str, JsonValue]) -> None:
    value = start.get("operation_ref")
    if not isinstance(value, str) or _CALLABLE_REF_PATTERN.fullmatch(value) is None:
        raise ContractError("executor_contracts.start_command.operation_ref is invalid")


def _validate_start_timeout(start: dict[str, JsonValue]) -> None:
    timeout = start.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ContractError("executor_contracts.start_command.timeout_seconds is invalid")
    if not 1 <= timeout <= 900:
        raise ContractError("executor_contracts.start_command.timeout_seconds is invalid")


def _validate_startup_readiness(start: dict[str, JsonValue]) -> None:
    source = "executor_contracts.start_command.startup_readiness"
    readiness = _json_object(start.get("startup_readiness"), source)
    required = {
        "contract_version",
        "budget_source",
        "budget_unit",
        "semantic_scope",
        "release_signal",
        "consumer_probe_purposes",
        "consumer_probe_refs",
        "downstream_reservations",
    }
    if set(readiness) != required:
        raise ContractError(f"{source} differs from the closed v1 shape")
    _require_exact_readiness_values(
        readiness,
        source,
        {
            "contract_version": 1,
            "budget_source": "executor_contracts.start_command.timeout_seconds",
            "budget_unit": "seconds",
            "semantic_scope": ("target_start_through_target_cli_health_status_healthy"),
            "release_signal": "target_cli_health_top_level_status_healthy",
            "consumer_probe_purposes": ["stage_exit", "completion"],
            "consumer_probe_refs": ["embedding_request_succeeds"],
        },
    )
    reservations_source = f"{source}.downstream_reservations"
    reservations = _json_object(
        readiness.get("downstream_reservations"),
        reservations_source,
    )
    if set(reservations) != {"governed_process_call_seconds"}:
        raise ContractError(f"{reservations_source} differs from the closed v1 shape")
    _validate_readiness_reservation(
        reservations.get("governed_process_call_seconds"),
        start.get("timeout_seconds"),
        reservations_source,
    )


def _require_exact_readiness_values(
    readiness: dict[str, JsonValue],
    source: str,
    expected_values: dict[str, JsonValue],
) -> None:
    for key, expected in expected_values.items():
        if readiness.get(key) != expected:
            raise ContractError(f"{source}.{key} is invalid")


def _validate_readiness_reservation(
    reserve: JsonValue,
    parent: JsonValue,
    reservations_source: str,
) -> None:
    if isinstance(reserve, bool) or not isinstance(reserve, int) or reserve < 1:
        raise ContractError(f"{reservations_source}.governed_process_call_seconds is invalid")
    if isinstance(parent, bool) or not isinstance(parent, int) or reserve >= parent:
        raise ContractError(f"{reservations_source}.governed_process_call_seconds is invalid")


def _validate_start_postconditions(start: dict[str, JsonValue]) -> None:
    probes = _string_tuple(
        start.get("postcondition_probe_refs"),
        "executor_contracts.start_command.postcondition_probe_refs",
    )
    if not probes or len(probes) != len(set(probes)):
        raise ContractError("executor_contracts.start postconditions must be non-empty and unique")


def _object(value: dict[str, JsonValue], key: str) -> dict[str, JsonValue]:
    return _json_object(value.get(key), key)


def _json_object(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value


def _registry(
    value: dict[str, JsonValue],
    key: str,
) -> dict[str, dict[str, JsonValue]]:
    raw = _object(value, key)
    parsed: dict[str, dict[str, JsonValue]] = {}
    for item_id, item in raw.items():
        if not isinstance(item, dict):
            raise ContractError(f"{key}.{item_id} must be an object")
        parsed[item_id] = item
    return parsed


def _string_tuple(value: JsonValue, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContractError(f"{label} must be a string array")
    return tuple(item for item in value if isinstance(item, str))
