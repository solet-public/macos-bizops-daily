"""Step-6 promotion (design section 5) and the zero-delta source path (section 4.8): F-PR-1/2, F-ZD-1/2, F-B-9.

- F-PR-1: a crash after each of the four steps of section 5.3 (journal ``promoting``, the promotion CAS,
  journal ``promoted``, the pointer release) resumes to one promotion, a verified row, a released pointer;
- F-B-9 / F-PR-2: a first doctor run that sees the pre-restart process is ``doctor_incomplete`` with
  ``needs_attention``; the rerun promotes; the doctor journal shows two runs and one promotion;
- F-ZD-1: an already-current diagnostic import (``enroll(truthful=True)`` at the candidate) gets
  ``verify_preview_ready`` with a fingerprint, reaches ``source_advanced`` with no ``fetch``/``merge`` in the
  observed argv set, then promotes; a second ``--dry-run`` is ``already_current`` with no fingerprint;
- F-ZD-2: a ``verify`` fingerprint is refused by an ``advance`` apply and vice versa (``probe_drift``);
- promotion preconditions: a tampered doctor evidence digest, or axes not at the candidate, refuse under
  ``promoting`` without an illegal second transition.
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import FakeHost, Fixture, advance_to_source_advanced, build_fixture, db_spy, expect, runtime_fingerprint  # noqa: E402
from _step6_support import SimulatedCrash, last_update_journal, observe_git, run_to_promoted  # noqa: E402
from solet_manager import maintenance_inventory as inventory_module  # noqa: E402
from solet_manager import update_promotion as promotion  # noqa: E402
from solet_manager.doctor_journal import read_doctor_journal  # noqa: E402
from solet_manager.errors import ProbeDriftError, StateConflictError  # noqa: E402
from solet_manager.models import ManagementState  # noqa: E402
from solet_manager.update_execution import apply_update, preview_update_instance  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _status(fixture: Fixture) -> str:
    return cast(str, last_update_journal(fixture)["status"])


def _assert_promoted(fixture: Fixture, label: str) -> None:
    record = fixture.record()
    journal = last_update_journal(fixture)
    promotions = sum(1 for item in cast(list[Any], journal["attempts"]) if cast(dict[str, Any], item)["status"] == "promoted")
    _check(journal["status"] == "promoted" and promotions == 1, f"{label}: one promoted transition")
    _check(record.active_operation is None and record.management_state is ManagementState.VERIFIED and record.verified_release is not None and record.verified_release.commit == fixture.candidate.commit and record.contract_identities.verified_contract_digest == fixture.contract and record.contract_identities.current_contract_digest == fixture.contract, f"{label}: verified row, pointer released")
    _check(record.last_verified_operation_id == journal["operation_id"] and record.last_verified_at is not None, f"{label}: last_verified names the update")


# --- F-PR-1 ---------------------------------------------------------------------------------------


def _crash_after(fixture: Fixture, fingerprint: str, target: str) -> None:
    """Crash after one named step of section 5.3; the next --yes resumes."""
    if target == "promoting_write":
        real = promotion.finish
        original_advance = None

        def advance_crash(self: Any, status: str, note: str, *, result: Any = None) -> None:
            cast(Any, original_advance)(self, status, note, result=result)
            if status == "promoting":
                raise SimulatedCrash("after the promoting write")

        from solet_manager.update_runtime_execution import RuntimeExecution  # noqa: PLC0415

        original_advance = RuntimeExecution.advance_status
        with patch.object(RuntimeExecution, "advance_status", advance_crash):
            expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash did not fire")
        del real
        return
    if target == "promotion_cas":
        real_cas = inventory_module.publish_promotion

        def cas_crash(*args: Any, **kwargs: Any) -> Any:
            real_cas(*args, **kwargs)
            raise SimulatedCrash("after the promotion CAS")

        with patch.object(promotion, "publish_promotion", cas_crash):
            expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash did not fire")
        return
    if target == "promoted_write":
        real_release = promotion.release_terminal_pointer

        def release_crash(paths: Any, record: Any) -> Any:
            raise SimulatedCrash("after the promoted write, before the release")

        with patch.object(promotion, "release_terminal_pointer", release_crash):
            expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash did not fire")
        del real_release
        return
    raise AssertionError(target)


def _assert_promotion_crashes(root: Path) -> None:
    expectations = {"promoting_write": ("promoting", True), "promotion_cas": ("promoting", True), "promoted_write": ("promoted", True)}
    for target, (crashed_status, pointer_set) in expectations.items():
        fixture = build_fixture(root / target)
        advance_to_source_advanced(fixture)
        fingerprint = runtime_fingerprint(fixture)
        _crash_after(fixture, fingerprint, target)
        _check(_status(fixture) == crashed_status and (fixture.record().active_operation is not None) is pointer_set, f"{target}: crashed at {crashed_status} with the pointer {'set' if pointer_set else 'released'}")
        if crashed_status == "promoted":
            exc = expect(ProbeDriftError, lambda fixture=fixture, fingerprint=fingerprint: apply_update(fixture.request, fingerprint), "resume after promoted accepted")
            _check("already promoted" in str(exc) or "already promoted" in str(cast(ProbeDriftError, exc).repair), f"{target}: the pointer is released on sight and the rerun is refused as already promoted")
        else:
            resumed = apply_update(fixture.request, fingerprint)
            _check(resumed.status == "promoted", f"{target}: resume reaches promoted: {resumed.status} {resumed.error_kind}")
        _assert_promoted(fixture, target)
        cas_writes = sum(1 for item in cast(list[Any], last_update_journal(fixture)["attempts"]) if cast(dict[str, Any], item)["status"] == "promoting")
        _check(cas_writes == 1, f"{target}: exactly one promoting transition")


# --- F-B-9 / F-PR-2 ------------------------------------------------------------------------------


def _assert_doctor_incomplete_then_promote(root: Path) -> None:
    host = FakeHost(pids=[4242, 4343])
    fixture = build_fixture(root, host=host)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    # After the restart the fixture's process table names a foreign interpreter once, then the target's own.
    host.processes = [{"pid": 4343, "lstart": "Fri Sep 18 12:00:00 2026", "command": "/usr/bin/python3 -m other"}]
    first = apply_update(fixture.request, fingerprint)
    _check((first.status, first.exit_code, _status(fixture)) == ("doctor_incomplete", 1, "doctor_incomplete"), f"first doctor run fails on the service check: {first.status} {first.exit_code}")
    record = fixture.record()
    _check((record.management_state, record.active_operation is not None, record.verified_release) == (ManagementState.NEEDS_ATTENTION, True, None), "needs_attention published; pointer kept; not promoted")
    _check({"runtime_process_outside_target", "update_in_progress"} <= set(record.update_eligibility.reason_codes), "eligibility carries the check's reason code beside update_in_progress")
    host.processes = None
    second = apply_update(fixture.request, fingerprint)
    _check(second.status == "promoted", f"the rerun promotes: {second.status} {second.error_kind}")
    _assert_promoted(fixture, "F-B-9")
    runs = _doctor_runs(fixture)
    _check([run["status"] for run in runs] == ["failed", "verified"], "the doctor journal shows two runs, one promotion")
    _check(sum(1 for call in fixture.host.launchctl_calls if call[0] == "bootout") == 1, "no mutation was repeated by the rerun")


def _doctor_runs(fixture: Fixture) -> list[dict[str, Any]]:
    notes = [cast(str, cast(dict[str, Any], item)["note"]) for item in cast(list[Any], last_update_journal(fixture)["attempts"])]
    doctor_id = next(note.split("doctor_operation_id=")[1].split(";")[0] for note in notes if "doctor_operation_id=" in note)
    doctor = read_doctor_journal(fixture.paths.operation_path(fixture.record().instance_id, doctor_id))
    return cast(list[dict[str, Any]], doctor["runs"])


# --- F-ZD-1 / F-ZD-2 ------------------------------------------------------------------------------


def _assert_zero_delta_promotion(root: Path) -> None:
    fixture = build_fixture(root, truthful=True, at_candidate=True)
    record = fixture.record()
    _check((record.source_release.commit, record.management_state) == (fixture.candidate.commit, ManagementState.DIAGNOSTIC), "an already-current diagnostic import")
    fingerprint = _assert_verify_preview(fixture)
    with observe_git() as observer:
        applied = apply_update(fixture.request, fingerprint)
    _check((applied.status, observer.verbs() & {"fetch", "merge"}) == ("source_advanced", set()), "--yes reaches source_advanced with no fetch/merge")
    journal = last_update_journal(fixture)
    notes = " | ".join(cast(str, cast(dict[str, Any], item)["note"]) for item in cast(list[Any], journal["attempts"]))
    _check((journal["source_mode"], "verify mode: no fetch" in notes, "verify mode: no merge" in notes) == ("verify", True, True), "the landed graph is walked nominally with verify-mode notes (n2)")
    runtime = runtime_fingerprint(fixture)
    with observe_git() as observer:
        result = apply_update(fixture.request, runtime)
    _check((result.status, observer.verbs() & {"fetch", "merge"}) == ("promoted", set()), f"every postcondition verifies by probe, the rest applies, then promoted: {result.status} {result.error_kind}")
    _assert_promoted(fixture, "F-ZD-1")
    again = preview_update_instance(fixture.request)
    _check((again.status, again.exit_code, again.data["approval_fingerprint"]) == ("already_current", 0, None), "a second --dry-run is already_current with no fingerprint")


def _assert_verify_preview(fixture: Fixture) -> str:
    preview = preview_update_instance(fixture.request)
    _check((preview.status, preview.exit_code, preview.data["approval_fingerprint"] is not None, preview.data["source_mode"]) == ("verify_preview_ready", 0, True, "verify"), f"verify_preview_ready with a fingerprint: {preview.status} {preview.error_kind}")
    topology = cast(dict[str, Any], preview.data["topology"])
    _check(("already_current" in cast(list[str], topology["reasons"]), topology["actionable"]) == (True, True), "already_current is recorded but does not block in verify mode")
    _check(preview.data["planned_actions"] == ["manager.acquire_update_candidate", "target.verify_exact_candidate"], "verify planned actions")
    return cast(str, preview.data["approval_fingerprint"])


def _assert_fingerprints_not_interchangeable(root: Path) -> None:
    verify = build_fixture(root / "verify", truthful=True, at_candidate=True)
    advance = build_fixture(root / "advance")
    verify_fp = cast(str, preview_update_instance(verify.request).data["approval_fingerprint"])
    advance_fp = cast(str, preview_update_instance(advance.request).data["approval_fingerprint"])
    _check(verify_fp != advance_fp, "distinct fingerprints")
    expect(ProbeDriftError, lambda: apply_update(verify.request, advance_fp), "advance fingerprint accepted by a verify apply")
    expect(ProbeDriftError, lambda: apply_update(advance.request, verify_fp), "verify fingerprint accepted by an advance apply")
    _check(verify.record().active_operation is None and advance.record().active_operation is None, "nothing was published by the refused applies")


# --- preconditions --------------------------------------------------------------------------------------


def _assert_preconditions(root: Path) -> None:
    fixture = build_fixture(root)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    from solet_manager.update_runtime_execution import RuntimeExecution  # noqa: PLC0415

    original = RuntimeExecution.advance_status

    def crash_after_verified(self: Any, status: str, note: str, *, result: Any = None) -> None:
        original(self, status, note, result=result)
        if status == "doctor_verified":
            raise SimulatedCrash("after doctor_verified")

    with patch.object(RuntimeExecution, "advance_status", crash_after_verified):
        expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash did not fire")
    _check(_status(fixture) == "doctor_verified", "journal at doctor_verified")
    record = fixture.record()
    path = fixture.paths.operation_path(record.instance_id, cast(str, record.active_operation and record.active_operation.operation_id))
    doctor_path = next(item for item in path.parent.glob("opr_*.json") if item != path and "\"doctor\"" in item.read_text())
    original_doctor = doctor_path.read_bytes()
    doctor_path.write_bytes(original_doctor.replace(b'"status": "verified"', b'"status": "incomplete"'))
    result = apply_update(fixture.request, fingerprint)
    _check(result.status == "doctor_verified" and result.exit_code == 3 and result.error_kind in {"promotion_interrupted", "doctor_interrupted", "corrupt_state"}, f"a doctor journal that no longer carries the verified run refuses promotion without an illegal transition: {result.status} {result.error_kind}")
    doctor_path.write_bytes(original_doctor)
    resumed = apply_update(fixture.request, fingerprint)
    _check(resumed.status == "promoted", "with the evidence restored, promotion completes")
    _assert_promoted(fixture, "preconditions")
    expect(StateConflictError, lambda: inventory_module.publish_promotion(fixture.paths.maintenance_inventory_path, fixture.record(), operation_id="opr_" + "0" * 32, verified_release=cast(Any, fixture.record().verified_release), verified_contract_digest=fixture.contract, eligibility=fixture.record().update_eligibility, doctor_evidence_digest="sha256:" + "0" * 64, now="2026-09-18T00:00:00Z"), "promotion CAS without the pointer accepted")


def main() -> int:
    with db_spy(), TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        _assert_promotion_crashes(root / "pr1")
        _assert_doctor_incomplete_then_promote(root / "b9")
        _assert_zero_delta_promotion(root / "zd1")
        _assert_fingerprints_not_interchangeable(root / "zd2")
        _assert_preconditions(root / "pre")
        reference = build_fixture(root / "reference")
        run_to_promoted(reference)
        _assert_promoted(reference, "reference")
    print(f"update_promotion_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
