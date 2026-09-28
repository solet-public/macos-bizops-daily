"""Closed v5 journal for an approved update (Steps 4, 5, 6 and 7).

The import journal (v1, ``maintenance_journal.py``) stays byte-compatible; the
update journal is a separate kind-specific document with its own closed status
graph.  v2 (Step 4) documents are upgraded in memory to v3 by adding
``runtime_approval=null`` and ``runtime_operations=[]`` (Step-5 design section
9.1), v3 (Step 5) documents to v4 by adding ``recovers=null``,
``source_mode="advance"``, ``retirement=null`` and a per-row ``operation_type``
(Step 6 design section 4.7), and v4 (Step 6) documents to v5 by adding the
top-level ``local_state`` object (Step 7 design section 6.7) with empty
``baseline``/``current`` snapshots and no revisions -- a v4 journal was written
under the clean-tree frontier, so "no preserved paths, no committed untracked
entries" is exactly its meaning; all are persisted at v5 on the next write.

``local_state`` carries the Step 7 commitment (section 6.6): ``baseline`` is
recorded at approval and immutable (it is in the fingerprint preimage),
``current`` is what every later check compares against (initially equal to
``baseline``) and ``revisions`` is the append-only list of how ``current`` got
there -- a per-operation re-baseline of declared targets, an operation's
preserved-surface disclosure, or the lifecycle stage's service writes.

v4 closes the graph: ``runtime_advanced`` continues into the final doctor and
promotion, ``promoted`` is the success terminal, ``abandoned`` is the
operator's pre-fast-forward exit, and a terminal ``blocked``/``failed``
document at the baseline can be *retired* (``retirement`` set, status and
result untouched).  ``source_advanced`` and ``runtime_advanced`` remain the two
nonterminal frontiers that ``_block``/``_terminal`` never terminalise.

A v3 declared row cannot be classified without its bundle, so the upgrade
stamps it ``v3_unclassified``; the executor classifies it once from the
approved bundle and the write validator admits exactly that one change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from solet_setup_contracts import canonical_sha256

from .errors import StateConflictError, StateError
from .existing_install_bundle import SYNTHESISED_OPERATION_TYPES
from .models import JsonValue, OperationType
from .state_io import atomic_write_json
from .transaction import utc_now

UPDATE_JOURNAL_SCHEMA_VERSION = 5
_V4_SCHEMA_VERSION = 4
_V3_SCHEMA_VERSION = 3
_LEGACY_SCHEMA_VERSION = 2
V3_UNCLASSIFIED = "v3_unclassified"
SOURCE_STATUSES = (
    "prepared",
    "operation_published",
    "target_fetched",
    "source_applying",
    "source_advanced",
)
RUNTIME_STATUSES = (
    "runtime_planned",
    "dependencies_applying",
    "dependencies_advanced",
    "migrations_applying",
    "migrations_advanced",
    "hydration_applying",
    "hydration_advanced",
    "lifecycle_applying",
    "lifecycle_advanced",
    "runtime_reconciling",
    "runtime_advanced",
)
DOCTOR_STATUSES = ("doctor_verifying", "doctor_verified", "doctor_incomplete", "promoting")
UPDATE_STATUSES = (*SOURCE_STATUSES, *RUNTIME_STATUSES, *DOCTOR_STATUSES, "blocked", "failed", "abandoned", "promoted")
TERMINAL_UPDATE_STATUSES = frozenset({"blocked", "failed", "abandoned", "promoted"})
FAILURE_TERMINAL_STATUSES = frozenset({"blocked", "failed"})
FRONTIER_STATUSES = frozenset({"source_advanced", "runtime_advanced"})
#: Statuses ``_terminal`` never leaves (Step 6 section 4.7, M6): read-only or
#: Manager-state-only phases where an exception says nothing about the target.
GUARDED_STATUSES = frozenset({*DOCTOR_STATUSES, *FRONTIER_STATUSES, *TERMINAL_UPDATE_STATUSES})
ABANDONABLE_STATUSES = frozenset({"prepared", "operation_published", "target_fetched", "source_applying"})
SOURCE_MODES = ("advance", "verify")
_TERMINALS = frozenset({"blocked", "failed"})
_ABANDON = frozenset({"abandoned"})
_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset({"operation_published"}) | _TERMINALS | _ABANDON,
    "operation_published": frozenset({"target_fetched"}) | _TERMINALS | _ABANDON,
    "target_fetched": frozenset({"source_applying"}) | _TERMINALS | _ABANDON,
    "source_applying": frozenset({"source_advanced"}) | _TERMINALS | _ABANDON,
    "source_advanced": frozenset({"runtime_planned"}),
    "runtime_planned": frozenset({"dependencies_applying"}) | _TERMINALS,
    "dependencies_applying": frozenset({"dependencies_advanced"}) | _TERMINALS,
    "dependencies_advanced": frozenset({"migrations_applying"}) | _TERMINALS,
    "migrations_applying": frozenset({"migrations_advanced"}) | _TERMINALS,
    "migrations_advanced": frozenset({"hydration_applying"}) | _TERMINALS,
    "hydration_applying": frozenset({"hydration_advanced"}) | _TERMINALS,
    "hydration_advanced": frozenset({"lifecycle_applying"}) | _TERMINALS,
    "lifecycle_applying": frozenset({"lifecycle_advanced"}) | _TERMINALS,
    "lifecycle_advanced": frozenset({"runtime_reconciling"}) | _TERMINALS,
    "runtime_reconciling": frozenset({"runtime_advanced"}) | _TERMINALS,
    "runtime_advanced": frozenset({"doctor_verifying"}),
    "doctor_verifying": frozenset({"doctor_verified", "doctor_incomplete"}),
    "doctor_incomplete": frozenset({"doctor_verifying"}),
    "doctor_verified": frozenset({"promoting"}),
    "promoting": frozenset({"promoted"}),
    "blocked": frozenset(),
    "failed": frozenset(),
    "abandoned": frozenset(),
    "promoted": frozenset(),
}
OPERATION_STATUSES = (
    "pending",
    "verified_by_probe",
    "applying",
    "applied",
    "verified",
    "not_applicable",
    "deferred",
    "blocked",
    "failed",
)
_V2_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "operation_id",
        "instance_id",
        "approval",
        "baseline",
        "candidate",
        "planned_actions",
        "attempts",
        "status",
        "result",
        "rollback_class",
        "created_at",
        "updated_at",
    }
)
_V3_KEYS = _V2_KEYS | {"runtime_approval", "runtime_operations"}
_V4_KEYS = _V3_KEYS | {"recovers", "source_mode", "retirement"}
_KEYS = _V4_KEYS | {"local_state"}
_LOCAL_STATE_KEYS = frozenset({"baseline", "current", "revisions"})
_LOCAL_STATE_SNAPSHOT_KEYS = frozenset({"preserved_tracked_paths", "committed_inventory", "local_state_commitment", "preserved_surface"})
_TRACKED_ENTRY_KEYS = frozenset({"path", "sha256", "size"})
_INVENTORY_ENTRY_KEYS = frozenset({"path", "kind", "mode", "size"})
_REBASELINE_REVISION_KEYS = frozenset({"operation_id", "at", "paths", "before", "after"})
_SURFACE_REVISION_KEYS = frozenset({"operation_id", "at", "preserved_surface_delta"})
_LIFECYCLE_REVISION_KEYS = frozenset({"stage", "at", "service_writes"})
_SERVICE_WRITES_KEYS = frozenset({"committed_additions", "preserved_surface_delta"})
_COMMITTED_ADDITION_KEYS = frozenset({"path", "kind", "mode", "size", "target_digest"})
_INVENTORY_KINDS = frozenset({"file", "symlink", "directory"})
#: The stage across which a ``service_writes`` revision was observed (the lifecycle restart is the designed one).
_REVISION_STAGES = frozenset({"source", "dependencies", "migrations_pre", "hydration", "lifecycle", "runtime_reconcile"})
EMPTY_LOCAL_STATE_SNAPSHOT: dict[str, JsonValue] = {"preserved_tracked_paths": [], "committed_inventory": [], "local_state_commitment": None, "preserved_surface": []}
_IMMUTABLE_KEYS = (
    "schema_version",
    "kind",
    "operation_id",
    "instance_id",
    "approval",
    "baseline",
    "candidate",
    "planned_actions",
    "rollback_class",
    "created_at",
    "recovers",
    "source_mode",
)
_ATTEMPT_KEYS = frozenset({"attempt", "stage_id", "status", "at", "note"})
_BASELINE_KEYS = frozenset({"commit", "tree", "branch"})
_CANDIDATE_KEYS = frozenset({"descriptor_digest", "commit", "tree", "tag", "receipt_digest", "contract_digest"})
_RUNTIME_APPROVAL_KEYS = frozenset({"fingerprint", "recorded_at", "planned_actions", "strategy", "forward_only_boundary"})
_RETIREMENT_KEYS = frozenset({"released_at", "head_observed", "reason"})
_OPERATION_KEYS = frozenset(
    {"operation_id", "operation_ref", "stage", "status", "idempotency_key", "mutation_class", "rollback_class", "attempts", "operation_type"}
)
_OPERATION_IMMUTABLE_KEYS = ("operation_id", "operation_ref", "stage", "idempotency_key", "mutation_class", "rollback_class")
_OPERATION_ATTEMPT_KEYS = frozenset({"attempt", "phase", "checkpoint_status", "error_kind", "evidence", "evidence_digest", "at"})
_OPERATION_PHASES = frozenset({"probe", "apply", "recover", "restore", "manager"})
_OPERATION_TYPES = frozenset({item.value for item in OperationType}) | {V3_UNCLASSIFIED}
_STRATEGIES = frozenset({"router_cutover", "single_color_restart"})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_BARE_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ENTRY_DIGEST = re.compile(r"^(?:[0-9a-f]{64}|unread)$")
_MODE = re.compile(r"^[0-7]{4}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_OPERATION_ID = re.compile(r"^opr_[0-9a-f]{32}$")
_INSTANCE_ID = re.compile(r"^ins_[0-9a-f]{32}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_OPERATION_REF = re.compile(r"^existing::[a-z][a-z0-9_.]*$")


def create_update_journal(
    *,
    operation_id: str,
    instance_id: str,
    fingerprint: str,
    baseline_commit: str,
    baseline_tree: str,
    branch: str,
    candidate_descriptor_digest: str,
    candidate_commit: str,
    candidate_tree: str,
    candidate_tag: str,
    candidate_contract_digest: str,
    receipt_digest: str,
    planned_actions: tuple[str, ...],
    source_mode: str = "advance",
    recovers: str | None = None,
    timestamp: str | None = None,
    local_state: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    now = utc_now() if timestamp is None else timestamp
    snapshot = dict(EMPTY_LOCAL_STATE_SNAPSHOT if local_state is None else local_state)
    value: dict[str, JsonValue] = {
        "schema_version": UPDATE_JOURNAL_SCHEMA_VERSION,
        "kind": "update",
        "operation_id": operation_id,
        "instance_id": instance_id,
        "approval": {"fingerprint": fingerprint, "recorded_at": now},
        "baseline": {"commit": baseline_commit, "tree": baseline_tree, "branch": branch},
        "candidate": {
            "descriptor_digest": candidate_descriptor_digest,
            "commit": candidate_commit,
            "tree": candidate_tree,
            "tag": candidate_tag,
            "receipt_digest": receipt_digest,
            "contract_digest": candidate_contract_digest,
        },
        "planned_actions": list(planned_actions),
        "attempts": [],
        "status": "prepared",
        "result": None,
        "rollback_class": "forward_only_source",
        "runtime_approval": None,
        "runtime_operations": [],
        "recovers": recovers,
        "source_mode": source_mode,
        "retirement": None,
        "local_state": {"baseline": snapshot, "current": dict(snapshot), "revisions": []},
        "created_at": now,
        "updated_at": now,
    }
    return parse_update_journal_bytes(json.dumps(value, sort_keys=True).encode())


def record_local_state_revision(
    value: dict[str, JsonValue],
    *,
    revision: dict[str, JsonValue],
    current: dict[str, JsonValue],
    timestamp: str | None = None,
) -> dict[str, JsonValue]:
    """Append one ``local_state.revisions`` row and move ``current`` (Step 7 section 6.6); the executor's only writer of it.

    ``baseline`` never moves; ``revisions`` is an immutable prefix like every other
    attempt list.  The row is one of the three closed shapes and ``current`` is a
    closed snapshot, both validated on write and read.
    """
    now = utc_now() if timestamp is None else timestamp
    validated = _validated(value)
    local_state = dict(cast(dict[str, JsonValue], validated["local_state"]))
    revisions = list(cast(list[JsonValue], local_state["revisions"]))
    row = dict(revision)
    row["at"] = now
    revisions.append(row)
    local_state["revisions"] = revisions
    local_state["current"] = dict(current)
    next_value = dict(validated)
    next_value["local_state"] = local_state
    next_value["updated_at"] = now
    return _validated(next_value)


def parse_update_journal_bytes(raw: bytes) -> dict[str, JsonValue]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("update journal is unreadable") from exc
    return _validated(_upgraded(value))


def read_update_journal(path: Path) -> dict[str, JsonValue]:
    try:
        return parse_update_journal_bytes(path.read_bytes())
    except OSError as exc:
        raise StateError("update journal is unreadable") from exc


def advance_update_journal(
    value: dict[str, JsonValue],
    *,
    status: str,
    stage_id: str,
    note: str,
    result: dict[str, JsonValue] | None = None,
    timestamp: str | None = None,
) -> dict[str, JsonValue]:
    """Append one attempt and move the closed status graph forward."""
    current = _validated(value)
    old_status = cast(str, current["status"])
    if status not in _TRANSITIONS[old_status]:
        raise StateConflictError(f"update journal transition {old_status} -> {status} is illegal")
    now = utc_now() if timestamp is None else timestamp
    attempts = list(cast(list[JsonValue], current["attempts"]))
    attempts.append({"attempt": len(attempts), "stage_id": stage_id, "status": status, "at": now, "note": note})
    next_value = dict(current)
    next_value["attempts"] = attempts
    next_value["status"] = status
    next_value["result"] = result
    next_value["updated_at"] = now
    return _validated(next_value)


def retire_update_journal(value: dict[str, JsonValue], *, head_observed: str, reason: str, timestamp: str | None = None) -> dict[str, JsonValue]:
    """Record the retirement of a terminal ``blocked``/``failed`` document (Step 6 section 4.3).

    Status, result and attempts are untouched; the only change is the
    immutable ``retirement`` record, which the pointer release cites as proof.
    """
    current = _validated(value)
    if current["status"] not in FAILURE_TERMINAL_STATUSES:
        raise StateConflictError("only a terminal blocked or failed update journal can be retired")
    if current["retirement"] is not None:
        raise StateConflictError("update journal is already retired")
    now = utc_now() if timestamp is None else timestamp
    next_value = dict(current)
    next_value["retirement"] = {"released_at": now, "head_observed": head_observed, "reason": reason}
    next_value["updated_at"] = now
    return _validated(next_value)


def is_retired(value: dict[str, JsonValue]) -> bool:
    return value["retirement"] is not None


def record_runtime_approval(
    value: dict[str, JsonValue],
    *,
    fingerprint: str,
    planned_actions: tuple[str, ...],
    strategy: str,
    forward_only_boundary: str | None,
    operations: tuple[dict[str, JsonValue], ...],
    note: str,
    timestamp: str | None = None,
) -> dict[str, JsonValue]:
    """Record the runtime approval and its operation rows while moving to ``runtime_planned``.

    This is the one write that creates ``runtime_operations``; every later
    change to those rows is an appended attempt.  It is only legal from
    ``source_advanced`` with no prior runtime approval.
    """
    current = _validated(value)
    if current["runtime_approval"] is not None:
        raise StateConflictError("update journal already carries a runtime approval")
    now = utc_now() if timestamp is None else timestamp
    rows = [
        {
            "operation_id": row["operation_id"],
            "operation_ref": row["operation_ref"],
            "stage": row["stage"],
            "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "mutation_class": row["mutation_class"],
            "rollback_class": row["rollback_class"],
            "operation_type": row["operation_type"],
            "attempts": [],
        }
        for row in operations
    ]
    old_status = cast(str, current["status"])
    if "runtime_planned" not in _TRANSITIONS[old_status]:
        raise StateConflictError(f"update journal transition {old_status} -> runtime_planned is illegal")
    attempts = list(cast(list[JsonValue], current["attempts"]))
    attempts.append({"attempt": len(attempts), "stage_id": "runtime_planned", "status": "runtime_planned", "at": now, "note": note})
    next_value = dict(current)
    next_value["runtime_approval"] = {
        "fingerprint": fingerprint,
        "recorded_at": now,
        "planned_actions": list(planned_actions),
        "strategy": strategy,
        "forward_only_boundary": forward_only_boundary,
    }
    next_value["runtime_operations"] = cast(list[JsonValue], rows)
    next_value["attempts"] = attempts
    next_value["status"] = "runtime_planned"
    next_value["result"] = None
    next_value["updated_at"] = now
    return _validated(next_value)


def record_operation_attempt(
    value: dict[str, JsonValue],
    operation_id: str,
    *,
    phase: str,
    checkpoint_status: str,
    status: str,
    evidence: dict[str, JsonValue],
    error_kind: str | None = None,
    timestamp: str | None = None,
) -> dict[str, JsonValue]:
    """Append one per-operation attempt with its evidence and move the row's status."""
    current = _validated(value)
    now = utc_now() if timestamp is None else timestamp
    rows = [dict(cast(dict[str, JsonValue], row)) for row in cast(list[JsonValue], current["runtime_operations"])]
    row = next((item for item in rows if item["operation_id"] == operation_id), None)
    if row is None:
        raise StateConflictError(f"update journal has no runtime operation {operation_id!r}")
    attempts = list(cast(list[JsonValue], row["attempts"]))
    attempts.append(
        {
            "attempt": len(attempts),
            "phase": phase,
            "checkpoint_status": checkpoint_status,
            "error_kind": error_kind,
            "evidence": evidence,
            "evidence_digest": canonical_sha256(evidence),
            "at": now,
        }
    )
    row["attempts"] = attempts
    row["status"] = status
    next_value = dict(current)
    next_value["runtime_operations"] = cast(list[JsonValue], rows)
    next_value["updated_at"] = now
    return _validated(next_value)


