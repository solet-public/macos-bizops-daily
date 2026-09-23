"""Discriminating discovered-decision, completion, and registration smoke."""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import solet_manager.completion_verifier as completion_module  # noqa: E402
import solet_manager.create_execution as create_execution_module  # noqa: E402
import solet_manager.decision_discovery as decision_discovery_module  # noqa: E402
import solet_manager.operation_executor as operation_executor_module  # noqa: E402
import solet_manager.preview_engine as preview_engine_module  # noqa: E402
import solet_manager.stage_boundaries as stage_boundaries_module  # noqa: E402
from solet_manager.adapters import (  # noqa: E402
    DiscoveredCandidate,
    OperationRequest,
    OperationResult,
    PlannedAction,
)
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle, target_contract_directory  # noqa: E402
from solet_manager.create import CreateManager  # noqa: E402
from solet_manager.flow import (  # noqa: E402
    SetupPlan,
    build_setup_plan,
    initial_stage_probe_statuses,
)
from solet_manager.models import CheckpointStatus, CommandResult, JsonValue  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.rendering import render_human, render_json  # noqa: E402
from solet_manager.transaction import (  # noqa: E402
    Transaction,
    canonical_sha256,
    write_transaction,
)

_REPO = Path(__file__).resolve().parents[2]
_CONTRACTS = _REPO / "plugins" / "github_midwife_plugin" / "knowledge_base"
_CHECKS = 0
_PRE_APPLY_PROBE_OWNERS = {
    "homebrew_available": "request_homebrew_install",
    "python_version_valid": "install_python_runtime",
    "instance_environment_dependency_closure_valid": "build_instance_environment",
    "postgres_binary_version_valid": "install_postgresql",
    "pgvector_ready": "install_postgresql",
    "postgres_role_policy_valid": "configure_postgresql",
    "tmux_available": "install_tmux",
    "genesis_artifacts_valid": "run_genesis",
    "fresh_shell_path_valid": "install_shell_integration",
    "launchagent_running": "install_launchagent",
    "codex_plugin_visible": "install_codex_plugin",
    "claude_plugin_visible": "install_claude_plugin",
    "codex_session_retrieval_valid": "register_codex_session_source",
    "claude_session_retrieval_valid": "register_claude_session_source",
    "google_workspace_connection_valid": "configure_google_workspace",
    "marketo_connection_valid": "configure_marketo",
    "salesforce_connection_valid": "configure_salesforce",
    "schwab_connection_valid": "configure_schwab",
    "snowflake_connection_valid": "configure_snowflake",
    "zuora_connection_valid": "configure_zuora",
    "external_postgres_connection_valid": "configure_external_postgres",
}


_INVOKE_MODULES = (
    completion_module,
    decision_discovery_module,
    operation_executor_module,
    preview_engine_module,
    stage_boundaries_module,
)
_PLAN_MODULES = (
    create_execution_module,
    operation_executor_module,
    preview_engine_module,
)
_WRITE_MODULES = (
    completion_module,
    create_execution_module,
    operation_executor_module,
    stage_boundaries_module,
)


def _patch_modules(modules: tuple[object, ...], name: str, value: object) -> None:
    for module in modules:
        setattr(module, name, value)


def _set_invoke_adapter(value: object) -> None:
    _patch_modules(_INVOKE_MODULES, "invoke_adapter", value)


def _set_plan_builder(value: object) -> None:
    _patch_modules(_PLAN_MODULES, "build_setup_plan", value)


def _set_write_transaction(value: object) -> None:
    _patch_modules(_WRITE_MODULES, "write_transaction", value)


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


