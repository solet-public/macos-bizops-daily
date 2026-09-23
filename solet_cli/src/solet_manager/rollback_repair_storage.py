"""Durable journal, retained generation, mutator, and independent oracle."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from .models import JsonValue
from .paths import ManagerPaths
from .rollback_repair_contract import (
    RECIPE_ID,
    RECIPE_VERSION,
    RepairPlan,
    RepairRefusedError,
    artifact_identity,
    collateral_census,
    json_digest,
    sha256_file,
    validate_service_bindings,
)
from .state_io import ensure_private_directory


class RollbackActionError(RuntimeError):
    """The declared inverse action failed at its mutator boundary."""


class RollbackPostconditionError(RuntimeError):
    """Independent verification rejected the resulting state."""


def journal_path(paths: ManagerPaths, failure_id: str) -> Path:
    return paths.state_dir / "repairs" / f"{failure_id}.jsonl"


def recovery_path(paths: ManagerPaths, failure_id: str) -> Path:
    return paths.state_dir / "repair_generations" / f"{failure_id}.before"


def read_journal(journal: Path, plan: RepairPlan) -> list[dict[str, JsonValue]]:
    events = read_json_lines(journal)
    for index, event in enumerate(events, 1):
        if (
            event.get("sequence") != index
            or event.get("input_fingerprint") != plan.input_fingerprint
        ):
            raise RepairRefusedError(
                "repair_journal",
                "journal sequence or input identity is contradictory",
                evidence=str(journal),
            )
    return events


def append_event(
    journal: Path,
    plan: RepairPlan,
    *,
    phase: str,
    status: str,
    evidence: dict[str, JsonValue],
    next_action: str,
) -> None:
    ensure_private_directory(journal.parent)
    event: dict[str, JsonValue] = {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "sequence": len(read_json_lines(journal)) + 1,
        "phase": phase,
        "status": status,
        "subject": {"name": plan.instance_name, "target": str(plan.target)},
        "manager_transaction": {
            "path": str(plan.manager_transaction_path),
            "operation_id": plan.manager_operation_id,
            "sha256": plan.transaction_digest,
        },
        "transaction_sha256": plan.transaction_digest,
        "failure_id": plan.failure_id,
        "recipe": {"id": RECIPE_ID, "version": RECIPE_VERSION},
        "input_fingerprint": plan.input_fingerprint,
        "touched_set": list(plan.touched_set),
        "evidence": evidence,
        "next_action": next_action,
    }
    descriptor = os.open(
        journal,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(journal, 0o600)
    fsync_directory(journal.parent)


def retain_recovery_generation(plan: RepairPlan, recovery: Path) -> None:
    ensure_private_directory(recovery.parent)
    if recovery.exists():
        identity = artifact_identity(recovery)
        if identity.digest != plan.fault_digest or identity.link_count != 1:
            raise RepairRefusedError(
                "recovery_generation",
                "existing recovery generation is untrusted",
                evidence=str(recovery),
            )
        return
    descriptor = os.open(
        recovery,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    source_fd = os.open(plan.artifact.path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(source_fd, "rb") as source, os.fdopen(descriptor, "wb") as destination:
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        recovery.unlink(missing_ok=True)
        raise
    if sha256_file(recovery) != plan.fault_digest:
        raise RepairRefusedError(
            "recovery_generation",
            "retained prior generation digest mismatch",
            evidence=str(recovery),
        )
    fsync_directory(recovery.parent)


def atomic_restore(plan: RepairPlan, stage: Path, recovery: Path) -> dict[str, JsonValue]:
    del recovery
    prepare_stage(plan, stage)
    before = artifact_identity(plan.artifact.path)
    if before.digest != plan.fault_digest:
        raise RollbackActionError("failing artifact changed at mutator boundary")
    os.replace(stage, plan.artifact.path)
    fsync_directory(plan.artifact.path.parent)
    return {
        "receipt_kind": "mutator_boundary_v1",
        "original_effect": "write_generated_service_bindings_json",
        "inverse_operation": "atomic_restore_authenticated_before_image",
        "touched_set": list(plan.touched_set),
        "before": before.public(),
        "after": artifact_identity(plan.artifact.path).public(),
        "mutation_count": 1,
    }


def reconstruct_action_receipt(
    plan: RepairPlan,
    recovery: Path,
) -> dict[str, JsonValue]:
    """Authenticate an interrupted inverse from durable before/after state."""
    retained = artifact_identity(recovery)
    observed = artifact_identity(plan.artifact.path)
    if retained.digest != plan.fault_digest or retained.link_count != 1:
        raise RollbackActionError(
            "cannot reconstruct action receipt from an unauthenticated recovery generation"
        )
    if observed.digest != plan.expected_digest or observed.link_count != 1:
        raise RollbackActionError(
            "cannot reconstruct action receipt because the intended inverse is not present"
        )
    return {
        "receipt_kind": "reconstructed_from_durable_generation_and_subject_v1",
        "original_effect": "write_generated_service_bindings_json",
        "inverse_operation": "atomic_restore_authenticated_before_image",
        "touched_set": list(plan.touched_set),
        "before": retained.public(),
        "after": observed.public(),
        "recovery": pointer(recovery, plan.fault_digest),
        "mutation_count": 1,
    }


def prepare_stage(plan: RepairPlan, stage: Path) -> None:
    if stage.exists() or stage.is_symlink():
        staged = artifact_identity(stage)
        if staged.digest != plan.expected_digest or staged.link_count != 1:
            raise RollbackActionError("untrusted interrupted staging artifact")
        return
    source_fd = os.open(plan.restore_source.path, os.O_RDONLY | os.O_NOFOLLOW)
    stage_fd = os.open(
        stage,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        plan.expected_mode,
    )
    try:
        with os.fdopen(source_fd, "rb") as source, os.fdopen(stage_fd, "wb") as destination:
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        stage.unlink(missing_ok=True)
        raise
    os.chmod(stage, plan.expected_mode, follow_symlinks=False)
    if artifact_identity(stage).digest != plan.expected_digest:
        raise RollbackActionError("staged restore bytes failed authentication")


def verify_postconditions(plan: RepairPlan) -> dict[str, JsonValue]:
    observed = artifact_identity(plan.artifact.path)
    if observed.digest != plan.expected_digest:
        raise RollbackPostconditionError("independent artifact digest oracle remained negative")
    metadata = (observed.mode, observed.uid, observed.gid, observed.link_count)
    if metadata != (plan.expected_mode, plan.expected_uid, plan.expected_gid, 1):
        raise RollbackPostconditionError("artifact metadata or link oracle failed")
    try:
        validate_service_bindings(plan.artifact.path)
    except RepairRefusedError as exc:
        raise RollbackPostconditionError("restored artifact schema oracle failed") from exc
    collateral_after = collateral_census(plan.target, exclude=plan.artifact.path)
    if collateral_after != plan.collateral_before:
        raise RollbackPostconditionError("collateral target census changed")
    return {
        "oracle": "independent_filesystem_reopen_v1",
        "artifact_after": observed.public(),
        "diagnostic_status": "verified",
        "collateral_census_sha256": json_digest(collateral_after),
        "collateral_unchanged": True,
    }


def read_json_lines(path: Path) -> list[dict[str, JsonValue]]:
    if not path.exists():
        return []
    identity = artifact_identity(path)
    if identity.uid != os.getuid():
        raise RepairRefusedError(
            "repair_journal",
            "repair journal is not user-owned",
            evidence=str(path),
        )
    values: list[dict[str, JsonValue]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RepairRefusedError(
                "repair_journal",
                "repair journal contains a non-object",
                evidence=str(path),
            )
        values.append(value)
    return values


def latest_receipt(events: list[dict[str, JsonValue]]) -> dict[str, JsonValue] | None:
    for event in reversed(events):
        if event.get("status") == "action_receipt_recorded":
            evidence = event.get("evidence")
            return evidence if isinstance(evidence, dict) else None
    return None


def has_event(events: list[dict[str, JsonValue]], status: str) -> bool:
    return any(item.get("status") == status for item in events)


def has_terminal(events: list[dict[str, JsonValue]], status: str) -> bool:
    return bool(events) and events[-1].get("status") == status


def pointer(path: Path, digest: str) -> dict[str, JsonValue]:
    return {"path": str(path), "sha256": digest}


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