def classify_v3_rows(value: dict[str, JsonValue], types: dict[str, str]) -> dict[str, JsonValue]:
    """Stamp ``operation_type`` onto every ``v3_unclassified`` row once, from the approved bundle."""
    rows = [dict(cast(dict[str, JsonValue], row)) for row in cast(list[JsonValue], value["runtime_operations"])]
    changed = False
    for row in rows:
        if row["operation_type"] != V3_UNCLASSIFIED:
            continue
        operation_type = types.get(cast(str, row["operation_id"]))
        if operation_type is None:
            raise StateError(f"v3 runtime operation {row['operation_id']!r} cannot be classified from the approved bundle")
        row["operation_type"] = operation_type
        changed = True
    if not changed:
        return value
    next_value = dict(value)
    next_value["runtime_operations"] = cast(list[JsonValue], rows)
    return _validated(next_value)


def operation_row(value: dict[str, JsonValue], operation_id: str) -> dict[str, JsonValue] | None:
    for row in cast(list[JsonValue], value["runtime_operations"]):
        item = cast(dict[str, JsonValue], row)
        if item["operation_id"] == operation_id:
            return item
    return None


def write_update_journal(
    path: Path, previous: dict[str, JsonValue] | None, next_value: dict[str, JsonValue]
) -> None:
    """Atomically persist an identity-preserving, attempt-prefix-preserving transition."""
    document = _validated(next_value)
    if previous is None:
        if document["status"] != "prepared" or document["attempts"]:
            raise StateConflictError("update journal must begin prepared with no attempts")
    else:
        _validate_transition(_validated(previous), document)
    atomic_write_json(path, document)
    if read_update_journal(path) != document:
        raise StateError("update journal read-back mismatch")


