"""Step-6 boundary fixtures around the single-colour reference run (design section 6.2: F-B-*, F-M-*, F-HO-*, F-RB-1).

The reference run takes an enrolled row through Step 4, the runtime approval,
every runtime stage, the final doctor and promotion; the exhaustive crash
sweeps over EVERY durable write and EVERY apply boundary of that run (F-CR-1/2)
live in ``update_crash_sweep_writes_{1,2,3,4}_smoke.py`` and
``update_crash_sweep_applies_smoke.py`` so each stays inside the gate's
per-smoke budget.  The named boundary fixtures here cover the rows the sweep
reaches only implicitly: fetch-completed abandon, fast-forward-completed
refusal, the mid-merge dirty tree, the manual migration block with its
successor, the forward-only migration block, the retry-safe additive migration,
the durable-copy handoff legs (cache purge, Manager upgrade, target edit, copy
tamper), and the no-destructive-git-vector scan.  Everything runs under the
fail-on-call database spy.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import (  # noqa: E402
    FakeHost,
    Fixture,
    advance_to_source_advanced,
    build_fixture,
    data,
    db_spy,
    expect,
    git,
    runtime_fingerprint,
)
from _step6_support import (  # noqa: E402
    CrashSweep,
    SimulatedCrash,
    additive_migration_document,
    forward_only_document,
    last_update_journal,
    manual_migration_document,
    observe_git,
    run_to_promoted,
)
from solet_manager import update_execution as execution_module  # noqa: E402
from solet_manager.contract_copies import read_transition_contract  # noqa: E402
from solet_manager.errors import AbandonRefusedError, ManagerError, SourceTransitionIncompleteError, TransitionContractMismatchError, UpdateBlockedError  # noqa: E402
from solet_manager.paths import transition_contract_dir  # noqa: E402
from solet_manager.update_execution import apply_update, preview_update_instance  # noqa: E402
from solet_manager.update_reconcile import abandon_update, preview_reconcile, reconcile_update  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _status(fixture: Fixture) -> str:
    return cast(str, fixture.journal()["status"])


# --- F-CR-1 / F-CR-2 --------------------------------------------------------------------------------


def _assert_sweep_reference(root: Path) -> None:
    """The reference run and the boundary counts; the exhaustive write/apply sweeps run in the sibling
    ``update_crash_sweep_writes_{1,2,3,4}_smoke.py`` and ``update_crash_sweep_applies_smoke.py`` so each stays
    inside the gate's per-smoke budget."""
    sweep = CrashSweep(build=lambda path: build_fixture(path), scenario=run_to_promoted, root=root)
    _, writes, applies = sweep.reference()
    _check((writes >= 25, applies >= 5) == (True, True), f"reference run crossed {writes} write and {applies} apply boundaries")


# --- F-B-1 / F-B-2 / F-B-2b --------------------------------------------------------------------------


def _assert_fetch_boundary(root: Path) -> None:
    """F-B-1: crash after ``git fetch``; resume refetches nothing (ref identity-equal); abandon leaves the ref."""
    fixture = build_fixture(root)
    preview = preview_update_instance(fixture.request)
    fingerprint = cast(str, preview.data["approval_fingerprint"])
    real = execution_module._run_git  # noqa: SLF001

    def crash_after_fetch(cwd: Path, args: tuple[str, ...], *, hooks_dir: Path | None = None) -> Any:
        completed = real(cwd, args, hooks_dir=hooks_dir)
        if args[0] == "fetch":
            raise SimulatedCrash("fetch")
        return completed

    from unittest.mock import patch  # noqa: PLC0415

    with patch.object(execution_module, "_run_git", crash_after_fetch):
        expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash did not fire")
    _check(_status(fixture) == "operation_published" and git(fixture.target, "rev-parse", "HEAD") == fixture.baseline.commit, "HEAD at baseline after the fetch crash")
    ref = "refs/solet/candidates/" + fixture.descriptor_digest[7:]
    _check(git(fixture.target, "rev-parse", f"{ref}^{{commit}}") == fixture.candidate.commit, "the private ref exists after the crash")
    with observe_git() as observer:
        resumed = apply_update(fixture.request, fingerprint)
    _check(resumed.status == "source_advanced" and "fetch" not in observer.verbs(), "resume refetches nothing; the ref is identity-equal")
    abandoned = build_fixture(root / "abandon")
    preview = preview_update_instance(abandoned.request)
    with patch.object(execution_module, "_run_git", crash_after_fetch):
        expect(SimulatedCrash, lambda: apply_update(abandoned.request, cast(str, preview.data["approval_fingerprint"])), "crash did not fire")
    result = abandon_update(abandoned.request)
    _check(result.status == "abandoned" and result.data["pointer_released"] is True and abandoned.record().active_operation is None, "abandon after fetch releases the pointer")
    abandoned_ref = "refs/solet/candidates/" + abandoned.descriptor_digest[7:]
    _check(git(abandoned.target, "rev-parse", f"{abandoned_ref}^{{commit}}") == abandoned.candidate.commit and result.data["private_candidate_ref_retained"] == abandoned_ref, "the private candidate ref survives the abandon (D9)")
    _check(last_update_journal(abandoned)["status"] == "abandoned", "journal reads abandoned")
    again = preview_update_instance(abandoned.request)
    _check(again.status == "preview_ready", "a fresh preview is possible after the abandon")


