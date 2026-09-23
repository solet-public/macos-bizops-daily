"""Doctor sections 9-16 (Step 6 design section 3.3): service/router, shell launchers, coding-agent
integrations, permissions, plugin roster, knowledge retrieval, pending release migrations, update availability.

The section-9 service check is conditioned on the closed service condition (the plist's launch topology plus
the journaled or enrolled router strategy, B3): attestation is required where the router serves, the launchd
process identity where a single colour does; each is ``not_applicable`` under the other's condition and
never because a probe was unavailable.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import cast

from .errors import AdapterError, AdapterProtocolError, ManagerError, StateConflictError
from .existing_install_adapters import ATTEST_PROCESS_KEY, KNOWLEDGE_SEARCH_PROCESS_KEY
from .existing_install_doctor_probe import DoctorProbe, artifact_check, artifact_facts, check, not_applicable, operation_by_ref, probe_status, unbound_reason, unknown, verdict
from .existing_solet_diagnostics import DiagnosticCheck, DiagnosticStatus
from .launch_topology import LEGACY_DIRECT, MATERIALIZED_SUPERVISOR
from .models import DoctorContractKind, JsonValue
from .update_runtime_plan import AttestationObservation, knowledge_removed_articles, roster_plugins

__all__ = ["availability", "coding_agents", "knowledge", "launchers", "migrations", "roster", "service"]

_LSTART = "%a %b %d %H:%M:%S %Y"


# --- 9 service / router -----------------------------------------------------------------------------------


def service(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    label = probe.record.service_identity.launchagent_label
    topology = probe.topology
    checks = [_topology_check(topology), _loaded_check(probe, label), _health_check(probe)]
    if topology in {LEGACY_DIRECT, MATERIALIZED_SUPERVISOR} and _router_serves(probe, topology):
        condition = f"launch_topology={topology};service=router_cutover"
        checks.extend(_attestation_checks(probe))
        checks.append(not_applicable("runtime_process_identity", condition, "manager_host"))
    elif topology in {LEGACY_DIRECT, MATERIALIZED_SUPERVISOR}:
        condition = f"launch_topology={topology};service=single_color"
        checks.append(not_applicable("runtime_attestation", condition, "bridge_read:self_deployment.attest"))
        checks.append(not_applicable("router_serving", condition, "bridge_read:self_deployment.attest"))
        checks.append(_process_identity(probe))
    else:
        for check_id in ("runtime_attestation", "router_serving", "runtime_process_identity"):
            checks.append(unknown(check_id, "The launch topology is unknown, so no service check has an authoritative source.", "manager_static", reason="launch_topology_unknown"))
    return tuple(checks)


def _router_serves(probe: DoctorProbe, topology: str) -> bool:
    """The closed service condition: a supervisor plist, the journaled router strategy, or (idle) router facts in the inventory."""
    if topology == MATERIALIZED_SUPERVISOR:
        return True
    if probe.journal is not None and probe.journal["runtime_approval"] is not None:
        return cast(dict[str, JsonValue], probe.journal["runtime_approval"])["strategy"] == "router_cutover"
    service = probe.record.service_identity
    return service.router_label is not None and service.router_socket is not None


def _topology_check(topology: str | None) -> DiagnosticCheck:
    if topology in {LEGACY_DIRECT, MATERIALIZED_SUPERVISOR}:
        return check("launch_topology", DiagnosticStatus.VERIFIED, "Launch topology derived from the instance plist.", "manager_static", observed=topology)
    return unknown("launch_topology", "The instance plist is unreadable or names an unsupported topology.", "manager_static", reason="launch_topology_unknown", observed=topology)


def _loaded_check(probe: DoctorProbe, label: str) -> DiagnosticCheck:
    printed = probe.seams.launchctl(probe.registry, "print", (f"gui/{probe.seams.uid}/{label}",), 30)
    probe.invoked_vectors.append("manager_host:launchctl print")
    if printed.returncode != 0:
        return check("launchagent_loaded", DiagnosticStatus.MISSING, f"LaunchAgent {label} is not loaded.", "manager_host", reason="launchagent_not_loaded", repair="bootstrap_launchagent", observed=printed.returncode)
    probe.pid_observed = _parse_pid(printed.stdout)
    return check("launchagent_loaded", DiagnosticStatus.VERIFIED, f"LaunchAgent {label} is loaded.", "manager_host", observed=probe.pid_observed)


def _parse_pid(stdout: str) -> int | None:
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            value = stripped.removeprefix("pid = ").strip()
            return int(value) if value.isdigit() else None
    return 0


def _health_check(probe: DoctorProbe) -> DiagnosticCheck:
    try:
        healthy = probe.seams.read_health(probe.registry, 30).get("status") == "healthy"
    except (AdapterError, AdapterProtocolError):
        return unknown("bridge_health", "The bridge did not answer; the service is offline or unreachable.", "bridge_read:health", reason="service_offline")
    probe.invoked_vectors.append("bridge_read:health")
    return verdict("bridge_health", healthy, "Bridge health envelope (never sufficient on its own).", "bridge_read:health", reason="service_unhealthy", observed=healthy, expected=True)


def _attestation_checks(probe: DoctorProbe) -> list[DiagnosticCheck]:
    source = "bridge_read:self_deployment.attest"
    modules: list[JsonValue] = [] if probe.candidate is None else list(probe.candidate.bundle.lifecycle.verification_modules)
    try:
        payload = probe.bridge(ATTEST_PROCESS_KEY, {"reconciliation_id": "rec_doctor", "verification_modules": modules}, "self_deployment.attest")
        attestation = AttestationObservation.from_payload(payload)
    except (AdapterError, AdapterProtocolError):
        return [unknown("runtime_attestation", "The running process could not be attested; the service is offline.", source, reason="service_offline"), unknown("router_serving", "No attestation, so router coherence is unknown.", source, reason="service_offline")]
    expected = _expected_release_id(probe)
    coherent = attestation.release_id == attestation.current_release_id and attestation.served_by_self
    names = expected is None or attestation.release_id == expected
    observed = attestation.to_dict()
    return [
        verdict("runtime_attestation", coherent and names, "The attested running release is the contract's runtime and served by itself.", source, reason="runtime_attestation_mismatch", observed=observed, expected=expected),
        verdict("router_serving", coherent, "Router current release and active colour cohere with the attestation.", source, reason="router_incoherent", observed=observed),
    ]


def _expected_release_id(probe: DoctorProbe) -> str | None:
    if probe.journal is None:
        return None
    for row in cast(list[JsonValue], probe.journal["runtime_operations"]):
        item = cast(dict[str, JsonValue], row)
        if item["operation_id"] != "lifecycle_cutover":
            continue
        for attempt in cast(list[JsonValue], item["attempts"]):
            outcome = cast(dict[str, JsonValue], cast(dict[str, JsonValue], attempt)["evidence"]).get("outcome")
            if isinstance(outcome, dict):
                evidence = cast(dict[str, JsonValue], outcome).get("evidence")
                if isinstance(evidence, dict) and isinstance(cast(dict[str, JsonValue], evidence).get("candidate_release_id"), str):
                    return cast(str, cast(dict[str, JsonValue], evidence)["candidate_release_id"])
    return None


def _process_rows(probe: DoctorProbe) -> list[tuple[int, str, str]] | None:
    try:
        completed = probe.seams.run_ps(30)
    except OSError:
        return None
    probe.invoked_vectors.append("manager_host:ps")
    if completed.returncode != 0:
        return None
    rows: list[tuple[int, str, str]] = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        rows.append((int(parts[0]), " ".join(parts[1:6]), parts[6]))
    return rows


def _process_identity(probe: DoctorProbe) -> DiagnosticCheck:
    pid = probe.pid_observed
    if pid is None or pid <= 0:
        return check("runtime_process_identity", DiagnosticStatus.FAILED, "launchd reports no running process for the instance.", "manager_host", reason="launchagent_not_running", repair="bootstrap_launchagent", observed=pid)
    rows = _process_rows(probe)
    if rows is None:
        return unknown("runtime_process_identity", "The process table could not be read.", "manager_host", reason="service_offline")
    command = next((command for row_pid, _, command in rows if row_pid == pid), None)
    before = _pid_before(probe)
    ok, reason = _process_identity_verdict(probe, command, pid, before)
    return verdict("runtime_process_identity", ok, "The launchd pid runs the target's own interpreter and postdates the restart.", "manager_host", reason=reason, observed={"pid": pid, "command": command, "pid_before": before}, expected=f"{probe.target}/.venv/", repair="restart_launchagent")


def _process_identity_verdict(probe: DoctorProbe, command: str | None, pid: int, before: int | None) -> tuple[bool, str]:
    under_target = command is not None and f"{probe.target}/.venv/" in command
    if not under_target:
        return False, "runtime_process_outside_target"
    if before is not None and before == pid:
        return False, "runtime_process_not_restarted"
    return True, "runtime_process_outside_target"


def _pid_before(probe: DoctorProbe) -> int | None:
    if probe.journal is None:
        return None
    for row in cast(list[JsonValue], probe.journal["runtime_operations"]):
        item = cast(dict[str, JsonValue], row)
        if item["operation_id"] != "lifecycle_restart_single_color":
            continue
        for attempt in cast(list[JsonValue], item["attempts"]):
            value = cast(dict[str, JsonValue], cast(dict[str, JsonValue], attempt)["evidence"]).get("pid_before")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


# --- 10 launchers / 11 coding agents ----------------------------------------------------------------------


def launchers(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    service = probe.record.service_identity
    if service.named_launcher_target is None:
        launcher = not_applicable("named_launcher_target", "named_launcher_target=absent", "manager_static")
    else:
        path = Path(service.named_launcher_path)
        resolved = path.resolve(strict=False) if path.exists() else None
        inside = resolved is not None and str(resolved).startswith(f"{probe.target}/")
        launcher = verdict("named_launcher_target", inside, "The named launcher resolves into the target.", "manager_static", reason="named_launcher_missing", observed=None if resolved is None else str(resolved), expected=str(probe.target))
    return (launcher, _mirrored_artifact(probe, "shell_startup_block", "shell_startup_block", "shell_startup_artifact"))


def _mirrored_artifact(probe: DoctorProbe, check_id: str, artifact_id: str, condition: str) -> DiagnosticCheck:
    declared = probe.candidate is not None and any(item.artifact_id == artifact_id for item in probe.candidate.bundle.managed_artifacts)
    if not declared:
        return not_applicable(check_id, f"{condition}=undeclared", "manager_static")
    if not probe.results:
        return unknown(check_id, f"Managed artifact {artifact_id} was not probed.", "existing_probe:existing::hydration.reconcile", reason=unbound_reason(probe))
    return artifact_check(check_id, artifact_id, artifact_facts(probe).get(artifact_id), "existing_probe:existing::hydration.reconcile")


def coding_agents(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    operation = operation_by_ref(probe, "existing::runtime.plugin_cache_refresh")
    result = None if operation is None else probe.results.get(operation.operation_id)
    if result is None:
        cache = unknown("plugin_cache_current", "No plugin-cache probe ran under this contract.", "existing_probe:existing::runtime.plugin_cache_refresh", reason=unbound_reason(probe))
    else:
        cache = check("plugin_cache_current", probe_status(result), "Coding-agent plugin cache against the tree (empty diff = current).", "existing_probe:existing::runtime.plugin_cache_refresh", reason=result.error_kind or "plugin_cache_stale", repair="refresh_plugin_cache", observed=result.checkpoint_status.value)
    return (cache, _mirrored_artifact(probe, "user_claude_md_section", "user_claude_md_section", "user_claude_md_artifact"), _stale_processes(probe))


def _stale_processes(probe: DoctorProbe) -> DiagnosticCheck:
    rows = _process_rows(probe)
    if rows is None:
        return unknown("stale_target_processes", "The process table could not be read.", "manager_host", reason="service_offline")
    reference = _reference_time(probe)
    if reference is None:
        return unknown("stale_target_processes", "No reference time is recorded for this contract.", "manager_host", reason="reference_time_unknown")
    stale = _stale_rows(probe, rows, reference)
    if not stale:
        return check("stale_target_processes", DiagnosticStatus.VERIFIED, "No target process predates the contract's reference time.", "manager_host", observed=[])
    repair = "rearm_watcher" if all(item["kind"] == "watcher" for item in stale) else "relaunch_client_session"
    return check("stale_target_processes", DiagnosticStatus.FAILED, f"{len(stale)} target process(es) predate the contract's reference time.", "manager_host", reason="stale_target_process", repair=repair, observed=cast(list[JsonValue], stale))


def _stale_rows(probe: DoctorProbe, rows: list[tuple[int, str, str]], reference: float) -> list[dict[str, JsonValue]]:
    stale: list[dict[str, JsonValue]] = []
    for pid, lstart, command in rows:
        if f"{probe.target}/.venv/bin/" not in command or pid == probe.pid_observed:
            continue
        started = _parse_lstart(lstart)
        if started is not None and started < reference:
            stale.append({"pid": pid, "kind": "watcher" if "solet-bridge watch" in command else "client_subprocess", "lstart": lstart})
    return stale


def _reference_time(probe: DoctorProbe) -> float | None:
    if probe.journal is not None:
        for attempt in cast(list[JsonValue], probe.journal["attempts"]):
            item = cast(dict[str, JsonValue], attempt)
            if item["status"] == "lifecycle_advanced":
                return _parse_iso(cast(str, item["at"]))
    return None if probe.record.last_verified_at is None else _parse_iso(probe.record.last_verified_at)


def _parse_iso(value: str) -> float | None:
    try:
        return time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except ValueError:
        return None


def _parse_lstart(value: str) -> float | None:
    try:
        return time.mktime(time.strptime(value, _LSTART))
    except ValueError:
        return None


# --- 13 roster / 14 knowledge / 15 migrations / 16 availability ----------------------------------------------------


def roster(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    try:
        roster = roster_plugins(probe.target)
    except ManagerError as exc:
        return (unknown("roster_matches_tree", f"The plugin roster is unreadable: {exc}", "manager_static", reason="profile_manifest_unreadable"),)
    plugins = probe.target / "plugins"
    tree = frozenset(entry.name for entry in plugins.iterdir() if entry.is_dir()) if plugins.is_dir() else frozenset()
    missing = sorted(set(roster) - tree)
    expected = cast(list[JsonValue], sorted(tree))
    observed = cast(list[JsonValue], list(roster))
    return (verdict("roster_matches_tree", not missing, "Every roster plugin exists in the release tree.", "manager_static", reason="roster_plugin_absent", observed=observed, expected=expected),)


def knowledge(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    checks = [_positive_search(probe)]
    for kb, path, title in _removed_articles(probe):
        checks.append(_negative_search(probe, kb, path, title))
    checks.append(unknown("session_retrieval", "No read-only session-retrieval probe is allowlisted.", "bridge_read:knowledge.read"))
    return tuple(checks)


def _removed_articles(probe: DoctorProbe) -> tuple[tuple[str, str, str], ...]:
    if probe.context is None or probe.contract is not DoctorContractKind.CANDIDATE:
        return ()
    try:
        return knowledge_removed_articles(probe.context)
    except ManagerError:
        return ()


def _search(probe: DoctorProbe, query: str) -> dict[str, JsonValue] | None:
    try:
        return probe.bridge(KNOWLEDGE_SEARCH_PROCESS_KEY, {"query": query, "top_k": 8}, "knowledge.read")
    except (AdapterError, AdapterProtocolError):
        return None


def _positive_search(probe: DoctorProbe) -> DiagnosticCheck:
    notes = probe.target / "RELEASE_NOTES.md"
    title = None
    if notes.is_file():
        title = next((line.removeprefix("# ").strip() for line in notes.read_text(encoding="utf-8", errors="replace").splitlines() if line.startswith("# ")), None)
    if title is None:
        return unknown("knowledge_positive_search", "RELEASE_NOTES.md has no title line to search for.", "bridge_read:knowledge.read", reason="release_notes_unavailable")
    data = _search(probe, title)
    if data is None:
        return unknown("knowledge_positive_search", "Knowledge search is unreachable.", "bridge_read:knowledge.read", reason="service_offline")
    non_empty = bool(json.dumps(data)) and any(isinstance(value, list) and value for value in data.values())
    return verdict("knowledge_positive_search", non_empty, "Knowledge search answers the release's title.", "bridge_read:knowledge.read", reason="knowledge_search_empty", observed=title)


def _negative_search(probe: DoctorProbe, kb: str, path: str, title: str) -> DiagnosticCheck:
    check_id = f"knowledge_negative_search:{kb}:{path}"
    data = _search(probe, title)
    if data is None:
        return unknown(check_id, "Knowledge search is unreachable.", "bridge_read:knowledge.read", reason="service_offline")
    hit = path in json.dumps(data, sort_keys=True)
    return verdict(check_id, not hit, f"No search hit cites the removed article {path}.", "bridge_read:knowledge.read", reason="knowledge_removal_not_applied", observed={"title": title, "hit": hit}, expected=[])


def migrations(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    if probe.candidate is None:
        return (unknown("migration", "No transition bundle binds release migrations under the diagnostic contract.", "manager_static"),)
    checks: list[DiagnosticCheck] = []
    for operation in probe.candidate.bundle.runtime_operations:
        if operation.stage not in {"migrations_pre", "runtime_reconcile"} or operation.operation_ref == "existing::runtime.plugin_cache_refresh":
            continue
        check_id = f"migration:{operation.operation_id}"
        source = f"existing_probe:{operation.operation_ref}"
        if operation.runner == "instance_bridge":
            checks.append(_platform_migration_check(probe, check_id))
            continue
        result = probe.results.get(operation.operation_id)
        if result is None:
            checks.append(unknown(check_id, f"Migration {operation.operation_id} was not probed.", source, reason=unbound_reason(probe)))
            continue
        checks.append(check(check_id, probe_status(result), f"Postcondition of release migration {operation.operation_id}.", source, reason=result.error_kind or "migration_incomplete", repair="reapply_migration", observed=result.checkpoint_status.value))
    return tuple(checks) or (not_applicable("migration", "release_migrations=none_declared", "manager_static"),)


def _platform_migration_check(probe: DoctorProbe, check_id: str) -> DiagnosticCheck:
    """T6/T7 postcondition: the migration's own dry-run, when a process key is carried (D7); else unknown."""
    source = "bridge_read:migration.read"
    key = None if probe.context is None else probe.context.operator_selections.get("process_key")
    if not isinstance(key, str):
        return unknown(check_id, "A platform migration's postcondition needs a declared process key (D7); bundle schema v1 declares none.", source)
    try:
        data = probe.bridge(key, {"dry_run": True}, "migration.read")
    except (AdapterError, AdapterProtocolError, StateConflictError):
        return unknown(check_id, "The migration's dry-run postcondition is unreachable.", source, reason="service_offline")
    applied = data.get("applied") is True
    return verdict(check_id, applied, "The platform migration answers its own dry-run as applied.", source, reason="migration_incomplete", observed=data, expected={"applied": True})


def availability(probe: DoctorProbe, name: str) -> tuple[DiagnosticCheck, ...]:
    del name
    installed = probe.installed_release
    if installed is None:
        descriptor = unknown("installed_descriptor_release", "The installed channel descriptor is unavailable.", "manager_static", reason="descriptor_unavailable")
    else:
        commit, tag = installed
        state = "current" if commit == probe.record.source_release.commit else "available"
        descriptor = check("installed_descriptor_release", DiagnosticStatus.VERIFIED, f"Installed channel release is {state} relative to the enrolled source.", "manager_static", observed={"commit": commit, "tag": tag, "state": state}, expected=probe.record.source_release.commit)
    checks = [descriptor]
    if probe.journal is not None:
        journal = probe.journal
        checks.append(check("active_update", DiagnosticStatus.VERIFIED, "The active update journal, as recorded.", "manager_static", observed={"operation_id": journal["operation_id"], "status": journal["status"], "attempts": len(cast(list[JsonValue], journal["attempts"])), "result": journal["result"], "source_mode": journal["source_mode"]}))
    return tuple(checks)
