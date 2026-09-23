"""Doctor sections 1-8 (Step 6 design section 3.3): identity, source topology, software, environment,
database policy, Keychain, configuration, managed artifacts.  ``build_sections`` renders all sixteen in the
closed order, delegating 9-16 to ``existing_install_doctor_service_checks``."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from .diagnostic_contract_checks import read_target_solet_name
from .errors import StateConflictError, StateError
from .existing_install_doctor_probe import (
    DoctorProbe,
    Section,
    artifact_check,
    artifact_facts,
    check,
    executed_code_overlap_paths,
    executed_code_verifiable,
    not_applicable,
    operation_by_ref,
    probe_status,
    run_probes,
    unbound_reason,
    unknown,
    unknown_section,
    verdict,
)
from .existing_install_doctor_service_checks import (
    availability,
    coding_agents,
    knowledge,
    launchers,
    migrations,
    roster,
    service,
)
from .existing_install_inspection import ExistingInstallFacts, InspectionStatus, ObservationAvailability, ObservedBoolean
from .existing_solet_diagnostics import DiagnosticCheck, DiagnosticStatus
from .host_software import host_checks
from .launch_topology import derive_launch_topology, launchagent_plist_path, plist_label
from .local_state import ObservedLocalState, allowed_service_write, compare, observe_local_state, snapshot_from_journal
from .models import DoctorContractKind, JsonValue
from .update_journal import read_update_journal
from .update_topology import git_metadata_paths, shape_changed_rows

__all__ = ["build_sections"]

_ENVIRONMENT_FACTS = {"python_313": "python_313", "pip_present": "pip", "build_backend": "build_backend", "bridge_cli_version": "private_solet"}
_TOPOLOGY_FACTS = (
    ("branch_attached", "detached", ObservedBoolean.FALSE, "detached_head"),
    ("not_shallow", "shallow", ObservedBoolean.FALSE, "shallow_history"),
)
_TOPOLOGY_PATHS = (
    ("no_repo_operation", "repository_operations", "repository_operation_present"),
    ("no_linked_worktree", "linked_worktrees", "linked_worktree_present"),
    ("no_submodule", "submodules", "submodule_present"),
)


def build_sections(probe: DoctorProbe) -> list[Section]:
    """The sixteen sections of governing section 9, in order, never omitting one."""
    builders = (
        ("identity", _identity), ("source_topology", _source_topology), ("software_dependencies", _software), ("environment_closure", _environment),
        ("database_policy", unknown_section), ("keychain_vault", _keychain), ("configuration", _configuration), ("files_managed_blocks", _artifacts),
        ("service_router", service), ("shell_launchers", launchers), ("coding_agent_integrations", coding_agents), ("permissions", unknown_section),
        ("plugin_roster", roster), ("knowledge_session_retrieval", knowledge), ("pending_release_migrations", migrations), ("update_availability", availability),
    )
    if probe.probes_bound and executed_code_verifiable(probe):
        run_probes(probe)
    return [Section(name, builder(probe, name)) for name, builder in builders]


# --- 1 identity / 2 source topology ------------------------------------------------------------------


def _identity(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    facts = probe.inspection.facts
    expected = probe.expected_source
    step2 = {check.check_id: check for check in probe.inspection.checks if check.section == "identity"}
    checks = [
        verdict("head_commit", facts.head_commit == expected.commit, "HEAD commit against the contract's source release.", "manager_git", reason="head_commit_mismatch", observed=facts.head_commit, expected=expected.commit),
        verdict("head_tree", facts.head_tree == expected.tree, "HEAD tree against the contract's source release.", "manager_git", reason="head_tree_mismatch", observed=facts.head_tree, expected=expected.tree),
    ]
    for check_id, step2_id in (("committed_provenance", "committed_provenance"), ("working_provenance", "working_provenance"), ("trailers", "seal_trailers")):
        item = step2.get(step2_id)
        if item is None:
            checks.append(unknown(check_id, "The Step-2 inspection produced no such identity check.", "manager_git"))
            continue
        checks.append(verdict(check_id, item.status is InspectionStatus.VERIFIED, item.summary, "manager_git", reason=item.reason_code or "identity_check_failed", observed=item.observed, expected=item.expected))
    checks.append(_enrollment_drift(probe))
    return tuple(checks)


def _enrollment_drift(probe: DoctorProbe) -> DiagnosticCheck:
    cached = probe.cached_bundle
    if cached is None:
        return unknown("enrollment_drift", "The cached inspection bundle is absent; the live inspection cannot be compared to enrollment.", "manager_static", reason="inspection_bundle_cache_missing")
    identity = probe.inspection.target_identity
    observed: dict[str, JsonValue] = {"canonical_display": str(identity.canonical_display), "device": identity.target_device, "inode": identity.target_inode}
    expected = cast(dict[str, JsonValue], cached.get("target_identity", {}))
    same = expected.get("canonical_display") == observed["canonical_display"] and cast(dict[str, JsonValue], expected.get("filesystem_identity", {})) == {"device": identity.target_device, "inode": identity.target_inode}
    return verdict("enrollment_drift", same, "Live target identity against the cached enrollment inspection.", "manager_static", reason="enrollment_identity_drift", observed=observed, expected=expected)


def _source_topology(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    facts = probe.inspection.facts
    canonical = probe.record.channel.canonical_repository
    checks = [verdict("origin_canonical", facts.origins == (canonical,), "Every origin remote names the canonical repository.", "manager_git", reason="origin_not_canonical", observed=list(facts.origins), expected=canonical)]
    for check_id, attribute, wanted, reason in _TOPOLOGY_FACTS:
        value = cast(ObservedBoolean, getattr(facts, attribute))
        checks.append(verdict(check_id, value is wanted, f"Repository fact {attribute} inspected.", "manager_git", reason=reason, observed=value.value, expected=wanted.value))
    for check_id, attribute, reason in _TOPOLOGY_PATHS:
        values = tuple(cast(tuple[str, ...], getattr(facts, attribute).values))
        checks.append(verdict(check_id, not values, f"Repository {attribute} inspected.", "manager_git", reason=reason, observed=list(values), expected=[]))
    checks.append(_local_state_admissible(probe))
    return tuple(checks)


def _local_state_admissible(probe: DoctorProbe) -> DiagnosticCheck:
    """Doctor section 2 (Step 7 section 6.6): the local state is in the two opened classes and, under the candidate
    contract, equals the journal's per-operation ``current``; a late B7 creation is disclosed, never a failure."""
    facts = probe.inspection.facts
    source = "manager_git"
    if any(item.availability is not ObservationAvailability.OBSERVED for item in (facts.tracked_paths, facts.staged_paths, facts.tracked_entries)):
        return unknown("local_state_admissible", "The tracked, staged or raw-diff probe did not answer.", source, reason="local_state_unobserved")
    overlap = executed_code_overlap_paths(probe)
    if overlap is None:
        return unknown("local_state_admissible", "The executed-code roots cannot be derived from an unreadable roster.", source, reason="roster_unreadable")
    observed = observe_local_state(probe.target, facts)
    detail = _local_state_detail(facts, observed, overlap)
    reason = _shape_reason(detail)
    if reason is None:
        reason, disclosed = _contract_disclosure(probe, observed, detail)
        detail["disclosed"] = cast(list[JsonValue], disclosed)
    return check("local_state_admissible", DiagnosticStatus.VERIFIED if reason is None else DiagnosticStatus.FAILED, "Local state is within the opened classes (unstaged content-only tracked edits, untracked non-code paths) and, under the candidate contract, equals the journaled commitment.", source, reason=reason, repair="operator_review", observed=detail, expected="admissible")


