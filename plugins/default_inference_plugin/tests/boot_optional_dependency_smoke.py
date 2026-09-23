#!/usr/bin/env python3
"""Regression smoke for the LM Studio boot-optional dependency path.

Run::

    .venv/bin/python3 plugins/default_inference_plugin/tests/boot_optional_dependency_smoke.py

Drives the production ``Plugin.prepare_for_readiness`` seam with a provider
that is unavailable twice and then becomes available.  It proves that startup
does not raise, the plugin remains explicitly unready while waiting, retries
at the plugin boundary, and refuses inference until the retry marks it ready.
No live LM Studio or network is used.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_inference_plugin" / "src"))

from ananta.core.domain import ActionStatus  # noqa: E402
from ananta.interfaces import (  # noqa: E402
    InferenceRequest,
    InferenceServiceUnavailableError,
    InferenceValidationError,
)

import default_inference_plugin.plugin as plugin_module  # noqa: E402
from default_inference_plugin.plugin import Plugin  # noqa: E402

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


class _FakeConfigProvider:
    _values: dict[str, object] = {
        "base_url": "http://localhost:1234/v1",
        "model": "test-model",
        "timeout_seconds": 30,
        "temperature": 0.0,
        "max_tokens": 32,
    }

    def get(self, key: str) -> object | None:
        return self._values.get(key)

    def get_int(self, key: str) -> int:
        value = self._values[key]
        assert isinstance(value, int)
        return value

    def get_float(self, key: str) -> float:
        value = self._values[key]
        assert isinstance(value, float)
        return value


class _FakeConfigManager:
    def get_plugin_config_provider(self, name: str) -> _FakeConfigProvider:
        assert name == "default_inference_plugin"
        return _FakeConfigProvider()


class _FakeOrchestrator:
    APP_HOME = "/tmp/boot-optional-dependency-smoke"
    config_manager = _FakeConfigManager()


class _EventuallyAvailableProvider:
    availability_checks = 0

    def __init__(self, base_url: str, model: str, timeout: int) -> None:
        self.base_url = base_url
        self.model = model
        self.timeout = timeout

    def validate_availability(self) -> dict[str, object]:
        type(self).availability_checks += 1
        status = (
            ActionStatus.COMPLETED.value
            if type(self).availability_checks >= 3
            else ActionStatus.ERROR.value
        )
        return {"action_status": status, "data": {}, "error": "unavailable"}

    def generate_completion(self, request: InferenceRequest) -> dict[str, object]:
        del request
        return {"action_status": ActionStatus.COMPLETED.value, "data": {}, "error": None}


class _AvailableProvider:
    """Provider double for a configured model that is immediately reachable."""

    def __init__(self, base_url: str, model: str, timeout: int) -> None:
        del base_url, model, timeout

    @staticmethod
    def validate_availability() -> dict[str, object]:
        return {"action_status": ActionStatus.COMPLETED.value, "data": {}, "error": None}


class _PrewarmFailingProvider:
    """Provider double for a reachable server that rejects grammar prewarm."""

    @staticmethod
    def generate_completion(_request: InferenceRequest) -> dict[str, object]:
        raise InferenceServiceUnavailableError("no models loaded")


class _QwenOneTokenProvider:
    """Reproduce Qwen's exact one-token grammar-prewarm failure."""

    def __init__(self) -> None:
        self.requests: list[InferenceRequest] = []

    def generate_completion(self, request: InferenceRequest) -> dict[str, object]:
        self.requests.append(request)
        if request.max_tokens == 1:
            raise InferenceValidationError(
                "Response truncated: model hit token limit (1 output tokens). "
                "The response is incomplete and cannot be parsed."
            )
        return {"action_status": ActionStatus.COMPLETED.value, "data": {}, "error": None}


