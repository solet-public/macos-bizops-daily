"""The doctor's probe context, section record and check constructors (Step 6 design section 3.3).

Every check binds to exactly one authoritative probe source from the closed
set ``{manager_static, manager_git, manager_host, existing_probe:<ref>,
bridge_read:<kind>}``.  A section with no authoritative source under the
selected contract reports ``unknown`` with ``no_authoritative_probe`` and is
never omitted (A6); an ``unknown`` or ``missing`` is never rewritten to
``failed``; ``not_applicable`` appears only under a closed contract condition,
never because a probe was unavailable.  Nothing here writes a target byte;
the only target-side effects are disclosed probe-phase adapter executions and
allowlisted read-only bridge calls, counted on the probe context.  The
section builders live in ``existing_install_doctor_checks`` (sections 1-8)
and ``existing_install_doctor_service_checks`` (sections 9-16).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from .adapter_protocol import OperationResult
from .errors import AdapterError, AdapterProtocolError, ManagerError, SourceError, StateConflictError
from .executed_code import RosterUnreadableError, executed_code_roots
from .existing_install_adapters import ExistingInstallAdapterRegistry
from .existing_install_bundle import RuntimeOperation
from .existing_install_inspection import ExistingInstallInspectionResult
from .existing_solet_diagnostics import DiagnosticCheck, DiagnosticStatus
from .installer_pins import installer_pinned_paths, read_target_blob
from .models import CheckpointStatus, DoctorContractKind, InstanceInventoryRecordV2, JsonValue, ReleaseIdentity
from .paths import ManagerPaths
from .update_candidate import UpdateCandidate
from .update_runtime_plan import PlanContext, RuntimeSeams, declared_closure_for, decode_facts, operation_request, public_inputs_for
from .update_topology import executed_code_overlap

__all__ = ["STALE_ARTIFACT_STATES", "VERIFIED_ARTIFACT_STATES", "DoctorProbe", "Section", "artifact_check", "artifact_facts", "check", "executed_code_overlap_paths", "executed_code_unknown_reason", "executed_code_verifiable", "not_applicable", "operation_by_ref", "probe_status", "run_probes", "unbound_reason", "unknown", "unknown_section", "verdict"]

VERIFIED_ARTIFACT_STATES = frozenset({"stamped_current"})
STALE_ARTIFACT_STATES = frozenset({"stamped_previous", "legacy_matched"})
_REFRESH_ONLY_SETTLED_STATES = frozenset({"absent", "locally_modified", "unknown_origin"})


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    checks: tuple[DiagnosticCheck, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {"section": self.name, "checks": [check.to_dict() for check in self.checks]}


@dataclass
class DoctorProbe:
    """Everything the section builders read, plus the disclosed execution counters."""

    contract: DoctorContractKind
    record: InstanceInventoryRecordV2
    expected_source: ReleaseIdentity
    expected_runtime: ReleaseIdentity | None
    inspection: ExistingInstallInspectionResult
    seams: RuntimeSeams
    registry: ExistingInstallAdapterRegistry
    target: Path
    journal: dict[str, JsonValue] | None
    candidate: UpdateCandidate | None
    context: PlanContext | None
    cached_bundle: dict[str, JsonValue] | None
    installed_release: tuple[str, str | None] | None
    executions: int = 0
    invoked_vectors: list[str] = field(default_factory=lambda: [])
    results: dict[str, OperationResult] = field(default_factory=lambda: {})
    topology: str | None = None
    pid_observed: int | None = None
    #: Step 7 section 6.5: the derived executed-code roots, or ``None`` when the roster could not be read.
    executed_code_roots: tuple[str, ...] | None = None
    #: Step 7: the Manager paths, so contract 2 can read the last verified update's journal for the B7 disclosure.
    paths: ManagerPaths | None = None
    executed_code_roots_reason: str | None = None
    #: iss_f1d8cfc2: tracked hook manifests proved to carry exactly the installer's interpreter pin (Class T, not executed-code).
    installer_pins: tuple[str, ...] = ()

    @property
    def probes_bound(self) -> bool:
        return self.context is not None and self.candidate is not None

    def probe(self, operation: RuntimeOperation) -> OperationResult:
        context = cast(PlanContext, self.context)
        closure, _ = declared_closure_for(context)
        artifacts = tuple((artifact, artifact.resolve_destination(home=str(self.seams.home), target=str(self.target), name=self.record.name, profile_home=str(self.target / "profile"))) for artifact in cast(UpdateCandidate, self.candidate).bundle.managed_artifacts)
        inputs = public_inputs_for(context, operation, closure, artifacts)
        request = operation_request(context, operation, phase="probe", probe_purpose="completion", approval_fingerprint=None, attempt=1, public_inputs=inputs)
        result = self.seams.invoke_adapter(self.registry, request)
        self.executions += 1
        self.invoked_vectors.append(f"existing_probe:{operation.operation_ref}")
        self.results[operation.operation_id] = result
        return result

    def bridge(self, key: str, arguments: dict[str, JsonValue], kind: str) -> dict[str, JsonValue]:
        self.invoked_vectors.append(f"bridge_read:{kind}")
        return self.seams.invoke_bridge(self.registry, key, arguments, kind, 60)


# --- check constructors -------------------------------------------------------------------------


def check(check_id: str, status: DiagnosticStatus, summary: str, source: str, *, reason: str | None = None, repair: str | None = None, observed: JsonValue = None, expected: JsonValue = None) -> DiagnosticCheck:
    if status in {DiagnosticStatus.VERIFIED, DiagnosticStatus.NOT_APPLICABLE}:
        return DiagnosticCheck(check_id, status, summary, None, None, observed, expected, source)
    return DiagnosticCheck(check_id, status, summary, reason or status.value, repair or "operator_review", observed, expected, source)


def unknown(check_id: str, summary: str, source: str, reason: str = "no_authoritative_probe", observed: JsonValue = None) -> DiagnosticCheck:
    return check(check_id, DiagnosticStatus.UNKNOWN, summary, source, reason=reason, repair="operator_review", observed=observed)


def not_applicable(check_id: str, condition: str, source: str) -> DiagnosticCheck:
    return check(check_id, DiagnosticStatus.NOT_APPLICABLE, f"Not applicable under the closed condition {condition}.", source, expected=condition)


def verdict(check_id: str, ok: bool, summary: str, source: str, *, reason: str, observed: JsonValue = None, expected: JsonValue = None, repair: str = "operator_review") -> DiagnosticCheck:
    return check(check_id, DiagnosticStatus.VERIFIED if ok else DiagnosticStatus.FAILED, summary, source, reason=reason, repair=repair, observed=observed, expected=expected)


def unknown_section(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del probe
    return (unknown(name, "No existing-install probe reaches this surface; never reported as failed.", "manager_static"),)


def executed_code_overlap_paths(probe: DoctorProbe) -> tuple[str, ...] | None:
    """Every tracked-or-untracked local path under a derived executed-code root (section 6.5), less the byte-exact
    installer interpreter pins (iss_f1d8cfc2); ``None`` when the roster or a pinned manifest's committed blob is unreadable."""
    if probe.executed_code_roots is None and probe.executed_code_roots_reason is None:
        try:
            probe.executed_code_roots = executed_code_roots(probe.target, probe.candidate)
            probe.installer_pins = installer_pinned_paths(probe.target, probe.inspection.facts, read_target_blob(probe.target))
        except RosterUnreadableError as exc:
            probe.executed_code_roots, probe.executed_code_roots_reason = None, f"roster_unreadable: {exc}"
        except SourceError as exc:
            probe.executed_code_roots, probe.executed_code_roots_reason = None, f"installer_pin_unreadable: {exc}"
    if probe.executed_code_roots is None:
        return None
    facts = probe.inspection.facts
    local = tuple(path for path in (*facts.tracked_paths.values, *facts.untracked_paths.values) if path not in probe.installer_pins)
    return executed_code_overlap(local, probe.executed_code_roots)


