"""v1 add rejects collisions while accepting an exact existing record."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

from import_enrollment_rerun_smoke import _inspection  # noqa: E402
from solet_manager.errors import RegistryUniquenessError, StateConflictError  # noqa: E402
from solet_manager.existing_install_inspection import ExistingInstallInspectionRequest  # noqa: E402
from solet_manager.import_enrollment import ImportPreview, ImportRequest, _inventory_record  # noqa: E402
from solet_manager.models import InstanceRecord, ManagementOrigin  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import (  # noqa: E402
    InstanceRegistry,
    _v1_identity_keys,
    build_combined_registry_snapshot,
    require_unique_identity,
)


def _record(name: str, target: str, launcher: str) -> InstanceRecord:
    return InstanceRecord(name, target, launcher, "repo", None, "a" * 40, "b" * 40, "profile", "flow", "rev", "sha256:" + "a" * 64, "2026-09-18T00:00:00Z", "2026-09-18T00:00:00Z")


def _assert_create_alias(root: Path, paths: ManagerPaths, legacy: InstanceRecord) -> None:
    target = Path(legacy.target)
    inspection = _inspection(ExistingInstallInspectionRequest(target, "stable", paths))
    preview = ImportPreview(ImportRequest(legacy.name, target, "stable", paths), "fp", {}, "ins_fixture", "opr_fixture", "key", inspection)
    record = _inventory_record(preview, "sha256:" + "1" * 64, "2026-09-24T00:00:00Z")
    record = replace(record, management_origin=ManagementOrigin.CREATE)
    for candidate in (record, replace(record, source_release=replace(record.source_release, commit="c" * 40), observed_provenance=replace(record.observed_provenance, seed_id="updated"))):
        snapshot = build_combined_registry_snapshot((legacy,), (candidate,))
        assert snapshot.indexes["name"][legacy.name] == legacy.name
    launcher = root / (legacy.name + "-alias-" + target.name)
    launcher.symlink_to(record.service_identity.named_launcher_path)
    linked = replace(record, service_identity=replace(record.service_identity, named_launcher_path=str(launcher)))
    build_combined_registry_snapshot((legacy,), (linked,))
    missing = replace(legacy, target=str(root / "missing"))
    try:
        build_combined_registry_snapshot((missing,), (record,))
    except RegistryUniquenessError:
        pass
    else:
        raise AssertionError("unknown filesystem identity accepted")
    mutations = (
        replace(record, management_origin=ManagementOrigin.IMPORT),
        replace(record, name="other"),
        replace(record, target=replace(record.target, canonical_path=str(root / "other"))),
        replace(record, target=replace(record.target, filesystem_identity=replace(record.target.filesystem_identity, inode=0))),
        replace(record, service_identity=replace(record.service_identity, launchagent_label="other")),
        replace(record, service_identity=replace(record.service_identity, named_launcher_path="/other")),
    )
    for candidate in mutations:
        try:
            build_combined_registry_snapshot((legacy,), (candidate,))
        except RegistryUniquenessError:
            pass
        else:
            raise AssertionError("partial/imported alias accepted")
    try:
        build_combined_registry_snapshot((legacy,), (record, record))
    except RegistryUniquenessError:
        pass
    else:
        raise AssertionError("duplicate v2 alias accepted")


def _assert_real_launcher_projection(root: Path) -> None:
    home = root / "home"
    public = home / ".local" / "bin"
    public.mkdir(parents=True)
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    target = root / "real-created"
    legacy_launcher = target / "client" / "bin" / "alpha"
    legacy_launcher.parent.mkdir(parents=True)
    legacy_launcher.write_text("legacy client")
    bridge = target / ".venv" / "bin" / "solet-bridge"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("bridge")
    (public / "alpha").symlink_to(bridge)
    legacy = _record("alpha", str(target), str(legacy_launcher))
    with patch.object(Path, "home", return_value=home):
        assert legacy_launcher.resolve() != (public / "alpha").resolve()
        _assert_create_alias(root, paths, legacy)
        _assert_cross_instance_launcher_collision(root, paths, legacy, public, bridge)


def _assert_cross_instance_launcher_collision(
    root: Path, paths: ManagerPaths, legacy: InstanceRecord, public: Path, bridge: Path,
) -> None:
    target = root / "different-instance"
    target.mkdir()
    (public / "beta").symlink_to(bridge)
    inspection = _inspection(ExistingInstallInspectionRequest(target, "stable", paths))
    preview = ImportPreview(ImportRequest("beta", target, "stable", paths), "fp", {}, "ins_beta", "opr_beta", "key", inspection)
    candidate = _inventory_record(preview, "sha256:" + "1" * 64, "2026-09-24T00:00:00Z")
    try:
        build_combined_registry_snapshot((legacy,), (candidate,))
    except RegistryUniquenessError as error:
        assert "duplicate persisted named_launcher_path" in str(error)
    else:
        raise AssertionError("cross-instance launcher collision accepted")


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        _assert_real_launcher_projection(root)
        paths = ManagerPaths(root / "config", root / "state", root / "cache")
        for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
            directory.mkdir(mode=0o700)
        registry = InstanceRegistry(
            paths.registry_path,
            maintenance_inventory_path=paths.maintenance_inventory_path,
            registry_lock_path=paths.registry_lock_path,
        )
        target = root / "alpha"
        target.mkdir()
        first = _record("alpha", str(target), str(target / "client" / "bin" / "alpha"))
        registry.add(first)
        assert paths.registry_lock_path.is_file()
        bytes_after_create = paths.registry_path.read_bytes()
        registry.add(first)
        assert paths.registry_path.read_bytes() == bytes_after_create
        try:
            registry.add(_record("alpha", "/tmp/other", "/tmp/bin/other"))
        except StateConflictError:
            pass
        else:
            raise AssertionError("duplicate name accepted")

        _assert_create_alias(root, paths, first)
        v1_keys = _v1_identity_keys(first)
        baseline = build_combined_registry_snapshot((first,), ())
        for key_name in ("name", "target", "filesystem_identity", "launchagent_label", "named_launcher_path"):
            requested = {
                "name": "other",
                "target": "/tmp/other",
                "filesystem_identity": (999, 998),
                "launchagent_label": "local.solet.other",
                "named_launcher_path": "/tmp/bin/other",
            }
            requested[key_name] = getattr(v1_keys, key_name)
            try:
                require_unique_identity(baseline.indexes, requested=requested)
            except RegistryUniquenessError as error:
                assert error.key_name == key_name
            else:
                raise AssertionError(f"v1 {key_name} collision was accepted")
    print("instance_registry_uniqueness_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
