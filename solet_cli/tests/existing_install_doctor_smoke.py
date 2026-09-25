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
import plistlib
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import ADAPTER_MODULE, FakeHost, Fixture, advance_to_source_advanced, build_fixture, data, db_spy, expect, git, runtime_fingerprint  # noqa: E402
from _step6_support import SimulatedCrash, byte_map, last_update_journal, run_to_promoted  # noqa: E402
from solet_manager import existing_install_doctor as doctor_module  # noqa: E402
from solet_manager import existing_install_doctor_service_checks as service_checks  # noqa: E402
from solet_manager import maintenance_inventory as inventory_module  # noqa: E402
from solet_manager import update_execution as execution_module  # noqa: E402
from solet_manager.doctor_journal import read_doctor_journal  # noqa: E402
from solet_manager.errors import ManagerError  # noqa: E402
from solet_manager.existing_install_doctor import run_doctor  # noqa: E402
from solet_manager.existing_solet_diagnostics import DiagnosticStatus  # noqa: E402
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


def _identity_case(root: Path, variant: str) -> DiagnosticStatus:
    """R5-style venv symlinks with a framework Python.app process display."""
    root = root.resolve()
    target = root / "target"
    home = root / "home"
    venv_bin = target / ".venv" / "bin"
    framework = root / "opt" / "homebrew" / "Cellar" / "python@3.13" / "3.13.15" / "Frameworks" / "Python.framework" / "Versions" / "3.13"
    base_python = framework / "bin" / "python3.13"
    app_python = framework / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    for directory in (venv_bin, base_python.parent, app_python.parent, target / "profile"):
        directory.mkdir(parents=True, exist_ok=True)
    base_python.write_bytes(b"fixture")
    app_python.write_bytes(b"fixture")
    (venv_bin / "python3.13").symlink_to(base_python)
    venv_python = venv_bin / "python3"
    venv_python.symlink_to("python3.13")
    _check(venv_python.resolve() == base_python and app_python != base_python, "R5 venv symlink and Python.app layout are distinct")
    label = "local.solet.fixture"
    plist = home / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True)
    arguments = [str(venv_python), "-m", "ananta.cli", "--app-home", str(target / "profile")]
    if variant == "foreign_plist":
        arguments[0] = str(root / "other" / ".venv" / "bin" / "python3")
    plist.write_bytes(b"not a plist" if variant == "bad_plist" else plistlib.dumps({"Label": label, "ProgramArguments": arguments}))
    command = f"{app_python} -m ananta.cli --app-home {target / 'profile'}"
    if variant == "decoy":
        command = f"/usr/bin/python3 -m ananta.cli --app-home /other/profile --note {target}/.venv/decoy"
    elif variant in {"pid_race", "foreign_plist", "bad_plist", "bad_recheck", "duplicate_recheck", "wrong_app_home"}:
        command = f"{venv_python} -m ananta.cli --app-home {target / 'profile'}"
    if variant == "wrong_app_home":
        command = f"{venv_python} -m ananta.cli --app-home {root / 'other' / 'profile'}"
    row_pid = 9999 if variant == "other_pid" else 3268
    recheck_pid = 3269 if variant == "pid_race" else 3268

    reads = 0

    def launchctl(_registry: object, _verb: str, _arguments: tuple[str, ...], _timeout: int) -> subprocess.CompletedProcess[str]:
        nonlocal reads
        reads += 1
        output = _identity_launchctl_output(variant, reads, recheck_pid)
        return subprocess.CompletedProcess(("launchctl",), 0, output, "")

    def run_ps(_timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(("ps",), 0, f"{row_pid} Fri Sep 18 12:00:00 2026 {command}\n", "")

    seams = SimpleNamespace(home=home, uid=501, launchctl=launchctl, run_ps=run_ps)
    record = SimpleNamespace(service_identity=SimpleNamespace(launchagent_label=label))
    probe = SimpleNamespace(target=target, seams=seams, record=record, registry=None, pid_observed=service_checks._parse_pid(launchctl(None, "print", (), 30).stdout), journal=None, invoked_vectors=[])
    return service_checks._process_identity(probe).status  # noqa: SLF001 -- focused production seam


def _real_shape_base(*, top_state: str | None, duplicate_state: str | None = None) -> list[str]:
    """Common header lines shared by every real-shape fixture variant below."""
    lines = ["gui/501/com.example.fixture-target = {\n"]
    lines.append("\tactive count = 1\n")
    lines.append("\tpath = /System/Library/LaunchAgents/com.example.fixture-target.plist\n")
    lines.append("\ttype = LaunchAgent\n")
    if top_state is not None:
        lines.append(f"\tstate = {top_state}\n")
    if duplicate_state is not None:
        lines.append(f"\tstate = {duplicate_state}\n")
    lines.append("\n\tprogram = /usr/libexec/example\n")
    return lines


def _real_shape_launchctl(*, top_state: str | None, top_pid: str | None, duplicate_state: str | None = None, coalition_state: str = "active") -> str:
    """A sanitized real-``launchctl print`` shape combining all three hazards at once: a
    ``label = {`` header, top-level ``state``/``pid`` fields, a nested ``pid-local
    endpoints`` block header (real output carries this; it must never be misread as a
    malformed ``pid`` field), two nested coalition blocks each with their own ``state``
    (real output carries these; they must never be counted as duplicate top-level
    state), and one value rendered with its own open and close brace on a single line
    (real output does this for an opaque nested XPC value; depth tracking must not
    require every ``}`` to be a line by itself). This is case 4 of 4 (the full combined
    shape) -- see ``_real_shape_pid_local_only`` and ``_real_shape_one_line_brace_only``
    below for the two hazards isolated on their own, so each RED-on-R2 result is
    attributable to exactly one defect rather than a confound of several.
    All identifiers below are synthetic -- no real host, user, or solet name.
    """
    lines = _real_shape_base(top_state=top_state, duplicate_state=duplicate_state)
    lines.append("\tpid-local endpoints = {\n\t\texample.endpoint => {\n\t\t\tport = 1\n\t\t}\n\t}\n")
    lines.append('\tdescriptor = {\n\t\t"aux-data" => {\n\t\t\t"a" => \t\t\t\t"b" => \t\t\t}\n\t}\n')
    if top_pid is not None:
        lines.append(f"\tpid = {top_pid}\n")
    lines.append(f"\n\tresource coalition = {{\n\t\tID = 1\n\t\ttype = resource\n\t\tstate = {coalition_state}\n\t\tactive count = 1\n\t}}\n")
    lines.append(f"\tjetsam coalition = {{\n\t\tID = 2\n\t\ttype = jetsam\n\t\tstate = {coalition_state}\n\t\tactive count = 1\n\t}}\n")
    lines.append("}\n")
    return "".join(lines)


def _real_shape_pid_local_only(*, top_state: str | None = "running", top_pid: str | None = "3268") -> str:
    """Case 2 of 4: isolates ONLY the ``pid-local endpoints`` block header hazard --
    no coalition blocks, no one-line-nested-brace value. R2's word-boundary regex
    matches the ``pid-local endpoints = {`` line as a malformed ``pid`` field (its key,
    ``pid-local endpoints``, is not exactly ``pid``) and fails closed on that line
    alone; this isolates that defect from the (unrelated) coalition duplicate-state and
    one-line-brace defects.
    """
    lines = _real_shape_base(top_state=top_state)
    lines.append("\tpid-local endpoints = {\n\t\texample.endpoint => {\n\t\t\tport = 1\n\t\t}\n\t}\n")
    if top_pid is not None:
        lines.append(f"\tpid = {top_pid}\n")
    lines.append("}\n")
    return "".join(lines)


def _real_shape_one_line_brace_only(*, top_state: str | None = "running", top_pid: str | None = "3268") -> str:
    """Case 3 of 4: isolates ONLY the one-line-nested-brace value hazard -- no
    pid-local block, no coalition blocks. Nothing here trips R2's word-boundary/
    malformed-key check, so R2 verifies this case too (GREEN); it is the
    per-character brace-counting fix specifically that this case regression-tests: a
    parser that still matches braces line-terminally (``endswith("{")`` /
    ``== "}"``) never sees the value's own close brace as a line by itself, so depth
    never returns to 0 and the job fails closed.
    """
    lines = _real_shape_base(top_state=top_state)
    lines.append('\tdescriptor = {\n\t\t"aux-data" => {\n\t\t\t"a" => \t\t\t\t"b" => \t\t\t}\n\t}\n')
    if top_pid is not None:
        lines.append(f"\tpid = {top_pid}\n")
    lines.append("}\n")
    return "".join(lines)


# A real `launchctl print gui/<uid>/<solet LaunchAgent label>` capture (2026-09-24), sanitized:
# label/paths -> synthetic `com.example.fixture-target`/`/Users/fixture/...`; the
# launchd socket hash, coalition IDs, domain asid, and PID -> generic placeholders
# (PID -> 3268, matching this fixture's convention). Structure and every other field
# are byte-identical to the capture. This case has no `pid-local endpoints` block and
# no one-line-nested-brace value -- unlike `real_shape_healthy` below, it isolates
# exactly the defect R44's R2 review blocked on: the two nested coalition blocks'
# own `state = active` lines, double-counted as duplicate top-level `state` by a
# depth-blind scan.
_REAL_CAPTURE_TARGET = "gui/501/com.example.fixture-target = {\n\tactive count = 1\n\tpath = /Users/fixture/Library/LaunchAgents/com.example.fixture-target.plist\n\ttype = LaunchAgent\n\tstate = running\n\n\tprogram = /Users/fixture/.local/releases/example/current/venv/bin/python3\n\targuments = {\n\t\t/Users/fixture/.local/releases/example/current/venv/bin/python3\n\t\t-m\n\t\texample_plugin.supervisor\n\t\t--app-home\n\t\t/Users/fixture/workspace/example/profile\n\t}\n\n\tworking directory = /Users/fixture/.local/runtime\n\n\tstdout path = /Users/fixture/.local/logs/example_autostart.log\n\tstderr path = /Users/fixture/.local/logs/example_autostart.log\n\tinherited environment = {\n\t\tSSH_AUTH_SOCK => /var/run/com.apple.launchd.XXXXXXXXXX/Listeners\n\t}\n\n\tdefault environment = {\n\t\tPATH => /usr/bin:/bin:/usr/sbin:/sbin\n\t}\n\n\tenvironment = {\n\t\tOSLogRateLimit => 64\n\t\tSOLET_NAME => example\n\t\tPATH => /opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\n\t\tXPC_SERVICE_NAME => com.example.fixture-target\n\t}\n\n\tdomain = gui/501 [100000]\n\tasid = 100000\n\tminimum runtime = 10\n\texit timeout = 5\n\truns = 2\n\tpid = 3268\n\timmediate reason = inefficient\n\tforks = 0\n\texecs = 2\n\tinitialized = 1\n\ttrampolined = 1\n\tstarted suspended = 0\n\tproxy started suspended = 0\n\tchecked allocations = 0 (queried = 1)\n\tchecked allocations reason = no host\n\tchecked allocations flags = 0x0\n\tlast exit code = 0\n\n\tresource coalition = {\n\t\tID = 210000\n\t\ttype = resource\n\t\tstate = active\n\t\tactive count = 1\n\t\tname = com.example.fixture-target\n\t}\n\n\tjetsam coalition = {\n\t\tID = 210001\n\t\ttype = jetsam\n\t\tstate = active\n\t\tactive count = 1\n\t\tname = com.example.fixture-target\n\t}\n\n\tspawn type = daemon (3)\n\tjetsam priority = 40\n\tjetsam memory limit (active) = (unlimited)\n\tjetsam memory limit (inactive) = (unlimited)\n\tjetsamproperties category = daemon\n\tjetsam thread limit = 32\n\tcpumon = default\n\n\tproperties = keepalive | runatload | inferred program\n}\n"


def _identity_launchctl_output(variant: str, read: int, recheck_pid: int) -> str:
    output = f"state = running\n pid = {3268 if read == 1 else recheck_pid}\n"
    if (variant.endswith("_initial") and read != 1) or (variant.endswith("_recheck") and read != 2) or (variant in {"bad_recheck", "duplicate_recheck"} and read != 2):
        return output
    cases = {
        "real_capture_target": _REAL_CAPTURE_TARGET,
        "real_shape_pid_local_only": _real_shape_pid_local_only(),
        "real_shape_one_line_brace_only": _real_shape_one_line_brace_only(),
        "real_shape_healthy": _real_shape_launchctl(top_state="running", top_pid="3268"),
        "real_shape_waiting": _real_shape_launchctl(top_state="waiting", top_pid="3268"),
        "real_shape_nested_state_only": _real_shape_launchctl(top_state=None, top_pid="3268", coalition_state="running"),
        "real_shape_duplicate_state": _real_shape_launchctl(top_state="running", top_pid="3268", duplicate_state="waiting"),
        "bad_recheck": "malformed",
        "duplicate_recheck": "state = running\n pid = 3268\n pid = 9999\n",
        "waiting_initial": "state = waiting\n pid = 3268\n",
        "waiting_recheck": "state = waiting\n pid = 3268\n",
        "missing_state_initial": "pid = 3268\n",
        "missing_state_recheck": "pid = 3268\n",
        "missing_pid_initial": "state = running\n",
        "missing_pid_recheck": "state = running\n",
        "unknown_state_initial": "state = something_else\n pid = 3268\n",
        "unknown_state_recheck": "state = something_else\n pid = 3268\n",
        "duplicate_state_initial": "state = running\n state = waiting\n pid = 3268\n",
        "duplicate_state_recheck": "state = running\n state = waiting\n pid = 3268\n",
        "duplicate_pid_initial": "state = running\n pid = 3268\n pid = 3268\n",
        "duplicate_pid_recheck": "state = running\n pid = 3268\n pid = 3268\n",
        "zero_pid_initial": "state = running\n pid = 0\n",
        "zero_pid_recheck": "state = running\n pid = 0\n",
        "negative_pid_initial": "state = running\n pid = -3268\n",
        "negative_pid_recheck": "state = running\n pid = -3268\n",
        "text_pid_initial": "state = running\n pid = unknown\n",
        "text_pid_recheck": "state = running\n pid = unknown\n",
    }
    return cases.get(variant, output)


def _assert_process_identity_regressions(root: Path) -> None:
    _check(_identity_case(root / "healthy", "healthy") is DiagnosticStatus.VERIFIED, "R5 framework Python.app from target venv verifies")
    _check(_identity_case(root / "decoy", "decoy") is DiagnosticStatus.FAILED, "decoy .venv/ argv on foreign interpreter refuses")
    _check(_identity_case(root / "other_pid", "other_pid") is DiagnosticStatus.FAILED, "unrelated ananta.cli process at another PID refuses")
    _check(_identity_case(root / "pid_race", "pid_race") is DiagnosticStatus.FAILED, "changed launchd PID after ps refuses")
    _check(_identity_case(root / "foreign_plist", "foreign_plist") is DiagnosticStatus.FAILED, "plist interpreter outside target venv refuses")
    _check(_identity_case(root / "bad_plist", "bad_plist") is DiagnosticStatus.FAILED, "malformed plist refuses")
    _check(_identity_case(root / "bad_recheck", "bad_recheck") is DiagnosticStatus.FAILED, "malformed launchctl PID recheck refuses")
    _check(_identity_case(root / "duplicate_recheck", "duplicate_recheck") is DiagnosticStatus.FAILED, "conflicting launchctl PID lines refuse")
    _check(_identity_case(root / "wrong_app_home", "wrong_app_home") is DiagnosticStatus.FAILED, "foreign app-home refuses")
    for variant in (
        "waiting_initial", "waiting_recheck", "missing_state_initial", "missing_state_recheck",
        "missing_pid_initial", "missing_pid_recheck",
        "unknown_state_initial", "unknown_state_recheck", "duplicate_state_initial", "duplicate_state_recheck",
        "duplicate_pid_initial", "duplicate_pid_recheck", "zero_pid_initial", "zero_pid_recheck",
        "negative_pid_initial", "negative_pid_recheck", "text_pid_initial", "text_pid_recheck",
    ):
        _check(_identity_case(root / variant, variant) is DiagnosticStatus.FAILED, f"{variant} launchctl read refuses")
    _check(_identity_case(root / "real_capture_target", "real_capture_target") is DiagnosticStatus.VERIFIED, "case 1/4: sanitized real launchctl-print capture, coalition duplicate-state ONLY (no pid-local block, no one-line-brace value) verifies -- RED on R2 attributable to the coalition depth-blindness defect alone")
    _check(_identity_case(root / "real_shape_pid_local_only", "real_shape_pid_local_only") is DiagnosticStatus.VERIFIED, "case 2/4: pid-local endpoints block header ONLY (no coalition blocks, no one-line-brace value) verifies -- RED on R2 attributable to the word-boundary/malformed-exact-key defect alone")
    _check(_identity_case(root / "real_shape_one_line_brace_only", "real_shape_one_line_brace_only") is DiagnosticStatus.VERIFIED, "case 3/4: one-line nested-brace value ONLY (no pid-local block, no coalition blocks) verifies -- GREEN on R2 (this hazard alone never trips R2's checks), RED only on a depth-tracking parser that still matches braces line-terminally, isolating the per-character brace-counting defect alone")
    _check(_identity_case(root / "real_shape_healthy", "real_shape_healthy") is DiagnosticStatus.VERIFIED, "case 4/4: real launchctl-print shape with all three hazards combined (header, pid-local endpoints, coalition blocks, one-line-nested value) verifies -- GREEN only once all three fixes are applied together")
    _check(_identity_case(root / "real_shape_waiting", "real_shape_waiting") is DiagnosticStatus.FAILED, "real-shape top-level waiting state refuses")
    _check(_identity_case(root / "real_shape_nested_state_only", "real_shape_nested_state_only") is DiagnosticStatus.FAILED, "real-shape with only a nested coalition state=running and no top-level state refuses (nested fields are never read as job state)")
    _check(_identity_case(root / "real_shape_duplicate_state", "real_shape_duplicate_state") is DiagnosticStatus.FAILED, "real-shape duplicate top-level state lines refuse")


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
        _assert_process_identity_regressions(root / "identity")
        _assert_router_offline(root / "router")
        _assert_rendering(root / "render")
    print(f"existing_install_doctor_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