class FakeAdapter:
    def __init__(
        self,
        *,
        empty_decision: str | None = None,
        fail_qualification: str | None = None,
        missing_metadata_decision: str | None = None,
        label_suffix: str = "v1",
        metadata_revision: str = "m1",
        action_revision: str = "",
        incomplete_completion: bool = False,
        failed_post_probe: str | None = None,
        failed_stage_probe_once: str | None = None,
    ) -> None:
        self.empty_decision = empty_decision
        self.fail_qualification = fail_qualification
        self.missing_metadata_decision = missing_metadata_decision
        self.label_suffix = label_suffix
        self.metadata_revision = metadata_revision
        self.action_revision = action_revision
        self.incomplete_completion = incomplete_completion
        self.failed_post_probe = failed_post_probe
        self.failed_stage_probe_once = failed_stage_probe_once
        self.stage_probe_failures_remaining = 1 if failed_stage_probe_once else 0
        self.requests: list[OperationRequest] = []

    def __call__(
        self,
        _registry: object,
        *,
        runner: str,
        request: OperationRequest,
    ) -> OperationResult:
        del runner
        self.requests.append(request)
        if request.probe_purpose == "decision_discovery":
            return self._discovery_result(request)
        if request.probe_purpose == "decision_qualification":
            return self._qualification_result(request)
        if request.probe_purpose in {"stage_entry", "stage_exit"}:
            return self._stage_boundary_result(request)
        if request.probe_purpose in {"preview", "pre_apply"}:
            return self._planned_action_result(request)
        if request.phase == "apply":
            return self._result(request, CheckpointStatus.APPLIED)
        if request.probe_purpose == "post_apply":
            return self._post_apply_result(request)
        if request.probe_purpose == "completion":
            return self._completion_result(request)
        raise AssertionError(f"unexpected request: {request}")

    def _discovery_result(self, request: OperationRequest) -> OperationResult:
        decision_id = str(request.public_inputs["decision_id"])
        candidates = (
            ()
            if decision_id == self.empty_decision
            else self._candidates(decision_id)
        )
        return self._result(
            request,
            CheckpointStatus.VERIFIED,
            candidates=candidates,
        )

    def _qualification_result(self, request: OperationRequest) -> OperationResult:
        decision_id = str(request.public_inputs["decision_id"])
        if decision_id != self.fail_qualification:
            return self._result(request, CheckpointStatus.VERIFIED)
        return self._result(
            request,
            CheckpointStatus.BLOCKED,
            error_kind="qualification_failed",
            repair="Select another returned candidate.",
            evidence=(
                {
                    "summary": "fixture qualification observed blocked",
                    "observed": False,
                },
            ),
        )

    def _stage_boundary_result(self, request: OperationRequest) -> OperationResult:
        should_fail = (
            request.operation_id == self.failed_stage_probe_once
            and self.stage_probe_failures_remaining > 0
        )
        if not should_fail:
            return self._result(request, CheckpointStatus.VERIFIED)
        self.stage_probe_failures_remaining -= 1
        return self._result(
            request,
            CheckpointStatus.BLOCKED,
            error_kind=f"{request.operation_id}_blocked",
            repair="Repair the fixture stage boundary and retry.",
        )

    def _planned_action_result(self, request: OperationRequest) -> OperationResult:
        operation_id = _PRE_APPLY_PROBE_OWNERS.get(
            request.operation_id,
            request.operation_id,
        )
        action = PlannedAction(
            id=f"host.{operation_id}",
            title=f"Apply {operation_id}",
            mutation_kind="fixture_mutation",
            target=(
                f"$TARGET/{operation_id}"
                + (f"?revision={self.action_revision}" if self.action_revision else "")
            ),
            requires_confirmation=True,
            condition_or_evidence_ref=f"{operation_id}.required",
        )
        return self._result(
            request,
            CheckpointStatus.PENDING,
            actions=(action,),
        )

    def _post_apply_result(self, request: OperationRequest) -> OperationResult:
        failed = self.failed_post_probe
        if failed is None or not request.operation_id.endswith(f".{failed}"):
            return self._result(request, CheckpointStatus.VERIFIED)
        return self._result(
            request,
            CheckpointStatus.BLOCKED,
            error_kind=f"{failed}_invalid",
            repair="Fixture identity postcondition failed.",
        )

    def _completion_result(self, request: OperationRequest) -> OperationResult:
        if not self.incomplete_completion or request.operation_id == "git_checkout_valid":
            return self._result(request, CheckpointStatus.VERIFIED)
        return self._result(
            request,
            CheckpointStatus.BLOCKED,
            error_kind="completion_incomplete",
            repair="Fixture leaves non-health completion unresolved.",
        )

    def _candidates(self, decision_id: str) -> tuple[DiscoveredCandidate, ...]:
        if decision_id == "embedding_model":
            metadata = {
                "provider": f"lm_studio_{self.metadata_revision}",
                "embedding_capable": True,
                "dimensions": 768,
                "local_disk_bytes": 1000,
            }
            other_metadata = {**metadata, "dimensions": 1024, "local_disk_bytes": 2000}
        else:
            metadata = {
                "provider": f"lm_studio_{self.metadata_revision}",
                "context_tokens": 32768,
                "structured_output_support": True,
                "estimated_memory_bytes": 3000,
            }
            other_metadata = {**metadata, "context_tokens": 65536, "estimated_memory_bytes": 4000}
        if decision_id == self.missing_metadata_decision:
            required_key = "dimensions" if decision_id == "embedding_model" else "context_tokens"
            metadata.pop(required_key, None)
            other_metadata.pop(required_key, None)
        return (
            DiscoveredCandidate(
                decision_id=decision_id,
                value=f"{decision_id}.recommended",
                label=f"Recommended {decision_id} {self.label_suffix}",
                recommendation_rank=0,
                metadata=metadata,
            ),
            DiscoveredCandidate(
                decision_id=decision_id,
                value=f"{decision_id}.alternate",
                label=f"Alternate {decision_id}",
                recommendation_rank=1,
                metadata=other_metadata,
            ),
        )

    @staticmethod
    def _result(
        request: OperationRequest,
        status: CheckpointStatus,
        *,
        actions: tuple[PlannedAction, ...] = (),
        candidates: tuple[DiscoveredCandidate, ...] = (),
        error_kind: str | None = None,
        repair: str | None = None,
        evidence: tuple[dict[str, JsonValue], ...] = (),
    ) -> OperationResult:
        return OperationResult(
            request_id=request.request_id,
            operation_id=request.operation_id,
            phase=request.phase,
            probe_purpose=request.probe_purpose,
            checkpoint_status=status,
            error_kind=error_kind,
            retry_safe=True,
            exit_code=0,
            timed_out=False,
            duration_ms=1,
            stdout="",
            stderr="",
            planned_actions=actions,
            discovered_candidates=candidates,
            evidence=evidence,
            repair=repair,
        )


