#!/usr/bin/env python3
"""Provenance round-trip: a reconciliation's identity survives into ``VERSION``.

On a seed-materialized target there is no git identity to appeal to, so the
approved reconciliation's fingerprint IS the provenance for the bytes a release
serves (adjudication convergence 6).  This smoke drives the REAL
``ReleaseBuilder.build`` against a synthetic source tree and reads the resulting
``VERSION`` back off disk — a fake builder would only prove the argument was
accepted, not that it was persisted.

Legs, and the mutation each one reds on:

===============================  ===================================================
Leg                              Red mutation it catches
===============================  ===================================================
provenance persists              drop the field from the ``_write_version`` payload
all three terms survive          persist only the reconciliation id
ordinary build writes null       omit the key when no reconciliation supplied
null is written, not absent      make the field conditional on a truthy value
===============================  ===================================================

The last leg is the one worth stating plainly: "this release was not produced by
a reconciliation" has to be a positive, readable claim.  If the key were simply
absent on ordinary deploys, a reader could not distinguish a plain deploy from a
release built by a version of this code that predates provenance entirely — the
same absence-is-not-evidence rule ``tree_state`` already follows.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "plugins" / "macos_self_deployment_plugin" / "src"),
)

import _release_manager_smoke_support as support  # noqa: E402

_PROVENANCE = {
    "reconciliation_id": "rec_provenance_smoke",
    "source_surface_sha256": "sha256:" + "a" * 64,
    "release_surface_sha256": "sha256:" + "b" * 64,
}


def _version_of(candidate_release_dir: Path) -> dict[str, object]:
    payload = json.loads((candidate_release_dir / "VERSION").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"VERSION is not an object: {candidate_release_dir}")
    return payload


def _reconciliation_build(rec: support.SmokeRecorder, root: Path) -> None:
    source = support.build_fake_source(root)
    manager = support.make_manager(source, root / "releases-rec")
    candidate = manager.build_candidate(reconciliation_provenance=_PROVENANCE)
    version = _version_of((root / "releases-rec") / candidate.release_id)
    recorded = version.get("reconciliation_provenance")
    rec.check(recorded == _PROVENANCE, "reconciliation provenance persists into VERSION verbatim")
    rec.check(
        isinstance(recorded, dict)
        and recorded.get("reconciliation_id") == _PROVENANCE["reconciliation_id"],
        "the authorizing reconciliation id survives the build",
    )
    rec.check(
        isinstance(recorded, dict)
        and recorded.get("source_surface_sha256") == _PROVENANCE["source_surface_sha256"]
        and recorded.get("release_surface_sha256") == _PROVENANCE["release_surface_sha256"],
        "both surface digests survive the build",
    )


def _ordinary_build(rec: support.SmokeRecorder, root: Path) -> None:
    source = support.build_fake_source(root / "plain")
    manager = support.make_manager(source, root / "releases-plain")
    candidate = manager.build_candidate()
    version = _version_of((root / "releases-plain") / candidate.release_id)
    rec.check(
        "reconciliation_provenance" in version,
        "ordinary build still WRITES the key (absence must not read as 'not a reconciliation')",
    )
    rec.check(
        version.get("reconciliation_provenance") is None,
        "ordinary build records null provenance",
    )


def main() -> int:
    rec = support.SmokeRecorder()
    root = support.scratch_root("provenance")
    root.mkdir(parents=True, exist_ok=True)
    try:
        _reconciliation_build(rec, root)
        _ordinary_build(rec, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return rec.report("reconciliation provenance round-trip")


if __name__ == "__main__":
    raise SystemExit(main())
