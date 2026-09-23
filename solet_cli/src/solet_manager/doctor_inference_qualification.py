"""Report representative inference qualification without making it a setup gate."""

from __future__ import annotations

import uuid

from .adapters import AdapterRegistry, OperationRequest, OperationResult, invoke_adapter
from .contracts import ContractBundle
from .doctor_advisory_result import advisory_verified, advisory_warn
from .inference_probe_policy import ADVISORY_ERROR_KIND
from .models import CheckpointStatus, JsonValue
from .probe_input_projection import probe_public_inputs
from .transaction import Transaction

_PROBE_ADVISORIES = (
    ("structured_action_qualification", "doctor::structured_action_qualification"),
    ("representative_inference_probe", "doctor::representative_inference_qualification"),
)
_REPRESENTATIVE_INFERENCE_TIMEOUT_SECONDS = 180


def collect_inference_qualification_advisories(
    bundle: ContractBundle,
    transaction: Transaction,
    registry: AdapterRegistry,
) -> list[JsonValue]:
    """Run non-blocking inference checks after model selection.

    A discovered served model is eligible for setup. These requests provide
    diagnostic evidence only and cannot turn setup into a blocker.
    """

    if not _has_inference_model(transaction):
        return []
    advisories: list[JsonValue] = []
    for probe_id, check_id in _PROBE_ADVISORIES:
        definition = bundle.probes.get(probe_id)
        if definition is not None:
            advisories.append(
                _collect_advisory(bundle, transaction, registry, definition, check_id, probe_id)
            )
    return advisories


def collect_inference_pre_probe_timeout_advisories(
    transaction: Transaction,
) -> list[JsonValue]:
    """Expose the latest inference-config dry-run timeout as a non-blocking warning."""

    latest = next(
        (
            attempt
            for attempt in reversed(transaction.operation_attempts)
            if attempt.get("operation_id") == "configure_lm_studio_inference"
            and attempt.get("phase") == "pre_probe"
        ),
        None,
    )
    if latest is None or not _is_inference_probe_advisory(latest):
        return []
    observed: dict[str, JsonValue] = {
        "checkpoint_status": latest.get("checkpoint_status"),
        "error_kind": latest.get("error_kind"),
        "timed_out": latest.get("timed_out"),
        "duration_ms": latest.get("duration_ms"),
    }
    repair = latest.get("repair")
    return [
        advisory_warn(
            "doctor::inference_configuration_pre_probe",
            "The served inference model was configured after an advisory pre-probe outcome.",
            {"checkpoint_status": CheckpointStatus.VERIFIED.value},
            observed,
            "setup::models.configure_lm_studio_inference",
            "inference_configuration_pre_probe_advisory",
            (
                repair
                if isinstance(repair, str) and repair
                else "Rerun solet doctor after the model is warm."
            ),
        )
    ]


def collect_inference_probe_advisories(transaction: Transaction) -> list[JsonValue]:
    """Render every journaled served-model probe outcome as a doctor warning."""

    advisories: list[JsonValue] = []
    for attempt in (*transaction.operation_attempts, *transaction.stage_probe_attempts):
        if not _is_inference_probe_advisory(attempt):
            continue
        operation_id = attempt.get("operation_id", attempt.get("probe_id"))
        if not isinstance(operation_id, str) or not operation_id:
            continue
        observed: dict[str, JsonValue] = {
            "checkpoint_status": attempt.get("checkpoint_status"),
            "error_kind": attempt.get("error_kind"),
            "timed_out": attempt.get("timed_out"),
            "duration_ms": attempt.get("duration_ms"),
        }
        repair = attempt.get("repair")
        advisories.append(
            advisory_warn(
                f"doctor::inference_probe.{operation_id}",
                "A served inference model had an advisory probe outcome.",
                {"checkpoint_status": CheckpointStatus.VERIFIED.value},
                observed,
                operation_id,
                "inference_probe_advisory",
                (
                    repair
                    if isinstance(repair, str) and repair
                    else "Rerun solet doctor after the model is warm."
                ),
            )
        )
    return advisories


def _is_inference_probe_advisory(attempt: dict[str, JsonValue]) -> bool:
    return (
        attempt.get("checkpoint_status") == CheckpointStatus.VERIFIED.value
        and attempt.get("error_kind") == ADVISORY_ERROR_KIND
    )


def _collect_advisory(
    bundle: ContractBundle,
    transaction: Transaction,
    registry: AdapterRegistry,
    definition: dict[str, JsonValue],
    check_id: str,
    probe_id: str,
) -> JsonValue:
    result = _run_probe(bundle, transaction, registry, definition, probe_id)
    expected: dict[str, JsonValue] = {
        "checkpoint_status": CheckpointStatus.VERIFIED.value
    }
    observed = _observed(result)
    source = str(definition["probe_ref"])
    if result.checkpoint_status is CheckpointStatus.VERIFIED:
        advisory: JsonValue = advisory_verified(
            check_id,
            "The configured inference model passed advisory qualification.",
            expected,
            observed,
            source,
        )
        return advisory
    advisory = advisory_warn(
        check_id,
        (
            "The configured inference model is installed and served, but advisory "
            "qualification did not pass."
        ),
        expected,
        observed,
        source,
        "representative_inference_qualification_failed",
        _repair(result),
    )
    return advisory


def _has_inference_model(transaction: Transaction) -> bool:
    decisions = transaction.answers.get("decisions")
    return isinstance(decisions, dict) and isinstance(decisions.get("inference_model"), str)


def _run_probe(
    bundle: ContractBundle,
    transaction: Transaction,
    registry: AdapterRegistry,
    definition: dict[str, JsonValue],
    probe_id: str,
) -> OperationResult:
    request = OperationRequest(
        request_id=str(uuid.uuid4()),
        operation_id=probe_id,
        operation_ref=str(definition["probe_ref"]),
        phase="probe",
        probe_purpose="completion",
        attempt=1,
        name=transaction.name,
        target=transaction.target,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        answers_fingerprint=transaction.answers_fingerprint,
        approval_fingerprint=None,
        dry_run=True,
        # The hydration adapter itself is a subprocess.  Its deadline must be
        # at least the shared inference handler's reasoning-model HTTP budget,
        # otherwise the manager kills that handler before it can finish.
        timeout_seconds=_REPRESENTATIVE_INFERENCE_TIMEOUT_SECONDS,
        public_inputs=probe_public_inputs(transaction, str(definition["probe_ref"])),
    )
    return invoke_adapter(registry, runner=str(definition["runner"]), request=request)


def _observed(result: OperationResult) -> dict[str, JsonValue]:
    return {
        "checkpoint_status": result.checkpoint_status.value,
        "error_kind": result.error_kind,
        "timed_out": result.timed_out,
        "summary": _summary(result),
    }


def _summary(result: OperationResult) -> str:
    for evidence in result.evidence:
        summary = evidence.get("summary")
        if isinstance(summary, str) and summary:
            return summary
    return (
        "No adapter evidence was returned; checkpoint status is "
        f"{result.checkpoint_status.value}."
    )


def _repair(result: OperationResult) -> str:
    return result.repair or "Inspect the representative inference result and rerun solet doctor."
