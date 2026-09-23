"""Step-6 runtime fault fixtures F-RT-1..8 (design section 6.2).

- F-RT-1/3: the router run promotes through ``runtime_attestation``; the single-colour run promotes through
  ``runtime_process_identity`` with the attestation seam poisoned (any attestation call raises) and the
  preview saying there is no zero-downtime rollback;
- F-RT-2 (in ``existing_install_router_failure_sweep_{1,2,3}_smoke.py``, sliced for the gate's per-smoke
  budget): the router candidate failure under BOTH sweep modes -- every crash point resumes to the reference
  terminal ``failed runtime_candidate_failed`` with at most one ``rec_`` apply and one recover, the runtime axis
  at the baseline, ``needs_attention``, and text naming the router previous as code-only;
- F-RT-4: startup quiescence -- readiness verifies on the fourth poll with no mutation between polls; a
  never-healthy service is ``failed readiness_timeout``;
- F-RT-5/7: a stale client subprocess and an armed watcher are advisory doctor failures with the exact repair
  codes; promotion is unaffected and nothing is killed;
- F-RT-6: a stale plugin cache is refreshed once; a crash after that apply never refreshes twice;
- F-RT-8: a deletion-only knowledge re-install runs the negative search; a search that keeps citing the removed
  article is ``failed knowledge_removal_not_applied``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import FakeHost, advance_to_source_advanced, build_fixture, data, db_spy, expect, runtime_fingerprint  # noqa: E402
from _step6_support import REMOVED_ARTICLE, SimulatedCrash, build_knowledge_removal_fixture, last_update_journal, run_to_promoted  # noqa: E402
from existing_install_router_cutover_smoke import BASELINE, CANDIDATE, _attestation, _router_fixture  # noqa: E402
from solet_manager.errors import UpdateFailedError  # noqa: E402
from solet_manager.existing_install_doctor import run_doctor  # noqa: E402
from solet_manager.models import CommandResult, ManagementState  # noqa: E402
from solet_manager.update_execution import apply_update, preview_update_instance  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _checks(result: CommandResult) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for section in cast(list[Any], result.data["sections"]):
        for check in cast(list[Any], data(section, "checks")):
            found[cast(str, data(check, "check_id"))] = cast(dict[str, Any], check)
    return found


def _assert_router_success(root: Path) -> None:
    fixture, controller = _router_fixture(root, attestations=[_attestation(BASELINE), _attestation(BASELINE), _attestation(BASELINE), _attestation(CANDIDATE)])
    result_status = run_to_promoted(fixture)
    _check(result_status == "promoted" and len([e for e in controller.envelopes if e["phase"] == "apply"]) == 1, "F-RT-1: router run promotes with exactly one apply")
    doctor = run_doctor(fixture.request)
    checks = _checks(doctor)
    _check(doctor.exit_code == 0 and checks["runtime_attestation"]["status"] == "verified" and checks["runtime_process_identity"]["status"] == "not_applicable", "the router's verified contract proves attestation, not process identity")


def _assert_single_colour_poisoned_attestation(root: Path) -> None:
    fixture = build_fixture(root, host=FakeHost(attestations=[]))
    advance_to_source_advanced(fixture)
    preview = preview_update_instance(fixture.request)
    _check(data(preview.data["lifecycle"], "zero_downtime_rollback") is False and data(preview.data["lifecycle"], "strategy") == "single_color_restart", "F-RT-3: the preview says there is no zero-downtime rollback")
    result = apply_update(fixture.request, cast(str, preview.data["runtime_approval_fingerprint"]))
    _check(result.status == "promoted", f"F-RT-3: promoted through runtime_process_identity with attestation poisoned: {result.status} {result.error_kind}")
    _check(not any(key.endswith("attest_runtime_code") for key, _ in fixture.host.bridge_calls), "the doctor never asked for an attestation on a legacy_direct install")
    doctor = run_doctor(fixture.request)
    checks = _checks(doctor)
    _check(checks["runtime_attestation"]["status"] == "not_applicable" and "launch_topology=legacy_direct" in cast(str, checks["runtime_attestation"]["expected"]) and checks["runtime_process_identity"]["status"] == "verified", "runtime_attestation is not_applicable under the closed topology condition")


def _assert_readiness(root: Path) -> None:
    # Step 7 section 7.3: the single-colour plan observes the service BEFORE fixing the strategy, once at the runtime
    # preview and once under the lock, so the health queue leads with two ``healthy`` answers; the unhealthy answers
    # are then what the restarted instance reports until the fourth readiness poll.
    fixture = build_fixture(root / "quiescence", host=FakeHost(health=[{"status": "healthy"}, {"status": "healthy"}, {"status": "unhealthy"}, {"status": "unhealthy"}, {"status": "unhealthy"}, {"status": "healthy"}]))
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    result = apply_update(fixture.request, fingerprint)
    _check(result.status == "promoted", f"F-RT-4: readiness verifies on the fourth poll: {result.status} {result.error_kind}")
    journal = last_update_journal(fixture)
    rows = {cast(str, data(row, "operation_id")): row for row in cast(list[Any], journal["runtime_operations"])}
    readiness = [cast(dict[str, Any], a) for a in cast(list[Any], data(rows["runtime_readiness"], "attempts"))]
    _check([a["phase"] for a in readiness] == ["probe"] and readiness[0]["evidence"]["signal"] == "bridge_health_healthy", "one readiness probe attempt journaled; no mutation between polls")
    _check(sum(1 for call in fixture.host.launchctl_calls if call[0] == "bootstrap") == 1, "no second bootstrap during quiescence")
    never = build_fixture(root / "never", host=FakeHost(health=[{"status": "healthy"}, {"status": "healthy"}, {"status": "unhealthy"}]))
    advance_to_source_advanced(never)
    fingerprint = runtime_fingerprint(never)
    exc = expect(UpdateFailedError, lambda: apply_update(never.request, fingerprint), "never-healthy accepted")
    _check(cast(UpdateFailedError, exc).error_kind == "readiness_timeout" and last_update_journal(never)["status"] == "failed" and never.record().management_state is ManagementState.NEEDS_ATTENTION, "never healthy: failed readiness_timeout with needs_attention")


def _assert_stale_processes(root: Path) -> None:
    fixture = build_fixture(root)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    fixture.host.processes = [
        {"pid": 4343, "lstart": "Fri Sep 18 12:00:00 2026", "command": "{TARGET}/.venv/bin/python3 -m ananta.cli"},
        {"pid": 9001, "lstart": "Mon Jan  1 00:00:00 2024", "command": "{TARGET}/.venv/bin/solet-bridge call x"},
        {"pid": 9002, "lstart": "Mon Jan  1 00:00:00 2024", "command": "{TARGET}/.venv/bin/solet-bridge watch"},
    ]
    result = apply_update(fixture.request, fingerprint)
    _check(result.status == "promoted", f"F-RT-5/7: promotion is unaffected by stale processes (advisory): {result.status}")
    doctor = run_doctor(fixture.request)
    stale = _checks(doctor)["stale_target_processes"]
    _check(stale["status"] == "failed" and stale["repair_code"] == "relaunch_client_session" and doctor.exit_code == 0, "stale client subprocess and watcher reported as an advisory failure with relaunch_client_session")
    kinds = sorted(item["kind"] for item in cast(list[Any], stale["observed"]))
    _check(kinds == ["client_subprocess", "watcher"], f"both a client subprocess and a watcher are classified: {kinds}")
    fixture.host.processes = [fixture.host.processes[0], fixture.host.processes[2]]
    doctor = run_doctor(fixture.request)
    _check(_checks(doctor)["stale_target_processes"]["repair_code"] == "rearm_watcher", "a lone armed watcher gets rearm_watcher; nothing is killed")
    _check(not any(call[0] in {"bootout", "kickstart"} for call in fixture.host.launchctl_calls[-2:]), "the doctor never bootouts or kickstarts")


def _assert_plugin_cache_refresh(root: Path) -> None:
    fixture = build_fixture(root)
    cache = fixture.home / ".claude" / "plugins" / "cache" / "fixture" / "coordination-hooks" / "1.0.0" / "hooks" / "wake_waiter.py"
    cache.write_text(cache.read_text() + "\n# stale\n")
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    seams = cast(Any, fixture.request.runtime_seams)
    real = seams.invoke_adapter
    applies: list[str] = []

    def crash_after_refresh(registry: Any, request: Any) -> Any:
        result = real(registry, request)
        if request.phase == "apply" and request.operation_ref == "existing::runtime.plugin_cache_refresh":
            applies.append(request.request_id)
            if len(applies) == 1:
                raise SimulatedCrash("after the plugin cache refresh apply")
        return result

    fixture.request = replace(fixture.request, runtime_seams=replace(seams, invoke_adapter=crash_after_refresh))
    expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash did not fire")
    _check(len(applies) == 1 and "# stale" not in cache.read_text(), "F-RT-6: the stale cache was refreshed once before the crash")
    resumed = apply_update(fixture.request, fingerprint)
    _check(resumed.status == "promoted" and len(applies) == 1, "resume verifies the refreshed cache by probe; no second refresh")
    doctor = run_doctor(fixture.request)
    _check(_checks(doctor)["plugin_cache_current"]["status"] == "verified", "doctor section 11 reports the cache current")


def _assert_knowledge_removal(root: Path) -> None:
    host = FakeHost()
    fixture = build_knowledge_removal_fixture(root / "applied", host=host)
    state = {"installed": False}

    def search(arguments: dict[str, Any]) -> dict[str, Any]:
        if state["installed"]:
            return {"results": []}
        return {"results": [{"path": REMOVED_ARTICLE, "title": "Retired Procedure"}]}

    real_bridge = host.invoke_bridge

    def bridge(registry: Any, key: str, arguments: Any, kind: str, timeout: int) -> Any:
        if kind == "knowledge":
            state["installed"] = True
        return real_bridge(registry, key, arguments, kind, timeout)

    host.search = search
    fixture.request = replace(fixture.request, runtime_seams=replace(cast(Any, fixture.request.runtime_seams), invoke_bridge=bridge))
    advance_to_source_advanced(fixture)
    preview = preview_update_instance(fixture.request)
    removed = cast(list[Any], preview.data["knowledge_removed_articles"])
    _check(len(removed) == 1 and data(removed[0], "path") == REMOVED_ARTICLE and data(removed[0], "title") == "Retired Procedure", f"the plan derives the removed article and its baseline title: {removed}")
    result = apply_update(fixture.request, cast(str, preview.data["runtime_approval_fingerprint"]))
    _check(result.status == "promoted", f"F-RT-8: re-install then negative search verified: {result.status} {result.error_kind}")
    journal = last_update_journal(fixture)
    rows = {cast(str, data(row, "operation_id")): row for row in cast(list[Any], journal["runtime_operations"])}
    attempts = [cast(dict[str, Any], a) for a in cast(list[Any], data(rows["knowledge_reinstall_0_kbx"], "attempts"))]
    _check(any(a["phase"] == "probe" and a["evidence"].get("hit") is False for a in attempts) and data(rows["knowledge_reinstall_0_kbx"], "status") == "verified" and data(rows["knowledge_reinstall_0_kbx"], "operation_type") == "knowledge_reinstall", "the negative search is journaled on the T8 row")
    stubborn_host = FakeHost()
    stubborn = build_knowledge_removal_fixture(root / "stubborn", host=stubborn_host)
    stubborn_host.search = lambda arguments: {"results": [{"path": REMOVED_ARTICLE}]}
    advance_to_source_advanced(stubborn)
    fingerprint = runtime_fingerprint(stubborn)
    exc = expect(UpdateFailedError, lambda: apply_update(stubborn.request, fingerprint), "stubborn search accepted")
    _check(cast(UpdateFailedError, exc).error_kind == "knowledge_removal_not_applied" and last_update_journal(stubborn)["status"] == "failed", "a search that keeps citing the removed path is failed knowledge_removal_not_applied")
    _check(json.dumps(last_update_journal(stubborn)["result"]).count("knowledge_removal_not_applied") == 1, "the terminal result names the reason")


def main() -> int:
    with db_spy(), TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        _assert_router_success(root / "rt1")
        _assert_single_colour_poisoned_attestation(root / "rt3")
        _assert_readiness(root / "rt4")
        _assert_stale_processes(root / "rt5")
        _assert_plugin_cache_refresh(root / "rt6")
        _assert_knowledge_removal(root / "rt8")
    print(f"existing_install_runtime_faults_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
