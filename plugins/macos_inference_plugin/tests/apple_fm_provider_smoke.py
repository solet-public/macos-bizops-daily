#!/usr/bin/env python3
"""Apple FM provider contract without a model, Xcode, or network."""

from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "macos_inference_plugin" / "src"))

from ananta.core.plugins.plugin_contracts import ActionStatus  # noqa: E402
from ananta.interfaces import (  # noqa: E402
    InferenceRequest,
    InferenceServiceUnavailableError,
    InferenceValidationError,
)
from macos_inference_plugin.providers.apple_fm_provider import (  # noqa: E402
    AppleFMProvider,
)


class _Reason(Enum):
    DEVICE_NOT_ELIGIBLE = 1


class _SDKError(Exception):
    pass


class _FakeModel:
    context_size = 2048

    def __init__(self) -> None:
        self.available = True

    def is_available(self) -> tuple[bool, _Reason | None]:
        return self.available, None if self.available else _Reason.DEVICE_NOT_ELIGIBLE

    async def token_count(
        self, value: str | None = None, *, instructions: str | None = None
    ) -> int:
        assert (value is None) != (instructions is None)
        return len(value if value is not None else instructions or "")


class _FakeOptions:
    def __init__(self, *, temperature: float, maximum_response_tokens: int) -> None:
        self.temperature = temperature
        self.maximum_response_tokens = maximum_response_tokens


class _GuidedResponse:
    @staticmethod
    def to_json() -> str:
        return json.dumps({"leading_nonce": "test-nonce", "summary": "A factual summary."})


class _FakeSession:
    calls: list[tuple[str, dict[str, Any] | None, _FakeOptions]] = []

    def __init__(self, *, instructions: str | None, model: _FakeModel) -> None:
        self.instructions = instructions
        self.model = model

    async def respond(
        self, prompt: str, *, json_schema: dict[str, Any] | None = None,
        options: _FakeOptions,
    ) -> str | _GuidedResponse:
        self.calls.append((prompt, json_schema, options))
        return _GuidedResponse() if json_schema is not None else "A factual summary."


def _provider(model: _FakeModel) -> AppleFMProvider:
    sdk = SimpleNamespace(
        SystemLanguageModel=lambda: model,
        LanguageModelSession=_FakeSession,
        GenerationOptions=_FakeOptions,
        FoundationModelsError=_SDKError,
        ExceededContextWindowSizeError=_SDKError,
        InvalidGenerationSchemaError=_SDKError,
        UnsupportedGuideError=_SDKError,
        AssetsUnavailableError=_SDKError,
        RateLimitedError=_SDKError,
    )
    provider = AppleFMProvider(timeout_seconds=2)
    provider._load_sdk = lambda: sdk  # type: ignore[method-assign]
    return provider


def _request(
    text: str, *, schema: dict[str, Any] | None = None, purpose: str | None = None
) -> InferenceRequest:
    return InferenceRequest(
        prompt=[
            {"role": "system", "content": "State only facts from the source."},
            {"role": "user", "content": text},
        ],
        temperature=0.1,
        max_tokens=160,
        response_schema=schema,
        use_structured_output=schema is not None,
        context_metadata={"purpose": purpose} if purpose else None,
    )


def _check_prose(provider: AppleFMProvider) -> None:
    ready = provider.validate_availability()
    assert ready["action_status"] == ActionStatus.COMPLETED.value
    assert ready["data"]["context_size"] == 2048

    prose = provider.generate_completion(_request("Apple FM summarizes the record."))
    prose_result = prose["data"]["result"]
    assert isinstance(prose_result, dict)
    assert prose_result["completion"] == "A factual summary."
    assert prose_result["usage"]["input_tokens"] > 0
    assert _FakeSession.calls[-1][2].temperature == 0.1
    assert _FakeSession.calls[-1][2].maximum_response_tokens == 160


def _check_guided(provider: AppleFMProvider) -> None:
    schema: dict[str, Any] = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["leading_nonce", "summary"],
        "properties": {
            "leading_nonce": {"type": "string"},
            "summary": {"type": "string"},
        },
        "additionalProperties": False,
    }
    guided = provider.generate_completion(_request("Summarize the migration.", schema=schema))
    guided_result = guided["data"]["result"]
    assert isinstance(guided_result, dict)
    assert json.loads(str(guided_result["completion"])) == {
        "leading_nonce": "test-nonce", "summary": "A factual summary."
    }
    adapted = _FakeSession.calls[-1][1]
    assert isinstance(adapted, dict)
    assert adapted["title"] == "Response"
    assert adapted["x-order"] == ["leading_nonce", "summary"]
    assert "$schema" not in adapted


def _check_context_budget(provider: AppleFMProvider) -> None:
    long_source = "Older record. " * 300
    try:
        provider.generate_completion(_request(long_source))
    except InferenceValidationError as exc:
        assert "caller must chunk" in str(exc)
    else:
        raise AssertionError("long non-summary input was accepted")
    clipped = provider.generate_completion(
        _request(long_source, purpose="session_ledger_auto_summarize")
    )
    clipped_result = clipped["data"]["result"]
    assert isinstance(clipped_result, dict)
    assert clipped_result["input_truncated"] is True
    assert clipped_result["omitted_input_chars"] > 0
    assert clipped_result["usage"]["input_tokens"] < clipped_result["original_input_tokens"]
    assert "Earlier transcript omitted" in _FakeSession.calls[-1][0]


def _check_unavailable(provider: AppleFMProvider, model: _FakeModel) -> None:
    model.available = False
    unavailable = provider.validate_availability()
    assert unavailable["action_status"] == ActionStatus.ERROR.value
    assert unavailable["data"]["reason"] == "DEVICE_NOT_ELIGIBLE"
    assert unavailable["error"]["severity"] == "WARNING"
    try:
        provider.generate_completion(_request("Summarize this."))
    except InferenceServiceUnavailableError as exc:
        assert "DEVICE_NOT_ELIGIBLE" in str(exc)
    else:
        raise AssertionError("unavailable model produced a completion")


def main() -> int:
    _FakeSession.calls.clear()
    model = _FakeModel()
    provider = _provider(model)
    _check_prose(provider)
    _check_guided(provider)
    _check_context_budget(provider)
    _check_unavailable(provider, model)
    print("apple_fm_provider_smoke: prose, guided output, budgets, and unavailable reason pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
