#!/usr/bin/env python3
"""Real ``list_plugins`` rows through the real roster doctor (iss_4b22fdeb, iss_7f4ce644).

The producer is ``LifecycleManagementService._build_plugin_row`` over real
``PluginBase`` readiness transitions; the consumer is the doctor's roster
parser. No hand-written row stands between them, so a field the producer emits
and the consumer ignores (or rejects) fails here.

Run::

    .venv/bin/python3 plugins/github_midwife_plugin/tests/plugin_roster_readiness_seam_smoke.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "github_midwife_plugin" / "src"))

from ananta.core.plugins.plugin_base import PluginBase, PluginReadiness  # noqa: E402
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
)
from github_midwife_plugin.installation_plugin_doctor import _observed_plugins  # noqa: E402

UNAVAILABLE = "Apple Foundation Models unavailable: DEVICE_NOT_ELIGIBLE"
TORN = "Core AI compiled cache invalid after purge and recompile: canary killed by signal 5"


class _Fixture(PluginBase):
    """A plain plugin whose readiness the test drives through the public transitions."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name


def _roster(*plugins: PluginBase) -> list[dict[str, object]]:
    orchestrator = SimpleNamespace(
        plugin_manager=SimpleNamespace(plugins={plugin.name: plugin for plugin in plugins}),
        config=SimpleNamespace(get_plugin_config=lambda _name: {"priority": 100, "enabled": True}),
    )
    listed = LifecycleManagementService(orchestrator).list_plugins()
    assert listed["action_status"] == "completed", listed
    return list(listed["data"]["plugins"])


class ReadinessTransitions(unittest.TestCase):
    def test_ready_with_a_warning_is_degraded_and_transitions_clear_it(self) -> None:
        plugin = _Fixture("fixture_plugin")
        plugin.set_ready(warning=UNAVAILABLE)
        self.assertTrue(plugin.is_ready())
        self.assertEqual(plugin.readiness_state, PluginReadiness.READY)
        self.assertIsNone(plugin.get_readiness_error())
        self.assertEqual(plugin.readiness_warning, UNAVAILABLE)
        plugin.set_ready()
        self.assertIsNone(plugin.readiness_warning)
        plugin.set_ready(warning=UNAVAILABLE)
        plugin.set_error(TORN)
        self.assertFalse(plugin.is_ready())
        self.assertIsNone(plugin.readiness_warning)


class RosterSeam(unittest.TestCase):
    def test_producer_rows_reach_the_doctor_with_names_reasons_and_warnings(self) -> None:
        healthy, degraded, torn, dormant = (
            _Fixture("healthy_plugin"), _Fixture("apple_like_plugin"),
            _Fixture("coreai_like_plugin"), _Fixture("dormant_plugin"),
        )
        healthy.set_ready()
        degraded.set_ready(warning=UNAVAILABLE)
        torn.set_error(TORN)
        rows = {str(row["name"]): row for row in _roster(healthy, degraded, torn, dormant)}
        self.assertEqual(rows["apple_like_plugin"]["status"], "ready")
        self.assertEqual(rows["apple_like_plugin"]["warning"], UNAVAILABLE)
        self.assertNotIn("last_error", rows["apple_like_plugin"])
        self.assertNotIn("warning", rows["healthy_plugin"])
        self.assertEqual(rows["coreai_like_plugin"]["last_error"], TORN)

        observed = _observed_plugins(list(rows.values()))
        assert observed is not None
        self.assertEqual(observed.actual, set(rows))
        self.assertEqual(observed.unready, {
            f"coreai_like_plugin: error: {TORN}",
            "dormant_plugin: uninitialized: no last_error reported",
        })
        self.assertEqual(observed.warnings, {f"apple_like_plugin: {UNAVAILABLE}"})

    def test_all_ready_roster_with_one_degraded_plugin_has_no_unready(self) -> None:
        healthy, degraded = _Fixture("healthy_plugin"), _Fixture("apple_like_plugin")
        healthy.set_ready()
        degraded.set_ready(warning=UNAVAILABLE)
        observed = _observed_plugins(list(_roster(healthy, degraded)))
        assert observed is not None
        self.assertEqual(observed.unready, set())
        self.assertEqual(observed.warnings, {f"apple_like_plugin: {UNAVAILABLE}"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
