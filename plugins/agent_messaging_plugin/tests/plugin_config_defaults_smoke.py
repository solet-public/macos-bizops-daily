#!/usr/bin/env python3
"""Plugin config defaults unification v1 smoke (no pytest; sandboxed).

Verifies the v1 mechanism shipped 2026-05-30 per
``workbench/2026-05-30_plugin_config_defaults_unification.md`` §8.3:

  1. ``load_plugin_yaml_defaults`` extracts ``{field: default}`` from a
     fixture ``plugin.yaml``'s ``config:`` block.
  2. Fields with ``secret: true`` are skipped (vault flow, not config-
     default flow).
  3. Fields without a ``default:`` key are skipped (caller's concern).
  4. Loader on a non-existent ``plugin_root`` returns empty dict (no
     plugin.yaml = no defaults = harmless empty merge layer).
  5. ``ConfigManager.get_plugin_config`` merges yaml defaults under
     override-file content: override wins for overlapping keys; yaml
     fills keys absent from the override file.
  6. With both empty, merged result is empty.
  7. With override only (no yaml), merged result equals override.

All assertions run against a /tmp sandbox; the live ``profile/`` tree
is never touched. Per
``MEMORY.md/feedback_sandbox_mutating_smokes.md``.

Operator-driven integration smoke (NOT in this script — requires a running
solet):

  - Edit ``plugins/agent_messaging_plugin/plugin.yaml``'s
    ``process_export_deny_patterns`` field to include a new sentinel
    pattern.
  - Restart the solet via ``apply_manifest`` (blue-green) or cold-start via
    ``python -m ananta.cli`` if blue-green is unavailable.
  - Verify the new pattern is in the effective deny list (e.g. via
    ``process_call`` on a process the new pattern denies, expecting a
    403 / rejected response).
  - The yaml edit alone must change runtime behavior — zero override-
    file changes between the edit and the restart.

Run:

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/plugin_config_defaults_smoke.py

Exits 0 on success, 1 on first failure with a labeled message.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.config.config_manager import ConfigManager  # noqa: E402
from ananta.core.config.plugin_yaml_loader import (  # noqa: E402
    load_plugin_yaml_defaults,
)


def _fail(label: str, detail: str) -> None:
    print(f"FAIL: {label}: {detail}", file=sys.stderr)
    sys.exit(1)


def _ok(label: str) -> None:
    print(f"  OK: {label}")


def _build_fixture_plugin_yaml(plugin_root: Path) -> None:
    """Author a plugin.yaml with one field of every relevant shape."""
    plugin_root.mkdir(parents=True, exist_ok=True)
    (plugin_root / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "fixture_plugin",
                "description": "Smoke fixture",
                "config": {
                    "bridge_idle_timeout_seconds": {
                        "type": "integer",
                        "default": 3600,
                        "description": "default-bearing int",
                    },
                    "enabled": {
                        "type": "boolean",
                        "default": True,
                        "description": "default-bearing bool",
                    },
                    "host": {
                        "type": "string",
                        "default": "127.0.0.1",
                        "description": "default-bearing string",
                    },
                    "allowed_backends": {
                        "type": "list",
                        "default": ["codex", "claude_code"],
                        "description": "default-bearing list",
                    },
                    "api_key": {
                        "type": "string",
                        "required": True,
                        "secret": True,
                        "description": "secret field — must be skipped",
                    },
                    "missing_default_field": {
                        "type": "integer",
                        "required": True,
                        "description": "no default — must be skipped",
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def case_1_loader_extracts_defaults(plugin_root: Path) -> None:
    defaults = load_plugin_yaml_defaults(plugin_root)
    expected = {
        "bridge_idle_timeout_seconds": 3600,
        "enabled": True,
        "host": "127.0.0.1",
        "allowed_backends": ["codex", "claude_code"],
    }
    if defaults != expected:
        _fail(
            "case 1 (extract typed defaults)",
            f"expected {expected!r}, got {defaults!r}",
        )
    _ok("case 1 — loader extracts typed defaults, preserves int/bool/str/list shapes")


def case_2_secret_skipped(plugin_root: Path) -> None:
    defaults = load_plugin_yaml_defaults(plugin_root)
    if "api_key" in defaults:
        _fail("case 2 (secret skip)", "api_key (secret) appeared in loader output")
    _ok("case 2 — secret: true field skipped (vault flow, not config-default flow)")


def case_3_no_default_skipped(plugin_root: Path) -> None:
    defaults = load_plugin_yaml_defaults(plugin_root)
    if "missing_default_field" in defaults:
        _fail(
            "case 3 (no-default skip)",
            "missing_default_field (no default:) appeared in loader output",
        )
    _ok("case 3 — field without default: skipped (caller's concern)")


def case_4_loader_handles_missing_yaml(sandbox: Path) -> None:
    empty_root = sandbox / "absent_plugin"
    empty_root.mkdir()
    defaults = load_plugin_yaml_defaults(empty_root)
    if defaults != {}:
        _fail("case 4 (missing yaml)", f"expected {{}}, got {defaults!r}")
    defaults_none = load_plugin_yaml_defaults(None)
    if defaults_none != {}:
        _fail("case 4 (None plugin_root)", f"expected {{}}, got {defaults_none!r}")
    _ok("case 4 — absent plugin.yaml / None root returns {} (no-op merge layer)")


def case_5_manager_merges_yaml_under_override(sandbox: Path) -> None:
    app_home = sandbox / "app_home_5"
    plugins_dir = app_home / "config" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "fixture_plugin.json").write_text(
        json.dumps({"host": "10.0.0.1"}),
        encoding="utf-8",
    )
    mgr = ConfigManager(str(app_home))
    yaml_defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "bridge_idle_timeout_seconds": 3600,
        "enabled": True,
    }
    merged = mgr.get_plugin_config("fixture_plugin", default_config=yaml_defaults)
    if merged.get("host") != "10.0.0.1":
        _fail(
            "case 5 (override wins)",
            f"expected host='10.0.0.1' (override), got {merged.get('host')!r}",
        )
    if merged.get("bridge_idle_timeout_seconds") != 3600:
        _fail(
            "case 5 (yaml fills missing)",
            f"expected bridge_idle_timeout_seconds=3600 (yaml default), "
            f"got {merged.get('bridge_idle_timeout_seconds')!r}",
        )
    if merged.get("enabled") is not True:
        _fail(
            "case 5 (yaml fills missing)",
            f"expected enabled=True (yaml default), got {merged.get('enabled')!r}",
        )
    _ok("case 5 — override-file value wins; yaml fills keys absent from override")


def case_6_both_empty_returns_empty(sandbox: Path) -> None:
    app_home = sandbox / "app_home_6"
    (app_home / "config" / "plugins").mkdir(parents=True)
    mgr = ConfigManager(str(app_home))
    merged = mgr.get_plugin_config("fixture_plugin", default_config={})
    if merged != {}:
        _fail("case 6 (both empty)", f"expected {{}}, got {merged!r}")
    _ok("case 6 — both layers empty → merged dict empty")


def case_7_override_only(sandbox: Path) -> None:
    app_home = sandbox / "app_home_7"
    plugins_dir = app_home / "config" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "fixture_plugin.json").write_text(
        json.dumps({"some_key": "some_value"}),
        encoding="utf-8",
    )
    mgr = ConfigManager(str(app_home))
    merged = mgr.get_plugin_config("fixture_plugin", default_config=None)
    if merged.get("some_key") != "some_value":
        _fail(
            "case 7 (override only)",
            f"expected some_key='some_value' (override), got {merged.get('some_key')!r}",
        )
    _ok("case 7 — override-only path: merged equals override content")


def main() -> int:
    print("=== Plugin config defaults unification v1 smoke ===")
    with tempfile.TemporaryDirectory(prefix="d4_yaml_smoke_") as tmp:
        sandbox = Path(tmp)
        fixture_root = sandbox / "fixture_plugin"
        _build_fixture_plugin_yaml(fixture_root)

        case_1_loader_extracts_defaults(fixture_root)
        case_2_secret_skipped(fixture_root)
        case_3_no_default_skipped(fixture_root)
        case_4_loader_handles_missing_yaml(sandbox)
        case_5_manager_merges_yaml_under_override(sandbox)
        case_6_both_empty_returns_empty(sandbox)
        case_7_override_only(sandbox)

    print("=== ALL SMOKE CASES PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