def _validate_transition(previous: dict[str, JsonValue], next_value: dict[str, JsonValue]) -> None:
    for key in _IMMUTABLE_KEYS:
        if previous[key] != next_value[key]:
            raise StateConflictError("update journal immutable identity changed")
    old_attempts = cast(list[JsonValue], previous["attempts"])
    new_attempts = cast(list[JsonValue], next_value["attempts"])
    if new_attempts[: len(old_attempts)] != old_attempts or len(new_attempts) - len(old_attempts) not in {0, 1}:
        raise StateConflictError("update journal attempts are not an immutable prefix")
    old_status, new_status = cast(str, previous["status"]), cast(str, next_value["status"])
    if old_status != new_status and new_status not in _TRANSITIONS[old_status]:
        raise StateConflictError("update journal transition is illegal")
    _validate_write_once(previous, next_value)
    _validate_local_state_transition(cast(dict[str, JsonValue], previous["local_state"]), cast(dict[str, JsonValue], next_value["local_state"]))
    _validate_operation_rows_transition(
        cast(list[JsonValue], previous["runtime_operations"]), cast(list[JsonValue], next_value["runtime_operations"])
    )


def _validate_write_once(previous: dict[str, JsonValue], next_value: dict[str, JsonValue]) -> None:
    """``runtime_approval`` and ``retirement`` are immutable once non-null."""
    for key, label in (("runtime_approval", "runtime approval"), ("retirement", "retirement")):
        if previous[key] is not None and previous[key] != next_value[key]:
            raise StateConflictError(f"update journal {label} is immutable once recorded")


