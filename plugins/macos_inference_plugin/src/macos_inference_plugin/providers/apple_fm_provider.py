"""In-process Apple Foundation Models provider for local summaries.

The SDK is imported only when the provider is used. This lets an unavailable
system model be reported as an explicit readiness warning after registration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from ananta.core.domain.types import ActionResult, ErrorDetail
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.interfaces import (
    InferenceRequest,
    InferenceServiceUnavailableError,
    InferenceTimeoutError,
    InferenceValidationError,
)

logger = logging.getLogger(__name__)

_MODEL_NAME = "apple-system"
_MAX_RESPONSE_TOKENS = 2048
_CONTEXT_RESERVE_TOKENS = 256
_SUMMARY_HEAD_CHARS = 1024
_SUMMARY_PURPOSE = "session_ledger_auto_summarize"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _reason_name(reason: object) -> str:
    name = getattr(reason, "name", None)
    return str(name) if isinstance(name, str) and name else "UNKNOWN"


def _adapt_schema(node: object, name: str = "Response") -> object:
    """Add the object metadata required by Apple's generation schema dialect."""
    if isinstance(node, list):
        return [_adapt_schema(value, name) for value in node]
    if not isinstance(node, dict):
        return node

    adapted = {
        key: _adapt_schema(value, str(key))
        for key, value in node.items()
        if key != "$schema"
    }
    if adapted.get("type") == "object":
        properties = adapted.get("properties")
        if not isinstance(properties, dict):
            raise InferenceValidationError("Structured output object has no properties")
        adapted["title"] = adapted.get("title") or name[:1].upper() + name[1:]
        adapted["x-order"] = list(properties)
    return adapted


def _message_parts(role: object, content: object) -> tuple[str, str]:
    if not isinstance(role, str) or not isinstance(content, str) or not content:
        raise InferenceValidationError("Apple FM requires nonempty text messages")
    if role not in {"system", "user", "assistant"}:
        raise InferenceValidationError(
            f"Apple FM does not support message role {role!r} in summary requests"
        )
    return role, content


def _prepare_messages(request: InferenceRequest) -> tuple[str | None, str]:
    instructions: list[str] = []
    turns: list[tuple[str, str]] = []
    for message in request.messages:
        role, content = _message_parts(message.get("role"), message.get("content"))
        if role == "system":
            instructions.append(content)
        else:
            turns.append((role, content))
    if not turns:
        raise InferenceValidationError("Apple FM request has no user or assistant message")
    if len(turns) == 1 and turns[0][0] == "user":
        prompt = turns[0][1]
    else:
        prompt = "\n\n".join(f"{role.title()}: {content}" for role, content in turns)
    return "\n\n".join(instructions) or None, prompt


async def _count_input(model: Any, instructions: str | None, prompt: str) -> int:
    """Count prompt and instructions separately, as required by SDK 0.2.1."""
    count = int(await model.token_count(prompt))
    if instructions:
        count += int(await model.token_count(instructions=instructions))
    return count


def _validate_request(request: InferenceRequest) -> None:
    if request.stop_sequences:
        raise InferenceValidationError("Apple FM does not support stop_sequences")
    if request.max_tokens <= 0:
        raise InferenceValidationError("Apple FM max_tokens must be positive")
    if not math.isfinite(request.temperature) or not 0 <= request.temperature <= 2:
        raise InferenceValidationError("Apple FM temperature must be between 0 and 2")


