"""``solet-manager reconcile <name>``: the one named answer to a terminal update (Step 6 design sections 4.3-4.4).

Four mutually exclusive forms:

- ``--dry-run`` (read-only): the recovery plan for a terminal ``blocked``/
  ``failed`` journal at the candidate -- the recorded reason, HEAD versus
  baseline/candidate, the inventory axes, every runtime-operation row with a
  fresh postcondition probe and its section-4.2 classification, the successor
  id it would mint, and a fingerprint over all of it; exit 3 unless HEAD is
  the candidate; never writes.
- ``--yes --approval-fingerprint``: mints the successor journal (``recovers``
  set, ``source_mode=verify``), swaps the pointer with ``replace_active_update``
  and brings it to ``source_advanced`` through the zero-delta path; the runtime
  plan is then previewed and approved with ``update`` exactly as in Step 5.
- ``--abandon --yes``: a nonterminal journal before the fast-forward goes
  ``abandoned``; a terminal journal at the baseline is *retired*; both release
  the pointer.  Refused with the exact reason past the fast-forward.  The
  private candidate ref is never deleted (D9).
- ``--release-pointer --yes``: the idempotent pointer repair ``doctor`` only
  reports.

Recovery is forward: no form here invokes ``reset``, ``checkout --``,
``restore``, ``stash``, ``rebase`` or a reverse commit.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from .errors import AbandonRefusedError, ApprovalFingerprintMalformedError, ApprovalFingerprintRequiredError, ProbeDriftError, StateError, UpdateBlockedError
from .maintenance_inventory import replace_active_update
from .models import CommandResult, ExitCode, InstanceInventoryRecordV2, JsonValue, MaintenanceOperationKind, OperationType
from .state_io import instance_lock
from .transaction import utc_now
from .update_execution import (
    _FINGERPRINT,
    REPAIR_RECONCILE_ABANDON,
    UpdateProbe,
    UpdateRequest,
    _candidate_execution,
    _Execution,
    _new_journal,
    dirty_tracked_paths,
    load_update_record,
    persist_contract_copies,
    probe_update,
    target_head,
)
from .update_journal import (
    ABANDONABLE_STATUSES,
    FAILURE_TERMINAL_STATUSES,
    advance_update_journal,
    is_retired,
    read_update_journal,
    retire_update_journal,
    write_update_journal,
)
from .update_pointer_repair import release_terminal_pointer, terminal_proof
from .update_runtime_plan import build_runtime_plan

__all__ = ["RESULT_KIND", "abandon_update", "preview_reconcile", "release_pointer", "reconcile_update"]

RESULT_KIND = "existing_install_reconcile"
_REAPPLY_TYPES = frozenset({OperationType.CLOSURE_REPAIR.value, OperationType.BACKED_UP_ARTIFACT.value, OperationType.ADDITIVE_PLATFORM_MIGRATION.value, OperationType.KNOWLEDGE_REINSTALL.value, OperationType.PLUGIN_CACHE_REFRESH.value})
_CONFIRM_TYPES = frozenset({OperationType.MANUAL_TARGET_MIGRATION.value, OperationType.MANUAL_ADDITIVE_PLATFORM_MIGRATION.value})


def preview_reconcile(request: UpdateRequest) -> CommandResult:
    """Read-only successor plan (``--dry-run``)."""
    paths = request.manager_paths
    record = load_update_record(request)
    journal, status = _active_journal(request, record)
    head = target_head(record)
    data = _base_data(record, journal, head)
    if status == "doctor_incomplete":
        return CommandResult(RESULT_KIND, "doctor_incomplete", "The active update needs no successor: re-run --yes with the recorded runtime fingerprint to rerun the final doctor.", ExitCode.HUMAN_ACTION, "doctor_incomplete", f"Run `solet-manager update {record.name} --yes --approval-fingerprint <runtime fingerprint>`.", data=data)
    if status not in FAILURE_TERMINAL_STATUSES:
        raise UpdateBlockedError("update_not_terminal", f"the active update is {status}, not terminal", repair=f"Resume it with `solet-manager update {record.name} --yes --approval-fingerprint <fingerprint>`.")
    if head != cast(dict[str, JsonValue], journal["candidate"])["commit"]:
        return _pre_candidate_preview(record, journal, head, data)
    execution = _candidate_execution(request, record, paths.operation_path(record.instance_id, cast(str, journal["operation_id"])), journal, cast(str, cast(dict[str, JsonValue], journal["approval"])["fingerprint"]))
    data["operation_rows"] = _classified_rows(execution, journal)
    probe = probe_update(request, record, recovers=cast(str, journal["operation_id"]))
    data["successor"] = {"operation_id": probe.operation_id, "source_mode": probe.source_mode, "planned_actions": list(probe.planned_actions), "recovers": journal["operation_id"]}
    data["approval_fingerprint"] = probe.fingerprint
    if journal["runtime_approval"] is not None and cast(dict[str, JsonValue], journal["runtime_approval"])["forward_only_boundary"] is not None:
        data["forward_only"] = {"boundary": cast(dict[str, JsonValue], journal["runtime_approval"])["forward_only_boundary"], "requires_new_backup_checkpoint": True, "router_previous_is_code_only": True}
    status_text = "successor_preview_ready" if probe.fingerprint is not None else "awaiting_user"
    return CommandResult(RESULT_KIND, status_text, "Successor operation planned; approve with --yes --approval-fingerprint. The router's previous release is a code-only rollback.", ExitCode.OK if probe.fingerprint else ExitCode.HUMAN_ACTION, None if probe.fingerprint else "successor_blocked", None if probe.fingerprint else "Resolve every listed reason, then preview again.", data=data)


def _pre_candidate_preview(record: InstanceInventoryRecordV2, journal: dict[str, JsonValue], head: str, data: dict[str, JsonValue]) -> CommandResult:
    if head == cast(dict[str, JsonValue], journal["baseline"])["commit"]:
        return CommandResult(RESULT_KIND, "retire_available", "The terminal update never moved the branch; retire it and start a fresh update.", ExitCode.HUMAN_ACTION, "head_at_baseline", f"Run `solet-manager reconcile {record.name} --abandon --yes`.", data=data)
    return CommandResult(RESULT_KIND, "head_elsewhere", "HEAD is neither the baseline nor the candidate; the Manager plans nothing.", ExitCode.HUMAN_ACTION, "managed_identity_drift", f"Inspect the target with `solet-manager doctor {record.name}`; do not reset.", data=data)


def _classified_rows(execution: _Execution, journal: dict[str, JsonValue]) -> list[JsonValue]:
    """Every runtime-operation row with a fresh postcondition probe and its section-4.2 classification."""
    rows: list[JsonValue] = []
    plan = None
    if journal["runtime_approval"] is not None:
        try:
            plan = build_runtime_plan(execution.plan_context("completion"))
        except UpdateBlockedError:
            plan = None
    now = {} if plan is None else {item.operation_id: item.postcondition_now for item in plan.operations}
    for raw in cast(list[JsonValue], journal["runtime_operations"]):
        row = cast(dict[str, JsonValue], raw)
        operation_type = cast(str, row["operation_type"])
        fresh = now.get(cast(str, row["operation_id"]), "unprobed")
        rows.append({"operation_id": row["operation_id"], "operation_type": operation_type, "journaled_status": row["status"], "postcondition_now": fresh, "classification": _classification(operation_type, fresh)})
    return rows


def _classification(operation_type: str, postcondition_now: str) -> str:
    if postcondition_now == "verified":
        return "verified_by_probe"
    if operation_type in _REAPPLY_TYPES:
        return "will_reapply"
    if operation_type in _CONFIRM_TYPES:
        return "operator_confirmation"
    if operation_type == OperationType.FORWARD_ONLY_MIGRATION.value:
        return "operator_confirmation_with_new_checkpoint"
    return "operator_action_required"


def reconcile_update(request: UpdateRequest, approved: str | None) -> CommandResult:
    """``--yes``: mint the successor under the lock and bring it to ``source_advanced`` (section 4.4)."""
    if approved is None:
        raise ApprovalFingerprintRequiredError("--yes requires --approval-fingerprint")
    if _FINGERPRINT.fullmatch(approved) is None:
        raise ApprovalFingerprintMalformedError("approval fingerprint must be sha256:<64 hex>")
    paths = request.manager_paths
    with instance_lock(paths.lock_path(request.name), create=True):
        record = load_update_record(request)
        record = release_terminal_pointer(paths, record) or record
        old, status = _active_journal(request, record)
        if status not in FAILURE_TERMINAL_STATUSES:
            raise UpdateBlockedError("update_not_terminal", f"the active update is {status}, not terminal", repair=f"Resume it with `solet-manager update {record.name} --yes --approval-fingerprint <fingerprint>`.")
        old_id = cast(str, old["operation_id"])
        if target_head(record) != cast(dict[str, JsonValue], old["candidate"])["commit"]:
            raise UpdateBlockedError("head_not_at_candidate", "a successor requires HEAD at the candidate", repair=f"Run `solet-manager reconcile {record.name} --dry-run` for the available recovery.")
        probe = probe_update(request, record, recovers=old_id)
        if probe.fingerprint is None or probe.fingerprint != approved:
            raise ProbeDriftError("approved fingerprint does not match the lock-time successor preview", repair="Run `reconcile --dry-run` again and approve the fingerprint it renders.")
        persist_contract_copies(paths, probe.descriptor, probe.candidate)
        journal = _successor_journal(probe, approved, old_id)
        with instance_lock(paths.registry_lock_path, create=True):
            record = replace_active_update(paths.maintenance_inventory_path, record, old_operation_id=old_id, old_status=status, new_operation_id=probe.operation_id, now=utc_now())
        result = _Execution(request, record, probe.descriptor, probe.candidate, probe.journal_path, journal, approved).run_verify()
        data = dict(result.data)
        data["recovers"] = old_id
        data["retained"] = {"old_journal": str(paths.operation_path(record.instance_id, old_id)), "backups_retained": True}
        return CommandResult(RESULT_KIND, "source_advanced", f"Successor {probe.operation_id} recovers {old_id}; runtime plan: `solet-manager update {record.name} --dry-run`.", ExitCode.OK, data=data)


def _successor_journal(probe: UpdateProbe, approved: str, old_id: str) -> dict[str, JsonValue]:
    """The successor's ``prepared`` journal: read back if a crash already wrote it, else written now."""
    if probe.journal_path.exists():
        journal = read_update_journal(probe.journal_path)
        if journal["recovers"] != old_id or journal["source_mode"] != "verify":
            raise StateError("an existing successor journal does not bind the operation it recovers")
        return journal
    journal = _new_journal(probe, approved)
    write_update_journal(probe.journal_path, None, journal)
    return journal