def _assert_fast_forward_boundary(root: Path) -> None:
    """F-B-2: crash after ``merge --ff-only``; resume runs from the candidate; abandon is refused."""
    fixture = build_fixture(root)
    preview = preview_update_instance(fixture.request)
    fingerprint = cast(str, preview.data["approval_fingerprint"])
    real = execution_module._run_git  # noqa: SLF001

    def crash_after_merge(cwd: Path, args: tuple[str, ...], *, hooks_dir: Path | None = None) -> Any:
        completed = real(cwd, args, hooks_dir=hooks_dir)
        if args[0] == "merge":
            raise SimulatedCrash("merge")
        return completed

    from unittest.mock import patch  # noqa: PLC0415

    with patch.object(execution_module, "_run_git", crash_after_merge):
        expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash did not fire")
    _check(_status(fixture) == "source_applying" and git(fixture.target, "rev-parse", "HEAD") == fixture.candidate.commit, "HEAD at candidate with the journal at source_applying")
    exc = expect(AbandonRefusedError, lambda: abandon_update(fixture.request), "abandon past the fast-forward accepted")
    _check("past the fast-forward" in str(exc), "abandon refused with the exact reason")
    with observe_git() as observer:
        resumed = apply_update(fixture.request, fingerprint)
    _check(resumed.status == "source_advanced" and not observer.verbs() & {"fetch", "merge"}, "resume runs from the candidate without fetch or merge")


def _assert_mid_merge_crash(root: Path) -> None:
    """F-B-2b (M4): the fixture half-checks-out the candidate tree without moving HEAD."""
    fixture = build_fixture(root)
    preview = preview_update_instance(fixture.request)
    fingerprint = cast(str, preview.data["approval_fingerprint"])
    real = execution_module._run_git  # noqa: SLF001

    def crash_before_merge(cwd: Path, args: tuple[str, ...], *, hooks_dir: Path | None = None) -> Any:
        if args[0] == "merge":
            raise SimulatedCrash("before merge")
        return real(cwd, args, hooks_dir=hooks_dir)

    from unittest.mock import patch  # noqa: PLC0415

    with patch.object(execution_module, "_run_git", crash_before_merge):
        expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash did not fire")
    _check(_status(fixture) == "source_applying", "journal at source_applying before the merge")
    git(fixture.target, "read-tree", "-m", "-u", fixture.candidate.commit)
    _check(git(fixture.target, "rev-parse", "HEAD") == fixture.baseline.commit and git(fixture.target, "status", "--porcelain") != "", "half-checkout: dirty tree, HEAD unmoved")
    before = fixture.paths.maintenance_inventory_path.read_bytes()
    with observe_git() as observer:
        exc = expect(UpdateBlockedError, lambda: apply_update(fixture.request, fingerprint), "interrupted merge not detected")
    _check(cast(UpdateBlockedError, exc).error_kind == "merge_interrupted" and "reconcile fixture --abandon --yes" in str(cast(UpdateBlockedError, exc).repair), "merge_interrupted with the exact repair")
    _check(_status(fixture) == "source_applying" and fixture.paths.maintenance_inventory_path.read_bytes() == before, "nothing mutated: journal and inventory unchanged")
    observer.assert_forward_only()
    _check(not observer.verbs() & {"reset", "checkout", "restore"}, "no discard verb ran")
    result = abandon_update(fixture.request)
    _check(result.status == "abandoned" and result.data["tracked_tree_dirty"] is True and "local state" in result.message, "abandon in that state releases the pointer and discloses the dirty tree")


