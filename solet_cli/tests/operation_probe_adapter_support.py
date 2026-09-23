"""Real bootstrap-route regression checks for operation-owned probes."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from solet_manager import operation_executor  # noqa: E402
from solet_manager.adapters import (  # noqa: E402
    OperationRequest,
    OperationResult,
    PlannedAction,
)
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.errors import StateConflictError  # noqa: E402
from solet_manager.flow import PlannedOperation  # noqa: E402
from solet_manager.models import CheckpointStatus, CommandResult, ExitCode, JsonValue  # noqa: E402
from solet_manager.operation_records import (  # noqa: E402
    operation_probe_request,
)
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import (  # noqa: E402
    Transaction,
)

from bootstrap_adapter.routes import execute_adapter_request  # noqa: E402

_CONTRACTS = Path(__file__).resolve().parents[2] / "plugins/github_midwife_plugin/knowledge_base"
_STAGE_ID = "system_dependencies"
_EXPECTED_PRE_APPLY_PROBES = (
    ("request_homebrew_install", "homebrew_available", "bootstrap::homebrew.probe"),
    ("install_python_runtime", "python_version_valid", "bootstrap::python.probe_version"),
    (
        "build_instance_environment",
        "instance_environment_dependency_closure_valid",
        "bootstrap::environment.probe_dependency_closure",
    ),
    (
        "install_postgresql",
        "postgres_binary_version_valid",
        "bootstrap::postgres.probe_version",
    ),
    (
        "configure_postgresql",
        "postgres_role_policy_valid",
        "bootstrap::postgres.probe_role_policy",
    ),
)


def operation_owned_probe_requests_use_probe_identity() -> None:
    """Drive every bootstrap pre-apply probe through the real closed route."""

    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        transaction = _transaction(bundle, root / "target")
        observed: list[tuple[str, str, str]] = []
        recorded_owner = False
        for operation_id, probe_id, probe_ref in _EXPECTED_PRE_APPLY_PROBES:
            operation = _operation(bundle, operation_id)
            runner, request = operation_probe_request(
                transaction,
                bundle,
                operation,
                probe_id=probe_id,
                purpose="pre_apply",
                attempt=1,
            )
            observed.append((operation_id, request.operation_id, request.operation_ref))
            _require(runner == "bootstrap", f"{operation_id} pre-apply probe uses bootstrap")
            _require(
                request.operation_id == probe_id
                and request.operation_ref == probe_ref
                and request.public_inputs == {},
                f"{operation_id} emits its probe's exact adapter identity and inputs",
            )
            raw_result = execute_adapter_request(
                request.to_dict(),
                runner=_unavailable_runner,
                which=_unavailable_which,
                now=_fixed_now,
            )
            _require(
                raw_result["error_kind"] not in {"adapter_missing", "adapter_protocol_error"},
                f"{operation_id} probe reaches its registered bootstrap route",
            )
            if operation_id == "request_homebrew_install":
                _assert_parent_journal_owner(root, transaction, operation, raw_result, request)
                recorded_owner = True
        _assert_pgvector_blocked_evidence(bundle, transaction, root)
        _require(
            observed == list(_EXPECTED_PRE_APPLY_PROBES),
            "all bootstrap probe triples are exact",
        )
        _require(recorded_owner, "parent-operation journal ownership was exercised")
        _assert_model_qualification_projections(bundle, transaction)


def _assert_model_qualification_projections(
    bundle: ContractBundle,
    transaction: Transaction,
) -> None:
    selections = (
        (
            "configure_lm_studio_embeddings",
            "embedding_model",
            "fixture-embedding-model",
            "embedding_model_qualification",
            "setup::models.qualify_embedding",
        ),
    )
    for operation_id, decision_id, selected, probe_id, probe_ref in selections:
        _assert_model_qualification_projection(
            bundle,
            transaction,
            operation_id=operation_id,
            decision_id=decision_id,
            selected=selected,
            probe_id=probe_id,
            probe_ref=probe_ref,
        )


def _assert_model_qualification_projection(
    bundle: ContractBundle,
    transaction: Transaction,
    *,
    operation_id: str,
    decision_id: str,
    selected: str,
    probe_id: str,
    probe_ref: str,
) -> None:
    operation = _operation(bundle, operation_id)
    resolved = replace(
        transaction,
        answers={
            "decisions": {
                "autostart": "disabled",
                "coding_agents": ["codex"],
                decision_id: selected,
            }
        },
    )
    runner, request = operation_probe_request(
        resolved,
        bundle,
        operation,
        probe_id=probe_id,
        purpose="post_apply",
        attempt=1,
    )
    _require(
        runner == "hydration"
        and request.operation_ref == probe_ref
        and request.public_inputs == {"candidate_id": selected},
        f"{operation_id} postcondition projects only the resolved candidate selection",
    )
    for label, answers in (
        ("missing", {"decisions": {}}),
        ("invalid", {"decisions": {decision_id: [selected]}}),
    ):
        try:
            operation_probe_request(
                replace(resolved, answers=answers),
                bundle,
                operation,
                probe_id=probe_id,
                purpose="post_apply",
                attempt=1,
            )
        except StateConflictError:
            continue
        raise AssertionError(f"{label} {decision_id} selection must fail closed")


def _assert_pgvector_blocked_evidence(
    bundle: ContractBundle,
    transaction: Transaction,
    root: Path,
) -> None:
    operation = _operation(bundle, "install_postgresql")
    _runner, request = operation_probe_request(
        transaction,
        bundle,
        operation,
        probe_id="pgvector_ready",
        purpose="post_apply",
        attempt=1,
    )
    prefix = root / "pgvector-prefix"

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(command)
        if command[0] == "/fixture/brew":
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(command, 0, "Homebrew 4.0\n", "")
            if command[1:] == ["list", "--formula"]:
                return subprocess.CompletedProcess(command, 0, "postgresql@17\npgvector\n", "")
            if command[1:] == ["--prefix", "postgresql@17"]:
                return subprocess.CompletedProcess(command, 0, f"{prefix}\n", "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "psql (PostgreSQL) 17.5\n", "")
        if command[0].endswith("pg_isready"):
            return subprocess.CompletedProcess(command, 0, "accepting connections\n", "")
        if "pg_available_extensions" in joined:
            return subprocess.CompletedProcess(command, 0, "1\n", "")
        if "pg_extension" in joined:
            return subprocess.CompletedProcess(command, 0, "0\n", "")
        return subprocess.CompletedProcess(command, 1, "", "unhandled fixture command")

    raw_result = execute_adapter_request(
        request.to_dict(),
        runner=runner,
        which=lambda name: "/fixture/brew" if name == "brew" else name,
        now=_fixed_now,
    )
    evidence = raw_result["evidence"]
    _require(
        raw_result["checkpoint_status"] == "blocked"
        and any(item["observed"] != item["expected"] for item in evidence),
        "blocked pgvector route emits evidence that disagrees with its expected state",
    )


class _RouteSplitAdapter:
    """Explicit parent-route versus declared-probe fixture for this drift class."""

    def __init__(
        self,
        operation: PlannedOperation,
        *,
        blocked_precondition: bool = False,
        error_kind: str = "fixture_precondition_blocked",
        pre_apply_revision: str = "approved",
        blocked_postcondition_probe_id: str | None = None,
        preconditions_verified: bool = False,
        inference_pre_probe_timeout: bool = False,
        inference_inventory_error_kind: str | None = None,
        inference_preview_error_kind: str | None = None,
    ) -> None:
        self.operation = operation
        self.blocked_precondition = blocked_precondition
        self.error_kind = error_kind
        self.pre_apply_revision = pre_apply_revision
        self.blocked_postcondition_probe_id = blocked_postcondition_probe_id
        self.preconditions_verified = preconditions_verified
        self.inference_pre_probe_timeout = inference_pre_probe_timeout
        self.inference_inventory_error_kind = inference_inventory_error_kind
        self.inference_preview_error_kind = inference_preview_error_kind
        self.requests: list[tuple[str, OperationRequest]] = []
        self.mutated_request_ids: list[str] = []
        self.apply_calls = 0
        self.parent_pre_apply_calls = 0

    def __call__(
        self,
        _registry: object,
        *,
        runner: str,
        request: OperationRequest,
    ) -> OperationResult:
        self.requests.append((runner, request))
        if request.phase == "apply":
            return self._apply_result(request)
        if request.probe_purpose == "post_apply":
            return self._post_apply_result(request)
        if request.operation_id == self.operation.operation_id:
            return self._parent_pre_apply_result(request)
        if request.operation_id in self.operation.precondition_probe_ids:
            return self._precondition_result(request)
        return _result(request, CheckpointStatus.VERIFIED)

    def _apply_result(self, request: OperationRequest) -> OperationResult:
        self.mutated_request_ids.append(request.request_id)
        self.apply_calls += 1
        return _result(request, CheckpointStatus.APPLIED)

    def _post_apply_result(self, request: OperationRequest) -> OperationResult:
        if request.operation_id == self.blocked_postcondition_probe_id:
            return _result(request, CheckpointStatus.BLOCKED, error_kind=self.error_kind)
        return _result(request, CheckpointStatus.VERIFIED)

    def _parent_pre_apply_result(self, request: OperationRequest) -> OperationResult:
        self.parent_pre_apply_calls += 1
        if request.probe_purpose == "preview" and self.inference_preview_error_kind is not None:
            return _failed_inference_result(request, self.inference_preview_error_kind)
        if self.inference_pre_probe_timeout and self.parent_pre_apply_calls == 1:
            return _failed_inference_result(request, "adapter_timeout")
        if self.inference_inventory_error_kind is not None and self.parent_pre_apply_calls == 2:
            return _failed_inference_result(
                request,
                self.inference_inventory_error_kind,
                actions=(_planned_action(self.operation, self.pre_apply_revision),),
            )
        return _result(
            request,
            CheckpointStatus.PENDING,
            actions=(_planned_action(self.operation, self.pre_apply_revision),),
        )

    def _precondition_result(self, request: OperationRequest) -> OperationResult:
        if self.preconditions_verified:
            return _result(request, CheckpointStatus.VERIFIED)
        if self.blocked_precondition:
            return _result(request, CheckpointStatus.BLOCKED, error_kind=self.error_kind)
        return _result(request, CheckpointStatus.PENDING)


class _FixtureRegistry:
    def refresh_base_python(self) -> None:
        pass


def _failed_inference_result(
    request: OperationRequest,
    error_kind: str,
    *,
    actions: tuple[PlannedAction, ...] = (),
) -> OperationResult:
    return _result(
        request,
        CheckpointStatus.FAILED,
        actions=actions,
        error_kind=error_kind,
        timed_out=error_kind == "adapter_timeout",
        retry_safe=False,
        exit_code=None,
    )


def _parent_pre_apply_probes(
    adapter: _RouteSplitAdapter,
    operation: PlannedOperation,
) -> list[tuple[str, OperationRequest]]:
    return [
        (runner, request)
        for runner, request in adapter.requests
        if request.operation_id == operation.operation_id
        and request.phase == "probe"
        and request.probe_purpose == "pre_apply"
    ]


def _is_safe_parent_reprobe(
    operation: PlannedOperation,
    runner: str,
    request: OperationRequest,
) -> bool:
    return (
        runner == operation.runner
        and request.operation_ref == operation.operation_ref
        and request.dry_run
        and request.approval_fingerprint is None
    )


def _run_pending_case(
    bundle: ContractBundle,
    root: Path,
    operation: PlannedOperation,
    adapter: _RouteSplitAdapter,
    *,
    approval: str = "sha256:" + "a" * 64,
    refreshed: str = "sha256:" + "b" * 64,
    result_kind: str | None = None,
    prior_status: CheckpointStatus = CheckpointStatus.PENDING,
) -> operation_executor.OperationOutcome:
    root.mkdir(parents=True)
    transaction = _transaction(bundle, root / "target").bind_operations(
        {operation.operation_id: operation.stage_id}
    ).approve(approval)
    if prior_status is not CheckpointStatus.PENDING:
        transaction = transaction.with_operation_status(operation.operation_id, prior_status)
    if result_kind is not None:
        transaction = transaction.with_result_kind(result_kind)
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    paths.transactions_dir.mkdir(parents=True)
    paths.transactions_dir.chmod(0o700)
    approved_action = _planned_action(operation, "approved")
    approved_actions: dict[str, list[JsonValue]] = {
        operation.operation_id: [
            {"operation_id": operation.operation_id, **approved_action.to_dict()}
        ]
    }
    with patch.object(operation_executor, "invoke_adapter", adapter):
        return operation_executor._run_pending_operation(
            bundle=bundle,
            operation=operation,
            transaction=transaction,
            approved_actions=approved_actions,
            registry=_FixtureRegistry(),
            paths=paths,
            refresh_preview=lambda: CommandResult(
                kind="create",
                status="preview_ready",
                message="fixture refresh",
                exit_code=ExitCode.OK,
                error_kind=None,
                repair=None,
                data={"approval_fingerprint": refreshed},
            ),
        )


def _run_existing_pending_case(
    bundle: ContractBundle,
    root: Path,
    operation: PlannedOperation,
    transaction: Transaction,
    adapter: _RouteSplitAdapter,
) -> operation_executor.OperationOutcome:
    root.mkdir(parents=True)
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    paths.transactions_dir.mkdir(parents=True)
    paths.transactions_dir.chmod(0o700)
    approved_action = _planned_action(operation, "approved")
    approved_actions: dict[str, list[JsonValue]] = {
        operation.operation_id: [
            {"operation_id": operation.operation_id, **approved_action.to_dict()}
        ]
    }
    with patch.object(operation_executor, "invoke_adapter", adapter):
        return operation_executor._run_pending_operation(
            bundle=bundle,
            operation=operation,
            transaction=transaction,
            approved_actions=approved_actions,
            registry=_FixtureRegistry(),
            paths=paths,
            refresh_preview=lambda: CommandResult(
                kind="create",
                status="preview_ready",
                message="fixture refresh",
                exit_code=ExitCode.OK,
                error_kind=None,
                repair=None,
                data={"approval_fingerprint": "sha256:" + "b" * 64},
            ),
        )


def _planned_action(operation: PlannedOperation, revision: str) -> PlannedAction:
    return PlannedAction(
        id=f"fixture.{operation.operation_id}",
        title=f"Apply {operation.operation_id}",
        mutation_kind="fixture_mutation",
        target=f"$TARGET/{operation.operation_id}?revision={revision}",
        requires_confirmation=True,
        condition_or_evidence_ref=f"{operation.operation_id}.required",
    )


def _result(
    request: OperationRequest,
    status: CheckpointStatus,
    *,
    actions: tuple[PlannedAction, ...] = (),
    error_kind: str | None = None,
    timed_out: bool = False,
    retry_safe: bool = True,
    exit_code: int | None = 0,
) -> OperationResult:
    return OperationResult(
        request_id=request.request_id,
        operation_id=request.operation_id,
        phase=request.phase,
        probe_purpose=request.probe_purpose,
        checkpoint_status=status,
        error_kind=error_kind,
        retry_safe=retry_safe,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=0,
        stdout="",
        stderr="",
        planned_actions=actions,
        discovered_candidates=(),
        evidence=(),
        repair="fixture repair" if error_kind else None,
    )


def _raises_state_conflict(callback: object, message: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except Exception as exc:
        _require(
            exc.__class__.__name__ == "StateConflictError",
            f"{message}; got {exc!r}",
        )
    else:
        raise AssertionError(message)


def _transaction(bundle: ContractBundle, target: Path) -> Transaction:
    target.mkdir()
    seed = SeedLock(
        "https://example.invalid/seed.git",
        "fixture",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "fixture",
    )
    return Transaction.create(
        name="operation-probe",
        target=target,
        input_fingerprint="sha256:" + "d" * 64,
        answers={
            "decisions": {
                "autostart": "disabled",
                "coding_agents": ["codex"],
                "embedding_model": "fixture-embedding",
                "inference_model": "fixture-inference",
                "embeddings_implementation": "lm_studio",
                "inference_implementation": "lm_studio",
            },
            "public_inputs": {"lm_studio_base_url": "http://localhost:1234/v1"},
        },
        seed=seed,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest="sha256:" + "e" * 64,
        stage_ids=(_STAGE_ID,),
        completion_probe_ids=(),
    )


def _operation(bundle: ContractBundle, operation_id: str) -> PlannedOperation:
    definition = bundle.operations[operation_id]
    idempotency = definition["idempotency"]
    if not isinstance(idempotency, dict):
        raise AssertionError(f"{operation_id} lacks an idempotency definition")
    preconditions = idempotency["precondition_probe_refs"]
    postconditions = idempotency["postcondition_probe_refs"]
    if not isinstance(preconditions, list) or not isinstance(postconditions, list):
        raise AssertionError(f"{operation_id} probe declarations are invalid")
    return PlannedOperation(
        stage_id=_STAGE_ID,
        operation_id=operation_id,
        operation_ref=str(definition["operation_ref"]),
        runner=str(definition["runner"]),
        risk=str(definition["risk"]),
        requires_confirmation=bool(definition["requires_confirmation"]),
        precondition_probe_ids=tuple(str(value) for value in preconditions),
        postcondition_probe_ids=tuple(str(value) for value in postconditions),
        public_inputs={"solet_name": "operation-probe"}
        if operation_id == "configure_postgresql"
        else {},
    )


def _assert_parent_journal_owner(
    root: Path,
    transaction: Transaction,
    operation: PlannedOperation,
    raw_result: dict[str, object],
    request: object,
) -> None:
    if not hasattr(request, "request_id"):
        raise AssertionError("operation probe request has no request identity")
    result = OperationResult.from_dict(raw_result, request)
    bound = transaction.bind_operations({operation.operation_id: operation.stage_id})
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    paths.transactions_dir.mkdir(parents=True)
    paths.transactions_dir.chmod(0o700)
    updated = operation_executor._record_operation_result(
        bound,
        operation,
        result,
        phase="pre_probe",
        attempt=1,
        paths=paths,
    )
    attempt = updated.operation_attempts[-1]
    _require(
        attempt["operation_id"] == operation.operation_id,
        "probe result journals under the parent operation, not the probe identity",
    )
    _require(
        Transaction.from_dict(updated.to_dict()).operation_attempts[-1]["operation_id"]
        == operation.operation_id,
        "journal validation retains the parent operation attribution",
    )


def _unavailable_runner(
    command: list[str], **_kwargs: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 1, "", "")


def _unavailable_which(_name: str) -> str | None:
    return None


def _fixed_now() -> datetime:
    return datetime(2026, 8, 31, tzinfo=UTC)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