def abandon_update(request: UpdateRequest) -> CommandResult:
    """``--abandon --yes``: abandon a nonterminal, or retire a terminal, operation whose HEAD is still the baseline (section 4.3)."""
    paths = request.manager_paths
    with instance_lock(paths.lock_path(request.name), create=True):
        record = load_update_record(request)
        journal, status = _orphan_prepared(request, record) if record.active_operation is None else _active_journal(request, record)
        head = target_head(record)
        baseline = cast(str, cast(dict[str, JsonValue], journal["baseline"])["commit"])
        candidate_ref = "refs/solet/candidates/" + cast(str, cast(dict[str, JsonValue], journal["candidate"])["descriptor_digest"]).removeprefix("sha256:")
        if journal["source_mode"] == "verify" or head != baseline:
            raise AbandonRefusedError(f"the update ({status}) is past the fast-forward boundary; recovery is forward only", repair=f"Run `solet-manager reconcile {record.name} --dry-run`.")
        path = paths.operation_path(record.instance_id, cast(str, journal["operation_id"]))
        dirty_paths = dirty_tracked_paths(Path(record.target.canonical_path))
        dirty = bool(dirty_paths)
        if status in ABANDONABLE_STATUSES:
            result: dict[str, JsonValue] = {"kind": "abandoned", "reason_code": "operator_abandon", "repair": "No target byte changed."}
            next_value = advance_update_journal(journal, status="abandoned", stage_id="abandoned", note=f"operator abandon at {status}; head={head}", result=result)
            outcome = "abandoned"
        elif status in FAILURE_TERMINAL_STATUSES and not is_retired(journal):
            next_value = retire_update_journal(journal, head_observed=head, reason="operator_abandon")
            outcome = "retired"
        elif is_retired(journal):
            next_value, outcome = journal, "already_retired"
        else:
            raise AbandonRefusedError(f"the update is {status}; nothing to abandon", repair=f"Run `solet-manager reconcile {record.name} --release-pointer --yes`.")
        if next_value is not journal:
            write_update_journal(path, journal, next_value)
        released = release_terminal_pointer(paths, record)
        data: dict[str, JsonValue] = {"instance_id": record.instance_id, "operation_id": journal["operation_id"], "journal_status": next_value["status"], "outcome": outcome, "head_observed": head, "retirement": next_value["retirement"], "pointer_released": released is not None, "private_candidate_ref_retained": candidate_ref, "tracked_tree_dirty": dirty, "dirty_tracked_paths": list(dirty_paths), "target_byte_writes": 0}
        message = f"{outcome}: no target byte changed; the private candidate ref {candidate_ref} remains (the Manager deletes nothing)."
        if dirty:
            message += " The tracked tree carries local state; the Manager never discards it."
        return CommandResult(RESULT_KIND, outcome, message, ExitCode.OK, data=data)


