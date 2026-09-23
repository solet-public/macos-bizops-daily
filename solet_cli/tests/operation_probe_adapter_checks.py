"""Real bootstrap-route regression checks for operation-owned probes."""

# ruff: noqa: E402

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from operation_probe_adapter_support import (  # noqa: E402
    _FixtureRegistry,
    _is_safe_parent_reprobe,
    _operation,
    _parent_pre_apply_probes,
    _raises_state_conflict,
    _require,
    _result,
    _RouteSplitAdapter,
    _run_existing_pending_case,
    _run_pending_case,
    _transaction,
)
from operation_probe_adapter_support import (
    operation_owned_probe_requests_use_probe_identity as _operation_owned_probe_requests_use_probe_identity,
)
from solet_manager import preview_engine  # noqa: E402
from solet_manager.adapters import (  # noqa: E402
    OperationRequest,
)
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.doctor_inference_qualification import (  # noqa: E402
    collect_inference_pre_probe_timeout_advisories,
    collect_inference_probe_advisories,
)
from solet_manager.flow import SetupPlan  # noqa: E402
from solet_manager.inference_probe_policy import (  # noqa: E402
    ADVISORY_ERROR_KIND,
    advisory_inference_probe_result,
)
from solet_manager.models import CheckpointStatus  # noqa: E402
from solet_manager.transaction import (  # noqa: E402
    load_transaction,
)

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
    _operation_owned_probe_requests_use_probe_identity()


def planned_action_drift_uses_operation_route() -> None:
    """Exercise the route-identity and remediation boundary without owner-mapping fakes."""

    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _postgres_route_split_regressions(bundle, root)
        _approved_inventory_action_regression(bundle, root)
        _true_drift_regression(bundle, root)
        _contract_route_closure_and_probe_purity(bundle, root)
        _post_apply_block_regression(bundle, root)
        from operation_attempt_reconciliation_smoke import interrupted_apply_reconciliation_regression

        interrupted_apply_reconciliation_regression(bundle, root)


def _postgres_route_split_regressions(bundle: ContractBundle, root: Path) -> None:
    _install_postgresql_route_split_regression(bundle, root)
    _configure_postgresql_route_split_regression(bundle, root)
    _no_drift_controls(bundle, root)


def _install_postgresql_route_split_regression(
    bundle: ContractBundle,
    root: Path,
) -> None:
    install = _operation(bundle, "install_postgresql")
    blocked_probe = "postgres_binary_version_valid"
    original_remediation = bundle.probes[blocked_probe]["remediation_operation_refs"]
    bundle.probes[blocked_probe]["remediation_operation_refs"] = []
    try:
        terminal_adapter = _RouteSplitAdapter(
            install,
            blocked_precondition=True,
            error_kind="postgres_probe_not_verified",
        )
        terminal = _run_pending_case(
            bundle,
            root / "postgres-terminal",
            install,
            terminal_adapter,
        )
    finally:
        bundle.probes[blocked_probe]["remediation_operation_refs"] = original_remediation
    _require(
        terminal.terminal_result is not None
        and terminal.terminal_result.error_kind == "postgres_probe_not_verified"
        and terminal.transaction.result_kind != "probe_drift",
        "install_postgresql preserves its declared blocked error when remediation does not apply",
    )
    _require(
        not any(request.phase == "apply" for _runner, request in terminal_adapter.requests),
        "terminal install_postgresql precondition does not apply",
    )

    remediable_adapter = _RouteSplitAdapter(
        install,
        blocked_precondition=True,
        error_kind="postgres_probe_not_verified",
    )
    remediable = _run_pending_case(
        bundle,
        root / "postgres-remediable",
        install,
        remediable_adapter,
    )
    _require(
        remediable.terminal_result is None,
        "install_postgresql blocked precondition does not become probe_drift",
    )
    _require(
        any(request.phase == "apply" for _runner, request in remediable_adapter.requests),
        "install_postgresql remediation named by its probe reaches apply",
    )
    _require(
        all(
            attempt["operation_id"] == install.operation_id
            for attempt in remediable.transaction.operation_attempts
        ),
        "declared probe and operation-route inventory preserve parent journal attribution",
    )


