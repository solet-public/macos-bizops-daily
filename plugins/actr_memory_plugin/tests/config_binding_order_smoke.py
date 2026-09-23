#!/usr/bin/env python3
"""Regression smoke for the 2026-08-06 config-binding-order fix.

Root cause (measured at source, ``ananta/src/ananta/core/orchestration/
startup_sequence.py``'s ``STARTUP_SEQUENCE``): the platform's real plugin
boot order runs ``prepare_for_readiness()`` (inside ``_start_service_plugins``)
BEFORE ``initialize()`` (inside ``_initialize_plugin_configs``, which depends
on ``create_service_wrappers``, itself downstream of
``_start_service_plugins``). ``ACTRMemoryPlugin.prepare_for_readiness`` used
to bind ``ConfigProvider(self.name, self._operator_config)`` from
``self._operator_config``, which was ONLY ever populated by ``initialize()``
— so on every real boot, ``prepare_for_readiness`` bound ``{}`` before
``initialize()`` ever ran, and ``initialize()``'s later capture reached an
already-constructed (and, pre-fix, never-reconsulted) provider.
``export_allowed_roots`` and the cron-override config keys never took
effect on any boot, with any config file.

This smoke drives the plugin in the PLATFORM'S REAL ORDER — construct,
``prepare_for_readiness()``, THEN ``initialize()`` — against a REAL
``ConfigManager`` (not a stub) pointed at a real config file on disk, and
asserts the containment path (``_resolve_export_allowed_roots``) sees the
configured roots immediately after ``prepare_for_readiness()`` returns,
before ``initialize()`` ever runs. Red-fails on the pre-fix code (which
would see ``{}`` / an empty roots list at that point).

Run::

    .venv/bin/python3 plugins/actr_memory_plugin/tests/config_binding_order_smoke.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))

import ananta.core.config.config_manager as _config_manager_module  # noqa: E402
from actr_memory_plugin.constants import PLUGIN_NAME  # noqa: E402
from actr_memory_plugin.plugin import ACTRMemoryPlugin  # noqa: E402
from ananta.core.config.config_manager import (  # noqa: E402
    ConfigManager,
    set_config_instance,
)
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
)

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


class _FakeFocusBufferState:
    """Satisfies exactly what prepare_for_readiness's backend construction
    needs: an empty focus_buffer table, so assert_no_unscoped_focus_rows()
    passes trivially. Nothing else is queried during construction."""

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace, query
        return {"action_status": "completed", "data": {"records": []}}


class _FakeOrchestrator:
    """Minimal orchestrator_ref — enough for prepare_for_readiness to reach
    the config-binding line and complete the backend construction that
    reads it, without any real vector/embedding/inference/scheduling
    service. None is a legal return for all four (the plugin logs and
    continues; only state_service is required)."""

    def __init__(self, app_home: str) -> None:
        self.APP_HOME = app_home
        self._state_service = _FakeFocusBufferState()
        self.config: Any = None
        self.plugin_manager: Any = None

    def get_service(self, name: str) -> Any:
        if name == "state_service":
            return self._state_service
        return None


class _FakePluginManager:
    """The minimal lifecycle-service plugin manager surface."""

    def __init__(self, plugin: ACTRMemoryPlugin) -> None:
        self.plugins = {PLUGIN_NAME: plugin}


def test_prepare_for_readiness_sees_configured_roots_before_initialize() -> None:
    with tempfile.TemporaryDirectory() as app_home:
        plugins_config_dir = Path(app_home) / "config" / "plugins"
        plugins_config_dir.mkdir(parents=True, exist_ok=True)
        configured_root = str(Path(app_home) / "allowed_export_root")
        Path(configured_root).mkdir(parents=True, exist_ok=True)
        (plugins_config_dir / f"{PLUGIN_NAME}.json").write_text(
            json.dumps({"export_allowed_roots": [configured_root]}),
            encoding="utf-8",
        )

        config_manager = ConfigManager(app_home)
        config_manager.initialize()
        set_config_instance(config_manager)
        try:
            plugin = ACTRMemoryPlugin()
            plugin.orchestrator_ref = _FakeOrchestrator(app_home)  # type: ignore[assignment]

            # PLATFORM'S REAL ORDER: prepare_for_readiness() runs first.
            plugin.prepare_for_readiness()

            _check(
                plugin.config_provider is not None,
                "config_provider is bound after prepare_for_readiness",
            )
            resolved = plugin._resolve_export_allowed_roots()  # noqa: SLF001
            _check(
                resolved == [configured_root],
                f"RED-vs-GREEN: prepare_for_readiness alone (BEFORE initialize() ever "
                f"runs) sees the configured export_allowed_roots from disk (got "
                f"{resolved!r}) — pre-fix this was always [] because "
                f"self._operator_config was only ever populated by initialize()",
            )
            _check(
                plugin._backend is not None  # noqa: SLF001
                and plugin._backend._export_allowed_roots == [configured_root],  # noqa: SLF001
                "the constructed backend itself was built with the configured roots, "
                "not an empty list",
            )

            # THEN the platform calls initialize() — must not un-bind or diverge.
            plugin.initialize(config_manager.get_plugin_config(PLUGIN_NAME, default_config={}))
            _check(
                plugin._resolve_export_allowed_roots() == [configured_root],  # noqa: SLF001
                "the configured roots survive initialize() running afterward, unchanged",
            )
        finally:
            # Not reset_config() — that RE-initializes with a new APP_HOME, it
            # doesn't clear the singleton. Directly null the module global so
            # a later get_config() call in this same process (defensive; this
            # smoke's own process exits right after, per this codebase's
            # one-file-one-process convention) never resolves to this test's
            # now-deleted tmp APP_HOME.
            _config_manager_module._config_instance = None  # noqa: SLF001


def test_reload_rebinds_live_backend_export_roots() -> None:
    """A config-file edit reaches the already-live backend through the real reload verb.

    Mutation: remove initialize()'s live-backend roots rebind.  The reload
    still reports success, but export to ``reloaded_export_root`` is refused.
    """
    with tempfile.TemporaryDirectory() as app_home:
        plugins_config_dir = Path(app_home) / "config" / "plugins"
        plugins_config_dir.mkdir(parents=True, exist_ok=True)
        initial_root = Path(app_home) / "initial_export_root"
        reloaded_root = Path(app_home) / "reloaded_export_root"
        initial_root.mkdir()
        reloaded_root.mkdir()
        config_path = plugins_config_dir / f"{PLUGIN_NAME}.json"
        config_path.write_text(
            json.dumps({"export_allowed_roots": [str(initial_root)]}),
            encoding="utf-8",
        )

        config_manager = ConfigManager(app_home)
        config_manager.initialize()
        set_config_instance(config_manager)
        try:
            plugin = ACTRMemoryPlugin()
            orchestrator = _FakeOrchestrator(app_home)
            plugin.orchestrator_ref = orchestrator  # type: ignore[assignment]
            plugin.prepare_for_readiness()
            orchestrator.config = config_manager
            orchestrator.plugin_manager = _FakePluginManager(plugin)

            # Operator edits the live profile; the lifecycle verb is the real
            # production path that reads it and invokes plugin.initialize().
            config_path.write_text(
                json.dumps({"export_allowed_roots": [str(reloaded_root)]}),
                encoding="utf-8",
            )
            reload_result = LifecycleManagementService(orchestrator).reload_plugin_config(
                PLUGIN_NAME
            )
            _check(
                reload_result["action_status"] == "completed"
                and reload_result["data"]["dirty_keys"] == ["export_allowed_roots"],
                "edit-config then reload reports export_allowed_roots as applied",
            )

            export_path = reloaded_root / "export.json"
            export_result = plugin.export_memories(file_path=str(export_path))
            _check(
                Path(export_result["file_path"]).exists(),
                "reload lets the live backend export into the newly allowed root",
            )

            # A malformed live config is still a loud reload fault; rebind must
            # validate through _resolve_export_allowed_roots rather than weaken
            # the containment policy.
            config_path.write_text(
                json.dumps({"export_allowed_roots": "not-a-list"}),
                encoding="utf-8",
            )
            malformed_result = LifecycleManagementService(orchestrator).reload_plugin_config(
                PLUGIN_NAME
            )
            _check(
                malformed_result["action_status"] == "error"
                and "must be a list of directory path strings" in malformed_result["error"],
                "malformed export_allowed_roots remains a loud reload fault",
            )
        finally:
            _config_manager_module._config_instance = None  # noqa: SLF001


def main() -> int:
    print("=== config_binding_order_smoke ===")
    test_prepare_for_readiness_sees_configured_roots_before_initialize()
    test_reload_rebinds_live_backend_export_roots()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
