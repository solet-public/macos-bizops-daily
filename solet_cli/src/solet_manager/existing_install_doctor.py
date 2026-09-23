"""``solet-manager doctor <name>``: dual-contract selection and the sectioned oracle (Step 6 design section 3).

The doctor selects exactly one of three contracts by explicit state, never by
digest equality (a diagnostic import's digest equals the candidate's whenever
import and update ran under the same installed Manager), renders the sixteen
sections of governing section 9 with the closed A6 status algebra, and
writes exactly W1 (one doctor-journal append) plus, under the verified
contract only, W2 (``publish_needs_attention``).  The three pointer repairs
it can detect are *reported* with the exact ``reconcile`` command and never
performed, so a crashed doctor can strand nothing (D5).  ``doctor.py`` stays
the v1 create oracle, untouched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import cast

from .contract_copies import descriptor_copy_exists, read_transition_contract, transition_contract_exists
from .doctor_journal import append_doctor_run, create_doctor_journal, doctor_operation_id, latest_run, read_doctor_journal, write_doctor_journal
from .errors import CandidateCopyMissingError, InstanceUnmanagedError, InstanceUnmanagedV2Error, ManagedIdentityDriftError, ManagerError, StateError
from .existing_install_adapters import ExistingInstallAdapterRegistry
from .existing_install_bundle import parse_transition_bundle
from .existing_install_doctor_checks import DoctorProbe, Section, build_sections
from .existing_install_inspection import InstalledInspectionMetadata
from .existing_solet_diagnostics import DiagnosticStatus
from .host_software import base_python_or_none
from .maintenance_inventory import publish_needs_attention, read_maintenance_inventory_v2
from .maintenance_journal import read_maintenance_operation
from .models import CommandResult, DoctorContractKind, DoctorRun, ExitCode, InstanceInventoryRecordV2, JsonValue, MaintenanceOperationKind, ManagementState, ReleaseIdentity
from .paths import ManagerPaths
from .registry import InstanceRegistry
from .seed_lock_parser import parse_seed_lock_bytes
from .state_io import instance_lock, load_json_object
from .transaction import utc_now
from .update_candidate import UpdateCandidate
from .update_execution import (
    UpdateRequest,
    _candidate_execution,
    _LoaderTracker,
    enrolled_metadata,
    inspect_with_metadata,
    target_head,
)
from .update_journal import DOCTOR_STATUSES, RUNTIME_STATUSES, TERMINAL_UPDATE_STATUSES, UPDATE_STATUSES, is_retired, read_update_journal
from .update_runtime_plan import PlanContext, RuntimeSeams

__all__ = ["RESULT_KIND", "DoctorSelection", "DoctorTable", "doctor_result", "run_doctor", "run_selected", "select_contract", "start_safety"]

RESULT_KIND = "existing_install_doctor"
_CROSSED_STATUSES = frozenset({"source_advanced", *RUNTIME_STATUSES, *DOCTOR_STATUSES})
_TIE_BREAK_STATUSES = frozenset({"source_applying", "blocked", "failed"})
_REPAIR_RELEASE = "Run `solet-manager reconcile {name} --release-pointer --yes`."
_STATUS_ORDER = {"verified": 0, "incomplete": 1, "failed": 2}


@dataclass(frozen=True, slots=True)
class DoctorTable:
    """The Manager-shipped closed (contract, check) -> required table (section 3.3, D1)."""

    rows: dict[str, dict[str, bool]]
    conditions: dict[str, str]
    order: tuple[str, ...]

    @classmethod
    def load(cls) -> DoctorTable:
        resource = resources.files("solet_manager").joinpath("released_metadata", "existing_install_doctor_contract.v1.json")
        raw = json.loads(resource.read_text(encoding="utf-8"))
        rows: dict[str, dict[str, bool]] = {}
        conditions: dict[str, str] = {}
        order: list[str] = []
        for section in cast(list[dict[str, JsonValue]], raw["sections"]):
            order.append(cast(str, section["section"]))
            for check in cast(list[dict[str, JsonValue]], section["checks"]):
                check_id = cast(str, check["check_id"])
                rows[check_id] = {kind: bool(value) for kind, value in cast(dict[str, JsonValue], check["required"]).items()}
                if isinstance(check.get("condition"), str):
                    conditions[check_id] = cast(str, check["condition"])
        return cls(rows, conditions, tuple(order))

    def required(self, contract: DoctorContractKind, check_id: str) -> bool:
        key = check_id.split(":", 1)[0]
        row = self.rows.get(key)
        if row is None:
            raise StateError(f"doctor check {check_id!r} is outside the shipped contract table")
        return row[contract.value]


@dataclass(frozen=True, slots=True)
class DoctorSelection:
    """The outcome of section 3.1: one contract, its bindings, and any pending repair the report must carry."""

    contract: DoctorContractKind
    record: InstanceInventoryRecordV2
    journal: dict[str, JsonValue] | None
    head: str
    expected_source: ReleaseIdentity
    expected_runtime: ReleaseIdentity | None
    bundle_digest: str | None
    update_operation_id: str | None
    pending: tuple[str, str] | None
    notes: tuple[str, ...]


def select_contract(paths: ManagerPaths, record: InstanceInventoryRecordV2, head: str) -> DoctorSelection:
    """The total function of section 3.1 over ``(record, journal, HEAD)``; every branch is closed."""
    active = record.active_operation
    if active is None:
        return _select_idle(record, head)
    if active.kind is MaintenanceOperationKind.IMPORT:
        return _select_import(paths, record, head)
    if active.kind is MaintenanceOperationKind.UPDATE:
        return _select_update(paths, record, head)
    raise StateError("the active pointer names a doctor operation; doctor never holds the pointer")


def _select_idle(record: InstanceInventoryRecordV2, head: str) -> DoctorSelection:
    verified = record.verified_release
    digest = record.contract_identities.verified_contract_digest
    if verified is not None and digest is not None:
        return DoctorSelection(DoctorContractKind.VERIFIED, record, None, head, verified, verified, digest, None, None, ())
    if verified is None and digest is None and record.management_state in {ManagementState.DIAGNOSTIC, ManagementState.NEEDS_ATTENTION}:
        return _diagnostic(record, None, head, None, ())
    raise StateError("inventory row is verified without a verified release, or carries a verified release without its digest")


def _diagnostic(record: InstanceInventoryRecordV2, journal: dict[str, JsonValue] | None, head: str, pending: tuple[str, str] | None, notes: tuple[str, ...]) -> DoctorSelection:
    return DoctorSelection(DoctorContractKind.DIAGNOSTIC, record, journal, head, record.source_release, record.runtime_release, None, None, pending, notes)


def _select_import(paths: ManagerPaths, record: InstanceInventoryRecordV2, head: str) -> DoctorSelection:
    operation_id = cast(str, record.active_operation and record.active_operation.operation_id)
    document = read_maintenance_operation(paths.operation_path(record.instance_id, operation_id))
    status = cast(str, document["status"])
    if status in {"prepared", "bundle_cached", "inventory_published"}:
        return _diagnostic(record, None, head, ("operation_in_progress", "Re-run the import with its recorded approval fingerprint."), (f"active import {operation_id} is {status}",))
    if status == "verified":
        return _diagnostic(record, None, head, ("import_finalization_pending", _REPAIR_RELEASE.format(name=record.name)), (f"active import {operation_id} is verified with the pointer still set",))
    return _diagnostic(record, None, head, (f"import_{status}", "Inspect the import journal; the enrollment did not complete."), (f"active import {operation_id} is terminal {status}",))


def _select_update(paths: ManagerPaths, record: InstanceInventoryRecordV2, head: str) -> DoctorSelection:
    operation_id = cast(str, record.active_operation and record.active_operation.operation_id)
    journal = _bound_update_journal(paths, record, operation_id)
    status = cast(str, journal["status"])
    pending = _pointer_pending(record, journal, status)
    if status == "promoted":
        # Section 3.1, last row: a promoted journal with the pointer still set is the verified contract plus the pointer repair.
        selection = _select_idle(record, head)
        return replace(selection, journal=journal, pending=pending, notes=(f"promoted update {operation_id} still holds the pointer",))
    if status == "abandoned" or is_retired(journal):
        return replace(_pre_source(record, journal, head, operation_id, status), pending=pending)
    if _crossed(journal, head):
        return _candidate_selection(record, journal, head, pending)
    pre_source = _pre_source(record, journal, head, operation_id, status)
    if head == cast(dict[str, JsonValue], journal["baseline"])["commit"]:
        return pre_source
    drift = ("managed_identity_drift", f"Inspect the target with `solet-manager doctor {record.name}`; HEAD is neither the baseline nor the candidate; do not reset.")
    return replace(pre_source, contract=DoctorContractKind.DIAGNOSTIC, pending=drift, notes=(*pre_source.notes, "HEAD is neither the approved baseline nor the approved candidate"))


def _bound_update_journal(paths: ManagerPaths, record: InstanceInventoryRecordV2, operation_id: str) -> dict[str, JsonValue]:
    journal = read_update_journal(paths.operation_path(record.instance_id, operation_id))
    if journal["instance_id"] != record.instance_id or journal["operation_id"] != operation_id or journal["status"] not in UPDATE_STATUSES:
        raise StateError("the update journal at the active pointer does not bind this instance and operation")
    return journal


def _crossed(journal: dict[str, JsonValue], head: str) -> bool:
    """Section 3.1's state predicate with exactly one HEAD read as the tie-break."""
    status = cast(str, journal["status"])
    candidate_commit = cast(dict[str, JsonValue], journal["candidate"])["commit"]
    return journal["source_mode"] == "verify" or status in _CROSSED_STATUSES or (status in _TIE_BREAK_STATUSES and head == candidate_commit)