def _local_state_detail(facts: ExistingInstallFacts, observed: ObservedLocalState, overlap: tuple[str, ...]) -> dict[str, JsonValue]:
    state = observed.state
    return {
        "tracked": list(facts.tracked_paths.values),
        "staged": list(facts.staged_paths.values),
        "committed": state.committed_rows(),
        "preserved_surface": [{"path": path, "kind": kind, "mode": mode, "size": size} for path, kind, mode, size in state.preserved_surface],
        "executed_code_overlap": list(overlap),
        "git_metadata": list(git_metadata_paths(facts.tracked_paths.values, facts.untracked_paths.values)),
        "shape_changes": [row.to_dict() for row in shape_changed_rows(facts.tracked_entries.values)],
        "revisions": None,
    }


_SHAPE_REASONS = (("staged", "staged_changes_present"), ("shape_changes", "tracked_shape_changed"), ("executed_code_overlap", "executed_code_modified"), ("git_metadata", "git_metadata_present"))


def _shape_reason(detail: dict[str, JsonValue]) -> str | None:
    """The first opened-class violation, in the section-6.2 order."""
    return next((reason for key, reason in _SHAPE_REASONS if detail[key]), None)


def _contract_disclosure(probe: DoctorProbe, observed: ObservedLocalState, detail: dict[str, JsonValue]) -> tuple[str | None, list[str]]:
    """Under the candidate contract the commitment must equal the journal's ``current``; under the verified contract
    (shape only) the platform's own late ``knowledge_bases/`` creation (B7) is still disclosed against the last
    verified update's ``current`` so the operator sees what the launch wrote."""
    if probe.contract is DoctorContractKind.CANDIDATE and probe.journal is not None:
        return _commitment_verdict(probe, observed, detail)
    if probe.contract is DoctorContractKind.VERIFIED:
        return None, _late_service_writes(probe, observed)
    return None, []


def _late_service_writes(probe: DoctorProbe, observed: ObservedLocalState) -> list[str]:
    """B7 creations since the last verified update's ``current`` (empty when no verified update journal is readable)."""
    operation_id = probe.record.last_verified_operation_id
    if operation_id is None or probe.paths is None:
        return []
    path = probe.paths.operation_path(probe.record.instance_id, operation_id)
    try:
        journal = read_update_journal(path)
    except (OSError, StateError):
        return []
    current = snapshot_from_journal(cast(dict[str, JsonValue], cast(dict[str, JsonValue], journal["local_state"])["current"]))
    previous = frozenset(row[0] for row in current.committed_inventory)
    delta = compare(current, observed)
    return [path for path in delta.committed if allowed_service_write(probe.target, path, previous) is not None]


def _commitment_verdict(probe: DoctorProbe, observed: ObservedLocalState, detail: dict[str, JsonValue]) -> tuple[str | None, list[str]]:
    journal = cast(dict[str, JsonValue], probe.journal)
    local_state = cast(dict[str, JsonValue], journal["local_state"])
    detail["revisions"] = len(cast(list[JsonValue], local_state["revisions"]))
    current = snapshot_from_journal(cast(dict[str, JsonValue], local_state["current"]))
    delta = compare(current, observed)
    previous = frozenset(row[0] for row in current.committed_inventory)
    disclosed = [path for path in delta.committed if allowed_service_write(probe.target, path, previous) is not None]
    violations: list[JsonValue] = [*delta.tracked, *(path for path in delta.committed if path not in disclosed)]
    if violations:
        detail["preservation_violated"] = violations
        return "preservation_violated", disclosed
    return None, disclosed


# --- 3 software / 4 environment (the closure probe) -------------------------------------------------------


