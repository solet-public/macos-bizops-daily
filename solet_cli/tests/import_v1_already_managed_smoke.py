"""Exact create-origin matching remains v1-byte-compatible and fail-closed."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

import solet_manager.import_enrollment as enrollment  # noqa: E402
from import_enrollment_rerun_smoke import _inspection  # noqa: E402
from solet_manager.errors import ManagedIdentityDriftError  # noqa: E402
from solet_manager.import_enrollment import ImportRequest  # noqa: E402
from solet_manager.models import InstanceRecord  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402


def _record(target: Path, *, commit: str = "a" * 40) -> InstanceRecord:
    return InstanceRecord(
        "fixture",
        str(target),
        str(Path.home() / ".local" / "bin" / "fixture"),
        "https://example.invalid/seed.git",
        None,
        commit,
        "b" * 40,
        "profile",
        "flow",
        "revision",
        "sha256:" + "a" * 64,
        "2026-09-18T00:00:00Z",
        "2026-09-18T00:00:00Z",
    )


def _assert_no_import_state(paths: ManagerPaths) -> None:
    assert not paths.maintenance_inventory_path.exists()
    assert not paths.operations_dir.exists()
    assert not (paths.cache_dir / "contracts").exists()


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / "existing"
        target.mkdir()
        paths = ManagerPaths(root / "config", root / "state", root / "cache")
        for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
            directory.mkdir(mode=0o700)
        registry = InstanceRegistry(paths.registry_path)
        registry.add(_record(target))
        v1_before = paths.registry_path.read_bytes()
        request = ImportRequest("fixture", target, "stable", paths)
        original = enrollment.inspect_existing_install
        enrollment.inspect_existing_install = lambda request, metadata_loader: _inspection(request)
        try:
            preview = enrollment.preview_import(request)
            result = enrollment.enroll_import(request, preview.fingerprint)
            assert result.status == "already_managed"
            rendered = result.to_command_result().data
            assert rendered["management_origin"] == "create"
            assert rendered["instance_id"] == "fixture"
            assert paths.registry_path.read_bytes() == v1_before
            _assert_no_import_state(paths)
        finally:
            enrollment.inspect_existing_install = original

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / "existing"
        target.mkdir()
        paths = ManagerPaths(root / "config", root / "state", root / "cache")
        for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
            directory.mkdir(mode=0o700)
        InstanceRegistry(paths.registry_path).add(_record(target, commit="c" * 40))
        request = ImportRequest("fixture", target, "stable", paths)
        original = enrollment.inspect_existing_install
        enrollment.inspect_existing_install = lambda request, metadata_loader: _inspection(request)
        try:
            preview = enrollment.preview_import(request)
            try:
                enrollment.enroll_import(request, preview.fingerprint)
            except ManagedIdentityDriftError:
                pass
            else:
                raise AssertionError("partial create-origin seed identity was accepted")
            _assert_no_import_state(paths)
        finally:
            enrollment.inspect_existing_install = original
    print("import_v1_already_managed_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
