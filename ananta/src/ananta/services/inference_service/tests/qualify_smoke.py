#!/usr/bin/env python3
"""Hermetic contract smoke for ``inference_service.qualify``.

The production verb makes one small structured provider request.  This smoke
uses a recorded provider result so it never contacts a live model.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.services.inference_service import InferenceService  # noqa: E402

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


class _RecordedProvider:
    """Provider double returning the recorded minimal structured response."""

    def __init__(self, completion: str = '{"qualified":true}') -> None:
        self.completion = completion
        self.requests: list[Any] = []
        self.readiness_error: str | None = None

    def is_ready(self) -> bool:
        return True

    def get_readiness_error(self) -> str | None:
        return self.readiness_error

    def get_configured_model_name(self) -> str:
        return "recorded-model"

    def validate_availability(self) -> dict[str, object]:
        raise AssertionError("not used by qualify")

    def get_model_info(self) -> dict[str, object]:
        raise AssertionError("not used by qualify")

    def get_inference_defaults(self) -> object:
        raise AssertionError("not used by qualify")

    def propose_name(
        self, params: dict[str, object], state: dict[str, object],
    ) -> dict[str, object]:
        raise AssertionError("not used by qualify")

    def generate_completion(self, request: Any) -> dict[str, object]:
        self.requests.append(request)
        return {
            "action_status": "completed",
            "data": {"result": {"completion": self.completion}},
            "error": None,
        }


class _PluginManager:
    def __init__(self, provider: _RecordedProvider) -> None:
        self.provider = provider

    def get_plugin(self, name: str) -> _RecordedProvider:
        if name != "recorded_provider":
            raise AssertionError(f"unexpected plugin: {name}")
        return self.provider


def _service(provider: _RecordedProvider) -> InferenceService:
    return InferenceService(
        plugin_manager=_PluginManager(provider),  # type: ignore[arg-type]
        inference_plugin_name="recorded_provider",
    )


def _success_case() -> None:
    provider = _RecordedProvider()
    result = _service(provider).qualify({}, {})
    data = result.get("data", {})
    _check(result.get("action_status") == "completed", "Q1 request completes")
    _check(data == {
        "provider": "recorded_provider",
        "model": "recorded-model",
        "completed": True,
        "structured_result_valid": True,
    }, "Q2 response is the bounded public shape only")
    _check(len(provider.requests) == 1, "Q3 exactly one provider request")
    request = provider.requests[0]
    _check(request.max_tokens <= 16 and request.temperature == 0.0, "Q4 request is bounded")
    _check(request.use_structured_output is True, "Q5 request requires structured output")


def _invalid_structure_case() -> None:
    result = _service(_RecordedProvider('{"unexpected":true}')).qualify({}, {})
    data = result.get("data", {})
    _check(
        result.get("action_status") == "completed"
        and data.get("completed") is True
        and data.get("structured_result_valid") is False,
        "Q6 invalid structured response is reported without provider payload",
    )


def main() -> int:
    print("Inference qualifier smoke:")
    _success_case()
    _invalid_structure_case()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        print("Failures: " + "; ".join(_failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
