#!/usr/bin/env python3
"""Standalone smoke: ReleaseManager build_candidate -> cutover -> rollback.

Drives the exact call sequence Phase-2 integration will use, against a
synthetic source tree under ``~/.ananta`` scratch (NEVER ``/tmp``). This
de-risks the §4.8 interface BEFORE anyone integrates it into
``swap_orchestrator.py`` / ``plugin.py``.

Coverage:
- ``build_candidate()`` materializes a finalized release: ``code/`` +
  ``venv/`` + ``VERSION`` present; ``CandidatePaths`` fields resolve.
- ``cutover(candidate)`` flips ``current`` (first deploy has no
  ``previous``); the ledger records ``current`` with ``in_progress``
  cleared.
- A second ``build_candidate`` + ``cutover`` demotes the prior release
  to ``previous`` (distinct release ids).
- ``rollback()`` swaps ``current``/``previous`` — the artifact half of
  the §4.5 durable rollback (the retained earlier release becomes
  ``current``, the rolled-back-from release stays on disk as
  ``previous``, GC-safe).

Proxy honesty: this exercises the **orchestration + artifact** half
(symlinks, ledger, finalized trees). The import-resolution half (a real
interpreter booting the release's own ``code/`` via the re-pointed
``.pth``, CWD-independently) was proven empirically in Claude-B's spike
measurements record §3 (dev-checkout workbench — not part of the
shipped tree). Together
they cover the deploy->fault->rollback cycle.

Run:
    .venv/bin/python3 \\
      plugins/macos_self_deployment_plugin/tests/release_manager_call_sequence_smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_manager_smoke_support as support  # noqa: E402


def _ledger(releases_root: Path) -> dict[str, object]:
    return json.loads((releases_root / "state.json").read_text())


def _symlink_target(link: Path) -> str | None:
    if not link.is_symlink():
        return None
    return Path(os.readlink(link)).name


def run(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    mgr = support.make_manager(source, releases)

    # --- build_candidate ---------------------------------------------------
    cand_a = mgr.build_candidate(manifest_etag="etag-a")
    rec.check(cand_a.release_dir.is_dir(), "candidate A release dir materialized")
    rec.check(
        (cand_a.code_root / "ananta" / "src" / "ananta" / "__init__.py").is_file(),
        "candidate A code/ contains the re-pointed ananta package",
    )
    rec.check(cand_a.version_file.is_file(), "candidate A VERSION written")
    rec.check(cand_a.venv_python.is_file(), "candidate A venv/bin/python3 present")
    version = json.loads(cand_a.version_file.read_text())
    rec.check(
        version.get("release_id") == cand_a.release_id,
        "VERSION release_id matches CandidatePaths.release_id",
    )
    rec.check(
        version.get("manifest_etag") == "etag-a",
        "VERSION carries the supplied manifest_etag",
    )
    rec.check(
        cand_a.missing_pth_targets == (),
        f"healthy build has no missing .pth targets (got {cand_a.missing_pth_targets})",
    )

    # --- cutover (first deploy: no previous) -------------------------------
    swap_a = mgr.cutover(cand_a)
    rec.check(swap_a.current == cand_a.release_id, "cutover A returns current=relA")
    rec.check(swap_a.previous is None, "first cutover has no previous")
    rec.check(
        _symlink_target(releases / "current") == cand_a.release_id,
        "current symlink -> relA",
    )
    rec.check(mgr.current_release == cand_a.release_id, "current_release property == relA")
    ledger_a = _ledger(releases)
    rec.check(ledger_a.get("current") == cand_a.release_id, "ledger current == relA")
    rec.check(ledger_a.get("in_progress") is None, "ledger in_progress cleared after cutover")

    # --- second build + cutover (demote relA to previous) ------------------
    cand_b = mgr.build_candidate(manifest_etag="etag-b")
    rec.check(
        cand_b.release_id != cand_a.release_id,
        "second build has a distinct release id",
    )
    swap_b = mgr.cutover(cand_b)
    rec.check(swap_b.current == cand_b.release_id, "cutover B returns current=relB")
    rec.check(swap_b.previous == cand_a.release_id, "cutover B demotes relA to previous")
    rec.check(
        _symlink_target(releases / "current") == cand_b.release_id,
        "current symlink -> relB",
    )
    rec.check(
        _symlink_target(releases / "previous") == cand_a.release_id,
        "previous symlink -> relA",
    )

    # --- rollback (durable: swap current <-> previous) ---------------------
    swap_rb = mgr.rollback()
    rec.check(swap_rb.current == cand_a.release_id, "rollback returns current=relA")
    rec.check(swap_rb.previous == cand_b.release_id, "rollback returns previous=relB")
    rec.check(mgr.current_release == cand_a.release_id, "after rollback current_release == relA")
    rec.check(
        mgr.previous_release == cand_b.release_id,
        "after rollback previous_release == relB",
    )
    rec.check(
        (releases / cand_b.release_id).is_dir(),
        "rolled-back-from relB still persists on disk (durable, GC-safe)",
    )
    ledger_rb = _ledger(releases)
    rec.check(ledger_rb.get("current") == cand_a.release_id, "ledger current == relA after rollback")
    rec.check(ledger_rb.get("in_progress") is None, "ledger in_progress cleared after rollback")


def main() -> int:
    rec = support.SmokeRecorder()
    scratch = support.scratch_root("callseq")
    print("=== release_manager_call_sequence_smoke ===")
    print(f"scratch: {scratch}")
    try:
        run(rec, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return rec.report("call_sequence")


if __name__ == "__main__":
    sys.exit(main())
