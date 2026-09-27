#!/usr/bin/env python3
"""Exercise selected-provider startup with the frozen Apple plugin, offline.

This half ships with every seed that carries the Apple plugin. The comparisons
against the legacy ``default_inference_plugin`` live in the checkout-only
companion ``selected_inference_legacy_startup_smoke.py``, because no capability
bundle ships both plugins.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[4]
for source in (
    "ananta/src", "plugins/macos_inference_plugin/src", "plugins/macos_inference_plugin/tests",
):
    sys.path.insert(0, str(ROOT / source))

from ananta.core.config.config_provider import ConfigProvider  # noqa: E402
from ananta.core.orchestration import startup_sequence as startup  # noqa: E402
from ananta.core.orchestration.service_bindings import ServiceBindings  # noqa: E402
from ananta.interfaces import InferenceServiceUnavailableError  # noqa: E402
from apple_fm_provider_smoke import _FakeModel, _FakeSession, _provider, _request  # noqa: E402
from macos_inference_plugin.configuration import default_config  # noqa: E402
from macos_inference_plugin.plugin import Plugin as MacOSPlugin  # noqa: E402


class SelectedInferenceStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (self.home / "config").mkdir()
        self.stack.enter_context(patch.dict("os.environ", {}, clear=True))
        self.setup_calls: list[str] = []
        for name in (
            "_seed_identity_memories", "_reindex_orphaned_memories",
            "_auto_install_knowledge_bases",
        ):
            self.stack.enter_context(patch.object(
                startup, name, side_effect=lambda _orch, step=name: self.setup_calls.append(step),
            ))
        self.model = _FakeModel()
        self.apple = MacOSPlugin()
        self.apple.set_config_provider(ConfigProvider(self.apple.name, default_config()))
        self.apple.prepare_for_readiness()
        self.apple.provider = _provider(self.model)
        self.apple_hook = self.stack.enter_context(patch.object(
            self.apple, "start_post_registration_work",
            wraps=self.apple.start_post_registration_work,
        ))
        self.embedding = SimpleNamespace(
            start_post_registration_qualification=Mock(),
            wait_for_post_registration_qualification=Mock(),
        )
        self.generation_count = len(_FakeSession.calls)

    def orchestrator(
        self, selected: object, plugins: dict[str, Any], active: object = None,
    ) -> SimpleNamespace:
        bindings = {} if selected is None else {"inference_service": selected}
        (self.home / "config/service_bindings.json").write_text(json.dumps(bindings))
        service_bindings = ServiceBindings(self.home)
        service_bindings.load()
        return SimpleNamespace(
            service_bindings=service_bindings,
            plugin_manager=SimpleNamespace(plugins={
                **plugins, "openai_embeddings_plugin": self.embedding,
            }),
            inference_service=SimpleNamespace(get_inference_provider=lambda: active),
            action_coordinator=SimpleNamespace(populate_discovery_after_registration=Mock()),
            registered=True,
        )

    def run_startup(self, orch: SimpleNamespace) -> dict[str, Any]:
        startup._run_post_registration_work(orch)
        self.assertTrue(orch.registered)
        self.embedding.start_post_registration_qualification.assert_called()
        self.embedding.wait_for_post_registration_qualification.assert_called()
        orch.action_coordinator.populate_discovery_after_registration.assert_called()
        self.assertEqual(self.setup_calls[-3:], [
            "_seed_identity_memories", "_reindex_orphaned_memories",
            "_auto_install_knowledge_bases",
        ])
        self.assertEqual(len(_FakeSession.calls), self.generation_count)
        return orch.post_registration_work_status

    def test_macos_only(self) -> None:
        orch = self.orchestrator(self.apple.name, {self.apple.name: self.apple}, self.apple)
        self.assertEqual(self.run_startup(orch), {
            "state": "started", "provider": self.apple.name,
        })
        self.apple_hook.assert_called_once()

    def test_unavailable_vm_warning_keeps_setup_and_registration(self) -> None:
        self.model.available = False
        orch = self.orchestrator(self.apple.name, {self.apple.name: self.apple}, self.apple)
        with self.assertLogs("macos_inference_plugin.plugin", level="WARNING") as logs:
            status = self.run_startup(orch)
        self.assertEqual(status["state"], "pending")
        self.assertEqual(status["warning"]["code"], "inference.provider_not_ready")
        self.assertEqual(status["warning"]["severity"], "WARNING")
        self.assertIn("DEVICE_NOT_ELIGIBLE", status["warning"]["message"])
        self.assertIn("DEVICE_NOT_ELIGIBLE", " ".join(logs.output))
        self.assertFalse(self.apple.is_ready())
        with self.assertRaises(InferenceServiceUnavailableError):
            self.apple.generate_completion(_request("Summarize facts."))
        self.assertEqual(len(_FakeSession.calls), self.generation_count)

    def test_recovery_rechecks_without_claiming_generation(self) -> None:
        self.model.available = False
        orch = self.orchestrator(self.apple.name, {self.apple.name: self.apple}, self.apple)
        self.assertEqual(self.run_startup(orch)["state"], "pending")
        self.model.available = True
        self.assertTrue(self.apple.is_ready())
        self.assertIsNone(self.apple.get_readiness_error())
        status = self.run_startup(orch)
        self.assertEqual(status["state"], "started")
        self.assertNotIn("warning", status)

    def test_uninitialized_wrapper_uses_explicit_binding(self) -> None:
        orch = self.orchestrator(self.apple.name, {self.apple.name: self.apple})
        self.assertEqual(self.run_startup(orch)["state"], "started")
        self.apple_hook.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