def _candidate_selection(record: InstanceInventoryRecordV2, journal: dict[str, JsonValue], head: str, pending: tuple[str, str] | None) -> DoctorSelection:
    candidate_row = cast(dict[str, JsonValue], journal["candidate"])
    candidate = ReleaseIdentity(record.channel.canonical_repository, cast(str, candidate_row["commit"]), cast(str, candidate_row["tree"]), cast(str, candidate_row["tag"]))
    runtime = candidate if _runtime_is_candidate(journal, record) else record.runtime_release
    operation_id = cast(str, journal["operation_id"])
    notes = (f"active update {operation_id} is {journal['status']}",)
    return DoctorSelection(DoctorContractKind.CANDIDATE, record, journal, head, candidate, runtime, cast(str, candidate_row["contract_digest"]), operation_id, pending, notes)


def _pre_source(record: InstanceInventoryRecordV2, journal: dict[str, JsonValue], head: str, operation_id: str, status: str) -> DoctorSelection:
    verified = record.verified_release
    digest = record.contract_identities.verified_contract_digest
    notes = (f"active update {operation_id} is {status} (pre-source)",)
    pending = _pointer_pending(record, journal, status)
    if verified is not None and digest is not None:
        return DoctorSelection(DoctorContractKind.VERIFIED, record, journal, head, verified, verified, digest, None, pending, notes)
    return _diagnostic(record, journal, head, pending, notes)


