"""Installation acceptance oracle over the pinned flow probes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .adapters import AdapterRegistry, resolve_long_lived_python
from .completion_verifier import run_completion_probes
from .contract_reconciliation import recover_contract_reconciliation
from .contracts import ContractBundle, target_contract_directory
from .create_applied_edits import accept_applied_edits, accepted_edit_paths
from .doctor_blue_green_census import collect_blue_green_advisories
from .doctor_credential_copy_census import collect_credential_copy_advisories
from .doctor_genesis_marker_census import collect_genesis_marker_advisories
from .doctor_inference_qualification import (
    collect_inference_probe_advisories,
    collect_inference_qualification_advisories,
)
from .doctor_lm_studio_census import collect_lm_studio_advisories
from .doctor_plugin_version_skew_census import collect_plugin_version_skew_advisories
from .doctor_postgres_pin_census import collect_postgres_pin_advisories
from .doctor_python_interpreter_census import collect_python_interpreter_advisories
from .doctor_python_on_request_census import collect_python_on_request_advisories
from .doctor_release_identity_census import collect_release_identity_advisories
from .doctor_residue_census import collect_residue_advisories
from .doctor_router_identity_census import collect_router_identity_advisories
from .doctor_secret_exposure_census import collect_secret_exposure_advisories
from .doctor_seed_integrity_census import collect_seed_integrity_advisories
from .doctor_terminal_return_keys import collect_terminal_return_key_advisories
from .doctor_vintage_census import collect_doctor_advisories
from .errors import StateConflictError
from .journal_rollup import _FINAL_STAGE_STATUSES
from .maintenance_inventory import read_maintenance_inventory_v2
from .models import CommandResult, ExitCode, InstanceRecord, JsonValue
from .paths import ManagerPaths
from .registry import InstanceRegistry, is_create_origin_alias
from .seed_tree_verifier import SeedTreeVerification, verify_seed_tree
from .state_io import instance_lock
from .transaction import Transaction, load_transaction, write_transaction


class InstallationDoctor:
    def __init__(self, *, paths: ManagerPaths, contract_directory: Path | None) -> None:
        self.paths = paths
        # Kept only for CLI compatibility. Managed doctor must never use the
        # formula/current explicit directory for an old instance.
        self.contract_directory = contract_directory
        self.registry = InstanceRegistry(paths.registry_path)

    def run(self, name: str) -> CommandResult:
        record = self.registry.require(name)
        if record.lifecycle_state == "setup_incomplete":
            return _provisional_doctor_result(
                name,
                record,
                load_transaction(self.paths.transaction_path(name)),
            )
        target = Path(record.target)
        with instance_lock(self.paths.lock_path(name), create=True):
            recover_contract_reconciliation(self.paths, name)
            transaction, bundle, adapter_registry = _doctor_context(
                self.paths,
                name,
                record,
                target,
            )
            seed_record = current_seed_record(self.paths, record)
            accepted = accepted_edit_paths(self.paths, name=name, target=target, create_operation_id=transaction.operation_id)
            seed_tree_verification = accept_applied_edits(verify_seed_tree(target, seed_record.seed_tree_hash), accepted)
            transaction, checks = run_completion_probes(
                bundle,
                transaction,
                adapter_registry,
                self.paths,
            )
            transaction = transaction.with_result_kind(
                "doctor_verified" if transaction.status.value == "verified" else "doctor_incomplete"
            )
            write_transaction(self.paths.transaction_path(name), transaction)
        advisories = collect_doctor_advisories(seed_record, transaction)
        advisories.extend(collect_residue_advisories(record))
        advisories.extend(collect_blue_green_advisories(record))
        advisories.extend(collect_seed_integrity_advisories(record, transaction))
        advisories.extend(collect_release_identity_advisories(record, transaction, accepted_edits=accepted))
        advisories.extend(collect_secret_exposure_advisories(record))
        advisories.extend(collect_genesis_marker_advisories(record))
        advisories.extend(collect_postgres_pin_advisories(record))
        advisories.extend(collect_python_interpreter_advisories(record))
        advisories.extend(collect_python_on_request_advisories(record))
        advisories.extend(collect_router_identity_advisories(record))
        advisories.extend(collect_credential_copy_advisories(record))
        advisories.extend(collect_plugin_version_skew_advisories(record))
        advisories.extend(collect_lm_studio_advisories(record))
        advisories.extend(collect_terminal_return_key_advisories(record))
        advisories.extend(
            collect_inference_qualification_advisories(
                bundle,
                transaction,
                adapter_registry,
            )
        )
        advisories.extend(collect_inference_probe_advisories(transaction))
        return _doctor_result(name, transaction, checks, seed_tree_verification, advisories)


def current_seed_record(paths: ManagerPaths, record: InstanceRecord) -> InstanceRecord:
    """The create record with its seed identity at the release the Manager last checked out (iss_a81b79e3).

    The v1 row is the immutable create record: ``create_transaction_matches_record`` binds it to
    the create transaction and ``is_create_origin_alias`` joins it to v2 on identity only.  Once
    ``update`` enrolls the instance, the v2 row's ``source_release`` is the Manager's record of the
    tree it checked out: the same promotion that publishes ``verified_release`` requires it at the
    candidate.  So a promoted update is read from there, never by rewriting the create record, and a
    solet promoted before this fix reads correctly on its first doctor with no migration.  No v2 row
    means never updated, so the create record is the seed identity.  A same-name v2 row that does not
    join the create record is refused rather than silently read past: which release this checkout
    should be at is then unknown.
    """

    row = next((item for item in read_maintenance_inventory_v2(paths.maintenance_inventory_path) if item.name == record.name), None)
    if row is None:
        return record
    if not is_create_origin_alias(record, row):
        target = row.target
        raise StateConflictError(
            f"managed instance {record.name!r}: the v2 inventory row (origin {row.management_origin.value}, target "
            f"{target.canonical_path}, inode {target.filesystem_identity.inode}) is not the create record's alias "
            f"(target {record.target}); the doctor cannot tell which seed release this checkout should be at"
        )
    release = row.source_release
    return replace(
        record,
        seed_repository=release.repository,
        seed_tag=release.tag,
        seed_commit=release.commit,
        seed_tree_hash=release.tree,
    )


def _provisional_doctor_result(
    name: str,
    record: InstanceRecord,
    transaction: Transaction | None,
) -> CommandResult:
    """Keep doctor from treating a materialized target as an installation."""

    return CommandResult(
        kind="installation_doctor",
        status="awaiting_user",
        message=f"Managed instance {name!r} setup is incomplete; doctor cannot verify it yet.",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="instance_setup_incomplete",
        repair=f"Resume with: solet create {name}",
        data={
            "name": name,
            "lifecycle_state": record.lifecycle_state,
            "transaction_status": None if transaction is None else transaction.status.value,
            "expected_router": {
                "name": record.expected_router_name,
                "socket": record.expected_router_socket,
                "port_range": record.expected_router_port_range,
            },
        },
    )


def _doctor_context(
    paths: ManagerPaths,
    name: str,
    record: InstanceRecord,
    target: Path,
) -> tuple[Transaction, ContractBundle, AdapterRegistry]:
    transaction = load_transaction(paths.transaction_path(name))
    if transaction is None:
        raise StateConflictError(f"managed instance {name!r} lacks its transaction journal")
    if (
        transaction.target != record.target
        or transaction.flow_source_revision != record.flow_source_revision
        or transaction.flow_contract_digest != record.flow_contract_digest
    ):
        raise StateConflictError("registry and transaction pinned identities differ")
    bundle = ContractBundle.load(
        source_revision=record.flow_source_revision,
        directory=target_contract_directory(target),
        expected_digest=record.flow_contract_digest,
        resume_compatibility=True,
    )
    registry = AdapterRegistry(
        target=target,
        base_python=resolve_long_lived_python(),
    )
    return transaction, bundle, registry


def _doctor_result(
    name: str,
    transaction: Transaction,
    checks: list[JsonValue],
    seed_tree_verification: SeedTreeVerification | None = None,
    advisories: list[JsonValue] | None = None,
) -> CommandResult:
    """Report the acceptance verdict over the checks doctor actually ran.

    The verdict is scoped to the completion probes, never to the whole retained
    setup-stage map.  Folding the two together is what let doctor print "repair
    the listed required checks" while every listed check was verified and the
    real obstacle was an unnamed setup stage.  A setup-stage blocker is a
    distinct diagnostic, so it is reported separately and by name.
    """

    verified_count = sum(
        1 for item in checks if isinstance(item, dict) and item.get("status") == "verified"
    )
    completion_verified = bool(checks) and verified_count == len(checks)
    completion_failed = any(
        isinstance(item, dict) and item.get("status") == "failed" for item in checks
    )
    blockers = _setup_stage_blockers(transaction)
    return CommandResult(
        kind="installation_doctor",
        status=transaction.status.value,
        message=_doctor_message(name, completion_verified, blockers),
        exit_code=_doctor_exit_code(completion_verified, completion_failed, blockers),
        error_kind=_doctor_error_kind(completion_verified, blockers),
        repair=_doctor_repair(completion_verified, blockers),
        data={
            "name": name,
            "checks": checks,
            "required_count": len(checks),
            "verified_count": verified_count,
            "completion_verified": completion_verified,
            "setup_stage_blockers": [dict(item) for item in blockers],
            "transaction_status": transaction.status.value,
            "flow_contract_digest": transaction.flow_contract_digest,
            "seed_tree_verification": None
            if seed_tree_verification is None
            else seed_tree_verification.to_dict(),
            "advisories": [] if advisories is None else advisories,
        },
    )


def _setup_stage_blockers(transaction: Transaction) -> tuple[dict[str, JsonValue], ...]:
    """Name every retained stage that is not in a final state.

    ``NOT_APPLICABLE`` and ``DECLINED`` are settled outcomes, not unfinished
    work, so they are not blockers.
    """

    return tuple(
        {"stage_id": stage_id, "status": status.value}
        for stage_id, status in transaction.stages.items()
        if status not in _FINAL_STAGE_STATUSES
    )


def _doctor_message(
    name: str,
    completion_verified: bool,
    blockers: tuple[dict[str, JsonValue], ...],
) -> str:
    headline = (
        "verified every required check"
        if completion_verified
        else "did not verify every required check"
    )
    message = f"Installation doctor {headline} for {name!r}."
    if blockers:
        named = ", ".join(f"{item['stage_id']} ({item['status']})" for item in blockers)
        message += f" Setup is not complete; stages still open: {named}."
    return message


def _doctor_repair(
    completion_verified: bool,
    blockers: tuple[dict[str, JsonValue], ...],
) -> str | None:
    repairs: list[str] = []
    if not completion_verified:
        repairs.append("Repair the listed required checks and rerun doctor.")
    if blockers:
        named = ", ".join(str(item["stage_id"]) for item in blockers)
        repairs.append(
            f"Finish the open setup stages ({named}) and rerun doctor; "
            "these are not completion checks and are not repaired by rerunning them."
        )
    return " ".join(repairs) if repairs else None


def _doctor_error_kind(
    completion_verified: bool,
    blockers: tuple[dict[str, JsonValue], ...],
) -> str | None:
    if not completion_verified:
        return "doctor_incomplete"
    if blockers:
        return "setup_stage_incomplete"
    return None


def _doctor_exit_code(
    completion_verified: bool,
    completion_failed: bool,
    blockers: tuple[dict[str, JsonValue], ...],
) -> ExitCode:
    if not completion_verified:
        return ExitCode.FAILED if completion_failed else ExitCode.HUMAN_ACTION
    return ExitCode.HUMAN_ACTION if blockers else ExitCode.OK
