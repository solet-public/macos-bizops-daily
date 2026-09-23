"""Scenario groups for discovered decisions, resumable create, doctor, and start."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import cast
from unittest.mock import patch

import solet_manager.doctor_inference_qualification as doctor_inference_qualification_module
import solet_manager.lifecycle as lifecycle_module
from discovered_decision_barrier_scenarios import run_barrier_scenario
from discovered_decision_entry_fixture import prepare_with_decision_review_entry_probe
from discovered_decision_projection_scenarios import (
    MODEL_OPERATION_INPUTS as _MODEL_OPERATION_INPUTS,
)
from discovered_decision_projection_scenarios import (
    MODEL_SELECTIONS as _MODEL_SELECTIONS,
)
from discovered_decision_projection_scenarios import (
    run_projection_scenarios,
)
from discovered_decision_support import (
    _CONTRACTS,
    FakeAdapter,
    VerifiedContext,
    _advance_to_model_review,
    _apply_current_frontier,
    _check,
    _consents_are_rendered,
    _crash_resume_is_safe,
    _decision_probe_attempts,
    _doctor_status_cause_is_precise,
    _entry_failure_is_safe,
    _exit_resume_is_safe,
    _find_request,
    _finish_from_model_review,
    _initial_stages_verified,
    _make_hydration_vector,
    _model_decisions_are_visible,
    _no_future_decision_requests,
    _persisted_consents_are_true,
    _prepare,
    _purity_stop_is_clean,
    _raises,
    _set_invoke_adapter,
    _set_plan_builder,
    _set_write_transaction,
    _stage_progress_is_typed,
    _stage_request_is_valid,
    _start_request_is_bound,
    _state_changing_read_only_plan,
    _status_rendering_matches,
    create_execution_module,
    decision_discovery_module,
    operation_executor_module,
    preview_engine_module,
)
from solet_manager.adapters import OperationRequest, OperationResult
from solet_manager.config import CreateConfig
from solet_manager.contracts import ContractBundle, target_contract_directory
from solet_manager.create import CreateManager
from solet_manager.decision_prompt import prompt_discovered_decisions
from solet_manager.doctor import InstallationDoctor
from solet_manager.errors import ContractError, ProbeDriftError, StateError
from solet_manager.flow import SetupPlan, initial_stage_probe_statuses
from solet_manager.lifecycle import LifecycleManager
from solet_manager.models import CheckpointStatus, CommandResult, JsonValue
from solet_manager.paths import ManagerPaths
from solet_manager.state_io import atomic_write_json
from solet_manager.transaction import (
    Transaction,
    canonical_sha256,
    load_transaction,
    write_transaction,
)


def _rendered_model_operation_inputs(value: object) -> dict[str, object]:
    if not isinstance(value, list):
        return {}
    return {
        str(item["operation_id"]): item.get("public_inputs")
        for item in value
        if isinstance(item, dict) and item.get("operation_id") in _MODEL_OPERATION_INPUTS
    }


def _recorded_model_request_inputs(
    fake: FakeAdapter,
    *,
    phase: str,
    purpose: str | None,
) -> dict[str, object]:
    return {
        request.operation_id: request.public_inputs
        for request in fake.requests
        if request.operation_id in _MODEL_OPERATION_INPUTS
        and request.phase == phase
        and request.probe_purpose == purpose
    }


def _has_no_model_operation_requests(fake: FakeAdapter) -> bool:
    return not any(request.operation_id in _MODEL_OPERATION_INPUTS for request in fake.requests)


def _assert_pre_model_projection(
    initial: CommandResult,
    fake: FakeAdapter,
) -> None:
    data = initial.data
    answers = cast(dict[str, object], data["normalized_answers"])
    public_inputs = cast(dict[str, object], answers["public_inputs"])
    _check(
        public_inputs.get("lm_studio_base_url") == "http://localhost:1234/v1"
        and not set(_rendered_model_operation_inputs(data["operations"]))
        and _has_no_model_operation_requests(fake),
        "system-dependencies frontier projects the provisioning URL without selecting or configuring models early",
    )


def _assert_reviewed_model_projection(
    reviewed: CommandResult,
    fake: FakeAdapter,
) -> None:
    data = reviewed.data
    _check(
        _rendered_model_operation_inputs(data["operations"]) == _MODEL_OPERATION_INPUTS,
        "planned model operations carry exact operation-local public inputs",
    )
    _check(
        _recorded_model_request_inputs(fake, phase="probe", purpose="preview")
        == _MODEL_OPERATION_INPUTS,
        "real model preview requests carry exact operation-local public inputs",
    )
    answers = cast(dict[str, object], data["normalized_answers"])
    fingerprint = canonical_sha256(answers)  # type: ignore[arg-type]
    _check(
        all(
            request.answers_fingerprint == fingerprint
            for request in fake.requests
            if request.operation_id in _MODEL_OPERATION_INPUTS
            and request.probe_purpose == "preview"
        ),
        "real model preview request fingerprint binds projected normalized answers",
    )


def _assert_applied_model_projection(fake: FakeAdapter) -> None:
    _check(
        _recorded_model_request_inputs(fake, phase="probe", purpose="pre_apply")
        == _MODEL_OPERATION_INPUTS,
        "real model pre-apply requests preserve exact operation-local public inputs",
    )
    _check(
        _recorded_model_request_inputs(fake, phase="apply", purpose=None)
        == _MODEL_OPERATION_INPUTS,
        "real model apply requests preserve exact operation-local public inputs",
    )


def _assert_embedding_qualification_dimensions(fake: FakeAdapter) -> None:
    inputs = [
        request.public_inputs
        for request in fake.requests
        if request.probe_purpose == "decision_qualification"
        and request.public_inputs.get("decision_id") == "embedding_model"
    ]
    _check(
        all("expected_dimension" not in item for item in inputs),
        "embedding qualification leaves platform expectation sourcing at the adapter seam",
    )


def _static_current_frontier_barrier_scenario(root: Path) -> None:
    paths, config, manager, transaction = _prepare(root, name="staticbarrier")
    fake = FakeAdapter()
    _set_invoke_adapter(fake)
    lifecycle_module.invoke_adapter = fake
    answers = dict(transaction.answers)
    decisions = dict(cast(dict[str, object], answers["decisions"]))
    decisions.pop("inference_implementation", None)
    answers["decisions"] = decisions
    write_transaction(
        paths.transaction_path(config.name),
        transaction.with_answers(answers),  # type: ignore[arg-type]
    )
    missing_static_config = CreateConfig(
        name=config.name,
        target=config.target,
        autostart=config.autostart,
        decisions={},
        decision_sources={},
    )
    preview = manager.preview(missing_static_config)
    create = manager.create(
        missing_static_config,
        approved_fingerprint="irrelevant",
    )
    prompt_ids = {
        str(item["id"]) for item in cast(list[dict[str, object]], preview.data["decision_prompts"])
    }
    _check(
        preview.status == "awaiting_user"
        and preview.error_kind == "decisions_required"
        and "inference_implementation" in cast(list[str], preview.data["unresolved_decisions"])
        and "inference_implementation" in prompt_ids
        and create.status == "awaiting_user"
        and create.error_kind == "decisions_required"
        and not any(request.phase == "apply" for request in fake.requests),
        "missing current static decision is refused before persistent effect",
    )


def _verified_scenario(
    root: Path,
    original_plan_builder: object,
) -> VerifiedContext:
    paths, config, manager, _transaction = _prepare(root, name="bizops")
    fake = FakeAdapter()
    _set_invoke_adapter(fake)
    lifecycle_module.invoke_adapter = fake
    selections = dict(_MODEL_SELECTIONS)

    fresh = manager.preview(config)
    fresh_unresolved = set(cast(list[str], fresh.data["unresolved_decisions"]))
    fresh_model_prompts = {
        str(item["id"])
        for item in cast(list[dict[str, object]], fresh.data["decision_prompts"])
        if item.get("id") in {"embedding_model", "inference_model"}
    }
    _check(
        fresh.status == "preview_ready"
        and fresh.data["frontier"] == ["system_dependencies"]
        and not fresh_unresolved.intersection({"embedding_model", "inference_model"})
        and not fresh_model_prompts
        and not any(
            request.public_inputs.get("decision_id") in {"embedding_model", "inference_model"}
            for request in fake.requests
        ),
        "fresh pre-model preview is actionable without model discovery or blocking",
    )
    fake.requests.clear()
    initial = manager.preview(config, decision_selections=selections)
    _check(initial.status == "preview_ready", "first executable frontier is preview-ready")
    _check(
        initial.data["frontier"] == ["system_dependencies"],
        "future model decisions do not block the dependency frontier",
    )
    _check(
        _initial_stages_verified(initial),
        "pure carrier-resolved read-only stages auto-advance before mutation review",
    )
    _check(
        not {
            request.probe_purpose
            for request in fake.requests
            if request.public_inputs.get("decision_id")
            in {
                "embedding_model",
                "inference_model",
            }
        },
        "future model discovery is deferred before the first mutation frontier",
    )
    _assert_pre_model_projection(initial, fake)
    _purity_scenario(root, original_plan_builder, selections)

    _set_invoke_adapter(fake)
    checkout_boundary = _find_request(
        fake,
        operation_id="git_checkout_valid",
        purpose="stage_entry",
    )
    _check(
        _stage_request_is_valid(checkout_boundary),
        "stage probe transport uses probe id, callable ref, and UUID request id",
    )
    fake.requests.clear()
    dependencies_result = manager.create(
        config,
        approved_fingerprint=str(initial.data["approval_fingerprint"]),
        decision_selections=selections,
    )
    _check(
        dependencies_result.error_kind == "next_stage_preview_required",
        "dependency mutation requires a fresh preview for genesis",
    )
    fake.requests.clear()
    replayed = manager.create(
        config,
        approved_fingerprint=str(initial.data["approval_fingerprint"]),
        decision_selections=selections,
    )
    _check(
        replayed.error_kind == "approval_stale"
        and not any(request.phase == "apply" for request in fake.requests),
        "preceding-frontier stale approval stops before mutation",
    )
    _genesis, genesis_result = _apply_current_frontier(
        manager,
        config,
        decision_selections=selections,
    )
    _check(
        genesis_result.error_kind == "next_stage_preview_required",
        "genesis mutation requires a fresh preview for models",
    )

    fake.requests.clear()
    awaiting_models = manager.preview(config)
    model_prompt_ids = {
        str(item["id"])
        for item in cast(list[dict[str, object]], awaiting_models.data["decision_prompts"])
    }
    named_required = set(cast(list[str], awaiting_models.data["unresolved_decisions"]))
    _check(
        awaiting_models.status == "awaiting_user"
        and awaiting_models.error_kind == "decisions_required"
        and model_prompt_ids == {"embedding_model", "inference_model"}
        and named_required.issubset(model_prompt_ids),
        "models-stage decision-required result names a prompt for every required decision",
    )
    with patch("builtins.input", side_effect=["1", "1"]):
        interactive_selections = prompt_discovered_decisions(
            awaiting_models,
            dict(config.decisions),
        )
    interactive = manager.preview(
        config,
        decision_selections=interactive_selections,
        decision_source="interactive",
    )
    interactive_answers = cast(dict[str, object], interactive.data["normalized_answers"])
    interactive_decisions = cast(dict[str, object], interactive_answers["decisions"])
    _check(
        interactive.status == "preview_ready"
        and interactive_decisions.get("embedding_model") == "embedding_model.recommended"
        and interactive_decisions.get("inference_model") == "inference_model.recommended",
        "models-stage prompts produce recorded interactive model selections",
    )
    fake.requests.clear()
    reviewed = manager.preview(config, decision_selections=selections)
    _check(
        reviewed.status == "preview_ready" and reviewed.data["frontier"] == ["models"],
        "qualified exact choices reach the model preview",
    )
    _check(bool(reviewed.data["planned_actions"]), "host planned actions rendered")
    _check(
        _model_decisions_are_visible(reviewed),
        "current and prior active decisions are visible without inactive followups",
    )
    _check(
        _consents_are_rendered(reviewed),
        "exact consent terms and mutations rendered",
    )
    _assert_embedding_qualification_dimensions(fake)
    _assert_reviewed_model_projection(reviewed, fake)

    fake.label_suffix = "v2"
    label_changed = manager.preview(config, decision_selections=selections)
    _check(
        label_changed.data["approval_fingerprint"] != reviewed.data["approval_fingerprint"],
        "candidate label participates in fingerprint",
    )
    fake.label_suffix = "v1"
    fake.metadata_revision = "m2"
    metadata_changed = manager.preview(config, decision_selections=selections)
    _check(
        metadata_changed.data["approval_fingerprint"] != reviewed.data["approval_fingerprint"],
        "candidate metadata participates in fingerprint",
    )
    fake.metadata_revision = "m1"

    fake.requests.clear()
    model_result = manager.create(
        config,
        approved_fingerprint=str(reviewed.data["approval_fingerprint"]),
        decision_selections=selections,
    )
    _check(
        model_result.error_kind == "next_stage_preview_required",
        "model mutation requires a new coding-agent preview",
    )
    _assert_applied_model_projection(fake)
    coding_preview, create_result = _apply_current_frontier(manager, config)
    _check(
        coding_preview.data["frontier"] == ["coding_agents"] and create_result.status == "verified",
        "coding-agent approval and safe read-only frontiers reach verified",
    )
    loaded_value = load_transaction(paths.transaction_path(config.name))
    _check(
        loaded_value is not None and bool(loaded_value.operation_attempts),
        "operation attempts persisted",
    )
    loaded = cast(Transaction, loaded_value)
    _check(
        _persisted_consents_are_true(loaded),
        "approved prospective consent booleans persisted",
    )
    decisions = cast(dict[str, object], loaded.answers["decisions"])
    _check(
        decisions["embedding_model"] == selections["embedding_model"],
        "qualified discovered selection persisted",
    )
    _check(
        _decision_probe_attempts(loaded)
        == {("decision_review", "exit"): 1, ("models", "entry"): 1},
        "reused manager probe attempt numbering starts at one per full tuple",
    )
    verified_status = LifecycleManager(paths).status(config.name)
    instance = cast(dict[str, object], verified_status.data["instance"])
    _check(
        instance["name"] == config.name,
        "verified checkpoint registers managed instance",
    )
    _check(
        _stage_progress_is_typed(verified_status),
        "status exposes stage entry, operation, and exit state",
    )
    _check(
        _status_rendering_matches(verified_status),
        "human and JSON status share the same typed stage progress",
    )
    doctor = InstallationDoctor(
        paths=paths,
        contract_directory=_CONTRACTS,
    ).run(config.name)
    _check(doctor.status == "verified", "doctor updates the pinned verified transaction")
    return VerifiedContext(paths, config, manager, fake, selections, loaded)


def _purity_scenario(
    root: Path,
    original_plan_builder: object,
    selections: dict[str, str],
) -> None:
    paths, config, manager, _transaction = _prepare(root, name="purity")
    fake = FakeAdapter()
    _set_invoke_adapter(fake)
    preview = manager.preview(config, decision_selections=selections)
    path = paths.transaction_path("purity")
    before = path.read_bytes()
    _set_plan_builder(partial(_state_changing_read_only_plan, original_plan_builder))
    fake.requests.clear()
    _raises(
        ProbeDriftError,
        lambda: manager.create(
            config,
            approved_fingerprint=str(preview.data["approval_fingerprint"]),
            decision_selections=selections,
        ),
        "durable read-only auto-advance refuses answer-state changes",
    )
    _set_plan_builder(original_plan_builder)
    _check(
        _purity_stop_is_clean(before, path, fake),
        "durable purity stop persists no answer and crosses no mutation",
    )


def _lifecycle_and_contract_scenario(context: VerifiedContext) -> None:
    paths = context.paths
    config = context.config
    manager = context.manager
    fake = context.fake
    selections = context.selections
    loaded = context.transaction

    fake.requests.clear()
    started = LifecycleManager(paths).start(config.name)
    _check(started.status == "verified", "start verifies every declared postcondition")
    _check(
        sum(request.phase == "apply" for request in fake.requests) == 1,
        "start applies the declared lifecycle operation exactly once",
    )
    start_apply = _find_request(fake, phase="apply")
    _check(
        _start_request_is_bound(start_apply, loaded),
        "start carries the transaction-recorded approval and declared timeout",
    )

    fake.failed_post_probe = "router_ready"
    fake.requests.clear()
    wrong_router = LifecycleManager(paths).start(config.name)
    _check(
        wrong_router.status != "verified"
        and wrong_router.error_kind == "router_ready_invalid"
        and sum(request.phase == "apply" for request in fake.requests) == 1,
        "applied start with the wrong router never verifies",
    )
    fake.failed_post_probe = "peer_identity_valid"
    fake.requests.clear()
    wrong_identity = LifecycleManager(paths).start(config.name)
    _check(
        wrong_identity.status != "verified"
        and wrong_identity.error_kind == "peer_identity_valid_invalid",
        "wrong peer identity never verifies lifecycle start",
    )
    fake.failed_post_probe = None

    fake.incomplete_completion = True
    post_doctor = InstallationDoctor(
        paths=paths,
        contract_directory=_CONTRACTS,
    ).run(config.name)
    status_after_doctor = LifecycleManager(paths).status(config.name)
    _check(
        _doctor_status_cause_is_precise(post_doctor, status_after_doctor),
        "status follows the doctor-updated transaction instead of target presence",
    )
    fake.incomplete_completion = False

    flow_path = target_contract_directory(config.target) / "macos_setup_flow.json"
    flow = json.loads(flow_path.read_text(encoding="utf-8"))
    flow["title"] = "N plus one changed flow"
    flow_path.write_text(json.dumps(flow, indent=2), encoding="utf-8")
    _raises(
        ContractError,
        lambda: manager.preview(config, decision_selections=selections),
        "N-to-N+1 resume refuses changed target flow bytes",
    )
    _raises(
        ContractError,
        lambda: InstallationDoctor(
            paths=paths,
            contract_directory=_CONTRACTS,
        ).run(config.name),
        "doctor refuses changed target flow bytes",
    )
    _raises(
        ContractError,
        lambda: LifecycleManager(paths).start(config.name),
        "lifecycle start refuses N-to-N+1 changed target flow bytes",
    )


def _decision_failure_scenarios(
    root: Path,
    selections: dict[str, str],
    original_invoke_adapter: object,
) -> None:
    _paths, config, manager, _transaction = _prepare(root, name="emptycase")
    _set_invoke_adapter(FakeAdapter(empty_decision="embedding_model"))
    _dependencies, _genesis, empty = _advance_to_model_review(
        manager,
        config,
        decision_selections=selections,
    )
    _check(
        empty.data["frontier"] == ["models"]
        and "candidate_set_empty" in str(empty.data["decision_errors"])
        and empty.repair
        == (
            "The selected model service responded successfully but exposes no "
            "models. Load an appropriate embedding or inference model, then "
            "rerun preview."
        ),
        "verified empty candidates block at models with a loaded-model repair",
    )

    _set_invoke_adapter(FakeAdapter())
    _paths, config, manager, _transaction = _prepare(root, name="ambiguouscase")
    _dependencies, _genesis, ambiguous = _advance_to_model_review(
        manager,
        config,
        decision_selections={
            "embedding_model": "not-returned",
            "inference_model": "inference_model.recommended",
        },
    )
    _check(
        "decision_selection_ambiguous" in str(ambiguous.data["decision_errors"]),
        "unknown or ambiguous selection blocks",
    )

    _set_invoke_adapter(FakeAdapter(fail_qualification="embedding_model"))
    _paths, config, manager, _transaction = _prepare(root, name="qualificationcase")
    _dependencies, _genesis, failed = _advance_to_model_review(
        manager,
        config,
        decision_selections=selections,
    )
    _check(
        "decision_qualification_failed" in str(failed.data["decision_errors"]),
        "failed qualification blocks",
    )
    _assert_zero_candidate_qualification_refusal(failed)

    paths, config, manager, _transaction = _prepare(root, name="healthonly")
    _set_invoke_adapter(FakeAdapter(incomplete_completion=True))
    _advance_to_model_review(manager, config, decision_selections=selections)
    _models, _coding_agents, result = _finish_from_model_review(
        manager,
        config,
        selections,
    )
    _check(result.status != "verified", "health-only subset cannot verify transaction")
    _check(
        LifecycleManager(paths).list_instances().data["instances"] == [],
        "synthetic pre-existing transaction remains unregistered",
    )
    _provisional_registry_scenario(root)
    _models_frontier_missing_adapter_scenario(root, original_invoke_adapter)


def _representative_inference_fallback_scenario(
    root: Path,
    selections: dict[str, str],
) -> None:
    """A failed representative probe warns in doctor without blocking setup."""

    paths, config, manager, _transaction = _prepare(root, name="representativefallback")
    fake = FakeAdapter()
    _set_invoke_adapter(fake)
    _dependencies, _genesis, models_preview = _advance_to_model_review(
        manager,
        config,
        decision_selections=selections,
    )
    _check(
        models_preview.status == "preview_ready"
        and _inference_qualification_refs(fake) == set(),
        "inference qualification does not gate the models frontier",
    )
    _check(
        _inference_qualification_timeout_seconds(fake) == set(),
        "manager does not spend the models frontier on inference qualification",
    )
    _models, _coding_agents, create_result = _finish_from_model_review(
        manager,
        config,
        selections,
    )
    original_advisory_invoke = doctor_inference_qualification_module.invoke_adapter
    try:
        doctor_inference_qualification_module.invoke_adapter = partial(
            _representative_timeout_adapter,
            fake,
        )
        doctor = InstallationDoctor(paths=paths, contract_directory=_CONTRACTS).run(config.name)
    finally:
        doctor_inference_qualification_module.invoke_adapter = original_advisory_invoke
    _check(
        create_result.status == "verified"
        and doctor.exit_code == 0
        and doctor.data.get("setup_stage_blockers") == []
        and _inference_qualification_warnings_match(doctor),
        "timed-out representative inference stays eligible and surfaces only doctor warnings",
    )
    _check(
        _representative_warning_preserves_timeout_shape(doctor),
        "doctor retains the recorded adapter-timeout no-evidence shape",
    )
def _inference_qualification_refs(fake: FakeAdapter) -> set[str]:
    return {
        request.operation_ref
        for request in fake.requests
        if request.probe_purpose == "decision_qualification"
        and request.public_inputs.get("decision_id") == "inference_model"
    }


def _inference_qualification_timeout_seconds(fake: FakeAdapter) -> set[int]:
    return {
        request.timeout_seconds
        for request in fake.requests
        if request.probe_purpose == "decision_qualification"
        and request.operation_ref == "setup::models.qualify_structured_actions"
    }


def _representative_timeout_adapter(
    fake: FakeAdapter,
    registry: object,
    *,
    runner: str,
    request: OperationRequest,
) -> OperationResult:
    if request.operation_id not in {
        "structured_action_qualification",
        "representative_inference_probe",
    }:
        return fake(registry, runner=runner, request=request)
    fake.requests.append(request)
    return fake._result(
        request,
        CheckpointStatus.FAILED,
        error_kind="adapter_timeout",
        repair="Inspect the target-local adapter and retry after resolving the timeout.",
    )


def _representative_probe_timeout_seconds(fake: FakeAdapter) -> int | None:
    requests = [
        request for request in fake.requests if request.operation_id == "representative_inference_probe"
    ]
    return requests[0].timeout_seconds if len(requests) == 1 else None


def _representative_warning(doctor: CommandResult) -> dict[str, object] | None:
    advisories = doctor.data.get("advisories")
    if not isinstance(advisories, list):
        return None
    return next(
        (
            item
            for item in advisories
            if isinstance(item, dict)
            and item.get("check_id") == "doctor::representative_inference_qualification"
        ),
        None,
    )


def _representative_warning_matches(doctor: CommandResult) -> bool:
    warning = _representative_warning(doctor)
    return (
        isinstance(warning, dict)
        and warning.get("status") == "warn"
        and warning.get("blocking") is False
        and warning.get("reason_code") == "representative_inference_qualification_failed"
    )


def _inference_qualification_warnings_match(doctor: CommandResult) -> bool:
    advisories = doctor.data.get("advisories")
    if not isinstance(advisories, list):
        return False
    warning_ids = {
        item.get("check_id")
        for item in advisories
        if isinstance(item, dict)
        and item.get("status") == "warn"
        and item.get("blocking") is False
    }
    return "doctor::representative_inference_qualification" in warning_ids


def _representative_warning_preserves_timeout_shape(doctor: CommandResult) -> bool:
    warning = _representative_warning(doctor)
    observed = warning.get("observed") if isinstance(warning, dict) else None
    return (
        isinstance(observed, dict)
        and observed.get("error_kind") == "adapter_timeout"
        and observed.get("summary")
        == "No adapter evidence was returned; checkpoint status is failed."
    )


def _provisional_registry_scenario(root: Path) -> None:
    """The create-execution materialization seam retains incomplete setup."""

    paths, config, manager, _transaction = _prepare(root, name="provisionalrecord")
    paths.transaction_path(config.name).unlink()
    shutil.rmtree(config.target)
    manager.contract_directory = _CONTRACTS
    create_execution_module.materialize_locked_seed = _materialize_fixture
    acquisition = manager.preview(config)
    result = manager.create(
        config,
        approved_fingerprint=str(acquisition.data["approval_fingerprint"]),
    )
    records = LifecycleManager(paths).list_instances().data["instances"]
    status = LifecycleManager(paths).status(config.name)
    _check(
        result.error_kind == "probe_drift",
        "materialized transaction pauses for the fresh target-local preview",
    )
    _check(
        isinstance(records, list)
        and len(records) == 1
        and records[0]["lifecycle_state"] == "setup_incomplete"
        and records[0]["input_fingerprint"] == _transaction.input_fingerprint,
        "create-execution materialization retains a provisional manager record",
    )
    _check(
        status.error_kind == "instance_setup_incomplete"
        and status.repair == f"Resume with: solet create {config.name}",
        "provisional status directs the same reviewed create transaction to resume",
    )


def _models_frontier_missing_adapter_scenario(
    root: Path,
    original_invoke_adapter: object,
) -> None:
    _paths, config, manager, _transaction = _prepare(root, name="missingadapter")
    fake = FakeAdapter()
    _set_invoke_adapter(fake)
    _apply_current_frontier(manager, config)
    _apply_current_frontier(manager, config)
    decision_discovery_module.invoke_adapter = original_invoke_adapter
    missing = manager.preview(config)
    _set_invoke_adapter(fake)
    errors = cast(list[object], missing.data["decision_errors"])
    missing_ids = {
        str(item["id"])
        for item in errors
        if isinstance(item, dict) and item.get("error_kind") == "adapter_missing"
    }
    _check(
        missing.data["frontier"] == ["models"]
        and missing.status == "awaiting_user"
        and missing.error_kind == "decisions_required"
        and missing_ids == {"embedding_model", "inference_model"}
        and missing.repair
        == (
            "Required discovery adapter for 'embedding_model', 'inference_model' is unavailable. "
            "Install the reviewed 'hydration' target-local adapter and resume."
        ),
        "unavailable model-frontier hydration adapter is a named loud refusal",
    )


def _genesis_hydration_vector_scenario(root: Path, selections: dict[str, str]) -> None:
    _paths, config, manager, _transaction = _prepare(
        root,
        name="genesisvector",
    )
    fake = FakeAdapter()
    _set_invoke_adapter(fake)
    _apply_current_frontier(manager, config, decision_selections=selections)
    _make_hydration_vector(config.target)
    fake.requests.clear()
    genesis = manager.preview(config, decision_selections=selections)
    _check(
        _genesis_defers_model_decisions(genesis, fake),
        "genesis hydration vector does not make future model decisions eligible",
    )
    manager.create(
        config,
        approved_fingerprint=str(genesis.data["approval_fingerprint"]),
        decision_selections=selections,
    )
    fake.requests.clear()
    models = manager.preview(config, decision_selections=selections)
    _check(
        _models_discover_and_qualify(models, fake),
        "models frontier still discovers and qualifies both model decisions",
    )


def _genesis_defers_model_decisions(result: CommandResult, fake: FakeAdapter) -> bool:
    model_ids = {"embedding_model", "inference_model"}
    prompts = cast(list[dict[str, object]], result.data["decision_prompts"])
    errors = cast(list[dict[str, object]], result.data["decision_errors"])
    return (
        result.data["frontier"] == ["genesis"]
        and result.status == "preview_ready"
        and not _model_decision_requests(fake)
        and not {str(item["id"]) for item in prompts if item.get("id") in model_ids}
        and not {str(item["id"]) for item in errors if item.get("id") in model_ids}
    )


def _models_discover_and_qualify(result: CommandResult, fake: FakeAdapter) -> bool:
    return result.data["frontier"] == ["models"] and _model_decision_requests(fake) == {
        "decision_discovery",
        "decision_qualification",
    }


def _model_decision_requests(fake: FakeAdapter) -> set[str | None]:
    return {
        request.probe_purpose
        for request in fake.requests
        if request.public_inputs.get("decision_id") in {"embedding_model", "inference_model"}
    }


def _assert_zero_candidate_qualification_refusal(failed: CommandResult) -> None:
    qualification_errors = [
        item
        for item in cast(list[object], failed.data["decision_errors"])
        if isinstance(item, dict)
        and item.get("id") == "embedding_model"
        and item.get("error_kind") == "decision_qualification_failed"
    ]
    qualification_error = qualification_errors[0] if qualification_errors else {}
    message_matches = failed.message == (
        "Required decision 'embedding_model' expected at least one permitted "
        "candidate after qualification; found zero. No decision value can be "
        "supplied until at least one candidate passes every required "
        "qualification probe."
    )
    repair_matches = failed.repair == (
        "No repair is available by adding a decision value: zero candidates "
        "passed qualification. Correct the failures listed in "
        "data.decision_errors so every required qualification probe returns "
        "verified for at least one candidate, then rerun preview."
    )
    _check(
        all(
            (
                message_matches,
                repair_matches,
                _qualification_failure_summary_matches(qualification_error),
                _candidate_failure_causes_match(qualification_error),
            )
        ),
        "zero-permitted-candidate refusal explains what failed without inventing "
        f"a decision value; actual={json.dumps(failed.to_dict(), sort_keys=True)}",
    )


def _qualification_failure_summary_matches(error: dict[str, object]) -> bool:
    return error.get("expected") == {
        "minimum_qualified_candidates": 1,
        "required_probe_status": "verified",
    } and error.get("found") == {
        "discovered_candidates": 2,
        "qualified_candidates": 0,
    }


def _candidate_failure_causes_match(error: dict[str, object]) -> bool:
    failures = cast(list[object], error.get("qualification_failures", []))
    expected = [
        {
            "probe_id": "embedding_model_qualification",
            "checkpoint_status": "blocked",
            "error_kind": "qualification_failed",
            "repair": "Select another returned candidate.",
            "observed_summary": "fixture qualification observed blocked",
        }
    ]
    return len(failures) == 2 and all(
        isinstance(item, dict) and item.get("failed_probes") == expected for item in failures
    )


def _stage_boundary_scenarios(root: Path) -> None:
    selections = dict(_MODEL_SELECTIONS)
    _paths, config, manager, _transaction = prepare_with_decision_review_entry_probe(
        root,
        name="entryfailure",
        probe_id="homebrew_available",
    )
    entry_adapter = FakeAdapter(failed_stage_probe_once="homebrew_available")
    _set_invoke_adapter(entry_adapter)
    entry_failure = manager.preview(config)
    _check(
        _entry_failure_is_safe(entry_failure, entry_adapter),
        "failed stage entry renders its declared repair without applying it",
    )

    _paths, config, manager, _transaction = _prepare(root, name="exitresume")
    exit_adapter = FakeAdapter(failed_stage_probe_once="postgres_ready")
    _set_invoke_adapter(exit_adapter)
    exit_preview = manager.preview(config, decision_selections=selections)
    exit_failed = manager.create(
        config,
        approved_fingerprint=str(exit_preview.data["approval_fingerprint"]),
        decision_selections=selections,
    )
    _check(
        exit_failed.error_kind == "stage_boundary_remediation_required",
        "failed stage exit renders a fresh remediation approval",
    )
    exit_resumed = manager.create(
        config,
        approved_fingerprint=str(exit_failed.data["approval_fingerprint"]),
        decision_selections=selections,
    )
    _check(
        _exit_resume_is_safe(exit_resumed, exit_adapter),
        "failed exit-probe resume retries the exit with zero duplicate apply",
    )

    _paths, config, manager, _transaction = _prepare(root, name="frontierchain")
    chain_adapter = FakeAdapter()
    _set_invoke_adapter(chain_adapter)
    first = manager.preview(config, decision_selections=selections)
    first_count = sum(request.probe_purpose == "preview" for request in chain_adapter.requests)
    _check(
        first_count == 15,
        "system-dependencies preview includes the consented tool provisioners",
    )
    manager.create(
        config,
        approved_fingerprint=str(first.data["approval_fingerprint"]),
        decision_selections=selections,
    )
    chain_adapter.requests.clear()
    second = manager.preview(config, decision_selections=selections)
    second_count = sum(request.probe_purpose == "preview" for request in chain_adapter.requests)
    manager.create(
        config,
        approved_fingerprint=str(second.data["approval_fingerprint"]),
        decision_selections=selections,
    )
    chain_adapter.requests.clear()
    third = manager.preview(config, decision_selections=selections)
    discovery_count = sum(
        request.probe_purpose == "decision_discovery" for request in chain_adapter.requests
    )
    _check(
        all(
            (
                first.data.get("frontier") == ["system_dependencies"],
                first_count == 15,
                second.data.get("frontier") == ["genesis"],
                second_count == 2,
                third.status == "preview_ready",
                third.data.get("frontier") == ["models"],
                discovery_count == 2,
            )
        ),
        "A-to-B-to-C operation call counts stay frontier-local while decisions close early",
    )


def _corrupt_state_scenario(root: Path) -> None:
    paths, config, manager, transaction = _prepare(root, name="corruptanswers")
    decisions = cast(dict[str, object], transaction.answers["decisions"])
    corrupt_answers = {
        **transaction.answers,
        "decisions": {**decisions, "undeclared_decision": "invalid"},
    }
    write_transaction(
        paths.transaction_path(config.name),
        transaction.with_answers(corrupt_answers),  # type: ignore[arg-type]
    )
    try:
        manager.preview(config)
    except StateError as exc:
        _check(
            exc.error_kind == "corrupt_state" and "recorded normalized answers" in str(exc),
            "corrupt recorded answers fail as corrupt_state before reconstruction",
        )
    else:
        _check(False, "corrupt recorded answers must fail closed")


def _applied_crash_scenario(
    root: Path,
    original_write: Callable[[Path, Transaction], None],
) -> None:
    paths, config, manager, _transaction = _prepare(root, name="applycrash")
    fake = FakeAdapter()
    _set_invoke_adapter(fake)
    selections = dict(_MODEL_SELECTIONS)
    preview = manager.preview(config, decision_selections=selections)
    state: dict[str, str | bool | None] = {
        "raised": False,
        "operation_id": None,
    }

    def crash_after_applied_write(path: Path, pending: Transaction) -> None:
        original_write(path, pending)
        if state["raised"] is True:
            return
        applied_ids = [
            operation_id
            for operation_id, status in pending.operation_statuses.items()
            if status is CheckpointStatus.APPLIED
        ]
        if applied_ids:
            state["raised"] = True
            state["operation_id"] = applied_ids[0]
            raise RuntimeError("fixture crash after durable applied checkpoint")

    _set_write_transaction(crash_after_applied_write)
    _raises(
        RuntimeError,
        lambda: manager.create(
            config,
            approved_fingerprint=str(preview.data["approval_fingerprint"]),
            decision_selections=selections,
        ),
        "fixture crashes after applied is durably journaled",
    )
    operation_id = str(state["operation_id"])
    interrupted = load_transaction(paths.transaction_path(config.name))
    _check(
        interrupted is not None
        and interrupted.operation_statuses.get(operation_id) is CheckpointStatus.APPLIED
        and any(
            attempt.get("operation_id") == operation_id
            and attempt.get("checkpoint_status") == CheckpointStatus.APPLIED.value
            for attempt in interrupted.operation_attempts
        ),
        "interrupted apply has durable applied attempt evidence",
    )
    _set_write_transaction(original_write)
    resumed = manager.create(
        config,
        approved_fingerprint=str(preview.data["approval_fingerprint"]),
        decision_selections=selections,
    )
    _check(
        _crash_resume_is_safe(resumed, fake, operation_id),
        "applied crash resumes through post-probe with zero duplicate apply",
    )


def _legacy_v1_operation_attempt(attempt: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Spell out the frozen pre-r12 operation-attempt key set."""

    return {
        "operation_id": attempt["operation_id"],
        "stage_id": attempt["stage_id"],
        "phase": attempt["phase"],
        "attempt": attempt["attempt"],
        "request_id": attempt["request_id"],
        "checkpoint_status": attempt["checkpoint_status"],
        "error_kind": attempt["error_kind"],
        "retry_safe": attempt["retry_safe"],
        "planned_actions": attempt["planned_actions"],
        "evidence": attempt["evidence"],
        "repair": attempt["repair"],
        "recorded_at": attempt["recorded_at"],
    }