def _prepare(
    root: Path,
    *,
    name: str,
    inference_implementation: str = "lm_studio",
    hydration_vector: bool = False,
) -> tuple[ManagerPaths, CreateConfig, CreateManager, Transaction]:
    home = root / f"manager-{name}"
    paths = ManagerPaths.resolve(explicit_home=home, home=root)
    target = root / "Solets" / name
    target.mkdir(parents=True)
    if hydration_vector:
        _make_hydration_vector(target)
    target_contracts = target_contract_directory(target)
    shutil.copytree(_CONTRACTS, target_contracts)
    seed = SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-2026-08-20",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "macos-bizops",
    )
    bundle = ContractBundle.load(source_revision=seed.commit, directory=target_contracts)
    config = CreateConfig(
        name=name,
        target=target.resolve(),
        autostart=True,
        decisions={
            "inference_implementation": inference_implementation,
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
    plan = build_setup_plan(
        bundle=bundle,
        config=config,
        seed=seed,
        journal_path=paths.transaction_path(name),
        decision_selections=config.decisions,
        decision_sources=config.decision_sources,
    )
    transaction = Transaction.create(
        name=name,
        target=config.target,
        input_fingerprint=canonical_sha256(config.to_identity_dict()),
        answers=plan.answers,
        seed=seed,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=tuple(bundle.stages),
        completion_probe_ids=bundle.completion_probe_ids,
        stage_probe_statuses=initial_stage_probe_statuses(bundle, plan.answers),
    )
    write_transaction(paths.transaction_path(name), transaction)
    seed_lock = root / f"{name}.seed.lock.json"
    seed_lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": seed.repository,
                "release_tag": seed.release_tag,
                "commit": seed.commit,
                "tree_hash": seed.tree_hash,
                "archive_sha256": seed.archive_sha256,
                "profile": seed.profile,
            }
        ),
        encoding="utf-8",
    )
    manager = CreateManager(paths=paths, contract_directory=None, seed_lock_path=seed_lock)
    return paths, config, manager, transaction


def _make_hydration_vector(target: Path) -> None:
    target_python = target / ".venv" / "bin" / "python3"
    target_python.parent.mkdir(parents=True)
    target_python.touch()
    adapter_module = (
        target
        / "plugins"
        / "github_midwife_plugin"
        / "src"
        / "github_midwife_plugin"
        / "setup_adapter.py"
    )
    adapter_module.parent.mkdir(parents=True)
    adapter_module.touch()


