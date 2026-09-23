"""A fresh process completes an import interrupted after inventory publication."""

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
from solet_manager.state_io import atomic_write_json, load_json_object, write_content_addressed_json  # noqa: E402

from solet_setup_contracts import canonical_sha256  # noqa: E402


def _publish_to_boundary(module: object, request: ImportRequest):
    preview = module.preview_import(request)
    bundle_digest = canonical_sha256(preview.bundle)
    assert not module._write_prepared_import_journal(preview, bundle_digest)
    paths = request.manager_paths
    write_content_addressed_json(paths.contract_cache_path(bundle_digest), preview.bundle, bundle_digest)
    module._advance_journal(preview, "inspection_bundle_cached", "bundle_cached", "bundle_cached")
    module._publish_inventory(preview, bundle_digest)
    module._advance_journal(preview, "inventory_published", "inventory_published", "inventory_published")
    return preview, bundle_digest


def _assert_cross_link_rejected(module: object, request: ImportRequest, preview: object) -> None:
    paths = request.manager_paths
    journal_path = paths.operation_path(preview.instance_id, preview.operation_id)
    journal_bytes = journal_path.read_bytes()
    inventory_bytes = paths.maintenance_inventory_path.read_bytes()
    corrupt_journal = load_json_object(journal_path)
    assert corrupt_journal is not None
    corrupt_journal["current_identity"]["inspection_bundle_digest"] = "sha256:" + "0" * 64
    atomic_write_json(journal_path, corrupt_journal)
    _assert_enrollment_rejected(module, request, preview, "corrupt journal-to-bundle link")
    journal_path.write_bytes(journal_bytes)
    corrupt_inventory = load_json_object(paths.maintenance_inventory_path)
    assert corrupt_inventory is not None
    corrupt_inventory["records"][0]["inspection_bundle_digest"] = "sha256:" + "0" * 64
    atomic_write_json(paths.maintenance_inventory_path, corrupt_inventory)
    _assert_enrollment_rejected(module, request, preview, "corrupt inventory-to-bundle link")
    paths.maintenance_inventory_path.write_bytes(inventory_bytes)


def _assert_enrollment_rejected(module: object, request: ImportRequest, preview: object, label: str) -> None:
    try:
        module.enroll_import(request, preview.fingerprint)
    except Exception:
        return
    raise AssertionError(f"{label} was accepted")


def _assert_recovered(module: object, request: ImportRequest, preview: object, marker: Path) -> None:
    fresh = importlib.reload(module)
    fresh.inspect_existing_install = lambda request, metadata_loader: _inspection(request)
    fresh_preview = fresh.preview_import(request)
    assert fresh.enroll_import(request, fresh_preview.fingerprint).status == "already_managed"
    record = read_maintenance_inventory_v2(request.manager_paths.maintenance_inventory_path)[0]
    assert record.active_operation is None
    assert record.last_verified_operation_id == preview.operation_id
    journal = load_json_object(request.manager_paths.operation_path(preview.instance_id, preview.operation_id))
    assert journal is not None and journal["status"] == "verified"
    assert marker.read_bytes() == b"unchanged"


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
            preview, bundle_digest = _publish_to_boundary(module, request)
            published = read_maintenance_inventory_v2(paths.maintenance_inventory_path)
            assert len(published) == 1
            assert published[0].active_operation is not None
            prepared = load_json_object(paths.operation_path(preview.instance_id, preview.operation_id))
            assert prepared is not None
            assert prepared["status"] == "inventory_published"
            assert prepared["current_identity"]["inspection_bundle_digest"] == bundle_digest
            assert prepared["stage_statuses"]["inspection_revalidated"] == "verified"
            assert prepared["stage_statuses"]["inventory_published"] == "verified"
            _assert_cross_link_rejected(module, request, preview)
            _assert_recovered(module, request, preview, marker)
        finally:
            module.inspect_existing_install = original
    print("import_inventory_published_recovery_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