def executed_code_unknown_reason(probe: DoctorProbe) -> str:
    """The stable reason code for an underivable executed-code set: ``roster_unreadable`` or ``installer_pin_unreadable``."""
    return (probe.executed_code_roots_reason or "roster_unreadable").split(":", 1)[0]


def executed_code_verifiable(probe: DoctorProbe) -> bool:
    """Section 6.5, under every contract: no local path under an executed-code root; under candidate/verified
    additionally the exact expected tree.  The CLEAN conjunct is gone -- a real clone is never clean."""
    overlap = executed_code_overlap_paths(probe)
    if overlap is None or overlap:
        return False
    if probe.contract is DoctorContractKind.DIAGNOSTIC:
        return True
    return probe.inspection.facts.head_tree == probe.expected_source.tree


def run_probes(probe: DoctorProbe) -> None:
    candidate = cast(UpdateCandidate, probe.candidate)
    baseline = cast(PlanContext, probe.context).baseline_commit
    for operation in candidate.bundle.runtime_operations:
        if operation.runner == "instance_bridge":
            continue
        if probe.contract is DoctorContractKind.CANDIDATE and not operation.applies_to(baseline):
            continue
        try:
            probe.probe(operation)
        except (AdapterError, AdapterProtocolError, StateConflictError) as exc:
            probe.results[operation.operation_id] = failed_probe(exc)