def _configure_postgresql_route_split_regression(
    bundle: ContractBundle,
    root: Path,
) -> None:
    configure = _operation(bundle, "configure_postgresql")
    configure_adapter = _RouteSplitAdapter(
        configure,
        blocked_precondition=True,
        error_kind="postgres_role_policy_valid",
    )
    configured = _run_pending_case(
        bundle,
        root / "postgres-configure",
        configure,
        configure_adapter,
    )
    _require(
        configured.terminal_result is None
        and any(request.phase == "apply" for _runner, request in configure_adapter.requests),
        "configure_postgresql uses its parent route and remediates its declared policy probe",
    )
    _require(
        configure_adapter.error_kind
        in [attempt["error_kind"] for attempt in configured.transaction.operation_attempts],
        "configure_postgresql journals its declared policy error under the parent operation",
    )


def _no_drift_controls(bundle: ContractBundle, root: Path) -> None:
    environment = _operation(bundle, "build_instance_environment")
    environment_adapter = _RouteSplitAdapter(environment)
    environment_result = _run_pending_case(
        bundle,
        root / "dependency-closure",
        environment,
        environment_adapter,
    )
    _require(
        environment_result.terminal_result is None
        and any(request.phase == "apply" for _runner, request in environment_adapter.requests),
        "build_instance_environment remains a no-drift apply control",
    )

    connector = _operation(bundle, "configure_salesforce")
    connector_adapter = _RouteSplitAdapter(connector)
    connector_result = _run_pending_case(
        bundle,
        root / "connector",
        connector,
        connector_adapter,
    )
    _require(
        connector_result.terminal_result is None
        and any(request.phase == "apply" for _runner, request in connector_adapter.requests),
        "connector operation compares its parent inventory rather than its actionless probe",
    )
    _inference_probe_policy_regression(bundle, root)


def _inference_probe_policy_regression(
    bundle: ContractBundle,
    root: Path,
) -> None:
    """Every inference-probe consumer records timeout and empty output as advisory."""

    inference = _operation(bundle, "configure_lm_studio_inference")
    inference = replace(inference, public_inputs={"model": "fixture-inference"})
    adapter = _RouteSplitAdapter(inference, inference_pre_probe_timeout=True)
    outcome = _run_pending_case(bundle, root / "inference-pre-probe-timeout", inference, adapter)
    timeout_attempt = outcome.transaction.operation_attempts[0]
    advisories = collect_inference_pre_probe_timeout_advisories(outcome.transaction)
    _require(
        outcome.terminal_result is None
        and adapter.apply_calls == 1
        and outcome.transaction.operation_statuses[inference.operation_id]
        is CheckpointStatus.VERIFIED,
        "inference pre-probe timeout keeps the served-model configuration on its apply path",
    )
    _require(
        timeout_attempt["phase"] == "pre_probe"
        and timeout_attempt["checkpoint_status"] == CheckpointStatus.VERIFIED.value
        and timeout_attempt["error_kind"] == ADVISORY_ERROR_KIND
        and timeout_attempt["timed_out"] is True,
        "inference pre-probe timeout remains durably recorded as an advisory",
    )
    _require(
        len(advisories) == 1
        and advisories[0]["check_id"] == "doctor::inference_configuration_pre_probe"
        and advisories[0]["status"] == "warn"
        and advisories[0]["blocking"] is False
        and advisories[0]["observed"] == {
            "checkpoint_status": CheckpointStatus.VERIFIED.value,
            "error_kind": ADVISORY_ERROR_KIND,
            "timed_out": True,
            "duration_ms": 0,
        },
        "doctor renders the retained inference pre-probe outcome as a non-blocking advisory",
    )
    _all_inference_probe_kinds_are_advisory(bundle, root)
    _preview_and_inventory_inference_probe_regressions(bundle, root)


