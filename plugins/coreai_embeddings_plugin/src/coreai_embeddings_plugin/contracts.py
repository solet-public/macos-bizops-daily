"""Stable runtime dimensions, errors, and service result envelopes."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ananta.core.domain.types import ActionResult

MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
DIMENSION = 768
BUCKETS = (256, 512, 1024, 2048)


class ErrorCode(StrEnum):
    """Machine-readable failures; unavailable models never yield vectors."""

    INVALID_INPUT = "coreai_embeddings.invalid_input"
    UNSUPPORTED_MODEL = "coreai_embeddings.unsupported_model"
    INPUT_TOO_LONG = "coreai_embeddings.input_too_long"
    ASSET_MISSING = "coreai_embeddings.asset_missing"
    ASSET_CORRUPT = "coreai_embeddings.asset_corrupt"
    UNAVAILABLE = "coreai_embeddings.unavailable"
    TIMEOUT = "coreai_embeddings.operation_timeout"
    INVALID_OUTPUT = "coreai_embeddings.invalid_output"
    INFERENCE_FAILED = "coreai_embeddings.inference_failed"


class EmbeddingError(RuntimeError):
    """An expected runtime failure that maps to an ActionResult."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def result(data: dict[str, Any]) -> ActionResult:
    """Return the synchronous embedding service envelope."""
    return {
        "action_status": "completed", "data": {"result": data},
        "actions": [], "error": None, "timestamp": datetime.now(UTC).isoformat(),
    }


def failure(error: EmbeddingError) -> ActionResult:
    """Return a typed failure without a partial embedding batch."""
    stamp = datetime.now(UTC).isoformat()
    return {
        "action_status": "error", "actions": [], "timestamp": stamp,
        "error": {"type": "CoreAIEmbeddingsError", "code": error.code.value,
                  "message": str(error), "details": {}, "severity": "error",
                  "timestamp": stamp},
    }
