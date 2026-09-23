"""Immutable request/result types for the closed adapter wire protocol."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self, cast

from .adapter_validation import (
    FINGERPRINT_PATTERN,
    NAME_PATTERN,
    OPERATION_REF_PATTERN,
    boolean,
    bounded_integer,
    identifier,
    nonnegative_integer,
    optional_identifier,
    optional_int,
    optional_public_string,
    public_string,
    public_value,
    stream,
    validate_evidence,
    validate_reason,
)
from .errors import AdapterProtocolError
from .models import CheckpointStatus, JsonValue

PROTOCOL_VERSION = 1
CREATE_FLOW_ID = "macos.repository_setup"
EXISTING_INSTALL_FLOW_ID = "existing-install"
EXISTING_INSTALL_REF_PREFIX = "existing::"
# Closed two-member flow set (design section 3.3, review D2).  The value says
# whether the flow's callables carry the ``existing::`` vocabulary; the seed's
# ``setup_adapter_contract`` and the bootstrap adapter's ``protocol`` hold the
# same table and a smoke proves all three agree.
FLOW_OPERATION_PAIRING: dict[str, bool] = {
    CREATE_FLOW_ID: False,
    EXISTING_INSTALL_FLOW_ID: True,
}
_RESULT_KEYS = {
    "protocol_version",
    "kind",
    "request_id",
    "operation_id",
    "phase",
    "probe_purpose",
    "checkpoint_status",
    "error_kind",
    "retry_safe",
    "exit_code",
    "timed_out",
    "duration_ms",
    "stdout",
    "stderr",
    "planned_actions",
    "discovered_candidates",
    "evidence",
    "reason",
    "repair",
}
_LEGACY_RESULT_KEYS = _RESULT_KEYS - {"reason"}
_PROBE_PURPOSES = {
    "preview",
    "pre_apply",
    "post_apply",
    "completion",
    "decision_discovery",
    "decision_qualification",
    "stage_entry",
    "stage_exit",
}
_EMPTY_ACTION_PURPOSES = {
    "post_apply",
    "completion",
    "decision_discovery",
    "decision_qualification",
    "stage_entry",
    "stage_exit",
}


@dataclass(frozen=True)
class OperationRequest:
    request_id: str
    operation_id: str
    operation_ref: str
    phase: str
    probe_purpose: str | None
    attempt: int
    name: str
    target: str
    flow_id: str
    flow_source_revision: str
    answers_fingerprint: str
    approval_fingerprint: str | None
    dry_run: bool
    timeout_seconds: int
    public_inputs: dict[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        self.validate()
        return {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "operation_request",
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "operation_ref": self.operation_ref,
            "phase": self.phase,
            "probe_purpose": self.probe_purpose,
            "attempt": self.attempt,
            "name": self.name,
            "target": self.target,
            "flow_id": self.flow_id,
            "flow_source_revision": self.flow_source_revision,
            "answers_fingerprint": self.answers_fingerprint,
            "approval_fingerprint": self.approval_fingerprint,
            "dry_run": self.dry_run,
            "timeout_seconds": self.timeout_seconds,
            "public_inputs": self.public_inputs,
        }

    def validate(self) -> None:
        _validate_request_identity(self)
        _validate_request_numbers(self)
        _validate_request_inputs(self)
        _validate_request_phase(self)


def _validate_request_identity(request: OperationRequest) -> None:
    try:
        uuid.UUID(request.request_id)
    except ValueError as exc:
        raise AdapterProtocolError("request_id must be a UUID") from exc
    identifier(request.operation_id, "operation_id")
    if OPERATION_REF_PATTERN.fullmatch(request.operation_ref) is None:
        raise AdapterProtocolError("operation_ref does not match the closed grammar")
    if NAME_PATTERN.fullmatch(request.name) is None:
        raise AdapterProtocolError("name does not match the closed grammar")
    if not Path(request.target).is_absolute():
        raise AdapterProtocolError("target must be absolute")


def _validate_request_numbers(request: OperationRequest) -> None:
    _validate_flow_pairing(request.flow_id, request.operation_ref)
    valid_revision = re.fullmatch(r"[0-9a-f]{40}", request.flow_source_revision)
    if valid_revision is None:
        raise AdapterProtocolError("flow identity is invalid")
    if FINGERPRINT_PATTERN.fullmatch(request.answers_fingerprint) is None:
        raise AdapterProtocolError("answers_fingerprint is invalid")
    if request.approval_fingerprint is not None:
        if FINGERPRINT_PATTERN.fullmatch(request.approval_fingerprint) is None:
            raise AdapterProtocolError("approval_fingerprint is invalid")
    if isinstance(request.attempt, bool) or request.attempt < 1:
        raise AdapterProtocolError("attempt must be a positive integer")
    if not 1 <= request.timeout_seconds <= 900:
        raise AdapterProtocolError("timeout_seconds is outside the closed bound")


def _validate_flow_pairing(flow_id: str, operation_ref: str) -> None:
    """Enforce the closed two-member flow set with its operation-ref cross-check.

    The create flow may only address create-vocabulary callables and the
    existing-install flow may only address ``existing::`` callables.  This is
    a closed pairing table, never an open string: a third flow id, or either
    flow paired with the other's vocabulary, is a protocol refusal.
    """
    if flow_id not in FLOW_OPERATION_PAIRING:
        raise AdapterProtocolError("flow identity is invalid")
    if (operation_ref.startswith(EXISTING_INSTALL_REF_PREFIX)) != (flow_id == EXISTING_INSTALL_FLOW_ID):
        raise AdapterProtocolError("operation_ref vocabulary does not match flow identity")


def _validate_request_inputs(request: OperationRequest) -> None:
    for key, value in request.public_inputs.items():
        identifier(key, "public input key")
        public_value(value, f"public input {key}")


def _validate_request_phase(request: OperationRequest) -> None:
    if request.phase not in {"probe", "apply"}:
        raise AdapterProtocolError(f"unsupported request phase: {request.phase!r}")
    if request.phase == "apply":
        _validate_apply_request(request)
        return
    if request.probe_purpose not in _PROBE_PURPOSES:
        raise AdapterProtocolError("probe request has an invalid probe_purpose")
    if not request.dry_run or request.approval_fingerprint is not None:
        raise AdapterProtocolError("probe request requires dry_run true and null approval")


def _validate_apply_request(request: OperationRequest) -> None:
    if request.probe_purpose is not None:
        raise AdapterProtocolError("apply request probe_purpose must be null")
    if request.dry_run or request.approval_fingerprint is None:
        raise AdapterProtocolError("apply request requires approval and dry_run false")


@dataclass(frozen=True)
class PlannedAction:
    """One host-conditional public mutation reported by an adapter probe."""

    id: str
    title: str
    mutation_kind: str
    target: str
    requires_confirmation: bool
    condition_or_evidence_ref: str

    @classmethod
    def from_dict(cls, raw: dict[str, JsonValue]) -> Self:
        required = {
            "id",
            "title",
            "mutation_kind",
            "target",
            "requires_confirmation",
            "condition_or_evidence_ref",
        }
        if set(raw) != required:
            raise AdapterProtocolError(
                "planned action does not match the closed v1 shape"
            )
        try:
            return cls(
                id=identifier(raw["id"], "planned action id"),
                title=public_string(
                    raw["title"], "planned action title", maximum=256
                ),
                mutation_kind=identifier(
                    raw["mutation_kind"], "planned action mutation_kind"
                ),
                target=public_string(
                    raw["target"], "planned action target", maximum=512
                ),
                requires_confirmation=boolean(
                    raw["requires_confirmation"], "requires_confirmation"
                ),
                condition_or_evidence_ref=public_string(
                    raw["condition_or_evidence_ref"],
                    "planned action condition_or_evidence_ref",
                    maximum=256,
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterProtocolError(
                f"planned action fields are invalid: {exc}"
            ) from exc

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "title": self.title,
            "mutation_kind": self.mutation_kind,
            "target": self.target,
            "requires_confirmation": self.requires_confirmation,
            "condition_or_evidence_ref": self.condition_or_evidence_ref,
        }


@dataclass(frozen=True)
class DiscoveredCandidate:
    """One public candidate produced by a contract-declared discovery probe."""

    decision_id: str
    value: str
    label: str
    recommendation_rank: int
    metadata: dict[str, JsonValue]

    @classmethod
    def from_dict(cls, raw: dict[str, JsonValue]) -> Self:
        required = {
            "decision_id",
            "value",
            "label",
            "recommendation_rank",
            "metadata",
        }
        if set(raw) != required:
            raise AdapterProtocolError(
                "discovered candidate does not match the closed v1 shape"
            )
        metadata = raw.get("metadata")
        if not isinstance(metadata, dict):
            raise AdapterProtocolError(
                "discovered candidate metadata must be an object"
            )
        return cast(Self, _candidate_from_fields(cls, raw, metadata))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "decision_id": self.decision_id,
            "value": self.value,
            "label": self.label,
            "recommendation_rank": self.recommendation_rank,
            "metadata": self.metadata,
        }


def _candidate_from_fields(
    candidate_type: type[DiscoveredCandidate],
    raw: dict[str, JsonValue],
    metadata: dict[str, JsonValue],
) -> DiscoveredCandidate:
    try:
        return candidate_type(
            decision_id=identifier(raw["decision_id"], "candidate decision_id"),
            value=public_string(raw["value"], "candidate value", maximum=256),
            label=public_string(raw["label"], "candidate label", maximum=256),
            recommendation_rank=nonnegative_integer(
                raw["recommendation_rank"], "candidate recommendation_rank"
            ),
            metadata={
                identifier(key, "candidate metadata key"): public_value(
                    value, f"candidate metadata {key}"
                )
                for key, value in metadata.items()
            },
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AdapterProtocolError(
            f"discovered candidate fields are invalid: {exc}"
        ) from exc


@dataclass(frozen=True)
class OperationResult:
    request_id: str
    operation_id: str
    phase: str
    probe_purpose: str | None
    checkpoint_status: CheckpointStatus
    error_kind: str | None
    retry_safe: bool
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str
    planned_actions: tuple[PlannedAction, ...]
    discovered_candidates: tuple[DiscoveredCandidate, ...]
    evidence: tuple[dict[str, JsonValue], ...]
    repair: str | None
    reason: dict[str, JsonValue] | None = None

    @classmethod
    def blocked(
        cls,
        request: OperationRequest,
        *,
        error_kind: str,
        repair: str,
    ) -> Self:
        return cls(
            request.request_id,
            request.operation_id,
            request.phase,
            request.probe_purpose,
            CheckpointStatus.BLOCKED,
            error_kind,
            False,
            None,
            False,
            0,
            "",
            "",
            (),
            (),
            (),
            repair,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, JsonValue], request: OperationRequest) -> Self:
        return cast(Self, _parse_operation_result(cls, raw, request))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "operation_result",
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "phase": self.phase,
            "probe_purpose": self.probe_purpose,
            "checkpoint_status": self.checkpoint_status.value,
            "error_kind": self.error_kind,
            "retry_safe": self.retry_safe,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "planned_actions": [item.to_dict() for item in self.planned_actions],
            "discovered_candidates": [
                item.to_dict() for item in self.discovered_candidates
            ],
            "evidence": list(self.evidence),
            "reason": self.reason,
            "repair": self.repair,
        }


def _parse_operation_result(
    result_type: type[OperationResult],
    raw: dict[str, JsonValue],
    request: OperationRequest,
) -> OperationResult:
    normalized = _normalize_legacy_result_envelope(raw)
    _validate_result_envelope(normalized, request)
    action_objects = _object_array(normalized.get("planned_actions"), "planned_actions")
    candidate_objects = _object_array(
        normalized.get("discovered_candidates"), "discovered_candidates"
    )
    evidence_objects = _object_array(normalized.get("evidence"), "evidence")
    result = _result_from_fields(
        result_type,
        normalized,
        request,
        action_objects,
        candidate_objects,
        evidence_objects,
    )
    _validate_result_status(result, request)
    _validate_result_collections(result, request)
    return result


def _normalize_legacy_result_envelope(
    raw: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Add only the absent reason field from the known pre-r13 result shape."""

    if set(raw) == _LEGACY_RESULT_KEYS:
        return {**raw, "reason": None}
    return raw


