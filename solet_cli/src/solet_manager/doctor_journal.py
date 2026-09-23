"""Closed kind=doctor journal (Step 6 design section 3.5).

One journal per ``(instance, contract, update operation)``; runs are appended
and never rewritten (A4: audit evidence is never deleted; growth is bounded by
the number of distinct contracts, D5).  The v1 ``maintenance_journal`` is an
import document with import stage invariants, so the doctor gets its own
closed shape here rather than loosening that one.  The final doctor inside
``update --yes`` writes to the same journal as a standalone ``doctor`` run
under contract 1, so the executor's oracle and the operator's report are one
record.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from solet_setup_contracts import canonical_sha256

from .errors import StateConflictError, StateError
from .models import DoctorContractKind, JsonValue
from .state_io import atomic_write_json
from .transaction import utc_now

DOCTOR_JOURNAL_SCHEMA_VERSION = 1
_KEYS = frozenset({"schema_version", "kind", "operation_id", "instance_id", "contract", "runs", "created_at", "updated_at"})
_CONTRACT_KEYS = frozenset({"kind", "bundle_digest", "update_operation_id", "expected_source", "expected_runtime"})
_RUN_KEYS = frozenset({"run", "started_at", "finished_at", "head_observed", "sections", "counts", "status", "exit_code", "preservation", "evidence_digest", "service_check_verified"})
_STATUSES = ("verified", "incomplete", "failed", "invalid")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPERATION_ID = re.compile(r"^opr_[0-9a-f]{32}$")
_INSTANCE_ID = re.compile(r"^ins_[0-9a-f]{32}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def doctor_operation_id(instance_id: str, contract: DoctorContractKind, bundle_digest: str | None, update_operation_id: str | None) -> str:
    return "opr_" + canonical_sha256(["doctor", instance_id, contract.value, bundle_digest or "", update_operation_id or ""]).removeprefix("sha256:")[:32]


def create_doctor_journal(
    *,
    instance_id: str,
    contract: DoctorContractKind,
    bundle_digest: str | None,
    update_operation_id: str | None,
    expected_source: dict[str, JsonValue],
    expected_runtime: dict[str, JsonValue] | None,
    timestamp: str | None = None,
) -> dict[str, JsonValue]:
    now = utc_now() if timestamp is None else timestamp
    value: dict[str, JsonValue] = {
        "schema_version": DOCTOR_JOURNAL_SCHEMA_VERSION,
        "kind": "doctor",
        "operation_id": doctor_operation_id(instance_id, contract, bundle_digest, update_operation_id),
        "instance_id": instance_id,
        "contract": {
            "kind": contract.value,
            "bundle_digest": bundle_digest,
            "update_operation_id": update_operation_id,
            "expected_source": expected_source,
            "expected_runtime": expected_runtime,
        },
        "runs": [],
        "created_at": now,
        "updated_at": now,
    }
    return _validated(value)


def append_doctor_run(
    value: dict[str, JsonValue],
    *,
    started_at: str,
    head_observed: str | None,
    sections: list[JsonValue],
    counts: dict[str, JsonValue],
    status: str,
    exit_code: int,
    preservation: dict[str, JsonValue],
    service_check_verified: bool,
    timestamp: str | None = None,
) -> dict[str, JsonValue]:
    """Append one complete run; ``evidence_digest`` is ``canonical_sha256`` over the run's sections."""
    current = _validated(value)
    now = utc_now() if timestamp is None else timestamp
    runs = list(cast(list[JsonValue], current["runs"]))
    runs.append(
        {
            "run": len(runs),
            "started_at": started_at,
            "finished_at": now,
            "head_observed": head_observed,
            "sections": sections,
            "counts": counts,
            "status": status,
            "exit_code": exit_code,
            "preservation": preservation,
            "evidence_digest": canonical_sha256(sections),
            "service_check_verified": service_check_verified,
        }
    )
    next_value = dict(current)
    next_value["runs"] = runs
    next_value["updated_at"] = now
    return _validated(next_value)