def release_pointer(request: UpdateRequest) -> CommandResult:
    """``--release-pointer --yes``: exactly the pointer repair doctor only reports (sections 3.1, 3.5)."""
    paths = request.manager_paths
    with instance_lock(paths.lock_path(request.name), create=True):
        record = load_update_record(request)
        if record.active_operation is None:
            return CommandResult(RESULT_KIND, "no_pointer", "No active operation pointer is set.", ExitCode.OK, data={"instance_id": record.instance_id, "pointer_released": False})
        proof = terminal_proof(paths, record)
        if proof is None or not proof.releasable:
            found = "none" if proof is None else f"{proof.kind.value} {proof.operation_id} at {proof.status}{' (retired)' if proof.retired else ''}"
            raise UpdateBlockedError("pointer_not_releasable", f"the active pointer names an operation that is not over: {found}", repair=f"Resume it with `solet-manager update {record.name} --yes --approval-fingerprint <fingerprint>`, or retire it: {REPAIR_RECONCILE_ABANDON.format(name=record.name)}")
        released = release_terminal_pointer(paths, record)
        return CommandResult(RESULT_KIND, "pointer_released", f"Released the pointer naming {proof.kind.value} {proof.operation_id} ({proof.status}).", ExitCode.OK, data={"instance_id": record.instance_id, "operation_id": proof.operation_id, "journal_status": proof.status, "pointer_released": released is not None})


