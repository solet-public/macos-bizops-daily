#!/usr/bin/env python3
"""Setup's Apple model check warns on model absence and blocks only unsupported hardware (iss_9b396043).

Every apple_fm_sdk 0.2.1 unavailable reason is driven on a physical Mac and on
a VM. APPLE_INTELLIGENCE_NOT_ENABLED, MODEL_NOT_READY and UNKNOWN proceed as
``verified`` with warning evidence that names the reason and the user action
(rul_18bd93a3, rul_73886083). DEVICE_NOT_ELIGIBLE warns on a VM and blocks on
physical hardware (rul_cc1afc13); a probe exception and a context other than
8192 tokens still block. Offline; the SDK and ``sysctl`` are faked.

Run::

    .venv/bin/python3 plugins/github_midwife_plugin/tests/apple_model_availability_warning_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "plugins" / "github_midwife_plugin" / "src"))

from github_midwife_plugin import apple_setup_adapter as apple  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome, Runtime  # noqa: E402

PHYSICAL, VIRTUAL = "Mac15,14", "VirtualMac2,1"


class _Reason(Enum):
    """apple_fm_sdk 0.2.1 SystemLanguageModelUnavailableReason names."""

    APPLE_INTELLIGENCE_NOT_ENABLED = 0
    DEVICE_NOT_ELIGIBLE = 1
    MODEL_NOT_READY = 2
    UNKNOWN = 0xFF


WARN_ONLY = {
    _Reason.APPLE_INTELLIGENCE_NOT_ENABLED: "enable Apple Intelligence in System Settings",
    _Reason.MODEL_NOT_READY: "still downloading",
    _Reason.UNKNOWN: "Apple Intelligence in System Settings",
}


class _Host:
    """Answers only the ``sysctl hw.model`` read that separates a VM from hardware."""

    def __init__(self, model: str) -> None:
        self.model = model

    def run(self, argv: tuple[str, ...], **_kwargs: object) -> CommandOutcome:
        assert argv == ("/usr/sbin/sysctl", "-n", "hw.model"), f"unreviewed command: {argv}"
        return CommandOutcome(0, False, 1, self.model, "")


class _Model:
    available = False
    reason: _Reason | None = _Reason.DEVICE_NOT_ELIGIBLE
    context_size = 8192

    def is_available(self) -> tuple[bool, _Reason | None]:
        return self.available, None if self.available else self.reason


def _request(target: Path) -> AdapterRequest:
    return AdapterRequest(
        request_id=str(uuid.uuid4()), operation_id="apple_model_availability",
        operation_ref="setup::apple.model_availability", phase="probe", probe_purpose="pre_apply",
        attempt=1, name="apple-test", target=target, flow_source_revision="a" * 40,
        answers_fingerprint="sha256:" + "b" * 64, approval_fingerprint=None, dry_run=True,
        timeout_seconds=30, public_inputs={},
    )


def _host(model: str) -> Runtime:
    return cast(Runtime, _Host(model))


def _probe(host: str, *, available: bool, reason: _Reason | None, context: int = 8192) -> JsonObject:
    model = type("Model", (_Model,), {"available": available, "reason": reason, "context_size": context})
    with tempfile.TemporaryDirectory() as target, \
            patch.dict(sys.modules, {"apple_fm_sdk": SimpleNamespace(SystemLanguageModel=model)}):
        return apple.model_availability(_request(Path(target)), _host(host))


def _evidence(response: JsonObject) -> dict[str, object]:
    return cast(list[dict[str, object]], response["evidence"])[0]


class ModelAbsenceWarns(unittest.TestCase):
    def test_every_non_hardware_reason_proceeds_on_both_hosts_naming_reason_and_action(self) -> None:
        for reason, action in WARN_ONLY.items():
            for host in (PHYSICAL, VIRTUAL):
                with self.subTest(reason=reason.name, host=host):
                    response = _probe(host, available=False, reason=reason)
                    self.assertEqual(response["checkpoint_status"], "verified")
                    self.assertIsNone(response["error_kind"])
                    item = _evidence(response)
                    self.assertEqual((item["status"], item["observed"]), ("warning", reason.name))
                    self.assertIn(reason.name, str(item["summary"]))
                    self.assertIn(action.lower(), f"{item['summary']} {response['repair']}".lower())

    def test_vm_device_not_eligible_is_unchanged_warning(self) -> None:
        response = _probe(VIRTUAL, available=False, reason=_Reason.DEVICE_NOT_ELIGIBLE)
        self.assertEqual(response["checkpoint_status"], "verified")
        self.assertEqual(
            _evidence(response)["summary"],
            "Apple system summarization unavailable on this VM; installation may proceed with a warning",
        )

    def test_available_model_passes(self) -> None:
        for host in (PHYSICAL, VIRTUAL):
            response = _probe(host, available=True, reason=None)
            self.assertEqual((response["checkpoint_status"], _evidence(response)["status"]), ("verified", "passed"))
            self.assertIsNone(response["repair"])


class StillBlocks(unittest.TestCase):
    def test_physical_device_not_eligible_blocks(self) -> None:
        response = _probe(PHYSICAL, available=False, reason=_Reason.DEVICE_NOT_ELIGIBLE)
        self.assertEqual(
            (response["checkpoint_status"], response["error_kind"]),
            ("blocked", "apple_physical_model_unavailable"),
        )

    def test_wrong_context_blocks_on_both_hosts(self) -> None:
        for host in (PHYSICAL, VIRTUAL):
            response = _probe(host, available=True, reason=None, context=4096)
            self.assertEqual(response["error_kind"], "apple_model_context_invalid")

    def test_probe_exception_blocks(self) -> None:
        def crash() -> object:
            raise RuntimeError("SDK probe crash fixture")

        with patch.dict(sys.modules, {"apple_fm_sdk": SimpleNamespace(SystemLanguageModel=crash)}), \
                tempfile.TemporaryDirectory() as target:
            response = apple.model_availability(_request(Path(target)), _host(PHYSICAL))
        self.assertEqual(response["error_kind"], "apple_model_probe_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
