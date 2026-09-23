"""Bounded, exact-key stdlib codec for the release_observer.v1 family.

Decoding proves the wire contract, never producer authority. Consumers must also
compare the decoded identity and binding with independently admitted values.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import types
from dataclasses import dataclass, fields
from datetime import datetime
from functools import cache
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, Self, TypeAliasType, Union, cast, get_args, get_origin, get_type_hints

if TYPE_CHECKING:
    from . import release_observer_contract as m

PROTOCOL = "release_observer.v1"
MAX_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 4096
MAX_DEPTH = 48
MAX_TEXT = 4096
MAX_INTEGER = 2**63 - 1

type Digest = Annotated[str, "sha256"]
type Identifier = Annotated[str, "identifier"]
type Timestamp = Annotated[str, "timestamp"]
type AbsolutePath = Annotated[str, "absolute"]
type Positive = Annotated[int, "positive"]
type Natural = Annotated[int, "natural"]
type FailureCode = Literal[
    "malformed", "digest_mismatch", "identity_mismatch", "binding_mismatch",
    "request_conflict", "missing_observation", "sequence_gap", "sequence_reordered",
    "duplicate_different", "wrong_predecessor", "stale_sample", "future_sample",
    "expired", "reversed_time", "generation_changed", "unavailable",
    "not_submitted", "execution_failed", "retention_partial", "artifact_mismatch",
    "unsupported_capability", "lock_timeout", "absent_target", "in_progress",
]


class ObserverContractError(ValueError):
    """Closed validation failure; evidence is the offending input at the caller."""

    def __init__(self, code: FailureCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


def require(condition: bool, detail: str, code: FailureCode = "malformed") -> None:
    if not condition:
        raise ObserverContractError(code, detail)


def timestamp(value: str) -> datetime:
    require(type(value) is str, "timestamp text required")
    require(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value) is not None, "timestamp needs explicit offset")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ObserverContractError("malformed", "invalid timestamp") from error
    require(result.utcoffset() is not None, "timezone required")
    return result


def absolute_path(value: str) -> None:
    require(type(value) is str, "path text required")
    require(value.startswith("/") and not value.startswith("//"), "absolute path required")
    require(value == "/" or all(part not in ("", ".", "..") for part in value[1:].split("/")), "normalized path required")
    require(not any(ch in value for ch in ("\\", "%", "?", "#")), "ambiguous path")


def _constraint(value: object, rule: object) -> None:
    checks = {
        "sha256": lambda: require(re.fullmatch(r"[0-9a-f]{64}", cast(str, value)) is not None, "lowercase SHA-256 required"),
        "identifier": lambda: require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@+\-]{0,255}", cast(str, value)) is not None, "bounded identifier required"),
        "timestamp": lambda: timestamp(cast(str, value)),
        "absolute": lambda: absolute_path(cast(str, value)),
        "positive": lambda: require(0 < cast(int, value) <= MAX_INTEGER, "positive bounded integer required"),
        "natural": lambda: require(0 <= cast(int, value) <= MAX_INTEGER, "nonnegative bounded integer required"),
    }
    require(rule in checks, "unknown scalar constraint")
    checks[cast(str, rule)]()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    require(len(pairs) <= MAX_ITEMS, "object population exceeds limit")
    result: dict[str, object] = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _nonfinite(value: str) -> object:
    raise ObserverContractError("malformed", f"non-finite number: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    require(math.isfinite(number), "non-finite number")
    return number


def _bounded(value: object, depth: int = 0) -> None:
    require(depth <= MAX_DEPTH, "nesting exceeds limit")
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        require(len(mapping) <= MAX_ITEMS, "object exceeds limit")
        for key, item in mapping.items():
            require(type(key) is str, "JSON object key must be text")
            _bounded(key, depth + 1)
            _bounded(item, depth + 1)
    elif isinstance(value, (tuple, list)):
        items = cast(tuple[object, ...], value)
        require(len(items) <= MAX_ITEMS, "array exceeds limit")
        for item in items:
            _bounded(item, depth + 1)
    else:
        _bounded_scalar(value)


def _bounded_scalar(value: object) -> None:
    require(type(value) in (str, int, bool, float, type(None)), "unsupported JSON value")
    if isinstance(value, str):
        require(len(value) <= MAX_TEXT, "text exceeds limit")
        require(not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value), "invalid text character")
    elif type(value) is int:
        require(abs(value) <= MAX_INTEGER, "integer exceeds limit")
    elif isinstance(value, float):
        require(math.isfinite(value), "non-finite number")


def canonical_json(value: object) -> bytes:
    """Canonical UTF-8 bytes; bounds also apply to locally constructed values."""
    _bounded(value)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    require(len(encoded) <= MAX_BYTES, "payload exceeds limit")
    return encoded


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def decode_json(encoded: bytes) -> object:
    require(type(encoded) is bytes and len(encoded) <= MAX_BYTES, "bounded UTF-8 bytes required")
    try:
        raw: object = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_nonfinite, parse_float=_finite_float)
        _bounded(raw)
        return raw
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, ObserverContractError):
            raise
        raise ObserverContractError("malformed", "invalid bounded UTF-8 JSON") from error


@cache
def _hints(cls: type[Contract]) -> dict[str, object]:
    return get_type_hints(cls, include_extras=True)


def _union_value(choices: tuple[object, ...], value: object) -> object:
    for choice in choices:
        try:
            return _decode(choice, value)
        except ObserverContractError:
            continue
    raise ObserverContractError("malformed", "value matches no declared branch")


def _decode(annotation: object, value: object) -> object:
    if isinstance(annotation, TypeAliasType):
        return _decode(annotation.__value__, value)
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        decoded = _decode(args[0], value)
        for rule in args[1:]:
            _constraint(decoded, rule)
        return decoded
    if origin in (Union, types.UnionType):
        return _union_value(args, value)
    if origin is Literal:
        require(any(type(value) is type(choice) and value == choice for choice in args), "invalid literal")
        return value
    if origin is tuple:
        return _array(args[0], value)
    return _scalar(annotation, value)


def _array(annotation: object, value: object) -> tuple[object, ...]:
    require(type(value) in (list, tuple), "array required")
    items = cast(tuple[object, ...], value)
    require(len(items) <= MAX_ITEMS, "array exceeds limit")
    return tuple(_decode(annotation, item) for item in items)


def _scalar(annotation: object, value: object) -> object:
    if isinstance(annotation, type) and issubclass(annotation, Contract):
        return value if type(value) is annotation else annotation.from_dict(value)
    require(annotation in (str, int, bool, type(None)), "unsupported field type")
    require(type(value) is annotation, "wrong scalar type (bool is not integer)")
    _bounded(value)
    if annotation is str:
        require(bool(cast(str, value).strip()), "empty text")
    return value


def _wire(value: object) -> object:
    if isinstance(value, Contract):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_wire(item) for item in cast(tuple[object, ...], value)]
    return value


@dataclass(frozen=True, slots=True)
class Contract:
    """Immutable closed records: constructors and decoding share validation.

    Each record hashes its complete wire object excluding only its own digest
    field. Nested records keep their digests in the enclosing hash.
    """

    kind: ClassVar[str]
    schema: ClassVar[str] = PROTOCOL
    digest_field: ClassVar[str] = "digest"

    def __post_init__(self) -> None:
        hints = _hints(type(self))
        for field in fields(self):
            value = getattr(self, field.name)
            decoded = _decode(hints[field.name], value)
            require(type(decoded) is type(value) and decoded == value, f"noncanonical field {field.name}")
        self.validate()

    def validate(self) -> None:
        """Additional branch invariants."""

    @classmethod
    def from_dict(cls, raw: object) -> Self:
        require(type(raw) is dict, f"{cls.kind} object required")
        data = cast(dict[str, object], raw)
        _bounded(data)
        names = {field.name for field in fields(cls)}
        require(set(data) == names | {"schema_version", "kind", cls.digest_field}, f"{cls.kind} missing/extra keys")
        require(data["schema_version"] == cls.schema and data["kind"] == cls.kind, "wrong protocol/kind")
        _decode(Digest, data[cls.digest_field])
        actual = sha256_json({key: value for key, value in data.items() if key != cls.digest_field})
        require(data[cls.digest_field] == actual, "record digest differs", "digest_mismatch")
        hints = _hints(cls)
        return cls(**{name: _decode(hints[name], data[name]) for name in names})

    def to_dict(self) -> dict[str, object]:
        content = {"schema_version": self.schema, "kind": self.kind, **{field.name: _wire(getattr(self, field.name)) for field in fields(self)}}
        return {**content, self.digest_field: sha256_json(content)}

    @property
    def digest(self) -> str:
        return cast(str, self.to_dict()[self.digest_field])


def unique(values: tuple[object, ...]) -> None:
    encoded = tuple(canonical_json(_wire(value)) for value in values)
    require(len(set(encoded)) == len(encoded), "duplicate population")


def encode_message(message: Contract) -> bytes:
    """Encode only declared top-level messages, with a constructor recheck."""
    from .release_observer_contract import MESSAGE_TYPES

    require(type(message) in MESSAGE_TYPES, "not a top-level message")
    raw = message.to_dict()
    type(message).from_dict(raw)
    return canonical_json(raw)


def decode_message(encoded: bytes) -> Contract:
    """Decode an exact member of the closed request/result union."""
    from .release_observer_contract import MESSAGE_TYPES

    raw = decode_json(encoded)
    require(type(raw) is dict, "message object required")
    data = cast(dict[str, object], raw)
    for cls in MESSAGE_TYPES:
        if data.get("kind") == cls.kind:
            return cls.from_dict(data)
    raise ObserverContractError("malformed", "unknown message kind")


def reservation_identity(target: m.TargetReservation, run_digest: Digest, nonce: Identifier) -> str:
    """Reservation identity binds the complete token/lease/location to admission."""
    content = target.to_dict()
    del content["identity_sha256"], content["digest"]
    return sha256_json({"reservation": content, "run_identity_sha256": run_digest, "admitted_nonce": nonce})


def validate_retention_success(result: m.RetentionResult) -> None:
    require(result.classification != "partial", "partial retention cannot succeed")
    absent = result.request.required_class == "absence_and_logs"
    require((result.classification == "absent") == absent, "absence branch differs")
    require(not absent or not result.saved_state_available, "absent target cannot have saved state")
    require(result.request.required_class != "saved_state" or result.saved_state_available, "saved state unavailable")
    validate_interval(result.capture_finished_at, result.request.header)


def validate_outcome(status: m.Status, reason: m.FailureEvidence | None) -> None:
    require((status == "succeeded") == (reason is None), "non-success requires typed reason/evidence")
    if status == "timeout":
        require(reason is not None and reason.code in ("expired", "lock_timeout"), "timeout reason differs")


def validate_command_deadline(command: m.CommandPlan, header: m.Header) -> None:
    available = (timestamp(header.deadline) - timestamp(header.created_at)).total_seconds() * 1000
    require(command.timeout_ms <= available, "command exceeds deadline", "expired")


def validate_commands(receipts: tuple[m.CommandReceipt, ...], plan: m.CommandPlan | m.NoCommand) -> None:
    from . import release_observer_contract as m

    if isinstance(plan, m.NoCommand):
        require(not receipts, "no-command branch has receipts")
    else:
        require(len(receipts) == 1 and receipts[0].command == plan, "command receipt differs", "identity_mismatch")


def validate_allocation_intent(header: m.Header, allocation: m.AllocationResult, intent: m.PhaseIntent) -> None:
    from . import release_observer_contract as m

    validate_identity(allocation.request.header, header, pre_intent=True)
    require(header.prior_phase_receipt_sha256 == allocation.request.header.prior_phase_receipt_sha256, "allocation phase predecessor differs", "wrong_predecessor")
    validate_interval(header.created_at, allocation.request.header)
    validate_interval(header.deadline, allocation.request.header)
    require(header.intent_sha256 == intent.digest, "intent digest differs", "digest_mismatch")
    require(intent.allocation_sha256 == allocation.digest, "intent allocation differs", "digest_mismatch")
    require((intent.operation_id, intent.phase, intent.attempt, intent.command) == (header.operation_id, header.phase, header.attempt, allocation.command), "intent identity differs", "identity_mismatch")
    if isinstance(intent.command, m.CommandPlan):
        validate_command_deadline(intent.command, header)


def validate_identity(expected: m.Header, actual: m.Header, *, pre_intent: bool = False) -> None:
    """Compare complete admitted identity, including run nonce and source pins.

    Request IDs, intervals and predecessor cursors belong to individual queries;
    compare those with validate_request or the observation-chain checks instead.
    """
    from . import release_observer_contract as m

    excluded = {"request_id", "created_at", "deadline", "prior_phase_receipt_sha256", "prior_observation_id", "prior_observation_sha256"}
    if pre_intent:
        require(expected.intent_sha256 is None, "pre-intent comparison needs allocation")
        excluded.add("intent_sha256")
    for field in fields(m.Header):
        if field.name not in excluded:
            require(getattr(expected, field.name) == getattr(actual, field.name), f"identity field differs: {field.name}", "identity_mismatch")


def validate_request(expected: m.Header, actual: m.Header, *, now: Timestamp) -> None:
    """Call at admission/issue using an independently held expected header."""
    require(expected == actual, "request header differs from admission", "identity_mismatch")
    validate_interval(now, actual)


def validate_interval(value: Timestamp, header: m.Header) -> None:
    require(timestamp(value) >= timestamp(header.created_at), "future request or stale sample", "stale_sample")
    require(timestamp(value) <= timestamp(header.deadline), "deadline exceeded", "expired")


def validate_capture_times(result: m.ObservationResult) -> None:
    start, end = timestamp(result.sample_started_at), timestamp(result.sample_finished_at)
    require(start <= end, "sample interval reversed", "reversed_time")
    require(start >= timestamp(result.request.header.created_at), "sample predates request", "stale_sample")
    if result.status == "succeeded":
        validate_interval(result.sample_finished_at, result.request.header)


def validate_observation_binding(result: m.ObservationResult | m.RetentionResult) -> None:
    binding, header = result.observer_binding, result.request.header
    require((result.producer_id, result.producer_generation) == (binding.producer_id, binding.producer_generation), "producer generation differs from binding", "binding_mismatch")
    require(binding.host_id == header.allocated_target.host_id, "observer host differs", "binding_mismatch")
    require(binding.capability_contract_sha256 == header.capability_contract_sha256, "observer contract differs", "binding_mismatch")


def validate_phase_facts(facts: m.Facts, header: m.Header) -> None:
    from . import release_observer_contract as m

    expected = {"WAIT_VM": m.LeaseFacts, "CLONE": m.CloneFacts, "MEMORY": m.MemoryFacts, "BOOT": m.BootFacts, "INSTALL": m.InstallFacts, "SETUP": m.SetupFacts, "DOCTOR": m.DoctorFacts, "HEALTH": m.HealthFacts}
    require(type(facts) is expected[header.phase], "phase facts differ")
    if isinstance(facts, m.LeaseFacts):
        require(facts.reservation == header.allocated_target, "foreign reservation", "identity_mismatch")
        return
    target = facts.clone.target if isinstance(facts, (m.MemoryFacts, m.BootFacts)) else facts.target
    require(target.reservation == header.allocated_target, "foreign materialized target", "identity_mismatch")
    if header.phase in ("BOOT", "INSTALL", "SETUP", "DOCTOR", "HEALTH"):
        require(target.boot_id is not None, "boot epoch missing")
    clone = facts.clone if isinstance(facts, (m.MemoryFacts, m.BootFacts)) else facts
    if isinstance(clone, m.CloneFacts):
        require(clone.pristine_before_sha256 == header.protected_pristine.content_manifest_sha256, "clone source differs", "identity_mismatch")
    _manager_facts(facts, header)


def validate_manager_header(header: m.Header) -> None:
    manager_phase = header.phase in ("INSTALL", "SETUP", "DOCTOR", "HEALTH")
    transaction_phase = header.phase in ("SETUP", "DOCTOR", "HEALTH")
    require((header.manager_instance_name is not None) == manager_phase, "Manager instance phase association differs")
    require((header.release_operation_id is not None) == manager_phase, "release operation phase association differs")
    require((header.manager_transaction_id is not None) == transaction_phase, "Manager transaction phase association differs")
    if header.manager_transaction_id is not None:
        require(
            re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", header.manager_transaction_id) is not None,
            "Manager transaction UUID required",
        )


def _manager_facts(facts: m.Facts, header: m.Header) -> None:
    from . import release_observer_contract as m

    if isinstance(facts, m.InstallFacts):
        require((facts.source_pin, facts.source_tree, facts.source_manifest) == (header.source_pin, header.source_tree, header.source_manifest), "installed source differs", "identity_mismatch")
    if isinstance(facts, m.HealthFacts):
        require(facts.source_pin == header.source_pin, "health source differs", "identity_mismatch")
    attempts: tuple[m.ManagerAttempt, ...] = ()
    if isinstance(facts, m.SetupFacts):
        attempts = tuple(item.owner_attempt for item in facts.frontiers)
    if isinstance(facts, m.DoctorFacts):
        attempts = tuple(item.owner_attempt for item in facts.checks)
    for attempt in attempts:
        require(attempt.release_operation_id == header.release_operation_id, "Manager release operation differs", "identity_mismatch")
        require(attempt.transaction_id == header.manager_transaction_id, "Manager transaction differs", "identity_mismatch")
    require(len({item.transaction_id for item in attempts}) <= 1, "mixed Manager transactions", "identity_mismatch")


def observation_artifacts(result: m.ObservationResult) -> tuple[m.ArtifactRef, ...]:
    """Complete distinct artifact population, including nested fact/binding refs."""
    found: list[m.ArtifactRef] = []
    _collect_artifacts(result, found)
    return tuple(found)


def _collect_artifacts(value: object, found: list[m.ArtifactRef]) -> None:
    from . import release_observer_contract as m

    if isinstance(value, m.ArtifactRef):
        if value not in found:
            found.append(value)
    elif isinstance(value, Contract):
        for field in fields(value):
            _collect_artifacts(getattr(value, field.name), found)
    elif isinstance(value, tuple):
        for item in cast(tuple[object, ...], value):
            _collect_artifacts(item, found)


def validate_replay(previous: Contract, current: Contract) -> bool:
    """Return True only for identical canonical request replay; else conflict."""
    require(type(previous) is type(current), "replay kind differs", "request_conflict")
    require(encode_message(previous) == encode_message(current), "replay bytes differ", "request_conflict")
    return True


def validate_observation(request: m.ObserveRequest, result: m.ObservationResult, *, expected_binding: m.ObserverBinding, now: Timestamp, previous: m.ObservationResult | None) -> bool:
    """Validate fresh adoption; False denotes an exact already-adopted replay.

    Owner storage/idempotent publication is implemented by U2/U3, not this codec.
    Caller supplies the actual last accepted event, never an executor assertion.
    """
    require(result.request == request, "observation request differs", "identity_mismatch")
    require(result.observer_binding == expected_binding, "unadmitted observer binding", "binding_mismatch")
    require(timestamp(result.sample_finished_at) <= timestamp(now), "sample is in future", "future_sample")
    if previous is not None and previous.observation_id == result.observation_id:
        require(encode_message(previous) == encode_message(result), "same ID has different bytes", "duplicate_different")
        return False
    validate_interval(now, request.header)
    _chain_predecessor(request, result, previous)
    return True


def _chain_predecessor(request: m.ObserveRequest, result: m.ObservationResult, previous: m.ObservationResult | None) -> None:
    if previous is None:
        require(request.prior_sample_sequence == 0 and result.sample_sequence == 1, "missing observation", "missing_observation")
        return
    require(previous.sample_sequence == request.prior_sample_sequence, "sequence reordered or gapped", "sequence_reordered")
    require((request.header.prior_observation_id, request.header.prior_observation_sha256) == (previous.observation_id, previous.digest), "wrong predecessor", "wrong_predecessor")
    require(timestamp(previous.sample_finished_at) <= timestamp(result.sample_started_at), "sample predates predecessor", "stale_sample")
    _allocation_identity(previous.request.header, request.header)
    old_target, new_target = facts_target(previous.facts), facts_target(result.facts)
    if old_target is not None and new_target is not None:
        require(old_target.reservation == new_target.reservation, "chain target differs", "identity_mismatch")
        require((old_target.device, old_target.inode) == (new_target.device, new_target.inode), "chain object differs", "identity_mismatch")
        require(old_target.boot_id is None or old_target.boot_id == new_target.boot_id, "chain boot differs", "identity_mismatch")


def _allocation_identity(expected: m.Header, actual: m.Header) -> None:
    from . import release_observer_contract as m

    dynamic = {"request_id", "operation_id", "intent_sha256", "phase", "logical_ordinal", "attempt", "prior_phase_receipt_sha256", "prior_observation_id", "prior_observation_sha256", "created_at", "deadline"}
    for field in fields(m.Header):
        if field.name not in dynamic:
            require(getattr(expected, field.name) == getattr(actual, field.name), "cross-run allocation chain", "identity_mismatch")


def facts_target(facts: m.Facts | None) -> m.MaterializedTarget | None:
    from . import release_observer_contract as m

    if facts is None or isinstance(facts, m.LeaseFacts):
        return None
    return facts.clone.target if isinstance(facts, (m.MemoryFacts, m.BootFacts)) else facts.target


def validate_history(query: m.ReadObservationRequest, result: m.ReadObservationResult, *, expected_binding: m.ObserverBinding, now: Timestamp) -> None:
    """Validate fresh wrapper against the query without expiring original history."""
    require(result.request == query, "historical query differs", "identity_mismatch")
    require(result.attestation.observer_binding == expected_binding, "read binding differs", "binding_mismatch")
    require(timestamp(result.attestation.verified_at) <= timestamp(now), "read attestation is in future", "future_sample")
    validate_interval(now, query.header)


def validate_retention_result(result: m.RetentionResult) -> None:
    validate_outcome(result.status, result.reason)
    require(result.retention_intent_sha256 == result.request.header.intent_sha256, "retention intent differs", "identity_mismatch")
    require(result.retained_location == result.request.custody.retained_root, "retained location differs", "identity_mismatch")
    times = (result.request.failed_at, result.action_started_at, result.capture_started_at, result.capture_finished_at)
    require(tuple(map(timestamp, times)) == tuple(sorted(map(timestamp, times))), "retention clock reversed", "reversed_time")
    require(bool(result.verification_evidence), "retention evidence required")
    if result.status == "succeeded":
        validate_retention_success(result)
    validate_observation_binding(result)
    if result.command_receipts:
        validate_commands(result.command_receipts, result.request.command)



def validate_retention_reservation(reservation: m.RetentionReservation, header: m.Header) -> None:
    """Reject lexical protected-object overlap; native aliases need owner proof."""
    require(reservation.target == header.allocated_target, "retention target differs", "identity_mismatch")
    retained = reservation.retained_root.rstrip("/") + "/"
    for protected in (header.protected_pristine, header.forbidden_r33):
        source = protected.storage_root.rstrip("/") + "/"
        require(not retained.startswith(source) and not source.startswith(retained), "retention overlaps protected storage", "identity_mismatch")