# --- F-B-5 / F-SU-1 and F-M-1 / F-M-2 --------------------------------------------------------------


def _assert_manual_migration_successor(root: Path) -> None:
    """F-B-5 + F-SU-1: a manual migration applied once with an unverified postcondition blocks; the successor completes."""
    fixture = build_fixture(root, document=manual_migration_document)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    from unittest.mock import patch  # noqa: PLC0415

    from solet_manager import update_runtime_execution as executor  # noqa: PLC0415

    original = executor.RuntimeExecution._verified_by_postcondition  # noqa: SLF001
    state = {"probes": 0}

    def unverified_once(self: Any, operation: Any, inputs: Any, contradiction: str, incomplete: str) -> bool:
        if operation.operation_id == "migration_export_root_containment":
            state["probes"] += 1
            if state["probes"] <= 2:
                self._probe(operation, "post_apply", inputs)  # noqa: SLF001
                if state["probes"] == 2:
                    raise UpdateBlockedError(incomplete, f"{operation.operation_id} was applied once and its postcondition still fails; manual retry policy", repair="reconcile")
                return False
        return original(self, operation, inputs, contradiction, incomplete)

    def applied(self: Any, operation: Any, inputs: Any, contradiction: str) -> None:
        self._record(operation.operation_id, "apply", None, status="applying", note={"request_id": "fixture"})  # noqa: SLF001
        raise SimulatedCrash("after manual apply")

    with patch.object(executor.RuntimeExecution, "_verified_by_postcondition", unverified_once), patch.object(executor.RuntimeExecution, "_apply_and_verify", applied):
        expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash did not fire")
    _check(_status(fixture) == "migrations_applying", "journal at migrations_applying with the manual row applying")
    with patch.object(executor.RuntimeExecution, "_verified_by_postcondition", unverified_once):
        exc = expect(UpdateBlockedError, lambda: apply_update(fixture.request, fingerprint), "manual row reapplied")
    _check(cast(UpdateBlockedError, exc).error_kind == "migration_incomplete" and _status(fixture) == "blocked", "terminal blocked migration_incomplete; the Manager never reapplies a manual row")
    _check(fixture.record().management_state.value == "needs_attention", "a terminal past the source boundary publishes needs_attention (D8)")
    plan = preview_reconcile(fixture.request)
    _check(plan.status == "successor_preview_ready" and plan.exit_code == 0, f"successor preview ready: {plan.status} {plan.error_kind}")
    rows = {cast(str, data(row, "operation_id")): row for row in cast(list[Any], plan.data["operation_rows"])}
    _check(data(rows["migration_export_root_containment"], "operation_type") == "manual_target_migration", "the manual row is typed T3")
    _check(data(rows["migration_export_root_containment"], "classification") in {"operator_confirmation", "verified_by_probe"}, "the successor classifies the manual row")
    old_id = cast(str, plan.data["operation_id"])
    successor_id = cast(str, data(plan.data["successor"], "operation_id"))
    _check(successor_id != old_id and data(plan.data["successor"], "recovers") == old_id and data(plan.data["successor"], "source_mode") == "verify", "distinct successor id in verify mode naming the old operation")
    minted = reconcile_update(fixture.request, cast(str, plan.data["approval_fingerprint"]))
    _check(minted.status == "source_advanced" and minted.data["recovers"] == old_id, "successor minted and at source_advanced")
    record = fixture.record()
    _check(record.active_operation is not None and record.active_operation.operation_id == successor_id, "pointer swapped once to the successor")
    _check(fixture.paths.operation_path(record.instance_id, old_id).exists(), "the old journal is retained")
    successor_fp = runtime_fingerprint(fixture)
    result = apply_update(fixture.request, successor_fp)
    _check(result.status == "promoted", f"the successor completes: {result.status} {result.error_kind} {result.message}")
    journal = fixture.journal()
    _check(journal["recovers"] == old_id and journal["source_mode"] == "verify" and journal["status"] == "promoted", "successor journal carries recovers and verify mode")
    exc2 = expect(ManagerError, lambda: preview_reconcile(fixture.request), "reconcile on a promoted row accepted")
    _check(cast(ManagerError, exc2).error_kind == "no_active_update", "a second reconcile finds no active update after promotion")


