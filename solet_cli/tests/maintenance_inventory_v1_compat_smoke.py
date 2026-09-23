"""v1 create-registry bytes remain lossless through the combined reader."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

from solet_manager.maintenance_inventory import read_combined_inventory  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths = ManagerPaths(root / "config", root / "state", root / "cache")
        paths.config_dir.mkdir()
        raw = b'{"schema_version":1,"instances":{}}\n'
        paths.registry_path.write_bytes(raw)
        assert read_combined_inventory(paths) == ()
        assert paths.registry_path.read_bytes() == raw
    print("maintenance_inventory_v1_compat_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
