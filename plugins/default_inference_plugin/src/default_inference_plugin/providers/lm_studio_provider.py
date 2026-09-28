"""LM Studio provider - OpenAI-compatible local inference."""

import json
import logging
import time
from datetime import UTC, datetime

import requests
from ananta.core.domain import ActionStatus
from ananta.core.domain.types import ActionResult, ErrorDetail
from ananta.interfaces import (
    InferenceError,
    InferenceRequest,
    InferenceServiceUnavailableError,
    InferenceTimeoutError,
    InferenceValidationError,
)

logger = logging.getLogger(__name__)


class LMStudioProvider:
    """LM Studio provider via OpenAI-compatible API.

    No fallback. No retry. Fails fast on error.
    """

    def __init__(self, base_url: str, model: str, timeout_seconds: int):
        self.base_url = base_url
        self.model = model
        self.timeout = timeout_seconds

        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

        logger.debug(f"LMStudioProvider initialized: {base_url}, model={model}")

    @staticmethod
    def _estimate_cache_status(
        prompt_tokens: int, completion_tokens: int, latency_ms: float
    ) -> tuple[str, float]:
        estimated_completion_ms = completion_tokens * 40
        prompt_processing_ms = max(1, latency_ms - estimated_completion_ms)
        prompt_tok_per_sec = prompt_tokens / prompt_processing_ms * 1000 if prompt_tokens else 0.0
        if prompt_tok_per_sec > 5000:
            cache_status = "HIT"
        elif prompt_tok_per_sec < 1000:
            cache_status = "MISS"
        else:
            cache_status = "PARTIAL"
        return cache_status, prompt_tok_per_sec

    @staticmethod
    def _check_truncation(
        finish_reason: str, completion_tokens: int, model: str
    ) -> None:
        if finish_reason == "length":
            raise InferenceValidationError(
                f"Response truncated: model hit token limit "
                f"({completion_tokens} output tokens). "
                f"The response is incomplete and cannot be parsed.",
                details={
                    "finish_reason": finish_reason,
                    "output_tokens": completion_tokens,
                    "model": model,
                },
            )

    def generate_completion(self, request: InferenceRequest) -> ActionResult:
        """Generate completion via LM Studio. NO RETRY."""

        payload = request.to_openai_format()
        payload["model"] = self.model

        # Log whether structured output is being used
        has_response_format = "response_format" in payload
        logger.debug(
            "INFERENCE_REQUEST",
            extra={
                "provider": "lm_studio",
                "model": self.model,
                "session_id": request.context_metadata.get("session_id"),
                "flow_id": request.context_metadata.get("flow_id"),
                "has_response_format": has_response_format,
                "response_format_type": payload.get("response_format", {}).get("type"),
            },
        )

        # Full request as a curl-compatible line, DEBUG-gated: the payload is
        # the ENTIRE conversation, so emitting it at INFO floods the default
        # log (each line is multi-KB) AND lands conversation content in the
        # log file. The structured INFERENCE_REQUEST record above already
        # carries the INFO-level metadata (provider/model/session/flow); the
        # full curl reproduction is a DEBUG-only debugging aid.
        endpoint = f"{self.base_url}/chat/completions"
        logger.debug(
            f"INFERENCE REQUEST:\n"
            f"curl -X POST {endpoint} \\\n"
            f"  -H 'Content-Type: application/json' \\\n"
            f"  -d '{json.dumps(payload)}'"
        )

        response: requests.Response | None = None
        try:
            start_time = time.time()
            response = self.session.post(
                endpoint,
                json=payload,
                timeout=self.timeout,
            )
            latency_ms = (time.time() - start_time) * 1000

            response.raise_for_status()
            data = response.json()

            completion = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            finish_reason = data["choices"][0].get("finish_reason", "stop")
            completion_tokens = usage.get("completion_tokens", 0)

            self._check_truncation(finish_reason, completion_tokens, self.model)

            prompt_tokens = usage.get("prompt_tokens", 0)
            cache_status, prompt_tok_per_sec = self._estimate_cache_status(
                prompt_tokens, completion_tokens, latency_ms
            )

            # Log full response for debugging
            logger.info(
                f"INFERENCE RESPONSE:\n"
                f"Model: {self.model} | Tokens: {prompt_tokens} in / "
                f"{completion_tokens} out | {latency_ms:.0f}ms | "
                f"Cache: {cache_status} ({prompt_tok_per_sec:.0f} tok/s)\n"
                f"{completion}"
            )

            logger.debug(
                "INFERENCE_COMPLETE",
                extra={
                    "provider": "lm_studio",
                    "model": self.model,
                    "input_tokens": prompt_tokens,
                    "output_tokens": completion_tokens,
                    "latency_ms": latency_ms,
                    "finish_reason": finish_reason,
                    "cache_status": cache_status,
                    "prompt_tok_per_sec": round(prompt_tok_per_sec),
                },
            )

            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {
                    "result": {
                        "completion": completion,
                        "model": self.model,
                        "provider": "lm_studio",
                        "usage": {
                            "input_tokens": usage.get("prompt_tokens", 0),
                            "output_tokens": usage.get("completion_tokens", 0),
                            "total_tokens": usage.get("total_tokens", 0),
                        },
                        "finish_reason": finish_reason,
                        "latency_ms": latency_ms,
                    }
                },
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }

        except requests.exceptions.ConnectionError as e:
            logger.error(f"LM Studio connection failed: {e}")
            raise InferenceServiceUnavailableError(
                f"LM Studio unavailable at {self.base_url}: {e}",
                details={
                    "base_url": self.base_url,
                    "model": self.model,
                    "error": str(e),
                },
            ) from e

        except requests.exceptions.Timeout as e:
            logger.error(f"LM Studio timeout after {self.timeout}s: {e}")
            raise InferenceTimeoutError(
                f"LM Studio timeout after {self.timeout}s: {e}",
                details={"timeout_seconds": self.timeout},
            ) from e

        except requests.exceptions.HTTPError as e:
            assert response is not None  # HTTPError only raised after response received
            status_code = response.status_code
            logger.error(
                "LM Studio HTTP error %s: %s; body=%s",
                status_code, e, response.text[:500],
            )

            if 400 <= status_code < 500:
                raise InferenceValidationError(
                    f"LM Studio request invalid (HTTP {status_code}): {e}",
                    details={
                        "status_code": status_code,
                        "response": response.text[:500],
                    },
                ) from e
            else:
                raise InferenceServiceUnavailableError(
                    f"LM Studio server error (HTTP {status_code}): {e}",
                    details={"status_code": status_code},
                ) from e

        except InferenceError:
            # Already classified (e.g. truncation) — propagate without re-wrapping.
            raise

        except Exception as e:
            logger.error(f"Unexpected LM Studio error: {e}", exc_info=True)
            raise InferenceServiceUnavailableError(
                f"LM Studio inference failed: {e}",
                details={"error": str(e), "type": type(e).__name__},
            ) from e

    def validate_availability(self) -> ActionResult:
        """Quick health check (<2s)."""
        try:
            response = self.session.get(f"{self.base_url}/models", timeout=2)
            response.raise_for_status()
            models_data = response.json()
            loaded_models = self._loaded_model_ids(models_data)
            if self.model not in loaded_models:
                raise ValueError(
                    f"Configured model '{self.model}' is not loaded; "
                    f"loaded models: {sorted(loaded_models)!r}"
                )

            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {
                    "available": True,
                    "provider": "lm_studio",
                    "model": self.model,
                    "endpoint": self.base_url,
                    "models": models_data,
                },
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        except Exception as e:
            logger.error(f"LM Studio health check failed: {e}")
            error_detail: ErrorDetail = {
                "type": "HealthCheckError",
                "code": "lm_studio.health_check_failed",
                "message": str(e),
                "details": {},
                "severity": "ERROR",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            return {
                "action_status": ActionStatus.ERROR.value,
                "data": {
                    "available": False,
                    "provider": "lm_studio",
                    "model": self.model,
                    "endpoint": self.base_url,
                },
                "error": error_detail,
                "timestamp": datetime.now(UTC).isoformat(),
            }

    @staticmethod
    def _loaded_model_ids(models_data: object) -> set[str]:
        """Return the OpenAI-compatible model IDs advertised by LM Studio."""
        if not isinstance(models_data, dict):
            raise ValueError("LM Studio /models response must be an object")
        raw_models = models_data.get("data")
        if not isinstance(raw_models, list):
            raise ValueError("LM Studio /models response is missing its data list")
        return {
            model_id
            for item in raw_models
            if isinstance(item, dict)
            and isinstance((model_id := item.get("id")), str)
            and model_id
        }

    def get_model_info(self) -> ActionResult:
        """Get model information."""
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "model_name": self.model,
                "provider": "lm_studio",
                "endpoint": self.base_url,
                "capabilities": {
                    "max_context_tokens": 8192,
                    "supports_streaming": False,
                    "supports_function_calling": False,
                },
                "cost_per_1k_tokens": {
                    "input": None,
                    "output": None,
                },
            },
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }
