"""Lifecycle and post-runtime stages of the Step-5 executor (design sections 7, 9.3).

The lifecycle transition is either the Manager-produced reconciliation
cutover through the blue-green router or an exact single-colour restart of
the instance LaunchAgent, followed in a fixed order by readiness, attestation
of the running process, and publication of the inventory's runtime axis; no
later step runs on the strength of a health signal alone.  The post-runtime
stage then runs release-declared platform migrations and knowledge
reinstalls through the running N+1 service over the instance bridge.  The
functions here take the executing :class:`RuntimeExecution`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .cutover_fingerprint import cutover_fingerprint
from .cutover_receipts import CutoverTerms, new_reconciliation_id, terminal_receipt_digest
from .errors import (
    AdapterError,
    AdapterProtocolError,
    ManagedIdentityDriftError,
    StateError,
    UpdateBlockedError,
    UpdateFailedError,
)
from .existing_install_adapters import KNOWLEDGE_SEARCH_PROCESS_KEY
from .existing_install_bundle import RuntimeOperation
from .launch_topology import derive_launch_topology, plist_sha256
from .maintenance_inventory import publish_runtime_advance
from .managed_artifact_backup import file_sha256
from .models import JsonValue, OperationType, ReleaseIdentity, RuntimePlan
from .reconciliation_request import build_reconciliation_envelope
from .state_io import instance_lock
from .transaction import utc_now
from .update_runtime_plan import attest, cutover_terms

if TYPE_CHECKING:
    from .update_runtime_execution import RuntimeExecution

__all__ = [
    "attest_and_publish",
    "classify_cutover",
    "dispatch_cutover",
    "knowledge_row_id",
    "lifecycle_stage",
    "runtime_reconcile_stage",
]

LIFECYCLE_CUTOVER_ID = "lifecycle_cutover"
LIFECYCLE_RESTART_ID = "lifecycle_restart_single_color"
READINESS_ID = "runtime_readiness"
KNOWLEDGE_INSTALL_KEY = "service_interface::knowledge_service::install"
_SUCCESS_CUTOVER = frozenset({"reconciled", "already_reconciled"})
_CANDIDATE_FAILED_CUTOVER = frozenset({"failed_prior_serving", "compensated_prior_verified"})
_POLL_SECONDS = 1.0
_RECONCILE = "Run `solet-manager reconcile {name} --dry-run` to plan a successor operation."
_CODE_ONLY = "the router's previous release is a code-only rollback; platform state is not rolled back"


def _reconcile(execution: RuntimeExecution) -> str:
    return _RECONCILE.format(name=execution.record.name)


def knowledge_search_hits(data: dict[str, JsonValue], removed_path: str) -> bool:
    """True when any hit in a ``knowledge_service::search`` result cites the removed article's path."""
    return removed_path in json.dumps(data, sort_keys=True)


def knowledge_row_id(index: int, knowledge_base: str) -> str:
    return f"knowledge_reinstall_{index}_{knowledge_base}".replace(".", "_")


# --- lifecycle stage -----------------------------------------------------------------


def lifecycle_stage(execution: RuntimeExecution) -> None:
    plan = execution._plan()  # noqa: SLF001 - stage body of the executor
    lifecycle = plan.lifecycle
    observed = file_sha256(Path(lifecycle.plist_path))
    if lifecycle.plist_expected_sha256 is not None and observed != lifecycle.plist_expected_sha256:
        raise UpdateBlockedError("probe_drift", "the LaunchAgent plist changed between hydration_advanced and lifecycle entry; nothing was spawned", repair="Inspect the plist; preview again.")
    if lifecycle.strategy == "router_cutover":
        _router_cutover(execution, plan)
    else:
        _single_color_restart(execution, plan)
    _readiness(execution, plan)
    execution._attest_and_publish(plan)  # noqa: SLF001


def _router_cutover(execution: RuntimeExecution, plan: RuntimePlan) -> None:
    row = execution._row(LIFECYCLE_CUTOVER_ID)  # noqa: SLF001
    if row["status"] in {"applied", "verified"}:
        return
    if row["status"] in {"failed", "blocked"}:
        # Step 6 (M5): the controller's outcome is already journaled on the row; a crash before the
        # terminal write must not re-dispatch -- re-raise the recorded terminal instead.
        _reraise_recorded_cutover(execution, row)
    pending = _pending_reconciliation(row)
    if pending is not None and _recover(execution, plan, pending):
        return
    attestation = attest(execution.context, new_reconciliation_id())
    execution.executed.append("attest:lifecycle_entry")
    if attestation.current_release_id != plan.lifecycle.current_release_id:
        raise ManagedIdentityDriftError("the running release at lifecycle entry is not the approval-bound runtime baseline", repair=f"Something other than this update moved the runtime; inspect with `solet-manager doctor {execution.record.name}`.")
    raw = Path(plan.lifecycle.plist_path).read_bytes()
    terms = cutover_terms(attestation, cast(str, plan.lifecycle.launch_topology), plan.lifecycle.launchagent_label, plist_sha256(raw), cast(str, plan.lifecycle.adapter_module_sha256), plan.lifecycle.adapter_module_replaced, plan.lifecycle.verification_modules)
    execution._dispatch_cutover(plan, terms, "apply")  # noqa: SLF001


def dispatch_cutover(execution: RuntimeExecution, plan: RuntimePlan, terms: CutoverTerms, phase: str, reconciliation_id: str | None = None) -> None:
    record = execution.record
    fingerprint = cutover_fingerprint(name=record.name, target=record.target.canonical_path, seed=_seed_identity(execution), files=_cutover_files(execution), terms=terms)
    rec = reconciliation_id or new_reconciliation_id()
    envelope = build_reconciliation_envelope(phase=phase, name=record.name, target_realpath=record.target.canonical_path, reconciliation_id=rec, approved_fingerprint=fingerprint, terms=terms, timeout_seconds=plan.lifecycle.readiness_budget_seconds)
    execution._record(LIFECYCLE_CUTOVER_ID, "apply" if phase == "apply" else "recover", None, status="applying", note={"reconciliation_id": rec, "cutover_fingerprint": fingerprint, "terms": terms.to_dict(), "phase": phase})  # noqa: SLF001
    outcome = execution.context.seams.invoke_reconciliation(execution.context.registry, envelope, plan.lifecycle.readiness_budget_seconds)
    execution.executed.append(f"cutover:{phase}:{rec}")
    execution._classify_cutover(outcome.status, outcome.error_kind, outcome.message, outcome.cutover, rec)  # noqa: SLF001


def classify_cutover(execution: RuntimeExecution, status: str, error_kind: str | None, message: str | None, cutover: dict[str, JsonValue] | None, rec: str) -> None:
    if status != "ok":
        execution._record(LIFECYCLE_CUTOVER_ID, "manager", None, status="blocked", note={"reconciliation_id": rec, "error_kind": error_kind, "message": message})  # noqa: SLF001
        raise UpdateBlockedError(cast(str, error_kind), f"the target refused the cutover before any byte changed: {message}", repair="Inspect the receipt; preview again.")
    if cutover is None:
        raise AdapterProtocolError("the cutover apply returned no controller outcome")
    outcome_status = str(cutover.get("status", ""))
    evidence: dict[str, JsonValue] = {"reconciliation_id": rec, "outcome": cutover, "receipt_sha256": terminal_receipt_digest(Path(execution.record.target.canonical_path), rec)}
    if outcome_status in _SUCCESS_CUTOVER:
        execution._record(LIFECYCLE_CUTOVER_ID, "manager", None, status="applied", note=evidence)  # noqa: SLF001
        return
    if outcome_status in _CANDIDATE_FAILED_CUTOVER:
        execution._record(LIFECYCLE_CUTOVER_ID, "manager", None, status="failed", note=evidence)  # noqa: SLF001
        raise UpdateFailedError("runtime_candidate_failed", f"the router kept or restored the prior release; the runtime axis stays at the baseline and the source stays at N+1 ({_CODE_ONLY})", repair=f"Inspect the router evidence; {_reconcile(execution)}")
    execution._record(LIFECYCLE_CUTOVER_ID, "manager", None, status="blocked", note=evidence)  # noqa: SLF001
    raise UpdateBlockedError(outcome_status or "cutover_unresolved", f"the cutover ended in {outcome_status!r}", repair=f"Inspect the durable cutover receipt; {_reconcile(execution)}")


def _reraise_recorded_cutover(execution: RuntimeExecution, row: dict[str, JsonValue]) -> None:
    latest = cast(dict[str, JsonValue], cast(list[JsonValue], row["attempts"])[-1])
    evidence = cast(dict[str, JsonValue], latest["evidence"])
    if row["status"] == "failed":
        raise UpdateFailedError("runtime_candidate_failed", f"the router kept or restored the prior release (recorded outcome, no second apply); {_CODE_ONLY}", repair=f"Inspect the router evidence; {_reconcile(execution)}")
    reason = evidence.get("error_kind")
    outcome = evidence.get("outcome")
    status = str(cast(dict[str, JsonValue], outcome).get("status", "")) if isinstance(outcome, dict) else ""
    raise UpdateBlockedError(reason if isinstance(reason, str) else (status or "cutover_unresolved"), "the cutover was refused or unresolved (recorded outcome, no second apply)", repair=f"Inspect the durable cutover receipt; {_reconcile(execution)}")


def _pending_reconciliation(row: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    if row["status"] != "applying":
        return None
    for item in reversed(cast(list[JsonValue], row["attempts"])):
        attempt = cast(dict[str, JsonValue], item)
        evidence = cast(dict[str, JsonValue], attempt["evidence"])
        if attempt["phase"] in {"apply", "recover"} and "reconciliation_id" in evidence and "terms" in evidence:
            return evidence
    return None


def _recover(execution: RuntimeExecution, plan: RuntimePlan, evidence: dict[str, JsonValue]) -> bool:
    """Section 9.3 ``recover`` row: the controller's own durable outcome decides.

    Step 6 (M5): a recovered ``failed_prior_serving`` is the same terminal the
    apply would have reached -- exactly one ``rec_`` apply and one recover, never
    a fresh apply built on the strength of the prior still serving.
    """
    terms = _terms_from_dict(cast(dict[str, JsonValue], evidence["terms"]))
    rec = cast(str, evidence["reconciliation_id"])
    fingerprint = cast(str, evidence["cutover_fingerprint"])
    envelope = build_reconciliation_envelope(phase="recover", name=execution.record.name, target_realpath=execution.record.target.canonical_path, reconciliation_id=rec, approved_fingerprint=fingerprint, terms=terms, timeout_seconds=plan.lifecycle.readiness_budget_seconds)
    outcome = execution.context.seams.invoke_reconciliation(execution.context.registry, envelope, plan.lifecycle.readiness_budget_seconds)
    execution.executed.append(f"cutover:recover:{rec}")
    execution._classify_cutover(outcome.status, outcome.error_kind, outcome.message, outcome.cutover, rec)  # noqa: SLF001
    return True


def _single_color_restart(execution: RuntimeExecution, plan: RuntimePlan) -> None:
    row = execution._row(LIFECYCLE_RESTART_ID)  # noqa: SLF001
    if row["status"] in {"applied", "verified"}:
        return
    label = plan.lifecycle.launchagent_label
    domain = f"gui/{execution.context.seams.uid}"
    if row["status"] in {"applying", "failed", "blocked"}:
        # A row past its first invocation (including one whose terminal write a crash skipped) is
        # inspected before any second invocation (G6).
        _resume_single_color_restart(execution, plan, domain, label, _pid_before(row))
        return
    before = _pre_transition_pid(plan)
    if before is None:
        before = _launchctl_print(execution, domain, label)
    execution._record(LIFECYCLE_RESTART_ID, "apply", None, status="applying", note={"label": label, "pid_before": before})  # noqa: SLF001
    execution.context.seams.launchctl(execution.context.registry, "bootout", (f"{domain}/{label}",), 60)
    execution.executed.append("launchctl:bootout")
    _wait_until_unloaded(execution, plan, domain, label)
    _bootstrap(execution, plan, domain, label, before)


def _pre_transition_pid(plan: RuntimePlan) -> int | None:
    """Step 7 section 7.3: the pid the approved plan observed before fixing the strategy, when it carries one."""
    observation = plan.lifecycle.pre_transition
    if observation is None:
        return None
    pid = observation.get("pid")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def _resume_single_color_restart(execution: RuntimeExecution, plan: RuntimePlan, domain: str, label: str, before: int | None) -> None:
    """Step 6 section 4.1 (G6): inspect launchd before any second invocation.

    Not loaded -> ``bootstrap`` only; loaded with a fresh pid -> the restart
    happened, go to readiness; loaded with the pre-restart pid -> the restart
    never happened, ``bootout`` + ``bootstrap``.
    """
    current = _launchctl_print(execution, domain, label)
    execution._record(LIFECYCLE_RESTART_ID, "probe", None, status=None, note={"label": label, "pid_before": before, "pid_observed": current, "resume": "inspect_before_second_invocation"})  # noqa: SLF001
    if current is None:
        _bootstrap(execution, plan, domain, label, before)
        return
    if before is not None and current != before and current > 0:
        execution._record(LIFECYCLE_RESTART_ID, "manager", None, status="applied", note={"label": label, "pid_before": before, "restarted_before_crash": True})  # noqa: SLF001
        return
    execution.context.seams.launchctl(execution.context.registry, "bootout", (f"{domain}/{label}",), 60)
    execution.executed.append("launchctl:bootout")
    _wait_until_unloaded(execution, plan, domain, label)
    _bootstrap(execution, plan, domain, label, before)


def _bootstrap(execution: RuntimeExecution, plan: RuntimePlan, domain: str, label: str, before: int | None) -> None:
    started = execution.context.seams.launchctl(execution.context.registry, "bootstrap", (domain, plan.lifecycle.plist_path), 60)
    execution.executed.append("launchctl:bootstrap")
    if started.returncode != 0:
        execution._record(LIFECYCLE_RESTART_ID, "manager", None, status="failed", note={"label": label, "bootstrap_exit": started.returncode})  # noqa: SLF001
        raise UpdateFailedError("launchagent_start_failed", f"launchctl bootstrap of {label} exited {started.returncode}", repair=f"Inspect the plist and launchd log; {_reconcile(execution)}")
    execution._record(LIFECYCLE_RESTART_ID, "manager", None, status="applied", note={"label": label, "pid_before": before})  # noqa: SLF001


def _wait_until_unloaded(execution: RuntimeExecution, plan: RuntimePlan, domain: str, label: str) -> None:
    deadline = time.monotonic() + plan.lifecycle.readiness_budget_seconds
    while _launchctl_print(execution, domain, label) is not None:
        if time.monotonic() > deadline:
            execution._record(LIFECYCLE_RESTART_ID, "manager", None, status="blocked", note={"label": label, "stop": "timeout"})  # noqa: SLF001
            raise UpdateBlockedError("launchagent_stop_timeout", f"{label} did not clear within the readiness budget after bootout", repair=f"Inspect launchctl; {_reconcile(execution)}")
        time.sleep(_POLL_SECONDS)


def _launchctl_print(execution: RuntimeExecution, domain: str, label: str) -> int | None:
    printed = execution.context.seams.launchctl(execution.context.registry, "print", (f"{domain}/{label}",), 30)
    if printed.returncode != 0:
        return None
    for line in printed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            value = stripped.removeprefix("pid = ").strip()
            return int(value) if value.isdigit() else None
    return 0


def _readiness(execution: RuntimeExecution, plan: RuntimePlan) -> None:
    row = execution._row(READINESS_ID)  # noqa: SLF001
    if row["status"] == "verified":
        return
    deadline = time.monotonic() + plan.lifecycle.readiness_budget_seconds
    while True:
        try:
            healthy = execution.context.seams.read_health(execution.context.registry, 30).get("status") == "healthy"
        except (AdapterError, AdapterProtocolError):
            healthy = False
        if healthy:
            execution._record(READINESS_ID, "probe", None, status="verified", note={"signal": "bridge_health_healthy"})  # noqa: SLF001
            return
        if time.monotonic() > deadline:
            execution._record(READINESS_ID, "probe", None, status="failed", note={"signal": "readiness_timeout"})  # noqa: SLF001
            raise UpdateFailedError("readiness_timeout", "the instance did not report healthy within the readiness budget", repair=f"Inspect the instance log; {_reconcile(execution)}")
        time.sleep(_POLL_SECONDS)


def attest_and_publish(execution: RuntimeExecution, plan: RuntimePlan) -> None:
    """Section 7.5 steps 2-5, in that order; nothing publishes on a health signal alone."""
    current = execution._current_record()  # noqa: SLF001
    fields = execution.context.candidate.fields
    candidate_release = ReleaseIdentity(fields.repository, fields.commit, fields.tree_hash, fields.release_tag)
    if plan.lifecycle.strategy == "router_cutover":
        _require_router_serving(execution, plan)
    else:
        _require_single_color_serving(execution, plan)
    if current.runtime_release == candidate_release and current.contract_identities.runtime_contract_digest == execution.context.candidate.bundle_digest:
        execution.record = current
        return
    with instance_lock(execution.paths.registry_lock_path, create=True):
        execution.record = publish_runtime_advance(execution.paths.maintenance_inventory_path, current, operation_id=execution.operation_id, runtime_release=candidate_release, runtime_contract_digest=execution.context.candidate.bundle_digest, now=utc_now())
    if execution._current_record() != execution.record:  # noqa: SLF001
        raise StateError("maintenance inventory runtime axis did not read back")
    execution.executed.append("inventory:publish_runtime_advance")


def _require_router_serving(execution: RuntimeExecution, plan: RuntimePlan) -> None:
    row = execution._row(LIFECYCLE_CUTOVER_ID)  # noqa: SLF001
    candidate_release_id = _candidate_release_id(_latest_outcome(row))
    attestation = attest(execution.context, new_reconciliation_id())
    execution.executed.append("attest:post_lifecycle")
    if attestation.release_id == candidate_release_id and attestation.current_release_id == candidate_release_id and attestation.served_by_self:
        execution._record(LIFECYCLE_CUTOVER_ID, "manager", None, status="verified", note={"attestation": attestation.to_dict()})  # noqa: SLF001
        return
    if attestation.current_release_id == plan.lifecycle.current_release_id:
        raise UpdateFailedError("runtime_candidate_not_serving", "the swap reported success but the router still serves the baseline release", repair=f"Inspect the router; {_reconcile(execution)}")
    raise ManagedIdentityDriftError("the attested runtime names a release that is neither the baseline nor the candidate", repair=f"Retain all evidence; inspect with `solet-manager doctor {execution.record.name}`.")


def _require_single_color_serving(execution: RuntimeExecution, plan: RuntimePlan) -> None:
    row = execution._row(LIFECYCLE_RESTART_ID)  # noqa: SLF001
    domain = f"gui/{execution.context.seams.uid}"
    pid = _launchctl_print(execution, domain, plan.lifecycle.launchagent_label)
    raw = Path(plan.lifecycle.plist_path).read_bytes()
    if pid is None or pid <= 0 or derive_launch_topology(raw) != plan.lifecycle.launch_topology:
        raise UpdateFailedError("launchagent_start_failed", "the LaunchAgent is not running the target's own process after the restart", repair=f"Inspect launchd; {_reconcile(execution)}")
    before = _pid_before(row)
    if before is not None and before == pid:
        raise UpdateFailedError("launchagent_start_failed", "the LaunchAgent still reports the pre-restart process", repair=f"Inspect launchd; {_reconcile(execution)}")
    execution._record(LIFECYCLE_RESTART_ID, "manager", None, status="verified", note={"pid_after": pid, "interpreter_root": execution.record.target.canonical_path})  # noqa: SLF001


# --- post-runtime reconcile stage (section 7.6) --------------------------------------------


def runtime_reconcile_stage(execution: RuntimeExecution) -> None:
    plan = execution._plan()  # noqa: SLF001
    bundle = execution.context.candidate.bundle
    for operation in execution._stage_operations("runtime_reconcile"):  # noqa: SLF001
        if operation.operation_ref == "existing::runtime.platform_migration":
            _platform_migration(execution, operation, plan)
        else:
            execution._drive(operation, execution._inputs(operation), contradiction="runtime_postcondition_contradiction", incomplete="runtime_reconcile_incomplete")  # noqa: SLF001
    for index, kb in enumerate(bundle.knowledge_removals):
        _knowledge_reinstall(execution, knowledge_row_id(index, kb), kb)


def _platform_migration(execution: RuntimeExecution, operation: RuntimeOperation, plan: RuntimePlan) -> None:
    """Step 6 section 4.2 rows T3b/T6/T7, dispatched on the journaled ``operation_type``."""
    row = execution._row(operation.operation_id)  # noqa: SLF001
    if row["status"] in {"verified", "verified_by_probe"}:
        return
    key = _migration_process_key(execution, operation, plan)
    operation_type = execution.operation_type(operation.operation_id)
    if _migration_postcondition(execution, operation, key):
        execution._set_status(operation.operation_id, "verified_by_probe")  # noqa: SLF001
        return
    if execution._prior_applying(operation.operation_id):  # noqa: SLF001
        _refuse_reapply(execution, operation, operation_type, _apply_attempts(row))
    _apply_platform_migration(execution, operation, key)
    if _migration_postcondition(execution, operation, key):
        execution._set_status(operation.operation_id, "verified")  # noqa: SLF001
        return
    # T6 only: reapply exactly once after a failed postcondition; a second contradiction is terminal.
    if operation_type == OperationType.ADDITIVE_PLATFORM_MIGRATION.value and _apply_attempts(execution._row(operation.operation_id)) < 2:  # noqa: SLF001
        _apply_platform_migration(execution, operation, key)
        if _migration_postcondition(execution, operation, key):
            execution._set_status(operation.operation_id, "verified")  # noqa: SLF001
            return
    raise UpdateFailedError("migration_postcondition_contradiction", f"{operation.operation_id} applied but its own dry-run postcondition does not report it applied", repair=f"Retain all evidence; {_reconcile(execution)}")


def _refuse_reapply(execution: RuntimeExecution, operation: RuntimeOperation, operation_type: str, applies: int) -> None:
    """A row that already applied once: T7 and T3b are never reapplied; T6 twice-unverified is a contradiction."""
    if operation_type == OperationType.FORWARD_ONLY_MIGRATION.value:
        raise UpdateBlockedError("forward_only_migration_incomplete", f"{operation.operation_id} was invoked once after the forward-only boundary and its postcondition is unverified; {_CODE_ONLY}", repair=f"{_reconcile(execution)} The successor requires a fresh `--backup-checkpoint <id>` and explicit approval.")
    if operation_type == OperationType.MANUAL_ADDITIVE_PLATFORM_MIGRATION.value:
        raise UpdateBlockedError("migration_incomplete", f"{operation.operation_id} was invoked once; manual retry policy", repair=f"{_reconcile(execution)} The successor plans it as operator_confirmation.")
    if applies >= 2:
        raise UpdateFailedError("migration_postcondition_contradiction", f"{operation.operation_id} was applied twice and its own dry-run postcondition still reports it unapplied", repair=f"Retain all evidence; {_reconcile(execution)}")


def _migration_process_key(execution: RuntimeExecution, operation: RuntimeOperation, plan: RuntimePlan) -> str:
    selections = plan.operator_selections
    key = selections.get("process_key")
    if not isinstance(key, str) or (operation.forward_only and not isinstance(selections.get("backup_checkpoint_id"), str)):
        needs = " and a verified backup checkpoint (`update --backup-checkpoint <id>`)" if operation.forward_only else ""
        raise UpdateBlockedError("operator_selection_required", f"{operation.operation_id} needs a declared process key{needs} in operator_selections", repair="Bundle schema v1 declares no process key; the release must be applied with a Manager and seed that supply one (Step 6, D7).")
    return key


def _migration_postcondition(execution: RuntimeExecution, operation: RuntimeOperation, key: str) -> bool:
    """T6/T7 oracle: the migration answers its own dry-run; VERIFIED iff ``data.applied`` is true."""
    try:
        data = execution.context.seams.invoke_bridge(execution.context.registry, key, {"dry_run": True}, "migration.read", 120)
    except (AdapterError, AdapterProtocolError) as exc:
        execution._record(operation.operation_id, "probe", None, status=None, note={"process_key": key, "postcondition": "unreachable", "error": str(exc)[:512]})  # noqa: SLF001
        return False
    applied = data.get("applied") is True
    execution._record(operation.operation_id, "probe", None, status=None, note={"process_key": key, "postcondition": "applied" if applied else "unapplied", "result": data})  # noqa: SLF001
    return applied


def _apply_platform_migration(execution: RuntimeExecution, operation: RuntimeOperation, key: str) -> None:
    execution._record(operation.operation_id, "apply", None, status="applying", note={"process_key": key})  # noqa: SLF001
    try:
        data = execution.context.seams.invoke_bridge(execution.context.registry, key, {"dry_run": False}, "migration", 300)
    except (AdapterError, AdapterProtocolError) as exc:
        execution._record(operation.operation_id, "manager", None, status="failed", note={"error": str(exc)[:512]})  # noqa: SLF001
        if operation.forward_only:
            raise UpdateFailedError("forward_only_migration_failed", f"{operation.operation_id} failed after the forward-only boundary; runtime and source stay at N+1; {_CODE_ONLY}", repair=_reconcile(execution)) from exc
        raise UpdateFailedError("migration_postcondition_contradiction", f"{operation.operation_id} failed: {exc}", repair=_reconcile(execution)) from exc
    execution.executed.append(f"bridge:{key}")
    execution._record(operation.operation_id, "manager", None, status="applied", note={"result": data})  # noqa: SLF001


def _apply_attempts(row: dict[str, JsonValue]) -> int:
    return sum(1 for item in cast(list[JsonValue], row["attempts"]) if cast(dict[str, JsonValue], item)["phase"] == "apply")


def _knowledge_reinstall(execution: RuntimeExecution, operation_id: str, knowledge_base: str) -> None:
    """T8: an idempotent re-install, then the negative search over every removed article of the base."""
    row = execution._row(operation_id)  # noqa: SLF001
    if row["status"] == "verified":
        return
    execution._record(operation_id, "apply", None, status="applying", note={"knowledge_base": knowledge_base})  # noqa: SLF001
    try:
        data = execution.context.seams.invoke_bridge(execution.context.registry, KNOWLEDGE_INSTALL_KEY, {"name": knowledge_base}, "knowledge", 300)
    except (AdapterError, AdapterProtocolError) as exc:
        execution._record(operation_id, "manager", None, status="failed", note={"error": str(exc)[:512]})  # noqa: SLF001
        raise UpdateFailedError("runtime_postcondition_contradiction", f"knowledge base {knowledge_base} could not be reinstalled: {exc}", repair=_reconcile(execution)) from exc
    execution.executed.append(f"bridge:knowledge_service::install:{knowledge_base}")
    execution._record(operation_id, "manager", None, status="applied", note={"result": data})  # noqa: SLF001
    _knowledge_negative_search(execution, operation_id, knowledge_base)
    execution._set_status(operation_id, "verified")  # noqa: SLF001


def _knowledge_negative_search(execution: RuntimeExecution, operation_id: str, knowledge_base: str) -> None:
    plan = execution._plan()  # noqa: SLF001
    for kb, path, title in plan.knowledge_removed_articles:
        if kb != knowledge_base:
            continue
        try:
            data = execution.context.seams.invoke_bridge(execution.context.registry, KNOWLEDGE_SEARCH_PROCESS_KEY, {"query": title, "top_k": 8}, "knowledge.read", 120)
        except (AdapterError, AdapterProtocolError) as exc:
            execution._record(operation_id, "manager", None, status="failed", note={"removed_path": path, "error": str(exc)[:512]})  # noqa: SLF001
            raise UpdateFailedError("runtime_postcondition_contradiction", f"negative search for {path!r} could not run: {exc}", repair=_reconcile(execution)) from exc
        hit = knowledge_search_hits(data, path)
        execution._record(operation_id, "probe", None, status=None, note={"removed_path": path, "title": title, "hit": hit})  # noqa: SLF001
        if hit:
            execution._record(operation_id, "manager", None, status="failed", note={"removed_path": path, "postcondition": "knowledge_removal_not_applied"})  # noqa: SLF001
            raise UpdateFailedError("knowledge_removal_not_applied", f"search still cites the removed article {path!r} after re-installing {knowledge_base}", repair=_reconcile(execution))


# --- helpers ----------------------------------------------------------------------------------


def _seed_identity(execution: RuntimeExecution) -> dict[str, JsonValue]:
    fields = execution.context.candidate.fields
    return {"repository": fields.repository, "commit": fields.commit, "tree": fields.tree_hash, "tag": fields.release_tag, "descriptor_digest": execution.context.candidate.descriptor_digest}


def _cutover_files(execution: RuntimeExecution) -> tuple[dict[str, JsonValue], ...]:
    rows: list[dict[str, JsonValue]] = [{"path": "existing_install_flow.json", "sha256": execution.context.candidate.bundle_digest}]
    rows.extend({"path": artifact.template_ref, "sha256": artifact.template_digest} for artifact in execution.context.candidate.bundle.managed_artifacts)
    return tuple(rows)


def _terms_from_dict(raw: dict[str, JsonValue]) -> CutoverTerms:
    return CutoverTerms(
        current_release_id=cast(str, raw["current_release_id"]),
        active_color=cast(str, raw["active_color"]),
        active_instance_id=cast(str, raw["active_instance_id"]),
        active_start_token=cast(str, raw["active_start_token"]),
        manifest_etag=cast(str, raw["manifest_etag"]),
        launch_topology=cast(str, raw["launch_topology"]),
        launchagent_label=cast(str, raw["launchagent_label"]),
        launchagent_plist_sha256=cast(str, raw["launchagent_plist_sha256"]),
        adapter_module_sha256=cast(str, raw["adapter_module_sha256"]),
        adapter_module_replaced=cast(bool, raw["adapter_module_replaced"]),
        source_surface_sha256=cast(str, raw["source_surface_sha256"]),
        release_surface_sha256=cast(str, raw["release_surface_sha256"]),
        verification_modules=tuple(cast(list[str], raw["verification_modules"])),
    )


def _latest_outcome(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
    for item in reversed(cast(list[JsonValue], row["attempts"])):
        evidence = cast(dict[str, JsonValue], cast(dict[str, JsonValue], item)["evidence"])
        outcome = evidence.get("outcome")
        if isinstance(outcome, dict):
            return outcome
    raise StateError("the cutover row carries no controller outcome")


def _candidate_release_id(outcome: dict[str, JsonValue]) -> str:
    evidence = outcome.get("evidence")
    if isinstance(evidence, dict):
        candidate = evidence.get("candidate_release_id")
        if isinstance(candidate, str) and candidate:
            return candidate
    raise StateError("the cutover outcome names no candidate release")


def _pid_before(row: dict[str, JsonValue]) -> int | None:
    for item in cast(list[JsonValue], row["attempts"]):
        evidence = cast(dict[str, JsonValue], cast(dict[str, JsonValue], item)["evidence"])
        value = evidence.get("pid_before")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None