def latest_run(value: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    runs = cast(list[JsonValue], value["runs"])
    return None if not runs else cast(dict[str, JsonValue], runs[-1])


def parse_doctor_journal_bytes(raw: bytes) -> dict[str, JsonValue]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("doctor journal is unreadable") from exc
    return _validated(value)


def read_doctor_journal(path: Path) -> dict[str, JsonValue]:
    try:
        return parse_doctor_journal_bytes(path.read_bytes())
    except OSError as exc:
        raise StateError("doctor journal is unreadable") from exc


def write_doctor_journal(path: Path, previous: dict[str, JsonValue] | None, next_value: dict[str, JsonValue]) -> None:
    """Atomically persist; the contract is immutable and runs are an immutable prefix."""
    document = _validated(next_value)
    if previous is not None:
        old = _validated(previous)
        for key in ("schema_version", "kind", "operation_id", "instance_id", "contract", "created_at"):
            if old[key] != document[key]:
                raise StateConflictError("doctor journal identity or contract changed")
        old_runs = cast(list[JsonValue], old["runs"])
        new_runs = cast(list[JsonValue], document["runs"])
        if new_runs[: len(old_runs)] != old_runs or len(new_runs) - len(old_runs) not in {0, 1}:
            raise StateConflictError("doctor journal runs are not an immutable prefix")
    atomic_write_json(path, document)
    if read_doctor_journal(path) != document:
        raise StateError("doctor journal read-back mismatch")


def _validated(raw: object) -> dict[str, JsonValue]:
    if not isinstance(raw, dict) or frozenset(raw) != _KEYS:
        raise StateError("doctor journal does not match the closed v1 shape")
    value = cast(dict[str, JsonValue], raw)
    if value["schema_version"] != DOCTOR_JOURNAL_SCHEMA_VERSION or value["kind"] != "doctor":
        raise StateError("doctor journal does not match the closed v1 shape")
    _pattern(value["operation_id"], _OPERATION_ID)
    _pattern(value["instance_id"], _INSTANCE_ID)
    _contract(value["contract"])
    runs = value["runs"]
    if not isinstance(runs, list):
        raise StateError("doctor journal runs are invalid")
    for index, item in enumerate(runs):
        _run(item, index)
    _text(value["created_at"])
    _text(value["updated_at"])
    return cast(dict[str, JsonValue], json.loads(json.dumps(value, sort_keys=True)))


def _contract(value: JsonValue) -> None:
    item = _object(value, _CONTRACT_KEYS)
    try:
        DoctorContractKind(_text(item["kind"]))
    except ValueError as exc:
        raise StateError("doctor journal contract kind is invalid") from exc
    if item["bundle_digest"] is not None:
        _pattern(item["bundle_digest"], _DIGEST)
    if item["update_operation_id"] is not None:
        _pattern(item["update_operation_id"], _OPERATION_ID)
    if not isinstance(item["expected_source"], dict):
        raise StateError("doctor journal expected source is invalid")
    if item["expected_runtime"] is not None and not isinstance(item["expected_runtime"], dict):
        raise StateError("doctor journal expected runtime is invalid")


def _run(value: JsonValue, index: int) -> None:
    item = _object(value, _RUN_KEYS)
    if item["run"] != index or isinstance(item["run"], bool):
        raise StateError("doctor journal run numbering is invalid")
    _text(item["started_at"])
    _text(item["finished_at"])
    if item["head_observed"] is not None:
        _pattern(item["head_observed"], _COMMIT)
    _run_evidence(item)
    _run_verdict(item)


def _run_evidence(item: dict[str, JsonValue]) -> None:
    sections = item["sections"]
    if not isinstance(sections, list) or canonical_sha256(sections) != item["evidence_digest"]:
        raise StateError("doctor journal run evidence digest mismatch")
    if not isinstance(item["counts"], dict) or not isinstance(item["preservation"], dict):
        raise StateError("doctor journal run counts or preservation are invalid")


def _run_verdict(item: dict[str, JsonValue]) -> None:
    if _text(item["status"]) not in _STATUSES:
        raise StateError("doctor journal run status is invalid")
    if isinstance(item["exit_code"], bool) or item["exit_code"] not in {0, 1, 2, 3}:
        raise StateError("doctor journal run exit code is invalid")
    if not isinstance(item["service_check_verified"], bool):
        raise StateError("doctor journal run service check flag is invalid")


def _object(value: JsonValue, keys: frozenset[str]) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise StateError("doctor journal nested object has invalid keys")
    return value


def _text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        raise StateError("doctor journal text field is invalid")
    return value


def _pattern(value: JsonValue, pattern: re.Pattern[str]) -> str:
    text = _text(value)
    if pattern.fullmatch(text) is None:
        raise StateError("doctor journal identifier is invalid")
    return text