def _preview_and_inventory_inference_probe_regressions(
    bundle: ContractBundle,
    root: Path,
) -> None:
    inference = replace(
        _operation(bundle, "configure_lm_studio_inference"),
        public_inputs={"model": "fixture-inference"},
    )
    transaction = _transaction(bundle, root / "preview-target")
    preview = SetupPlan(transaction.answers, (inference,), (), ())
    for error_kind in ("adapter_timeout", "adapter_empty_content"):
        preview_adapter = _RouteSplitAdapter(
            inference,
            inference_preview_error_kind=error_kind,
        )
        with patch.object(preview_engine, "invoke_adapter", preview_adapter):
            preview_results, preview_failures = preview_engine._probe_operations(
                transaction,
                bundle,
                preview,
                _FixtureRegistry(),
            )
        preview_result = preview_results[inference.operation_id]
        _require(
            preview_result.checkpoint_status is CheckpointStatus.VERIFIED
            and preview_result.error_kind == ADVISORY_ERROR_KIND
            and not preview_failures,
            f"preview {error_kind} is advisory for the selected served inference model",
        )
        inventory_adapter = _RouteSplitAdapter(
            inference,
            inference_pre_probe_timeout=True,
            inference_inventory_error_kind=error_kind,
        )
        outcome = _run_pending_case(
            bundle,
            root / f"inventory-{error_kind}",
            inference,
            inventory_adapter,
        )
        _require(
            outcome.terminal_result is None
            and inventory_adapter.apply_calls == 1
            and outcome.transaction.operation_statuses[inference.operation_id]
            is CheckpointStatus.VERIFIED,
            f"repeat inventory {error_kind} cannot block inference configuration",
        )


def _all_inference_probe_kinds_are_advisory(bundle: ContractBundle, root: Path) -> None:
    transaction = _transaction(bundle, root / "policy-target")
    answers = transaction.answers
    controls = (
        ("pre_probe", "setup::models.configure_lm_studio_inference", "pre_apply"),
        ("post_apply", "setup::models.configure_lm_studio_inference", "post_apply"),
        ("qualification", "setup::models.qualify_structured_actions", "decision_qualification"),
        ("representative", "setup::models.qualify_representative_inference", "completion"),
        ("identity_completion", "service_interface::inference_service.qualify", "completion"),
        ("identity_stage_entry", "service_interface::inference_service.qualify", "stage_entry"),
        ("identity_stage_exit", "service_interface::inference_service.qualify", "stage_exit"),
    )
    for label, operation_ref, purpose in controls:
        request = OperationRequest(
            request_id="00000000-0000-4000-8000-000000000007",
            operation_id=f"inference.{label}",
            operation_ref=operation_ref,
            phase="probe",
            probe_purpose=purpose,
            attempt=1,
            name="operation-probe",
            target=root / label,
            flow_id=bundle.flow_id,
            flow_source_revision=bundle.source_revision,
            answers_fingerprint="sha256:" + "a" * 64,
            approval_fingerprint=None,
            dry_run=True,
            timeout_seconds=30,
            public_inputs={},
        )
        for error_kind, timed_out in (("adapter_timeout", True), ("adapter_empty_content", False)):
            observed = advisory_inference_probe_result(
                answers,
                request,
                _result(
                    request,
                    CheckpointStatus.FAILED,
                    error_kind=error_kind,
                    timed_out=timed_out,
                    retry_safe=False,
                    exit_code=None,
                ),
            )
            _require(
                observed.checkpoint_status is CheckpointStatus.VERIFIED
                and observed.error_kind == ADVISORY_ERROR_KIND,
                f"{label} {error_kind} is advisory for the selected served inference model",
            )
    exact_refusal = advisory_inference_probe_result(
        answers,
        request,
        _result(request, CheckpointStatus.FAILED, error_kind="inference_model_identity_mismatch"),
    )
    embedding = advisory_inference_probe_result(
        answers,
        replace(request, operation_ref="setup::models.qualify_embedding"),
        _result(request, CheckpointStatus.FAILED, error_kind="adapter_timeout"),
    )
    _require(
        exact_refusal.checkpoint_status is CheckpointStatus.FAILED
        and embedding.checkpoint_status is CheckpointStatus.FAILED,
        "exact inference identity refusal and embedding qualification remain decisive",
    )
    journal_attempts = tuple(
        {
            "operation_id": f"inference.{label}",
            "checkpoint_status": CheckpointStatus.VERIFIED.value,
            "error_kind": ADVISORY_ERROR_KIND,
            "timed_out": label.endswith("timeout"),
            "duration_ms": 0,
        }
        for label, _operation_ref, _purpose in controls[:5]
    )
    stage_attempts = tuple(
        {
            "probe_id": f"inference.{label}",
            "checkpoint_status": CheckpointStatus.VERIFIED.value,
            "error_kind": ADVISORY_ERROR_KIND,
            "timed_out": False,
            "duration_ms": 0,
        }
        for label, _operation_ref, _purpose in controls[5:]
    )
    doctor_advisories = collect_inference_probe_advisories(
        replace(
            transaction,
            operation_attempts=journal_attempts,
            stage_probe_attempts=stage_attempts,
        )
    )
    _require(
        len(doctor_advisories) == len(controls)
        and all(
            item["status"] == "warn" and item["blocking"] is False
            for item in doctor_advisories
        ),
        "doctor exposes every journaled inference probe result as advisory",
    )


