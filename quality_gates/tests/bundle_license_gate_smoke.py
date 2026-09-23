#!/usr/bin/env python3
"""Hermetic red-first controls for the bundle-license gate."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from quality_gates import bundle_license_gate as gate  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    """Count and assert one smoke condition."""

    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _write_undeclared_license_fixture(root: Path) -> tuple[Path, Path]:
    """Create one bundle member whose project metadata omits ``license``."""

    bundles_path = root / "capability_bundles.yaml"
    bundles_path.write_text(
        "bundles:\n"
        "  fixture_bundle:\n"
        "    plugins:\n"
        "      - missing_license_plugin\n",
        encoding="utf-8",
    )
    plugins_root = root / "plugins"
    plugin_root = plugins_root / "missing_license_plugin"
    plugin_root.mkdir(parents=True)
    (plugin_root / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'missing-license-plugin'\n"
        "version = '0.0.0'\n",
        encoding="utf-8",
    )
    return bundles_path, plugins_root


def _check_undeclared_license_blocks() -> None:
    """The named red mutation is removing this fixture's Apache-2.0 license."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        bundles_path, plugins_root = _write_undeclared_license_fixture(root)
        with (
            patch.object(gate, "BUNDLES_PATH", bundles_path),
            patch.object(gate, "PLUGINS_ROOT", plugins_root),
        ):
            findings = gate.collect_findings()
            exit_code = gate.main([])
            allowlist_path = root / "allowlist.txt"
            allowlist_path.write_text(
                "B0 fixture_bundle::missing_license_plugin"
                "  # owner: smoke-fixture reason: tracked test debt expires: 2099-01-01\n",
                encoding="utf-8",
            )
            allowlisted_exit_code = gate.main(["--allowlist", str(allowlist_path)])

    expected = gate.Finding(
        gate.CHECK_UNDECLARED,
        "fixture_bundle",
        "missing_license_plugin",
        "ships in this bundle but declares no license",
    )
    _check(findings == [expected], "fixture's undeclared license is the sole finding")
    _check(exit_code == 1, "a seeded undeclared license blocks the gate")
    _check(
        allowlisted_exit_code == 0,
        "the exact tracked-debt key suppresses only the explicit fixture finding",
    )


def main() -> int:
    """Run every independent red-first fixture control."""

    _check_undeclared_license_blocks()
    print(f"bundle_license_gate_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
