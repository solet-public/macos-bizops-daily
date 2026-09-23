#!/usr/bin/env python3
"""Standalone smoke: ReleaseManager schema-snapshot seam (Phase-2 preflight).

Covers the dependency-inverted schema-snapshot seam that feeds Claude-B's
§3 preflight DDL-free gate (contract settled C<->B 2026-06-28). The seam
keeps ReleaseManager dependency-free: a caller-supplied
``schema_snapshot_fn`` (carrying the platform ``collect_schemas`` import)
is invoked against the materialized clone's ``code/``; its result is
stored verbatim in ``VERSION`` + on ``CandidatePaths`` and read back via
``current_schema_snapshot()``.

Cases:
- **with callback** — the fn runs AFTER ``code/`` is materialized,
  receives the staging ``code/`` root, and its return is surfaced on
  ``CandidatePaths.schema_snapshot``, persisted in ``VERSION``, and
  readable as the gate's OLD side via ``current_schema_snapshot()`` after
  cutover.
- **deferred (None)** — B's THIS-cycle path: no producer -> ``None``
  snapshot on ``CandidatePaths``, ``null`` in ``VERSION``,
  ``current_schema_snapshot()`` -> ``None`` (gate DEFERs loudly).
- **callback raises** — propagates raw and fails the build (no
  snapshot-less release that would silently defeat the gate).
- **virgin first boot** — ``current_schema_snapshot()`` -> ``None`` with
  no current release.
- **corrupt current VERSION** — a torn / malformed-shape (``[]``) current
  ``VERSION`` makes ``current_schema_snapshot()`` raise
  ReleaseManagerError, NOT silently return ``None``. Critical for the gate:
  a corrupt OLD snapshot must not masquerade as 'no old schema -> additive'
  (false-additive). Distinct from the legitimate ``None`` (first deploy /
  deferred producer), which stays ``None``.

Run:
    .venv/bin/python3 \\
      plugins/macos_self_deployment_plugin/tests/release_manager_schema_snapshot_smoke.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_manager_smoke_support as support  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    ReleaseManagerError,
)

_MARKER_SNAPSHOT: dict[str, object] = {
    "core": {"sessions": {"id": {"type": "uuid", "primary_key": True}}}
}


def _with_callback(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    seen: dict[str, object] = {}

    def snapshot_fn(code_root: Path) -> dict[str, object]:
        seen["name"] = code_root.name
        seen["clone_present"] = (
            code_root / "ananta" / "src" / "ananta" / "__init__.py"
        ).is_file()
        return _MARKER_SNAPSHOT

    mgr = support.make_manager(source, releases)
    cand = mgr.build_candidate(schema_snapshot_fn=snapshot_fn)
    rec.check(seen.get("name") == "code", "callback received the clone's code/ root")
    rec.check(
        seen.get("clone_present") is True,
        "callback ran AFTER code/ materialized (clone visible at the path)",
    )
    rec.check(
        cand.schema_snapshot == _MARKER_SNAPSHOT,
        "CandidatePaths.schema_snapshot == callback return (gate NEW side)",
    )
    version = json.loads(cand.version_file.read_text())
    rec.check(
        version.get("schema_snapshot") == _MARKER_SNAPSHOT,
        "VERSION stores schema_snapshot verbatim",
    )
    mgr.cutover(cand)
    rec.check(
        mgr.current_schema_snapshot() == _MARKER_SNAPSHOT,
        "current_schema_snapshot() == current release's snapshot (gate OLD side)",
    )


def _deferred_none(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    mgr = support.make_manager(source, releases)
    cand = mgr.build_candidate()  # schema_snapshot_fn=None — B's this-cycle DEFER path
    rec.check(
        cand.schema_snapshot is None,
        "[defer] no callback -> CandidatePaths.schema_snapshot is None",
    )
    version = json.loads(cand.version_file.read_text())
    rec.check(
        "schema_snapshot" in version and version["schema_snapshot"] is None,
        "[defer] VERSION schema_snapshot key present and null",
    )
    mgr.cutover(cand)
    rec.check(
        mgr.current_schema_snapshot() is None,
        "[defer] current_schema_snapshot() is None (gate DEFERs loudly)",
    )


def _callback_raises(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    mgr = support.make_manager(source, releases)

    def boom(_: Path) -> dict[str, object]:
        raise RuntimeError("snapshot producer failed")

    raised = False
    try:
        mgr.build_candidate(schema_snapshot_fn=boom)
    except RuntimeError:
        raised = True
    rec.check(raised, "callback exception propagates raw (fail-loud build)")
    rec.check(
        mgr.current_release is None,
        "failed build left no current release (no snapshot-less release shipped)",
    )


def _first_boot_none(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    mgr = support.make_manager(source, scratch / "releases_never")
    rec.check(
        mgr.current_schema_snapshot() is None,
        "current_schema_snapshot() is None on virgin first boot",
    )


def _corrupt_current_version(rec: support.SmokeRecorder, scratch: Path) -> None:
    """A corrupt CURRENT VERSION must FAIL LOUD (REM), not collapse to None.

    Silent-None would make the §3 preflight read 'no old schema' and treat
    every diff as additive — a false-additive that defeats the durable-
    rollback guarantee. After the shared ``_read_version_or_raise`` fold,
    current_schema_snapshot raises ReleaseManagerError on a torn/malformed
    current VERSION instead.
    """
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    mgr = support.make_manager(source, releases)
    cand = mgr.build_candidate(schema_snapshot_fn=lambda _code_root: _MARKER_SNAPSHOT)
    mgr.cutover(cand)
    for shape, tag in (("{ torn version", "torn"), ("[]", "malformed-shape")):
        (releases / cand.release_id / "VERSION").write_text(shape)
        raised = False
        try:
            mgr.current_schema_snapshot()
        except ReleaseManagerError:
            raised = True
        rec.check(
            raised,
            f"[corrupt-current/{tag}] current_schema_snapshot raises REM (not silent None)",
        )


def main() -> int:
    rec = support.SmokeRecorder()
    scratch = support.scratch_root("schemasnap")
    print("=== release_manager_schema_snapshot_smoke ===")
    print(f"scratch: {scratch}")
    try:
        _with_callback(rec, scratch / "with_cb")
        _deferred_none(rec, scratch / "deferred")
        _callback_raises(rec, scratch / "raises")
        _first_boot_none(rec, scratch / "first_boot")
        _corrupt_current_version(rec, scratch / "corrupt_current")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return rec.report("schema_snapshot")


if __name__ == "__main__":
    sys.exit(main())