def _runtime_is_candidate(journal: dict[str, JsonValue], record: InstanceInventoryRecordV2) -> bool:
    """Section 3.1 row 1 (n1): the candidate is the expected runtime once the journal passed lifecycle_advanced
    and the inventory's runtime axis names it -- the topology-conditioned service check then proves it live."""
    statuses = [cast(str, cast(dict[str, JsonValue], item)["status"]) for item in cast(list[JsonValue], journal["attempts"])]
    past_lifecycle = "lifecycle_advanced" in statuses or cast(str, journal["status"]) in {"lifecycle_advanced", "runtime_reconciling", "runtime_advanced", *DOCTOR_STATUSES, "promoted"}
    runtime = record.runtime_release
    return past_lifecycle and runtime is not None and runtime.commit == cast(dict[str, JsonValue], journal["candidate"])["commit"]


def _pointer_pending(record: InstanceInventoryRecordV2, journal: dict[str, JsonValue], status: str) -> tuple[str, str] | None:
    if status in {"promoted", "abandoned"} or is_retired(journal):
        return ("pointer_release_pending", _REPAIR_RELEASE.format(name=record.name))
    if status in TERMINAL_UPDATE_STATUSES:
        result = cast(dict[str, JsonValue], journal["result"])
        return (cast(str, result["reason_code"]), cast(str, result["repair"]))
    return None