def _legacy_v1_journal(transaction: Transaction) -> dict[str, JsonValue]:
    """Construct a literal v1 document without relabeling current writer bytes."""

    return {
        "schema_version": 1,
        "operation_id": transaction.operation_id,
        "name": transaction.name,
        "target": transaction.target,
        "input_fingerprint": transaction.input_fingerprint,
        "answers": transaction.answers,
        "answers_fingerprint": transaction.answers_fingerprint,
        "approval_fingerprint": transaction.approval_fingerprint,
        "approval_recorded_at": transaction.approval_recorded_at,
        "seed_repository": transaction.seed.repository,
        "seed_tag": transaction.seed.release_tag,
        "seed_commit": transaction.seed.commit,
        "seed_tree_hash": transaction.seed.tree_hash,
        "seed_archive_sha256": transaction.seed.archive_sha256,
        "profile": transaction.seed.profile,
        "flow_id": transaction.flow_id,
        "flow_source_revision": transaction.flow_source_revision,
        "flow_contract_digest": transaction.flow_contract_digest,
        "status": transaction.status.value,
        "stages": {stage_id: status.value for stage_id, status in transaction.stages.items()},
        "stage_probe_statuses": {
            stage_id: {
                boundary: {probe_id: status.value for probe_id, status in probes.items()}
                for boundary, probes in boundaries.items()
            }
            for stage_id, boundaries in transaction.stage_probe_statuses.items()
        },
        "stage_probe_attempts": list(transaction.stage_probe_attempts),
        "operation_stages": transaction.operation_stages,
        "operation_statuses": {
            operation_id: status.value
            for operation_id, status in transaction.operation_statuses.items()
        },
        "operation_attempts": [
            _legacy_v1_operation_attempt(attempt) for attempt in transaction.operation_attempts
        ],
        "evidence": list(transaction.evidence),
        "completion": {
            probe_id: status.value for probe_id, status in transaction.completion.items()
        },
        "result_kind": transaction.result_kind,
        "created_at": transaction.created_at,
        "updated_at": transaction.updated_at,
    }


