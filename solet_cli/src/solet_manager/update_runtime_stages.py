"""Hydration stage of the Step-5 executor (design sections 6.2-6.5, 9.3).

Each declared managed artifact is probed, backed up, written, and re-probed
one at a time; a later artifact that conflicts after earlier writes restores
every earlier write byte-exact before the stage goes terminal ``blocked``.
The functions here take the executing :class:`RuntimeExecution` and are bound
to it through thin delegating methods so a smoke can patch one stage point.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from .adapter_protocol import OperationResult
from .errors import AdapterProtocolError, BackupMissingError, RestoreTargetDivergedError, UpdateBlockedError, UpdateFailedError
from .existing_install_bundle import RuntimeOperation
from .managed_artifact_backup import action_backup_key, backup_root, file_sha256, legacy_indexed_backup_covers, restore_artifact, write_backup
from .models import CheckpointStatus, JsonValue, ManagedArtifactState, RuntimePlan
from .update_runtime_plan import artifact_states

if TYPE_CHECKING:
    from .update_runtime_execution import RuntimeExecution

__all__ = ["artifact_state", "backup_targets", "hydrate_one", "hydration_stage", "require_backups_on_reentry", "require_plist_expected"]
_RECONCILE = "Run `solet-manager reconcile {name} --dry-run` to plan a successor operation."


def require_backups_on_reentry(execution: RuntimeExecution, operation: RuntimeOperation, probe: OperationResult) -> None:
    """Step 6 section 4.2 (T2): a row re-entering ``applying`` must still hold a backup for every planned absolute target."""
    row = execution._row(operation.operation_id)  # noqa: SLF001
    if row["status"] != "applying":
        return
    root = backup_root(execution.paths, execution.record.instance_id, execution.operation_id)
    for action in probe.planned_actions:
        target = Path(action.target)
        if not target.is_absolute():
            continue
        if (root / action_backup_key(operation.operation_id, action.id) / "before.json").is_file():
            continue
        if not legacy_indexed_backup_covers(execution.paths, execution.record.instance_id, execution.operation_id, operation.operation_id, target):
            raise BackupMissingError(f"{operation.operation_id} re-entered applying without its backup record for {action.target}", repair=_RECONCILE.format(name=execution.record.name))

def backup_targets(execution: RuntimeExecution, operation: RuntimeOperation, probe: OperationResult) -> None:
    """Capture each planned absolute target's entry state, keyed by action id, before apply."""
    for action in probe.planned_actions:
        target = Path(action.target)
        if not target.is_absolute():
            continue
        if legacy_indexed_backup_covers(execution.paths, execution.record.instance_id, execution.operation_id, operation.operation_id, target):
            continue  # an r56 Manager keyed this backup by list position; it is the true entry state, never re-capture over it
        record = write_backup(execution.paths, execution.record.instance_id, execution.operation_id, action_backup_key(operation.operation_id, action.id), target)
        execution._record(operation.operation_id, "manager", None, status=None, note={"backup": record.to_dict()})  # noqa: SLF001


_REFUSALS = frozenset({CheckpointStatus.BLOCKED, CheckpointStatus.AWAITING_USER, CheckpointStatus.FAILED})


def hydration_stage(execution: RuntimeExecution) -> None:
    plan = execution._plan()  # noqa: SLF001 - stage body of the executor
    written: list[tuple[str, str, str | None]] = []
    for operation in execution._stage_operations("hydration"):  # noqa: SLF001
        inputs = execution._inputs(operation)  # noqa: SLF001
        for artifact_id in cast(list[str], inputs.get("artifact_ids", [])):
            try:
                after = execution._hydrate_one(operation, inputs, artifact_id, plan)  # noqa: SLF001
            except UpdateBlockedError:
                _rollback_written(execution, written)
                raise
            if after is not None:
                written.append((operation.operation_id, artifact_id, after))
        execution._rebaseline(operation)  # noqa: SLF001
        execution._set_status(operation.operation_id, "verified")  # noqa: SLF001
    execution._require_plist_expected(plan)  # noqa: SLF001
    execution.verify_advanced(execution.journal)


def hydrate_one(execution: RuntimeExecution, operation: RuntimeOperation, inputs: dict[str, JsonValue], artifact_id: str, plan: RuntimePlan) -> str | None:
    """Probe, back up, write, and re-probe ONE artifact; returns its post-write digest or ``None`` when untouched."""
    one = _single_artifact_inputs(inputs, artifact_id)
    probe = execution._probe(operation, "pre_apply", one)  # noqa: SLF001
    state = execution._artifact_state(operation, probe, artifact_id, plan)  # noqa: SLF001
    if probe.checkpoint_status in _REFUSALS or state.conflict is not None:
        reason = state.conflict or cast(str, probe.error_kind)
        raise UpdateBlockedError(reason, f"managed artifact {artifact_id} at {state.destination}: {reason}", repair=probe.repair or "Resolve the conflict by hand, then preview again.")
    if state.action == "none" and probe.checkpoint_status is CheckpointStatus.VERIFIED:
        return None
    return _write_artifact(execution, operation, one, state, plan)