def _validate_local_state_transition(previous: dict[str, JsonValue], next_value: dict[str, JsonValue]) -> None:
    """``baseline`` is immutable and ``revisions`` an immutable prefix; ``current`` moves only with a new revision."""
    if previous["baseline"] != next_value["baseline"]:
        raise StateConflictError("update journal local-state baseline is immutable")
    old_revisions = cast(list[JsonValue], previous["revisions"])
    new_revisions = cast(list[JsonValue], next_value["revisions"])
    if new_revisions[: len(old_revisions)] != old_revisions or len(new_revisions) - len(old_revisions) not in {0, 1}:
        raise StateConflictError("update journal local-state revisions are not an immutable prefix")
    if previous["current"] != next_value["current"] and len(new_revisions) == len(old_revisions):
        raise StateConflictError("update journal local-state current moved without a revision")


def _validate_operation_rows_transition(old_rows: list[JsonValue], new_rows: list[JsonValue]) -> None:
    if not old_rows:
        return
    if len(old_rows) != len(new_rows):
        raise StateConflictError("update journal runtime operations cannot be added or removed after approval")
    for old_raw, new_raw in zip(old_rows, new_rows, strict=True):
        old, new = cast(dict[str, JsonValue], old_raw), cast(dict[str, JsonValue], new_raw)
        for key in _OPERATION_IMMUTABLE_KEYS:
            if old[key] != new[key]:
                raise StateConflictError("update journal runtime operation identity changed")
        if old["operation_type"] != new["operation_type"] and old["operation_type"] != V3_UNCLASSIFIED:
            raise StateConflictError("update journal runtime operation type is immutable once classified")
        old_attempts = cast(list[JsonValue], old["attempts"])
        new_attempts = cast(list[JsonValue], new["attempts"])
        if new_attempts[: len(old_attempts)] != old_attempts:
            raise StateConflictError("update journal runtime operation attempts are not an immutable prefix")


