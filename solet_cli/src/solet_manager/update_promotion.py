"""Final doctor and promotion: the tail of ``update --yes`` past ``runtime_advanced`` (Step 6 design section 5).

Promotion is the Manager-state operation that makes the candidate contract the
instance's active verified release contract.  It touches no target byte, is
gated on the final doctor's *evidence digest* rather than on the apply's
success, and is the only writer of ``verified_release``,
``verified_``/``current_contract_digest``, ``management_state``,
``last_verified_*`` and (through the release) the active pointer.  The
sequence is crash-safe at every boundary (section 5.3): journal
``doctor_verified -> promoting``, the idempotent ``publish_promotion`` CAS,
journal ``promoting -> promoted``, then ``release_active_operation`` against
the terminal proof.  A crash anywhere resumes by re-checking the
preconditions against fresh reads and repeating only the idempotent step.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from .contract_copies import read_transition_contract
from .doctor_journal import doctor_operation_id, latest_run, read_doctor_journal
from .errors import StateConflictError, StateError, TransitionContractMismatchError
from .maintenance_inventory import publish_promotion, read_maintenance_inventory_v2
from .models import CommandResult, DoctorContractKind, ExitCode, InstanceInventoryRecordV2, JsonValue, ManagementState, ReleaseIdentity, UpdateEligibility, UpdateEligibilityState
from .state_io import instance_lock
from .transaction import utc_now
from .update_deferral import deferred_rows
from .update_pointer_repair import release_terminal_pointer

if TYPE_CHECKING:
    from .update_runtime_execution import RuntimeExecution

__all__ = ["finish"]

RESULT_KIND = "existing_install_update"
_EVIDENCE = re.compile(r"evidence_digest=(sha256:[0-9a-f]{64})")


def finish(execution: RuntimeExecution) -> CommandResult:
    """Drive ``runtime_advanced`` through the final doctor and promotion; returns at the first stop."""
    if execution.status in {"runtime_advanced", "doctor_incomplete"}:
        execution.advance_status("doctor_verifying", "final doctor started under the candidate contract (a rerun repeats the whole read-only run)")
    if execution.status == "doctor_verifying":
        stop = _final_doctor(execution)
        if stop is not None:
            return stop
    if execution.status == "doctor_verified":
        _require_preconditions(execution)
        execution.advance_status("promoting", "promotion preconditions verified from fresh reads under the registry lock")
    if execution.status == "promoting":
        _promote(execution)
    return _promoted_result(execution)


def _final_doctor(execution: RuntimeExecution) -> CommandResult | None:
    from .existing_install_doctor import doctor_result, run_selected, select_contract  # noqa: PLC0415 - the doctor imports the executor's module

    request = execution.request
    if request is None:
        raise StateError("the executor carries no request; the final doctor cannot bind its probes")
    record = execution.current_record()
    selection = select_contract(execution.paths, record, execution.head())
    if selection.contract is not DoctorContractKind.CANDIDATE or selection.update_operation_id != execution.operation_id:
        raise StateError("the doctor did not select the candidate contract of the active update")
    run = run_selected(request, selection, execution.context)
    execution.executed.append(f"doctor:{run.doctor_operation_id}:{run.run}")
    if run.status == "verified":
        execution.advance_status("doctor_verified", f"final doctor verified; doctor_operation_id={run.doctor_operation_id}; run={run.run}; evidence_digest={run.evidence_digest}")
        return None
    execution.advance_status("doctor_incomplete", f"final doctor {run.status}; doctor_operation_id={run.doctor_operation_id}; run={run.run}; evidence_digest={run.evidence_digest}")
    execution.publish_needs_attention(_unresolved_codes(run.sections, execution))
    result = doctor_result(selection, run)
    data = dict(result.data)
    data["update"] = execution.result_data()
    exit_code = ExitCode.FAILED if run.status == "failed" else ExitCode.HUMAN_ACTION
    return CommandResult(RESULT_KIND, "doctor_incomplete", f"Final doctor {run.status}; the update stays active and management_state is needs_attention.", exit_code, result.error_kind, f"Repair the listed checks, then re-run `solet-manager update {record.name} --yes --approval-fingerprint <runtime fingerprint>` to rerun the doctor.", data=data)


def _unresolved_codes(sections: tuple[dict[str, JsonValue], ...], execution: RuntimeExecution) -> tuple[str, ...]:
    from .existing_install_doctor import DoctorTable  # noqa: PLC0415

    table = DoctorTable.load()
    codes: set[str] = set()
    for section in sections:
        for check in cast(list[dict[str, JsonValue]], section["checks"]):
            check_id = cast(str, check["check_id"])
            if check["status"] in {"failed", "missing", "unknown"} and table.required(DoctorContractKind.CANDIDATE, check_id):
                codes.add(cast(str, check["reason_code"] or check_id))
    del execution
    return tuple(sorted(codes)) or ("doctor_incomplete",)


def _require_preconditions(execution: RuntimeExecution) -> None:
    """Section 5.2, all four, from fresh reads."""
    _require_verified_doctor_run(execution)
    _require_axes_at_candidate(execution)
    contract = execution.context.candidate.bundle_digest
    if read_transition_contract(execution.paths, contract) != execution.context.candidate.bundle_files:
        raise TransitionContractMismatchError("the durable transition contract copy no longer matches the proven candidate bundle")


def _require_verified_doctor_run(execution: RuntimeExecution) -> None:
    """5.2 (1) and (3): the doctor journal carries the recorded verified run and its service check verified."""
    digest = _recorded_evidence_digest(execution.journal)
    run = _doctor_run(execution)
    if run is None or run["evidence_digest"] != digest or run["status"] != "verified":
        raise StateConflictError("the doctor journal does not carry the verified run the update journal recorded")
    if run["service_check_verified"] is not True:
        raise StateConflictError("the final doctor's topology-conditioned service check did not verify")


def _require_axes_at_candidate(execution: RuntimeExecution) -> None:
    """5.2 (2): same pointer, both axes and both digests at the candidate, update-in-progress eligibility."""
    current = execution.current_record()
    candidate = _candidate_release(execution)
    contract = execution.context.candidate.bundle_digest
    identities = current.contract_identities
    axes_ok = current.source_release == candidate and current.runtime_release == candidate and identities.source_contract_digest == contract and identities.runtime_contract_digest == contract
    if not axes_ok:
        raise StateConflictError("promotion requires source and runtime axes at the candidate")
    eligibility = current.update_eligibility
    if eligibility.state is not UpdateEligibilityState.BLOCKED or "update_in_progress" not in eligibility.reason_codes:
        raise StateConflictError("promotion requires the update-in-progress eligibility")


def _recorded_evidence_digest(journal: dict[str, JsonValue]) -> str:
    for item in reversed(cast(list[JsonValue], journal["attempts"])):
        attempt = cast(dict[str, JsonValue], item)
        if attempt["status"] == "doctor_verified":
            match = _EVIDENCE.search(cast(str, attempt["note"]))
            if match is not None:
                return match.group(1)
    raise StateError("the update journal's doctor_verified attempt records no evidence digest")


def _doctor_run(execution: RuntimeExecution) -> dict[str, JsonValue] | None:
    operation_id = doctor_operation_id(execution.record.instance_id, DoctorContractKind.CANDIDATE, execution.context.candidate.bundle_digest, execution.operation_id)
    path = execution.paths.operation_path(execution.record.instance_id, operation_id)
    if not path.exists():
        return None
    return latest_run(read_doctor_journal(path))


def _candidate_release(execution: RuntimeExecution) -> ReleaseIdentity:
    fields = execution.context.candidate.fields
    return ReleaseIdentity(fields.repository, fields.commit, fields.tree_hash, fields.release_tag)


def _promote(execution: RuntimeExecution) -> None:
    """Section 5.3 steps 2-4; every step is idempotent against what a crash may already have written."""
    current = execution.current_record()
    candidate = _candidate_release(execution)
    contract = execution.context.candidate.bundle_digest
    # A verify-mode update at the already-verified release must still clear a needs_attention row (iss_6d26db73).
    if current.verified_release != candidate or current.contract_identities.verified_contract_digest != contract or current.management_state is not ManagementState.VERIFIED:
        digest = _recorded_evidence_digest(execution.journal)
        with instance_lock(execution.paths.registry_lock_path, create=True):
            current = publish_promotion(execution.paths.maintenance_inventory_path, current, operation_id=execution.operation_id, verified_release=candidate, verified_contract_digest=contract, eligibility=_eligibility(execution, candidate), doctor_evidence_digest=digest, now=utc_now())
        execution.executed.append("inventory:publish_promotion")
    execution.record = current
    deferred = deferred_rows(execution.journal)
    if deferred:
        # Before the pointer releases, so a crash cannot leave a VERIFIED row that hides a deferral (iss_6d26db73).
        execution.publish_needs_attention(tuple(sorted({error_kind for _operation, error_kind, _repair in deferred})))
        outcome: dict[str, JsonValue] = {"kind": "promoted", "reason_code": "migrations_deferred", "repair": " ".join(repair for _operation, _kind, repair in deferred)}
    else:
        outcome = {"kind": "promoted", "reason_code": "verified", "repair": "No operator action is required."}
    execution.advance_status("promoted", "promotion published and read back; the candidate is the active verified release contract", result=outcome)
    released = release_terminal_pointer(execution.paths, execution.current_record())
    if released is not None:
        execution.record = released
        execution.executed.append("inventory:release_active_operation")


def _eligibility(execution: RuntimeExecution, candidate: ReleaseIdentity) -> UpdateEligibility:
    """Computed against the *installed* descriptor now (section 4.5), so a Manager upgraded mid-operation reports truthfully."""
    request = execution.request
    installed = None
    if request is not None:
        from .update_execution import _LoaderTracker  # noqa: PLC0415

        try:
            installed = request.descriptor_loader(execution.record.channel.channel_id, _LoaderTracker()).metadata.channel_identity.commit
        except (ValueError, OSError):
            installed = None
    if installed is None:
        return UpdateEligibility(UpdateEligibilityState.AVAILABLE, ("descriptor_unavailable",))
    return UpdateEligibility(UpdateEligibilityState.CURRENT if installed == candidate.commit else UpdateEligibilityState.AVAILABLE, ())


def _promoted_result(execution: RuntimeExecution) -> CommandResult:
    if execution.status != "promoted":
        raise StateError(f"promotion ended at {execution.status!r} instead of promoted")
    record = _fresh(execution)
    data = execution.result_data()
    data["management_state"] = record.management_state.value
    data["update_eligibility"] = {"state": record.update_eligibility.state.value, "reason_codes": list(record.update_eligibility.reason_codes)}
    data["active_operation"] = None if record.active_operation is None else record.active_operation.operation_id
    deferred = deferred_rows(execution.journal)
    data["deferred_operations"] = [{"operation_id": operation, "reason_code": kind, "repair": repair} for operation, kind, repair in deferred]
    if deferred:
        repair = " ".join(item for _operation, _kind, item in deferred)
        return CommandResult(RESULT_KIND, "promoted", f"The candidate was promoted; {len(deferred)} release migration(s) deferred with the prior configuration still active: {repair}", ExitCode.OK, data=data)
    return CommandResult(RESULT_KIND, "promoted", "Final doctor verified and the candidate was promoted to the active verified release contract.", ExitCode.OK, data=data)


def _fresh(execution: RuntimeExecution) -> InstanceInventoryRecordV2:
    records = read_maintenance_inventory_v2(execution.paths.maintenance_inventory_path)
    current = next((item for item in records if item.instance_id == execution.record.instance_id), None)
    if current is None:
        raise StateError("maintenance inventory lost the instance row")
    return current
