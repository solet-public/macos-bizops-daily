"""End-to-end Step-4 update against a real Git fixture.

Covers: actionable preview and cache reuse, hostile-configuration refusal
before any probe, the ignored-file clobber collision, the untracked-only
frontier, the exact hook-suppressed fast-forward with axis-split inventory
publication, idempotent re-apply, crash resume on both sides of the
fast-forward, a post-pointer blocked journal, and the descriptor misbinding
refusal.  The only test seam is ``transport_url``; every Git action is real.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]
import solet_manager.update_execution as execution  # noqa: E402
from _step5_support import bundle_digest, bundle_document, bundle_files, candidate_tree_files  # noqa: E402
from solet_manager._existing_install_inspection_metadata import InstalledUpdateDescriptor  # noqa: E402
from solet_manager.errors import (  # noqa: E402
    InventoryChannelDescriptorMisbindingError,
    ProbeDriftError,
    UpdateBlockedError,
)
from solet_manager.existing_install_inspection import (  # noqa: E402
    ChannelInspectionIdentity,
    ExistingInstallContractIdentity,
    InstalledInspectionMetadata,
)
from solet_manager.maintenance_inventory import (  # noqa: E402
    read_maintenance_inventory_v2,
    write_maintenance_inventory_v2,
)
from solet_manager.models import (  # noqa: E402
    ActiveOperation,
    ChannelIdentity,
    ContractIdentities,
    FilesystemIdentity,
    InstanceInventoryRecordV2,
    JsonValue,
    MaintenanceOperationKind,
    ManagementOrigin,
    ManagementState,
    ObservedProvenanceIdentity,
    ReleaseIdentity,
    ServiceIdentity,
    TargetIdentity,
    UpdateEligibility,
    UpdateEligibilityState,
)
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.release_lock import seed_lock_from_fields  # noqa: E402
from solet_manager.seed_lock_parser import parse_seed_lock_bytes  # noqa: E402
from solet_manager.update_execution import (  # noqa: E402
    PLANNED_ACTIONS,
    UpdateRequest,
    apply_update,
    preview_update_instance,
)
from solet_manager.update_journal import read_update_journal  # noqa: E402

ORIGIN_ID = "123e4567-e89b-12d3-a456-426614174001"
CANONICAL = "https://github.com/example/seed.git"
# Bound per fixture: the digest of the transition bundle sealed into the candidate.
CONTRACT = "sha256:" + "e" * 64
_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


@dataclass(frozen=True)
class Release:
    commit: str
    tree: str
    provenance: bytes
    seed_id: str
    manifest: str
    source_commit: str
    tag: str


@dataclass(frozen=True)
class Fixture:
    root: Path
    source: Path
    target: Path
    paths: ManagerPaths
    baseline: Release
    candidate: Release
    descriptor_digest: str
    request: UpdateRequest


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(("git", "-C", str(repo), *args), check=True, capture_output=True, text=True, env=_ENV)
    return completed.stdout.strip()


def _stamp(source_commit: str, manifest: str) -> tuple[bytes, str]:
    seed_id = str(uuid.uuid5(uuid.UUID(ORIGIN_ID), f"{source_commit}:{manifest}::"))
    stamp = {
        "ancestry": [],
        "bundle": {"name": "macos-bizops", "platform": "local"},
        "lineage": [],
        "manifest_sha256": manifest,
        "origin_id": ORIGIN_ID,
        "schema_version": 1,
        "seed_id": seed_id,
        "signature": None,
        "source_commit": source_commit,
        "source_date": "2026-09-16T00:00:00+00:00",
    }
    return (json.dumps(stamp, indent=2, sort_keys=True) + "\n").encode(), seed_id


def _seal(repo: Path, source_commit: str, manifest: str, tag: str, files: dict[str, str]) -> Release:
    provenance, seed_id = _stamp(source_commit, manifest)
    (repo / "PROVENANCE.json").write_bytes(provenance)
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A", "-f")
    message = "\n".join(
        (
            "Seed bundle (factory-sealed)",
            "",
            f"Seed-Id: {seed_id}",
            f"Origin-Id: {ORIGIN_ID}",
            f"Manifest-SHA256: {manifest}",
            f"Assembled-Ref: {source_commit}",
            "License-Policy: public_apache",
            "Minted-At: 2026-09-16T00:00:00+00:00",
        )
    )
    _git(repo, "commit", "--quiet", "-m", message)
    _git(repo, "tag", "-a", tag, "-m", tag)
    return Release(_git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}"), provenance, seed_id, manifest, source_commit, tag)


def _descriptor(candidate: Release) -> bytes:
    value = {
        "schema_version": 3,
        "channel_id": "stable",
        "repository": CANONICAL,
        "release_tag": candidate.tag,
        "commit": candidate.commit,
        "tree_hash": candidate.tree,
        "archive_sha256": "f" * 64,
        "profile": "macos-bizops",
        "provenance": {
            "schema_version": 1,
            "provenance_sha256": hashlib.sha256(candidate.provenance).hexdigest(),
            "seed_id": candidate.seed_id,
            "origin_id": ORIGIN_ID,
            "manifest_sha256": candidate.manifest,
            "bundle_name": "macos-bizops",
            "platform": "local",
            "source_commit": candidate.source_commit,
            "source_date": "2026-09-16T00:00:00+00:00",
        },
        "existing_install_contract": {"flow_id": "existing-install", "flow_schema_version": 1, "bundle_digest": CONTRACT},
        "allowed_repository_migrations": [],
    }
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _installed(candidate: Release, descriptor: bytes) -> InstalledUpdateDescriptor:
    digest = "sha256:" + hashlib.sha256(descriptor).hexdigest()
    identity = ChannelInspectionIdentity(
        "stable",
        CANONICAL,
        candidate.tag,
        candidate.commit,
        candidate.tree,
        "macos-bizops",
        hashlib.sha256(candidate.provenance).hexdigest(),
        candidate.seed_id,
        ORIGIN_ID,
        candidate.manifest,
        ExistingInstallContractIdentity("existing-install", 1, CONTRACT),
        "catalog",
        "1" * 64,
        "seed",
        digest[7:],
        digest,
        "anchors",
        "2" * 64,
    )
    metadata = InstalledInspectionMetadata(identity, seed_lock_from_fields(parse_seed_lock_bytes(descriptor)), ())
    return InstalledUpdateDescriptor(metadata, descriptor)


def _enroll(paths: ManagerPaths, target: Path, baseline: Release, *, descriptor_digest: str) -> InstanceInventoryRecordV2:
    target_stat, parent_stat = target.stat(), target.parent.stat()
    now = "2026-09-18T00:00:00Z"
    record = InstanceInventoryRecordV2(
        "ins_" + hashlib.sha256(str(target).encode()).hexdigest()[:32],
        "fixture",
        TargetIdentity(str(target), FilesystemIdentity(target_stat.st_dev, target_stat.st_ino), FilesystemIdentity(parent_stat.st_dev, parent_stat.st_ino)),
        ManagementOrigin.IMPORT,
        ManagementState.DIAGNOSTIC,
        UpdateEligibility(UpdateEligibilityState.AVAILABLE, ()),
        ServiceIdentity(str(target / ".venv/bin/solet"), str(target / ".venv/bin/solet-bridge"), str(target.parent / "bin/fixture"), None, None, str(target / "profile"), "local.solet.fixture", None, None),
        ChannelIdentity("stable", descriptor_digest, CANONICAL),
        ObservedProvenanceIdentity("strict", "sha256:" + hashlib.sha256(baseline.provenance).hexdigest(), baseline.seed_id, ORIGIN_ID, "sha256:" + baseline.manifest, None),
        ReleaseIdentity(CANONICAL, baseline.commit, baseline.tree, baseline.tag),
        None,
        None,
        ContractIdentities(CONTRACT, None, None, None, None),
        "sha256:" + "4" * 64,
        None,
        "opr_" + "5" * 32,
        now,
        now,
        now,
        now,
    )
    write_maintenance_inventory_v2(paths.maintenance_inventory_path, (record,))
    return record


def _fixture(root: Path, *, misbound: bool = False) -> Fixture:
    global CONTRACT
    source = root / "source"
    source.mkdir()
    _git(source, "init", "--quiet", "-b", "main")
    _git(source, "config", "user.name", "Fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    baseline = _seal(source, "a" * 40, "b" * 64, "r1", {".gitignore": "local.cfg\n*.log\n", "README.md": "release n\n"})
    files = bundle_files(bundle_document(baseline))
    CONTRACT = bundle_digest(files)
    tree = {str(k): (v.decode() if isinstance(v, bytes) else v) for k, v in candidate_tree_files(files).items()}
    tree["local.cfg"] = "candidate config\n"
    candidate = _seal(source, "c" * 40, "d" * 64, "r2", tree)
    target = root / "target"
    subprocess.run(("git", "clone", "--quiet", str(source), str(target)), check=True, capture_output=True, env=_ENV)
    _git(target, "checkout", "--quiet", "-B", "main", baseline.commit)
    _git(target, "remote", "set-url", "origin", CANONICAL)
    # Step 7 (CH-10/11): a fresh --yes refuses without an instance interpreter, so the target carries the
    # genesis-shaped venv marker every real clone has (ignored, never part of the local state).
    (target / ".git" / "info" / "exclude").write_text(".venv/\n", encoding="utf-8")
    (target / ".venv" / "bin").mkdir(parents=True)
    (target / ".venv" / "bin" / "python3").write_text("fixture\n")
    (target / ".venv" / "pyvenv.cfg").write_text("home = /fixture\nversion = 3.13.0\n")
    paths = ManagerPaths(root / "manager" / "config", root / "manager" / "state", root / "manager" / "cache")
    for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
        directory.mkdir(parents=True, mode=0o700)
    descriptor = _descriptor(candidate)
    installed = _installed(candidate, descriptor)
    _enroll(paths, target, baseline, descriptor_digest=CONTRACT if misbound else "sha256:" + "3" * 64)
    request = UpdateRequest("fixture", paths, descriptor_loader=lambda channel, tracker: installed, transport_url=str(source))
    return Fixture(root, source, target, paths, baseline, candidate, installed.metadata.channel_identity.descriptor_digest, request)


def _expect(kind: type[Exception], action: Callable[[], object], label: str) -> Exception:
    try:
        action()
    except kind as exc:
        return exc
    raise AssertionError(label)


def _data(value: JsonValue, key: str) -> JsonValue:
    assert isinstance(value, dict), value
    return value[key]


def _record(fixture: Fixture) -> InstanceInventoryRecordV2:
    records = read_maintenance_inventory_v2(fixture.paths.maintenance_inventory_path)
    assert len(records) == 1
    return records[0]


def _assert_preview_ready(fixture: Fixture) -> str:
    first = preview_update_instance(fixture.request)
    assert first.status == "preview_ready", first
    assert first.exit_code == 0
    fingerprint = first.data["approval_fingerprint"]
    assert isinstance(fingerprint, str)
    assert fingerprint.startswith("sha256:")
    assert _data(first.data["candidate_cache"], "status") == "acquired"
    assert first.data["collisions"] == []
    assert first.data["planned_actions"] == list(PLANNED_ACTIONS)
    assert _data(first.data["preservation"], "manager_state_writes") == 0
    assert _data(first.data["preservation"], "manager_cache_writes") == 1
    return fingerprint


def _assert_preview_reuse(fixture: Fixture, fingerprint: str) -> None:
    second = preview_update_instance(fixture.request)
    assert _data(second.data["candidate_cache"], "status") == "reused"
    assert second.data["approval_fingerprint"] == fingerprint
    assert not (fixture.paths.state_dir / "operations").exists()
    assert not (fixture.paths.state_dir / "locks").exists()
    assert _git(fixture.target, "rev-parse", "HEAD") == fixture.baseline.commit


def _assert_hostile_config_refused(fixture: Fixture) -> None:
    marker = fixture.root / "FSMON_RAN"
    hook = fixture.root / "fsmon.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\necho\n")
    hook.chmod(0o755)
    _git(fixture.target, "config", "core.fsmonitor", str(hook))
    hostile = preview_update_instance(fixture.request)
    assert hostile.exit_code == 3, hostile
    assert hostile.error_kind == "git_execution_surface_unsafe"
    assert hostile.data["approval_fingerprint"] is None
    assert not marker.exists(), "fsmonitor hook executed during a blocked preview"
    _git(fixture.target, "config", "--unset", "core.fsmonitor")


def _assert_ignored_collision_refused(fixture: Fixture, fingerprint: str) -> None:
    inventory_before = fixture.paths.maintenance_inventory_path.read_bytes()
    local_cfg = fixture.target / "local.cfg"
    local_cfg.write_text("operator local\n")
    assert _git(fixture.target, "status", "--porcelain") == ""
    collided = preview_update_instance(fixture.request)
    assert collided.status == "awaiting_user", collided
    assert collided.exit_code == 3
    assert {"reason": "untracked_destination_collision", "path": "local.cfg"} in cast(list[JsonValue], collided.data["collisions"])
    assert collided.data["approval_fingerprint"] is None
    _expect(ProbeDriftError, lambda: apply_update(fixture.request, fingerprint), "collided target applied")
    assert local_cfg.read_text() == "operator local\n"
    assert _git(fixture.target, "rev-parse", "HEAD") == fixture.baseline.commit
    assert fixture.paths.maintenance_inventory_path.read_bytes() == inventory_before
    assert not (fixture.paths.state_dir / "operations").exists()
    local_cfg.unlink()


def _assert_untracked_frontier(fixture: Fixture) -> None:
    """Step 7 section 6.1 (Class U, A7.2): an untracked non-colliding path is admitted, committed and bound into the fingerprint."""
    notes = fixture.target / "notes.txt"
    notes.write_text("operator notes\n")
    untracked = preview_update_instance(fixture.request)
    assert untracked.status == "preview_ready", untracked
    assert "tracked_state_present" not in cast(list[JsonValue], _data(untracked.data["topology"], "reasons"))
    local_state = cast(dict[str, JsonValue], untracked.data["local_state"])
    committed = cast(list[JsonValue], local_state["committed"])
    assert [cast(dict[str, JsonValue], row)["path"] for row in committed] == ["notes.txt"], committed
    assert isinstance(local_state["local_state_commitment"], str)
    committed_fingerprint = untracked.data["approval_fingerprint"]
    notes.unlink()
    clean = preview_update_instance(fixture.request)
    assert clean.data["approval_fingerprint"] != committed_fingerprint, "the commitment is not in the approval preimage"
    assert cast(dict[str, JsonValue], clean.data["local_state"])["local_state_commitment"] is None


def _assert_apply(fixture: Fixture, fingerprint: str) -> str:
    hooks = fixture.target / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    hook_marker = fixture.root / "HOOK_RAN"
    post_merge = hooks / "post-merge"
    post_merge.write_text(f"#!/bin/sh\ntouch {hook_marker}\n")
    post_merge.chmod(0o755)
    applied = apply_update(fixture.request, fingerprint)
    assert applied.status == "source_advanced", applied
    assert applied.exit_code == 0
    assert applied.data["target_actions_executed"] == ["target.fetch_exact_candidate", "target.fast_forward_exact_candidate"]
    assert not hook_marker.exists(), "post-merge hook executed during the fast-forward"
    return cast(str, applied.data["operation_id"])


def _assert_target_advanced(fixture: Fixture) -> None:
    assert _git(fixture.target, "rev-parse", "HEAD") == fixture.candidate.commit
    assert _git(fixture.target, "rev-parse", "HEAD^{tree}") == fixture.candidate.tree
    assert _git(fixture.target, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(fixture.target, "status", "--porcelain") == ""
    assert (fixture.target / "local.cfg").read_text() == "candidate config\n"
    assert (fixture.target / "docs" / "new.txt").exists()
    assert _git(fixture.target, "rev-parse", f"refs/solet/candidates/{fixture.descriptor_digest[7:]}^{{commit}}") == fixture.candidate.commit


def _assert_axis_split(record: InstanceInventoryRecordV2, fixture: Fixture) -> None:
    assert record.source_release == ReleaseIdentity(CANONICAL, fixture.candidate.commit, fixture.candidate.tree, "r2")
    assert record.runtime_release is None
    assert record.verified_release is None
    assert record.management_state is ManagementState.DIAGNOSTIC
    assert record.update_eligibility == UpdateEligibility(UpdateEligibilityState.BLOCKED, ("update_in_progress",))


def _assert_identity_axes(record: InstanceInventoryRecordV2, fixture: Fixture, operation_id: str) -> None:
    assert record.active_operation == ActiveOperation(MaintenanceOperationKind.UPDATE, operation_id)
    assert record.channel == ChannelIdentity("stable", fixture.descriptor_digest, CANONICAL)
    assert record.channel.descriptor_digest != CONTRACT
    assert record.contract_identities == ContractIdentities(CONTRACT, None, CONTRACT, None, None)
    assert record.observed_provenance.provenance_sha256 == "sha256:" + hashlib.sha256(fixture.candidate.provenance).hexdigest()
    assert record.last_verified_operation_id == "opr_" + "5" * 32
    assert record.last_verified_at == "2026-09-18T00:00:00Z"


def _assert_journal_and_idempotence(fixture: Fixture, fingerprint: str, operation_id: str) -> None:
    journal = read_update_journal(fixture.paths.operation_path(_record(fixture).instance_id, operation_id))
    assert journal["status"] == "source_advanced"
    stages = [cast(dict[str, JsonValue], item)["stage_id"] for item in cast(list[JsonValue], journal["attempts"])]
    assert stages == ["operation_published", "target_fetch_verified", "source_fast_forward", "source_identity_verified"], stages
    again = apply_update(fixture.request, fingerprint)
    assert (again.status, again.data["target_actions_executed"]) == ("source_advanced", [])
    _assert_runtime_preview_blocked(fixture, operation_id)


def _assert_runtime_preview_blocked(fixture: Fixture, operation_id: str) -> None:
    # Step 5: at source_advanced, --dry-run renders the RUNTIME plan.  This
    # fixture has no target adapter vector and no LaunchAgent plist, so the
    # runtime preview is truthfully blocked before any target write.
    after = preview_update_instance(fixture.request)
    assert (after.status, after.exit_code) == ("awaiting_user", 3), after
    observed = (after.data["journal_status"], after.data["operation_id"], after.data["runtime_approval_fingerprint"])
    assert observed == ("source_advanced", operation_id, None), observed
    _expect(ProbeDriftError, lambda: apply_update(fixture.request, "sha256:" + "0" * 64), "foreign fingerprint resumed the active update")


def _assert_pending_after_crash(fixture: Fixture, fingerprint: str) -> None:
    record = _record(fixture)
    assert record.active_operation is not None
    assert record.source_release.commit == fixture.baseline.commit
    journal_path = fixture.paths.operation_path(record.instance_id, record.active_operation.operation_id)
    assert read_update_journal(journal_path)["status"] == "source_applying"
    pending = preview_update_instance(fixture.request)
    assert pending.status == "resume_pending", pending
    assert pending.exit_code == 3
    assert pending.data["approval_fingerprint"] == fingerprint


def _assert_resume_before_fast_forward(root: Path) -> None:
    fixture = _fixture(root)
    fingerprint = cast(str, preview_update_instance(fixture.request).data["approval_fingerprint"])
    original = execution._run_git

    def crashing(cwd: Path, args: tuple[str, ...], *, hooks_dir: Path | None = None) -> subprocess.CompletedProcess[bytes]:
        if args[0] == "merge":
            raise RuntimeError("simulated crash before the fast-forward")
        return original(cwd, args, hooks_dir=hooks_dir)

    with patch.object(execution, "_run_git", crashing):
        _expect(RuntimeError, lambda: apply_update(fixture.request, fingerprint), "crash injection did not fire")
    _assert_pending_after_crash(fixture, fingerprint)
    assert _git(fixture.target, "rev-parse", "HEAD") == fixture.baseline.commit
    resumed = apply_update(fixture.request, fingerprint)
    assert resumed.status == "source_advanced"
    assert resumed.data["target_actions_executed"] == ["target.fast_forward_exact_candidate"]
    assert _git(fixture.target, "rev-parse", "HEAD") == fixture.candidate.commit
    assert _record(fixture).source_release.commit == fixture.candidate.commit


def _assert_resume_after_fast_forward(root: Path) -> None:
    fixture = _fixture(root)
    fingerprint = cast(str, preview_update_instance(fixture.request).data["approval_fingerprint"])

    def crash_verify(self: object) -> object:
        raise RuntimeError("simulated crash after the fast-forward")

    with patch.object(execution._Execution, "_verify_advanced", crash_verify):
        _expect(RuntimeError, lambda: apply_update(fixture.request, fingerprint), "crash injection did not fire")
    _assert_pending_after_crash(fixture, fingerprint)
    assert _git(fixture.target, "rev-parse", "HEAD") == fixture.candidate.commit
    resumed = apply_update(fixture.request, fingerprint)
    assert resumed.status == "source_advanced"
    assert resumed.data["target_actions_executed"] == []
    advanced = _record(fixture)
    assert advanced.source_release.commit == fixture.candidate.commit
    assert advanced.runtime_release is None


def _assert_blocked_after_pointer(root: Path) -> None:
    fixture = _fixture(root)
    fingerprint = cast(str, preview_update_instance(fixture.request).data["approval_fingerprint"])

    def blocked_fetch(self: object) -> None:
        raise UpdateBlockedError("history_diverged", "injected divergence", repair="Step-6 reconciliation is required.")

    with patch.object(execution._Execution, "_fetch", blocked_fetch):
        exc = _expect(UpdateBlockedError, lambda: apply_update(fixture.request, fingerprint), "blocked fetch did not propagate")
    assert cast(UpdateBlockedError, exc).error_kind == "history_diverged"
    record = _record(fixture)
    assert record.active_operation is not None and record.source_release.commit == fixture.baseline.commit
    journal = read_update_journal(fixture.paths.operation_path(record.instance_id, record.active_operation.operation_id))
    assert journal["status"] == "blocked" and _data(journal["result"], "reason_code") == "history_diverged"
    assert _git(fixture.target, "rev-parse", "HEAD") == fixture.baseline.commit
    terminal = preview_update_instance(fixture.request)
    assert terminal.status == "blocked" and terminal.exit_code == 3 and terminal.error_kind == "history_diverged"
    _expect(UpdateBlockedError, lambda: apply_update(fixture.request, fingerprint), "terminal journal re-applied")


def main() -> int:
    with TemporaryDirectory() as temporary:
        fixture = _fixture(Path(temporary).resolve())
        fingerprint = _assert_preview_ready(fixture)
        _assert_preview_reuse(fixture, fingerprint)
        _assert_hostile_config_refused(fixture)
        _assert_ignored_collision_refused(fixture, fingerprint)
        _assert_untracked_frontier(fixture)
        operation_id = _assert_apply(fixture, fingerprint)
        _assert_target_advanced(fixture)
        _assert_axis_split(_record(fixture), fixture)
        _assert_identity_axes(_record(fixture), fixture, operation_id)
        _assert_journal_and_idempotence(fixture, fingerprint, operation_id)
    with TemporaryDirectory() as temporary:
        _assert_resume_before_fast_forward(Path(temporary).resolve())
    with TemporaryDirectory() as temporary:
        _assert_resume_after_fast_forward(Path(temporary).resolve())
    with TemporaryDirectory() as temporary:
        _assert_blocked_after_pointer(Path(temporary).resolve())
    with TemporaryDirectory() as temporary:
        misbound = _fixture(Path(temporary).resolve(), misbound=True)
        _expect(InventoryChannelDescriptorMisbindingError, lambda: preview_update_instance(misbound.request), "misbound row previewed")
        assert _git(misbound.target, "rev-parse", "HEAD") == misbound.baseline.commit
    print("update_execution_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
