"""v1 add rejects collisions while accepting an exact existing record."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

from solet_manager.errors import RegistryUniquenessError, StateConflictError  # noqa: E402
from solet_manager.models import InstanceRecord  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import (  # noqa: E402
    InstanceRegistry,
    _v1_identity_keys,
    build_combined_registry_snapshot,
    require_unique_identity,
)


def _record(name: str, target: str, launcher: str) -> InstanceRecord:
    return InstanceRecord(name, target, launcher, "repo", None, "a" * 40, "b" * 40, "profile", "flow", "rev", "sha256:" + "a" * 64, "2026-09-18T00:00:00Z", "2026-09-18T00:00:00Z")


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
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
        first = _record("alpha", str(target), str(root / "bin" / "alpha"))
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