# --- running -----------------------------------------------------------------------------------------


def run_doctor(request: UpdateRequest) -> CommandResult:
    """Standalone ``doctor <name>``: instance lock, selection, one run, W1 (+W2), the sectioned result."""
    paths = request.manager_paths
    # Step 7 section 12.3: the record is loaded (a read) BEFORE the lock, so an unmanaged name never creates
    # ``state/locks/<name>.lock``; it is re-loaded under the lock exactly as ``_write_w2`` re-reads under ``registry.lock``.
    _load_record(paths, request.name)
    with instance_lock(paths.lock_path(request.name), create=True):
        record = _load_record(paths, request.name)
        try:
            _require_identity(record)
            head = target_head(record)
            selection = select_contract(paths, record, head)
            run = run_selected(request, selection, None)
        except ManagedIdentityDriftError as exc:
            return _invalid(request.name, exc)
        return doctor_result(selection, run)


def _require_identity(record: InstanceInventoryRecordV2) -> None:
    """Section 3.1 step 2: the pinned target identity is re-proved before any other read (A6: invalid on mismatch)."""
    target = Path(record.target.canonical_path)
    try:
        info, parent = target.stat(), target.parent.stat()
    except OSError as exc:
        raise ManagedIdentityDriftError("the enrolled target no longer exists", repair="Re-inspect the target; the enrolled path no longer names a directory.") from exc
    expected = record.target
    same = (info.st_dev, info.st_ino) == (expected.filesystem_identity.device, expected.filesystem_identity.inode) and (parent.st_dev, parent.st_ino) == (expected.parent_filesystem_identity.device, expected.parent_filesystem_identity.inode)
    if not same or target.is_symlink():
        raise ManagedIdentityDriftError("target filesystem identity differs from the enrolled record", repair="Re-inspect the target; the enrolled path no longer names the same directory.")


def _load_record(paths: ManagerPaths, name: str) -> InstanceInventoryRecordV2:
    record = next((item for item in read_maintenance_inventory_v2(paths.maintenance_inventory_path) if item.name == name), None)
    if record is not None:
        if record.channel.descriptor_digest == record.contract_identities.diagnostic_contract_digest:
            raise StateError("inventory channel descriptor digest equals the transition-contract digest")
        return record
    if paths.registry_path.exists() and InstanceRegistry(paths.registry_path).get(name) is not None:
        raise InstanceUnmanagedV2Error(f"{name!r} is a v1 create instance", repair=f"Run `solet doctor {name}`.")
    raise InstanceUnmanagedError(f"no v2 inventory record is named {name!r}", repair=f"Run `solet-manager import {name} --target <path> --channel <channel> --dry-run`.")


def run_selected(request: UpdateRequest, selection: DoctorSelection, context: PlanContext | None) -> DoctorRun:
    """One doctor run under an already-selected contract; ``context`` is the executor's when the final doctor runs inside ``update --yes``."""
    started = utc_now()
    paths = request.manager_paths
    table = DoctorTable.load()
    probe = _probe(request, selection, context)
    sections = build_sections(probe)
    status, exit_code, counts, service_ok = _reduce(table, selection, sections)
    journal_path, journal = _journal(paths, selection)
    journal = append_doctor_run(
        journal,
        started_at=started,
        head_observed=selection.head,
        sections=[section.to_dict() for section in sections],
        counts=counts,
        status=status,
        exit_code=int(exit_code),
        preservation=_preservation(probe, writes=1),
        service_check_verified=service_ok,
    )
    write_doctor_journal(journal_path, read_doctor_journal(journal_path) if journal_path.exists() else None, journal)
    writes = 1
    if selection.contract is DoctorContractKind.VERIFIED and _attention_codes(table, selection, sections):
        _write_w2(paths, selection, _attention_codes(table, selection, sections))
        writes = 2
    run = cast(dict[str, JsonValue], latest_run(journal))
    return DoctorRun(cast(str, journal["operation_id"]), cast(int, run["run"]), selection.contract, status, exit_code, cast(str, run["evidence_digest"]), selection.head, tuple(section.to_dict() for section in sections), counts, _preservation(probe, writes=writes), service_ok)


