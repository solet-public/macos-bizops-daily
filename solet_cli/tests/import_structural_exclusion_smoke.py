"""Import has no target mutation edge, statically or at runtime."""

from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

import solet_manager.import_enrollment as enrollment  # noqa: E402
from existing_install_inspection_call_boundary import called_symbols  # noqa: E402
from import_enrollment_rerun_smoke import _inspection  # noqa: E402
from solet_manager.import_enrollment import ImportRequest  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.transaction import read_maintenance_operation  # noqa: E402


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted((*directories, *names)):
            path = current_path / name
            relative = path.relative_to(root).as_posix().encode()
            info = path.lstat()
            digest.update(relative)
            digest.update(str(info.st_mode).encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                digest.update(path.read_bytes())
    return digest.hexdigest()


def _forbidden(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("import reached a forbidden target-capable operation")


def _assert_static_exclusion() -> None:
    path = _ROOT / "solet_cli" / "src" / "solet_manager" / "import_enrollment.py"
    calls = called_symbols(ast.parse(path.read_text(encoding="utf-8")))
    forbidden = {
        "AdapterRegistry",
        "StateManagementInterface",
        "atomic_remove",
        "atomic_replace_bytes",
        "launchctl",
        "materialize_locked_seed",
        "run_operations",
        "target_install_state_projection",
        "write_transaction",
    }
    assert not {item.rsplit(".", 1)[-1] for item in calls} & forbidden


def _make_fixture(root: Path) -> tuple[Path, ManagerPaths, ImportRequest]:
    target = root / "existing"
    (target / ".git").mkdir(parents=True)
    (target / ".git" / "index").write_bytes(b"operator git index")
    (target / "operator-owned.txt").write_bytes(b"operator content")
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
        directory.mkdir(mode=0o700)
    return target, paths, ImportRequest("fixture", target, "stable", paths)


def _enroll_with_forbidden_calls(request: ImportRequest, target: Path, before: str):
    with (
        patch("solet_manager.adapters.AdapterRegistry", _forbidden),
        patch("solet_manager.create_execution.materialize_locked_seed", _forbidden),
        patch("solet_manager.source_acquisition.materialize_locked_seed", _forbidden),
        patch("solet_manager.transaction.write_transaction", _forbidden),
        patch.object(subprocess, "run", _forbidden),
    ):
        preview = enrollment.preview_import(request)
        assert _tree_digest(target) == before
        result = enrollment.enroll_import(request, preview.fingerprint)
        assert result.status == "imported"
        assert _tree_digest(target) == before
        assert enrollment.enroll_import(request, preview.fingerprint).status == "already_managed"
        assert _tree_digest(target) == before
    return result


def _assert_preservation(paths: ManagerPaths, result: object) -> None:
    journal = read_maintenance_operation(paths.operation_path(result.preview.instance_id, result.preview.operation_id))
    preservation = journal["preservation_inventory"]
    for key in (
        "target_byte_writes", "secret_value_reads", "secret_value_writes", "database_reads",
        "database_writes", "target_process_executions", "permission_prompts",
    ):
        assert preservation[key] == 0


def main() -> int:
    _assert_static_exclusion()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        target, paths, request = _make_fixture(root)
        before = _tree_digest(target)
        original_inspection = enrollment.inspect_existing_install
        enrollment.inspect_existing_install = lambda request, metadata_loader: _inspection(request)
        try:
            result = _enroll_with_forbidden_calls(request, target, before)
            _assert_preservation(paths, result)
        finally:
            enrollment.inspect_existing_install = original_inspection
    print("import_structural_exclusion_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
