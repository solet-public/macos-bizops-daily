#!/usr/bin/env python3
"""Focused regression smoke for metadata-registry truthfulness (regs 229-235)."""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.platform.platform_services_manager import PlatformServicesManager  # noqa: E402
from ananta.platform.plugin_metadata_manager import PluginMetadataManager  # noqa: E402
from ananta.platform.unified_metadata_registry import MetadataLayer, UnifiedMetadataRegistry  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _captured_logs(logger_name: str) -> Generator[_LogCapture]:
    target = logging.getLogger(logger_name)
    capture = _LogCapture()
    target.addHandler(capture)
    try:
        yield capture
    finally:
        target.removeHandler(capture)


@contextmanager
def _environment(updates: dict[str, str]) -> Generator[None]:
    original = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_plugin(plugins_path: Path, plugin_id: str, schema: str) -> None:
    metadata_path = plugins_path / plugin_id / "metadata"
    metadata_path.mkdir(parents=True)
    (metadata_path / "schema.json").write_text(schema, encoding="utf-8")


def _fixture_registry(root: Path, plugins_path: Path) -> UnifiedMetadataRegistry:
    platform_path = root / "platform"
    (platform_path / "schemas").mkdir(parents=True)
    app_path = root / "app"
    (app_path / "actions").mkdir(parents=True)
    return UnifiedMetadataRegistry(str(platform_path), str(plugins_path), str(app_path))


def _case_missing_dependency_invalidates_registry_and_absent_version_stays_absent() -> None:
    print("\nCase 1: missing plugin dependencies are registry errors, not unread warnings")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plugins_path = root / "plugins"
        plugins_path.mkdir()
        _write_plugin(
            plugins_path,
            "requires_missing",
            '{"plugin_id": "requires_missing", "dependencies": {"requires": ["absent_plugin"]}}',
        )
        registry = _fixture_registry(root, plugins_path)
        _check(registry.initialize(), "fixture registry initializes with the plugin layer present")
        validation = registry.validate_complete_system()
        dependency_issues = cast(list[object], validation["dependency_issues"])
        _check(validation["valid"] is False, "missing dependency makes registry validation invalid")
        _check(
            any("absent_plugin" in str(issue) for issue in dependency_issues),
            "missing dependency is preserved on the consumed dependency-issues surface",
        )
        plugin_manager = registry.get_layer_manager(MetadataLayer.PLUGIN)
        plugin = plugin_manager.get_plugin("requires_missing") if plugin_manager else None
        _check(plugin is not None and plugin.version is None, "an absent plugin version remains None")


def _case_layer_status_records_initialize_results() -> None:
    print("\nCase 2: layer status reports the initialization result, not object construction")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        missing_plugins = root / "missing_plugins"
        registry = _fixture_registry(root, missing_plugins)
        _check(registry.initialize() is False, "registry initialization reports the missing plugin root")
        validation = registry.validate_complete_system()
        layer_status = cast(dict[str, object], validation["layer_status"])
        plugin_status = cast(dict[str, object], layer_status["plugin"])
        _check(
            plugin_status["initialized"] is False,
            "plugin layer status remains false after its initialize() result is false",
        )


def _case_registry_failure_is_logged_and_exposed_by_platform_status() -> None:
    print("\nCase 3: platform manager logs and exposes a non-fatal registry failure")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        app_home = root / "app_home"
        (app_home / "app" / "actions").mkdir(parents=True)
        invalid_plugins_root = root / "plugins_file"
        invalid_plugins_root.write_text("not a directory", encoding="utf-8")
        with _environment(
            {"APP_HOME": str(app_home), "ANANTA_PLUGINS_PATH": str(invalid_plugins_root)}
        ):
            manager = PlatformServicesManager(
                metadata_folder=str(root / "metadata"), output_folder=str(root / "generated")
            )
            with _captured_logs("ananta.platform.platform_services_manager") as logs:
                _check(manager.initialize(), "registry failure does not hard-fail platform service boot")
            status = manager.get_service_status()
            registry_status_value = status.get("unified_metadata_registry")
            registry_status = (
                cast(dict[str, object], registry_status_value)
                if isinstance(registry_status_value, dict)
                else {}
            )
            _check(
                registry_status.get("initialized") is False,
                "service status exposes the failed unified registry result",
            )
            _check(
                any("layer_results" in record.getMessage() for record in logs.records),
                "registry failure is logged with per-layer results",
            )


def _case_schema_parse_failure_is_logged_with_plugin_identity() -> None:
    print("\nCase 4: malformed plugin metadata is logged as a parse failure, not absent capability")
    with tempfile.TemporaryDirectory() as tmp:
        plugins_path = Path(tmp) / "plugins"
        plugins_path.mkdir()
        _write_plugin(plugins_path, "broken_plugin", "{not valid json")
        manager = PluginMetadataManager(str(plugins_path))
        with _captured_logs("ananta.platform.plugin_metadata_manager") as logs:
            _check(manager.initialize(), "a malformed plugin does not stop discovery of other plugins")
        _check(
            any(
                "broken_plugin" in record.getMessage() and record.exc_info is not None
                for record in logs.records
            ),
            "schema parse failure is logged with the plugin identity and exception",
        )


def main() -> int:
    print("== metadata registry truthfulness smoke ==")
    _case_missing_dependency_invalidates_registry_and_absent_version_stays_absent()
    _case_layer_status_records_initialize_results()
    _case_registry_failure_is_logged_and_exposed_by_platform_status()
    _case_schema_parse_failure_is_logged_with_plugin_identity()
    print(f"\n== passed={_passed} failed={len(_failed)} ==")
    if _failed:
        print("FAIL labels:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