def _approved_inventory_action_regression(
    bundle: ContractBundle,
    root: Path,
) -> None:
    """An approved action must apply even when a declared probe already verifies."""

    cases = (
        (
            "embedding-qualified",
            _operation(bundle, "configure_lm_studio_embeddings"),
            True,
            CheckpointStatus.PENDING,
        ),
        (
            "inference-qualified",
            _operation(bundle, "configure_lm_studio_inference"),
            True,
            CheckpointStatus.PENDING,
        ),
        (
            "operation-fallback",
            _operation(bundle, "open_background_items_settings"),
            False,
            CheckpointStatus.PENDING,
        ),
        (
            "embedding-resumed-awaiting-user",
            _operation(bundle, "configure_lm_studio_embeddings"),
            True,
            CheckpointStatus.AWAITING_USER,
        ),
    )
    for label, operation, preconditions_verified, prior_status in cases:
        adapter = _RouteSplitAdapter(
            operation,
            preconditions_verified=preconditions_verified,
        )
        _run_pending_case(
            bundle,
            root / label,
            operation,
            adapter,
            prior_status=prior_status,
        )
        _require(
            adapter.apply_calls == 1,
            f"{label} preserves its approved pending inventory action",
        )


def _true_drift_regression(bundle: ContractBundle, root: Path) -> None:
    operation = _operation(bundle, "install_postgresql")
    adapter = _RouteSplitAdapter(operation, pre_apply_revision="changed")
    approval = "sha256:" + "a" * 64
    refreshed = "sha256:" + "b" * 64
    outcome = _run_pending_case(
        bundle,
        root / "true-drift",
        operation,
        adapter,
        approval=approval,
        refreshed=refreshed,
    )
    _require(
        outcome.terminal_result is not None
        and outcome.terminal_result.error_kind == "probe_drift"
        and outcome.terminal_result.data["approval_fingerprint"] == refreshed,
        "changed parent action reports probe_drift with the refreshed approval fingerprint",
    )
    _require(
        refreshed != approval
        and not any(request.phase == "apply" for _runner, request in adapter.requests),
        "true parent-route drift cannot apply or return its rejected fingerprint",
    )
    reloaded = load_transaction(
        root / "true-drift" / "state" / "transactions" / "operation-probe.json"
    )
    _require(
        reloaded is not None
        and reloaded.operation_statuses[operation.operation_id]
        is CheckpointStatus.AWAITING_USER
        and reloaded.result_kind == "probe_drift"
        and reloaded.operation_attempts[-1]["phase"] == "pre_probe"
        and reloaded.operation_attempts[-1]["checkpoint_status"]
        != CheckpointStatus.AWAITING_USER.value,
        "probe-drift refusal journal reloads with its no-action pre-probe attempt",
    )

    same_fingerprint_adapter = _RouteSplitAdapter(operation, pre_apply_revision="changed")
    _raises_state_conflict(
        lambda: _run_pending_case(
            bundle,
            root / "same-fingerprint",
            operation,
            same_fingerprint_adapter,
            approval=approval,
            refreshed=approval,
        ),
        "probe_drift rejects a refreshed fingerprint equal to the rejected approval",
    )


