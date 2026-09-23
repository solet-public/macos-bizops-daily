"""One policy for non-blocking probes of already served inference models."""

from __future__ import annotations

from dataclasses import replace

from .adapters import OperationRequest, OperationResult
from .models import CheckpointStatus, JsonValue

_INFERENCE_PROBE_REFS = frozenset(
    {
        "setup::models.configure_lm_studio_inference",
        "setup::models.qualify_structured_actions",
        "setup::models.qualify_representative_inference",
        "service_interface::inference_service.qualify",
    }
)
ADVISORY_ERROR_KIND = "inference_probe_advisory"
IDENTITY_REFUSAL_KIND = "inference_model_identity_mismatch"


def advisory_inference_probe_result(
    answers: dict[str, JsonValue],
    request: OperationRequest,
    result: OperationResult,
) -> OperationResult:
    """Pass a served inference model while retaining probe facts as advisory.

    The selector admits only served inference candidates. Once that fact is
    established, qualification, representative, identity, and configuration
    probes supply diagnostics rather than a second setup gate. Exact identity
    refusal remains decisive.
    """

    if (
        result.checkpoint_status is CheckpointStatus.VERIFIED
        or not _is_served_inference_request(answers, request)
        or result.error_kind == IDENTITY_REFUSAL_KIND
    ):
        return result
    return replace(
        result,
        checkpoint_status=CheckpointStatus.VERIFIED,
        error_kind=ADVISORY_ERROR_KIND,
        retry_safe=True,
    )


def _is_served_inference_request(
    answers: dict[str, JsonValue],
    request: OperationRequest,
) -> bool:
    if request.operation_ref not in _INFERENCE_PROBE_REFS:
        return False
    if request.public_inputs.get("decision_id") == "inference_model":
        return True
    decisions = answers.get("decisions")
    return (
        isinstance(decisions, dict)
        and isinstance(decisions.get("inference_model"), str)
        and bool(decisions["inference_model"])
    )
