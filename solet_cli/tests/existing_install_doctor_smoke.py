"""Step-6 ``solet-manager doctor`` (design section 3; fixtures F-DOC-1..4).

- F-DOC-3: one fixture per branch of the 3.1 selector -- diagnostic import, verified instance, in-flight
  pre-source, in-flight crossed, terminal, promoted-with-pointer, drift HEAD -- and the digest-collision
  fixture (diagnostic digest == candidate digest) still selects by state;
- F-DOC-4: exit-code closure -- required failed -> 1, identity substituted -> 2, required missing -> 3,
  required unknown -> 3, all verified -> 0 -- with the full check list in every non-invalid result;
- F-DOC-1: health alone never verifies -- router topology with attestation unreachable is ``unknown
  service_offline`` (exit 3); legacy topology with a process outside ``<target>/.venv/`` is failed (exit 1);
- F-DOC-2: unknown is never rewritten; the write set is exactly W1 (+ W2 under the verified contract on a
  failed/missing required check); zero target byte writes by tree snapshot; no write-kind bridge call and no
  adapter apply ever run; the pointer-repair fixtures report ``reconcile --release-pointer`` and leave the
  pointer in place; human/JSON parity renders one line per check.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import ADAPTER_MODULE, FakeHost, Fixture, advance_to_source_advanced, build_fixture, data, db_spy, expect, git, runtime_fingerprint  # noqa: E402
from _step6_support import SimulatedCrash, byte_map, last_update_journal, run_to_promoted  # noqa: E402
from solet_manager import existing_install_doctor as doctor_module  # noqa: E402
from solet_manager import maintenance_inventory as inventory_module  # noqa: E402
from solet_manager import update_execution as execution_module  # noqa: E402
from solet_manager.doctor_journal import read_doctor_journal  # noqa: E402
from solet_manager.errors import ManagerError  # noqa: E402
from solet_manager.existing_install_doctor import run_doctor  # noqa: E402
from solet_manager.models import CommandResult  # noqa: E402
from solet_manager.rendering import render_human, render_json  # noqa: E402
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


def _sections(result: CommandResult) -> list[str]:
    return [cast(str, data(section, "section")) for section in cast(list[Any], result.data["sections"])]


class _WriteSpy:
    """F-DOC-2: every write the doctor could reach, counted; a write-kind bridge call or adapter apply fails loud."""

    def __init__(self, fixture: Fixture) -> None:
        self.fixture = fixture
        self.journal_writes = 0
        self.inventory_writes = 0
        self.doctor_writes = 0

    def __enter__(self) -> _WriteSpy:
        seams = cast(Any, self.fixture.request.runtime_seams)
        real_adapter, real_bridge = seams.invoke_adapter, seams.invoke_bridge

        def adapter(registry: Any, request: Any) -> Any:
            assert request.phase == "probe" and request.dry_run, f"doctor sent a non-probe adapter request: {request.phase}"
            return real_adapter(registry, request)

        def bridge(registry: Any, key: str, arguments: Any, kind: str, timeout: int) -> Any:
            assert kind not in {"migration", "knowledge"}, f"doctor invoked a write-kind bridge call: {kind} {key}"
            return real_bridge(registry, key, arguments, kind, timeout)

        self.fixture.request = replace(self.fixture.request, runtime_seams=replace(seams, invoke_adapter=adapter, invoke_bridge=bridge))
        real_journal = execution_module.write_update_journal
        real_inventory = inventory_module.write_maintenance_inventory_v2
        real_doctor = doctor_module.write_doctor_journal

        def journal(*args: Any, **kwargs: Any) -> None:
            self.journal_writes += 1
            real_journal(*args, **kwargs)

        def inventory(*args: Any, **kwargs: Any) -> None:
            self.inventory_writes += 1
            real_inventory(*args, **kwargs)

        def doctor(*args: Any, **kwargs: Any) -> None:
            self.doctor_writes += 1
            real_doctor(*args, **kwargs)

        self._patches = [patch.object(execution_module, "write_update_journal", journal), patch.object(inventory_module, "write_maintenance_inventory_v2", inventory), patch.object(doctor_module, "write_doctor_journal", doctor)]
        for item in self._patches:
            item.start()
        self._seams = seams
        return self

    def __exit__(self, *exc: object) -> None:
        for item in self._patches:
            item.stop()
        self.fixture.request = replace(self.fixture.request, runtime_seams=self._seams)


def _doctor(fixture: Fixture) -> tuple[CommandResult, _WriteSpy, dict[str, str]]:
    before = byte_map(fixture)
    with _WriteSpy(fixture) as spy:
        result = run_doctor(fixture.request)
    after = byte_map(fixture)
    _check(before == after and result.data["preservation"]["target_byte_writes"] == 0, "the doctor wrote no target byte (tree snapshot)")
    _check(spy.journal_writes == 0, "the doctor never writes the update journal")
    return result, spy, after


# --- F-DOC-3 selection --------------------------------------------------------------------------------


def _assert_diagnostic_import(root: Path) -> None:
    fixture = build_fixture(root, truthful=True)
    result, spy, _ = _doctor(fixture)
    _check(data(result.data["contract"], "kind") == "diagnostic" and result.status == "verified" and result.exit_code == 0, f"a truthful diagnostic import verifies under contract 3: {result.status} {result.error_kind}")
    checks = _checks(result)
    _check(checks["enrollment_drift"]["status"] == "verified", "enrollment_drift verifies against the real cached inspection bundle")
    _check(checks["managed_artifact"]["status"] == "unknown" and checks["dependency_closure"]["status"] == "unknown", "no bundle binds probes under contract 3; unknown, never failed")
    _check(checks["runtime_process_identity"]["status"] == "failed" or checks["runtime_process_identity"]["status"] == "verified", "the legacy service check is rendered under contract 3 (advisory)")
    _check(len(_sections(result)) == 16 and (spy.doctor_writes, spy.inventory_writes) == (1, 0), "sixteen sections; write set is exactly W1")
    _check(result.data["preservation"]["manager_state_writes"] == 1 and data(result.data["start_safety"], "safe") is False, "preservation discloses one Manager-state write; start is unsafe under contract 3")
    _check(fixture.record().management_state.value == "diagnostic", "a green diagnostic never promotes")


def _assert_verified_instance(root: Path) -> Fixture:
    fixture = build_fixture(root)
    run_to_promoted(fixture)
    record = fixture.record()
    _check(record.contract_identities.diagnostic_contract_digest == record.contract_identities.verified_contract_digest, "digest-collision fixture: diagnostic digest equals the candidate digest")
    result, spy, _ = _doctor(fixture)
    _check(data(result.data["contract"], "kind") == "verified" and result.status == "verified" and result.exit_code == 0, f"the verified instance verifies under contract 2 by state, not digest: {result.status} {result.error_kind}")
    checks = _checks(result)
    _check(checks["runtime_process_identity"]["status"] == "verified" and checks["runtime_attestation"]["status"] == "not_applicable", "legacy topology: process identity required, attestation not_applicable under the closed condition")
    _check(checks["runtime_attestation"]["expected"] == "launch_topology=legacy_direct;service=single_color", "not_applicable names its closed condition")
    _check((spy.doctor_writes, spy.inventory_writes) == (1, 0) and data(result.data["start_safety"], "safe") is True, "W1 only on a green verified run; start is safe")
    journal = read_doctor_journal(fixture.paths.operation_path(record.instance_id, cast(str, result.data["doctor_operation_id"])))
    _check(len(cast(list[Any], journal["runs"])) == 1 and journal["contract"]["kind"] == "verified", "one doctor journal per (instance, contract), one run appended")
    again, _, _ = _doctor(fixture)
    _check(again.data["doctor_operation_id"] == result.data["doctor_operation_id"] and again.data["run"] == 1, "a second run appends to the same journal")
    return fixture


def _assert_in_flight(root: Path) -> None:
    pre = build_fixture(root / "pre")
    preview = preview_update_instance(pre.request)
    fingerprint = cast(str, preview.data["approval_fingerprint"])
    real = execution_module._run_git  # noqa: SLF001

    def crash_before_fetch(cwd: Path, args: tuple[str, ...], *, hooks_dir: Path | None = None) -> Any:
        if args[0] == "fetch":
            raise SimulatedCrash("before fetch")
        return real(cwd, args, hooks_dir=hooks_dir)

    with patch.object(execution_module, "_run_git", crash_before_fetch):
        expect(SimulatedCrash, lambda: apply_update(pre.request, fingerprint), "crash did not fire")
    result, _, _ = _doctor(pre)
    _check(data(result.data["contract"], "kind") == "diagnostic" and "pre-source" in json.dumps(result.data["active_operation"]), "in-flight pre-source selects the pre-update contract and reports the active update")
    _check(pre.record().active_operation is not None, "the doctor left the pointer in place")
    crossed = build_fixture(root / "crossed")
    advance_to_source_advanced(crossed)
    result, _, _ = _doctor(crossed)
    checks = _checks(result)
    _check(data(result.data["contract"], "kind") == "candidate" and result.status == "incomplete" and result.exit_code == 3, f"crossed the source boundary: candidate contract; artifacts not yet hydrated: {result.status}")
    _check(checks["managed_artifact:shell_startup_block"]["status"] == "missing", "an absent managed artifact is missing, never failed")
    _check(checks["active_update"]["status"] == "verified" and data(checks["active_update"]["observed"], "status") == "source_advanced", "section 16 reports the active update")
    drift = build_fixture(root / "drift")
    preview = preview_update_instance(drift.request)
    with patch.object(execution_module, "_run_git", crash_before_fetch):
        expect(SimulatedCrash, lambda: apply_update(drift.request, cast(str, preview.data["approval_fingerprint"])), "crash did not fire")
    git(drift.target, "-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit", "--quiet", "--allow-empty", "-m", "moved")
    result, _, _ = _doctor(drift)
    _check(result.exit_code == 3 and result.error_kind == "managed_identity_drift" and len(_sections(result)) == 16, "HEAD elsewhere: exit 3 managed_identity_drift with the full report still rendered (never collapse)")


def _assert_terminal_and_pointer_pending(root: Path) -> None:
    terminal = build_fixture(root / "terminal", host=FakeHost(bootstrap_fails=True))
    advance_to_source_advanced(terminal)
    fingerprint = runtime_fingerprint(terminal)
    expect(ManagerError, lambda: apply_update(terminal.request, fingerprint), "bootstrap failure accepted")
    _check(last_update_journal(terminal)["status"] == "failed", "terminal failed fixture")
    result, _, _ = _doctor(terminal)
    _check(data(result.data["contract"], "kind") == "candidate" and result.exit_code in {1, 3} and result.error_kind == "launchagent_start_failed", "a terminal journal at the candidate: candidate contract, the terminal reason carried as the pending repair")
    _check("reconcile" in cast(str, result.repair), "the repair names reconcile")
    promoted = build_fixture(root / "promoted")
    advance_to_source_advanced(promoted)
    fingerprint = runtime_fingerprint(promoted)
    from solet_manager import update_promotion  # noqa: PLC0415

    real_release = update_promotion.release_terminal_pointer

    def no_release(paths: Any, record: Any) -> Any:
        raise SimulatedCrash("after the promoted write, before the pointer release")

    with patch.object(update_promotion, "release_terminal_pointer", no_release):
        expect(SimulatedCrash, lambda: apply_update(promoted.request, fingerprint), "crash did not fire")
    _check(last_update_journal(promoted)["status"] == "promoted" and promoted.record().active_operation is not None, "promoted journal with the pointer still set")
    result, spy, _ = _doctor(promoted)
    _check(data(result.data["contract"], "kind") == "verified" and result.exit_code == 3 and result.error_kind == "pointer_release_pending" and "reconcile fixture --release-pointer --yes" in cast(str, result.repair), "promoted-with-pointer: verified contract, pointer_release_pending reported")
    _check(promoted.record().active_operation is not None and spy.inventory_writes == 0, "doctor reports the pointer repair and never performs it (D5)")
    del real_release


# --- F-DOC-1 / F-DOC-4 ----------------------------------------------------------------------------------


def _assert_exit_codes(root: Path) -> None:
    fixture = build_fixture(root)
    run_to_promoted(fixture)
    green, _, _ = _doctor(fixture)
    _check((green.exit_code, green.status) == (0, "verified"), "all verified -> 0")
    _assert_required_failed(fixture)
    _assert_executed_code_edit(fixture)
    _assert_advisory_and_unknown(fixture, green)
    _assert_missing_and_invalid(fixture)


def _assert_required_failed(fixture: Fixture) -> None:
    fixture.host.processes = [{"pid": fixture.host.pids[0], "lstart": "Fri Sep 18 12:00:00 2026", "command": "/usr/bin/python3 -m something.else"}]
    failed, spy, _ = _doctor(fixture)
    checks = _checks(failed)
    _check((failed.exit_code, failed.status, checks["runtime_process_identity"]["status"], checks["bridge_health"]["status"]) == (1, "failed", "failed", "verified"), "F-DOC-1 legacy: healthy bridge but a process outside <target>/.venv/ -> failed exit 1; health alone never verifies")
    record = fixture.record()
    _check((spy.inventory_writes, record.management_state.value, "runtime_process_outside_target" in record.update_eligibility.reason_codes) == (1, "needs_attention", True), "W2: a failed required check on a verified instance publishes needs_attention (blocked eligibility, no pointer)")
    again, spy, _ = _doctor(fixture)
    _check((spy.inventory_writes, again.data["preservation"]["manager_state_writes"], fixture.record().management_state.value) == (0, 2, "needs_attention"), "W2 is idempotent: the CAS finds the row already carrying the values and writes nothing")
    fixture.host.processes = None


def _assert_executed_code_edit(fixture: Fixture) -> None:
    """F-DOC-4 (Step 7): an unstaged edit under an executed-code root fails ``local_state_admissible`` -> exit 1."""
    adapter = fixture.target / ADAPTER_MODULE
    original = adapter.read_bytes()
    adapter.write_bytes(original + b"\n# local edit\n")
    failed, _, _ = _doctor(fixture)
    check = _checks(failed)["local_state_admissible"]
    _check((failed.exit_code, failed.status, check["status"], check["reason_code"], check["observed"]["executed_code_overlap"]) == (1, "failed", "failed", "executed_code_modified", [ADAPTER_MODULE]), f"F-DOC-4 (Step 7): an unstaged edit under a roster plugin's tree is executed_code_modified, required failed -> 1: {failed.exit_code} {check['status']} {check.get('reason_code')} {check['observed'].get('executed_code_overlap')}")
    adapter.write_bytes(original)


def _assert_advisory_and_unknown(fixture: Fixture, green: CommandResult) -> None:
    fixture.host.health = [{"status": "unhealthy"}]
    unhealthy, _, _ = _doctor(fixture)
    _check((unhealthy.exit_code, _checks(unhealthy)["bridge_health"]["status"]) == (0, "failed"), "an advisory failure never changes the verdict")
    fixture.host.health = [{"status": "healthy"}]
    fixture.host.ps_fails = True
    unknown, _, _ = _doctor(fixture)
    checks = _checks(unknown)
    shape = (unknown.exit_code, unknown.status, checks["runtime_process_identity"]["status"], checks["runtime_process_identity"]["reason_code"], checks["stale_target_processes"]["status"])
    _check(shape == (3, "incomplete", "unknown", "service_offline", "unknown"), "required unknown -> 3; unknown is never rewritten to failed")
    fixture.host.ps_fails = False
    _check(len(_checks(unknown)) == len(_checks(green)), "the full check list is present in every non-invalid result")


def _assert_missing_and_invalid(fixture: Fixture) -> None:
    plist = fixture.plist_path
    original = plist.read_bytes()
    plist.write_bytes(b"not a plist")
    garbage, _, _ = _doctor(fixture)
    _check((garbage.exit_code, _checks(garbage)["launch_topology"]["status"], _checks(garbage)["launchagent_label_coherence"]["status"]) == (1, "unknown", "failed"), "an unreadable plist: topology unknown (never failed) while the label check fails on its own evidence")
    plist.unlink()
    missing, _, _ = _doctor(fixture)
    _check((missing.exit_code, _checks(missing)["launchagent_label_coherence"]["status"], len(_sections(missing))) == (3, "missing", 16), "required missing -> 3; all sixteen sections rendered")
    plist.write_bytes(original)
    target = fixture.target
    moved = target.with_name("moved")
    shutil.move(str(target), str(moved))
    os.mkdir(target)
    invalid = run_doctor(fixture.request)
    _check((invalid.exit_code, invalid.status, invalid.error_kind) == (2, "invalid", "target_identity_invalid"), "identity substituted -> 2 (A6)")
    os.rmdir(target)
    shutil.move(str(moved), str(target))


def _assert_router_offline(root: Path) -> None:
    from existing_install_router_cutover_smoke import BASELINE, CANDIDATE, _attestation, _router_fixture  # noqa: PLC0415

    fixture, _ = _router_fixture(root, attestations=[_attestation(BASELINE), _attestation(BASELINE), _attestation(BASELINE), _attestation(CANDIDATE)])
    run_to_promoted(fixture)
    fixture.host.attestations = []
    result, _, _ = _doctor(fixture)
    checks = _checks(result)
    _check(result.exit_code == 3 and checks["runtime_attestation"]["status"] == "unknown" and checks["runtime_attestation"]["reason_code"] == "service_offline" and checks["bridge_health"]["status"] == "verified", "F-DOC-1 router: healthy bridge, attestation unreachable -> unknown service_offline, exit 3")
    _check(checks["runtime_process_identity"]["status"] == "not_applicable", "the legacy check is not_applicable on a supervisor topology")


def _assert_rendering(root: Path) -> None:
    fixture = build_fixture(root)
    run_to_promoted(fixture)
    result, _, _ = _doctor(fixture)
    human = render_human(result)
    payload = json.loads(render_json(result))
    ids = [check["check_id"] for section in payload["data"]["sections"] for check in section["checks"]]
    _check(all(check_id in human for check_id in ids) and all(f"[{name}]" in human for name in _sections(result)), "human rendering lists every section and one line per check")
    _check("Step-6" not in human and "Step-6" not in render_json(result), "no Step-6 placeholder survives in any rendered string")


def main() -> int:
    with db_spy(), TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        _assert_diagnostic_import(root / "diagnostic")
        _assert_verified_instance(root / "verified")
        _assert_in_flight(root / "inflight")
        _assert_terminal_and_pointer_pending(root / "terminal")
        _assert_exit_codes(root / "exits")
        _assert_router_offline(root / "router")
        _assert_rendering(root / "render")
    print(f"existing_install_doctor_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