def _write_legacy_operation_attempt_journal(
    path: Path,
    transaction: Transaction,
) -> None:
    """Persist the exact pre-r12 attempt shape and retain its corruption control."""

    legacy_journal = _legacy_v1_journal(transaction)
    atomic_write_json(path, legacy_journal)
    legacy_loaded = load_transaction(path)
    _check(
        legacy_loaded is not None
        and all(
            attempt["exit_code"] is None
            and attempt["timed_out"] is False
            and attempt["duration_ms"] == 0
            and attempt["reason"] is None
            for attempt in legacy_loaded.operation_attempts
        ),
        "exact pre-r12 operation attempts load with explicit diagnostic defaults",
    )
    malformed_journal = json.loads(json.dumps(legacy_journal))
    del malformed_journal["operation_attempts"][0]["request_id"]
    atomic_write_json(path, malformed_journal)
    _raises(
        StateError,
        lambda: load_transaction(path),
        "operation attempt missing an original required field remains corrupt",
    )
    atomic_write_json(path, legacy_journal)


def _legacy_contract_resume_scenario(
    root: Path,
    original_write: Callable[[Path, Transaction], None],
) -> None:
    """Resume the frozen pre-fix flow without reapplying its durable operation."""

    paths, config, manager, transaction = _prepare(root, name="legacy-contract-resume")
    legacy_revision = "4ff38b3de29d5d643757802bf7727b8594cded04"
    contract_directory = target_contract_directory(config.target)
    shutil.copytree(
        Path(__file__).parent / "fixtures/contracts/reconcile_contract_legacy_4ff38b3d",
        contract_directory,
        dirs_exist_ok=True,
    )
    flow_path = contract_directory / "macos_setup_flow.json"
    frozen_bytes = flow_path.read_bytes()
    legacy_bundle = ContractBundle.load(
        source_revision=legacy_revision,
        directory=contract_directory,
        resume_compatibility=True,
    )
    authentic_legacy_transaction = Transaction.create(
        name=transaction.name,
        target=transaction.target,
        input_fingerprint=transaction.input_fingerprint,
        answers=transaction.answers,
        seed=transaction.seed,
        flow_id=legacy_bundle.flow_id,
        flow_source_revision=legacy_revision,
        flow_contract_digest=legacy_bundle.contract_digest,
        stage_ids=tuple(legacy_bundle.stages),
        completion_probe_ids=legacy_bundle.completion_probe_ids,
        stage_probe_statuses=initial_stage_probe_statuses(legacy_bundle, transaction.answers),
    )
    original_write(paths.transaction_path(config.name), authentic_legacy_transaction)
    fake = FakeAdapter()
    _set_invoke_adapter(fake)
    selections = dict(_MODEL_SELECTIONS)
    preview = manager.preview(config, decision_selections=selections)
    state: dict[str, str | bool | None] = {"raised": False, "operation_id": None}

    def crash_after_applied_write(path: Path, pending: Transaction) -> None:
        original_write(path, pending)
        if state["raised"] is True:
            return
        applied_ids = [
            operation_id
            for operation_id, status in pending.operation_statuses.items()
            if status is CheckpointStatus.APPLIED
        ]
        if applied_ids:
            state["raised"] = True
            state["operation_id"] = applied_ids[0]
            raise RuntimeError("fixture crash after durable applied checkpoint")

    _set_write_transaction(crash_after_applied_write)
    _raises(
        RuntimeError,
        lambda: manager.create(
            config,
            approved_fingerprint=str(preview.data["approval_fingerprint"]),
            decision_selections=selections,
        ),
        "legacy fixture crashes after the known operation is durably applied",
    )
    interrupted = load_transaction(paths.transaction_path(config.name))
    if interrupted is None:
        raise AssertionError("legacy fixture lacks its interrupted transaction")
    _raises(
        ContractError,
        lambda: ContractBundle.load(
            source_revision=legacy_revision,
            directory=contract_directory,
        ),
        "fresh loading rejects the authentic legacy later-remediation shape",
    )
    forged_directory = root / "forged-legacy-contracts"
    shutil.copytree(contract_directory, forged_directory)
    forged_flow_path = forged_directory / "macos_setup_flow.json"
    forged_flow = json.loads(forged_flow_path.read_text(encoding="utf-8"))
    forged_flow["operations"]["install_python_runtime"]["idempotency"][
        "postcondition_probe_refs"
    ] = ["python_version_valid", "fresh_shell_python_valid"]
    forged_flow["operations"]["install_postgresql"]["idempotency"]["postcondition_probe_refs"] = [
        "postgres_binary_version_valid",
        "pgvector_ready",
    ]
    forged_flow["operations"]["open_background_items_settings"]["idempotency"][
        "postcondition_probe_refs"
    ] = ["launchagent_running"]
    forged_flow_path.write_text(json.dumps(forged_flow, indent=2), encoding="utf-8")
    _raises(
        ContractError,
        lambda: ContractBundle.load(
            source_revision=legacy_revision,
            directory=forged_directory,
            resume_compatibility=True,
        ),
        "resume compatibility refuses mutated bytes claiming legacy identity",
    )
    _check(
        legacy_bundle.operations["install_python_runtime"]["idempotency"][
            "postcondition_probe_refs"
        ]
        == ["python_version_valid"],
        "resume normalizes the known legacy Python postcondition declaration",
    )
    _check(
        legacy_bundle.operations["install_postgresql"]["idempotency"]["postcondition_probe_refs"]
        == ["postgres_binary_version_valid"],
        "resume normalizes the known legacy PostgreSQL postcondition declaration",
    )
    _check(
        legacy_bundle.operations["open_background_items_settings"]["idempotency"][
            "postcondition_probe_refs"
        ]
        == [],
        "resume normalizes the known legacy Background Items postcondition declaration",
    )
    _check(
        legacy_bundle.flow["decisions"]["inference_model"]["option_source"][
            "candidate_contract"
        ]["qualification_probe_refs"]
        == [],
        "resumed served inference remains eligible when qualification times out",
    )
    _check(
        flow_path.read_bytes() == frozen_bytes,
        "resume normalization never mutates the pinned legacy flow bytes",
    )
    frozen_transaction = interrupted
    _write_legacy_operation_attempt_journal(
        paths.transaction_path(config.name),
        frozen_transaction,
    )
    _set_write_transaction(original_write)
    resumed_preview = manager.preview(config, decision_selections=selections)
    _check(
        resumed_preview.status == "preview_ready",
        "legacy journal reaches the ordinary resume-preview path",
    )
    resumed = manager.create(
        config,
        approved_fingerprint=str(resumed_preview.data["approval_fingerprint"]),
        decision_selections=selections,
    )
    _check(
        _crash_resume_is_safe(resumed, fake, str(state["operation_id"])),
        "legacy journal resumes through post-probe with zero duplicate apply",
    )


