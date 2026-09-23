#!/usr/bin/env python3
"""Standalone smoke: ReleaseManager .pth re-point + target validation (§8.6).

Exercises the design §4.7 / §8.6 acceptance criterion against a
synthetic source tree (under ``~/.ananta`` scratch, NEVER ``/tmp``) that
includes a deliberately-stale ``__editable__`` ``.pth`` whose target
directory is absent — mirroring the real
``__editable__.local_blue_green_deployment_plugin`` stale line that
Claude-B's spike confirmed is the one genuinely-missing target among the
66 (``2026-06-27_blue_green_spike_measurements.md`` §7).

Coverage:
- Non-strict build (default): succeeds, and the stale target is
  surfaced in ``CandidatePaths.missing_pth_targets`` (exactly one, the
  ``deleted_plugin`` line) rather than silently baked in.
- Re-point correctness: the built release's ``.pth`` files carry ZERO
  residual source-root references (mirrors §4.4 "0 residual repo-prefix")
  and the healthy targets resolve into the release's own ``code/``.
- Strict build (``strict_pth_validation=True``): the same stale tree
  *fails* the build loudly with ``ReleaseManagerError``.

Run:
    .venv/bin/python3 \\
      plugins/macos_self_deployment_plugin/tests/release_manager_pth_validation_smoke.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_manager_smoke_support as support  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CandidatePaths,
    ReleaseManagerError,
)


def _pth_files(release_dir: Path) -> list[Path]:
    venv = release_dir / "venv"
    return sorted(venv.glob("lib/python*/site-packages/*.pth"))


def _assert_stale_surfaced(
    rec: support.SmokeRecorder, candidate: CandidatePaths
) -> None:
    missing = candidate.missing_pth_targets
    rec.check(len(missing) == 1, f"exactly one missing .pth target (got {len(missing)})")
    rec.check(
        any(support.STALE_PLUGIN_NAME in target for target in missing),
        f"the missing target is the stale {support.STALE_PLUGIN_NAME} line",
    )
    rec.check(
        all(str(candidate.code_root) in target for target in missing),
        "missing target was re-pointed into the release's own code/ (not the dev tree)",
    )


def _residual_refs(release_dir: Path, src_prefix: str) -> list[str]:
    return [
        str(pth)
        for pth in _pth_files(release_dir)
        if src_prefix in pth.read_text()
    ]


def _healthy_targets(release_dir: Path) -> list[str]:
    return [
        line
        for pth in _pth_files(release_dir)
        for line in pth.read_text().splitlines()
        if support.STALE_PLUGIN_NAME not in line
    ]


def _assert_repoint_correct(
    rec: support.SmokeRecorder, release_dir: Path, source: Path
) -> None:
    residual = _residual_refs(release_dir, f"{source}/")
    rec.check(not residual, f"zero residual source-root refs in .pth (got {residual})")
    healthy = _healthy_targets(release_dir)
    rec.check(
        len(healthy) >= 2 and all(Path(line).exists() for line in healthy),
        "healthy re-pointed targets resolve into the release code/",
    )


def _assert_strict_fails(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch, include_stale_pth=True)
    mgr = support.make_manager(source, scratch / "releases", strict_pth=True)
    raised = False
    try:
        mgr.build_candidate()
    except ReleaseManagerError:
        raised = True
    rec.check(raised, "strict_pth_validation raises on a missing .pth target")


def run(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch, include_stale_pth=True)
    mgr = support.make_manager(source, scratch / "releases")
    candidate = mgr.build_candidate()
    _assert_stale_surfaced(rec, candidate)
    _assert_repoint_correct(rec, candidate.release_dir, source)
    _assert_strict_fails(rec, scratch / "strict")


def main() -> int:
    rec = support.SmokeRecorder()
    scratch = support.scratch_root("pthvalid")
    print("=== release_manager_pth_validation_smoke ===")
    print(f"scratch: {scratch}")
    try:
        run(rec, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return rec.report("pth_validation")


if __name__ == "__main__":
    sys.exit(main())