def _journal(paths: ManagerPaths, selection: DoctorSelection) -> tuple[Path, dict[str, JsonValue]]:
    record = selection.record
    operation_id = doctor_operation_id(record.instance_id, selection.contract, selection.bundle_digest, selection.update_operation_id)
    path = paths.operation_path(record.instance_id, operation_id)
    if path.exists():
        return path, read_doctor_journal(path)
    journal = create_doctor_journal(
        instance_id=record.instance_id,
        contract=selection.contract,
        bundle_digest=selection.bundle_digest,
        update_operation_id=selection.update_operation_id,
        expected_source=_release_dict(selection.expected_source),
        expected_runtime=None if selection.expected_runtime is None else _release_dict(selection.expected_runtime),
    )
    return path, journal


def _write_w2(paths: ManagerPaths, selection: DoctorSelection, codes: tuple[str, ...]) -> None:
    with instance_lock(paths.registry_lock_path, create=True):
        current = next((item for item in read_maintenance_inventory_v2(paths.maintenance_inventory_path) if item.instance_id == selection.record.instance_id), None)
        if current is None:
            raise StateError("maintenance inventory lost the instance row")
        publish_needs_attention(paths.maintenance_inventory_path, current, reason_codes=codes, now=utc_now())


def _attention_codes(table: DoctorTable, selection: DoctorSelection, sections: list[Section]) -> tuple[str, ...]:
    codes: set[str] = set()
    for section in sections:
        for check in section.checks:
            if table.required(selection.contract, check.check_id) and check.status in {DiagnosticStatus.FAILED, DiagnosticStatus.MISSING}:
                codes.add(check.reason_code or check.check_id)
    return tuple(sorted(codes))


def _preservation(probe: DoctorProbe, *, writes: int) -> dict[str, JsonValue]:
    return {"target_byte_writes": 0, "manager_state_writes": writes, "target_process_executions": probe.executions, "invoked_vectors": list(probe.invoked_vectors)}


def _reduce(table: DoctorTable, selection: DoctorSelection, sections: list[Section]) -> tuple[str, ExitCode, dict[str, JsonValue], bool]:
    """Section 3.4: over required checks only; ``not_applicable`` satisfies but never counts as verified."""
    required, advisory = _count(table, selection, sections)
    service_ok = any(check.check_id in {"runtime_attestation", "runtime_process_identity"} and check.status is DiagnosticStatus.VERIFIED for section in sections for check in section.checks)
    status = _overall_status(required, selection)
    exit_code = {"failed": ExitCode.FAILED, "incomplete": ExitCode.HUMAN_ACTION, "verified": ExitCode.OK}[status]
    counts: dict[str, JsonValue] = {"required": cast(dict[str, JsonValue], dict(required)), "advisory": cast(dict[str, JsonValue], dict(advisory))}
    return status, exit_code, counts, service_ok


def _count(table: DoctorTable, selection: DoctorSelection, sections: list[Section]) -> tuple[dict[str, int], dict[str, int]]:
    required: dict[str, int] = dict.fromkeys(("verified", "missing", "failed", "unknown", "not_applicable", "total"), 0)
    advisory: dict[str, int] = dict(required)
    for section in sections:
        for check in section.checks:
            bucket = required if table.required(selection.contract, check.check_id) else advisory
            bucket[check.status.value] += 1
            bucket["total"] += 1
    return required, advisory