def failed_probe(exc: ManagerError) -> OperationResult:
    return OperationResult("", "", "probe", "completion", CheckpointStatus.BLOCKED, "probe_unavailable", False, None, False, 0, "", str(exc)[:512], (), (), (), None)


def operation_by_ref(probe: DoctorProbe, ref: str) -> RuntimeOperation | None:
    if probe.candidate is None:
        return None
    return next((item for item in probe.candidate.bundle.runtime_operations if item.operation_ref == ref), None)


def probe_status(result: OperationResult | None) -> DiagnosticStatus:
    if result is None:
        return DiagnosticStatus.UNKNOWN
    status = result.checkpoint_status
    if status is CheckpointStatus.VERIFIED:
        return DiagnosticStatus.VERIFIED
    if status is CheckpointStatus.PENDING:
        return DiagnosticStatus.MISSING
    if status is CheckpointStatus.FAILED:
        return DiagnosticStatus.FAILED
    return DiagnosticStatus.UNKNOWN


def unbound_reason(probe: DoctorProbe) -> str:
    if not probe.probes_bound:
        return "no_authoritative_probe"
    if probe.executed_code_roots is None and probe.executed_code_roots_reason is not None:
        return executed_code_unknown_reason(probe)
    return "target_code_unverifiable"


def artifact_facts(probe: DoctorProbe) -> dict[str, dict[str, str]]:
    facts: dict[str, dict[str, str]] = {}
    for result in probe.results.values():
        for item in result.evidence:
            if str(item["id"]).startswith("artifact."):
                decoded = decode_facts(item["observed"])
                facts[decoded.get("artifact_id", "")] = decoded
    return facts


def _refresh_only_check(check_id: str, artifact_id: str, facts: dict[str, str], source: str) -> DiagnosticCheck | None:
    """A ``rendered_whole`` file is only ever refreshed, so absent or operator-edited is the settled state an update leaves it in."""
    state = facts.get("state", "unknown")
    if facts.get("kind") != "rendered_whole" or state not in _REFRESH_ONLY_SETTLED_STATES:
        return None
    observed: dict[str, JsonValue] = dict(facts)
    return check(check_id, DiagnosticStatus.VERIFIED, f"Managed artifact {artifact_id} is {state}: it is refresh-only, so it was left as it is and never overwritten.", source, observed=observed, expected="stamped_current_or_left_in_place")


def artifact_check(check_id: str, artifact_id: str, facts: dict[str, str] | None, source: str) -> DiagnosticCheck:
    if facts is None:
        return unknown(check_id, f"No probe reported managed artifact {artifact_id}.", source, reason="artifact_unreported")
    settled = _refresh_only_check(check_id, artifact_id, facts, source)
    if settled is not None:
        return settled
    state, conflict = facts.get("state", "unknown"), facts.get("conflict", "none")
    observed: dict[str, JsonValue] = dict(facts)
    if conflict != "none":
        return check(check_id, DiagnosticStatus.FAILED, f"Managed artifact {artifact_id} conflicts: {conflict}.", source, reason=conflict, repair="resolve_managed_artifact_conflict", observed=observed, expected="stamped_current")
    if state == "absent":
        return check(check_id, DiagnosticStatus.MISSING, f"Managed artifact {artifact_id} is absent.", source, reason="artifact_absent", repair="hydrate_managed_artifact", observed=observed, expected="stamped_current")
    if state in VERIFIED_ARTIFACT_STATES:
        return check(check_id, DiagnosticStatus.VERIFIED, f"Managed artifact {artifact_id} is the current render.", source, observed=observed, expected="stamped_current")
    if state in STALE_ARTIFACT_STATES:
        # A recognised previous render is not a contradiction: the current render is missing.
        return check(check_id, DiagnosticStatus.MISSING, f"Managed artifact {artifact_id} is {state}; the current render is missing.", source, reason="artifact_stale", repair="hydrate_managed_artifact", observed=observed, expected="stamped_current")
    return check(check_id, DiagnosticStatus.FAILED, f"Managed artifact {artifact_id} is {state}, not the current render.", source, reason="artifact_state_unrecognised", repair="hydrate_managed_artifact", observed=observed, expected="stamped_current")