def _apply_current_frontier(
    manager: CreateManager,
    config: CreateConfig,
    *,
    decision_selections: dict[str, str] | None = None,
) -> tuple[CommandResult, CommandResult]:
    preview = manager.preview(config, decision_selections=decision_selections)
    _check(preview.status == "preview_ready", "current frontier reaches preview_ready")
    result = manager.create(
        config,
        approved_fingerprint=str(preview.data["approval_fingerprint"]),
        decision_selections=decision_selections,
    )
    return preview, result


def _advance_to_model_review(
    manager: CreateManager,
    config: CreateConfig,
    *,
    decision_selections: dict[str, str] | None = None,
) -> tuple[CommandResult, CommandResult, CommandResult]:
    dependencies, dependencies_result = _apply_current_frontier(
        manager,
        config,
        decision_selections=decision_selections,
    )
    _check(
        dependencies.data["frontier"] == ["system_dependencies"]
        and dependencies_result.error_kind == "next_stage_preview_required",
        "system dependency approval advances only to the next frontier",
    )
    genesis, genesis_result = _apply_current_frontier(
        manager,
        config,
        decision_selections=decision_selections,
    )
    _check(
        genesis.data["frontier"] == ["genesis"]
        and genesis_result.error_kind == "next_stage_preview_required",
        "genesis approval advances only to the model frontier",
    )
    review = manager.preview(
        config,
        decision_selections=decision_selections,
    )
    return dependencies, genesis, review


def _finish_from_model_review(
    manager: CreateManager,
    config: CreateConfig,
    selections: dict[str, str],
) -> tuple[CommandResult, CommandResult, CommandResult]:
    models, models_result = _apply_current_frontier(
        manager,
        config,
        decision_selections=selections,
    )
    _check(
        models.data["frontier"] == ["models"]
        and models_result.error_kind == "next_stage_preview_required",
        "model approval advances only to the coding-agent frontier",
    )
    coding_agents, result = _apply_current_frontier(manager, config)
    _check(
        coding_agents.data["frontier"] == ["coding_agents"],
        "coding-agent frontier is reviewed separately",
    )
    return models, coding_agents, result


@dataclass(frozen=True)
class VerifiedContext:
    paths: ManagerPaths
    config: CreateConfig
    manager: CreateManager
    fake: FakeAdapter
    selections: dict[str, str]
    transaction: Transaction


def _initial_stages_verified(initial: CommandResult) -> bool:
    statuses = initial.data.get("stage_statuses")
    return bool(
        isinstance(statuses, dict)
        and statuses.get("preflight") == "verified"
        and statuses.get("decision_review") == "verified"
    )


def _no_future_decision_requests(fake: FakeAdapter) -> bool:
    return not any(
        request.probe_purpose in {"decision_discovery", "decision_qualification"}
        for request in fake.requests
    )


def _purity_stop_is_clean(
    before: bytes,
    path: Path,
    fake: FakeAdapter,
) -> bool:
    return before == path.read_bytes() and not any(
        request.phase == "apply" for request in fake.requests
    )


def _stage_request_is_valid(request: OperationRequest) -> bool:
    return (
        request.operation_ref == "setup::git.verify_checkout"
        and str(uuid.UUID(request.request_id)) == request.request_id
    )


