#!/usr/bin/env python3
"""Standalone smoke: ReleaseManager virgin first-boot (no releases yet).

This is the REAL Phase-2 startup entry condition: Phase 2 calls
``reconcile()`` on every boot — including the very first boot of a
freshly-birthed solet, when ``~/.ananta/releases/<name>/`` does not
exist yet and no release has ever been built. Every other ReleaseManager
smoke seeds at least one release first, so this virgin path had zero
assertion coverage (Reviewer-B's N1).

A fresh manager against a nonexistent ``releases_root`` must be inert:
- ``reconcile()`` -> ``noop`` with ``current``/``previous`` == ``None``.
- ``gc()`` -> empty (nothing deleted, nothing retained).
- ``current_release`` / ``previous_release`` properties == ``None``.
- NO exception, and these read-only ops do NOT materialize the
  ``releases_root`` dir as a side effect.

Run:
    .venv/bin/python3 \\
      plugins/macos_self_deployment_plugin/tests/release_manager_first_boot_smoke.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_manager_smoke_support as support  # noqa: E402


def run(rec: support.SmokeRecorder, scratch: Path) -> None:
    # releases_root deliberately does NOT exist (and is never created by
    # build_fake_source — that only makes the source tree). source_root is
    # supplied for construction but never read on this read-only path.
    source = support.build_fake_source(scratch)
    releases = scratch / "releases_never_created"
    rec.check(not releases.exists(), "precondition: releases_root absent before first boot")

    mgr = support.make_manager(source, releases)

    rec.check(mgr.current_release is None, "current_release == None on virgin first boot")
    rec.check(mgr.previous_release is None, "previous_release == None on virgin first boot")

    result = mgr.reconcile()
    rec.check(result.action == "noop", "reconcile() == noop on virgin first boot")
    rec.check(result.current is None, "reconcile() current == None")
    rec.check(result.previous is None, "reconcile() previous == None")

    gc_result = mgr.gc()
    rec.check(gc_result.deleted == (), "gc() deletes nothing on virgin first boot")
    rec.check(gc_result.retained == (), "gc() retains nothing on virgin first boot")

    rec.check(
        not releases.exists(),
        "read-only reconcile()/gc()/props did NOT materialize releases_root",
    )


def main() -> int:
    rec = support.SmokeRecorder()
    scratch = support.scratch_root("firstboot")
    print("=== release_manager_first_boot_smoke ===")
    print(f"scratch: {scratch}")
    try:
        run(rec, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return rec.report("first_boot")


if __name__ == "__main__":
    sys.exit(main())
