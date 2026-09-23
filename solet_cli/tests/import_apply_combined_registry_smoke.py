"""Import apply locks and checks both registry generations before writing."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

import solet_manager.import_enrollment as enrollment  # noqa: E402
from import_enrollment_rerun_smoke import _inspection  # noqa: E402
from solet_manager.errors import ManagedIdentityDriftError, RegistryUniquenessError  # noqa: E402
from solet_manager.import_enrollment import ImportRequest  # noqa: E402
from solet_manager.models import InstanceRecord  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402


def _legacy_record(name: str, target: Path) -> InstanceRecord:
    return InstanceRecord(
        name,
        str(target),
        str(Path.home() / ".local" / "bin" / name),
        "https://example.invalid/seed.git",
        None,
        "a" * 40,
        "b" * 40,
        "profile",
        "flow",
        "revision",
        "sha256:" + "a" * 64,
        "2026-09-18T00:00:00Z",
        "2026-09-18T00:00:00Z",
    )


def _assert_v2_target_collision(paths: ManagerPaths, target: Path) -> None:
    inventory_before = paths.maintenance_inventory_path.read_bytes()
    collision = ImportRequest("other", target, "stable", paths)
    preview = enrollment.preview_import(collision)
    try:
        enrollment.enroll_import(collision, preview.fingerprint)
    except RegistryUniquenessError as error:
        assert error.key_name in {"target", "filesystem_identity"}
    else:
        raise AssertionError("v2 target collision was accepted")
    assert not paths.operation_path(preview.instance_id, preview.operation_id).exists()
    assert paths.maintenance_inventory_path.read_bytes() == inventory_before


def _assert_v1_name_collision(paths: ManagerPaths, target: Path, legacy_target: Path) -> None:
    InstanceRegistry(paths.registry_path).add(_legacy_record("legacy", legacy_target))
    request = ImportRequest("legacy", target, "stable", paths)
    preview = enrollment.preview_import(request)
    try:
        enrollment.enroll_import(request, preview.fingerprint)
    except ManagedIdentityDriftError:
        pass
    else:
        raise AssertionError("v1 name collision was accepted as an import rerun")
    assert not paths.operation_path(preview.instance_id, preview.operation_id).exists()


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / "existing"
        target.mkdir()
        candidate = root / "candidate"
        candidate.mkdir()
        legacy_target = root / "legacy-existing"
        legacy_target.mkdir()
        paths = ManagerPaths(root / "config", root / "state", root / "cache")
        for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
            directory.mkdir(mode=0o700)
        original_inspection = enrollment.inspect_existing_install
        original_lock = enrollment.instance_lock
        acquired: list[Path] = []

        @contextmanager
        def recording_lock(path: Path, *, create: bool = True):
            acquired.append(path)
            with original_lock(path, create=create) as handle:
                yield handle

        enrollment.inspect_existing_install = lambda request, metadata_loader: _inspection(request)
        enrollment.instance_lock = recording_lock
        try:
            request = ImportRequest("fixture", target, "stable", paths)
            preview = enrollment.preview_import(request)
            assert acquired == []
            assert enrollment.enroll_import(request, preview.fingerprint).status == "imported"
            assert acquired[:2] == [paths.lock_path("fixture"), paths.registry_lock_path]
            _assert_v2_target_collision(paths, target)
            _assert_v1_name_collision(paths, candidate, legacy_target)
        finally:
            enrollment.inspect_existing_install = original_inspection
            enrollment.instance_lock = original_lock
    print("import_apply_combined_registry_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
