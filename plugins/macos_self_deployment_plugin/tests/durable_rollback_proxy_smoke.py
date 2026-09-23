#!/usr/bin/env python3
"""§8.1 durable-rollback PROXY smoke (no pytest).

Design ``2026-06-27_true_local_blue_green_materialized_artifacts_design.md``
§8.1: durable code rollback — build relA, build relB (distinct,
detectable code marker), cut over to relB, then roll back ``current`` →
``previous`` (relA) and assert the rolled-back release's code is what
``current`` now resolves to, **after** relB is no longer active. This is
the property the SQL lockdown needs and that neither local nor cloud has
today.

THROWAWAY-PROXY, NOT a live swap (HARD constraint from the Phase-2
brief). It deliberately does NOT swap the running solet the fleet is
messaging through. It drives the **real** ``ReleaseManager`` (real
``cp -c`` clone + real ``current``/``previous`` symlinks + real
``state.json`` ledger) against a **synthetic** source tree under
``~/.ananta`` scratch (NEVER ``/tmp``) — mirroring this author's Phase-1
spike harness and C's ``ReleaseManager`` standalone smokes — and proves
the integration wiring: build (candidate) → cutover → reconcile →
rollback.

The "cold boot" is a PROXY: it reads each release's immutable code marker
**through the ``current`` symlink** (``current/code/...``), proving which
release's code tree a cold boot would resolve its interpreter + ``.pth``
to. It is NOT a live interpreter boot — the synthetic venv carries a stub
``python3`` — and the orchestrator's candidate-threading → cutover path is
covered separately by ``swap_round_trip_smoke.py``. The real live-swap
proof is a separate, operator-coordinated step.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/durable_rollback_proxy_smoke.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))

from _release_manager_smoke_support import (  # noqa: E402
    SmokeRecorder,
    build_fake_source,
    make_manager,
    scratch_root,
)
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CODE_DIRNAME,
    CURRENT_LINK_NAME,
    PREVIOUS_LINK_NAME,
    RECONCILE_NOOP,
)

# The marker lands inside the cloned ``ananta/`` subtree, so it is readable
# through ``current/code/ananta/<MARKER>`` once a release is active.
_MARKER_RELPATH = ("ananta", "RELEASE_MARKER")


def _write_marker(source_root: Path, value: str) -> None:
    """Stamp a distinct, detectable code marker into the source tree.

    Each build CoW-clones the source as it stands, so overwriting the
    marker between builds gives relA and relB genuinely different
    immutable code (an APFS clone shares extents with the source at build
    time; a later source overwrite never mutates the prior clone).
    """
    (source_root.joinpath(*_MARKER_RELPATH)).write_text(value)


def _read_current_marker(releases_root: Path) -> str:
    """PROXY cold boot: read the active release's marker via ``current``.

    Resolves ``current`` (symlink) → ``rel-<id>/code/ananta/RELEASE_MARKER``
    — i.e. exactly which release's code tree the cold-start interpreter +
    re-pointed ``.pth`` would import from. Reads through the symlink so the
    pointer flip is what is being asserted.
    """
    marker = releases_root.joinpath(CURRENT_LINK_NAME, CODE_DIRNAME, *_MARKER_RELPATH)
    return marker.read_text()


def run_smoke() -> int:
    rec = SmokeRecorder()
    print("=== durable_rollback_proxy_smoke (§8.1: durable code rollback) ===")
    scratch = scratch_root("durable-rollback")
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        source = build_fake_source(scratch)
        releases_root = scratch / "releases"
        mgr = make_manager(source, releases_root)

        # ---- Build relA, cut over to it ----
        _write_marker(source, "relA")
        cand_a = mgr.build_candidate()
        swap_a = mgr.cutover(cand_a)
        rec.check(
            swap_a.current == cand_a.release_id and swap_a.previous is None,
            f"cutover A: current={swap_a.current} previous={swap_a.previous}",
        )
        rec.check(
            _read_current_marker(releases_root) == "relA",
            "cold-boot proxy after cutover A resolves relA's code",
        )

        # ---- Build relB (distinct marker), cut over to it ----
        _write_marker(source, "relB")
        cand_b = mgr.build_candidate()
        rec.check(
            cand_b.release_id != cand_a.release_id,
            f"relB is a distinct release id ({cand_a.release_id} vs {cand_b.release_id})",
        )
        swap_b = mgr.cutover(cand_b)
        rec.check(
            swap_b.current == cand_b.release_id and swap_b.previous == cand_a.release_id,
            f"cutover B: current={swap_b.current} previous={swap_b.previous}",
        )
        rec.check(
            _read_current_marker(releases_root) == "relB",
            "cold-boot proxy after cutover B resolves relB's code",
        )

        # ---- reconcile is a no-op post-cutover (no interrupted swap) ----
        reconcile_after_cutover = mgr.reconcile()
        rec.check(
            reconcile_after_cutover.action == RECONCILE_NOOP
            and reconcile_after_cutover.current == cand_b.release_id,
            f"reconcile after a clean cutover is a no-op: {reconcile_after_cutover.action}",
        )

        # ---- Simulate a fault on relB, then DURABLE rollback to relA ----
        # (The fault is modelled by "relB's process is gone"; the durable
        # path does not use the in-window router rollback.)
        swap_rb = mgr.rollback()
        rec.check(
            swap_rb.current == cand_a.release_id and swap_rb.previous == cand_b.release_id,
            f"durable rollback: current={swap_rb.current} previous={swap_rb.previous}",
        )
        rec.check(
            (releases_root / PREVIOUS_LINK_NAME).is_symlink(),
            "previous symlink present after rollback (roll-forward target retained)",
        )

        # ---- reconcile again (still no interrupted swap) ----
        reconcile_after_rollback = mgr.reconcile()
        rec.check(
            reconcile_after_rollback.action == RECONCILE_NOOP,
            f"reconcile after rollback is a no-op: {reconcile_after_rollback.action}",
        )

        # ---- THE PROOF: cold-boot proxy now resolves relA's code ----
        rec.check(
            _read_current_marker(releases_root) == "relA",
            "DURABLE ROLLBACK: cold-boot proxy resolves relA's code after relB is gone",
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return rec.report("durable_rollback_proxy_smoke")


if __name__ == "__main__":
    sys.exit(run_smoke())
