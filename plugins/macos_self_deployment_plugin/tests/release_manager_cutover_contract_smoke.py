#!/usr/bin/env python3
"""Standalone smoke: ReleaseManager.cutover() public failure contract (F1).

Phase-2 adversarial review (Codex + Reviewer-C) found that cutover's
ledger I/O brackets the inner symlink try, so a raw ``OSError`` /
``json.JSONDecodeError`` / ``KeyError`` could escape PAST the
orchestrator's ``except ReleaseManagerError`` and silently skip its
``_handle_cutover_failure`` compensation. The fix (coordinated C<->B,
belt-and-suspenders with B broadening its orchestrator catch): cutover
raises ONLY ``ReleaseManagerError`` (chaining the raw cause) and
compensates back to the pre-swap state, leaving a reconcile-coherent
ledger.

Cases (synthetic source under ``~/.ananta`` scratch, NEVER ``/tmp``):
- **ledger write fails BEFORE the symlink swap** (BEGIN step) -> cutover
  raises ReleaseManagerError (not raw OSError), chains __cause__,
  current unchanged, in_progress cleared, reconcile noop.
- **ledger write fails AFTER both symlinks** (DONE step) -> raises
  ReleaseManagerError, compensates current/previous back to prior (§4.7
  "unchanged"), in_progress cleared, reconcile noop, candidate intact.
- **compensate-intent-produced (organic)** -> fault DONE + the
  compensate-CLEAR write so the PHASE_COMPENSATE intent that
  ``_compensate_failed_swap`` actually WROTE survives on disk; assert its
  field mapping is the prior pair (``new_rel==prior_current``,
  ``old_rel==prior_previous``). Closes the advisor's circularity gap: the
  crash smoke only tests reconcile's *consumption* of a hand-crafted intent,
  so a write-side transposition would pass there yet reintroduce the Codex
  gap.
- **corrupt on-disk ledger** (torn state.json) -> the bracketing
  ledger.read raises json parse error, converted to ReleaseManagerError
  chaining a ValueError.
- **malformed ledger shape** (valid JSON, wrong top-level type: ``[]`` /
  scalar) -> ReleaseLedger.read rejects the non-dict as ValueError BEFORE
  the ``.get`` calls, so cutover raises ReleaseManagerError (not a raw
  AttributeError); closes the Codex parse-SHAPE contract leak.
- **reconcile write fault** -> a mid-cutover death leaves a surviving
  in_progress; reconcile's forward-complete write then fails -> reconcile
  raises ReleaseManagerError (not raw), chains __cause__ (same contract,
  since Phase 2 calls reconcile at startup).
- **Q4 filesystem error** -> the raw fs ops OUTSIDE the swap wrap (cutover's
  pre-try _is_finalized, gc's enumeration) under ``chmod 000`` also surface
  as ReleaseManagerError, not raw PermissionError.

Run:
    .venv/bin/python3 \\
      plugins/macos_self_deployment_plugin/tests/release_manager_cutover_contract_smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_manager_smoke_support as support  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    LEDGER_STEP_BEGIN,
    LEDGER_STEP_COMPENSATE_CLEAR,
    LEDGER_STEP_DONE,
    LEDGER_STEP_RECONCILE,
    PHASE_COMPENSATE,
    CandidatePaths,
    ReleaseManagerError,
)


class _DeathError(Exception):
    """Non-OSError raised mid-swap to leave a surviving in_progress row."""


def _ledger(releases_root: Path) -> dict[str, object]:
    return json.loads((releases_root / "state.json").read_text())


def _symlink_target(link: Path) -> str | None:
    if not link.is_symlink():
        return None
    return Path(os.readlink(link)).name


def _seed(source: Path, releases: Path) -> tuple[str, CandidatePaths]:
    """Build relA + cutover it (writes a valid ledger), build relB (uncut)."""
    mgr = support.make_manager(source, releases)
    cand_a = mgr.build_candidate()
    mgr.cutover(cand_a)
    cand_b = mgr.build_candidate()
    return cand_a.release_id, cand_b


def _raise_oserror_on(target_step: str) -> Callable[[str], None]:
    def hook(step: str) -> None:
        if step == target_step:
            raise OSError(f"simulated ledger write failure at step={step}")
    return hook


def _raise_oserror_on_any(target_steps: frozenset[str]) -> Callable[[str], None]:
    def hook(step: str) -> None:
        if step in target_steps:
            raise OSError(f"simulated ledger write failure at step={step}")
    return hook


def _capture(thunk: Callable[[], object]) -> Exception | None:
    try:
        thunk()
    except Exception as exc:  # noqa: BLE001 — we assert the concrete type below
        return exc
    return None


def _case_write_before_swap(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    rel_a, cand_b = _seed(source, releases)
    mgr = support.make_manager(
        source, releases, ledger_write_hook=_raise_oserror_on(LEDGER_STEP_BEGIN),
    )
    exc = _capture(lambda: mgr.cutover(cand_b))
    rec.check(
        isinstance(exc, ReleaseManagerError),
        f"[write@begin] cutover raises ReleaseManagerError, not raw ({type(exc).__name__})",
    )
    rec.check(
        isinstance(exc.__cause__, OSError) if exc else False,
        "[write@begin] chains the raw OSError as __cause__",
    )
    rec.check(
        _symlink_target(releases / "current") == rel_a,
        "[write@begin] current unchanged (relA) after failed cutover",
    )
    rec.check(
        _ledger(releases).get("in_progress") is None,
        "[write@begin] in_progress cleared (reconcile-coherent)",
    )
    fresh = support.make_manager(source, releases)
    rec.check(
        fresh.reconcile().action == "noop",
        "[write@begin] post-failure reconcile is a noop",
    )


def _case_write_after_swaps(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    rel_a, cand_b = _seed(source, releases)
    mgr = support.make_manager(
        source, releases, ledger_write_hook=_raise_oserror_on(LEDGER_STEP_DONE),
    )
    exc = _capture(lambda: mgr.cutover(cand_b))
    rec.check(
        isinstance(exc, ReleaseManagerError),
        f"[write@done] cutover raises ReleaseManagerError, not raw ({type(exc).__name__})",
    )
    rec.check(
        isinstance(exc.__cause__, OSError) if exc else False,
        "[write@done] chains the raw OSError as __cause__",
    )
    rec.check(
        _symlink_target(releases / "current") == rel_a,
        "[write@done] current restored to relA (§4.7: current/previous unchanged)",
    )
    rec.check(
        _ledger(releases).get("in_progress") is None,
        "[write@done] in_progress cleared after compensation (reconcile-coherent)",
    )
    fresh = support.make_manager(source, releases)
    rec.check(
        fresh.reconcile().action == "noop",
        "[write@done] post-failure reconcile is a noop",
    )
    rec.check(
        (releases / cand_b.release_id).is_dir(),
        "[write@done] candidate relB build artifact left intact",
    )


def _case_compensate_intent_produced(rec: support.SmokeRecorder, scratch: Path) -> None:
    """ORGANIC production check of ``_compensate_failed_swap`` (advisor catch).

    The crash-consistency smoke's ``_compensate_intent_case`` hand-crafts the
    PHASE_COMPENSATE row on disk and verifies reconcile *consumes* it — but a
    transposition in ``_compensate_failed_swap`` (writing ``new_rel`` /
    ``old_rel`` swapped) would still pass that test AND every other test, then
    make reconcile drive to current/previous SWAPPED — reintroducing the exact
    Codex gap. So drive a REAL failed cutover, let the compensate-INTENT write
    land but fault the compensate-CLEAR write (leaving the intent on disk), and
    assert the surviving field mapping is the PRIOR pair.

    Setup: current->relA (prior_current=relA, prior_previous=None), build relB.
    Fault DONE (trips compensation) + COMPENSATE_CLEAR (strands the intent).
    """
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    rel_a, cand_b = _seed(source, releases)
    mgr = support.make_manager(
        source,
        releases,
        ledger_write_hook=_raise_oserror_on_any(
            frozenset({LEDGER_STEP_DONE, LEDGER_STEP_COMPENSATE_CLEAR})
        ),
    )
    exc = _capture(lambda: mgr.cutover(cand_b))
    rec.check(
        isinstance(exc, ReleaseManagerError),
        f"[comp-produced] cutover still raises ReleaseManagerError ({type(exc).__name__})",
    )
    surviving = _ledger(releases).get("in_progress")
    rec.check(
        isinstance(surviving, dict),
        "[comp-produced] compensate intent survives on disk (clear write faulted)",
    )
    intent = surviving if isinstance(surviving, dict) else {}
    rec.check(
        intent.get("phase") == PHASE_COMPENSATE,
        "[comp-produced] surviving intent phase == compensate",
    )
    rec.check(
        intent.get("new_rel") == rel_a,
        "[comp-produced] new_rel == prior_current (relA) — terminal current target",
    )
    rec.check(
        intent.get("old_rel") is None,
        "[comp-produced] old_rel == prior_previous (None) — NOT transposed",
    )

    # The mapping is direction-correct -> a fresh reconcile drives BACK to the
    # prior pair (current=relA, previous absent), proving the intent that
    # _compensate_failed_swap PRODUCED is the one reconcile needs.
    fresh = support.make_manager(source, releases)
    comp_result = fresh.reconcile()
    rec.check(
        comp_result.action == "compensated",
        "[comp-produced] reconcile of the produced intent reports COMPENSATED (drove back)",
    )
    rec.check(
        _symlink_target(releases / "current") == rel_a,
        "[comp-produced] reconcile drove current BACK to relA from the produced intent",
    )
    rec.check(
        _ledger(releases).get("in_progress") is None,
        "[comp-produced] in_progress cleared after reconcile",
    )


def _case_compensate_intent_two_real(rec: support.SmokeRecorder, scratch: Path) -> None:
    """A.5 (reviewer-optional, strictly-stronger transposition discriminator).

    Same organic produced-intent check as ``_case_compensate_intent_produced``
    but with TWO real prior releases so BOTH ``prior_current`` AND
    ``prior_previous`` are non-None and DISTINCT. The None-previous case can
    only assert ``old_rel is None``; here ``old_rel`` carries a real id, so a
    ``new_rel`` <-> ``old_rel`` transposition is caught on BOTH fields, and
    reconcile must drive BOTH symlinks back to a distinct pair.
    """
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    # One seed manager for all three builds: its counting-sha increments per
    # build (a fresh manager per build would restart at sha000 and collide).
    seed_mgr = support.make_manager(source, releases)
    cand_a = seed_mgr.build_candidate()
    seed_mgr.cutover(cand_a)  # current->relA, previous absent
    cand_b = seed_mgr.build_candidate()
    seed_mgr.cutover(cand_b)  # current->relB, previous->relA  (the prior pair)
    cand_c = seed_mgr.build_candidate()  # uncut candidate
    rel_a, rel_b = cand_a.release_id, cand_b.release_id

    mgr = support.make_manager(
        source,
        releases,
        ledger_write_hook=_raise_oserror_on_any(
            frozenset({LEDGER_STEP_DONE, LEDGER_STEP_COMPENSATE_CLEAR})
        ),
    )
    exc = _capture(lambda: mgr.cutover(cand_c))
    rec.check(
        isinstance(exc, ReleaseManagerError),
        f"[comp-two-real] cutover raises ReleaseManagerError ({type(exc).__name__})",
    )
    surviving = _ledger(releases).get("in_progress")
    intent = surviving if isinstance(surviving, dict) else {}
    rec.check(
        intent.get("phase") == PHASE_COMPENSATE,
        "[comp-two-real] surviving intent phase == compensate",
    )
    rec.check(
        intent.get("new_rel") == rel_b,
        "[comp-two-real] new_rel == prior_current (relB) — not transposed",
    )
    rec.check(
        intent.get("old_rel") == rel_a,
        "[comp-two-real] old_rel == prior_previous (relA) — distinct, not transposed",
    )

    fresh = support.make_manager(source, releases)
    comp_result = fresh.reconcile()
    rec.check(
        comp_result.action == "compensated",
        "[comp-two-real] reconcile reports COMPENSATED",
    )
    rec.check(
        _symlink_target(releases / "current") == rel_b,
        "[comp-two-real] reconcile drove current BACK to relB",
    )
    rec.check(
        _symlink_target(releases / "previous") == rel_a,
        "[comp-two-real] reconcile drove previous BACK to relA (distinct pair)",
    )


def _case_compensation_restore_oserror_preserves_intent(
    rec: support.SmokeRecorder, scratch: Path
) -> None:
    """A restore fault must leave the compensate intent for reconcile.

    The failed DONE write enters compensation. Injecting an ``OSError`` into
    current-link restoration leaves a partially restored pair; clearing the
    ledger marker here would make ``reconcile`` incorrectly no-op.
    """
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    rel_a, cand_b = _seed(source, releases)
    mgr = support.make_manager(
        source, releases, ledger_write_hook=_raise_oserror_on(LEDGER_STEP_DONE),
    )
    original_restore = mgr._symlinks.restore  # noqa: SLF001 - fixture seam

    def fail_current_restore(link: Path, target: str | None) -> None:
        if link == mgr._symlinks.current:  # noqa: SLF001 - fixture seam
            raise OSError("simulated compensation current-link restore failure")
        original_restore(link, target)

    mgr._symlinks.restore = fail_current_restore  # type: ignore[method-assign]  # noqa: SLF001
    try:
        exc = _capture(lambda: mgr.cutover(cand_b))
    finally:
        mgr._symlinks.restore = original_restore  # type: ignore[method-assign]  # noqa: SLF001
    rec.check(
        isinstance(exc, ReleaseManagerError),
        f"[restore-fault] cutover raises ReleaseManagerError ({type(exc).__name__})",
    )
    surviving = _ledger(releases).get("in_progress")
    rec.check(
        isinstance(surviving, dict) and surviving.get("phase") == PHASE_COMPENSATE,
        "[restore-fault] incomplete compensation preserves PHASE_COMPENSATE intent",
    )
    result = support.make_manager(source, releases).reconcile()
    rec.check(
        result.action == "compensated",
        "[restore-fault] reconcile consumes the surviving compensate intent",
    )
    rec.check(
        _symlink_target(releases / "current") == rel_a,
        "[restore-fault] reconcile restores current to the prior release",
    )


def _case_corrupt_ledger_read(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    _, cand_b = _seed(source, releases)
    (releases / "state.json").write_text("{ this is not valid json")
    mgr = support.make_manager(source, releases)
    exc = _capture(lambda: mgr.cutover(cand_b))
    rec.check(
        isinstance(exc, ReleaseManagerError),
        f"[corrupt-read] cutover raises ReleaseManagerError, not raw ({type(exc).__name__})",
    )
    rec.check(
        isinstance(exc.__cause__, ValueError) if exc else False,
        "[corrupt-read] chains the json parse error (ValueError) as __cause__",
    )


def _assert_malformed_shape_rejected(
    rec: support.SmokeRecorder, scratch: Path, shape: str
) -> None:
    """One malformed-shape probe: seed, overwrite state.json with ``shape``,
    assert cutover raises REM chaining a ValueError (not a raw AttributeError).
    """
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    _, cand_b = _seed(source, releases)
    (releases / "state.json").write_text(shape)
    mgr = support.make_manager(source, releases)
    exc = _capture(lambda: mgr.cutover(cand_b))
    rec.check(
        isinstance(exc, ReleaseManagerError),
        f"[malformed-shape {shape!r}] cutover raises REM, not raw ({type(exc).__name__})",
    )
    rec.check(
        isinstance(exc.__cause__, ValueError) if exc else False,
        f"[malformed-shape {shape!r}] chains a ValueError (non-dict top level) as __cause__",
    )


def _case_malformed_ledger_shape(rec: support.SmokeRecorder, scratch: Path) -> None:
    """Codex harness: a STRUCTURALLY-wrong state.json (valid JSON, wrong
    top-level type — an array ``[]`` or a bare scalar) parses fine, then the
    ledger's ``raw.get("in_progress")`` would raise a raw ``AttributeError``
    that escapes ``_read_ledger_or_raise``'s (OSError, ValueError, KeyError)
    net — so cutover would raise AttributeError, NOT ReleaseManagerError,
    falsifying the module's "raises ONLY ReleaseManagerError" claim. The fix
    rejects a non-dict top level as ValueError inside ReleaseLedger.read.
    This completes the Architect Q4 intent (the fs-op wrap missed the
    parse-SHAPE vector that torn-JSON's parse-FAILURE vector does cover).
    """
    for index, shape in enumerate(("[]", "42", '"a bare string"')):
        _assert_malformed_shape_rejected(rec, scratch / f"shape_{index}", shape)


def _case_reconcile_write_fault(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    _, cand_b = _seed(source, releases)

    # Simulate process death mid-cutover so a forward-completable
    # in_progress row survives on disk (the startup reconcile entry state).
    def die() -> None:
        raise _DeathError

    crasher = support.make_manager(source, releases, mid_swap_hook=die)
    death = _capture(lambda: crasher.cutover(cand_b))
    rec.check(
        isinstance(death, _DeathError),
        "[reconcile-write] setup: mid-cutover death left a surviving in_progress",
    )
    rec.check(
        _ledger(releases).get("in_progress") is not None,
        "[reconcile-write] setup: in_progress row present pre-reconcile",
    )

    # reconcile's forward-complete ledger write now fails -> B calls
    # reconcile() at startup, so it must raise REM (not raw), same contract.
    mgr = support.make_manager(
        source, releases, ledger_write_hook=_raise_oserror_on(LEDGER_STEP_RECONCILE),
    )
    exc = _capture(mgr.reconcile)
    rec.check(
        isinstance(exc, ReleaseManagerError),
        f"[reconcile-write] reconcile raises ReleaseManagerError, not raw ({type(exc).__name__})",
    )
    rec.check(
        isinstance(exc.__cause__, OSError) if exc else False,
        "[reconcile-write] chains the raw OSError as __cause__",
    )


def _case_q4_filesystem_error(rec: support.SmokeRecorder, scratch: Path) -> None:
    """Q4 (Architect): the raw filesystem ops OUTSIDE the swap wrap — the
    pre-try _is_finalized in cutover, and gc's enumeration/symlink reads —
    must also surface as ReleaseManagerError. ``chmod 000`` on releases_root
    makes is_dir/is_file/readlink/iterdir raise PermissionError.
    """
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    _, cand_b = _seed(source, releases)
    os.chmod(releases, 0o000)
    try:
        mgr = support.make_manager(source, releases)
        exc_cutover = _capture(lambda: mgr.cutover(cand_b))
        rec.check(
            isinstance(exc_cutover, ReleaseManagerError),
            f"[q4] cutover under fs-denied releases raises REM ({type(exc_cutover).__name__})",
        )
        exc_gc = _capture(mgr.gc)
        rec.check(
            isinstance(exc_gc, ReleaseManagerError),
            f"[q4] gc under fs-denied releases raises REM ({type(exc_gc).__name__})",
        )
    finally:
        os.chmod(releases, 0o755)  # restore so the outer rmtree can recurse


def main() -> int:
    rec = support.SmokeRecorder()
    scratch = support.scratch_root("cutovercontract")
    print("=== release_manager_cutover_contract_smoke (F1 failure contract) ===")
    print(f"scratch: {scratch}")
    try:
        _case_write_before_swap(rec, scratch / "before")
        _case_write_after_swaps(rec, scratch / "after")
        _case_compensate_intent_produced(rec, scratch / "comp_produced")
        _case_compensate_intent_two_real(rec, scratch / "comp_two_real")
        _case_compensation_restore_oserror_preserves_intent(
            rec, scratch / "restore_fault"
        )
        _case_corrupt_ledger_read(rec, scratch / "corrupt")
        _case_malformed_ledger_shape(rec, scratch / "malformed")
        _case_reconcile_write_fault(rec, scratch / "reconcile")
        _case_q4_filesystem_error(rec, scratch / "q4")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return rec.report("cutover_contract")


if __name__ == "__main__":
    sys.exit(main())
