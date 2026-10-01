"""Closed request/result types for the target-local setup adapter."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type PublicEvidenceValue = JsonScalar | list[str]

_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")
_OPERATION_REF = re.compile(r"^[a-z][a-z0-9_]*::[a-z][a-z0-9_.]*$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|credential)",
    re.IGNORECASE,
)
#: ``result()`` fits a ``repair`` into this many characters, cutting the middle behind a visible marker so the remedy a text ends with survives.
#: The Manager accepts 2048 (``adapter_protocol``); this seed-side cut is what an operator reads (iss_67d2597e, iss_49f37c32).
REPAIR_LIMIT = 512
# Every rule the Manager's ``adapter_validation.public_string`` enforces on an envelope text, mirrored here because the seed never imports the Manager,
# and what holds it: ``public_text`` below, applied by ``result``, ``evidence`` and ``planned_action`` and by every producer that builds an envelope
# field itself (iss_67472e3f).  A text that breaks one makes the Manager refuse the WHOLE envelope.
#  1. a non-empty ``str`` of at most the field's maximum characters: an empty text becomes ``[empty]``, a longer one is cut behind a visible marker;
#  2. no secret-shaped text (the two ``_SECRET_SHAPED`` patterns: key=value first, absorbing any run of ``bearer`` words (a header that carries its prefix twice is real), so both ``Authorization: Bearer <token>`` and ``Bearer token=<secret>`` lose their secret; two plain patterns in either order leave one of them raw), replaced by ``[REDACTED]``;
#  3. at most 8192 bytes once redacted: a text with a non-ASCII character is held to 2048 characters, which cannot exceed 8192 bytes at 4 bytes each;
#  4. no ``/Cellar/solet/`` (a Homebrew keg path), replaced by ``[keg path]`` so the rest of the text stays readable.
_SECRET_SHAPED = (
    re.compile(r"(?i)(password|secret|token|authorization|oauth[_ -]?code|private[_ -]?key)\s*[:=]\s*(?:bearer\s+)*\S+"),
    re.compile(r"(?i)bearer(?:\s+bearer)*\s+[A-Za-z0-9._~+/-]+"),
)
_FORMULA_MARKER = "/Cellar/solet/"
_REDACTED = "[REDACTED]"
_KEG_PATH = "[keg path]"
_EMPTY = "[empty]"
_WITHHELD = "[withheld]"
_STABLE_PASSES = 8
_WIDE_TEXT_LIMIT = 2048
_TAIL_SHARE = 3  # of every 5 kept characters: the remedy is written last
CREATE_FLOW_ID = "macos.repository_setup"
EXISTING_INSTALL_FLOW_ID = "existing-install"
EXISTING_INSTALL_REF_PREFIX = "existing::"
#: Closed two-member flow set; the value says whether the flow's callables
#: carry the ``existing::`` vocabulary.  Never an open string.
FLOW_OPERATION_PAIRING: dict[str, bool] = {
    CREATE_FLOW_ID: False,
    EXISTING_INSTALL_FLOW_ID: True,
}
_REQUEST_KEYS = {
    "protocol_version",
    "kind",
    "request_id",
    "operation_id",
    "operation_ref",
    "phase",
    "probe_purpose",
    "attempt",
    "name",
    "target",
    "flow_id",
    "flow_source_revision",
    "answers_fingerprint",
    "approval_fingerprint",
    "dry_run",
    "timeout_seconds",
    "public_inputs",
}
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


class AdapterInputError(ValueError):
    """The adapter input violates the closed v1 request contract."""


@dataclass(frozen=True, slots=True)
class AdapterRequest:
    """Validated operation request received from the setup manager."""

    request_id: str
    operation_id: str
    operation_ref: str
    phase: str
    probe_purpose: str | None
    attempt: int
    name: str
    target: Path
    flow_source_revision: str
    answers_fingerprint: str
    approval_fingerprint: str | None
    dry_run: bool
    timeout_seconds: int
    public_inputs: JsonObject
    # Last, with the create default, so every existing positional construction
    # keeps its meaning; the existing-install flow sets it explicitly.
    flow_id: str = CREATE_FLOW_ID

    @classmethod
    def from_json(cls, raw_text: str) -> AdapterRequest:
        try:
            raw: object = json.loads(raw_text, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise AdapterInputError("stdin must contain exactly one JSON object") from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise AdapterInputError("request must be a JSON object")
        return cls.from_dict(cast(dict[str, object], raw))

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> AdapterRequest:
        if set(raw) != _REQUEST_KEYS:
            raise AdapterInputError("request does not match the closed v1 field set")
        cls._validate_identity(raw)
        cls._validate_phase(raw)
        public_inputs = raw["public_inputs"]
        if not isinstance(public_inputs, dict) or not all(
            isinstance(key, str) for key in public_inputs
        ):
            raise AdapterInputError("public_inputs must be an object")
        _validate_public_inputs(cast(dict[str, object], public_inputs))
        return cls(
            request_id=cast(str, raw["request_id"]),
            operation_id=cast(str, raw["operation_id"]),
            operation_ref=cast(str, raw["operation_ref"]),
            phase=cast(str, raw["phase"]),
            probe_purpose=cast(str | None, raw["probe_purpose"]),
            attempt=cast(int, raw["attempt"]),
            name=cast(str, raw["name"]),
            target=Path(cast(str, raw["target"])),
            flow_source_revision=cast(str, raw["flow_source_revision"]),
            answers_fingerprint=cast(str, raw["answers_fingerprint"]),
            approval_fingerprint=cast(str | None, raw["approval_fingerprint"]),
            dry_run=cast(bool, raw["dry_run"]),
            timeout_seconds=cast(int, raw["timeout_seconds"]),
            public_inputs=_as_json_object(public_inputs),
            flow_id=cast(str, raw["flow_id"]),
        )

    @staticmethod
    def _validate_identity(raw: dict[str, object]) -> None:
        _validate_request_header(raw)
        _validate_request_identifiers(raw)
        _validate_request_provenance(raw)
        _validate_request_fingerprints(raw)
        _validate_request_bounds(raw)

    @staticmethod
    def _validate_phase(raw: dict[str, object]) -> None:
        _validate_request_phase(raw)

    @property
    def action_arrays_must_be_empty(self) -> bool:
        return self.phase == "apply" or self.probe_purpose in _EMPTY_ACTION_PURPOSES


def _validate_request_header(raw: dict[str, object]) -> None:
    if raw["protocol_version"] != 1 or raw["kind"] != "operation_request":
        raise AdapterInputError("unsupported adapter protocol or kind")
    try:
        uuid.UUID(_required_string(raw, "request_id"))
    except ValueError as exc:
        raise AdapterInputError("request_id must be a UUID") from exc


def _validate_request_identifiers(raw: dict[str, object]) -> None:
    for key, pattern in (
        ("operation_id", _IDENTIFIER),
        ("operation_ref", _OPERATION_REF),
        ("name", _NAME),
    ):
        if pattern.fullmatch(_required_string(raw, key)) is None:
            raise AdapterInputError(f"{key} is invalid")


def _validate_request_provenance(raw: dict[str, object]) -> None:
    target_text = _required_string(raw, "target")
    if "\x00" in target_text or any(ord(character) < 32 or ord(character) == 127 for character in target_text):
        raise AdapterInputError("target must not contain control characters")
    target = Path(target_text)
    if not target.is_absolute():
        raise AdapterInputError("target must be absolute")
    _validate_flow_pairing(raw)
    if _REVISION.fullmatch(_required_string(raw, "flow_source_revision")) is None:
        raise AdapterInputError("flow_source_revision is invalid")


def _validate_flow_pairing(raw: dict[str, object]) -> None:
    """Closed two-member flow set with the operation-ref cross-check (design section 3.3).

    The create flow may only address create-vocabulary callables and the
    existing-install flow may only address ``existing::`` callables.  Any
    other flow id, or either flow paired with the other's vocabulary, is an
    ``AdapterInputError`` (exit 2) before dispatch, exactly as before.  The
    Manager's ``adapter_protocol`` and the bootstrap adapter's ``protocol``
    carry the same table; a smoke proves all three agree.
    """
    flow_id = raw["flow_id"]
    if not isinstance(flow_id, str) or flow_id not in FLOW_OPERATION_PAIRING:
        raise AdapterInputError("flow_id is invalid")
    operation_ref = _required_string(raw, "operation_ref")
    if operation_ref.startswith(EXISTING_INSTALL_REF_PREFIX) != FLOW_OPERATION_PAIRING[flow_id]:
        raise AdapterInputError("operation_ref vocabulary does not match flow_id")


def _validate_request_fingerprints(raw: dict[str, object]) -> None:
    if _FINGERPRINT.fullmatch(_required_string(raw, "answers_fingerprint")) is None:
        raise AdapterInputError("answers_fingerprint is invalid")
    approval = raw["approval_fingerprint"]
    if approval is not None and (
        not isinstance(approval, str) or _FINGERPRINT.fullmatch(approval) is None
    ):
        raise AdapterInputError("approval_fingerprint is invalid")


def _validate_request_bounds(raw: dict[str, object]) -> None:
    attempt = raw["attempt"]
    timeout = raw["timeout_seconds"]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise AdapterInputError("attempt must be a positive integer")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 900:
        raise AdapterInputError("timeout_seconds is outside the closed bound")


def _validate_request_phase(raw: dict[str, object]) -> None:
    phase = raw["phase"]
    purpose = raw["probe_purpose"]
    dry_run = raw["dry_run"]
    approval = raw["approval_fingerprint"]
    if phase == "probe":
        if purpose not in _PROBE_PURPOSES or dry_run is not True or approval is not None:
            raise AdapterInputError("probe request phase fields are inconsistent")
        return
    if phase == "apply":
        if purpose is not None or dry_run is not False or approval is None:
            raise AdapterInputError("apply request requires reviewed approval")
        return
    raise AdapterInputError("phase is invalid")


def _required_string(raw: dict[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise AdapterInputError(f"{key} must be a string")
    return value


def _validate_public_inputs(public_inputs: dict[str, object]) -> None:
    for key, value in public_inputs.items():
        if _IDENTIFIER.fullmatch(key) is None:
            raise AdapterInputError(f"public input key is invalid: {key!r}")
        if _SECRET_KEY.search(key):
            raise AdapterInputError(f"secret-like public input is forbidden: {key!r}")
        if not _is_public_value(value):
            raise AdapterInputError(f"public input value is invalid: {key!r}")


def _is_public_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return False
    return len(value) == len(set(value))


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _as_json_object(value: object) -> JsonObject:
    return cast(JsonObject, value)


def neutralized(text: str) -> str:
    """``text`` with every secret-shaped run and formula-keg path replaced, stable under a second pass (what the Manager re-checks)."""
    for _ in range(_STABLE_PASSES):
        cleaned = text
        for pattern in _SECRET_SHAPED:
            cleaned = pattern.sub(_REDACTED, cleaned)
        cleaned = cleaned.replace(_FORMULA_MARKER, _KEG_PATH)
        if cleaned == text:
            return text
        text = cleaned
    return _WITHHELD


def _fitted(text: str, limit: int) -> str:
    """``text`` unchanged when it fits ``limit`` characters, else its head and its tail around a marker that says how long it was."""
    if len(text) <= limit:
        return text
    marker = f" [... {len(text)} characters, middle cut ...] "
    kept = limit - len(marker)
    if kept < 2:
        return text[:limit]
    tail = kept * _TAIL_SHARE // 5
    return text[: kept - tail] + marker + text[len(text) - tail :]


def public_text(text: str, limit: int) -> str:
    """``text`` as the Manager's ``public_string`` accepts it for a field of at most ``limit`` characters; a text that already does is returned as it is."""
    current = text or _EMPTY
    for _ in range(_STABLE_PASSES):
        cleaned = neutralized(current)
        cleaned = _fitted(cleaned, limit if cleaned.isascii() else min(limit, _WIDE_TEXT_LIMIT))
        if cleaned == current:
            return current
        current = cleaned
    return _WITHHELD


