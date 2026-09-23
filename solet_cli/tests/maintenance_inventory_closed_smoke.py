"""Closed v2 inventory rejection smoke."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

from solet_manager.errors import StateError  # noqa: E402
from solet_manager.maintenance_inventory import parse_maintenance_inventory_v2_bytes  # noqa: E402


def main() -> int:
    for payload in (b"{}", b'{"schema_version":2,"records":{},"extra":null}'):
        try:
            parse_maintenance_inventory_v2_bytes(payload)
        except StateError:
            pass
        else:
            raise AssertionError("open or malformed v2 inventory was accepted")
    print("maintenance_inventory_closed_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
