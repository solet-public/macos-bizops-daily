#!/usr/bin/env python3
"""LMStudioProvider timeout-wiring smoke — the 2026-07-01 dead-timeout fix.

Run::

    .venv/bin/python3 \
        plugins/default_inference_plugin/tests/timeout_wiring_smoke.py

Proves both halves of the fix without a live LM Studio (the HTTP session is
stubbed — no network):

1. ``generate_completion`` passes ``timeout=self.timeout`` to the chat POST.
   Previously it hardcoded ``timeout=None`` — the configured ``timeout_seconds``
   was dead-lettered and the ``except requests.Timeout`` handler was
   unreachable dead code.

2. A ``requests.exceptions.Timeout`` raised by the session now surfaces as
   ``InferenceTimeoutError`` (the handler is reachable again), and the raised
   error names the configured timeout.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_inference_plugin" / "src"))

from ananta.core.domain import ActionStatus  # noqa: E402
from ananta.interfaces import (  # noqa: E402
    InferenceRequest,
    InferenceTimeoutError,
)

from default_inference_plugin.providers.lm_studio_provider import (  # noqa: E402
    LMStudioProvider,
)

_TIMEOUT_SECONDS = 600
_UNSET: Any = object()

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


class _FakeResponse:
    """Minimal stand-in for a successful ``requests.Response``."""

    @staticmethod
    def raise_for_status() -> None:
        return None

    @staticmethod
    def json() -> dict[str, Any]:
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }


class _RecordingSession:
    """HTTP-session double: records the POST ``timeout`` kwarg, optionally raises."""

    def __init__(self, *, raise_timeout: bool = False) -> None:
        self.headers: dict[str, str] = {}
        self._raise_timeout = raise_timeout
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.captured_timeout: object = _UNSET

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        # Mirrors requests.Session.post(url, ...); the provider passes the
        # endpoint positionally and json= / timeout= as keywords.
        self.calls.append((url, kwargs))
        self.captured_timeout = kwargs.get("timeout", _UNSET)
        if self._raise_timeout:
            raise requests.exceptions.Timeout("simulated LM Studio timeout")
        return _FakeResponse()


class _ModelsResponse:
    """Minimal successful response for the ``/models`` readiness probe."""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    @staticmethod
    def raise_for_status() -> None:
        return None

    def json(self) -> object:
        return self._payload


class _ModelsSession:
    """HTTP-session double for model-loaded readiness checks."""

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _ModelsResponse:
        self.calls.append((url, kwargs))
        return _ModelsResponse(self.payload)


def _make_request() -> InferenceRequest:
    return InferenceRequest(
        prompt="ping",
        temperature=0.0,
        max_tokens=16,
        use_structured_output=False,
    )


def test_post_carries_configured_timeout() -> None:
    """The chat POST carries ``timeout=self.timeout`` (was a hardcoded None)."""
    provider = LMStudioProvider("http://localhost:1234/v1", "test-model", _TIMEOUT_SECONDS)
    session = _RecordingSession()
    provider.session = session  # type: ignore[assignment]

    result = provider.generate_completion(_make_request())

    _check(
        session.captured_timeout == _TIMEOUT_SECONDS,
        f"chat POST carries timeout=self.timeout ({_TIMEOUT_SECONDS}s)",
    )
    _check(
        session.captured_timeout is not None,
        "chat POST timeout is not None (dead-timeout regression guard)",
    )
    _check(
        result.get("action_status") == ActionStatus.COMPLETED.value,
        "a successful completion still returns action_status=completed",
    )


def test_session_timeout_surfaces_as_inference_timeout_error() -> None:
    """A ``requests.Timeout`` now reaches the handler → ``InferenceTimeoutError``."""
    provider = LMStudioProvider("http://localhost:1234/v1", "test-model", _TIMEOUT_SECONDS)
    provider.session = _RecordingSession(raise_timeout=True)  # type: ignore[assignment]

    raised: InferenceTimeoutError | None = None
    try:
        provider.generate_completion(_make_request())
    except InferenceTimeoutError as exc:
        raised = exc

    _check(
        raised is not None,
        "requests.Timeout now surfaces as InferenceTimeoutError (handler reachable)",
    )
    _check(
        raised is not None and f"{_TIMEOUT_SECONDS}s" in str(raised),
        f"the raised error names the configured timeout ({_TIMEOUT_SECONDS}s)",
    )


def test_model_loaded_is_required_for_availability() -> None:
    provider = LMStudioProvider("http://localhost:1234/v1", "test-model", _TIMEOUT_SECONDS)
    unavailable_session = _ModelsSession({"data": []})
    provider.session = unavailable_session  # type: ignore[assignment]

    unavailable = provider.validate_availability()
    unavailable_error = unavailable.get("error")
    _check(
        unavailable.get("action_status") == ActionStatus.ERROR.value,
        "a reachable LM Studio with no loaded model is not available",
    )
    _check(
        isinstance(unavailable_error, dict)
        and "Configured model 'test-model' is not loaded" in unavailable_error.get("message", ""),
        "the no-model readiness error names the configured model",
    )
    _check(
        unavailable_session.calls == [("http://localhost:1234/v1/models", {"timeout": 2})],
        "availability checks the OpenAI-compatible models endpoint",
    )

    ready_session = _ModelsSession({"data": [{"id": "test-model"}]})
    provider.session = ready_session  # type: ignore[assignment]
    available = provider.validate_availability()
    _check(
        available.get("action_status") == ActionStatus.COMPLETED.value,
        "availability succeeds only when the configured model is advertised as loaded",
    )


def main() -> int:
    print("=== timeout_wiring_smoke (lm_studio_provider dead-timeout fix 2026-07-01) ===")
    test_post_carries_configured_timeout()
    test_session_timeout_surfaces_as_inference_timeout_error()
    test_model_loaded_is_required_for_availability()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