def _orphan_prepared(request: UpdateRequest, record: InstanceInventoryRecordV2) -> tuple[dict[str, JsonValue], str]:
    """Section 4.3: before the pointer, the only abandonable document is the orphan ``prepared`` journal."""
    directory = request.manager_paths.operations_dir / record.instance_id
    orphans: list[dict[str, JsonValue]] = []
    for path in sorted(directory.glob("opr_*.json")) if directory.is_dir() else []:
        try:
            journal = read_update_journal(path)
        except StateError:
            continue
        if journal["status"] == "prepared" and journal["instance_id"] == record.instance_id:
            orphans.append(journal)
    if len(orphans) != 1:
        raise UpdateBlockedError("no_active_update", "no update is active for this instance and no single orphan prepared journal exists", repair=f"Run `solet-manager update {record.name} --dry-run` to start one.")
    return orphans[0], "prepared"


def _active_journal(request: UpdateRequest, record: InstanceInventoryRecordV2) -> tuple[dict[str, JsonValue], str]:
    active = record.active_operation
    if active is None or active.kind is not MaintenanceOperationKind.UPDATE:
        raise UpdateBlockedError("no_active_update", "no update is active for this instance", repair=f"Run `solet-manager update {record.name} --dry-run` to start one.")
    journal = read_update_journal(request.manager_paths.operation_path(record.instance_id, active.operation_id))
    if journal["operation_id"] != active.operation_id or journal["instance_id"] != record.instance_id:
        raise StateError("the update journal at the active pointer names a different operation")
    return journal, cast(str, journal["status"])


def _base_data(record: InstanceInventoryRecordV2, journal: dict[str, JsonValue], head: str) -> dict[str, JsonValue]:
    result = journal["result"]
    return {
        "instance": {"instance_id": record.instance_id, "name": record.name, "management_state": record.management_state.value},
        "operation_id": journal["operation_id"],
        "journal_status": journal["status"],
        "result": result,
        "evidence_path": None,
        "head_observed": head,
        "baseline": journal["baseline"],
        "candidate": journal["candidate"],
        "axes": {"source": record.source_release.commit, "runtime": None if record.runtime_release is None else record.runtime_release.commit, "verified": None if record.verified_release is None else record.verified_release.commit},
        "preservation": {"target_byte_writes": 0, "manager_state_writes": 0},
        "approval_fingerprint": None,
    }

