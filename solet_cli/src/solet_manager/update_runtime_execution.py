"""Step-5 stage executor: re-entry invariant, bounded resume, runtime-axis publication.

Drives one approved runtime plan from ``runtime_planned`` to
``runtime_advanced`` (design section 9) and then, since Step 6, through the
final doctor and promotion to ``promoted`` (``update_promotion``).  Every ``*_applying`` status is
written and read back before the first adapter apply of its stage; every
``*_advanced`` only after the stage's postcondition probes verify.  Each
re-entry re-proves the exact N+1 source identity and re-probes the current
stage postcondition-first, so a crash at any boundary resumes without
guessing.  ``blocked`` and ``failed`` are the only terminal exits, and the
two frontiers (``source_advanced``, ``runtime_advanced``) are never
terminalised.

The Step-4 local-state commitment (section 9.2 step 3, landed by Step 7
section 6.6) is re-established by ``_verify_advanced`` on every re-entry: the
exact N+1 identity plus the recomputed local state equal to the journal's
``local_state.current`` -- the tracked digests, the committed-tier commitment
and an empty staged set -- with the preserved surface (``profile/config/**``)
disclosed rather than committed.  ``current`` moves only through a journaled
revision: a declared operation's re-baseline of its own targets, an
operation's preserved-surface disclosure, or the lifecycle stage's service
writes (the running solet's own ``knowledge_bases/`` symlink creation, B7).
Any other hard-tier change is ``preservation_violated`` (after an apply) or
``SourceTransitionIncompleteError`` (at re-entry).  The destination rules of
section 6.3 are re-run before the hydration stage's first apply.

The hydration stage lives in ``update_runtime_stages`` and the lifecycle and
post-runtime stages in ``update_runtime_lifecycle``; this module owns the
journal, the generic postcondition-first driver, and the stage loop.  No code
path here opens a database connection: platform state moves only through the
running N+1 service over the instance bridge.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from solet_setup_contracts import canonical_sha256

from . import update_promotion as promotion
from . import update_runtime_lifecycle as lifecycle_stage
from . import update_runtime_stages as stages
from .adapter_protocol import OperationResult
from .cutover_receipts import CutoverTerms
from .errors import (
    ManagerError,
    ProbeDriftError,
    StateError,
    UpdateBlockedError,
    UpdateFailedError,
)
from .existing_install_bundle import SYNTHESISED_OPERATION_TYPES, RuntimeOperation
from .existing_install_inspection import ExistingInstallInspectionResult
from .maintenance_inventory import publish_needs_attention, read_maintenance_inventory_v2
from .managed_artifact_backup import write_backup
from .models import (
    ActiveOperation,
    CheckpointStatus,
    CommandResult,
    ExitCode,
    InstanceInventoryRecordV2,
    JsonValue,
    MaintenanceOperationKind,
    ManagedArtifactState,
    OperationType,
    ReleaseIdentity,
    RuntimePlan,
)
from .paths import ManagerPaths
from .state_io import instance_lock
from .transaction import utc_now
from .update_journal import (
    DOCTOR_STATUSES,
    GUARDED_STATUSES,
    RUNTIME_STATUSES,
    TERMINAL_UPDATE_STATUSES,
    advance_update_journal,
    classify_v3_rows,
    operation_row,
    read_update_journal,
    record_local_state_revision,
    record_operation_attempt,
    record_runtime_approval,
    write_update_journal,
)
from .update_runtime_plan import (
    PlanContext,
    build_runtime_plan,
    operation_request,
    public_inputs_for,
)
from .update_runtime_plan_codec import plan_from_json, plan_to_json

if TYPE_CHECKING:
    from .update_execution import UpdateRequest, _Execution

__all__ = ["LIFECYCLE_CUTOVER_ID", "LIFECYCLE_RESTART_ID", "READINESS_ID", "RESULT_KIND", "RuntimeExecution", "StagePausedError"]

RESULT_KIND = "existing_install_update"
LIFECYCLE_CUTOVER_ID = "lifecycle_cutover"
LIFECYCLE_RESTART_ID = "lifecycle_restart_single_color"
READINESS_ID = "runtime_readiness"
_STAGES: tuple[tuple[str, str, str], ...] = (
    # (stage, applying status, advanced status)
    ("dependencies", "dependencies_applying", "dependencies_advanced"),
    ("migrations_pre", "migrations_applying", "migrations_advanced"),
    ("hydration", "hydration_applying", "hydration_advanced"),
    ("lifecycle", "lifecycle_applying", "lifecycle_advanced"),
    ("runtime_reconcile", "runtime_reconciling", "runtime_advanced"),
)
_ENTRY_STATUS = {"runtime_planned": 0, "dependencies_advanced": 1, "migrations_advanced": 2, "hydration_advanced": 3, "lifecycle_advanced": 4}
_APPLYING_STATUS = {applying: index for index, (_, applying, _) in enumerate(_STAGES)}
_REFUSALS = frozenset({CheckpointStatus.BLOCKED, CheckpointStatus.AWAITING_USER, CheckpointStatus.FAILED})
_EVIDENCE_PREFIXES = ("artifact.", "closure.", "environment.", "migration.", "cache.")
_MANUAL_TYPES = frozenset({OperationType.MANUAL_TARGET_MIGRATION.value, OperationType.MANUAL_ADDITIVE_PLATFORM_MIGRATION.value})
REPAIR_RECONCILE = "Run `solet-manager reconcile {name} --dry-run` to plan a successor operation."


class StagePausedError(ManagerError):
    """A retry-safe apply failed; the stage stays ``*_applying`` and the operator re-runs ``--yes``."""

    exit_code = 3

    def __init__(self, reason_code: str, message: str, *, repair: str) -> None:
        super().__init__(message, repair=repair)
        self.error_kind = reason_code


@dataclass
class RuntimeExecution:
    """Stage-driven Step-5 apply that continues from whatever the journal already proved."""

    paths: ManagerPaths
    record: InstanceInventoryRecordV2
    context: PlanContext
    journal_path: Path
    journal: dict[str, JsonValue]
    approved: str
    verify_advanced: Callable[[dict[str, JsonValue] | None], ExistingInstallInspectionResult]
    executed: list[str] = field(default_factory=lambda: [])
    plan: RuntimePlan | None = None
    request: UpdateRequest | None = None
    #: Step 7 section 6.6: the source execution that owns the local-state commitment (its re-entry report and the
    #: per-operation re-baseline); ``None`` only for a caller that never reaches a target write.
    source: _Execution | None = None

    @property
    def status(self) -> str:
        return cast(str, self.journal["status"])

    @property
    def operation_id(self) -> str:
        return cast(str, self.journal["operation_id"])

    def current_record(self) -> InstanceInventoryRecordV2:
        return self._current_record()

    def head(self) -> str:
        from .update_execution import target_head  # noqa: PLC0415 - the update module imports this one

        return target_head(self.record)

    def advance_status(self, status: str, note: str, *, result: dict[str, JsonValue] | None = None) -> None:
        """Public journal advance for the promotion tail (Step 6 section 5)."""
        self._write(advance_update_journal(self.journal, status=status, stage_id=status, note=note, result=result))

    def result_data(self) -> dict[str, JsonValue]:
        return self._result_data()

    # --- entry ------------------------------------------------------------------

    def run(self) -> CommandResult:
        try:
            return self._drive_to_promotion()
        except StagePausedError as exc:
            return CommandResult(RESULT_KIND, self.status, str(exc), ExitCode.HUMAN_ACTION, exc.error_kind, exc.repair, data=self._result_data())
        except UpdateFailedError as exc:
            return self._failure(exc, "failed")
        except ManagerError as exc:
            return self._failure(exc, "blocked")

    def _drive_to_promotion(self) -> CommandResult:
        if self.status == "source_advanced":
            self._plan_and_approve()
        self._classify_legacy_rows()
        if self.status == "runtime_planned":
            self._plan()
        while self.status not in TERMINAL_UPDATE_STATUSES and self.status != "runtime_advanced" and self.status not in DOCTOR_STATUSES:
            self._step()
        if self.status == "runtime_advanced" or self.status in DOCTOR_STATUSES:
            return promotion.finish(self)
        return self._result()

    def _failure(self, exc: ManagerError, kind: str) -> CommandResult:
        """A runtime-stage exception terminalises the journal and re-raises; a Step 6 phase exception is only reported (M6)."""
        if self.status in DOCTOR_STATUSES:
            return self._interrupted(exc)
        self._terminal(kind, exc.error_kind, str(exc), exc.repair)
        raise exc

    def _interrupted(self, exc: ManagerError) -> CommandResult:
        """Step 6 section 4.7 (M6): an exception in a read-only or Manager-state-only phase leaves the status where it is."""
        phase = "promotion_interrupted" if self.status == "promoting" else "doctor_interrupted"
        kind = exc.error_kind if exc.error_kind in {"transition_contract_mismatch", "corrupt_state"} else phase
        repair = f"Re-run `solet-manager update {self.record.name} --yes --approval-fingerprint <runtime fingerprint>`."
        return CommandResult(RESULT_KIND, self.status, f"{phase}: {exc}", ExitCode.HUMAN_ACTION, kind, repair, data=self._result_data())

    def _classify_legacy_rows(self) -> None:
        """A v3 journal's declared rows are typed once from the approved bundle and persisted (section 4.2)."""
        if self.journal["runtime_approval"] is None:
            return
        types = {item.operation_id: item.operation_type.value for item in self.context.candidate.bundle.runtime_operations}
        classified = classify_v3_rows(self.journal, types)
        if classified is not self.journal and classified != self.journal:
            self._write(classified)

    def _plan_and_approve(self) -> None:
        try:
            plan = build_runtime_plan(self.context)
        except UpdateBlockedError as exc:
            raise ProbeDriftError(
                f"the runtime plan cannot be reproduced under the lock ({exc.error_kind}): {exc}",
                repair="Run --dry-run again and approve the runtime fingerprint it renders.",
            ) from exc
        if plan.fingerprint is None or plan.fingerprint != self.approved:
            raise ProbeDriftError(
                "approved runtime fingerprint does not match the lock-time runtime plan",
                repair="Run --dry-run again and approve the runtime fingerprint it renders.",
            )
        self.plan = plan
        actions = tuple(f"{item.operation_id}:{action}" for item in plan.operations for action in item.planned_actions)
        next_value = record_runtime_approval(
            self.journal,
            fingerprint=plan.fingerprint,
            planned_actions=actions or ("lifecycle",),
            strategy=plan.lifecycle.strategy,
            forward_only_boundary=plan.forward_only_boundary,
            operations=tuple(self._operation_rows(plan)),
            note="runtime plan approved under the instance lock before any target write",
        )
        self._write(next_value)
        # The approved plan is journaled on the lifecycle row so a resume reads
        # the plan it approved instead of re-probing stages that already moved.
        self._record(self._lifecycle_row_id(), "manager", None, status=None, note={"approved_plan": plan_to_json(plan)})

    def _lifecycle_row_id(self) -> str:
        approval = cast(dict[str, JsonValue], self.journal["runtime_approval"])
        return LIFECYCLE_CUTOVER_ID if approval["strategy"] == "router_cutover" else LIFECYCLE_RESTART_ID

    def _operation_rows(self, plan: RuntimePlan) -> list[dict[str, JsonValue]]:
        declared = {item.operation_id: item for item in self.context.candidate.bundle.runtime_operations}
        rows: list[dict[str, JsonValue]] = [
            {
                "operation_id": item.operation_id,
                "operation_ref": item.operation_ref,
                "stage": item.stage,
                "status": "pending" if item.applies else "not_applicable",
                "idempotency_key": item.idempotency_key,
                "mutation_class": item.mutation_class,
                "rollback_class": item.rollback_class,
                "operation_type": declared[item.operation_id].operation_type.value,
            }
            for item in plan.operations
        ]
        router = plan.lifecycle.strategy == "router_cutover"
        rows.append(self._synth_row(LIFECYCLE_CUTOVER_ID if router else LIFECYCLE_RESTART_ID, "existing::lifecycle.cutover" if router else "existing::lifecycle.restart_single_color", "lifecycle", "process_lifecycle", "runtime_previous_release"))
        rows.append(self._synth_row(READINESS_ID, "existing::runtime.readiness", "lifecycle", "process_lifecycle", "reversible"))
        for index, kb in enumerate(self.context.candidate.bundle.knowledge_removals):
            rows.append(self._synth_row(lifecycle_stage.knowledge_row_id(index, kb), "existing::runtime.knowledge_reinstall", "runtime_reconcile", "knowledge_index", "reversible"))
        return rows

    def _synth_row(self, operation_id: str, ref: str, stage: str, mutation: str, rollback: str) -> dict[str, JsonValue]:
        """Synthesised rows are typed at synthesis (section 4.2, M1), never classified by ref later."""
        key = canonical_sha256([self.context.candidate.fields.commit, operation_id, self.record.instance_id])
        return {"operation_id": operation_id, "operation_ref": ref, "stage": stage, "status": "pending", "idempotency_key": key, "mutation_class": mutation, "rollback_class": rollback, "operation_type": SYNTHESISED_OPERATION_TYPES[ref].value}

    def operation_type(self, operation_id: str) -> str:
        """The row's journaled resume-rule type; the executor dispatches on this and nothing else."""
        return cast(str, self._row(operation_id)["operation_type"])

    # --- stage loop ---------------------------------------------------------------

    def _step(self) -> None:
        if self.status in _ENTRY_STATUS:
            index = _ENTRY_STATUS[self.status]
        elif self.status in _APPLYING_STATUS:
            index = _APPLYING_STATUS[self.status]
        else:
            raise StateError(f"update journal status {self.status!r} is not a Step-5 stage boundary")
        stage, applying, advanced = _STAGES[index]
        self._reenter()
        if self.status != applying:
            self._advance(applying, f"{stage} stage entered after re-entry invariant")
        runner: dict[str, Callable[[], None]] = {
            "dependencies": self._dependencies,
            "migrations_pre": self._migrations,
            "hydration": self._hydration,
            "lifecycle": self._lifecycle,
            "runtime_reconcile": self._runtime_reconcile,
        }
        runner[stage]()
        if advanced == "runtime_advanced":
            self._record(self._lifecycle_row_id(), "manager", None, status=None, note={"mutation_counters": self.mutation_counters()})
        self._advance(advanced, f"{stage} stage postconditions verified")

    def _reenter(self) -> None:
        """Section 9.2: exact N+1 identity, clean tree, same pointer, same approval."""
        approval = cast(dict[str, JsonValue], self.journal["runtime_approval"])
        if approval["fingerprint"] != self.approved:
            raise ProbeDriftError("approved fingerprint does not match the recorded runtime approval")
        current = self._current_record()
        if current.active_operation != ActiveOperation(MaintenanceOperationKind.UPDATE, self.operation_id):
            # Section 4.1 (m2): an orphaned nonterminal journal is corrupt state, never reconstructed from target bytes.
            raise StateError("the inventory no longer points at this update; the journal is orphaned")
        self.record = current
        self.verify_advanced(self.journal)
        self._record_service_writes()

    def _record_service_writes(self) -> None:
        """Section 6.6: what the running solet wrote across the stage just left is a disclosed ``service_writes`` revision."""
        if self.source is None or self.source.local_state_report is None:
            return
        report = self.source.local_state_report
        if not report.additions and not report.delta.surface:
            return
        revision: dict[str, JsonValue] = {"stage": self._stage_just_left(), "service_writes": {"committed_additions": list(report.additions), "preserved_surface_delta": list(report.delta.surface)}}
        self._write(record_local_state_revision(self.journal, revision=revision, current=report.observed.snapshot()))

    def _stage_just_left(self) -> str:
        """The stage whose span the re-entry observation covers: the one before an entry status, the one in progress on a mid-stage resume."""
        if self.status in _ENTRY_STATUS:
            index = _ENTRY_STATUS[self.status]
            return "source" if index == 0 else _STAGES[index - 1][0]
        return _STAGES[_APPLYING_STATUS[self.status]][0]

    def _rebaseline(self, operation: RuntimeOperation) -> None:
        """Section 6.6: after an apply verifies, only the operation's declared in-tree targets may have moved."""
        if self.source is None:
            raise StateError("the executor carries no source execution; the local-state commitment cannot be re-baselined")
        outcome = self.source.rebaseline_local_state(self.journal, operation.operation_id, self._declared_targets(operation))
        if outcome is None:
            return
        revision, current = outcome
        self._write(record_local_state_revision(self.journal, revision=revision, current=current))

    def _declared_targets(self, operation: RuntimeOperation) -> frozenset[str]:
        """Every in-tree ``planned_targets`` path the operation declared, relative to the target root."""
        target = Path(self.record.target.canonical_path)
        declared: set[str] = set()
        plan = self.plan
        if plan is not None:
            for item in plan.operations:
                if item.operation_id == operation.operation_id:
                    declared.update(item.planned_targets)
        for attempt in cast(list[JsonValue], self._row(operation.operation_id)["attempts"]):
            evidence = cast(dict[str, JsonValue], cast(dict[str, JsonValue], attempt)["evidence"])
            declared.update(str(item) for item in cast(list[JsonValue], evidence.get("planned_targets", [])))
        relative: set[str] = set()
        for raw in declared:
            path = Path(raw)
            if not path.is_absolute():
                continue
            try:
                relative.add(str(path.relative_to(target)))
            except ValueError:
                continue
        return frozenset(relative)

    def _current_record(self) -> InstanceInventoryRecordV2:
        records = read_maintenance_inventory_v2(self.paths.maintenance_inventory_path)
        current = next((item for item in records if item.instance_id == self.record.instance_id), None)
        if current is None:
            raise StateError("maintenance inventory lost the instance row")
        return current

    # --- stage bodies (delegates keep one patch point per stage) --------------------

    def _dependencies(self) -> None:
        for operation in self._stage_operations("dependencies"):
            self._drive(operation, self._inputs(operation), contradiction="dependency_postcondition_contradiction", incomplete="dependency_incomplete")

    def _migrations(self) -> None:
        for operation in self._stage_operations("migrations_pre"):
            self._drive(operation, self._inputs(operation), contradiction="migration_postcondition_contradiction", incomplete="migration_incomplete")

    def _hydration(self) -> None:
        stages.hydration_stage(self)

    def _hydrate_one(self, operation: RuntimeOperation, inputs: dict[str, JsonValue], artifact_id: str, plan: RuntimePlan) -> str | None:
        return stages.hydrate_one(self, operation, inputs, artifact_id, plan)

    def _artifact_state(self, operation: RuntimeOperation, probe: OperationResult, artifact_id: str, plan: RuntimePlan) -> ManagedArtifactState:
        return stages.artifact_state(self, operation, probe, artifact_id, plan)

    def _require_plist_expected(self, plan: RuntimePlan) -> None:
        stages.require_plist_expected(plan)

    def _lifecycle(self) -> None:
        lifecycle_stage.lifecycle_stage(self)

    def _dispatch_cutover(self, plan: RuntimePlan, terms: CutoverTerms, phase: str, reconciliation_id: str | None = None) -> None:
        lifecycle_stage.dispatch_cutover(self, plan, terms, phase, reconciliation_id)

    def _classify_cutover(self, status: str, error_kind: str | None, message: str | None, cutover: dict[str, JsonValue] | None, rec: str) -> None:
        lifecycle_stage.classify_cutover(self, status, error_kind, message, cutover, rec)

    def _attest_and_publish(self, plan: RuntimePlan) -> None:
        lifecycle_stage.attest_and_publish(self, plan)

    def _runtime_reconcile(self) -> None:
        lifecycle_stage.runtime_reconcile_stage(self)

    # --- generic postcondition-first operation driver (section 5.2) -------------------

    def _stage_operations(self, stage: str) -> list[RuntimeOperation]:
        return [item for item in self.context.candidate.bundle.operations_in_stage(stage) if self._row(item.operation_id)["status"] != "not_applicable"]

    def _row(self, operation_id: str) -> dict[str, JsonValue]:
        row = operation_row(self.journal, operation_id)
        if row is None:
            raise StateError(f"update journal has no runtime operation {operation_id!r}")
        return row

    def _prior_applying(self, operation_id: str) -> bool:
        return any(cast(dict[str, JsonValue], item)["phase"] == "apply" for item in cast(list[JsonValue], self._row(operation_id)["attempts"]))

    def _probe(self, operation: RuntimeOperation, purpose: str, inputs: dict[str, JsonValue]) -> OperationResult:
        request = operation_request(self.context, operation, phase="probe", probe_purpose=purpose, approval_fingerprint=None, attempt=self._attempt_number(operation.operation_id), public_inputs=inputs)
        result = self.context.seams.invoke_adapter(self.context.registry, request)
        self.executed.append(f"probe:{operation.operation_id}:{purpose}")
        self._record(operation.operation_id, "probe", result, status=None)
        return result

    def _apply(self, operation: RuntimeOperation, inputs: dict[str, JsonValue]) -> OperationResult:
        request = operation_request(self.context, operation, phase="apply", probe_purpose=None, approval_fingerprint=self.approved, attempt=self._attempt_number(operation.operation_id), public_inputs=inputs)
        self._record(operation.operation_id, "apply", None, status="applying", note={"request_id": request.request_id})
        result = self.context.seams.invoke_adapter(self.context.registry, request)
        self.executed.append(f"apply:{operation.operation_id}")
        self._record(operation.operation_id, "apply", result, status="applied" if result.checkpoint_status is CheckpointStatus.APPLIED else None)
        return result

    def _attempt_number(self, operation_id: str) -> int:
        return len(cast(list[JsonValue], self._row(operation_id)["attempts"])) + 1

    def _record(self, operation_id: str, phase: str, result: OperationResult | None, *, status: str | None, note: dict[str, JsonValue] | None = None) -> None:
        row = self._row(operation_id)
        evidence: dict[str, JsonValue] = dict(note or {})
        checkpoint = "requested"
        error_kind: str | None = None
        if result is not None:
            checkpoint = result.checkpoint_status.value
            error_kind = result.error_kind
            evidence.update(_result_evidence(result))
        next_value = record_operation_attempt(self.journal, operation_id, phase=phase, checkpoint_status=checkpoint, status=status or cast(str, row["status"]), evidence=evidence, error_kind=error_kind)
        self._write(next_value)

    def _drive(self, operation: RuntimeOperation, inputs: dict[str, JsonValue], *, contradiction: str, incomplete: str) -> None:
        """Section 5.2 truth table, applied to every stage's operations."""
        if self._verified_by_postcondition(operation, inputs, contradiction, incomplete):
            return
        pre = self._probe(operation, "pre_apply", inputs)
        if pre.checkpoint_status in _REFUSALS:
            raise UpdateBlockedError(cast(str, pre.error_kind), f"{operation.operation_id} refused before apply: {pre.repair}", repair=pre.repair or self._reconcile_repair())
        if self.operation_type(operation.operation_id) == OperationType.BACKED_UP_ARTIFACT.value:
            stages.require_backups_on_reentry(self, operation, pre)
            self._backup_targets(operation, pre)
        self._apply_and_verify(operation, inputs, contradiction)

    def _reconcile_repair(self) -> str:
        return REPAIR_RECONCILE.format(name=self.record.name)

    def _verified_by_postcondition(self, operation: RuntimeOperation, inputs: dict[str, JsonValue], contradiction: str, incomplete: str) -> bool:
        probe = self._probe(operation, "post_apply", inputs)
        if probe.checkpoint_status is CheckpointStatus.VERIFIED:
            # A row that verifies by probe may have applied before a crash (section 6.6's crash window): its declared
            # writes are re-baselined here exactly as the apply path does.
            self._rebaseline(operation)
            self._set_status(operation.operation_id, "verified_by_probe")
            return True
        if probe.checkpoint_status is CheckpointStatus.FAILED:
            raise UpdateFailedError(contradiction, f"{operation.operation_id} postcondition probe reported contradictory evidence", repair=f"Retain all evidence; {self._reconcile_repair()}")
        if probe.checkpoint_status in {CheckpointStatus.BLOCKED, CheckpointStatus.AWAITING_USER}:
            raise UpdateBlockedError(cast(str, probe.error_kind), f"{operation.operation_id} is blocked: {probe.repair}", repair=probe.repair or self._reconcile_repair())
        if self._prior_applying(operation.operation_id) and self.operation_type(operation.operation_id) in _MANUAL_TYPES:
            # T3/T3b (section 4.2): the Manager never reapplies a manual row; the successor plans it as operator_confirmation.
            raise UpdateBlockedError(incomplete, f"{operation.operation_id} was applied once and its postcondition still fails; manual retry policy", repair=f"Inspect the evidence for {operation.operation_id}; {self._reconcile_repair()}")
        return False

    def _apply_and_verify(self, operation: RuntimeOperation, inputs: dict[str, JsonValue], contradiction: str) -> None:
        applied = self._apply(operation, inputs)
        if applied.checkpoint_status is CheckpointStatus.FAILED and applied.retry_safe:
            self._set_status(operation.operation_id, "pending")
            raise StagePausedError(cast(str, applied.error_kind), f"{operation.operation_id} apply failed and is retry-safe; re-run --yes to reapply what is still missing", repair=applied.repair or "Re-run --yes with the recorded runtime fingerprint.")
        if applied.checkpoint_status in _REFUSALS:
            raise UpdateBlockedError(cast(str, applied.error_kind), f"{operation.operation_id} apply was refused", repair=applied.repair or self._reconcile_repair())
        post = self._probe(operation, "post_apply", inputs)
        if post.checkpoint_status is not CheckpointStatus.VERIFIED:
            raise UpdateFailedError(contradiction, f"{operation.operation_id} apply reported success but its postcondition does not verify", repair=f"Retain all evidence; {self._reconcile_repair()}")
        self._rebaseline(operation)
        self._set_status(operation.operation_id, "verified")

    def _backup_targets(self, operation: RuntimeOperation, probe: OperationResult) -> None:
        for index, action in enumerate(probe.planned_actions):
            target = Path(action.target)
            if not target.is_absolute():
                continue
            record = write_backup(self.paths, self.record.instance_id, self.operation_id, f"{operation.operation_id}.{index}", target)
            self._record(operation.operation_id, "manager", None, status=None, note={"backup": record.to_dict()})

    def _set_status(self, operation_id: str, status: str) -> None:
        self._record(operation_id, "manager", None, status=status, note={"status": status})

    def _inputs(self, operation: RuntimeOperation) -> dict[str, JsonValue]:
        plan = self._plan()
        artifacts = tuple((self.context.candidate.bundle.artifact(state.artifact_id), state.destination) for state in plan.managed_artifacts)
        return public_inputs_for(self.context, operation, plan.declared_closure, artifacts)

    def _plan(self) -> RuntimePlan:
        """The approved plan: in memory on a fresh approval, from the journal on resume."""
        if self.plan is None:
            row = self._row(self._lifecycle_row_id())
            for item in cast(list[JsonValue], row["attempts"]):
                evidence = cast(dict[str, JsonValue], cast(dict[str, JsonValue], item)["evidence"])
                approved = evidence.get("approved_plan")
                if isinstance(approved, dict):
                    plan = plan_from_json(approved)
                    if plan.fingerprint != self.approved:
                        raise StateError("the journaled runtime plan does not carry the recorded approval fingerprint")
                    self.plan = plan
                    break
            if self.plan is None:
                self.plan = self._replan_after_approval_crash()
        return self.plan

    def _replan_after_approval_crash(self) -> RuntimePlan:
        """A crash between the approval write and the approved-plan evidence write: rebuild and re-prove the plan.

        Legal only while no stage has started (``runtime_planned``); the plan must
        reproduce the recorded runtime fingerprint exactly, then it is journaled.
        """
        if self.status != "runtime_planned":
            raise StateError("the update journal carries a runtime approval without its approved plan")
        try:
            plan = build_runtime_plan(self.context)
        except UpdateBlockedError as exc:
            raise ProbeDriftError(f"the approved runtime plan cannot be reproduced ({exc.error_kind}): {exc}", repair="Run --dry-run again and approve the runtime fingerprint it renders.") from exc
        if plan.fingerprint != self.approved:
            raise ProbeDriftError("the rebuilt runtime plan does not reproduce the recorded runtime approval", repair="Run --dry-run again and approve the runtime fingerprint it renders.")
        self.plan = plan
        self._record(self._lifecycle_row_id(), "manager", None, status=None, note={"approved_plan": plan_to_json(plan), "rebuilt_after_crash": True})
        return plan

    # --- journal plumbing -------------------------------------------------------------

    def _advance(self, status: str, note: str) -> None:
        next_value = advance_update_journal(self.journal, status=status, stage_id=status, note=note)
        self._write(next_value)

    def _write(self, next_value: dict[str, JsonValue]) -> None:
        write_update_journal(self.journal_path, self.journal, next_value)
        self.journal = next_value

    def _terminal(self, kind: str, reason_code: str, message: str, repair: str | None) -> None:
        """Terminalise a runtime stage; never a frontier, a terminal, or a Step 6 read-only phase (M6)."""
        if self.status in GUARDED_STATUSES:
            return
        past_source = self.status in RUNTIME_STATUSES
        result: dict[str, JsonValue] = {"kind": kind, "reason_code": reason_code, "repair": repair or self._reconcile_repair()}
        next_value = advance_update_journal(self.journal, status=kind, stage_id=kind, note=message[:200], result=result)
        self._write(next_value)
        if past_source:
            self.publish_needs_attention((reason_code,))

    def publish_needs_attention(self, reason_codes: tuple[str, ...]) -> None:
        """Step 6 section 5.4 (D8): the inventory is truthful without a doctor run."""
        current = self._current_record()
        with instance_lock(self.paths.registry_lock_path, create=True):
            self.record = publish_needs_attention(self.paths.maintenance_inventory_path, current, reason_codes=reason_codes, now=utc_now())
        self.executed.append("inventory:publish_needs_attention")

    def mutation_counters(self) -> dict[str, JsonValue]:
        """Counted from what this process executed; journaled so a sweep can assert 'no mutation repeated'."""
        counters: dict[str, int] = {}
        for item in self.executed:
            key = item.split(":", 1)[0]
            if key in {"apply", "launchctl", "bridge", "cutover", "inventory"}:
                counters[key] = counters.get(key, 0) + 1
        return cast(dict[str, JsonValue], dict(sorted(counters.items())))

    def _result(self) -> CommandResult:
        if read_update_journal(self.journal_path) != self.journal:
            raise StateError("update journal read-back mismatch")
        return CommandResult(
            RESULT_KIND,
            self.status,
            "Runtime transitioned to the exact candidate; the final doctor and promotion follow on the next --yes.",
            ExitCode.OK,
            data=self._result_data(),
        )

    def _result_data(self) -> dict[str, JsonValue]:
        record = self.record
        return {
            "instance_id": record.instance_id,
            "operation_id": self.operation_id,
            "journal_status": self.status,
            "management_state": record.management_state.value,
            "update_eligibility": {"state": record.update_eligibility.state.value, "reason_codes": list(record.update_eligibility.reason_codes)},
            "source_release": _release_dict(record.source_release),
            "runtime_release": _release_dict(record.runtime_release),
            "verified_release": _release_dict(record.verified_release),
            "source_contract_digest": record.contract_identities.source_contract_digest,
            "runtime_contract_digest": record.contract_identities.runtime_contract_digest,
            "verified_contract_digest": record.contract_identities.verified_contract_digest,
            "last_verified_operation_id": record.last_verified_operation_id,
            "target_actions_executed": list(self.executed),
            "mutation_counters": self.mutation_counters(),
            "source_mode": self.journal["source_mode"],
            "recovers": self.journal["recovers"],
            "runtime_operations": [
                {"operation_id": cast(dict[str, JsonValue], row)["operation_id"], "status": cast(dict[str, JsonValue], row)["status"]}
                for row in cast(list[JsonValue], self.journal["runtime_operations"])
            ],
            "router_previous_is_code_only": True,
        }


def _result_evidence(result: OperationResult) -> dict[str, JsonValue]:
    return {
        "request_id": result.request_id,
        "probe_purpose": result.probe_purpose,
        "planned_actions": [action.id for action in result.planned_actions],
        "planned_targets": [action.target for action in result.planned_actions],
        "evidence_ids": [str(item["id"]) for item in result.evidence],
        "facts": [item["observed"] for item in result.evidence if str(item["id"]).startswith(_EVIDENCE_PREFIXES)],
    }


def _release_dict(release: ReleaseIdentity | None) -> dict[str, JsonValue] | None:
    if release is None:
        return None
    return {"repository": release.repository, "commit": release.commit, "tree": release.tree, "tag": release.tag}