def _overall_status(required: dict[str, int], selection: DoctorSelection) -> str:
    """Section 3.1 (3c): a HEAD outside {baseline, candidate} is exit 3 managed_identity_drift -- operator action
    outside the Manager -- and the identity failures are its consequence, so they do not make it exit 1."""
    drift = selection.pending is not None and selection.pending[0] == "managed_identity_drift"
    if required["failed"] and not drift:
        return "failed"
    if required["missing"] or required["unknown"] or required["failed"] or selection.pending is not None:
        return "incomplete"
    return "verified"


# --- probe assembly ----------------------------------------------------------------------------------------


def _probe(request: UpdateRequest, selection: DoctorSelection, context: PlanContext | None) -> DoctorProbe:
    paths = request.manager_paths
    record = selection.record
    target = Path(record.target.canonical_path)
    seams = request.runtime_seams or RuntimeSeams()
    registry = ExistingInstallAdapterRegistry(record.name, target, Path(record.service_identity.bridge_cli_path), base_python_or_none(seams))
    candidate, metadata, context = _bindings(request, selection, context, registry, seams)
    inspection = inspect_with_metadata(request, record, metadata)
    return DoctorProbe(
        selection.contract, record, selection.expected_source, selection.expected_runtime, inspection, seams, registry, target, selection.journal, candidate, context,
        _cached_bundle(paths, record), _installed_release(request, record), paths=paths,
    )


def _bindings(request: UpdateRequest, selection: DoctorSelection, context: PlanContext | None, registry: ExistingInstallAdapterRegistry, seams: RuntimeSeams) -> tuple[UpdateCandidate | None, InstalledInspectionMetadata, PlanContext | None]:
    paths = request.manager_paths
    record = selection.record
    if selection.contract is DoctorContractKind.DIAGNOSTIC:
        descriptor = request.descriptor_loader(record.channel.channel_id, _LoaderTracker())
        return None, enrolled_metadata(record, parse_seed_lock_bytes(descriptor.descriptor_bytes)), None
    if context is not None:
        return context.candidate, _copy_metadata(paths, context.candidate.descriptor_digest), replace(context, probe_purpose="completion")
    if selection.contract is DoctorContractKind.CANDIDATE:
        journal = cast(dict[str, JsonValue], selection.journal)
        execution = _candidate_execution(request, record, paths.operation_path(record.instance_id, cast(str, journal["operation_id"])), journal, cast(str, cast(dict[str, JsonValue], journal["approval"])["fingerprint"]))
        return execution.candidate, execution.descriptor.metadata, execution.plan_context("completion")
    digest = record.channel.descriptor_digest
    bundle_digest = cast(str, selection.bundle_digest)
    if not descriptor_copy_exists(paths, digest) or not transition_contract_exists(paths, bundle_digest):
        raise CandidateCopyMissingError("the durable contract copies for the verified release are absent", repair=f"Reinstall the Manager release that shipped {selection.expected_source.tag} or run `solet-manager reconcile {record.name} --dry-run`.")
    from ._existing_install_inspection_metadata import descriptor_from_copy  # noqa: PLC0415 - keeps the copy reader optional at import time

    descriptor = descriptor_from_copy(paths, digest)
    files = read_transition_contract(paths, bundle_digest)
    candidate = UpdateCandidate(digest, parse_seed_lock_bytes(descriptor.descriptor_bytes), "sha256:" + "0" * 64, parse_transition_bundle(files["existing_install_flow.json"]), files, "copy")
    target = Path(record.target.canonical_path)
    context = PlanContext(record, candidate, "opr_" + "0" * 32, "sha256:" + "0" * 64, selection.expected_source.commit, selection.expected_source.tree, registry, target, seams, "completion", dict(request.operator_selections))
    return candidate, descriptor.metadata, context


def _copy_metadata(paths: ManagerPaths, descriptor_digest: str) -> InstalledInspectionMetadata:
    from ._existing_install_inspection_metadata import descriptor_from_copy  # noqa: PLC0415

    return descriptor_from_copy(paths, descriptor_digest).metadata