def _contract_route_closure_and_probe_purity(
    bundle: ContractBundle,
    root: Path,
) -> None:
    runners: set[str] = set()
    for operation_id in _operation_ids_with_preconditions(bundle):
        runners.add(_assert_operation_route_reprobe(bundle, root, operation_id))
    _require(
        runners == {"bootstrap", "external_cli", "genesis", "hydration", "platform_process"},
        "every operation runner was exercised",
    )


def _operation_ids_with_preconditions(bundle: ContractBundle) -> tuple[str, ...]:
    operation_ids: list[str] = []
    for operation_id, definition in bundle.operations.items():
        idempotency = definition["idempotency"]
        if not isinstance(idempotency, dict):
            raise AssertionError(f"{operation_id} lacks idempotency")
        preconditions = idempotency["precondition_probe_refs"]
        if not isinstance(preconditions, list):
            raise AssertionError(f"{operation_id} preconditions are invalid")
        if preconditions:
            operation_ids.append(operation_id)
    return tuple(operation_ids)


def _assert_operation_route_reprobe(
    bundle: ContractBundle,
    root: Path,
    operation_id: str,
) -> str:
    operation = _operation(bundle, operation_id)
    adapter = _RouteSplitAdapter(operation)
    outcome = _run_pending_case(
        bundle,
        root / f"route-{operation_id}",
        operation,
        adapter,
    )
    parent_probes = _parent_pre_apply_probes(adapter, operation)
    _require(
        outcome.terminal_result is None
        and parent_probes
        and all(
            _is_safe_parent_reprobe(operation, runner, request)
            for runner, request in parent_probes
        ),
        f"{operation_id} drift inventory re-probes its own route as a dry run",
    )
    _require(
        all(
            request.request_id not in adapter.mutated_request_ids
            for _runner, request in parent_probes
        ),
        f"{operation_id} parent pre-apply probe is side-effect-free in its runner fixture",
    )
    return operation.runner


def _post_apply_block_regression(bundle: ContractBundle, root: Path) -> None:
    """A legacy bad postcondition must not retain a stale terminal result kind."""

    repaired_operation = _operation(bundle, "install_postgresql")
    legacy_operation = replace(
        repaired_operation,
        postcondition_probe_ids=("postgres_binary_version_valid", "pgvector_ready"),
    )
    legacy_adapter = _RouteSplitAdapter(
        legacy_operation,
        error_kind="postgres_probe_not_verified",
        blocked_postcondition_probe_id="pgvector_ready",
    )
    blocked = _run_pending_case(
        bundle,
        root / "post-apply-block",
        legacy_operation,
        legacy_adapter,
        result_kind="probe_drift",
    )
    _require(
        blocked.terminal_result is not None
        and blocked.terminal_result.error_kind == "postgres_probe_not_verified",
        "blocked declared postcondition retains its probe error kind",
    )
    _require(
        any(
            attempt["phase"] == "apply"
            and attempt["checkpoint_status"] == CheckpointStatus.APPLIED.value
            for attempt in blocked.transaction.operation_attempts
        ),
        "post-apply block retains the successful apply attempt",
    )
    _require(
        blocked.transaction.result_kind is None,
        "post-apply block clears stale probe_drift result_kind",
    )

    resumed_adapter = _RouteSplitAdapter(
        repaired_operation,
        preconditions_verified=True,
    )
    resumed = _run_existing_pending_case(
        bundle,
        root / "post-apply-resume",
        repaired_operation,
        blocked.transaction,
        resumed_adapter,
    )
    _require(
        resumed.terminal_result is None
        and not resumed_adapter.mutated_request_ids
        and len(legacy_adapter.mutated_request_ids) == 1,
        "a repaired contract resumes a legacy post-apply block without re-applying",
    )