def _any_pinned_inference_contract_scenario(root: Path) -> None:
    """A non-legacy pinned inference contract cannot restore a probe gate."""

    _paths, config, _manager, transaction = _prepare(root, name="any-pinned-inference")
    contract_directory = target_contract_directory(config.target)
    flow_path = contract_directory / "macos_setup_flow.json"
    pinned_flow = json.loads(flow_path.read_text(encoding="utf-8"))
    candidate_contract = pinned_flow["decisions"]["inference_model"]["option_source"][
        "candidate_contract"
    ]
    candidate_contract["qualification_probe_refs"] = [
        "structured_action_qualification",
        "representative_inference_probe",
    ]
    flow_path.write_text(json.dumps(pinned_flow, indent=2), encoding="utf-8")
    pinned_bundle = ContractBundle.load(
        source_revision="non-legacy-pinned-flow",
        directory=contract_directory,
        resume_compatibility=True,
    )
    pinned_refs = pinned_bundle.flow["decisions"]["inference_model"]["option_source"][
        "candidate_contract"
    ]["qualification_probe_refs"]
    _check(
        pinned_refs == ["structured_action_qualification", "representative_inference_probe"],
        "non-legacy pinned flow retains its exact qualification refs",
    )

    fake = FakeAdapter(fail_qualification="inference_model")
    _set_invoke_adapter(fake)
    plan = SetupPlan(
        answers=transaction.answers,
        operations=(),
        unresolved_decisions=(),
        unresolved_consents=(),
    )
    discovery = decision_discovery_module._discover_decision(  # noqa: SLF001
        bundle=pinned_bundle,
        transaction=transaction,
        plan=plan,
        registry=cast(object, None),
        decision_id="inference_model",
        selected="inference_model.recommended",
    )
    candidates = discovery.prompt["candidates"]
    _check(
        discovery.errors == ()
        and isinstance(candidates, list)
        and len(candidates) == 2
        and not _inference_qualification_refs(fake),
        "served inference stays eligible for any pinned flow despite stale probe refs",
    )

    exact_refusal = decision_discovery_module._discover_decision(  # noqa: SLF001
        bundle=pinned_bundle,
        transaction=transaction,
        plan=plan,
        registry=cast(object, None),
        decision_id="inference_model",
        selected="not-returned-by-discovery",
    )
    _check(
        len(exact_refusal.errors) == 1
        and isinstance(exact_refusal.errors[0], dict)
        and exact_refusal.errors[0].get("error_kind") == "decision_selection_ambiguous",
        "inference eligibility waiver does not permit an undiscovered exact-model selection",
    )