def public_value(value: PublicEvidenceValue, limit: int) -> PublicEvidenceValue:
    """An evidence ``observed``/``expected`` value with every string made public; a list keeps its order and drops the entries that became equal."""
    if isinstance(value, str):
        return public_text(value, limit)
    if isinstance(value, list):
        return list(dict.fromkeys(public_text(item, limit) for item in value))
    return value


def planned_action(
    *,
    action_id: str,
    title: str,
    mutation_kind: str,
    target: str,
    evidence_ref: str,
) -> JsonObject:
    """Create one closed public mutation record."""

    return {
        "id": action_id,
        "title": public_text(title, 256),
        "mutation_kind": mutation_kind,
        "target": public_text(target, 512),
        "requires_confirmation": True,
        "condition_or_evidence_ref": public_text(evidence_ref, 256),
    }


def evidence(
    *,
    evidence_id: str,
    kind: str,
    status: str,
    summary: str,
    observed: PublicEvidenceValue,
    expected: PublicEvidenceValue,
    source: str,
    sensitivity: str = "public",
) -> JsonObject:
    """Create bounded evidence without including command streams or secrets."""

    public_observed = public_value(observed, 4096)
    digest_source = json.dumps(public_observed, sort_keys=True, separators=(",", ":"))
    response: JsonObject = {
        "id": evidence_id,
        "kind": kind,
        "status": public_text(status, 512),
        "summary": public_text(summary, 512),
        "observed": cast(JsonValue, public_observed),
        "expected": cast(JsonValue, public_value(expected, 4096)),
        "source": public_text(source, 512),
        "digest": "sha256:" + hashlib.sha256(digest_source.encode()).hexdigest(),
        "captured_at": datetime.now(UTC).isoformat(),
        "sensitivity": sensitivity,
    }
    return response