def _assert_forward_only_block(root: Path) -> None:
    """F-M-2: a forward-only migration applied once with an unverified postcondition blocks; text says code-only."""
    host = FakeHost()
    fixture = build_fixture(root, document=forward_only_document, host=host)
    fixture.request = replace(fixture.request, operator_selections={"process_key": "service_interface::fixture_state_service::forward_only_migration", "backup_checkpoint_id": "fx_1"})
    advance_to_source_advanced(fixture)
    preview = preview_update_instance(fixture.request)
    _check(preview.status == "runtime_preview_ready" and data(preview.data["rollback_limit"], "forward_only_boundary") == "platform_forward_only", "forward-only boundary disclosed in the preview")
    fingerprint = cast(str, preview.data["runtime_approval_fingerprint"])
    real = host.invoke_bridge

    def bridge(registry: Any, key: str, arguments: Any, kind: str, timeout: int) -> Any:
        payload = real(registry, key, arguments, kind, timeout)
        if kind == "migration.read":
            return {"applied": False}
        if kind == "migration":
            raise SimulatedCrash("after the forward-only bridge call")
        return payload

    fixture.request = replace(fixture.request, runtime_seams=replace(cast(Any, fixture.request.runtime_seams), invoke_bridge=bridge))
    expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash did not fire")
    _check(_status(fixture) == "runtime_reconciling", "journal at runtime_reconciling with the forward-only row applying")
    exc = expect(UpdateBlockedError, lambda: apply_update(fixture.request, fingerprint), "forward-only row reapplied")
    blocked = cast(UpdateBlockedError, exc)
    _check(blocked.error_kind == "forward_only_migration_incomplete" and _status(fixture) == "blocked", "terminal blocked forward_only_migration_incomplete")
    rendered = f"{blocked} {blocked.repair}"
    _check("code-only" in rendered and "database rollback" not in rendered and "--backup-checkpoint" in rendered, "the text names the router previous as code-only and demands a new checkpoint")
    calls = [key for key, arguments in host.bridge_calls if key.endswith("forward_only_migration") and arguments.get("dry_run") is False]
    _check(len(calls) == 1, "exactly one forward-only apply ever ran")
    plan = preview_reconcile(fixture.request)
    _check(plan.status == "successor_preview_ready" and data(plan.data["forward_only"], "requires_new_backup_checkpoint") is True, "the successor preview demands a new checkpoint")


def _assert_additive_migration_retry(root: Path) -> None:
    """F-M-1 / T6: postcondition first; reapply once after a failed postcondition; twice unverified is a contradiction."""
    host = FakeHost()
    fixture = build_fixture(root, document=additive_migration_document, host=host)
    fixture.request = replace(fixture.request, operator_selections={"process_key": "service_interface::fixture_state_service::apply_release_migration"})
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    state = {"applied": 0}
    real = host.invoke_bridge

    def bridge(registry: Any, key: str, arguments: Any, kind: str, timeout: int) -> Any:
        payload = real(registry, key, arguments, kind, timeout)
        if kind == "migration.read":
            return {"applied": state["applied"] >= 2}
        if kind == "migration":
            state["applied"] += 1
        return payload

    fixture.request = replace(fixture.request, runtime_seams=replace(cast(Any, fixture.request.runtime_seams), invoke_bridge=bridge))
    result = apply_update(fixture.request, fingerprint)
    _check(result.status == "promoted" and state["applied"] == 2, f"a retry-safe additive migration reapplies exactly once after an unverified postcondition: {result.status} {state}")


# --- F-HO-1..4 ------------------------------------------------------------------------------------------


def _assert_handoff_copies(root: Path) -> None:
    """F-HO-1: purge the cache and poison the installed-descriptor seam after source_advanced; resume reads the copies."""
    fixture = build_fixture(root)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    shutil.rmtree(fixture.paths.cache_dir)
    fixture.paths.cache_dir.mkdir(mode=0o700)

    def poisoned(channel: str, tracker: Any) -> Any:
        # The real loader's failure mode when the formula's seed lock is gone: informational
        # sections (update availability, post-promotion eligibility) report it; resume never needs it.
        raise ValueError("installed seed lock is unavailable")

    fixture.request = replace(fixture.request, descriptor_loader=poisoned)
    result = apply_update(fixture.request, fingerprint)
    _check(result.status == "promoted", f"resume from the durable copies reaches promoted: {result.status} {result.error_kind} {result.message}")
    _check(fixture.record().update_eligibility.reason_codes == ("descriptor_unavailable",), "eligibility after promotion is honest about the unreadable installed descriptor")


