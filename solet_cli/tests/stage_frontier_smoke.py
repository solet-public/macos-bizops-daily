"""Red-first discriminators for stage probes and executable frontiers."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager import (
    operation_executor,  # noqa: E402
    stage_boundaries,  # noqa: E402
)
from solet_manager.contracts import (  # noqa: E402
    ContractBundle,
    active_decision_ids,
    validate_normalized_answers,
)
from solet_manager.decision_state import declined_answer, derive_decision_dispositions  # noqa: E402
from solet_manager.errors import (  # noqa: E402
    ContractError,
    ReopenUnsafeAppliedStateError,
    StateError,
)
from solet_manager.flow import (  # noqa: E402
    PlannedOperation,
    SetupPlan,
    current_frontier_stage_ids,
    initial_probe_activations,
    initial_stage_probe_statuses,
    reconcile_stage_probe_activation,
    validate_stage_probe_keys,
    validate_stage_probe_state,
)
from solet_manager.journal_migrations import activation_site_key  # noqa: E402
from solet_manager.journal_rollup import derive_stage_statuses  # noqa: E402
from solet_manager.models import CheckpointStatus, CommandResult, ExitCode  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.plan_builder import (  # noqa: E402
    _selected_option_input_refs,
    selected_operation_ids,
)
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.stage_boundaries import (  # noqa: E402
    BoundaryFailure,
    BoundaryProbeOutcome,
    run_stage_boundaries,
)
from solet_manager.transaction import Transaction  # noqa: E402

_CHECKS = 0
_CONTRACTS = (
    Path(__file__).resolve().parents[2] / "plugins" / "github_midwife_plugin" / "knowledge_base"
)


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


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _flow_at(flow: dict[str, object], *keys: str) -> dict[str, object]:
    current = flow
    for key in keys:
        current = _mapping(current.get(key), ".".join(keys))
    return current


def _contract_error(
    root: Path,
    name: str,
    mutation: Callable[[dict[str, object]], None],
) -> ContractError:
    directory = root / name
    shutil.copytree(_CONTRACTS, directory)
    flow_path = directory / "macos_setup_flow.json"
    flow = _mapping(json.loads(flow_path.read_text(encoding="utf-8")), name)
    mutation(flow)
    flow_path.write_text(json.dumps(flow, indent=2), encoding="utf-8")
    try:
        ContractBundle.load(source_revision="a" * 40, directory=directory)
    except ContractError as exc:
        return exc
    raise AssertionError(f"{name} mutation must fail contract loading")


def _unknown_resolution_stage(flow: dict[str, object]) -> None:
    _flow_at(flow, "decisions", "embedding_model")["resolution_stage_ref"] = "unknown_stage"


def _missing_resolution_stage(flow: dict[str, object]) -> None:
    del _flow_at(flow, "decisions", "embedding_model")["resolution_stage_ref"]


def _strand_inference_model(flow: dict[str, object]) -> None:
    option_paths = (
        ("decisions", "setup_profile", "option_source", "options", "macos-bizops"),
        ("decisions", "setup_profile", "option_source", "options", "custom"),
        (
            "decisions",
            "inference_implementation",
            "option_source",
            "options",
            "lm_studio",
        ),
        (
            "decisions",
            "inference_implementation",
            "option_source",
            "options",
            "alternative_plugin",
        ),
    )
    for path in option_paths:
        option = _flow_at(flow, *path)
        refs = option.get("followup_decision_refs")
        if not isinstance(refs, list):
            raise AssertionError(f"{'.'.join(path)} follow-ups must be an array")
        option["followup_decision_refs"] = [item for item in refs if item != "inference_model"]


def _late_required_when(flow: dict[str, object]) -> None:
    _flow_at(flow, "decisions", "coding_agents")["required_when"] = {
        "decision_ref": "embedding_model",
        "operator": "equals",
        "value": "embedding_model.recommended",
    }


def _undeclared_qualification_probe(flow: dict[str, object]) -> None:
    candidate = _flow_at(
        flow,
        "decisions",
        "embedding_model",
        "option_source",
        "candidate_contract",
    )
    refs = candidate.get("qualification_probe_refs")
    if not isinstance(refs, list):
        raise AssertionError("embedding_model qualification refs must be an array")
    candidate["qualification_probe_refs"] = [
        *refs,
        "undeclared_qualification_probe",
    ]


def _later_postcondition_remediation(
    operation_id: str,
    probe_id: str,
) -> Callable[[dict[str, object]], None]:
    def mutate(flow: dict[str, object]) -> None:
        idempotency = _flow_at(flow, "operations", operation_id, "idempotency")
        refs = idempotency.get("postcondition_probe_refs")
        if not isinstance(refs, list):
            raise AssertionError(f"{operation_id} postconditions must be an array")
        refs.append(probe_id)

    return mutate


def _seed() -> SeedLock:
    return SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-1",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "macos-bizops",
    )


def _answers(target: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "flow_id": "macos.repository_setup",
        "flow_source_revision": "a" * 40,
        "name": "bizops",
        "target": str(target),
        "public_inputs": {},
        "decisions": {
            "setup_profile": "macos-bizops",
            "autostart": "enabled",
            "embeddings_implementation": "lm_studio",
            "embedding_model": "fixture-embedding",
            "inference_implementation": "lm_studio",
            "inference_model": "fixture-inference",
            "coding_agents": ["codex", "claude_code"],
            "execution_topology": "fleet",
            "connector_configuration_timing": "first_use",
        },
        "consents": {},
        "resolution_evidence": [],
    }


def _assert_activation_contract(bundle: ContractBundle) -> None:
    free_active = active_decision_ids(
        bundle,
        {"setup_profile": "free", "autostart": "enabled"},
    )
    bizops_active = active_decision_ids(
        bundle,
        {"setup_profile": "macos-bizops", "autostart": "enabled"},
    )
    _check(
        free_active
        == {
            "setup_profile",
            "embeddings_implementation",
            "embedding_model",
            "coding_agents",
            "session_sources",
        },
        "free activation is exactly the selected profile follow-up closure",
    )
    _check(
        bizops_active
        == {
            "setup_profile",
            "embeddings_implementation",
            "embedding_model",
            "inference_implementation",
            "inference_model",
            "coding_agents",
            "execution_topology",
            "connector_configuration_timing",
            "session_sources",
        },
        "macos-bizops activation is exactly the selected profile follow-up closure",
    )
    _check(
        "autostart" not in free_active and "autostart" not in bizops_active,
        "dedicated autostart carrier is recordable without global activation",
    )
    inactive_selections = {
        "setup_profile": "macos-bizops",
        "autostart": "enabled",
        "connectors_to_configure": ["jira"],
        "git_mutation_control": "designated_controller",
    }
    _check(
        "configure_jira" not in selected_operation_ids(bundle, inactive_selections),
        "an inactive decision cannot activate an operation selection",
    )
    _check(
        "install_launchagent" in selected_operation_ids(bundle, inactive_selections),
        "independent autostart carrier remains eligible for operation selection",
    )
    _check(
        "git_controller_name" not in _selected_option_input_refs(bundle, inactive_selections),
        "an inactive decision cannot activate an option input selection",
    )
    _check(
        bundle.probes["git_checkout_valid"]["runner"] == "bootstrap",
        "checkout probe is pre-venv",
    )
    _check(
        all(
            field not in definition
            for registry in (bundle.probes, bundle.stages)
            for definition in registry.values()
            for field in ("failure_behavior", "restart_behavior", "live_required")
        ),
        "unimplemented lifecycle policies are absent from the executable contract",
    )
    _check(
        bundle.stages["decision_review"]["exit_probe_refs"] == ["decisions_resolved"],
        "decision review uses manager probe",
    )
    _check(bundle.probes["decisions_resolved"]["runner"] == "manager", "manager probe declared")


def _assert_launchagent_follows_models_contract(bundle: ContractBundle) -> None:
    genesis = _mapping(bundle.stages.get("genesis"), "genesis")
    models = _mapping(bundle.stages.get("models"), "models")
    genesis_operations = genesis.get("operation_refs")
    models_operations = models.get("operation_refs")
    genesis_exit_probes = genesis.get("exit_probe_refs")
    models_exit_probes = models.get("exit_probe_refs")
    _check(
        isinstance(genesis_operations, list)
        and "install_launchagent" not in genesis_operations,
        "genesis does not start the service before models configure inference",
    )
    _check(
        models_operations
        == [
            "configure_lm_studio_embeddings",
            "configure_lm_studio_inference",
            "install_launchagent",
        ],
        "models configures inference before starting the LaunchAgent",
    )
    _check(
        isinstance(genesis_exit_probes, list)
        and "launchagent_running" not in genesis_exit_probes
        and "router_ready" not in genesis_exit_probes,
        "genesis completion does not require the service that awaits models",
    )
    _check(
        models_exit_probes
        == ["embedding_request_succeeds", "launchagent_running", "router_ready"],
        "models verifies the configured service and router after it starts",
    )


def _assert_contract_rejections() -> None:
    with tempfile.TemporaryDirectory() as contract_raw:
        contract_root = Path(contract_raw)
        bad_callable = contract_root / "bad-callable"
        shutil.copytree(_CONTRACTS, bad_callable)
        callable_flow_path = bad_callable / "macos_setup_flow.json"
        callable_flow = json.loads(callable_flow_path.read_text(encoding="utf-8"))
        callable_flow["probes"]["git_checkout_valid"]["probe_ref"] = "Setup::git/verify"
        callable_flow_path.write_text(json.dumps(callable_flow, indent=2), encoding="utf-8")
        _raises(
            ContractError,
            lambda: ContractBundle.load(source_revision="a" * 40, directory=bad_callable),
            "bundle load rejects broad-schema callable refused by transport grammar",
        )

        bad_runner = contract_root / "bad-runner"
        shutil.copytree(_CONTRACTS, bad_runner)
        runner_flow_path = bad_runner / "macos_setup_flow.json"
        runner_flow = json.loads(runner_flow_path.read_text(encoding="utf-8"))
        runner_flow["probes"]["git_checkout_valid"]["runner"] = "operator"
        runner_flow_path.write_text(json.dumps(runner_flow, indent=2), encoding="utf-8")
        _raises(
            ContractError,
            lambda: ContractBundle.load(source_revision="a" * 40, directory=bad_runner),
            "bundle load rejects boundary probe without a reviewed runner path",
        )

        unknown_stage_error = _contract_error(
            contract_root,
            "unknown-resolution-stage",
            _unknown_resolution_stage,
        )
        _check(
            "missing or unknown resolution_stage_ref" in str(unknown_stage_error),
            "bundle load rejects an unknown decision resolution stage",
        )
        missing_stage_error = _contract_error(
            contract_root,
            "missing-resolution-stage",
            _missing_resolution_stage,
        )
        _check(
            "missing or unknown resolution_stage_ref" in str(missing_stage_error),
            "bundle load rejects a missing decision resolution stage",
        )
        unreachable_error = _contract_error(
            contract_root,
            "unreachable-inference-model",
            _strand_inference_model,
        )
        _check(
            "unreachable" in str(unreachable_error) and "inference_model" in str(unreachable_error),
            "bundle load rejects an inference model stranded by follow-up edits",
        )
        later_condition_error = _contract_error(
            contract_root,
            "later-required-when",
            _late_required_when,
        )
        _check(
            "coding_agents" in str(later_condition_error)
            and "decision_review" in str(later_condition_error)
            and "embedding_model" in str(later_condition_error)
            and "models" in str(later_condition_error),
            "bundle load names both stages for a later required_when decision",
        )
        undeclared_probe_error = _contract_error(
            contract_root,
            "undeclared-qualification-probe",
            _undeclared_qualification_probe,
        )
        _check(
            "undeclared probe" in str(undeclared_probe_error)
            and "undeclared_qualification_probe" in str(undeclared_probe_error),
            "bundle load rejects an undeclared qualification probe",
        )


def _assert_postcondition_scope_contract(bundle: ContractBundle) -> None:
    controls = (
        ("configure_postgresql", "pgvector_ready"),
        ("install_shell_integration", "fresh_shell_python_valid"),
    )
    for operation_id, probe_id in controls:
        idempotency = _flow_at(bundle.flow, "operations", operation_id, "idempotency")
        refs = idempotency.get("postcondition_probe_refs")
        _check(
            isinstance(refs, list) and probe_id in refs,
            f"correct postcondition {operation_id}->{probe_id} loads without a later remediation",
        )

    precondition_subset_controls = (
        (
            "configure_postgresql",
            ("postgres_role_policy_valid", "postgres_ready", "pgvector_ready"),
        ),
        (
            "install_shell_integration",
            ("fresh_shell_path_valid", "fresh_shell_python_valid"),
        ),
        ("configure_lm_studio_embeddings", ("embedding_model_qualification",)),
        ("configure_lm_studio_inference", ()),
    )
    for operation_id, expected in precondition_subset_controls:
        idempotency = _flow_at(bundle.flow, "operations", operation_id, "idempotency")
        refs = idempotency.get("precondition_probe_refs")
        _check(
            refs == list(expected),
            f"{operation_id} precondition is the full postcondition predicate",
        )

    violations = (
        ("install_postgresql", "pgvector_ready", "configure_postgresql"),
        ("install_python_runtime", "fresh_shell_python_valid", "install_shell_integration"),
        ("open_background_items_settings", "launchagent_running", "install_launchagent"),
    )
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        for operation_id, probe_id, later_operation in violations:
            error = _contract_error(
                root,
                f"later-postcondition-{operation_id}",
                _later_postcondition_remediation(operation_id, probe_id),
            )
            _check(
                operation_id in str(error)
                and probe_id in str(error)
                and later_operation in str(error),
                f"bundle load rejects later postcondition remediation {operation_id}->{probe_id}",
            )


def _remediation_plan(bundle: ContractBundle, answers: dict[str, object]) -> SetupPlan:
    definition = bundle.operations["configure_postgresql"]
    idempotency = _mapping(definition.get("idempotency"), "configure_postgresql")
    return SetupPlan(
        answers=answers,  # type: ignore[arg-type]
        operations=(
            PlannedOperation(
                stage_id="system_dependencies",
                operation_id="configure_postgresql",
                operation_ref=str(definition["operation_ref"]),
                runner=str(definition["runner"]),
                risk=str(definition["risk"]),
                requires_confirmation=bool(definition["requires_confirmation"]),
                precondition_probe_ids=tuple(
                    cast(list[str], idempotency["precondition_probe_refs"])
                ),
                postcondition_probe_ids=tuple(
                    cast(list[str], idempotency["postcondition_probe_refs"])
                ),
                public_inputs={},
            ),
        ),
        unresolved_decisions=(),
        unresolved_consents=(),
    )


def _remediation_preview() -> CommandResult:
    return CommandResult(
        kind="create_preview",
        status="preview_ready",
        message="fixture remediation preview",
        exit_code=ExitCode.OK,
        error_kind=None,
        repair=None,
        data={"approval_fingerprint": "sha256:" + "f" * 64},
    )


def _assert_boundary_remediation(
    bundle: ContractBundle,
    transaction: Transaction,
    answers: dict[str, object],
    root: Path,
) -> None:
    with (
        patch.object(
            stage_boundaries,
            "_boundary_probe_ids",
            return_value=("launchagent_running",),
        ),
        patch.object(
            stage_boundaries,
            "_run_boundary_probe",
            return_value=BoundaryProbeOutcome(transaction, {}, True),
        ),
    ):
        _updated, _observations, permission_failures = run_stage_boundaries(
            bundle=bundle,
            transaction=transaction,
            registry=None,
            stage_ids=("models",),
            boundary="exit",
            answers=answers,
            persist_path=None,
        )
    _check(
        len(permission_failures) == 1
        and permission_failures[0].stage_id == "models"
        and permission_failures[0].probe_id == "launchagent_running"
        and permission_failures[0].remediation_operation_ids == ("install_launchagent",),
        "a failed launch-agent boundary prefers its declared repair over Settings navigation",
    )
    with (
        patch.object(
            stage_boundaries,
            "_boundary_probe_ids",
            return_value=("postgres_ready",),
        ),
        patch.object(
            stage_boundaries,
            "_run_boundary_probe",
            return_value=BoundaryProbeOutcome(transaction, {}, True),
        ),
    ):
        _updated, _observations, postgres_entry_failures = run_stage_boundaries(
            bundle=bundle,
            transaction=transaction,
            registry=None,
            stage_ids=("system_dependencies",),
            boundary="entry",
            answers=answers,
            persist_path=None,
        )
    _check(
        len(postgres_entry_failures) == 1
        and postgres_entry_failures[0].remediation_operation_ids
        == ("install_postgresql", "configure_postgresql"),
        "a declared probe remediation is available at an entry boundary",
    )
    remediation_paths = ManagerPaths(
        root / "remediation-config",
        root / "remediation-state",
        root / "remediation-cache",
    )
    remediation_paths.transactions_dir.mkdir(parents=True)
    remediation_paths.transactions_dir.chmod(0o700)
    postgres_failure = BoundaryFailure(
        identity="system_dependencies:exit:postgres_ready",
        stage_id="system_dependencies",
        boundary="exit",
        probe_id="postgres_ready",
        remediation_operation_ids=("configure_postgresql",),
    )
    with patch.object(
        operation_executor,
        "run_stage_boundaries",
        return_value=(transaction, {}, [postgres_failure]),
    ):
        exit_outcome = operation_executor._finish_operation_stages(
            bundle=bundle,
            plan=_remediation_plan(bundle, answers),
            transaction=transaction,
            registry=None,
            paths=remediation_paths,
            refresh_preview=_remediation_preview,
        )
    _check(
        exit_outcome.terminal_result is not None
        and exit_outcome.terminal_result.error_kind == "stage_boundary_remediation_required"
        and exit_outcome.transaction.result_kind == "stage_boundary_remediation_required",
        "a remediable exit boundary renders a new approval-required preview",
    )
    with (
        patch.object(
            operation_executor,
            "reconcile_stage_probe_activation",
            return_value=transaction,
        ),
        patch.object(
            operation_executor,
            "run_stage_boundaries",
            side_effect=[(transaction, {}, []), (transaction, {}, [postgres_failure])],
        ),
    ):
        resume_outcome = operation_executor._cross_read_only_frontier(
            bundle=bundle,
            transaction=transaction,
            plan=SetupPlan(
                answers=answers,  # type: ignore[arg-type]
                operations=(),
                unresolved_decisions=(),
                unresolved_consents=(),
            ),
            frontier=("system_dependencies",),
            registry=None,
            paths=remediation_paths,
            refresh_preview=_remediation_preview,
        )
    _check(
        resume_outcome.terminal_result is not None
        and resume_outcome.terminal_result.error_kind == "stage_boundary_remediation_required",
        "a resumed read-only exit boundary renders its declared remediation preview",
    )
    with (
        patch.object(
            stage_boundaries,
            "_boundary_probe_ids",
            return_value=("launchagent_running",),
        ),
        patch.object(
            stage_boundaries,
            "_run_boundary_probe",
            return_value=BoundaryProbeOutcome(transaction, {}, False),
        ),
    ):
        _updated, _observations, successful_failures = run_stage_boundaries(
            bundle=bundle,
            transaction=transaction,
            registry=None,
            stage_ids=("models",),
            boundary="exit",
            answers=answers,
            persist_path=None,
        )
    _check(
        successful_failures == [],
        "a successful permission boundary emits no settings remediation",
    )


def _initial_transaction(
    bundle: ContractBundle,
    target: Path,
    answers: dict[str, object],
) -> tuple[Transaction, dict[str, object]]:
    stage_probes = initial_stage_probe_statuses(bundle, answers)
    guarded_connector_probes = (
        "google_workspace_connection_valid",
        "marketo_connection_valid",
        "salesforce_connection_valid",
        "schwab_connection_valid",
        "snowflake_connection_valid",
        "zuora_connection_valid",
        "external_postgres_connection_valid",
    )
    probe_activations = initial_probe_activations(bundle, answers)
    _check(
        all(
            stage_probes["optional_accounts"]["exit"][probe_id] is CheckpointStatus.NOT_APPLICABLE
            and probe_activations[activation_site_key("optional_accounts", "exit", probe_id)][
                "state"
            ]
            == "inactive"
            and probe_activations[activation_site_key("optional_accounts", "exit", probe_id)][
                "reason"
            ]
            == "decision_unselected"
            for probe_id in guarded_connector_probes
        ),
        "inactive connector probes retain pending outcomes with explicit activation causes",
    )
    active_unresolved_answers = deepcopy(answers)
    active_unresolved_answers["decisions"]["connector_configuration_timing"] = "configure_now"
    active_unresolved_statuses = initial_stage_probe_statuses(bundle, active_unresolved_answers)
    active_unresolved_activations = initial_probe_activations(bundle, active_unresolved_answers)
    _check(
        all(
            active_unresolved_statuses["optional_accounts"]["exit"][probe_id]
            is CheckpointStatus.PENDING
            and active_unresolved_activations[
                activation_site_key("optional_accounts", "exit", probe_id)
            ]["state"]
            == "active"
            for probe_id in guarded_connector_probes
        ),
        "an activated but unresolved connector decision remains conservative",
    )
    newly_introduced_activations = initial_probe_activations(
        bundle,
        active_unresolved_answers,
        prior_decision_ids=frozenset(
            set(active_unresolved_answers["decisions"]) - {"connectors_to_configure"}
        ),
    )
    _check(
        newly_introduced_activations[
            activation_site_key("optional_accounts", "exit", "google_workspace_connection_valid")
        ]["reason"]
        == "newly_introduced_pending",
        "a newly introduced active decision drives its probe activation reason",
    )
    return (
        Transaction.create(
            name="bizops",
            target=target,
            input_fingerprint="sha256:" + "1" * 64,
            answers=answers,
            seed=_seed(),
            flow_id=bundle.flow_id,
            flow_source_revision=bundle.source_revision,
            flow_contract_digest=bundle.contract_digest,
            stage_ids=tuple(bundle.stages),
            stage_probe_statuses=stage_probes,
            probe_activations=probe_activations,
            completion_probe_ids=bundle.completion_probe_ids,
        ),
        active_unresolved_answers,
    )


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    _assert_activation_contract(bundle)
    _assert_launchagent_follows_models_contract(bundle)
    _assert_contract_rejections()
    _assert_postcondition_scope_contract(bundle)

    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "Solets" / "bizops"
        answers = _answers(target)
        transaction, active_unresolved_answers = _initial_transaction(bundle, target, answers)
        serialized = transaction.to_dict()
        _check("stage_probe_statuses" in serialized, "journal serializes stage probe statuses")
        _check("stage_probe_attempts" in serialized, "journal serializes stage probe attempts")
        _check(
            set(serialized["stage_probe_statuses"]["preflight"]["entry"])
            == {"minimum_physical_memory_valid", "git_checkout_valid"}
            and set(serialized["stage_probe_statuses"]["preflight"]["exit"])
            == {"python_version_valid"},
            "preflight separates host-and-checkout entry probes from runtime exit keys",
        )
        _check(
            current_frontier_stage_ids(bundle, transaction, answers) == ("preflight",),
            "only preflight is initially executable",
        )
        inactive_dependency_bundle = deepcopy(bundle)
        inactive_dependency_bundle.stages["preflight"]["required_when"] = {
            "decision_ref": "setup_profile",
            "operator": "equals",
            "value": "free",
        }
        inactive_dependency_statuses = initial_stage_probe_statuses(
            inactive_dependency_bundle,
            answers,
        )
        inactive_dependency_transaction = Transaction.create(
            name="bizops",
            target=target,
            input_fingerprint="sha256:" + "1" * 64,
            answers=answers,
            seed=_seed(),
            flow_id=inactive_dependency_bundle.flow_id,
            flow_source_revision=inactive_dependency_bundle.source_revision,
            flow_contract_digest=inactive_dependency_bundle.contract_digest,
            stage_ids=tuple(inactive_dependency_bundle.stages),
            stage_probe_statuses=inactive_dependency_statuses,
            completion_probe_ids=inactive_dependency_bundle.completion_probe_ids,
        ).with_statuses(
            stages={
                "preflight": CheckpointStatus.NOT_APPLICABLE,
                **{
                    stage_id: CheckpointStatus.PENDING
                    for stage_id in inactive_dependency_bundle.stages
                    if stage_id != "preflight"
                },
            }
        )
        _check(
            current_frontier_stage_ids(
                inactive_dependency_bundle,
                inactive_dependency_transaction,
                answers,
            )
            == ("decision_review",),
            "a dependent stage is executable when its inactive dependency is not applicable",
        )
        _raises(
            StateError,
            lambda: derive_stage_statuses(
                {"known": CheckpointStatus.PENDING},
                {"known": {"entry": {}, "exit": {}}},
                {"unknown-operation": "unknown"},
                {"unknown-operation": CheckpointStatus.PENDING},
            ),
            "unknown operation stage raises StateError rather than KeyError",
        )
        _assert_boundary_remediation(bundle, transaction, answers, Path(raw))
        ram_verified = transaction.with_stage_probe_status(
            "preflight",
            "entry",
            "minimum_physical_memory_valid",
            CheckpointStatus.VERIFIED,
            attempt={
                "probe_id": "minimum_physical_memory_valid",
                "stage_id": "preflight",
                "boundary": "entry",
                "attempt": 1,
                "request_id": "7f2f3ed3-03fc-4f58-915e-eb400a172a67",
                "checkpoint_status": "verified",
                "error_kind": None,
                "retry_safe": True,
                "evidence": [],
                "repair": None,
                "recorded_at": "2026-08-21T03:29:00Z",
            },
        )
        _check(
            ram_verified.stages["preflight"] is CheckpointStatus.PENDING,
            "one verified entry cannot verify a stage with another pending entry",
        )
        first = ram_verified.with_stage_probe_status(
            "preflight",
            "entry",
            "git_checkout_valid",
            CheckpointStatus.VERIFIED,
            attempt={
                "probe_id": "git_checkout_valid",
                "stage_id": "preflight",
                "boundary": "entry",
                "attempt": 1,
                "request_id": "8f2f3ed3-03fc-4f58-915e-eb400a172a67",
                "checkpoint_status": "verified",
                "error_kind": None,
                "retry_safe": True,
                "evidence": [],
                "repair": None,
                "recorded_at": "2026-08-21T03:30:00Z",
            },
        )
        _check(
            first.stages["preflight"] is CheckpointStatus.PENDING,
            "verified entries cannot verify a stage with a pending exit",
        )
        transaction = first.with_stage_probe_status(
            "preflight",
            "exit",
            "python_version_valid",
            CheckpointStatus.VERIFIED,
            attempt={
                "probe_id": "python_version_valid",
                "stage_id": "preflight",
                "boundary": "exit",
                "attempt": 1,
                "request_id": "9f2f3ed3-03fc-4f58-915e-eb400a172a67",
                "checkpoint_status": "verified",
                "error_kind": None,
                "retry_safe": True,
                "evidence": [],
                "repair": None,
                "recorded_at": "2026-08-21T03:31:00Z",
            },
        )
        _check(
            transaction.stages["preflight"] is CheckpointStatus.VERIFIED,
            "no-operation stage needs exits",
        )
        _check(
            current_frontier_stage_ids(bundle, transaction, answers) == ("decision_review",),
            "frontier advances only after dependency verification",
        )
        encoded = json.dumps(transaction.to_dict(), sort_keys=True)
        _check("not_yet_executable" not in encoded, "presentation-only state never enters journal")
        canonical = json.dumps(transaction.to_dict(), sort_keys=True, separators=(",", ":"))
        _check(
            json.dumps(
                Transaction.from_dict(transaction.to_dict()).to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
            == canonical,
            "valid stage journal round-trips byte-identically",
        )
        _raises(
            StateError,
            lambda: transaction.with_stage_probe_status(
                "preflight",
                "entry",
                "git_checkout_valid",
                CheckpointStatus.VERIFIED,
                attempt=transaction.stage_probe_attempts[0],
            ),
            "duplicate full stage attempt tuple is rejected",
        )

        missing_key = initial_stage_probe_statuses(bundle, answers)
        del missing_key["preflight"]["entry"]["git_checkout_valid"]
        _raises(
            StateError,
            lambda: validate_stage_probe_keys(bundle, missing_key),
            "missing declared stage probe key is rejected",
        )
        extra_key = initial_stage_probe_statuses(bundle, answers)
        extra_key["preflight"]["exit"]["undeclared"] = CheckpointStatus.PENDING
        _raises(
            StateError,
            lambda: validate_stage_probe_keys(bundle, extra_key),
            "extra stage probe key is rejected",
        )
        inactive_mismatch = Transaction.create(
            name="bizops",
            target=target,
            input_fingerprint="sha256:" + "1" * 64,
            answers=answers,
            seed=_seed(),
            flow_id=bundle.flow_id,
            flow_source_revision=bundle.source_revision,
            flow_contract_digest=bundle.contract_digest,
            stage_ids=tuple(bundle.stages),
            stage_probe_statuses=initial_stage_probe_statuses(bundle, answers),
            completion_probe_ids=bundle.completion_probe_ids,
        ).with_probe_activations(
            {
                **initial_probe_activations(bundle, answers),
                activation_site_key("preflight", "entry", "git_checkout_valid"): {
                    "state": "inactive",
                    "reason": "decision_unselected",
                    "decided_by": "setup_profile",
                },
            }
        )
        _raises(
            StateError,
            lambda: validate_stage_probe_state(bundle, inactive_mismatch, answers),
            "activation carrier inconsistent with declared conditions is rejected",
        )
        reason_activations = initial_probe_activations(bundle, active_unresolved_answers)
        reason_activations[activation_site_key("preflight", "entry", "git_checkout_valid")] = {
            "state": "active",
            "reason": "plan_bound",
            "decided_by": "plan",
        }
        reason_activations[activation_site_key("models", "exit", "launchagent_running")] = {
            "state": "active",
            "reason": "decision_selected",
            "decided_by": "autostart",
        }
        reason_activations[
            activation_site_key("optional_accounts", "exit", "google_workspace_connection_valid")
        ] = {
            "state": "inactive",
            "reason": "decision_unselected",
            "decided_by": "connectors_to_configure",
        }
        reason_activations[
            activation_site_key("optional_accounts", "exit", "jira_connection_valid")
        ] = {"state": "inactive", "reason": "plan_dropped", "decided_by": "plan"}
        reason_activations[
            activation_site_key("optional_accounts", "exit", "marketo_connection_valid")
        ] = {
            "state": "active",
            "reason": "newly_introduced_pending",
            "decided_by": "connectors_to_configure",
        }
        five_reason_transaction = Transaction.create(
            name="bizops",
            target=target,
            input_fingerprint="sha256:" + "1" * 64,
            answers=active_unresolved_answers,
            seed=_seed(),
            flow_id=bundle.flow_id,
            flow_source_revision=bundle.source_revision,
            flow_contract_digest=bundle.contract_digest,
            stage_ids=tuple(bundle.stages),
            stage_probe_statuses=initial_stage_probe_statuses(bundle, active_unresolved_answers),
            probe_activations=reason_activations,
            completion_probe_ids=bundle.completion_probe_ids,
        )
        _check(
            {
                item["reason"]
                for item in Transaction.from_dict(
                    five_reason_transaction.to_dict()
                ).probe_activations.values()
            }
            >= {
                "plan_bound",
                "decision_selected",
                "decision_unselected",
                "plan_dropped",
                "newly_introduced_pending",
            },
            "all five closed probe activation reasons round-trip through v3",
        )
        unsafe = transaction.bind_operations(
            {"configure_google_workspace": "optional_accounts"}
        ).with_operation_status("configure_google_workspace", CheckpointStatus.APPLIED)
        unsafe = unsafe.with_statuses(
            stages={**unsafe.stages, "optional_accounts": CheckpointStatus.VERIFIED}
        )
        _raises(
            ReopenUnsafeAppliedStateError,
            lambda: reconcile_stage_probe_activation(bundle, unsafe, active_unresolved_answers),
            "newly active probe refuses reopening a verified stage with applied work",
        )
        probe_only = transaction.with_statuses(
            stages={**transaction.stages, "optional_accounts": CheckpointStatus.VERIFIED}
        )
        reopened = reconcile_stage_probe_activation(bundle, probe_only, active_unresolved_answers)
        _check(
            reopened.stages["optional_accounts"] is CheckpointStatus.PENDING,
            "probe-only reopening is allowed and reopens the verified stage",
        )
        declined_answers = deepcopy(answers)
        declined_answers["decisions"]["connector_configuration_timing"] = declined_answer(
            decided_at="2026-09-04T08:20:00Z", decided_by="interactive"
        )
        validate_normalized_answers(bundle, declined_answers)
        _check(
            derive_decision_dispositions(bundle, declined_answers["decisions"])[
                "connector_configuration_timing"
            ]
            == "declined",
            "explicit declined carrier derives declined",
        )
        absent_answers = deepcopy(answers)
        del absent_answers["decisions"]["connector_configuration_timing"]
        _check(
            derive_decision_dispositions(bundle, absent_answers["decisions"])[
                "connector_configuration_timing"
            ]
            != "declined",
            "absent answer never infers declined",
        )
        stale = transaction.to_dict()
        stale["stage_probe_statuses"]["preflight"]["entry"][  # type: ignore[index]
            "git_checkout_valid"
        ] = "pending"
        _raises(
            StateError,
            lambda: Transaction.from_dict(stale),
            "stale stage status disagreeing with latest attempt is rejected",
        )
        invalid_request_id = transaction.to_dict()
        invalid_request_id["stage_probe_attempts"][0][  # type: ignore[index]
            "request_id"
        ] = "not-a-uuid"
        _raises(
            StateError,
            lambda: Transaction.from_dict(invalid_request_id),
            "corrupt stage attempt with non-UUID request id is rejected",
        )
        missing_attempt = transaction.to_dict()
        missing_attempt["stage_probe_attempts"] = [
            attempt
            for attempt in missing_attempt["stage_probe_attempts"]  # type: ignore[union-attr]
            if attempt["probe_id"] != "git_checkout_valid"  # type: ignore[index]
        ]
        _raises(
            StateError,
            lambda: Transaction.from_dict(missing_attempt),
            "verified stage status without an attempt is rejected",
        )

        validate_normalized_answers(bundle, answers)
        free_answers = _answers(target)
        free_answers["decisions"] = {
            "setup_profile": "free",
            "autostart": "enabled",
            "embeddings_implementation": "lm_studio",
            "embedding_model": "fixture-embedding",
            "coding_agents": ["codex", "claude_code"],
            "session_sources": ["codex_local", "claude_code_local"],
            "execution_topology": "solo",
        }
        _raises(
            ContractError,
            lambda: validate_normalized_answers(bundle, free_answers),
            "inactive carrier is rejected while dedicated autostart is accepted",
        )

    print(f"stage_frontier_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
