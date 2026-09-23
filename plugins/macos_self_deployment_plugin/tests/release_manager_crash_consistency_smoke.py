#!/usr/bin/env python3
"""Standalone smoke: ReleaseManager crash-consistency + reconcile (§8.5).

Exercises design §4.6 (ledger + ordering + startup reconcile + GC-safety)
and §4.7 (a failed cutover leaves current/previous unchanged) against a
synthetic source tree under ``~/.ananta`` scratch (NEVER ``/tmp``).

The two os.replace symlink swaps are not atomic as a pair; a crash
between them can leave an incoherent rollback target. The ``mid_swap_hook``
test seam fires between the ``previous`` and ``current`` ``os.replace``
calls — exactly the §8.5 crash window — and discriminates two failure
modes that MUST be handled differently:

- **Process-death case** (hook raises a non-OSError): the half-swapped
  state survives with an ``in_progress`` ledger row. A fresh manager's
  ``reconcile()`` forward-completes to a coherent ``current``/``previous``
  pair, and ``gc(keep=1)`` deletes NEITHER symlinked release (GC-safety).
- **In-process-abort case** (hook raises OSError, e.g. disk full): the
  swap is reverted and ``in_progress`` cleared in-process, so
  ``current``/``previous`` are unchanged (§4.7) and a later ``reconcile()``
  is a no-op. This is the invariant that makes the forward-complete in
  the first case safe: a surviving ``in_progress`` means ONLY death.

Run:
    .venv/bin/python3 \\
      plugins/macos_self_deployment_plugin/tests/release_manager_crash_consistency_smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_manager_smoke_support as support  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    PHASE_COMPENSATE,
    CandidatePaths,
    ReleaseManagerError,
    ReleaseSymlinks,
)


class _SimulatedCrashError(Exception):
    """A non-OSError raised mid-swap to simulate process death."""


def _ledger(releases_root: Path) -> dict[str, object]:
    return json.loads((releases_root / "state.json").read_text())


def _symlink_target(link: Path) -> str | None:
    if not link.is_symlink():
        return None
    return Path(os.readlink(link)).name


def _seed_two_releases(source: Path, releases: Path) -> tuple[str, CandidatePaths]:
    """Build relA + cutover it, build relB (uncut). Return (relA_id, cand_b)."""
    mgr = support.make_manager(source, releases)
    cand_a = mgr.build_candidate()
    mgr.cutover(cand_a)
    cand_b = mgr.build_candidate()
    return cand_a.release_id, cand_b


def _crash_case(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    rel_a, cand_b = _seed_two_releases(source, releases)
    rel_b = cand_b.release_id

    def crash() -> None:
        raise _SimulatedCrashError

    crashing = support.make_manager(source, releases, mid_swap_hook=crash)
    crashed = False
    try:
        crashing.cutover(cand_b)
    except _SimulatedCrashError:
        crashed = True
    rec.check(crashed, "[crash] cutover interrupted by simulated process death")

    # Half-swapped, incoherent state survives: previous flipped, current not,
    # in_progress row present.
    rec.check(
        _symlink_target(releases / "current") == rel_a,
        "[crash] current NOT yet flipped (still relA)",
    )
    rec.check(
        _symlink_target(releases / "previous") == rel_a,
        "[crash] previous already flipped to relA (mid-swap)",
    )
    rec.check(
        _ledger(releases).get("in_progress") is not None,
        "[crash] in_progress row survives a true death",
    )

    # GC-safety of the in-progress candidate (§8.5: "no release named by
    # either symlink OR the in-progress ledger row is GC'd"). At the crash
    # point relB is named ONLY by in_progress.new_rel — no symlink points
    # at it. gc(keep=0) must still spare it. keep=0 is the discriminating
    # retention: keep>=1 would retain relB as the newest-in-window
    # regardless of the in_progress protection.
    precrash_gc = support.make_manager(source, releases, keep_releases=0)
    precrash_result = precrash_gc.gc()
    rec.check(
        rel_b not in precrash_result.deleted,
        "[crash] gc(keep=0) pre-reconcile spares relB (protected by in_progress.new_rel)",
    )
    rec.check(
        (releases / rel_b).is_dir(),
        "[crash] in-progress candidate relB dir survives gc(keep=0)",
    )

    # Fresh manager (no hook) reconciles on startup -> forward-complete.
    recovered = support.make_manager(source, releases)
    result = recovered.reconcile()
    rec.check(result.action == "forward_completed", "[crash] reconcile forward-completes")
    rec.check(
        _symlink_target(releases / "current") == rel_b,
        "[crash] reconcile yields current -> relB",
    )
    rec.check(
        _symlink_target(releases / "previous") == rel_a,
        "[crash] reconcile yields previous -> relA (coherent, distinct pair)",
    )
    rec.check(
        _ledger(releases).get("in_progress") is None,
        "[crash] in_progress cleared after reconcile",
    )

    # GC-safety: neither symlinked release is deleted even at keep=1.
    gc_mgr = support.make_manager(source, releases, keep_releases=1)
    gc_result = gc_mgr.gc()
    rec.check(
        rel_a not in gc_result.deleted and rel_b not in gc_result.deleted,
        "[crash] gc(keep=1) deletes neither current nor previous (GC-safety)",
    )
    rec.check(
        (releases / rel_a).is_dir() and (releases / rel_b).is_dir(),
        "[crash] both symlinked release dirs persist after gc",
    )


def _abort_case(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    rel_a, cand_b = _seed_two_releases(source, releases)

    def disk_full() -> None:
        raise OSError("simulated ENOSPC mid-swap")

    aborting = support.make_manager(source, releases, mid_swap_hook=disk_full)
    raised = False
    try:
        aborting.cutover(cand_b)
    except ReleaseManagerError:
        raised = True
    rec.check(raised, "[abort] in-process OSError surfaces as ReleaseManagerError")

    # §4.7 postcondition: current/previous unchanged, in_progress cleared.
    rec.check(
        _symlink_target(releases / "current") == rel_a,
        "[abort] current unchanged (still relA)",
    )
    rec.check(
        _symlink_target(releases / "previous") is None,
        "[abort] previous restored to prior absent state",
    )
    rec.check(
        _ledger(releases).get("in_progress") is None,
        "[abort] in_progress cleared in-process (NOT left for reconcile)",
    )

    # A later reconcile is a no-op precisely because in_progress was cleared.
    recovered = support.make_manager(source, releases)
    result = recovered.reconcile()
    rec.check(result.action == "noop", "[abort] reconcile is a no-op after in-process revert")
    rec.check(
        _symlink_target(releases / "current") == rel_a,
        "[abort] reconcile leaves current at relA",
    )


def _abandon_case(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    mgr = support.make_manager(source, releases)
    cand_a = mgr.build_candidate()
    mgr.cutover(cand_a)
    rel_a = cand_a.release_id

    # Simulate a ledger whose in_progress names a candidate that never
    # finalized (corruption / a build interrupted before finalize).
    # reconcile must NOT forward to it (that would point current at a
    # non-existent dir and brick cold-boot) — it abandons + keeps current.
    state = {
        "current": rel_a,
        "previous": None,
        "in_progress": {
            "old_rel": rel_a,
            "new_rel": "rel-bogus-never-built",
            "phase": "cutover",
        },
    }
    (releases / "state.json").write_text(json.dumps(state))

    recovered = support.make_manager(source, releases)
    result = recovered.reconcile()
    rec.check(
        result.action == "abandoned",
        "[abandon] reconcile abandons an unfinalized in_progress candidate",
    )
    rec.check(
        _symlink_target(releases / "current") == rel_a,
        "[abandon] current unchanged (still relA)",
    )
    rec.check(
        _ledger(releases).get("in_progress") is None,
        "[abandon] in_progress cleared after abandon",
    )


def _compensate_intent_case(rec: support.SmokeRecorder, scratch: Path) -> None:
    """Architect F1 ruling: death AFTER the compensate-intent write, BEFORE
    the symlink restore. The surviving in_progress=PHASE_COMPENSATE row
    (terminal = the PRIOR pair) must make reconcile drive BACK to prior —
    NOT strand at, nor forward-complete to, the candidate. This is the exact
    Codex gap a binary in_progress could not close.
    """
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    rel_a, cand_b = _seed_two_releases(source, releases)  # current->relA, build relB
    rel_b = cand_b.release_id

    # Craft the on-disk state at the death point: symlinks still at the
    # candidate position (the forward swap completed), ledger holds a
    # COMPENSATE intent whose terminal is the prior pair (relA, None).
    symlinks = ReleaseSymlinks(releases)
    symlinks.point(symlinks.current, rel_b)
    symlinks.point(symlinks.previous, rel_a)
    (releases / "state.json").write_text(
        json.dumps(
            {
                "current": rel_a,
                "previous": None,
                "in_progress": {"old_rel": None, "new_rel": rel_a, "phase": PHASE_COMPENSATE},
            }
        )
    )

    result = support.make_manager(source, releases).reconcile()
    rec.check(
        result.action == "compensated",
        "[compensate-intent] reconcile reports COMPENSATED (drove back, not forward)",
    )
    rec.check(
        _symlink_target(releases / "current") == rel_a,
        "[compensate-intent] reconcile drove current BACK to relA (not stranded/forward at relB)",
    )
    rec.check(
        _symlink_target(releases / "previous") is None,
        "[compensate-intent] previous restored to prior (absent)",
    )
    rec.check(
        _ledger(releases).get("in_progress") is None,
        "[compensate-intent] in_progress cleared",
    )
    rec.check(
        (releases / rel_b).is_dir(),
        "[compensate-intent] candidate relB still on disk (GC-safe)",
    )


def _first_deploy_compensate_case(rec: support.SmokeRecorder, scratch: Path) -> None:
    """First-deploy boundary: prior_current=None -> compensate-intent
    new_rel=None. reconcile must drive to the EMPTY state, not blow up on
    _is_finalized(None) / point(current, None) (Architect F1 ruling #2).
    """
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    candidate = support.make_manager(source, releases).build_candidate()  # never cut over

    # Half-applied first cutover that died mid-compensation with a
    # None-terminal compensate intent.
    symlinks = ReleaseSymlinks(releases)
    symlinks.point(symlinks.current, candidate.release_id)
    (releases / "state.json").write_text(
        json.dumps(
            {
                "current": None,
                "previous": None,
                "in_progress": {"old_rel": None, "new_rel": None, "phase": PHASE_COMPENSATE},
            }
        )
    )

    result = support.make_manager(source, releases).reconcile()
    rec.check(
        result.action == "compensated",
        "[first-deploy-comp] reconcile reports COMPENSATED on None-terminal intent (no blow-up)",
    )
    rec.check(
        _symlink_target(releases / "current") is None,
        "[first-deploy-comp] current unlinked -> empty state",
    )
    rec.check(
        _ledger(releases).get("in_progress") is None,
        "[first-deploy-comp] in_progress cleared",
    )


def main() -> int:
    rec = support.SmokeRecorder()
    scratch = support.scratch_root("crash")
    print("=== release_manager_crash_consistency_smoke ===")
    print(f"scratch: {scratch}")
    try:
        _crash_case(rec, scratch / "crash_case")
        _abort_case(rec, scratch / "abort_case")
        _abandon_case(rec, scratch / "abandon_case")
        _compensate_intent_case(rec, scratch / "compensate_intent")
        _first_deploy_compensate_case(rec, scratch / "first_deploy_comp")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return rec.report("crash_consistency")


if __name__ == "__main__":
    sys.exit(main())
