#!/usr/bin/env python3
"""Regression smoke for iss_0a40ccfc: coreai_embeddings_plugin never reached
ready on a fresh boot.

Root cause (measured at source, ``ananta/src/ananta/core/orchestration/
startup_sequence.py``'s ``STARTUP_SEQUENCE``): the platform's real plugin
boot order runs ``prepare_for_readiness()`` (inside ``_start_service_plugins``,
STARTUP_SEQUENCE Phase 1) BEFORE ``initialize()`` (inside
``_initialize_plugin_configs``, which depends on ``create_service_wrappers``,
itself downstream of ``start_service_plugins``) — so Phase 1 always ran on
the plugin's default/empty config. ``CoreAIEmbeddingsPlugin.initialize()``
then deliberately invalidated readiness (``self._close()`` + ``set_error``)
when the real config arrived, and nothing downstream re-invoked
``prepare_for_readiness()``: this plugin defines no ``start_services`` /
``stop_services`` / ``is_running`` / ``set_active``, so it is not
``LifecycleManaged`` (``ananta/core/plugins/protocols.py``'s
``LifecycleManaged`` Protocol) and ``_verify_readiness()`` — which only
checks ``is_lifecycle_managed`` plugins — never even caught it. Boot
completed silently with the plugin stuck not-ready. ``reload_plugin_config``
(``ananta/services/lifecycle_management_service/service.py``) hits the
identical gap: it also calls only ``initialize()``, never
``prepare_for_readiness()`` again.

This smoke drives the plugin in the PLATFORM'S REAL ORDER — construct (bare,
as ``PluginManager`` discovery does), ``prepare_for_readiness()``, THEN
``initialize(real_config)`` — and separately through the real
``reload_plugin_config`` call site. Red-fails on the pre-fix code (readiness
stays invalidated after ``initialize()`` in both cases).

Run::

    .venv/bin/python3 plugins/coreai_embeddings_plugin/tests/boot_order_readiness_smoke.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "coreai_embeddings_plugin" / "src"))

import ananta.core.config.config_manager as _config_manager_module  # noqa: E402
from ananta.core.config.config_manager import ConfigManager, set_config_instance  # noqa: E402
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
)
from coreai_embeddings_plugin.contracts import EmbeddingError, ErrorCode  # noqa: E402
from coreai_embeddings_plugin.plugin import CoreAIEmbeddingsPlugin  # noqa: E402

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


class _FakeRuntime:
    """Fault-injection fixture at the model boundary; never used for numeric evidence."""

    fail_prepare = False

    def __init__(self, _root: Path, _preference: str) -> None:
        pass

    def prepare(self) -> None:
        if self.fail_prepare:
            raise EmbeddingError(ErrorCode.ASSET_MISSING, "asset absent")

    def close(self) -> None:
        pass

    def diagnostics(self) -> dict[str, object]:
        return {"observed_compute_unit": "fixture"}


class _FakePluginManager:
    """The minimal lifecycle-service plugin manager surface."""

    def __init__(self, plugin: CoreAIEmbeddingsPlugin) -> None:
        self.plugins = {"coreai_embeddings_plugin": plugin}


class _FakeOrchestrator:
    def __init__(self, app_home: str, plugin: CoreAIEmbeddingsPlugin) -> None:
        self.APP_HOME = app_home
        self.config: Any = None
        self.plugin_manager: Any = _FakePluginManager(plugin)


def test_fresh_boot_order_reaches_ready_after_initialize() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asset_root = Path(tmp) / "assets"
        asset_root.mkdir()
        real_config = {"asset_root": str(asset_root), "compute_preference": "cpu"}

        with patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", _FakeRuntime):
            # Manifest discovery: bare construction, matching PluginManager.
            plugin = CoreAIEmbeddingsPlugin()

            # STARTUP_SEQUENCE step "start_service_plugins" Phase 1 — runs
            # BEFORE the plugin's real config is loaded.
            plugin.prepare_for_readiness()
            _check(
                not plugin.is_ready(),
                "Phase 1 prepare on default/empty config leaves the plugin not-ready "
                "(no asset_root yet — matches the reported symptom)",
            )

            # STARTUP_SEQUENCE step "initialize_plugin_configs" — runs AFTER
            # start_service_plugins/create_service_wrappers per the real
            # dependency graph in startup_sequence.py.
            plugin.initialize(real_config)
            _check(
                plugin.is_ready(),
                "RED-vs-GREEN: initialize() with the real config reaches ready "
                "without a second explicit prepare_for_readiness() call — pre-fix "
                "initialize() only invalidated readiness and nothing re-prepared it",
            )


def test_negative_control_genuine_prepare_failure_stays_not_ready() -> None:
    """A plugin whose prepare genuinely fails must stay not-ready, with its reason."""
    with tempfile.TemporaryDirectory() as tmp:
        asset_root = Path(tmp) / "assets"
        asset_root.mkdir()
        bad_config = {"asset_root": str(asset_root), "compute_preference": "cpu"}

        with patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", _FakeRuntime):
            _FakeRuntime.fail_prepare = True
            try:
                plugin = CoreAIEmbeddingsPlugin()
                plugin.prepare_for_readiness()
                plugin.initialize(bad_config)
                _check(
                    not plugin.is_ready(),
                    "a genuinely failing prepare stays not-ready even through "
                    "the fixed initialize()",
                )
                _check(
                    bool(plugin.get_readiness_error()),
                    "the not-ready plugin reports a real reason, never a silent pass",
                )
            finally:
                _FakeRuntime.fail_prepare = False


def test_reload_plugin_config_reaches_ready() -> None:
    """The second real call site: reload_plugin_config also calls only initialize()."""
    with tempfile.TemporaryDirectory() as app_home:
        plugins_config_dir = Path(app_home) / "config" / "plugins"
        plugins_config_dir.mkdir(parents=True, exist_ok=True)
        asset_root = Path(app_home) / "assets"
        asset_root.mkdir()
        (plugins_config_dir / "coreai_embeddings_plugin.json").write_text(
            json.dumps({"asset_root": str(asset_root), "compute_preference": "cpu"}),
            encoding="utf-8",
        )

        config_manager = ConfigManager(app_home)
        config_manager.initialize()
        set_config_instance(config_manager)
        try:
            with patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", _FakeRuntime):
                plugin = CoreAIEmbeddingsPlugin()
                orchestrator = _FakeOrchestrator(app_home, plugin)
                # Boot-order Phase 1 with default/empty config, exactly as above.
                plugin.prepare_for_readiness()
                orchestrator.config = config_manager

                reload_result = LifecycleManagementService(orchestrator).reload_plugin_config(
                    "coreai_embeddings_plugin"
                )
                _check(
                    reload_result["action_status"] == "completed",
                    "reload_plugin_config reports success",
                )
                _check(
                    plugin.is_ready(),
                    "RED-vs-GREEN: reload_plugin_config's call to initialize() alone "
                    "reaches ready — pre-fix it only invalidated readiness with "
                    "nothing left to re-prepare it",
                )
        finally:
            _config_manager_module._config_instance = None  # noqa: SLF001


def main() -> int:
    print("=== coreai_embeddings_plugin boot_order_readiness_smoke ===")
    test_fresh_boot_order_reaches_ready_after_initialize()
    test_negative_control_genuine_prepare_failure_stays_not_ready()
    test_reload_plugin_config_reaches_ready()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
