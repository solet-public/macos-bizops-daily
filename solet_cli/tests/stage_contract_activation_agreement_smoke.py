#!/usr/bin/env python3
"""Regression proof for optional remediation and decision-bound stage contracts."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager import operation_executor, stage_boundaries  # noqa: E402
from solet_manager.adapters import OperationResult  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.errors import StateConflictError  # noqa: E402
from solet_manager.flow import PlannedOperation  # noqa: E402
from solet_manager.journal_migrations import activation_site_key  # noqa: E402
from solet_manager.models import CheckpointStatus, JsonValue  # noqa: E402
from solet_manager.plan_builder import selected_operation_ids  # noqa: E402
from solet_manager.stage_activation import initial_probe_activations  # noqa: E402
from solet_manager.transaction import Transaction  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS = _ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
_CHECKS = 0


class _StopTransaction:
    status = CheckpointStatus.PENDING

    def to_dict(self) -> dict[str, JsonValue]:
        return {}


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


def _answers(autostart: str) -> dict[str, JsonValue]:
    return {
        "setup_profile": "macos-bizops",
        "autostart": autostart,
        "embeddings_implementation": "lm_studio",
        "embedding_model": "fixture-embedding",
        "inference_implementation": "lm_studio",
        "inference_model": "fixture-inference",
        "coding_agents": ["codex", "claude_code"],
        "execution_topology": "fleet",
        "connector_configuration_timing": "first_use",
    }


def _embedding_operation(bundle: ContractBundle) -> PlannedOperation:
    definition = bundle.operations["configure_lm_studio_embeddings"]
    idempotency = definition["idempotency"]
    if not isinstance(idempotency, dict):
        raise AssertionError("embedding operation lacks idempotency")
    preconditions = idempotency["precondition_probe_refs"]
    postconditions = idempotency["postcondition_probe_refs"]
    if not isinstance(preconditions, list) or not isinstance(postconditions, list):
        raise AssertionError("embedding operation probe declarations are invalid")
    return PlannedOperation(
        stage_id="models",
        operation_id="configure_lm_studio_embeddings",
        operation_ref=str(definition["operation_ref"]),
        runner=str(definition["runner"]),
        risk=str(definition["risk"]),
        requires_confirmation=bool(definition["requires_confirmation"]),
        precondition_probe_ids=tuple(str(item) for item in preconditions),
        postcondition_probe_ids=tuple(str(item) for item in postconditions),
        public_inputs={},
    )


def _blocked_embedding_probe() -> OperationResult:
    return OperationResult(
        request_id="fixture",
        operation_id="embedding_request_succeeds",
        phase="probe",
        probe_purpose="pre_apply",
        checkpoint_status=CheckpointStatus.BLOCKED,
        error_kind="fixture_embedding_blocked",
        retry_safe=True,
        exit_code=1,
        timed_out=False,
        duration_ms=0,
        stdout="",
        stderr="",
        planned_actions=(),
        discovered_candidates=(),
        evidence=(),
        repair="fixture repair",
    )


def _assert_optional_remediation(bundle: ContractBundle) -> None:
    operation = _embedding_operation(bundle)
    blocked_probe = _blocked_embedding_probe()
    qualified_probe = replace(
        blocked_probe,
        operation_id="embedding_model_qualification",
        checkpoint_status=CheckpointStatus.VERIFIED,
        error_kind=None,
        exit_code=0,
        repair=None,
    )
    legacy_operation = replace(
        operation,
        precondition_probe_ids=("embedding_request_succeeds",),
        postcondition_probe_ids=("embedding_request_succeeds",),
    )
    for autostart in ("disabled", "enabled"):
        selected = selected_operation_ids(bundle, _answers(autostart))
        _check(
            "configure_lm_studio_embeddings" in selected,
            f"{autostart} selects the embedding operation with the blocked precondition",
        )
        _check(
            ("install_launchagent" in selected) is (autostart == "enabled"),
            f"{autostart} preserves the LaunchAgent selection boundary",
        )
        _check(
            operation.precondition_probe_ids == ("embedding_model_qualification",)
            and operation.postcondition_probe_ids == ("embedding_model_qualification",),
            f"{autostart} uses manager-direct embedding qualification at both operation boundaries",
        )
        terminal = operation_executor._pre_apply_stop(
            bundle,
            operation,
            qualified_probe,
            cast(Transaction, _StopTransaction()),
        )
        _check(
            terminal is None,
            f"{autostart} proceeds past pre-apply without a running target platform",
        )
        legacy_terminal = operation_executor._pre_apply_stop(
            bundle,
            legacy_operation,
            blocked_probe,
            cast(Transaction, _StopTransaction()),
        )
        _check(
            legacy_terminal is not None
            and legacy_terminal.error_kind == "fixture_embedding_blocked",
            f"killing mutation: old platform-process precondition stops for {autostart}",
        )
        with patch.object(
            operation_executor,
            "_probe_remediates_operation",
            side_effect=StateConflictError("legacy missing-key rejection"),
        ):
            _raises(
                StateConflictError,
                lambda: operation_executor._pre_apply_stop(
                    bundle,
                    legacy_operation,
                    blocked_probe,
                    cast(Transaction, _StopTransaction()),
                ),
                f"killing mutation: legacy missing-key rejection is red for {autostart}",
            )

    definition = bundle.probes["embedding_request_succeeds"]
    definition["remediation_operation_refs"] = None
    try:
        _raises(
            StateConflictError,
            lambda: operation_executor._probe_remediates_operation(
                bundle,
                "embedding_request_succeeds",
                "configure_lm_studio_embeddings",
            ),
            "present null remediation refs remain malformed for operation preconditions",
        )
        _raises(
            StateConflictError,
            lambda: stage_boundaries._remediation_operation_ids(
                bundle, "embedding_request_succeeds"
            ),
            "present null remediation refs remain malformed for stage boundaries",
        )
    finally:
        del definition["remediation_operation_refs"]


def _stage_operations(bundle: ContractBundle) -> dict[str, str]:
    owners: dict[str, str] = {}
    for stage_id, definition in bundle.stages.items():
        operation_ids = definition.get("operation_refs")
        if not isinstance(operation_ids, list) or not all(
            isinstance(operation_id, str) for operation_id in operation_ids
        ):
            raise AssertionError(f"stage {stage_id} operation refs are invalid")
        for operation_id in operation_ids:
            if operation_id in owners:
                raise AssertionError(f"operation {operation_id} has multiple stage owners")
            owners[operation_id] = stage_id
    return owners


def _boundary_remediation_uses(
    bundle: ContractBundle,
) -> dict[str, list[tuple[str, str]]]:
    uses: dict[str, list[tuple[str, str]]] = {}
    for stage_id, stage in bundle.stages.items():
        for field_name in ("entry_probe_refs", "exit_probe_refs"):
            _record_boundary_remediation_uses(bundle, uses, stage_id, stage, field_name)
    return uses


def _record_boundary_remediation_uses(
    bundle: ContractBundle,
    uses: dict[str, list[tuple[str, str]]],
    stage_id: str,
    stage: dict[str, JsonValue],
    field_name: str,
) -> None:
    probe_ids = stage.get(field_name, [])
    if not isinstance(probe_ids, list) or not all(
        isinstance(probe_id, str) for probe_id in probe_ids
    ):
        raise AssertionError(f"stage {stage_id} {field_name} is invalid")
    for probe_id in probe_ids:
        _record_probe_remediation_uses(bundle, uses, stage_id, probe_id)


def _record_probe_remediation_uses(
    bundle: ContractBundle,
    uses: dict[str, list[tuple[str, str]]],
    stage_id: str,
    probe_id: str,
) -> None:
    remediation = bundle.probes[probe_id].get("remediation_operation_refs", [])
    if not isinstance(remediation, list) or not all(
        isinstance(operation_id, str) for operation_id in remediation
    ):
        raise AssertionError(f"probe {probe_id} remediation refs are invalid")
    for operation_id in remediation:
        uses.setdefault(operation_id, []).append((stage_id, probe_id))


def _assert_option_stage_agreement(bundle: ContractBundle) -> None:
    owners = _stage_operations(bundle)
    remediation_uses = _boundary_remediation_uses(bundle)
    expected = _operation_activation_leaves(bundle)
    for decision_id, decision in bundle.decisions.items():
        source = decision.get("option_source")
        options = source.get("options") if isinstance(source, dict) else None
        if not isinstance(options, dict):
            continue
        for option_id, option in options.items():
            _assert_option_agreement(
                bundle, owners, remediation_uses, decision_id, option_id, option, expected
            )


def _operation_activation_leaves(bundle: ContractBundle) -> dict[str, set[tuple[str, str, str]]]:
    expected: dict[str, set[tuple[str, str, str]]] = {}
    for decision_id, decision in bundle.decisions.items():
        source = decision.get("option_source")
        options = source.get("options") if isinstance(source, dict) else None
        if not isinstance(options, dict):
            continue
        for option_id, option in options.items():
            for operation_id in _option_operation_ids(decision_id, option_id, option):
                expected.setdefault(operation_id, set()).add((decision_id, "equals", option_id))
    return expected


def _assert_option_agreement(
    bundle: ContractBundle,
    owners: dict[str, str],
    remediation_uses: dict[str, list[tuple[str, str]]],
    decision_id: str,
    option_id: str,
    option: JsonValue,
    expected: dict[str, set[tuple[str, str, str]]],
) -> None:
    operation_ids = _option_operation_ids(decision_id, option_id, option)
    for operation_id in operation_ids:
        _check(
            operation_id in owners,
            f"{decision_id}.{option_id} activates stage-owned {operation_id}",
        )
        _assert_remediation_conditions(
            bundle, remediation_uses, operation_id, decision_id, option_id, expected[operation_id]
        )


def _option_operation_ids(
    decision_id: str,
    option_id: str,
    option: JsonValue,
) -> list[str]:
    if not isinstance(option, dict):
        raise AssertionError(f"decision {decision_id} option {option_id} is invalid")
    activates = option.get("activates", {})
    if not isinstance(activates, dict):
        raise AssertionError(f"decision {decision_id} option {option_id} activates is invalid")
    operation_ids = activates.get("operation_refs", [])
    if not isinstance(operation_ids, list) or not all(
        isinstance(operation_id, str) for operation_id in operation_ids
    ):
        raise AssertionError(
            f"decision {decision_id} option {option_id} operation refs are invalid"
        )
    return operation_ids


def _assert_remediation_conditions(
    bundle: ContractBundle,
    remediation_uses: dict[str, list[tuple[str, str]]],
    operation_id: str,
    decision_id: str,
    option_id: str,
    expected: set[tuple[str, str, str]],
) -> None:
    for stage_id, probe_id in remediation_uses.get(operation_id, []):
        _check(
            _activation_leaves(bundle.probes[probe_id].get("required_when")) == expected,
            f"{decision_id}.{option_id} agrees with {stage_id} boundary {probe_id}",
        )


def _activation_leaves(condition: JsonValue) -> set[tuple[str, str, str]]:
    if not isinstance(condition, dict):
        raise AssertionError("activation condition must be an object")
    if set(condition) == {"any"}:
        children = condition["any"]
        if not isinstance(children, list) or not children:
            raise AssertionError("activation union must have children")
        return set().union(*(_activation_leaves(child) for child in children))
    if set(condition) != {"decision_ref", "operator", "value"}:
        raise AssertionError("activation condition must be a leaf or exact union")
    decision, operator, value = condition["decision_ref"], condition["operator"], condition["value"]
    if not all(isinstance(item, str) for item in (decision, operator, value)):
        raise AssertionError("activation leaf must use strings")
    return {(str(decision), str(operator), str(value))}


def _assert_shared_activation_controls(bundle: ContractBundle) -> None:
    probe = bundle.probes["lm_studio_cli_available"]
    saved = probe["required_when"]
    embedding = {"decision_ref": "embeddings_implementation", "operator": "equals", "value": "lm_studio"}
    unrelated = {"decision_ref": "inference_implementation", "operator": "equals", "value": "none"}
    for condition in (embedding, {"any": [saved, unrelated]}):
        probe["required_when"] = condition
        try:
            _raises(AssertionError, lambda: _assert_option_stage_agreement(bundle), "shared probe rejects a missing selector or an unrelated activating selector")
        finally:
            probe["required_when"] = saved


def _assert_autostart_controls(bundle: ContractBundle) -> None:
    enabled = _answers("enabled")
    disabled = _answers("disabled")
    _check(
        "install_launchagent" in selected_operation_ids(bundle, enabled),
        "enabled selection includes LaunchAgent",
    )
    _check(
        "install_launchagent" not in selected_operation_ids(bundle, disabled),
        "disabled selection excludes LaunchAgent",
    )
    launchagent = bundle.probes["launchagent_running"]
    _check(
        launchagent.get("remediation_operation_refs") == ["install_launchagent"],
        "LaunchAgent exit probe declares its matching operation",
    )
    site = activation_site_key("models", "exit", "launchagent_running")
    _check(
        initial_probe_activations(bundle, {"decisions": enabled})[site]["state"] == "active",
        "enabled LaunchAgent exit probe is active",
    )
    _check(
        initial_probe_activations(bundle, {"decisions": disabled})[site]["state"] == "inactive",
        "disabled LaunchAgent exit probe is inactive",
    )

    original_activates = bundle.decisions["autostart"]["option_source"]
    if not isinstance(original_activates, dict):
        raise AssertionError("autostart option source is invalid")
    options = original_activates.get("options")
    if not isinstance(options, dict) or not isinstance(options.get("enabled"), dict):
        raise AssertionError("autostart enabled option is invalid")
    enabled_option = options["enabled"]
    original_operations = enabled_option["activates"]
    if not isinstance(original_operations, dict):
        raise AssertionError("autostart enabled activation is invalid")
    saved_operation_refs = original_operations["operation_refs"]
    original_operations["operation_refs"] = []
    try:
        _raises(
            AssertionError,
            lambda: _assert_autostart_controls(bundle),
            "killing mutation: removing enabled LaunchAgent activation is red",
        )
    finally:
        original_operations["operation_refs"] = saved_operation_refs

    saved_condition = launchagent["required_when"]
    launchagent["required_when"] = {
        "decision_ref": "autostart",
        "operator": "equals",
        "value": "disabled",
    }
    try:
        _raises(
            AssertionError,
            lambda: _assert_option_stage_agreement(bundle),
            "killing mutation: inverted LaunchAgent condition is red",
        )
    finally:
        launchagent["required_when"] = saved_condition


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    _assert_optional_remediation(bundle)
    _assert_option_stage_agreement(bundle)
    _assert_autostart_controls(bundle)
    _assert_shared_activation_controls(bundle)
    print(f"stage_contract_activation_agreement_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