def run_journal_resume_round_trip_scenario() -> None:
    """Drive the hermetic interrupted-apply resume guarantee in isolation."""

    original = decision_discovery_module.invoke_adapter
    original_plan_builder = preview_engine_module.build_setup_plan
    original_lifecycle = lifecycle_module.invoke_adapter
    original_write = operation_executor_module.write_transaction
    try:
        with tempfile.TemporaryDirectory() as raw:
            _applied_crash_scenario(Path(raw), original_write)
        with tempfile.TemporaryDirectory() as raw:
            _legacy_contract_resume_scenario(Path(raw), original_write)
        with tempfile.TemporaryDirectory() as raw:
            _any_pinned_inference_contract_scenario(Path(raw))
    finally:
        _set_invoke_adapter(original)
        _set_plan_builder(original_plan_builder)
        _set_write_transaction(original_write)
        lifecycle_module.invoke_adapter = original_lifecycle


def _materialize_fixture(
    _seed: object,
    destination: Path,
    *,
    cache_dir: Path,
) -> Path:
    del cache_dir
    destination.mkdir(parents=True)
    shutil.copytree(_CONTRACTS, target_contract_directory(destination))
    return destination


def _future_model_carrier_scenario(root: Path) -> None:
    paths = ManagerPaths.resolve(explicit_home=root / "manager-early-model", home=root)
    target = root / "Solets" / "early-model"
    selected_model = "embedding_model.recommended"
    config = CreateConfig(
        name="early-model",
        target=target.resolve(),
        autostart=True,
        decisions={
            "inference_implementation": "lm_studio",
            "execution_topology": "solo",
            "git_mutation_control": "single_session",
            "session_sources": [],
            "embedding_model": selected_model,
            "inference_model": "inference_model.recommended",
        },
        decision_sources={
            "inference_implementation": "config",
            "execution_topology": "config",
            "git_mutation_control": "config",
            "session_sources": "config",
            "embedding_model": "config",
            "inference_model": "config",
        },
    )
    seed_lock = root / "early-model.seed.lock.json"
    seed_lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "https://github.com/solet-public/macos-bizops.git",
                "release_tag": "release-2026-08-20",
                "commit": "a" * 40,
                "tree_hash": "b" * 40,
                "archive_sha256": "c" * 64,
                "profile": "macos-bizops",
            }
        ),
        encoding="utf-8",
    )
    create_execution_module.materialize_locked_seed = _materialize_fixture
    fake = FakeAdapter()
    _set_invoke_adapter(fake)
    manager = CreateManager(
        paths=paths,
        contract_directory=_CONTRACTS,
        seed_lock_path=seed_lock,
    )

    acquisition = manager.preview(config)
    deferred_config = CreateConfig(
        name=config.name,
        target=config.target,
        autostart=config.autostart,
        decisions={
            "inference_implementation": "lm_studio",
            "execution_topology": "solo",
            "git_mutation_control": "single_session",
            "session_sources": [],
        },
        decision_sources={
            "inference_implementation": "config",
            "execution_topology": "config",
            "git_mutation_control": "config",
            "session_sources": "config",
        },
    )
    deferred_acquisition = manager.preview(deferred_config)
    _check(
        config.decisions.get("embedding_model") == selected_model,
        "public config retains the future model choice until its declared stage",
    )
    _check(
        {"embedding_model", "inference_model"}.issubset(
            set(cast(list[str], acquisition.data["deferred_setup_decisions"]))
        )
        and acquisition.data["approval_fingerprint"]
        == deferred_acquisition.data["approval_fingerprint"],
        "future config selections are deferred from pre-model approval",
    )
    manager.create(
        config,
        approved_fingerprint=str(acquisition.data["approval_fingerprint"]),
    )
    dependency_preview = manager.preview(config)
    dependency_answers = cast(dict[str, object], dependency_preview.data["normalized_answers"])
    dependency_decisions = cast(dict[str, object], dependency_answers["decisions"])
    _check(
        dependency_preview.data["frontier"] == ["system_dependencies"]
        and "embedding_model" not in dependency_decisions
        and "inference_model" not in dependency_decisions
        and _no_future_decision_requests(fake),
        "dependency preview defers future config selections without discovery",
    )
    manager.create(
        config,
        approved_fingerprint=str(dependency_preview.data["approval_fingerprint"]),
    )
    genesis_preview, genesis_result = _apply_current_frontier(manager, config)
    _check(
        genesis_preview.data["frontier"] == ["genesis"]
        and genesis_result.error_kind == "next_stage_preview_required",
        "ordinary manager resume reaches the model frontier",
    )
    persisted_value = load_transaction(paths.transaction_path(config.name))
    _check(persisted_value is not None, "future-carrier transaction is persisted")
    persisted = cast(Transaction, persisted_value)
    persisted_decisions = cast(dict[str, object], persisted.answers["decisions"])
    _check(
        "embedding_model" not in persisted_decisions
        and "inference_model" not in persisted_decisions
        and _no_future_decision_requests(fake),
        "pre-model journal defers supplied model selections without discovery",
    )

    fake.requests.clear()
    model_preview = manager.preview(config)
    model_answers = cast(dict[str, object], model_preview.data["normalized_answers"])
    model_decisions = cast(dict[str, object], model_answers["decisions"])
    model_requests = [
        request
        for request in fake.requests
        if request.public_inputs.get("decision_id") == "embedding_model"
    ]
    _check(
        model_preview.data["frontier"] == ["models"]
        and model_decisions.get("embedding_model") == selected_model
        and model_decisions.get("inference_model") == "inference_model.recommended"
        and not {
            str(item["id"])
            for item in cast(list[dict[str, object]], model_preview.data["decision_prompts"])
            if item.get("id") in {"embedding_model", "inference_model"}
        }
        and {request.probe_purpose for request in model_requests}
        == {"decision_discovery", "decision_qualification"},
        "deferred config model selections apply and qualify at models without prompting",
    )