async def _render_completion(
    session: Any, request: InferenceRequest, prompt: str, options: Any
) -> str:
    if request.use_structured_output:
        schema = request.response_schema or request.STANDARD_OUTPUT_SCHEMA
        response = await session.respond(
            prompt, json_schema=_adapt_schema(schema), options=options
        )
        if not hasattr(response, "to_json"):
            raise InferenceValidationError("Apple FM guided response has no JSON serialization")
        completion = str(response.to_json())
        try:
            parsed = json.loads(completion)
        except json.JSONDecodeError as exc:
            raise InferenceValidationError("Apple FM guided response is invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise InferenceValidationError("Apple FM guided response is not a JSON object")
        return completion
    response = await session.respond(prompt, options=options)
    if not isinstance(response, str):
        raise InferenceValidationError("Apple FM returned non-text for prose request")
    return response


class AppleFMProvider:
    """Synchronous local provider over Apple's asynchronous Python SDK."""

    model = _MODEL_NAME

    def __init__(self, timeout_seconds: int) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Apple FM timeout_seconds must be positive")
        self.timeout = timeout_seconds
        # A timed-out native call may still be winding down. Do not accumulate
        # background model requests on a host whose framework is stalled.
        self._request_slot = threading.BoundedSemaphore(1)

    @staticmethod
    def _load_sdk() -> Any:
        try:
            return import_module("apple_fm_sdk")
        except (ImportError, OSError) as exc:
            raise InferenceServiceUnavailableError(
                f"Apple Foundation Models SDK is not installed or loadable: {exc}",
                details={"provider": "apple_fm", "reason": "SDK_UNAVAILABLE"},
            ) from exc

    def validate_availability(self) -> ActionResult:
        """Observe the system model and return the exact unavailable reason."""
        try:
            sdk = self._load_sdk()
            model = sdk.SystemLanguageModel()
            available, reason = model.is_available()
            context_size = int(model.context_size)
        except InferenceServiceUnavailableError as exc:
            # Initialization failures are reportable readiness states, not a
            # reason for the rest of the application to fail startup.
            reason_name = "SDK_UNAVAILABLE"
            message = str(exc)
            context_size = None
            available = False
        except Exception as exc:
            reason_name = "PROBE_FAILED"
            message = f"Apple Foundation Models availability probe failed: {exc}"
            context_size = None
            available = False
        else:
            reason_name = _reason_name(reason) if not available else "AVAILABLE"
            message = (
                f"Apple Foundation Models unavailable: {reason_name}"
                if not available
                else ""
            )

        data: dict[str, object] = {
            "available": bool(available),
            "provider": "apple_fm",
            "model": self.model,
            "reason": reason_name,
            "context_size": context_size,
            "repair_instruction": (
                "Enable Apple Intelligence in System Settings and allow the system model "
                "to download on an eligible macOS 27 Apple Silicon host. Install the verified "
                "vendored SDK wheel if SDK_UNAVAILABLE; rerun availability after repair."
                if not available else None
            ),
        }
        if available:
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": data,
                "error": None,
                "timestamp": _timestamp(),
            }
        error: ErrorDetail = {
            "type": "ModelUnavailable",
            "code": "apple_fm.model_unavailable",
            "message": message,
            "details": {"reason": reason_name},
            "severity": "WARNING",
            "timestamp": _timestamp(),
        }
        return {
            "action_status": ActionStatus.ERROR.value,
            "data": data,
            "error": error,
            "timestamp": _timestamp(),
        }

    async def _fit_summary_prompt(
        self,
        model: Any,
        instructions: str | None,
        prompt: str,
        budget: int,
    ) -> tuple[str, int, int]:
        """Keep the summary instruction and recent transcript within the model window."""
        original_tokens = await _count_input(model, instructions, prompt)
        if original_tokens <= budget:
            return prompt, original_tokens, 0

        prefix = prompt[:_SUMMARY_HEAD_CHARS]
        low, high = 0, len(prompt) - len(prefix)
        selected: tuple[str, int, int] | None = None
        while low <= high:
            tail_chars = (low + high) // 2
            omitted = len(prompt) - len(prefix) - tail_chars
            candidate = (
                f"{prefix}\n\n[Earlier transcript omitted: {omitted} characters "
                "to fit the Apple model context.]\n\n"
                f"{prompt[-tail_chars:] if tail_chars else ''}"
            )
            count = await _count_input(model, instructions, candidate)
            if count <= budget:
                selected = candidate, count, omitted
                low = tail_chars + 1
            else:
                high = tail_chars - 1
        if selected is None:
            raise InferenceValidationError(
                "Summary instructions alone exceed the Apple model input budget",
                details={"input_tokens": original_tokens, "input_budget": budget},
            )
        return selected

    async def _generate_async(
        self, sdk: Any, request: InferenceRequest
    ) -> tuple[str, int, int, int, int, int, int]:
        model = sdk.SystemLanguageModel()
        available, reason = model.is_available()
        if not available:
            reason_name = _reason_name(reason)
            raise InferenceServiceUnavailableError(
                f"Apple Foundation Models unavailable: {reason_name}",
                details={"provider": "apple_fm", "reason": reason_name},
            )
        _validate_request(request)

        instructions, prompt = _prepare_messages(request)
        context_size = int(model.context_size)
        response_limit = min(request.max_tokens, _MAX_RESPONSE_TOKENS)
        input_budget = context_size - response_limit - _CONTEXT_RESERVE_TOKENS
        if input_budget <= 0:
            raise InferenceValidationError(
                "Apple FM context is too small for the requested response budget",
                details={"context_size": context_size, "max_tokens": response_limit},
            )
        original_tokens = await _count_input(model, instructions, prompt)
        omitted_chars = 0
        input_tokens = original_tokens
        if original_tokens > input_budget:
            if request.context_metadata.get("purpose") != _SUMMARY_PURPOSE:
                raise InferenceValidationError(
                    "Apple FM input exceeds its context; caller must chunk the source",
                    details={
                        "input_tokens": original_tokens,
                        "input_budget": input_budget,
                        "context_size": context_size,
                    },
                )
            prompt, input_tokens, omitted_chars = await self._fit_summary_prompt(
                model, instructions, prompt, input_budget
            )
        options = sdk.GenerationOptions(
            temperature=request.temperature,
            maximum_response_tokens=response_limit,
        )
        session = sdk.LanguageModelSession(instructions=instructions, model=model)
        completion = await _render_completion(session, request, prompt, options)
        if not completion.strip():
            raise InferenceValidationError("Apple FM returned an empty completion")
        output_tokens = int(await model.token_count(completion))
        if output_tokens >= response_limit:
            raise InferenceValidationError(
                "Apple FM response reached the output token limit",
                details={"output_tokens": output_tokens, "max_tokens": response_limit},
            )
        return (
            completion, input_tokens, output_tokens, omitted_chars, response_limit, context_size,
            original_tokens
        )

    def generate_completion(self, request: InferenceRequest) -> ActionResult:
        """Generate one local completion, with bounded sync-to-async bridging."""
        sdk = self._load_sdk()
        if not self._request_slot.acquire(timeout=self.timeout):
            raise InferenceTimeoutError(
                "Apple FM is still serving a previous request",
                details={"timeout_seconds": self.timeout},
            )
        future: Future[tuple[str, int, int, int, int, int, int]] = Future()

        def run() -> None:
            try:
                result = asyncio.run(
                    asyncio.wait_for(self._generate_async(sdk, request), timeout=self.timeout)
                )
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
            finally:
                self._request_slot.release()

        start = time.monotonic()
        threading.Thread(target=run, name="apple-fm-request", daemon=True).start()
        try:
            (
                completion, input_tokens, output_tokens, omitted_chars, response_limit, context_size,
                original_tokens
            ) = future.result(timeout=self.timeout + 1)
        except FutureTimeoutError as exc:
            raise InferenceTimeoutError(
                f"Apple FM request exceeded {self.timeout}s",
                details={"timeout_seconds": self.timeout},
            ) from exc
        except sdk.ExceededContextWindowSizeError as exc:
            raise InferenceValidationError(
                "Apple FM context window exceeded",
                details={"model": self.model},
            ) from exc
        except (sdk.InvalidGenerationSchemaError, sdk.UnsupportedGuideError) as exc:
            raise InferenceValidationError(
                f"Apple FM guided output schema is unsupported: {exc}"
            ) from exc
        except (sdk.AssetsUnavailableError, sdk.RateLimitedError) as exc:
            raise InferenceServiceUnavailableError(
                f"Apple FM model is temporarily unavailable: {exc}",
                details={"provider": "apple_fm", "reason": type(exc).__name__},
            ) from exc
        except sdk.FoundationModelsError as exc:
            raise InferenceValidationError(
                f"Apple FM generation failed: {exc}",
                details={"reason": type(exc).__name__},
            ) from exc

        latency_ms = (time.monotonic() - start) * 1000
        logger.info(
            "Apple FM inference complete: input_tokens=%s output_limit=%s "
            "omitted_chars=%s latency_ms=%.0f",
            input_tokens, response_limit, omitted_chars, latency_ms,
        )
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "result": {
                    "completion": completion,
                    "model": self.model,
                    "provider": "apple_fm",
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                    },
                    "finish_reason": "stop",
                    "latency_ms": latency_ms,
                    "original_input_tokens": original_tokens,
                    "input_truncated": omitted_chars > 0,
                    "omitted_input_chars": omitted_chars,
                    "max_response_tokens": response_limit,
                    "context_size": context_size,
                }
            },
            "error": None,
            "timestamp": _timestamp(),
        }

    def get_model_info(self) -> ActionResult:
        """Return the actual system context size and current availability."""
        availability = self.validate_availability()
        data = availability.get("data", {})
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "model_name": self.model,
                "provider": "apple_fm",
                "available": data.get("available", False),
                "unavailable_reason": data.get("reason"),
                "capabilities": {
                    "max_context_tokens": data.get("context_size"),
                    "supports_streaming": False,
                    "supports_function_calling": False,
                    "supports_cache_warming": False,
                    "supports_guided_summaries": True,
                },
                "cost_per_1k_tokens": {"input": None, "output": None},
            },
            "error": None,
            "timestamp": _timestamp(),
        }
