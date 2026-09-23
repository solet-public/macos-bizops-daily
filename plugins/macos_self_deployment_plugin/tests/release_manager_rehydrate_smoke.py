#!/usr/bin/env python3
"""Standalone smoke: ReleaseManager.candidate_for() rehydration seam.

The durable-rollback verb (Claude-B's ``rollback_release``) needs to turn a
finalized ``release_id`` (``ledger.previous``) back into the
:class:`CandidatePaths` spawn paths so it can launch the prior release as a
fresh process — WITHOUT hand-duplicating the code/venv/VERSION layout in the
orchestrator. ``candidate_for`` is the inverse of ``build_candidate`` for an
existing release; the layout→CandidatePaths composition lives ONCE in
``ReleaseBuilder`` (``_compose_candidate``), shared by ``build`` and the new
``rehydrate``.

Cases (synthetic source under ``~/.ananta`` scratch, NEVER ``/tmp``):
- **round-trip equality** (the drift discriminator) — ``candidate_for`` of a
  freshly-built release's id ``==`` the ``CandidatePaths`` ``build_candidate``
  returned, field-for-field (release_id + all path fields + missing_pth +
  schema_snapshot). A path or the ``bin/python3`` suffix diverging between the
  inline build and the rehydrate is invisible to any weaker check; full
  equality catches it. Exercised both for a plain release and for one carrying
  a non-empty ``missing_pth_targets`` + a real ``schema_snapshot`` (so those
  fields round-trip through ``VERSION``, not just the trivial empty/None case).
- **not finalized** -> ``candidate_for`` raises ReleaseManagerError (no dir /
  no VERSION).
- **torn VERSION** / **malformed-shape VERSION** (``[]``) -> the
  shared ``_read_version_or_raise`` converts the parse/shape error to
  ReleaseManagerError (chaining a ValueError), NOT a raw
  AttributeError/JSONDecodeError — the F1 contract for a corrupt rollback
  target.

Run:
    .venv/bin/python3 \\
      plugins/macos_self_deployment_plugin/tests/release_manager_rehydrate_smoke.py
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_manager_smoke_support as support  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    ReleaseManagerError,
)


def _capture(thunk: Callable[[], object]) -> Exception | None:
    try:
        thunk()
    except Exception as exc:  # noqa: BLE001 — concrete type asserted by caller
        return exc
    return None


def _case_round_trip_plain(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    mgr = support.make_manager(source, releases)
    built = mgr.build_candidate()  # finalized but not cut over

    rehydrated = mgr.candidate_for(built.release_id)
    rec.check(
        rehydrated == built,
        "[round-trip] candidate_for(id) == build_candidate()'s CandidatePaths (field-for-field)",
    )
    rec.check(
        rehydrated.venv_python.parent.name == "bin"
        and rehydrated.venv_python.name == "python3",
        "[round-trip] rehydrated venv_python is <release>/venv/bin/python3",
    )
    rec.check(
        rehydrated.code_root == releases / built.release_id / "code",
        "[round-trip] rehydrated code_root is <release>/code",
    )
    # Rehydration works on ANY finalized release, not just current — assert it
    # does not depend on the symlinks (no cutover happened here).
    rec.check(
        mgr.current_release is None,
        "[round-trip] (precondition) no current symlink — rehydrate is symlink-independent",
    )


def _case_round_trip_rich_fields(rec: support.SmokeRecorder, scratch: Path) -> None:
    """missing_pth_targets + schema_snapshot must round-trip through VERSION,
    not collapse to ()/None — the fields B's rollback logging/gate read.
    """
    source = support.build_fake_source(scratch, include_stale_pth=True)
    releases = scratch / "releases"
    mgr = support.make_manager(source, releases)
    snapshot: dict[str, object] = {
        "public": {"sessions": {"id": {"type": "uuid", "nullable": False}}}
    }
    built = mgr.build_candidate(schema_snapshot_fn=lambda _code_root: snapshot)

    rec.check(
        len(built.missing_pth_targets) == 1,
        "[rich] (precondition) built carries a non-empty missing_pth_targets (stale .pth)",
    )
    rehydrated = mgr.candidate_for(built.release_id)
    rec.check(
        rehydrated == built,
        "[rich] candidate_for round-trips rich fields exactly (== built)",
    )
    rec.check(
        rehydrated.missing_pth_targets == built.missing_pth_targets,
        "[rich] missing_pth_targets round-trips from VERSION (non-empty tuple)",
    )
    rec.check(
        rehydrated.schema_snapshot == snapshot,
        "[rich] schema_snapshot round-trips from VERSION (verbatim dict)",
    )


def _case_not_finalized(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    mgr = support.make_manager(source, releases)
    mgr.build_candidate()  # a real release exists, but we ask for a different id
    exc = _capture(lambda: mgr.candidate_for("rel-bogus-never-built"))
    rec.check(
        isinstance(exc, ReleaseManagerError),
        f"[not-finalized] candidate_for(unknown id) raises REM ({type(exc).__name__})",
    )


def _case_corrupt_version(rec: support.SmokeRecorder, scratch: Path, shape: str, tag: str) -> None:
    source = support.build_fake_source(scratch)
    releases = scratch / "releases"
    mgr = support.make_manager(source, releases)
    built = mgr.build_candidate()
    # The release dir + VERSION still EXIST (so _assert_finalized passes); only
    # the VERSION *content* is corrupt, so the rehydrate read is what must fail.
    (releases / built.release_id / "VERSION").write_text(shape)
    exc = _capture(lambda: mgr.candidate_for(built.release_id))
    rec.check(
        isinstance(exc, ReleaseManagerError),
        f"[{tag}] candidate_for over a corrupt VERSION raises REM ({type(exc).__name__})",
    )
    rec.check(
        isinstance(exc.__cause__, ValueError) if exc else False,
        f"[{tag}] chains a ValueError (parse/shape) as __cause__ — not raw",
    )


def main() -> int:
    rec = support.SmokeRecorder()
    scratch = support.scratch_root("rehydrate")
    print("=== release_manager_rehydrate_smoke (candidate_for seam) ===")
    print(f"scratch: {scratch}")
    try:
        _case_round_trip_plain(rec, scratch / "plain")
        _case_round_trip_rich_fields(rec, scratch / "rich")
        _case_not_finalized(rec, scratch / "notfinal")
        _case_corrupt_version(rec, scratch / "torn", "{ not valid json", "torn-version")
        _case_corrupt_version(rec, scratch / "shape", "[]", "malformed-shape")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return rec.report("rehydrate")


if __name__ == "__main__":
    sys.exit(main())