def _cached_bundle(paths: ManagerPaths, record: InstanceInventoryRecordV2) -> dict[str, JsonValue] | None:
    try:
        raw = load_json_object(paths.contract_cache_path(record.inspection_bundle_digest), missing_ok=True)
    except ManagerError:
        return None
    return raw


def _installed_release(request: UpdateRequest, record: InstanceInventoryRecordV2) -> tuple[str, str | None] | None:
    try:
        descriptor = request.descriptor_loader(record.channel.channel_id, _LoaderTracker())
    except (ValueError, OSError, ManagerError):
        return None
    return descriptor.metadata.channel_identity.commit, descriptor.metadata.channel_identity.release_tag


# --- result ------------------------------------------------------------------------------------------------


def doctor_result(selection: DoctorSelection, run: DoctorRun) -> CommandResult:
    record = selection.record
    error_kind, repair = (None, None) if selection.pending is None else selection.pending
    if run.status == "failed" and error_kind is None:
        error_kind, repair = "required_check_failed", "Repair the failed required checks, then run the doctor again."
    elif run.status == "incomplete" and error_kind is None:
        error_kind, repair = "required_check_unresolved", "Resolve the missing or unknown required checks, then run the doctor again."
    data: dict[str, JsonValue] = {
        "instance": {"instance_id": record.instance_id, "name": record.name, "canonical_target": record.target.canonical_path, "management_state": record.management_state.value},
        "contract": {"kind": selection.contract.value, "bundle_digest": selection.bundle_digest, "update_operation_id": selection.update_operation_id, "expected_source": _release_dict(selection.expected_source), "expected_runtime": None if selection.expected_runtime is None else _release_dict(selection.expected_runtime)},
        "active_operation": None if record.active_operation is None else {"kind": record.active_operation.kind.value, "operation_id": record.active_operation.operation_id, "notes": list(selection.notes)},
        "head_observed": selection.head,
        "sections": list(run.sections),
        "counts": run.counts,
        "preservation": run.preservation,
        "doctor_operation_id": run.doctor_operation_id,
        "run": run.run,
        "evidence_digest": run.evidence_digest,
        "start_safety": _start_safety_dict(selection, run),
    }
    return CommandResult(RESULT_KIND, run.status, f"Doctor completed under the {selection.contract.value} contract.", run.exit_code, error_kind, repair, data=data)


def _invalid(name: str, exc: ManagedIdentityDriftError) -> CommandResult:
    return CommandResult(RESULT_KIND, "invalid", str(exc), ExitCode.INVALID, "target_identity_invalid", exc.repair, data={"instance": {"name": name}, "sections": [], "counts": {"required": {}, "advisory": {}}, "preservation": {"target_byte_writes": 0, "manager_state_writes": 0}})


def _start_safety_dict(selection: DoctorSelection, run: DoctorRun) -> dict[str, JsonValue]:
    safe, reasons = start_safety(selection, run)
    return {"safe": safe, "reasons": list(reasons)}


def start_safety(selection: DoctorSelection, run: DoctorRun) -> tuple[bool, tuple[str, ...]]:
    """D10: the refusal predicate a future ``start`` verb uses (the verb itself is Step 7)."""
    reasons: list[str] = []
    if selection.contract is not DoctorContractKind.VERIFIED:
        reasons.append(f"contract_{selection.contract.value}")
    if selection.record.active_operation is not None:
        reasons.append("active_operation_pointer_set")
    wanted = {"identity", "source_topology", "service_router"}
    for section in run.sections:
        if section["section"] not in wanted:
            continue
        for check in cast(list[dict[str, JsonValue]], section["checks"]):
            if check["status"] not in {"verified", "not_applicable"} and _required_for_start(cast(str, check["check_id"])):
                reasons.append(f"{section['section']}:{check['check_id']}")
    return (not reasons, tuple(reasons))


def _required_for_start(check_id: str) -> bool:
    return check_id not in {"enrollment_drift", "bridge_health", "router_serving"}


def _release_dict(release: ReleaseIdentity) -> dict[str, JsonValue]:
    return {"repository": release.repository, "commit": release.commit, "tree": release.tree, "tag": release.tag}

