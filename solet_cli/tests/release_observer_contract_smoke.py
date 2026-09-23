#!/usr/bin/env python3
"""U1 T01/T02/T03/T14/T15/T18; inert fixtures, zero owner or VM effects."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "solet_setup_contracts/src"))

from solet_setup_contracts.release_observer_codec import (  # noqa: E402
    MAX_BYTES,
    MAX_ITEMS,
    Contract,
    ObserverContractError,
    canonical_json,
    decode_json,
    decode_message,
    encode_message,
    sha256_json,
)

from solet_setup_contracts import release_observer_contract as m  # noqa: E402

_checks = 0
_D = "a" * 64
_E = "b" * 64
_START = "2026-09-13T12:00:00+00:00"
_END = "2026-09-13T12:05:00+00:00"
_SAMPLE = "2026-09-13T12:01:00+00:00"
_NOW = "2026-09-13T12:02:00+00:00"
_LATER = "2026-09-14T12:00:00+00:00"


def _check(condition: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


def _reject(callback: Callable[[], object], label: str, code: str | None = None) -> None:
    try:
        callback()
    except ObserverContractError as error:
        _check(code is None or error.code == code, label + ": " + str(error))
    else:
        _check(False, label)


def _artifact(name: str = "evidence") -> m.ArtifactRef:
    return m.ArtifactRef(name, f"file:///host/{name}.json", _D, 12, "application/json", "owner")


def _binding(generation: str = "generation-1") -> m.ObserverBinding:
    return m.ObserverBinding("owner", generation, "/bin/observer", _D, "/bin/python3", "/prefix", (m.ModuleBinding("observer", "/prefix/observer.py", _D),), "manager", m.GitOID("sha1", "a" * 40), "host-1", "host", _D, _artifact("installed"))


def _header(phase: m.Phase = "WAIT_VM") -> m.Header:
    target = m.TargetReservation("host-1", "disposable", "/guest/app", "/vms/disposable", "token", "lease", _D)
    target = replace(target, identity_sha256=m.reservation_identity(target, _D, "nonce-1"))
    return m.Header(
        request_id="allocate-1", operation_id="release-operation-1", intent_sha256=None,
        run_identity_sha256=_D, proof_key=_D, admitted_nonce="nonce-1", repository="example-repo",
        release_id="rel-1", membership_revision=1, completion_sha256=_D, assembly_sha256=_D,
        source_pin=m.GitOID("sha1", "a" * 40), source_tree=m.GitOID("sha256", _D),
        source_manifest=_artifact("source"), phase=phase, logical_ordinal=1, attempt=1,
        policy_sha256=_D, capability_contract_sha256=_D,
        protected_pristine=m.ProtectedIdentity("host-1", "pristine", "/vms/pristine", _D, "snapshot-1"),
        forbidden_r33=m.ProtectedIdentity("host-1", "r33", "/vms/r33", _E, "snapshot-33"),
        protected_snapshot_manifest=_artifact("snapshots"), allocation_id="allocation-1",
        allocated_target=target,
        manager_instance_name="instance-1" if phase in ("INSTALL", "SETUP", "DOCTOR", "HEALTH") else None,
        manager_transaction_id="11111111-1111-1111-1111-111111111111" if phase in ("SETUP", "DOCTOR", "HEALTH") else None,
        release_operation_id="release-operation-1" if phase in ("INSTALL", "SETUP", "DOCTOR", "HEALTH") else None,
        prior_phase_receipt_sha256=None, prior_observation_id=None,
        prior_observation_sha256=None, created_at=_START, deadline=_END,
    )


def _allocation(header: m.Header, command: m.CommandPlan | m.NoCommand | None = None) -> m.AllocationResult:
    plan = m.NoCommand("reserve_only") if command is None else command
    outputs = (plan.stdout, plan.stderr, plan.result) if isinstance(plan, m.CommandPlan) else (m.OutputLocation("/host/output", 10000),)
    request = m.AllocationRequest(header, header.phase, outputs, ())
    retention = m.RetentionReservation("retention-1", header.allocated_target, "/retained/disposable", "hold-1", True)
    return m.AllocationResult(request, "owner", "generation-1", "reserved", plan, (header.phase,), retention)


def _request(phase: m.Phase = "WAIT_VM", command: m.CommandPlan | None = None) -> m.ObserveRequest:
    allocation = _allocation(_header(phase), command)
    intent = m.PhaseIntent("release-operation-1", allocation.digest, phase, 1, allocation.command)
    header = replace(allocation.request.header, request_id="observe-1", intent_sha256=intent.digest)
    target = _target(header, "boot-1") if phase in ("INSTALL", "SETUP", "DOCTOR", "HEALTH") else None
    return m.ObserveRequest(header, allocation, intent, (), phase, target, 0, "challenge-1")


def _target(header: m.Header, boot_id: str | None = None) -> m.MaterializedTarget:
    return m.MaterializedTarget(header.allocated_target, 1, 100, _D, _artifact("content"), boot_id)


def _observation(request: m.ObserveRequest | None = None) -> m.ObservationResult:
    req = _request() if request is None else request
    facts = m.LeaseFacts(req.header.allocated_target, _artifact("inventory"))
    return m.ObservationResult(req, "observation-1", "owner", "generation-1", _binding(), req.prior_sample_sequence + 1, _SAMPLE, _SAMPLE, "state-1", "state-1", "succeeded", facts, (), (_artifact(),), req.transport_challenge, None)


def _failure() -> m.FailureEvidence:
    return m.FailureEvidence("unavailable", "Owner source unavailable", (_artifact(),))


def _wire_child(raw: dict[str, object], *path: str) -> dict[str, object]:
    result = raw
    for key in path:
        result = cast(dict[str, object], result[key])
    return result


def _rehash(value: object) -> None:
    if isinstance(value, dict):
        data = cast(dict[str, object], value)
        for item in data.values():
            _rehash(item)
        if "schema_version" in data:
            digest_fields = {
                "allocation_request": "request_sha256", "observe_request": "request_sha256",
                "execution_request": "request_sha256", "retention_request": "request_sha256",
                "read_observation_request": "request_sha256", "allocation_result": "allocation_sha256",
                "phase_intent": "intent_sha256", "observation_result": "observation_sha256",
                "retention_result": "retention_result_sha256",
            }
            key = digest_fields.get(cast(str, data["kind"]), "digest")
            data[key] = sha256_json({name: item for name, item in data.items() if name != key})
    elif isinstance(value, list):
        for item in cast(list[object], value):
            _rehash(item)


def _changed(message: Contract, path: tuple[str, ...], key: str, value: object) -> bytes:
    raw = copy.deepcopy(message.to_dict())
    _wire_child(raw, *path)[key] = value
    _rehash(raw)
    return canonical_json(raw)


def test_t01_exact_codec() -> None:
    request = _request()
    raw = encode_message(request)
    _check(raw == encode_message(decode_message(raw)), "T01 canonical byte-stable roundtrip")
    _check(b'"schema_version":"release_observer.v1"' in raw, "T01 protocol")
    spaced = json.dumps(request.to_dict(), indent=2).encode()
    _check(encode_message(decode_message(spaced)) == raw, "T01 whitespace canonicalized")
    bad_json = (b'{"kind":1,"kind":2}', b'{"n":NaN}', b'{"n":Infinity}', b'{"n":-Infinity}', b'{"n":1e999}', b'\xff', b'{}x', b'[]', b'null', b'{"kind":"foreign"}')
    for encoded in bad_json:
        _reject(lambda encoded=encoded: decode_message(encoded), "T01 malformed JSON")
    duplicate = raw.replace(b'"request_id":"observe-1"', b'"request_id":"observe-1","request_id":"observe-1"')
    _reject(lambda: decode_message(duplicate), "T01 nested duplicate keys")
    for key, value in (("extra", 1), ("attempt", True), ("attempt", 0), ("attempt", -1), ("attempt", 1.0), ("membership_revision", False), ("logical_ordinal", 0), ("proof_key", "A" * 64), ("proof_key", "a" * 63), ("request_id", "bad id"), ("request_id", "../escape"), ("request_id", "x" * 257), ("request_id", ""), ("created_at", "2026-09-13"), ("created_at", "2026-09-13T12:00:00"), ("created_at", "2026-02-30T12:00:00Z"), ("deadline", None), ("prior_observation_id", "only-one")):
        _reject(lambda key=key, value=value: decode_message(_changed(request, ("header",), key, value)), "T01 scalar/key " + key)
    for path in ("relative", "/a/../b", "/a/./b", "//a", "/a//b", "/a/", "/a%2fb", "/a\\b"):
        _reject(lambda path=path: m.ModuleBinding("module", path, _D), "T01 absolute path")
    for uri in ("file://guest/a", "https://host/a", "file:///a?x", "file:///a#x", "file:////a", "file:///a/../b"):
        _reject(lambda uri=uri: replace(_artifact(), uri=uri), "T01 artifact namespace")
    for algorithm, oid in (("sha1", "a" * 39), ("sha256", "A" * 64), ("sha256", "a" * 40)):
        raw_oid = m.GitOID("sha1", "a" * 40).to_dict()
        raw_oid.update(algorithm=algorithm, value=oid)
        _rehash(raw_oid)
        _reject(lambda raw_oid=raw_oid: m.GitOID.from_dict(raw_oid), "T01 Git algorithm/full width")
    _reject(lambda: replace(_artifact(), byte_count=True), "T01 bool size")
    _reject(lambda: replace(_artifact(), byte_count=-1), "T01 negative size")
    _reject(lambda: replace(request.header, attempt=2**63), "T01 integer bound")
    _reject(lambda: decode_json(b'[' * 1000 + b'0' + b']' * 1000), "T01 nesting bound")
    _reject(lambda: decode_json(b'[' + b'0,' * MAX_ITEMS + b'0]'), "T01 collection bound")
    _reject(lambda: decode_json(b'"' + b'x' * (MAX_BYTES + 1) + b'"'), "T01 payload bound")
    _reject(lambda: decode_json(b'"\\ud800"'), "T01 surrogate")
    _reject(lambda: decode_json(b'"\\u0000"'), "T01 NUL")
    _reject(lambda: decode_json(b'"' + b'x' * 4097 + b'"'), "T01 text bound")
    _reject(lambda: decode_json(b'{"integer":9223372036854775808}'), "T01 JSON integer bound")
    _reject(lambda: canonical_json(float("nan")), "T01 encoder nonfinite")
    _reject(lambda: canonical_json({1: "invalid"}), "T01 encoder key")
    for key in request.to_dict():
        missing = request.to_dict()
        del missing[key]
        _reject(lambda missing=missing: decode_message(canonical_json(missing)), "T01 missing " + key)
    for path in ((), ("header",), ("allocation", "request", "header", "source_pin")):
        _reject(lambda path=path: decode_message(_changed(request, path, "injected", "bad")), "T01 recursive extra key")
    _reject(lambda: decode_message(raw.replace(b'"request_sha256":"', b'"request_sha256":"0', 1)), "T01 digest corruption")
    _check(replace(request.header, created_at="2026-09-13T05:00:00-07:00").created_at.endswith("-07:00"), "T01 non-UTC explicit offset allowed")
    _check(m.GitOID("sha256", _D).value == _D, "T01 sha256 Git positive")
    unicode_raw = replace(_failure(), detail="測定不可").to_dict()
    _check("測定不可".encode() in canonical_json(unicode_raw), "T01 UTF-8 canonical")


def test_t02_t03_admission() -> None:
    header = _header()
    _check(_header("INSTALL").manager_instance_name == "instance-1", "T02 INSTALL has planned Manager instance")
    _check(_header("INSTALL").manager_transaction_id is None, "T02 INSTALL has no transaction")
    _check(_header("SETUP").manager_transaction_id == "11111111-1111-1111-1111-111111111111", "T02 SETUP has canonical transaction UUID")
    _reject(lambda: replace(_header("INSTALL"), manager_instance_name=None), "T02 missing planned Manager instance")
    _reject(lambda: replace(_header(), manager_instance_name="instance-1"), "T02 VM phase Manager instance")
    _reject(lambda: replace(_header("SETUP"), manager_transaction_id="not-a-uuid"), "T02 malformed Manager transaction")
    mutations: tuple[tuple[str, object], ...] = (
        ("operation_id", "foreign-op"), ("run_identity_sha256", _E), ("admitted_nonce", "foreign-nonce"),
        ("release_id", "foreign-release"), ("membership_revision", 2), ("completion_sha256", _E),
        ("assembly_sha256", _E), ("source_pin", m.GitOID("sha1", "b" * 40)),
        ("source_tree", m.GitOID("sha256", _E)), ("source_manifest", _artifact("foreign-source")),
        ("repository", "foreign"), ("policy_sha256", _E), ("capability_contract_sha256", _E),
        ("allocation_id", "foreign-allocation"), ("attempt", 2),
    )
    for key, value in mutations:
        _reject(lambda key=key, value=value: m.validate_request(header, replace(header, **{key: value}), now=_NOW), "T02/T03 admitted " + key)
    # Fully coherent foreign messages have locally valid digests; expected admission still wins.
    for key, value in mutations:
        if key in ("run_identity_sha256", "admitted_nonce"):
            target = replace(header.allocated_target, identity_sha256=m.reservation_identity(header.allocated_target, _E if key == "run_identity_sha256" else _D, "foreign-nonce" if key == "admitted_nonce" else "nonce-1"))
            foreign = replace(header, allocated_target=target, **{key: value})
        else:
            foreign = replace(header, **{key: value})
        result = _allocation(foreign)
        decoded = cast(m.AllocationResult, decode_message(encode_message(result)))
        _check(decoded.request.header.proof_key == header.proof_key, "T02/T03 same proof key")
        _reject(lambda decoded=decoded: m.validate_identity(header, decoded.request.header), "T02/T03 fully rehashed " + key, "identity_mismatch")
    _check(m.validate_request(header, header, now=_NOW) is None, "T02 exact admission")
    request = _request()
    _check(m.validate_replay(request, decode_message(encode_message(request))), "T02 identical request replay")
    _reject(lambda: m.validate_replay(request, replace(request, transport_challenge="changed")), "T02 changed replay", "request_conflict")
    _reject(lambda: m.validate_replay(request, request.allocation), "T02 wrong replay kind", "request_conflict")


def _next(previous: m.ObservationResult) -> tuple[m.ObserveRequest, m.ObservationResult]:
    header = replace(previous.request.header, request_id="observe-2", prior_observation_id=previous.observation_id, prior_observation_sha256=previous.digest)
    request = replace(previous.request, header=header, prior_sample_sequence=previous.sample_sequence, transport_challenge="challenge-2")
    result = replace(previous, request=request, observation_id="observation-2", sample_sequence=2, transport_challenge=request.transport_challenge)
    return request, result


def _adopt(result: m.ObservationResult, previous: m.ObservationResult | None = None, now: str = _NOW) -> bool:
    return m.validate_observation(result.request, result, expected_binding=_binding(), now=now, previous=previous)


def test_t14_chain() -> None:
    first = _observation()
    _, second = _next(first)
    _check(_adopt(first), "T14 first sample")
    _check(_adopt(second, first), "T14 contiguous same-clock sample")
    _check(not _adopt(first, first, _LATER), "T14 identical adoption replay after deadline")
    _reject(lambda: _adopt(second), "T14 missing chain", "missing_observation")
    _reject(lambda: replace(first, sample_sequence=2), "T14 gap", "sequence_gap")
    _reject(lambda: replace(first, sample_sequence=True), "T14 bool sequence")
    _reject(lambda: _adopt(first, second), "T14 reordered", "sequence_reordered")
    changed = replace(first, source_artifacts=(_artifact("different"),))
    _reject(lambda: _adopt(changed, first), "T14 duplicate-different", "duplicate_different")
    wrong_header = replace(second.request.header, prior_observation_sha256=_E)
    wrong_request = replace(second.request, header=wrong_header)
    wrong = replace(second, request=wrong_request)
    _reject(lambda: _adopt(wrong, first), "T14 wrong predecessor", "wrong_predecessor")
    _reject(lambda: replace(first, state_generation_after="changed"), "T14 incoherent generation", "generation_changed")
    stale = replace(second, sample_started_at="2026-09-13T12:00:30Z", sample_finished_at="2026-09-13T12:00:30Z")
    _reject(lambda: _adopt(stale, first), "T14 stale sample", "stale_sample")
    foreign_allocation = _allocation(replace(_header(), release_id="foreign"))
    foreign_intent = replace(second.request.intent, allocation_sha256=foreign_allocation.digest)
    foreign_request = replace(second.request, allocation=foreign_allocation, intent=foreign_intent, header=replace(second.request.header, release_id="foreign", intent_sha256=foreign_intent.digest))
    foreign_result = replace(second, request=foreign_request)
    _reject(lambda: _adopt(foreign_result, first), "T14 cross-Release chain", "identity_mismatch")
    _reject(lambda: replace(first, transport_challenge="foreign"), "T14 wrong challenge", "binding_mismatch")
    _reject(lambda: _adopt(replace(first, observer_binding=replace(_binding(), executable_sha256=_E))), "T14 unadmitted binding", "binding_mismatch")
    _reject(lambda: replace(first, observer_binding=replace(_binding(), host_id="foreign")), "T14 foreign host", "binding_mismatch")
    _reject(lambda: replace(first, facts=m.LeaseFacts(replace(first.request.header.allocated_target, name="foreign"), _artifact())), "T14 foreign nested target", "identity_mismatch")


def _history(original: m.ObservationResult) -> m.ReadObservationResult:
    header = replace(original.request.header, request_id="read-1", created_at=_LATER, deadline="2026-09-14T12:05:00Z")
    query = m.ReadObservationRequest(header, original.observation_id, original.digest, "fresh-challenge")
    att = m.ReadAttestation(query.digest, original.observation_id, original.digest, original.producer_id, "generation-2", _binding("generation-2"), "2026-09-14T12:01:00Z", m.observation_artifacts(original), query.transport_challenge)
    return m.ReadObservationResult(query, original, att)


def test_t15_time_and_history() -> None:
    original = _observation()
    _reject(lambda: replace(original.request.header, deadline=_START), "T15 reversed request", "reversed_time")
    _reject(lambda: replace(original, sample_started_at=_NOW), "T15 reversed sample", "reversed_time")
    _reject(lambda: replace(original, sample_finished_at=_LATER), "T15 expired capture", "expired")
    _reject(lambda: _adopt(original, now=_START), "T15 future capture", "future_sample")
    _reject(lambda: _adopt(original, now=_LATER), "T15 expired new adoption", "expired")
    _reject(lambda: m.validate_request(original.request.header, original.request.header, now="2026-09-13T11:59:00Z"), "T15 future request", "stale_sample")
    for status in ("absent", "waiting", "failed", "unknown", "conflicting", "timeout"):
        reason = m.FailureEvidence("expired", "Deadline expired", (_artifact(),)) if status == "timeout" else _failure()
        wire = original.to_dict()
        wire.update(status=status, facts=None, reason=reason.to_dict())
        _rehash(wire)
        result = cast(m.ObservationResult, decode_message(canonical_json(wire)))
        _check(result.status == status, "T15 typed " + status)
        _reject(lambda result=result: replace(result, reason=None), "T15 missing reason")
        _reject(lambda result=result: replace(result, facts=original.facts), "T15 facts outside success")
    _reject(lambda: replace(_failure(), evidence=()), "T15 missing failure evidence")
    _reject(lambda: replace(original, reason=_failure()), "T15 success has failure")
    history = _history(original)
    decoded = cast(m.ReadObservationResult, decode_message(encode_message(history)))
    _check(encode_message(decoded.observation) == encode_message(original), "T15 immutable original byte equality")
    _check(decoded.observation.transport_challenge == "challenge-1", "T15 original challenge untouched")
    _check(decoded.attestation.transport_challenge == "fresh-challenge", "T15 separate fresh challenge")
    _check(m.validate_history(history.request, decoded, expected_binding=_binding("generation-2"), now="2026-09-14T12:02:00Z") is None, "T15 valid historical read after expiry")
    _reject(lambda: replace(history, attestation=replace(history.attestation, transport_challenge="challenge-1")), "T15 stale read challenge", "binding_mismatch")
    _reject(lambda: replace(history, observation=replace(original, source_artifacts=(_artifact("different"),))), "T15 original changed", "digest_mismatch")
    _reject(lambda: replace(history, attestation=replace(history.attestation, verified_artifacts=())), "T15 missing historical artifacts", "artifact_mismatch")
    _reject(lambda: m.validate_history(history.request, history, expected_binding=replace(_binding(), prefix="/foreign"), now="2026-09-14T12:02:00Z"), "T15 historical producer binding", "binding_mismatch")
    _reject(lambda: m.validate_history(history.request, history, expected_binding=_binding("generation-2"), now=_LATER), "T15 future read attestation", "future_sample")
    _reject(lambda: m.validate_history(history.request, history, expected_binding=_binding("generation-2"), now="2026-09-15T12:00:00Z"), "T15 expired read wrapper", "expired")


def _command() -> m.CommandPlan:
    return m.CommandPlan(_binding(), ("/bin/observer", "execute"), "/guest/app", (m.EnvironmentEntry("TART_NO_AUTO_PRUNE", "1"),), _D, _artifact("stdin"), 1000, m.OutputLocation("/host/stdout.json", 100), m.OutputLocation("/host/stderr.json", 100), m.OutputLocation("/host/result.json", 100))


def _receipt(plan: m.CommandPlan) -> m.CommandReceipt:
    return m.CommandReceipt(plan, "exited", 0, _artifact("stdout"), _artifact("stderr"), _artifact("result"), _SAMPLE, _SAMPLE)


def test_execution_and_retention() -> None:
    observation = _request(command=_command())
    execution = m.ExecutionRequest(observation.header, observation.allocation, observation.intent)
    receipt = _receipt(_command())
    executed = m.ExecutionResult(execution, "owner", "generation-1", "submitted", "succeeded", (receipt,), None)
    for message in (observation.allocation.request, observation.allocation, execution, executed):
        _check(encode_message(decode_message(encode_message(message))) == encode_message(message), "ownership roundtrip " + message.kind)
    _reject(lambda: replace(executed, submission="not_submitted"), "execution submission branch")
    _reject(lambda: replace(executed, command_receipts=()), "execution missing result")
    _reject(lambda: replace(executed, command_receipts=(replace(receipt, exit_code=1),)), "execution failed command")
    _reject(lambda: replace(receipt, exit_code=True), "execution bool exit")
    _reject(lambda: replace(receipt, outcome="unknown"), "execution unknown exit")
    _reject(lambda: replace(receipt, stdout=_artifact("other")), "execution wrong artifact")
    _reject(lambda: replace(receipt, stdout=replace(receipt.stdout, byte_count=101)), "execution stream limit")
    _reject(lambda: replace(_command(), stderr=_command().stdout), "execution output alias")
    _reject(lambda: replace(_command(), argv=("/bin/foreign",)), "execution argv drift")
    _reject(lambda: _allocation(_header(), replace(_command(), timeout_ms=1000000)), "execution deadline")
    retention = m.RetentionRequest(observation.header, observation.header.operation_id, _D, "failed-observation", _D, _START, None, observation.allocation.retention, "retention-operation", "absence_and_logs", m.NoCommand("retain_in_place"), (_artifact(),))
    retained = m.RetentionResult(retention, observation.header.intent_sha256 or _D, _binding(), "owner", "generation-1", 1, _SAMPLE, _SAMPLE, _SAMPLE, "succeeded", "absent", _artifact("retained-manifest"), "/retained/disposable", False, _artifact("hold"), (_artifact(),), (), None)
    for message in (retention, retained):
        _check(encode_message(decode_message(encode_message(message))) == encode_message(message), "retention roundtrip")
    partial = replace(retained, status="unknown", classification="partial", reason=m.FailureEvidence("retention_partial", "Incomplete capture", (_artifact(),)))
    _check(decode_message(encode_message(partial)) == partial, "retention partial preserves evidence")
    _reject(lambda: replace(retained, classification="partial"), "partial cannot succeed")
    _reject(lambda: replace(retained, retained_location="/vms/pristine"), "retention foreign location")
    _reject(lambda: replace(retention, failed_operation_id="foreign"), "retention foreign failure")
    _reject(lambda: replace(retained, capture_finished_at=_START), "retention reversed capture")
    _reject(lambda: replace(retention, custody=replace(retention.custody, retained_root="/vms/pristine")), "retention protected source")
    _reject(lambda: replace(retained, saved_state_available=True), "absent target saved-state branch")
    materialized = replace(retention, target=_target(retention.header, "boot-1"), required_class="saved_state")
    captured = replace(retained, request=materialized, classification="complete", saved_state_available=True)
    _check(decode_message(encode_message(captured)) == captured, "materialized retention complete")
    _reject(lambda: replace(captured, saved_state_available=False), "required saved state unavailable")


def test_measurement_branches() -> None:
    for phase in ("CLONE", "MEMORY", "BOOT"):
        request = _request(cast(m.Phase, phase))
        target = _target(request.header, "boot-1" if phase == "BOOT" else None)
        clone = m.CloneFacts(target, _D, _D, _artifact("lineage"), _artifact("inventory"))
        facts: m.Facts = clone
        if phase == "MEMORY":
            facts = m.MemoryFacts(clone, 24576, _artifact("configuration"))
        if phase == "BOOT":
            facts = m.BootFacts(clone, 24576, 25769803776, _artifact("process"), _artifact("kernel"))
        result = replace(_observation(), request=request, facts=facts)
        _check(decode_message(encode_message(result)) == result, "phase roundtrip " + phase)
        _reject(lambda clone=clone: replace(clone, pristine_after_sha256=_E), "clone drift")
    attempt = m.ManagerAttempt("release-operation-1", "11111111-1111-1111-1111-111111111111", "adapter-op", "probe-op", "22222222-2222-2222-2222-222222222222", 1, _artifact())
    _check(attempt.release_operation_id != attempt.transaction_id != attempt.adapter_operation_id, "distinct Manager identities")
    _reject(lambda: replace(attempt, transaction_id="release-operation-1"), "transaction cannot be release ID")
    request = _request("SETUP")
    target = cast(m.MaterializedTarget, request.expected_target)
    frontier = m.SetupFrontier(attempt, _artifact("preview"), _D, ("key",), (_artifact("submission"),), _artifact("transaction"))
    facts = m.SetupFacts(_artifact("profile"), target, _D, (frontier,), ("adapter-op",), ("adapter-op",), "verified", True, _artifact("transaction"))
    result = replace(_observation(), request=request, facts=facts, source_artifacts=(_artifact(), frontier.preview, *frontier.submissions))
    _check(decode_message(encode_message(result)) == result, "setup closed facts")
    _reject(lambda: replace(facts, completed_operations=()), "setup subset")
    _reject(lambda: replace(result, facts=replace(facts, target=replace(target, boot_id="foreign"))), "foreign boot epoch", "identity_mismatch")
    foreign_frontier = replace(frontier, owner_attempt=replace(attempt, release_operation_id="foreign"))
    _reject(lambda: replace(result, facts=replace(facts, frontiers=(foreign_frontier,))), "foreign Manager operation", "identity_mismatch")
    foreign_transaction = replace(frontier, owner_attempt=replace(attempt, transaction_id="33333333-3333-3333-3333-333333333333"))
    _reject(lambda: replace(result, facts=replace(facts, frontiers=(foreign_transaction,))), "foreign Manager transaction", "identity_mismatch")
    _reject(lambda: replace(result, source_artifacts=(_artifact(),)), "missing retained setup artifacts", "artifact_mismatch")


def test_owner_fact_boundaries() -> None:
    install_request = _request("INSTALL")
    install_target = cast(m.MaterializedTarget, install_request.expected_target)
    install = m.InstallFacts(install_target, _D, _artifact("formula"), m.GitOID("sha1", "b" * 40), _binding(), _artifact("manager-lock"), m.GitOID("sha1", "c" * 40), m.GitOID("sha256", _D), _artifact("seed-manifest"), install_request.header.source_pin, install_request.header.source_tree, install_request.header.source_manifest, "CLEAN")
    install_result = replace(_observation(), request=install_request, facts=install)
    _check(decode_message(encode_message(install_result)) == install_result, "install closed facts")
    _reject(lambda: replace(install_result, facts=replace(install, source_tree=m.GitOID("sha256", _E))), "install foreign source", "identity_mismatch")
    _reject(lambda: replace(install_result, facts=replace(install, source_manifest=_artifact("foreign"))), "install foreign manifest", "identity_mismatch")
    plan = _command()
    doctor_request = _request("DOCTOR", plan)
    target = cast(m.MaterializedTarget, doctor_request.expected_target)
    attempt = m.ManagerAttempt("release-operation-1", "11111111-1111-1111-1111-111111111111", "doctor", "post-probe", "22222222-2222-2222-2222-222222222222", 1, _artifact())
    check = m.DoctorCheck("database", attempt, True, "verified", _artifact("read-only"))
    doctor = m.DoctorFacts(target, _receipt(plan), _artifact("doctor-result"), (check,), ("database",), _D, "verified", True)
    doctor_result = replace(_observation(), request=doctor_request, facts=doctor, command_receipts=(_receipt(plan),))
    _check(decode_message(encode_message(doctor_result)) == doctor_result, "doctor closed facts")
    _reject(lambda: replace(doctor, checks=()), "doctor empty checks")
    _reject(lambda: replace(doctor, required_check_ids=("database", "missing")), "doctor subset")
    _reject(lambda: replace(doctor_result, command_receipts=()), "doctor missing execution receipt")
    _reject(lambda: replace(doctor_result, producer_generation="foreign"), "producer generation binding", "binding_mismatch")
    health_binding = replace(_binding(), executable="/guest/app/.venv/bin/solet-bridge")
    health_plan = replace(plan, binding=health_binding, argv=(health_binding.executable, "health"))
    health_request = _request("HEALTH", health_plan)
    health_target = cast(m.MaterializedTarget, health_request.expected_target)
    health = m.HealthFacts(health_target, _receipt(health_plan), _artifact("health-result"), "healthy", "instance-1", _D, health_request.header.source_pin, m.GitOID("sha256", _D), _binding().digest)
    health_result = replace(_observation(), request=health_request, facts=health, command_receipts=(_receipt(health_plan),))
    _check(decode_message(encode_message(health_result)) == health_result, "health closed facts")
    _reject(lambda: replace(health_result, facts=replace(health, source_pin=m.GitOID("sha1", "b" * 40))), "health foreign source", "identity_mismatch")
    _reject(lambda: replace(health, command=_receipt(plan)), "health wrong command")
    _reject(lambda: replace(health_request, header=replace(health_request.header, deadline=_LATER)), "cannot extend allocation deadline", "expired")
    _reject(lambda: replace(health_request, header=replace(health_request.header, prior_phase_receipt_sha256=_D)), "allocation predecessor changed", "wrong_predecessor")
    native = m.NativeBinding("/bin/tart", _D, "2.32.1", _D)
    native_plan = replace(plan, binding=native, argv=("/bin/tart", "clone", "pristine", "disposable"))
    _check(m.CommandPlan.from_dict(native_plan.to_dict()) == native_plan, "native binding has no invented Python provenance")
    history = _history(_observation())
    _reject(lambda: replace(history, attestation=replace(history.attestation, producer_generation="foreign")), "history producer generation binding", "binding_mismatch")


def test_t18_standalone() -> None:
    with tempfile.TemporaryDirectory(prefix="observer-u1-") as temp:
        script = "\n".join((
            "import sys, importlib.util, json",
            f"sys.path.insert(0, {str(_ROOT / 'solet_setup_contracts/src')!r})",
            "assert importlib.util.find_spec('ananta') is None",
            "assert importlib.util.find_spec('seed_factory_plugin') is None",
            "from solet_setup_contracts import release_observer_contract as model, release_observer_codec as codec",
            "assert model.PROTOCOL == 'release_observer.v1'",
            "assert 'ananta' not in sys.modules and 'seed_factory_plugin' not in sys.modules",
            "print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,'model':model.__file__,'codec':codec.__file__}))",
        ))
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script], cwd=temp, capture_output=True, text=True, timeout=30, check=False)
        _check(result.returncode == 0, "T18 standalone import: " + result.stderr)
        evidence = cast(dict[str, str], json.loads(result.stdout))
        for key in ("model", "codec"):
            _check(Path(evidence[key]).is_relative_to(_ROOT), "T18 actual child candidate import")
        print("T18 provenance: " + result.stdout.strip())
    config = tomllib.loads((_ROOT / "plugins/seed_factory_plugin/pyproject.toml").read_text())
    _check(config["project"]["dependencies"].count("solet-setup-contracts") == 1, "T18 direct dependency once")
    contract_config = tomllib.loads((_ROOT / "solet_setup_contracts/pyproject.toml").read_text())
    _check(contract_config["project"]["dependencies"] == [], "T18 shared package stdlib only")
    for module in (m, sys.modules["solet_setup_contracts.release_observer_codec"]):
        _check(Path(cast(str, module.__file__)).is_relative_to(_ROOT), "T18 main process candidate import")


def main() -> None:
    for test in (test_t01_exact_codec, test_t02_t03_admission, test_t14_chain, test_t15_time_and_history, test_execution_and_retention, test_measurement_branches, test_owner_fact_boundaries, test_t18_standalone):
        test()
    print(f"PASS: {_checks} assertions; 8 groups passed, 0 failed, 0 skipped")
    print("Smoke SHA256: " + hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