def _materialization_scenario(
    root: Path,
    selections: dict[str, str],
) -> None:
    paths = ManagerPaths.resolve(explicit_home=root / "manager-staged", home=root)
    target = root / "Solets" / "staged"
    config = CreateConfig(
        name="staged",
        target=target.resolve(),
        autostart=True,
        decisions={
            "inference_implementation": "lm_studio",
            "execution_topology": "solo",
            "git_mutation_control": "single_session",
            "session_sources": [],
        },
        decision_sources={
            "inference_implementation": "config",
            "execution_topology": "config",
            "git_mutation_control": "config",
            "session_sources": "config",
        },
    )
    seed_lock = root / "staged.seed.lock.json"
    seed_lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "https://github.com/solet-public/macos-bizops.git",
                "release_tag": "release-2026-08-20",
                "commit": "a" * 40,
                "tree_hash": "b" * 40,
                "archive_sha256": "c" * 64,
                "profile": "macos-bizops",
            }
        ),
        encoding="utf-8",
    )

    create_execution_module.materialize_locked_seed = _materialize_fixture
    _set_invoke_adapter(FakeAdapter())
    manager = CreateManager(
        paths=paths,
        contract_directory=_CONTRACTS,
        seed_lock_path=seed_lock,
    )
    acquisition = manager.preview(config)
    materialized = manager.create(
        config,
        approved_fingerprint=str(acquisition.data["approval_fingerprint"]),
    )
    _dependencies, _genesis, discovered = _advance_to_model_review(
        manager,
        config,
        decision_selections=selections,
    )
    selected_preview = manager.preview(config, decision_selections=selections)
    _models, _coding_agents, result = _finish_from_model_review(
        manager,
        config,
        selections,
    )
    _check(
        materialized.error_kind == "probe_drift"
        and discovered.status == "preview_ready"
        and selected_preview.status == "preview_ready"
        and result.status == "verified",
        "materialize then pre-activate, requalify, and finish with bound answers",
    )


def run_scenarios() -> None:
    original = decision_discovery_module.invoke_adapter
    original_plan_builder = preview_engine_module.build_setup_plan
    original_lifecycle = lifecycle_module.invoke_adapter
    original_materialize = create_execution_module.materialize_locked_seed
    original_write = operation_executor_module.write_transaction
    try:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_barrier_scenario(root)
            _static_current_frontier_barrier_scenario(root)
            context = _verified_scenario(root, original_plan_builder)
            _lifecycle_and_contract_scenario(context)
            _decision_failure_scenarios(root, context.selections, original)
            _representative_inference_fallback_scenario(root, context.selections)
            _stage_boundary_scenarios(root)
            _corrupt_state_scenario(root)
            _applied_crash_scenario(root, original_write)
            _future_model_carrier_scenario(root)
            _genesis_hydration_vector_scenario(root, context.selections)
            run_projection_scenarios(root)
            _materialization_scenario(root, context.selections)
    finally:
        _set_invoke_adapter(original)
        _set_plan_builder(original_plan_builder)
        create_execution_module.materialize_locked_seed = original_materialize
        _set_write_transaction(original_write)
        lifecycle_module.invoke_adapter = original_lifecycle
