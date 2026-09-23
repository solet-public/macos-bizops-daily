"""Structural no-target-mutation guard for the Step 3 enrollment root."""

from __future__ import annotations

from pathlib import Path


def main() -> int:
    source = (Path(__file__).parents[1] / "src/solet_manager/import_enrollment.py").read_text()
    forbidden = ("AdapterRegistry", "write_transaction", "target_install_state_projection", "subprocess.run", "launchctl", "materialize_locked_seed")
    for symbol in forbidden:
        assert symbol not in source, f"forbidden target-capable edge: {symbol}"
    print("import_preservation_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
