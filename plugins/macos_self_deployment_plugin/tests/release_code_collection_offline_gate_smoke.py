#!/usr/bin/env python3
"""Offline deploy-materialization gate for the release-code collector.

This gate exercises the same collection sequence used by
``ReleaseManager.build_candidate`` without calling a running platform instance or a
blue-green cutover.  Its source, release destination, and Git checkout are
all throwaways beneath the release-manager smoke scratch root.

The three load-bearing cases are:

* an ignored local symlink and ignored bytecode are excluded before traversal;
* a tracked symlink still refuses with the deploy-path signature; and
* a stray bytecode file introduced after materialization trips the selected-set
  equality check before a release can be finalized.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_manager_smoke_support as support  # noqa: E402
from macos_self_deployment_plugin.release_code_collection import (  # noqa: E402
    ReleaseCodeCollectionError,
    ReleaseCodeCollector,
)
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CODE_SUBTREES,
    SELECTED_FILE_MANIFEST_FILENAME,
    ReleaseManagerError,
    write_selected_file_manifest,
)

_GIT_IDENTITY = (
    "-c",
    "user.name=offline-gate",
    "-c",
    "user.email=offline-gate@example.invalid",
    "-c",
    "commit.gpgsign=false",
)
_SET_MISMATCH = "materialized selected-file set differs from manifest population"
_LINK_REFUSAL = "refusing symbolic link in release code"


def _git(source: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args!r} failed: {result.stderr.strip()}")


def _committed_source(root: Path) -> Path:
    source = support.build_fake_source(root)
    (source / ".gitignore").write_text(
        "plugins/foo_plugin/local-assets/\n"
        "plugins/foo_plugin/src/foo_plugin/__pycache__/\n"
    )
    _git(source, "init", "--quiet")
    _git(source, "add", "-A")
    _git(source, *_GIT_IDENTITY, "commit", "--quiet", "-m", "baseline")
    return source


def _collector(source: Path) -> ReleaseCodeCollector:
    return ReleaseCodeCollector(
        source_root=source,
        code_subtrees=CODE_SUBTREES,
        cp_binary="cp",
        clone_timeout_seconds=30.0,
    )


def _materialize_and_verify(source: Path, destination: Path) -> tuple[ReleaseCodeCollector, object]:
    collector = _collector(source)
    plan = collector.plan()
    collector.materialize(plan, destination)
    manifest = destination.parent / SELECTED_FILE_MANIFEST_FILENAME
    write_selected_file_manifest(manifest, collector.manifest_payload(plan))
    collector.verify_materialized_manifest(plan, destination, manifest)
    return collector, plan


def _ignored_assets_pass(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = _committed_source(scratch / "ignored-assets")
    local_assets = source / "plugins" / "foo_plugin" / "local-assets"
    local_assets.mkdir()
    (local_assets / "target.txt").write_text("local-only asset\n")
    os.symlink("target.txt", local_assets / "current")
    pycache = source / "plugins" / "foo_plugin" / "src" / "foo_plugin" / "__pycache__"
    pycache.mkdir()
    (pycache / "__init__.cpython-313.pyc").write_bytes(b"not shipped")

    destination = scratch / "ignored-assets-release" / "code"
    try:
        collector, plan = _materialize_and_verify(source, destination)
    except ReleaseCodeCollectionError as exc:
        rec.check(False, f"[ignored-assets] offline materialization passed (got {exc})")
        return
    excluded = {item.relative_path: item.reason for item in plan.exclusions}  # type: ignore[attr-defined]
    rec.check(
        excluded.get("plugins/foo_plugin/local-assets") == "gitignored_path",
        "[ignored-assets] ignored local symlink directory is excluded before traversal",
    )
    rec.check(
        excluded.get("plugins/foo_plugin/src/foo_plugin/__pycache__") == "gitignored_path",
        "[ignored-assets] ignored bytecode directory is excluded from selected files",
    )
    rec.check(
        any(item.relative_path == "plugins/foo_plugin/src/foo_plugin/__init__.py" for item in plan.files),  # type: ignore[attr-defined]
        "[ignored-assets] real tracked plugin source remains selected",
    )
    rec.check(
        (destination / "plugins/foo_plugin/src/foo_plugin/__init__.py").is_file(),
        "[ignored-assets] selected tracked source materialized into temporary destination",
    )
    manager = support.make_manager(source, scratch / "ignored-assets-manager-releases")
    try:
        candidate = manager.build_candidate()
    except ReleaseManagerError as exc:
        rec.check(False, f"[ignored-assets] ReleaseManager build_candidate passed (got {exc})")
    else:
        rec.check(
            (candidate.release_dir / "code" / "plugins/foo_plugin/src/foo_plugin/__init__.py").is_file(),
            "[ignored-assets] real ReleaseManager finalizes the temporary collected release",
        )
        rec.check(
            (candidate.release_dir / SELECTED_FILE_MANIFEST_FILENAME).is_file(),
            "[ignored-assets] finalized release carries the verified selected-file manifest",
        )
    # Keep the name bound after the direct-path proof: this protects against a
    # future refactor that accidentally swaps in an unrelated verification helper.
    rec.check(isinstance(collector, ReleaseCodeCollector), "[ignored-assets] real collector executed")


def _tracked_link_refuses(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = _committed_source(scratch / "tracked-link")
    package = source / "plugins" / "foo_plugin" / "src" / "foo_plugin"
    os.symlink("__init__.py", package / "tracked-link.py")
    try:
        _collector(source).plan()
    except ReleaseCodeCollectionError as exc:
        rec.check(
            _LINK_REFUSAL in str(exc),
            f"[tracked-link] tracked link retains refusal signature ({exc})",
        )
    else:
        rec.check(False, "[tracked-link] tracked link must fail closed during plan")


def _stray_bytecode_refuses(rec: support.SmokeRecorder, scratch: Path) -> None:
    source = _committed_source(scratch / "stray-bytecode")
    manager = support.make_manager(source, scratch / "stray-bytecode-releases")

    def write_bytecode(code_root: Path) -> dict[str, object]:
        stray = code_root / "plugins" / "foo_plugin" / "src" / "foo_plugin" / "__pycache__"
        stray.mkdir()
        (stray / "__init__.cpython-313.pyc").write_bytes(b"stray generated bytecode")
        return {"fixture": "post-materialization-bytecode"}

    try:
        manager.build_candidate(schema_snapshot_fn=write_bytecode)
    except ReleaseManagerError as exc:
        rec.check(
            _SET_MISMATCH in str(exc),
            f"[stray-bytecode] build_candidate rejects post-copy population drift with set-equality signature ({exc})",
        )
    else:
        rec.check(False, "[stray-bytecode] build_candidate must fail selected-file equality")


def main() -> int:
    rec = support.SmokeRecorder()
    scratch = support.scratch_root("release-collection-offline-gate")
    print("=== release_code_collection_offline_gate_smoke ===")
    print(f"scratch: {scratch}")
    try:
        _ignored_assets_pass(rec, scratch)
        _tracked_link_refuses(rec, scratch)
        _stray_bytecode_refuses(rec, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return rec.report("release_code_collection_offline_gate")


if __name__ == "__main__":
    sys.exit(main())