def _upgraded(raw: object) -> object:
    """Upgrade a v2, v3 or v4 document to v5 in memory; every other shape passes through untouched."""
    if not isinstance(raw, dict):
        return raw
    keys = frozenset(cast(dict[str, object], raw))
    value = cast(dict[str, JsonValue], raw)
    if keys == _V2_KEYS and value.get("schema_version") == _LEGACY_SCHEMA_VERSION:
        upgraded = dict(value)
        upgraded["runtime_approval"] = None
        upgraded["runtime_operations"] = []
        return _upgraded_v4(_upgraded_v3(upgraded))
    if keys == _V3_KEYS and value.get("schema_version") == _V3_SCHEMA_VERSION:
        return _upgraded_v4(_upgraded_v3(dict(value)))
    if keys == _V4_KEYS and value.get("schema_version") == _V4_SCHEMA_VERSION:
        return _upgraded_v4(dict(value))
    return raw


def _upgraded_v4(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """v4 -> v5: the ``local_state`` object with empty snapshots (the clean-tree frontier's exact meaning, CH-43)."""
    value["schema_version"] = UPDATE_JOURNAL_SCHEMA_VERSION
    value["local_state"] = {"baseline": dict(EMPTY_LOCAL_STATE_SNAPSHOT), "current": dict(EMPTY_LOCAL_STATE_SNAPSHOT), "revisions": []}
    return value


def _upgraded_v3(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """v3 -> v4: the three new keys and a per-row ``operation_type`` (synthesised rows by ref)."""
    value["schema_version"] = _V4_SCHEMA_VERSION
    value["recovers"] = None
    value["source_mode"] = "advance"
    value["retirement"] = None
    rows: list[JsonValue] = []
    for raw in cast(list[JsonValue], value.get("runtime_operations", [])):
        if not isinstance(raw, dict):
            rows.append(raw)
            continue
        row = dict(cast(dict[str, JsonValue], raw))
        if "operation_type" not in row:
            synthesised = SYNTHESISED_OPERATION_TYPES.get(cast(str, row.get("operation_ref", "")))
            row["operation_type"] = V3_UNCLASSIFIED if synthesised is None else synthesised.value
        rows.append(row)
    value["runtime_operations"] = rows
    return value


def _validated(raw: object) -> dict[str, JsonValue]:
    if not isinstance(raw, dict) or frozenset(raw) != _KEYS:
        raise StateError("update journal does not match the closed v5 shape")
    value = cast(dict[str, JsonValue], raw)
    if value["schema_version"] != UPDATE_JOURNAL_SCHEMA_VERSION or value["kind"] != "update":
        raise StateError("update journal does not match the closed v5 shape")
    if value["rollback_class"] != "forward_only_source":
        raise StateError("update journal rollback class is invalid")
    operation_id = _identifier(value["operation_id"], _OPERATION_ID)
    _identifier(value["instance_id"], _INSTANCE_ID)
    status = _one_of(value["status"], UPDATE_STATUSES)
    _approval(value["approval"])
    _baseline(value["baseline"])
    _candidate(value["candidate"])
    _source_mode(value["source_mode"], value["baseline"], value["candidate"])
    _recovers(value["recovers"], operation_id)
    _retirement(status, value["retirement"])
    _actions(value["planned_actions"])
    _attempts(value["attempts"])
    _result(status, value["result"])
    _runtime_approval(status, value["runtime_approval"], value["runtime_operations"])
    _local_state(value["local_state"])
    _text(value["created_at"])
    _text(value["updated_at"])
    return cast(dict[str, JsonValue], json.loads(json.dumps(value, sort_keys=True)))


def _local_state(value: JsonValue) -> None:
    item = _object(value, _LOCAL_STATE_KEYS)
    _local_state_snapshot(item["baseline"])
    _local_state_snapshot(item["current"])
    revisions = item["revisions"]
    if not isinstance(revisions, list):
        raise StateError("update journal local-state revisions are invalid")
    for row in revisions:
        _local_state_revision(row)


def _local_state_snapshot(value: JsonValue) -> None:
    item = _object(value, _LOCAL_STATE_SNAPSHOT_KEYS)
    tracked = item["preserved_tracked_paths"]
    if not isinstance(tracked, list):
        raise StateError("update journal preserved tracked paths are invalid")
    for raw in tracked:
        row = _object(raw, _TRACKED_ENTRY_KEYS)
        _text(row["path"])
        _identifier(row["sha256"], _BARE_DIGEST)
        _size(row["size"])
    for key in ("committed_inventory", "preserved_surface"):
        rows = item[key]
        if not isinstance(rows, list):
            raise StateError(f"update journal {key} is invalid")
        for raw in rows:
            _inventory_entry(raw)
    commitment = item["local_state_commitment"]
    if commitment is not None:
        _identifier(commitment, _DIGEST)
    if commitment is None and item["committed_inventory"]:
        raise StateError("update journal local-state commitment is null with a non-empty committed inventory")


def _inventory_entry(raw: JsonValue) -> None:
    row = _object(raw, _INVENTORY_ENTRY_KEYS)
    _text(row["path"])
    if _text(row["kind"]) not in _INVENTORY_KINDS:
        raise StateError("update journal inventory kind is invalid")
    _identifier(row["mode"], _MODE)
    _size(row["size"])


def _size(value: JsonValue) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise StateError("update journal size field is invalid")


def _local_state_revision(raw: JsonValue) -> None:
    if not isinstance(raw, dict):
        raise StateError("update journal local-state revision is invalid")
    keys = frozenset(cast(dict[str, JsonValue], raw))
    row = cast(dict[str, JsonValue], raw)
    if "at" not in row:
        raise StateError("update journal local-state revision lacks its timestamp")
    _text(row["at"])
    if keys == _REBASELINE_REVISION_KEYS:
        _identifier(row["operation_id"], _IDENTIFIER)
        _digest_map(row["before"])
        _digest_map(row["after"])
        _path_list(row["paths"])
        return
    if keys == _SURFACE_REVISION_KEYS:
        _identifier(row["operation_id"], _IDENTIFIER)
        _path_list(row["preserved_surface_delta"])
        return
    if keys == _LIFECYCLE_REVISION_KEYS:
        if row["stage"] not in _REVISION_STAGES:
            raise StateError("update journal service-writes revision names an unknown stage")
        writes = _object(row["service_writes"], _SERVICE_WRITES_KEYS)
        _path_list(writes["preserved_surface_delta"])
        additions = writes["committed_additions"]
        if not isinstance(additions, list):
            raise StateError("update journal service writes are invalid")
        for item in additions:
            addition = _object(item, _COMMITTED_ADDITION_KEYS)
            _inventory_entry({key: addition[key] for key in _INVENTORY_ENTRY_KEYS})
            _identifier(addition["target_digest"], _BARE_DIGEST)
        return
    raise StateError("update journal local-state revision is outside the closed shapes")


def _digest_map(value: JsonValue) -> None:
    if not isinstance(value, dict):
        raise StateError("update journal revision digest map is invalid")
    for path, digest in cast(dict[str, JsonValue], value).items():
        _text(path)
        if digest is not None:
            _identifier(digest, _ENTRY_DIGEST)


def _path_list(value: JsonValue) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise StateError("update journal revision path list is invalid")


def _approval(value: JsonValue) -> None:
    item = _object(value, frozenset({"fingerprint", "recorded_at"}))
    _identifier(item["fingerprint"], _DIGEST)
    _text(item["recorded_at"])


def _baseline(value: JsonValue) -> None:
    item = _object(value, _BASELINE_KEYS)
    _identifier(item["commit"], _COMMIT)
    _identifier(item["tree"], _COMMIT)
    _text(item["branch"])


def _candidate(value: JsonValue) -> None:
    item = _object(value, _CANDIDATE_KEYS)
    _identifier(item["descriptor_digest"], _DIGEST)
    _identifier(item["commit"], _COMMIT)
    _identifier(item["tree"], _COMMIT)
    _text(item["tag"])
    _identifier(item["receipt_digest"], _DIGEST)
    _identifier(item["contract_digest"], _DIGEST)
    if item["descriptor_digest"] == item["contract_digest"]:
        raise StateError("update journal conflates descriptor and transition-contract identities")


def _source_mode(value: JsonValue, baseline: JsonValue, candidate: JsonValue) -> None:
    mode = _one_of(value, SOURCE_MODES)
    same = cast(dict[str, JsonValue], baseline)["commit"] == cast(dict[str, JsonValue], candidate)["commit"]
    if mode == "verify" and not same:
        raise StateError("a verify-mode update journal must name the candidate as its baseline")
    if mode == "advance" and same:
        raise StateError("an advance-mode update journal cannot already sit at its candidate")


def _recovers(value: JsonValue, operation_id: str) -> None:
    if value is None:
        return
    if _identifier(value, _OPERATION_ID) == operation_id:
        raise StateError("update journal cannot recover itself")


def _retirement(status: str, value: JsonValue) -> None:
    if value is None:
        return
    if status not in FAILURE_TERMINAL_STATUSES:
        raise StateError("update journal retirement is legal only on a terminal blocked or failed document")
    item = _object(value, _RETIREMENT_KEYS)
    _text(item["released_at"])
    _identifier(item["head_observed"], _COMMIT)
    _text(item["reason"])


def _actions(value: JsonValue) -> None:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise StateError("update journal has invalid approved plan")


def _attempts(value: JsonValue) -> None:
    if not isinstance(value, list):
        raise StateError("update journal attempts are invalid")
    for index, raw in enumerate(value):
        item = _object(raw, _ATTEMPT_KEYS)
        if item["attempt"] != index or isinstance(item["attempt"], bool):
            raise StateError("update journal attempt numbering is invalid")
        _text(item["stage_id"])
        _one_of(item["status"], UPDATE_STATUSES)
        _text(item["at"])
        if not isinstance(item["note"], str):
            raise StateError("update journal attempt note is invalid")


def _result(status: str, value: JsonValue) -> None:
    if status not in TERMINAL_UPDATE_STATUSES:
        if value is not None:
            raise StateError("nonterminal update journal has a result")
        return
    item = _object(value, frozenset({"kind", "reason_code", "repair"}))
    if item["kind"] != status:
        raise StateError("update journal terminal result kind is invalid")
    _text(item["reason_code"])
    _text(item["repair"])


def _runtime_approval(status: str, value: JsonValue, operations: JsonValue) -> None:
    if not isinstance(operations, list):
        raise StateError("update journal runtime operations are invalid")
    if value is None:
        _require_no_runtime_approval(status, operations)
        return
    if status in SOURCE_STATUSES or status == "abandoned":
        raise StateError("a runtime approval cannot precede source_advanced")
    _approval_shape(_object(value, _RUNTIME_APPROVAL_KEYS))
    ids = [_operation(row) for row in operations]
    if len(ids) != len(set(ids)):
        raise StateError("update journal runtime operations are not unique")


def _require_no_runtime_approval(status: str, operations: list[JsonValue]) -> None:
    if status in RUNTIME_STATUSES or status in DOCTOR_STATUSES or status == "promoted":
        raise StateError("a Step-5 or Step 6 journal status requires a recorded runtime approval")
    if operations:
        raise StateError("runtime operations require a recorded runtime approval")


def _approval_shape(item: dict[str, JsonValue]) -> None:
    _identifier(item["fingerprint"], _DIGEST)
    _text(item["recorded_at"])
    actions = item["planned_actions"]
    if not isinstance(actions, list) or not all(isinstance(entry, str) and entry for entry in actions):
        raise StateError("update journal runtime approval planned actions are invalid")
    _one_of(item["strategy"], tuple(sorted(_STRATEGIES)))
    boundary = item["forward_only_boundary"]
    if boundary is not None:
        _identifier(boundary, _IDENTIFIER)


def _operation(raw: JsonValue) -> str:
    row = _object(raw, _OPERATION_KEYS)
    operation_id = _identifier(row["operation_id"], _IDENTIFIER)
    _identifier(row["operation_ref"], _OPERATION_REF)
    _text(row["stage"])
    _one_of(row["status"], OPERATION_STATUSES)
    _text(row["idempotency_key"])
    _text(row["mutation_class"])
    _text(row["rollback_class"])
    if _text(row["operation_type"]) not in _OPERATION_TYPES:
        raise StateError("update journal runtime operation type is outside the closed vocabulary")
    _operation_attempts(row["attempts"])
    return operation_id


def _operation_attempts(attempts: JsonValue) -> None:
    if not isinstance(attempts, list):
        raise StateError("update journal runtime operation attempts are invalid")
    for index, item_raw in enumerate(attempts):
        item = _object(item_raw, _OPERATION_ATTEMPT_KEYS)
        if item["attempt"] != index or isinstance(item["attempt"], bool):
            raise StateError("update journal runtime operation attempt numbering is invalid")
        if item["phase"] not in _OPERATION_PHASES:
            raise StateError("update journal runtime operation attempt phase is invalid")
        _text(item["checkpoint_status"])
        if item["error_kind"] is not None:
            _text(item["error_kind"])
        evidence = item["evidence"]
        if not isinstance(evidence, dict):
            raise StateError("update journal runtime operation attempt evidence is invalid")
        if canonical_sha256(evidence) != item["evidence_digest"]:
            raise StateError("update journal runtime operation attempt evidence digest mismatch")
        _text(item["at"])


def _object(value: JsonValue, keys: frozenset[str]) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise StateError("update journal nested object has invalid keys")
    return value


def _text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        raise StateError("update journal text field is invalid")
    return value


def _identifier(value: JsonValue, pattern: re.Pattern[str]) -> str:
    text = _text(value)
    if pattern.fullmatch(text) is None:
        raise StateError("update journal identifier is invalid")
    return text


def _one_of(value: JsonValue, allowed: tuple[str, ...]) -> str:
    text = _text(value)
    if text not in allowed:
        raise StateError("update journal status is invalid")
    return text