def _software(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    operation = operation_by_ref(probe, "existing::dependencies.reconcile")
    source = "existing_probe:existing::dependencies.reconcile"
    result = None if operation is None else probe.results.get(operation.operation_id)
    # Step 7 section 7.1: the six host-software rows, Manager-owned, ``missing`` never ``unknown`` for an absent binary.
    host = tuple(item.to_check() for item in host_checks(probe.seams, probe.target))
    if result is None:
        return (unknown("dependency_closure", "No dependency-closure probe ran under this contract.", source, reason=unbound_reason(probe)), *host)
    status = probe_status(result)
    return (check("dependency_closure", status, "Declared dependency closure by the target's own bootstrap adapter.", source, reason=result.error_kind or "closure_incomplete", repair="reapply_dependency_closure", observed=result.checkpoint_status.value, expected="verified"), *host)


def _environment(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    operation = operation_by_ref(probe, "existing::dependencies.reconcile")
    source = "existing_probe:existing::dependencies.reconcile"
    result = None if operation is None else probe.results.get(operation.operation_id)
    facts: dict[str, JsonValue] = {}
    if result is not None:
        for item in result.evidence:
            evidence_id = str(item["id"])
            if evidence_id.startswith("environment."):
                facts[evidence_id.removeprefix("environment.")] = item["observed"]
    checks: list[DiagnosticCheck] = []
    for check_id, fact in _ENVIRONMENT_FACTS.items():
        if fact not in facts:
            checks.append(unknown(check_id, f"The closure probe reported no fact {fact!r}.", source, reason=unbound_reason(probe) if result is None else "fact_unreported"))
            continue
        checks.append(verdict(check_id, facts[fact] is True, f"Environment fact {fact} from the closure probe.", source, reason=f"{fact}_missing", observed=facts[fact], expected=True))
    return tuple(checks)


# --- 6 keychain / 7 configuration -----------------------------------------------------------------------


def _keychain(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    service = f"{probe.record.name}.postgres_state_management_plugin"
    source = "manager_host:security find-generic-password (attributes only, no secret read)"
    try:
        completed = probe.seams.run_security(service, "db_password")
    except (OSError, StateConflictError) as exc:
        return (unknown("keychain_item_present", f"The Keychain metadata probe could not run: {exc}", source, reason="keychain_unqueryable"),)
    probe.invoked_vectors.append("manager_host:security")
    if completed.returncode != 0:
        return (unknown("keychain_item_present", "The Keychain metadata probe did not answer; whether a prompt would be needed cannot be decided.", source, reason="keychain_unqueryable", observed=completed.returncode),)
    return (check("keychain_item_present", DiagnosticStatus.VERIFIED, "A Keychain item exists for the instance's state service.", source, observed=service),)


def _configuration(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    target, service = probe.target, probe.record.service_identity
    genesis = target / ".solet" / "genesis.json"
    app_home = Path(service.app_home) if service.app_home else target / "profile"
    checks = [
        _root_manifest_name(probe),
        check("genesis_marker", DiagnosticStatus.VERIFIED if genesis.is_file() else DiagnosticStatus.MISSING, "Genesis marker presence; a legacy import legitimately lacks one.", "manager_static", reason="genesis_marker_absent", repair="operator_review", observed=genesis.is_file()),
        verdict("profile_app_home", app_home.is_dir(), "The enrolled profile app home exists.", "manager_static", reason="profile_app_home_missing", observed=str(app_home)),
        _label_coherence(probe),
    ]
    return tuple(checks)


def _root_manifest_name(probe: DoctorProbe) -> DiagnosticCheck:
    """Step 7 section 12.1: read ``root_manifest.yaml`` (the file the seed ships) and compare ``solet_name`` to the record."""
    manifest = probe.target / "root_manifest.yaml"
    source = "manager_static"
    if not manifest.is_file():
        return check("root_manifest_name", DiagnosticStatus.MISSING, "Root manifest root_manifest.yaml is absent.", source, reason="root_manifest_absent", repair="operator_review", observed=str(manifest))
    observed = read_target_solet_name(manifest)
    if observed is None:
        return unknown("root_manifest_name", "root_manifest.yaml carries no single unquoted solet_name.", source, reason="root_manifest_unparseable", observed=str(manifest))
    return verdict("root_manifest_name", observed == probe.record.name, "root_manifest.yaml names this solet.", source, reason="root_manifest_name_mismatch", observed=observed, expected=probe.record.name)


def _label_coherence(probe: DoctorProbe) -> DiagnosticCheck:
    label = probe.record.service_identity.launchagent_label
    plist = launchagent_plist_path(probe.seams.home, label)
    if not plist.is_file():
        return check("launchagent_label_coherence", DiagnosticStatus.MISSING, "The instance LaunchAgent plist is absent.", "manager_static", reason="launchagent_plist_missing", repair="hydrate_launchagent", observed=str(plist))
    raw = plist.read_bytes()
    probe.topology = derive_launch_topology(raw)
    observed = plist_label(raw)
    return verdict("launchagent_label_coherence", observed == label, "The plist Label equals the enrolled LaunchAgent label.", "manager_static", reason="launchagent_label_mismatch", observed=observed, expected=label)


# --- 8 managed artifacts --------------------------------------------------------------------------------


def _artifacts(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    if probe.candidate is None:
        return (unknown("managed_artifact", "No transition bundle binds managed artifacts under the diagnostic contract.", "manager_static"),)
    facts = artifact_facts(probe)
    reason = unbound_reason(probe)
    checks: list[DiagnosticCheck] = []
    for artifact in probe.candidate.bundle.managed_artifacts:
        check_id = f"managed_artifact:{artifact.artifact_id}"
        if not probe.results:
            checks.append(unknown(check_id, f"Managed artifact {artifact.artifact_id} was not probed.", "existing_probe:existing::hydration.reconcile", reason=reason))
            continue
        checks.append(artifact_check(check_id, artifact.artifact_id, facts.get(artifact.artifact_id), "existing_probe:existing::hydration.reconcile"))
    return tuple(checks) or (not_applicable("managed_artifact", "managed_artifacts=none_declared", "manager_static"),)