def _assert_manager_upgrade_mid_operation(root: Path) -> None:
    """F-HO-2: the installed descriptor moves to a third release after runtime_planned; resume never reads it."""
    fixture = build_fixture(root)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    from unittest.mock import patch  # noqa: PLC0415

    from solet_manager import update_runtime_execution as executor  # noqa: PLC0415

    def crash(self: Any) -> None:
        raise SimulatedCrash("after runtime_planned")

    with patch.object(executor.RuntimeExecution, "_dependencies", crash):
        expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash did not fire")
    _check(_status(fixture) == "dependencies_applying", "journal past runtime_planned")
    reads: list[str] = []
    real_loader = fixture.request.descriptor_loader

    def moved(channel: str, tracker: Any) -> Any:
        reads.append(_status(fixture))
        return real_loader(channel, tracker)

    fixture.request = replace(fixture.request, descriptor_loader=moved)
    result = apply_update(fixture.request, fingerprint)
    _check(result.status == "promoted", f"resume completes from the self-sufficient copy: {result.status} {result.error_kind}")
    _check(reads and all(status in {"doctor_verifying", "promoting"} for status in reads), f"the installed loader is consulted only by the informational doctor section and post-promotion eligibility, never on the resume path: {reads}")


def _assert_target_edits_still_fail(root: Path) -> None:
    """F-HO-3: a dirty or committed edit of the target's transition bundle after source_advanced is refused."""
    dirty = build_fixture(root / "dirty")
    advance_to_source_advanced(dirty)
    fingerprint = runtime_fingerprint(dirty)
    flow = dirty.target / "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json"
    flow.write_text(flow.read_text() + "\n")
    exc = expect(SourceTransitionIncompleteError, lambda: apply_update(dirty.request, fingerprint), "dirty bundle accepted")
    _check(cast(ManagerError, exc).error_kind == "source_transition_incomplete" and _status(dirty) == "source_advanced" and not (dirty.home / ".zshrc").exists(), "dirty edit: source_transition_incomplete, nothing applied")
    committed = build_fixture(root / "committed")
    advance_to_source_advanced(committed)
    fingerprint = runtime_fingerprint(committed)
    flow = committed.target / "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json"
    flow.write_text(flow.read_text() + "\n")
    git(committed.target, "-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit", "--quiet", "-am", "edit")
    exc = expect(ManagerError, lambda: apply_update(committed.request, fingerprint), "committed bundle edit accepted")
    _check(cast(ManagerError, exc).error_kind in {"source_transition_incomplete", "managed_identity_drift"} and not (committed.home / ".zshrc").exists(), "committed edit: HEAD moved, refused before any apply")


def _assert_copy_tamper(root: Path) -> None:
    """F-HO-4: a byte flipped in the durable copy is transition_contract_mismatch at re-entry (exit 3)."""
    fixture = build_fixture(root)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    copy = transition_contract_dir(fixture.paths, fixture.contract) / "existing_install_flow.json"
    raw = bytearray(copy.read_bytes())
    raw[-2] = ord("X") if raw[-2] != ord("X") else ord("Y")
    copy.write_bytes(bytes(raw))
    exc = expect(TransitionContractMismatchError, lambda: apply_update(fixture.request, fingerprint), "tampered copy accepted")
    _check(cast(ManagerError, exc).exit_code == 3 and _status(fixture) == "source_advanced", "transition_contract_mismatch exit 3; the frontier is never terminalised")
    expect(TransitionContractMismatchError, lambda: read_transition_contract(fixture.paths, fixture.contract), "reader accepted the tampered copy")


# --- F-RB-1 ------------------------------------------------------------------------------------------------


def _assert_no_destructive_vectors(root: Path) -> None:
    fixture = build_fixture(root)
    with observe_git() as observer:
        run_to_promoted(fixture)
    observer.assert_forward_only()
    _check({"fetch", "merge"} <= observer.verbs(), "the two approved writes were observed")


def main() -> int:
    with db_spy(), TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        _assert_sweep_reference(root / "sweep")
        _assert_fetch_boundary(root / "fetch")
        _assert_fast_forward_boundary(root / "ff")
        _assert_mid_merge_crash(root / "midmerge")
        _assert_manual_migration_successor(root / "manual")
        _assert_forward_only_block(root / "forward")
        _assert_additive_migration_retry(root / "additive")
        _assert_handoff_copies(root / "ho1")
        _assert_manager_upgrade_mid_operation(root / "ho2")
        _assert_target_edits_still_fail(root / "ho3")
        _assert_copy_tamper(root / "ho4")
        _assert_no_destructive_vectors(root / "rb1")
    print(f"update_crash_sweep_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