def _validate_result_envelope(
    raw: dict[str, JsonValue], request: OperationRequest
) -> None:
    if set(raw) != _RESULT_KEYS:
        raise AdapterProtocolError("adapter result does not match the closed v1 envelope")
    if raw.get("protocol_version") != PROTOCOL_VERSION:
        raise AdapterProtocolError("adapter result does not match the closed v1 envelope")
    if raw.get("kind") != "operation_result":
        raise AdapterProtocolError("adapter result does not match the closed v1 envelope")
    if raw.get("request_id") != request.request_id:
        raise AdapterProtocolError("adapter result did not echo request identity")
    if raw.get("operation_id") != request.operation_id:
        raise AdapterProtocolError("adapter result did not echo request identity")
    if raw.get("phase") != request.phase:
        raise AdapterProtocolError("adapter result phase differs from request")
    if raw.get("probe_purpose") != request.probe_purpose:
        raise AdapterProtocolError("adapter result probe_purpose differs from request")


def _object_array(value: JsonValue | None, label: str) -> list[dict[str, JsonValue]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AdapterProtocolError(f"adapter {label} must be an object array")
    return cast(list[dict[str, JsonValue]], value)


def _result_from_fields(
    result_type: type[OperationResult],
    raw: dict[str, JsonValue],
    request: OperationRequest,
    action_objects: list[dict[str, JsonValue]],
    candidate_objects: list[dict[str, JsonValue]],
    evidence_objects: list[dict[str, JsonValue]],
) -> OperationResult:
    try:
        return result_type(
            request.request_id,
            request.operation_id,
            request.phase,
            request.probe_purpose,
            CheckpointStatus(str(raw["checkpoint_status"])),
            optional_identifier(raw["error_kind"], "error_kind"),
            boolean(raw["retry_safe"], "retry_safe"),
            optional_int(raw["exit_code"], "exit_code", minimum=0, maximum=255),
            boolean(raw["timed_out"], "timed_out"),
            bounded_integer(raw["duration_ms"], "duration_ms", minimum=0),
            stream(raw["stdout"], "stdout"),
            stream(raw["stderr"], "stderr"),
            _planned_actions(action_objects),
            _discovered_candidates(candidate_objects),
            tuple(validate_evidence(item) for item in evidence_objects),
            optional_public_string(raw["repair"], "repair", maximum=2048),
            validate_reason(raw["reason"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AdapterProtocolError(f"adapter result fields are invalid: {exc}") from exc


def _planned_actions(
    objects: list[dict[str, JsonValue]],
) -> tuple[PlannedAction, ...]:
    return tuple(sorted((PlannedAction.from_dict(item) for item in objects), key=lambda item: item.id))


def _discovered_candidates(
    objects: list[dict[str, JsonValue]],
) -> tuple[DiscoveredCandidate, ...]:
    return tuple(
        sorted(
            (DiscoveredCandidate.from_dict(item) for item in objects),
            key=lambda item: (
                item.decision_id,
                item.recommendation_rank,
                item.value,
            ),
        )
    )


def _validate_result_status(
    result: OperationResult,
    request: OperationRequest,
) -> None:
    error_statuses = {
        CheckpointStatus.BLOCKED,
        CheckpointStatus.FAILED,
        CheckpointStatus.AWAITING_USER,
    }
    if result.error_kind is None and result.checkpoint_status in error_statuses:
        raise AdapterProtocolError("non-success adapter status requires error_kind")
    request.validate()
    if request.phase == "apply" and result.checkpoint_status is CheckpointStatus.VERIFIED:
        raise AdapterProtocolError(
            "apply results cannot own verification; run a post-apply probe"
        )
    mutation_statuses = {
        CheckpointStatus.APPLYING,
        CheckpointStatus.APPLIED,
        CheckpointStatus.CONSENTED,
    }
    if request.phase == "probe" and result.checkpoint_status in mutation_statuses:
        raise AdapterProtocolError(
            "probe results cannot report mutation-phase checkpoint statuses"
        )


def _validate_result_collections(
    result: OperationResult,
    request: OperationRequest,
) -> None:
    if request.phase == "apply" or request.probe_purpose in _EMPTY_ACTION_PURPOSES:
        if result.planned_actions:
            raise AdapterProtocolError(
                "apply, post-apply, and completion results must have empty planned_actions"
            )
    if request.probe_purpose != "decision_discovery":
        if result.discovered_candidates:
            raise AdapterProtocolError(
                "discovered_candidates must be empty outside a decision_discovery probe"
            )
    _require_unique_action_ids(result.planned_actions)
    _require_unique_candidate_keys(result.discovered_candidates)


def _require_unique_action_ids(actions: tuple[PlannedAction, ...]) -> None:
    action_ids = tuple(action.id for action in actions)
    if len(action_ids) != len(set(action_ids)):
        raise AdapterProtocolError("adapter planned_actions contains duplicate ids")


def _require_unique_candidate_keys(
    candidates: tuple[DiscoveredCandidate, ...],
) -> None:
    values = tuple((item.decision_id, item.value) for item in candidates)
    if len(values) != len(set(values)):
        raise AdapterProtocolError(
            "adapter discovered_candidates contains duplicate values"
        )
    ranks = tuple((item.decision_id, item.recommendation_rank) for item in candidates)
    if len(ranks) != len(set(ranks)):
        raise AdapterProtocolError(
            "adapter discovered_candidates contains duplicate recommendation ranks"
        )