class _MessageCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _wait_until_ready(plugin: Plugin, timeout_seconds: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if plugin.is_ready():
            return True
        time.sleep(0.01)
    return plugin.is_ready()


def test_unavailable_boot_degrades_then_recovers() -> None:
    original_provider = plugin_module.LMStudioProvider
    original_logging = plugin_module.configure_plugin_logging
    original_sleep = plugin_module.time.sleep
    capture = _MessageCapture()
    test_logger = logging.getLogger("boot_optional_dependency_smoke")
    test_logger.addHandler(capture)
    _EventuallyAvailableProvider.availability_checks = 0
    prewarm_calls: list[bool] = []
    retry_started = threading.Event()
    release_retry = threading.Event()

    def _blocked_retry_sleep(_seconds: float) -> None:
        retry_started.set()
        release_retry.wait(timeout=1.0)

    plugin_module.LMStudioProvider = _EventuallyAvailableProvider  # type: ignore[assignment]
    plugin_module.configure_plugin_logging = lambda *_args: test_logger
    plugin_module.time.sleep = _blocked_retry_sleep
    try:
        plugin = Plugin()
        plugin.orchestrator_ref = _FakeOrchestrator()  # type: ignore[assignment]
        plugin._prewarm_canonical_grammars = lambda: prewarm_calls.append(True)  # type: ignore[method-assign]

        raised: Exception | None = None
        try:
            plugin.prepare_for_readiness()
        except Exception as exc:  # RED: current code raises here.
            raised = exc

        _check(raised is None, "unavailable LM Studio does not abort prepare_for_readiness")
        _check(
            _EventuallyAvailableProvider.availability_checks == 0,
            "prepare_for_readiness makes no inference request before router registration",
        )
        plugin.start_post_registration_work()
        _check(retry_started.wait(timeout=0.1), "background retry starts after the boot-time miss")
        _check(not plugin.is_ready(), "plugin is explicitly unready during the retry window")
        _check(
            plugin.get_readiness_error()
            == "LM Studio not available at http://localhost:1234/v1 after router registration",
            "readiness error names the unavailable endpoint",
        )
        release_retry.set()
        _check(
            _wait_until_ready(plugin),
            "background retry reaches availability and marks the plugin ready",
        )
        _check(
            _EventuallyAvailableProvider.availability_checks == 3,
            "retry rechecks availability until the first successful response",
        )
        _check(prewarm_calls == [True], "prewarm runs once after availability succeeds")
        wait_lines = [line for line in capture.messages if "LM Studio not available" in line]
        _check(len(wait_lines) == 1, "one actionable waiting line is logged, not one per retry")
    finally:
        test_logger.removeHandler(capture)
        plugin_module.LMStudioProvider = original_provider
        plugin_module.configure_plugin_logging = original_logging
        plugin_module.time.sleep = original_sleep


def test_cold_prewarm_does_not_delay_startup_readiness() -> None:
    """A blocked grammar compile must not hold router registration hostage."""
    original_provider = plugin_module.LMStudioProvider
    original_logging = plugin_module.configure_plugin_logging
    entered_prewarm = threading.Event()
    release_prewarm = threading.Event()

    def _blocked_prewarm() -> None:
        entered_prewarm.set()
        release_prewarm.wait(timeout=1.0)

    plugin_module.LMStudioProvider = _AvailableProvider  # type: ignore[assignment]
    plugin_module.configure_plugin_logging = lambda *_args: logging.getLogger(
        "boot_optional_dependency_cold_prewarm"
    )
    try:
        plugin = Plugin()
        plugin.orchestrator_ref = _FakeOrchestrator()  # type: ignore[assignment]
        plugin._prewarm_canonical_grammars = _blocked_prewarm  # type: ignore[method-assign]

        plugin.prepare_for_readiness()
        _check(not entered_prewarm.wait(timeout=0.05), "prewarm waits for router registration")
        plugin.start_post_registration_work()

        _check(
            entered_prewarm.wait(timeout=0.1),
            "cold grammar prewarm begins in a background worker",
        )
        _check(
            plugin.is_ready(),
            "startup readiness completes while cold grammar prewarm is blocked",
        )
    finally:
        release_prewarm.set()
        plugin_module.LMStudioProvider = original_provider
        plugin_module.configure_plugin_logging = original_logging


def test_inference_is_refused_while_unready() -> None:
    plugin = Plugin()
    plugin.provider = _EventuallyAvailableProvider("http://localhost:1234/v1", "test-model", 30)
    plugin.set_error("LM Studio not available at http://localhost:1234/v1")
    raised: Exception | None = None
    try:
        plugin.generate_completion(
            InferenceRequest(
                prompt="ping",
                temperature=0.0,
                max_tokens=1,
                use_structured_output=False,
            )
        )
    except InferenceServiceUnavailableError as exc:
        raised = exc
    _check(raised is not None, "inference is refused clearly while the plugin is unready")
    _check(
        raised is not None and "LM Studio not available" in str(raised),
        "unready inference error preserves the actionable readiness detail",
    )


def test_text_completion_is_refused_while_unready() -> None:
    """Context compaction reaches this method without generate_completion()."""
    plugin = Plugin()
    plugin.provider = _EventuallyAvailableProvider("http://localhost:1234/v1", "test-model", 30)
    plugin.set_error("LM Studio not available at http://localhost:1234/v1")
    raised: Exception | None = None
    try:
        plugin.generate_text_completion(
            "summarize this context", max_tokens=1, temperature=0.0, load_context=False
        )
    except Exception as exc:  # RED: an unready path currently reaches later setup.
        raised = exc
    _check(
        isinstance(raised, InferenceServiceUnavailableError),
        "text completion is refused cleanly while the plugin is unready",
    )
    _check(
        isinstance(raised, InferenceServiceUnavailableError)
        and "LM Studio not available" in str(raised),
        "text completion preserves the actionable readiness detail",
    )


def test_prewarm_failure_is_not_reported_as_compiled() -> None:
    plugin = Plugin()
    capture = _MessageCapture()
    test_logger = logging.getLogger("boot_optional_dependency_prewarm_failure")
    test_logger.addHandler(capture)
    plugin.provider = _PrewarmFailingProvider()  # type: ignore[assignment]
    plugin.logger = test_logger

    raised: InferenceServiceUnavailableError | None = None
    try:
        plugin._prewarm_one_schema("model-loaded regression", {"type": "object"})
    except InferenceServiceUnavailableError as exc:
        raised = exc
    finally:
        test_logger.removeHandler(capture)

    _check(raised is not None, "prewarm provider error propagates instead of being swallowed")
    _check(
        any("PRE-WARM: model-loaded regression failed" in line for line in capture.messages),
        "prewarm failure is logged as failed",
    )
    _check(
        not any("PRE-WARM: model-loaded regression compiled" in line for line in capture.messages),
        "prewarm failure is never logged as a successful compile",
    )


def test_prewarm_budget_avoids_qwen_one_token_truncation() -> None:
    provider = _QwenOneTokenProvider()
    old_request = InferenceRequest(
        [{"role": "user", "content": "Say OK."}],
        temperature=0.0,
        max_tokens=1,
        response_schema={"type": "object"},
        context_metadata={"purpose": "grammar_prewarm"},
    )
    old_failure: InferenceValidationError | None = None
    try:
        provider.generate_completion(old_request)
    except InferenceValidationError as exc:
        old_failure = exc

    _check(
        old_failure is not None
        and "Response truncated: model hit token limit (1 output tokens)" in str(old_failure),
        "one-token Qwen probe reproduces the observed truncation signature",
    )

    plugin = Plugin()
    plugin.provider = provider  # type: ignore[assignment]
    plugin.logger = logging.getLogger("boot_optional_dependency_qwen_prewarm")
    raised: Exception | None = None
    try:
        plugin._prewarm_one_schema("qwen token-budget regression", {"type": "object"})
    except Exception as exc:  # RED: the one-token production probe raises here.
        raised = exc

    fixed_request = provider.requests[-1]
    _check(raised is None, "prewarm no longer trips Qwen's one-token truncation")
    _check(
        fixed_request.max_tokens == plugin_module._GRAMMAR_PREWARM_MAX_TOKENS,
        "prewarm uses the bounded schema-completion budget",
    )
    _check(
        fixed_request.messages
        == [{"role": "user", "content": plugin_module._GRAMMAR_PREWARM_PROMPT}],
        "prewarm explicitly asks for the shortest schema-valid response",
    )


def main() -> int:
    print("=== boot_optional_dependency_smoke ===")
    test_unavailable_boot_degrades_then_recovers()
    test_cold_prewarm_does_not_delay_startup_readiness()
    test_inference_is_refused_while_unready()
    test_text_completion_is_refused_while_unready()
    test_prewarm_failure_is_not_reported_as_compiled()
    test_prewarm_budget_avoids_qwen_one_token_truncation()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
