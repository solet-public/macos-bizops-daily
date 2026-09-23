"""Connector-graph assertions invoked by the registered manager foundation smoke."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from solet_manager import operation_executor
from solet_manager.adapters import AdapterRegistry, OperationRequest, OperationResult
from solet_manager.config import CreateConfig
from solet_manager.contracts import ContractBundle
from solet_manager.flow import PlannedOperation, build_setup_plan, initial_stage_probe_statuses
from solet_manager.models import CheckpointStatus
from solet_manager.paths import ManagerPaths
from solet_manager.release_lock import SeedLock
from solet_manager.transaction import Transaction

_CONNECTORS = [
    "google_workspace",
    "jira",
    "marketo",
    "salesforce",
    "schwab",
    "snowflake",
    "zuora",
    "external_postgres",
]
_OPERATIONS = {
    "configure_google_workspace",
    "configure_jira",
    "configure_marketo",
    "configure_salesforce",
    "configure_schwab",
    "configure_snowflake",
    "configure_zuora",
    "configure_external_postgres",
}
_CONNECTION_PROBES = (
    "google_workspace_connection_valid",
    "jira_connection_valid",
    "marketo_connection_valid",
    "salesforce_connection_valid",
    "schwab_connection_valid",
    "snowflake_connection_valid",
    "zuora_connection_valid",
    "external_postgres_connection_valid",
)


def connector_graph_plan_check(
    *,
    root: Path,
    paths: ManagerPaths,
    bundle: ContractBundle,
    config: CreateConfig,
    seed: SeedLock,
    contracts: Path,
) -> None:
    selections = {
        "connector_configuration_timing": "configure_now",
        "connectors_to_configure": _CONNECTORS,
    }
    plan = _plan(bundle, config, seed, paths, selections, "connector-graph")
    _assert_jira_selection_plans_operation_and_consent(bundle, config, seed, paths)
    _require(
        _OPERATIONS <= {operation.operation_id for operation in plan.operations},
        "configure-now connector selections must schedule every configuration operation",
    )
    exits = initial_stage_probe_statuses(bundle, plan.answers)["optional_accounts"]["exit"]
    _require(
        all(exits[probe_id] is CheckpointStatus.PENDING for probe_id in _CONNECTION_PROBES),
        "optional-accounts must check each selected connector's live connection",
    )
    _assert_single_connector_filter(bundle, config, seed, paths)
    _assert_google_wiring_mutation_is_red(root, paths, config, seed, contracts, selections)


def _plan(
    bundle: ContractBundle,
    config: CreateConfig,
    seed: SeedLock,
    paths: ManagerPaths,
    selections: dict[str, str | list[str]],
    name: str,
    operation_stage_ids: set[str] | None = None,
):
    return build_setup_plan(
        bundle=bundle,
        config=config,
        seed=seed,
        journal_path=paths.transaction_path(name),
        decision_selections=selections,
        operation_stage_ids=operation_stage_ids,
    )


def _assert_single_connector_filter(
    bundle: ContractBundle,
    config: CreateConfig,
    seed: SeedLock,
    paths: ManagerPaths,
) -> None:
    plan = _plan(
        bundle,
        config,
        seed,
        paths,
        {
            "connector_configuration_timing": "configure_now",
            "connectors_to_configure": ["google_workspace"],
        },
        "connector-graph-google",
    )
    exits = initial_stage_probe_statuses(bundle, plan.answers)["optional_accounts"]["exit"]
    _require(
        exits["google_workspace_connection_valid"] is CheckpointStatus.PENDING
        and exits["marketo_connection_valid"] is CheckpointStatus.NOT_APPLICABLE,
        "optional-accounts must probe only the selected connector",
    )


def _assert_jira_selection_plans_operation_and_consent(
    bundle: ContractBundle,
    config: CreateConfig,
    seed: SeedLock,
    paths: ManagerPaths,
) -> None:
    plan = _plan(
        bundle,
        config,
        seed,
        paths,
        {
            "connector_configuration_timing": "configure_now",
            "connectors_to_configure": ["jira"],
        },
        "connector-graph-jira",
        operation_stage_ids={"optional_accounts"},
    )
    operation_ids = tuple(operation.operation_id for operation in plan.operations)
    _require(
        operation_ids == ("configure_jira",)
        and plan.unresolved_consents == ("jira_destructive_access_consent",),
        "Jira selection must plan configure_jira and require its destructive-access "
        f"consent; operations={operation_ids}, consents={plan.unresolved_consents}",
    )


def _assert_google_wiring_mutation_is_red(
    root: Path,
    paths: ManagerPaths,
    config: CreateConfig,
    seed: SeedLock,
    contracts: Path,
    selections: dict[str, str | list[str]],
) -> None:
    changed_contracts = root / "connector-graph-contracts"
    shutil.copytree(contracts, changed_contracts)
    changed_flow_path = changed_contracts / "macos_setup_flow.json"
    changed_flow = json.loads(changed_flow_path.read_text(encoding="utf-8"))
    activates = changed_flow["decisions"]["connectors_to_configure"]["option_source"][
        "options"
    ]["google_workspace"]["activates"]
    activates.pop("operation_refs")
    changed_flow_path.write_text(json.dumps(changed_flow, indent=2), encoding="utf-8")
    changed_bundle = ContractBundle.load(source_revision="a" * 40, directory=changed_contracts)
    changed_plan = _plan(
        changed_bundle,
        config,
        seed,
        paths,
        selections,
        "connector-graph",
    )
    _require(
        "configure_google_workspace"
        not in {operation.operation_id for operation in changed_plan.operations},
        "removing Google's operation ref must make the configure-now plan red before approval",
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def genesis_topology_probe_activation_is_correct(bundle: ContractBundle) -> bool:
    disabled_free = initial_stage_probe_statuses(
        bundle,
        {"decisions": {"setup_profile": "free", "autostart": "disabled"}},
    )
    enabled_business = initial_stage_probe_statuses(
        bundle,
        {"decisions": {"setup_profile": "macos-bizops", "autostart": "enabled"}},
    )
    return all((
        disabled_free["models"]["exit"]["launchagent_running"]
        is CheckpointStatus.NOT_APPLICABLE,
        disabled_free["models"]["exit"]["router_ready"]
        is CheckpointStatus.NOT_APPLICABLE,
        enabled_business["models"]["exit"]["launchagent_running"]
        is CheckpointStatus.PENDING,
        enabled_business["models"]["exit"]["router_ready"]
        is CheckpointStatus.PENDING,
    ))


def declared_operation_probe_dispatch_check(bundle: ContractBundle, target: Path) -> None:
    transaction = replace(
        _probe_dispatch_transaction(target),
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
    )
    operation = PlannedOperation(
        stage_id="session_sources",
        operation_id="register_codex_session_source",
        operation_ref="hydration::sessions.register_codex_filesystem",
        runner="hydration",
        risk="medium",
        requires_confirmation=True,
        precondition_probe_ids=("session_sources_retrievable",),
        postcondition_probe_ids=("session_sources_retrievable",),
        public_inputs={},
    )
    observed: list[tuple[str, str]] = []

    def fake_invoke(
        _registry: AdapterRegistry,
        *,
        runner: str,
        request: OperationRequest,
    ) -> OperationResult:
        observed.append((runner, request.operation_ref))
        return OperationResult.blocked(request, error_kind="fixture_stop", repair="fixture")

    with patch.object(operation_executor, "invoke_adapter", fake_invoke):
        for purpose in ("pre_apply", "post_apply"):
            result = operation_executor._invoke_operation_probe(
                bundle=bundle,
                operation=operation,
                transaction=transaction,
                registry=AdapterRegistry(target=target),
                purpose=purpose,
                attempt=1,
            )
            _require(
                result.error_kind == "fixture_stop",
                f"declared {purpose} probe result must be retained",
            )
    expected = (
        "platform_process",
        "service_interface::session_ledger_service.qualify_selected_sources",
    )
    _require(
        observed == [expected, expected],
        "declared session-source probes must replace the mutating operation callable",
    )


def _probe_dispatch_transaction(target: Path) -> Transaction:
    seed = SeedLock(
        "https://example.invalid/seed.git",
        "release-fixture",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "fixture",
    )
    return Transaction.create(
        name="probe-dispatch",
        target=target,
        input_fingerprint="sha256:" + "d" * 64,
        answers={},
        seed=seed,
        flow_id="fixture.flow",
        flow_source_revision="c" * 40,
        flow_contract_digest="sha256:" + "d" * 64,
        stage_ids=("fixture",),
        completion_probe_ids=("fixture",),
    )