def result(
    request: AdapterRequest,
    *,
    status: str,
    error_kind: str | None = None,
    retry_safe: bool = True,
    exit_code: int | None = 0,
    timed_out: bool = False,
    duration_ms: int = 0,
    actions: list[JsonObject] | None = None,
    candidates: list[JsonObject] | None = None,
    evidence_items: list[JsonObject] | None = None,
    reason: JsonObject | None = None,
    repair: str | None = None,
) -> JsonObject:
    """Build a closed result and enforce phase-specific purity."""

    planned = [] if actions is None else actions
    discovered = [] if candidates is None else candidates
    if request.action_arrays_must_be_empty:
        planned = []
    if request.probe_purpose not in {"decision_discovery"}:
        discovered = []
    response: JsonObject = {
        "protocol_version": 1,
        "kind": "operation_result",
        "request_id": request.request_id,
        "operation_id": request.operation_id,
        "phase": request.phase,
        "probe_purpose": request.probe_purpose,
        "checkpoint_status": status,
        "error_kind": error_kind,
        "retry_safe": retry_safe,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": max(0, duration_ms),
        "stdout": "",
        "stderr": "",
        "planned_actions": cast(JsonValue, planned),
        "discovered_candidates": cast(JsonValue, discovered),
        "evidence": cast(JsonValue, [] if evidence_items is None else evidence_items),
        "reason": cast(JsonValue, reason),
        "repair": None if repair is None else public_text(repair, REPAIR_LIMIT),
    }
    return response


def public_string(request: AdapterRequest, key: str) -> str | None:
    value = request.public_inputs.get(key)
    return value if isinstance(value, str) and value else None


__all__ = [
    "CREATE_FLOW_ID",
    "EXISTING_INSTALL_FLOW_ID",
    "EXISTING_INSTALL_REF_PREFIX",
    "FLOW_OPERATION_PAIRING",
    "AdapterInputError",
    "AdapterRequest",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "evidence",
    "neutralized",
    "planned_action",
    "public_string",
    "public_text",
    "public_value",
    "result",
]
