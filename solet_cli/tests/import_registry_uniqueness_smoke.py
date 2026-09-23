"""Focused closed collision behavior for Step 3 registry identity keys."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

from solet_manager.errors import RegistryUniquenessError  # noqa: E402
from solet_manager.registry import require_unique_identity  # noqa: E402


def main() -> int:
    keys = ("name", "target", "filesystem_identity", "launchagent_label", "named_launcher_path")
    requested = {key: key for key in keys}
    for key in keys:
        indexes = {item: {} for item in keys}
        indexes[key][key] = "ins_existing"
        try:
            require_unique_identity(indexes, requested=requested)
        except RegistryUniquenessError as exc:
            assert exc.key_name == key
        else:
            raise AssertionError(f"{key} collision was accepted")
    print("import_registry_uniqueness_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