def _single_artifact_inputs(inputs: dict[str, JsonValue], artifact_id: str) -> dict[str, JsonValue]:
    one = dict(inputs)
    one["artifact_ids"] = [artifact_id]
    one["planned_destinations"] = [item for item in cast(list[str], inputs["planned_destinations"]) if item.startswith(f"{artifact_id}=")]
    return one


def _require_reviewed_adoption(state: ManagedArtifactState, plan: RuntimePlan, digest: str | None) -> None:
    """An adoption runs only over the bytes whose diff the approval showed; a plan journaled before r65 showed none, so it adopts nothing.

    ``digest`` is the digest of the bytes about to be replaced: the pre-apply probe's, and again the backup's, so a file that changed after the probe and before the backup stops the write.
    """
    approved = next(item for item in plan.managed_artifacts if item.artifact_id == state.artifact_id)
    if state.adopt_diff and digest != approved.current_sha256:
        raise UpdateBlockedError("probe_drift", f"managed artifact {state.artifact_id} at {state.destination} changed between approval and apply; nothing was written", repair="Inspect the file, then preview again.")


def _write_artifact(execution: RuntimeExecution, operation: RuntimeOperation, one: dict[str, JsonValue], state: ManagedArtifactState, plan: RuntimePlan) -> str:
    _require_reviewed_adoption(state, plan, state.current_sha256)
    destination = Path(state.destination)
    backup = write_backup(execution.paths, execution.record.instance_id, execution.operation_id, state.artifact_id, destination)
    _require_reviewed_adoption(state, plan, backup.sha256)
    execution._record(operation.operation_id, "manager", None, status=None, note={"artifact_id": state.artifact_id, "backup": backup.to_dict()})  # noqa: SLF001
    applied = execution._apply(operation, one)  # noqa: SLF001
    if applied.checkpoint_status is not CheckpointStatus.APPLIED:
        raise UpdateBlockedError(applied.error_kind or "hydration_apply_refused", f"managed artifact {state.artifact_id} apply was refused", repair=applied.repair or _RECONCILE.format(name=execution.record.name))
    post = execution._probe(operation, "post_apply", one)  # noqa: SLF001
    after_state = execution._artifact_state(operation, post, state.artifact_id, plan)  # noqa: SLF001
    after = file_sha256(destination)
    if post.checkpoint_status is not CheckpointStatus.VERIFIED or after != state.expected_sha256 or after_state.state not in {"stamped_current", "verified"}:
        raise UpdateFailedError("hydration_postcondition_contradiction", f"managed artifact {state.artifact_id} does not read back as the planned render", repair=f"Retain all evidence; {_RECONCILE.format(name=execution.record.name)}")
    execution._record(operation.operation_id, "manager", None, status=None, note={"artifact_id": state.artifact_id, "after_sha256": after, "before_sha256": backup.sha256})  # noqa: SLF001
    return cast(str, after)


def artifact_state(execution: RuntimeExecution, operation: RuntimeOperation, probe: OperationResult, artifact_id: str, plan: RuntimePlan) -> ManagedArtifactState:
    artifacts = tuple((execution.context.candidate.bundle.artifact(state.artifact_id), state.destination) for state in plan.managed_artifacts)
    for state in artifact_states(operation, probe, artifacts):
        if state.artifact_id == artifact_id:
            return state
    raise AdapterProtocolError(f"adapter probe did not report artifact {artifact_id}")


def _rollback_written(execution: RuntimeExecution, written: list[tuple[str, str, str | None]]) -> None:
    """Section 9.3: a later artifact blocked after earlier writes; restore the stage's entry state."""
    for operation_id, artifact_id, after in reversed(written):
        try:
            record = restore_artifact(execution.paths, execution.record.instance_id, execution.operation_id, artifact_id, expected_after_sha256=after)
        except RestoreTargetDivergedError as exc:
            raise UpdateBlockedError("hydration_partial_unrestorable", f"managed artifact {artifact_id} could not be restored: {exc}", repair=f"Inspect the destination by hand; {_RECONCILE.format(name=execution.record.name)}") from exc
        execution._record(operation_id, "restore", None, status=None, note={"artifact_id": artifact_id, "restored_to_sha256": record.sha256, "absent": record.absent})  # noqa: SLF001


def require_plist_expected(plan: RuntimePlan) -> None:
    expected = plan.lifecycle.plist_expected_sha256
    observed = file_sha256(Path(plan.lifecycle.plist_path))
    if expected is not None and observed != expected:
        raise UpdateFailedError("hydration_postcondition_contradiction", "the LaunchAgent plist does not digest to the approval-bound expectation", repair="Retain all evidence; run `solet-manager reconcile <name> --dry-run` to plan a successor operation.")
