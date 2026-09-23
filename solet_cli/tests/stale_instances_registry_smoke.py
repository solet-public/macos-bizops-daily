"""Focused regression coverage for stale manager registry entries."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager import create_execution  # noqa: E402
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.errors import StateConflictError  # noqa: E402
from solet_manager.models import InstanceRecord  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _raises(callback: object, label: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except StateConflictError:
        _check(True, label)
    else:
        _check(False, label)


def _record(name: str, target: Path) -> InstanceRecord:
    return InstanceRecord(
        name=name,
        target=str(target),
        launcher=str(target / "client" / "bin" / name),
        seed_repository="https://example.invalid/seed.git",
        seed_tag="release-fixture",
        seed_commit="a" * 40,
        seed_tree_hash="b" * 40,
        profile="fixture",
        flow_id="fixture.flow",
        flow_source_revision="c" * 40,
        flow_contract_digest="sha256:" + "d" * 64,
        created_at="2026-09-15T00:00:00Z",
        updated_at="2026-09-15T00:00:00Z",
    )


def _seed() -> SeedLock:
    return SeedLock(
        "https://example.invalid/seed.git",
        "release-fixture",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "fixture",
    )


def _bundle() -> SimpleNamespace:
    return SimpleNamespace(
        flow_id="fixture.flow",
        source_revision="c" * 40,
        contract_digest="sha256:" + "d" * 64,
        stages={},
    )


def _create(paths: ManagerPaths, registry: InstanceRegistry, config: CreateConfig) -> Transaction:
    with (
        patch.object(create_execution, "load_seed_lock", return_value=_seed()),
        patch.object(create_execution.ContractBundle, "load", return_value=_bundle()),
        patch.object(create_execution, "build_setup_plan", return_value=SimpleNamespace(answers={})),
        patch.object(create_execution, "static_decision_selections", return_value={}),
        patch.object(create_execution, "resolved_completion_probe_ids", return_value=()),
        patch.object(create_execution, "initial_stage_probe_statuses", return_value={}),
        patch.object(create_execution, "initial_probe_activations", return_value={}),
    ):
        return create_execution._load_or_create_transaction(
            paths=paths,
            contract_directory=None,
            seed_lock_path=paths.config_dir / "seed.lock.json",
            registry=registry,
            config=config,
            approved_fingerprint="sha256:" + "e" * 64,
            selections={},
            decision_source="flag",
            sources={},
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        home = root / "home"
        paths = ManagerPaths.resolve(environ={}, home=home, explicit_home=root / "manager")
        config = CreateConfig(
            name="fixture", target=home / "Solets" / "fixture", autostart=True
        )
        registry = InstanceRegistry(paths.registry_path)
        orphan = _record(config.name, config.target)
        registry.add(orphan)
        created = _create(paths, registry, config)
        reconciled = registry.get(config.name)
        _check(
            reconciled is not None
            and reconciled != orphan
            and reconciled.input_fingerprint == created.input_fingerprint,
            "apply replaces an orphaned entry with its new provisional record",
        )
        _check(
            create_execution.load_transaction(paths.transaction_path(config.name)) == created,
            "orphan reconciliation persists the matching create transaction",
        )
        conflicting = CreateConfig(
            name=config.name, target=home / "Solets" / "different", autostart=True
        )
        _raises(
            lambda: _create(paths, registry, conflicting),
            "a live same-name transaction with different create identity refuses loudly",
        )
        _check(
            registry.get(config.name) == reconciled,
            "live transaction conflict leaves the registered record intact",
        )
    print(f"stale_instances_registry_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
