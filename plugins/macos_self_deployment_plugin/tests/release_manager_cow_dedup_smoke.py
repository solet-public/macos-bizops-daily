#!/usr/bin/env python3
"""Standalone smoke: ReleaseManager CoW dedup via df free-space delta (§8.4).

Asserts the design §4.4 / §8.4 acceptance criterion: a second
materialized release costs FAR less physical disk than the size of the
cloned library tree, because ``cp -c`` shares unchanged extents
copy-on-write. Measured with the **df free-space delta**, never ``du``
— Claude-B's spike confirmed ``du`` double-counts shared CoW extents by
~70× (``2026-06-27_blue_green_spike_measurements.md`` §2), so ``du`` is
printed for context only and the assertion is on ``df``.

Method (synthetic source under ``~/.ananta`` scratch, NEVER ``/tmp``):
a ``BIG`` real-bytes file is planted once in the synthetic ``.venv``.
Each ``build_candidate`` CoW-clones it. The df free-space consumed by
the SECOND build (measured in the tightest possible window around the
single build call, to keep filesystem noise below the threshold) must be
``<< BIG`` — a non-CoW copy would consume a second full ``BIG``.

Run:
    .venv/bin/python3 \\
      plugins/macos_self_deployment_plugin/tests/release_manager_cow_dedup_smoke.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_manager_smoke_support as support  # noqa: E402

# 256 MiB real-bytes library blob: large enough that a non-CoW second
# copy is unmistakable against filesystem noise, small enough to write +
# clone quickly. Threshold = 1/4 of BIG (a generous margin over the few
# KB of inode/dirent metadata a synthetic ~10-file tree actually costs).
_BIG_BYTES = 256 * 1024 * 1024
_THRESHOLD_BYTES = _BIG_BYTES // 4


def _mib(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MiB"


def run(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch, big_file_bytes=_BIG_BYTES)
    releases = scratch / "releases"
    mgr = support.make_manager(source, releases)

    free_before_a = support.free_bytes(releases.parent)
    mgr.build_candidate()
    free_after_a = support.free_bytes(releases.parent)
    consumed_a = free_before_a - free_after_a

    # Tight window around the single second build — the assertion target.
    free_before_b = support.free_bytes(releases.parent)
    cand_b = mgr.build_candidate()
    free_after_b = support.free_bytes(releases.parent)
    consumed_b = free_before_b - free_after_b

    du_release = support.du_bytes(cand_b.release_dir)
    du_source_venv = support.du_bytes(source / ".venv")

    print(f"  BIG library blob          : {_mib(_BIG_BYTES)}")
    print(f"  release-A physical df Δ    : {_mib(consumed_a)}")
    print(f"  release-B physical df Δ    : {_mib(consumed_b)}  (assertion target)")
    print(f"  du(release-B) [logical]    : {_mib(du_release)}  (double-counts CoW)")
    print(f"  du(source .venv) [logical] : {_mib(du_source_venv)}")
    print(f"  threshold (BIG/4)          : {_mib(_THRESHOLD_BYTES)}")

    rec.check(
        consumed_b < _THRESHOLD_BYTES,
        f"second build df Δ ({_mib(consumed_b)}) << BIG ({_mib(_BIG_BYTES)}) — CoW dedup",
    )
    rec.check(
        consumed_a < _THRESHOLD_BYTES,
        f"first build df Δ ({_mib(consumed_a)}) << BIG — clones from the dev tree CoW too",
    )
    # du sees ~the full BIG inside each release; df does not. This IS the
    # §4.4 caveat: du is invalid for the dedup assertion.
    rec.check(
        du_release > _BIG_BYTES,
        f"du over-counts (release logical {_mib(du_release)} > BIG) — proves du is invalid",
    )


def main() -> int:
    rec = support.SmokeRecorder()
    scratch = support.scratch_root("cowdedup")
    print("=== release_manager_cow_dedup_smoke (df free-space delta, NOT du) ===")
    print(f"scratch: {scratch}")
    try:
        run(rec, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return rec.report("cow_dedup")


if __name__ == "__main__":
    sys.exit(main())
