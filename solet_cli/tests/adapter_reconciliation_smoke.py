#!/usr/bin/env python3
"""Focused controls for snapshot-only frozen-code reconciliation."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.adapter_reconciliation import AdapterReconciliationManager  # noqa: E402
from solet_manager.cli import run  # noqa: E402
from solet_manager.errors import VenvIncompatibleError  # noqa: E402
from solet_manager.models import InstanceRecord  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_lock(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "https://github.com/solet-public/macos-bizops.git",
                "release_tag": "snapshot-fixture-1",
                "commit": "a" * 40,
                "tree_hash": "b" * 40,
                "profile": "macos-bizops",
            }
        ),
        encoding="utf-8",
    )


def _record(name: str, target: Path) -> InstanceRecord:
    return InstanceRecord(
        name=name,
        target=str(target),
        launcher=str(target / "client" / "bin" / name),
        seed_repository="https://github.com/solet-public/macos-bizops.git",
        seed_tag="snapshot-fixture-0",
        seed_commit="c" * 40,
        seed_tree_hash="d" * 40,
        profile="macos-bizops",
        flow_id="macos.repository_setup",
        flow_source_revision="e" * 40,
        flow_contract_digest="sha256:" + "f" * 64,
        created_at="2026-09-02T00:00:00+00:00",
        updated_at="2026-09-02T00:00:00+00:00",
    )


def _manager(paths: ManagerPaths, lock: Path, payload: Path) -> AdapterReconciliationManager:
    def materialize(_seed: object, _target: Path, _paths: ManagerPaths) -> Path:
        return payload

    def compatible(_target: Path, _files: tuple[Path, ...]) -> None:
        return None

    return AdapterReconciliationManager(
        paths=paths,
        seed_lock_path=lock,
        payload_materializer=materialize,
        compatibility_check=compatible,
    )


def _fixture(root: Path) -> tuple[ManagerPaths, Path, Path, Path]:
    target = root / "target"
    payload = root / "payload"
    _write(
        target / "plugins" / "alpha" / "src" / "alpha" / "adapter.py", 'VALUE = "old adapter"\n'
    )
    _write(target / "plugins" / "obsolete" / "src" / "obsolete" / "old.py", 'VALUE = "remove me"\n')
    _write(
        target / "solet_setup_contracts" / "src" / "solet_setup_contracts" / "selected.py",
        'VALUE = "old contracts package"\n',
    )
    _write(
        payload / "plugins" / "alpha" / "src" / "alpha" / "adapter.py", 'VALUE = "new adapter"\n'
    )
    _write(
        payload / "plugins" / "beta" / "src" / "beta" / "plugin.py", 'VALUE = "new plugin"\n'
    )
    _write(
        payload / "solet_setup_contracts" / "src" / "solet_setup_contracts" / "selected.py",
        'VALUE = "new contracts package"\n',
    )
    lock = root / "seed.lock.json"
    _seed_lock(lock)
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    InstanceRegistry(paths.registry_path).add(_record("snapshot", target))
    return paths, lock, target, payload


def _run() -> None:
    with tempfile.TemporaryDirectory(prefix="adapter-reconciliation-smoke-") as temporary:
        root = Path(temporary)
        live_refusal = run(
            ["--home", str(root / "live-refusal-home"), "reconcile-adapter", "snapshot"]
        )
        _check(live_refusal.status == "not_yet_invokable", "CLI refuses normal live invocation")
        paths, lock, target, payload = _fixture(root)
        manager = _manager(paths, lock, payload)
        knowledge_base = target / "knowledge_base" / "preserved.md"
        _write(knowledge_base, "must not change\n")

        before_contract = (target / "solet_setup_contracts" / "src" / "solet_setup_contracts" / "selected.py").read_text()
        preview = manager.run("snapshot", dry_run=True, approved_fingerprint=None)
        fingerprint = str(preview.data["approval_fingerprint"])
        _check(preview.status == "preview_ready", "preview is available")
        _check(preview.data["dry_run_writes"] == 0, "preview writes no target bytes")
        _check("obsolete" in "\n".join(map(str, preview.data["changed_files"])), "preview includes deletion")
        _check(
            (target / "solet_setup_contracts" / "src" / "solet_setup_contracts" / "selected.py").read_text()
            == before_contract,
            "dry-run leaves target unchanged",
        )

        wrong = manager.run("snapshot", dry_run=False, approved_fingerprint="sha256:" + "0" * 64)
        _check(wrong.error_kind == "probe_drift", "wrong approval stops before mutation")

        applied = manager.run("snapshot", dry_run=False, approved_fingerprint=fingerprint)
        _check(applied.status == "reconciled_not_yet_invokable", "apply remains explicitly non-live")
        _check(
            (target / "plugins" / "alpha" / "src" / "alpha" / "adapter.py").read_text()
            == 'VALUE = "new adapter"\n',
            "adapter stratum refreshed",
        )
        _check(
            (target / "plugins" / "beta" / "src" / "beta" / "plugin.py").read_text()
            == 'VALUE = "new plugin"\n',
            "plugin-tree stratum refreshed",
        )
        _check(
            (target / "solet_setup_contracts" / "src" / "solet_setup_contracts" / "selected.py").read_text()
            == 'VALUE = "new contracts package"\n',
            "solet_setup_contracts stratum refreshed",
        )
        _check(
            not (target / "plugins" / "obsolete" / "src" / "obsolete" / "old.py").exists(),
            "obsolete frozen-code byte removed",
        )
        receipt = paths.adapter_reconciliations_dir / "snapshot.json"
        _check(receipt.exists(), "receipt written")
        receipt_bytes = receipt.read_bytes()
        _check(b"before_sha256" in receipt_bytes and b"after_sha256" in receipt_bytes, "receipt hashes both sides")

        no_op = manager.run("snapshot", dry_run=True, approved_fingerprint=None)
        _check(no_op.data["changed_files"] == [], "at-vintage target is a clean no-op")

        _write(target / "plugins" / "alpha" / "src" / "alpha" / "adapter.py", "drifted after preview\n")
        stale = manager.run("snapshot", dry_run=False, approved_fingerprint=str(no_op.data["approval_fingerprint"]))
        _check(stale.error_kind == "probe_drift", "between-preview-and-apply drift stops")

        def incompatible(_target: Path, _files: tuple[Path, ...]) -> None:
            raise VenvIncompatibleError("target venv lacks imports required by the locked-seed code: missing_dep")

        incompatible_manager = AdapterReconciliationManager(
            paths=paths,
            seed_lock_path=lock,
            payload_materializer=lambda _seed, _target, _paths: payload,
            compatibility_check=incompatible,
        )
        try:
            incompatible_manager.run("snapshot", dry_run=True, approved_fingerprint=None)
        except VenvIncompatibleError as exc:
            _check("missing_dep" in str(exc), "venv incompatibility fails closed")
            _check(exc.error_kind == "venv_incompatible", "venv incompatibility has a distinct outcome")
        else:
            _check(False, "venv incompatibility must stop")

        _check(knowledge_base.read_text() == "must not change\n", "knowledge_base remains untouched")

        paths, lock, target, payload = _fixture(root / "venv-incompatible")
        interpreter = target / ".venv" / "bin" / "python3"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(Path(sys.executable))
        _write(
            payload / "plugins" / "alpha" / "src" / "alpha" / "adapter.py",
            "import os, snapshot_missing_dependency\n",
        )
        default_probe_manager = AdapterReconciliationManager(
            paths=paths,
            seed_lock_path=lock,
            payload_materializer=lambda _seed, _target, _paths: payload,
        )
        target_before_incompatible_probe = {
            path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()
        }
        try:
            default_probe_manager.run("snapshot", dry_run=True, approved_fingerprint=None)
        except VenvIncompatibleError as exc:
            _check("snapshot_missing_dependency" in str(exc), "real venv compatibility fails closed")
            _check(exc.error_kind == "venv_incompatible", "real incompatibility is machine-readable")
            _check(
                {
                    path.relative_to(target): path.read_bytes()
                    for path in target.rglob("*")
                    if path.is_file()
                }
                == target_before_incompatible_probe,
                "incompatible venv refusal leaves target bytes unchanged",
            )
        else:
            _check(False, "real venv incompatibility must stop")


if __name__ == "__main__":
    _run()
    print(f"adapter_reconciliation_smoke: {_CHECKS} checks passed")