def _model_decisions_are_visible(preview: CommandResult) -> bool:
    rendered = preview.data.get("decisions")
    if not isinstance(rendered, list):
        return False
    rendered_ids = {
        str(item["id"])
        for item in rendered
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    return {
        "embedding_model",
        "inference_model",
        "session_sources",
    } <= rendered_ids and "connectors_to_configure" not in rendered_ids


def _consents_are_rendered(preview: CommandResult) -> bool:
    consents = preview.data.get("consents")
    required = {"title", "scope", "mutations", "decline_consequence"}
    return isinstance(consents, list) and all(
        isinstance(item, dict) and required.issubset(item) for item in consents
    )


def _persisted_consents_are_true(transaction: Transaction) -> bool:
    consents = transaction.answers.get("consents")
    return isinstance(consents, dict) and bool(consents) and all(consents.values())


def _decision_probe_attempts(transaction: Transaction) -> dict[tuple[object, object], object]:
    return {
        (attempt["stage_id"], attempt["boundary"]): attempt["attempt"]
        for attempt in transaction.stage_probe_attempts
        if attempt["probe_id"] == "decisions_resolved"
    }


def _stage_progress_is_typed(status: CommandResult) -> bool:
    progress = status.data.get("stage_progress")
    expected = {"stage_id", "checkpoint_status", "entry", "operations", "exit"}
    return isinstance(progress, list) and all(
        isinstance(stage, dict) and expected == set(stage) for stage in progress
    )


def _status_rendering_matches(status: CommandResult) -> bool:
    return json.loads(render_json(status)) == status.to_dict() and (
        '"stage_progress"' in render_human(status)
    )


def _start_request_is_bound(
    applied: OperationRequest,
    transaction: Transaction,
) -> bool:
    return (
        applied.approval_fingerprint == transaction.approval_fingerprint
        and applied.timeout_seconds == 120
    )


def _doctor_status_cause_is_precise(
    doctor: CommandResult,
    status: CommandResult,
) -> bool:
    return (
        doctor.status != "verified"
        and status.status != "verified"
        and status.error_kind == "completion_incomplete"
        and status.data.get("cause")
        == {
            "stage_id": "completion",
            "checkpoint_status": "blocked",
            "kind": "operation",
            "operation_id": "lm_studio_jit_disabled",
        }
    )


def _entry_failure_is_safe(result: CommandResult, fake: FakeAdapter) -> bool:
    return (
        result.status == "preview_ready"
        and [item["operation_id"] for item in result.data.get("operations", [])]
        == ["request_homebrew_install"]
        and not any(
            request.phase == "apply"
            for request in fake.requests
        )
    )


def _apply_counts(fake: FakeAdapter) -> dict[str, int]:
    operation_ids = {
        request.operation_id for request in fake.requests if request.phase == "apply"
    }
    return {
        operation_id: sum(
            request.phase == "apply" and request.operation_id == operation_id
            for request in fake.requests
        )
        for operation_id in operation_ids
    }


def _exit_resume_is_safe(
    result: CommandResult,
    fake: FakeAdapter,
) -> bool:
    counts = _apply_counts(fake)
    exit_attempts = sum(
        request.operation_id == "postgres_ready"
        and request.probe_purpose == "stage_exit"
        for request in fake.requests
    )
    return bool(
        result.error_kind == "next_stage_preview_required"
        and counts
        and set(counts.values()) == {1}
        and exit_attempts == 2
    )


def _frontier_chain_is_precise(
    first: CommandResult,
    first_count: int,
    second: CommandResult,
    second_count: int,
    third: CommandResult,
    discovery_count: int,
) -> bool:
    return (
        first.data.get("frontier") == ["system_dependencies"]
        and first_count == 4
        and second.data.get("frontier") == ["genesis"]
        and second_count == 2
        and third.status == "preview_ready"
        and third.data.get("frontier") == ["models"]
        and discovery_count == 2
    )


def _crash_resume_is_safe(
    result: CommandResult,
    fake: FakeAdapter,
    operation_id: str,
) -> bool:
    applies = sum(
        request.phase == "apply" and request.operation_id == operation_id
        for request in fake.requests
    )
    post_probe = any(
        request.probe_purpose == "post_apply"
        and _PRE_APPLY_PROBE_OWNERS.get(request.operation_id, request.operation_id)
        == operation_id
        for request in fake.requests
    )
    return (
        result.error_kind == "next_stage_preview_required"
        and applies == 1
        and post_probe
    )


def _state_changing_read_only_plan(
    original: object,
    **kwargs: object,
) -> SetupPlan:
    plan = original(**kwargs)  # type: ignore[operator]
    if (
        kwargs.get("prospective_consents") is not False
        or kwargs.get("resolution_stage_ids") != {"preflight"}
    ):
        return plan
    changed_answers = json.loads(json.dumps(plan.answers))
    changed_answers["decisions"]["coding_agents"] = ["codex"]
    changed_answers["consents"]["system_change_consent"] = False
    return SetupPlan(
        answers=changed_answers,
        operations=plan.operations,
        unresolved_decisions=plan.unresolved_decisions,
        unresolved_consents=plan.unresolved_consents,
    )


def _find_request(
    fake: FakeAdapter,
    *,
    operation_id: str | None = None,
    phase: str | None = None,
    purpose: str | None = None,
) -> OperationRequest:
    for request in fake.requests:
        if operation_id is not None and request.operation_id != operation_id:
            continue
        if phase is not None and request.phase != phase:
            continue
        if purpose is not None and request.probe_purpose != purpose:
            continue
        return request
    raise AssertionError("expected adapter request was not recorded")
