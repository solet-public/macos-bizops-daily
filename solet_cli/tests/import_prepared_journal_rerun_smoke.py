"""A fresh process resumes a crash immediately after the prepared journal."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

import solet_manager.import_enrollment as enrollment  # noqa: E402
from import_enrollment_rerun_smoke import _inspection  # noqa: E402
from solet_manager.import_enrollment import ImportRequest  # noqa: E402
from solet_manager.maintenance_inventory import read_maintenance_inventory_v2  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.transaction import read_maintenance_operation  # noqa: E402

from solet_setup_contracts import canonical_sha256  # noqa: E402


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / "existing"
        target.mkdir()
        marker = target / "operator-owned.txt"
        marker.write_bytes(b"unchanged")
        paths = ManagerPaths(root / "config", root / "state", root / "cache")
        for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
            directory.mkdir(mode=0o700)
        request = ImportRequest("fixture", target, "stable", paths)
        module = enrollment
        original = module.inspect_existing_install
        module.inspect_existing_install = lambda request, metadata_loader: _inspection(request)
        try:
            preview = module.preview_import(request)
            assert not module._write_prepared_import_journal(
                preview, canonical_sha256(preview.bundle)
            )
            assert not paths.maintenance_inventory_path.exists()
            module = importlib.reload(module)
            module.inspect_existing_install = lambda request, metadata_loader: _inspection(request)
            fresh = module.preview_import(request)
            assert module.enroll_import(request, fresh.fingerprint).status == "already_managed"
            assert len(read_maintenance_inventory_v2(paths.maintenance_inventory_path)) == 1
            assert read_maintenance_operation(
                paths.operation_path(preview.instance_id, preview.operation_id)
            )["status"] == "verified"
            assert marker.read_bytes() == b"unchanged"
        finally:
            module.inspect_existing_install = original
    print("import_prepared_journal_rerun_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
