"""Core AI adapter, imported only when the runtime is explicitly prepared."""

import importlib
import logging
from pathlib import Path
from typing import Any

import numpy as np

from .contracts import BUCKETS, DIMENSION, EmbeddingError, ErrorCode
from .tokenization import EncodedInput

logger = logging.getLogger(__name__)


class NativeModel:
    """One native model and its functions, confined to the runtime worker."""

    def __init__(self) -> None:
        self._model: Any = None
        self._path: Path | None = None
        self._functions: dict[int, Any] = {}
        self._profiler: Any = None
        self.compute_preference = "gpu"
        self.observed_compute_unit = "unknown"
        self.compute_evidence: set[str] = set()
        self.fallback_reason: str | None = None

    def _observe(self, event: Any) -> None:
        name = str(event.event_id)
        if name == "call.MPSGraph":
            self.compute_evidence.add(name)
            self.observed_compute_unit = "gpu"

    async def load(self, path: Path, preference: str) -> None:
        """Try GPU, with an explicit logged CPU-only fallback on load failure."""
        sdk = importlib.import_module("coreai.runtime")
        AIModel, ComputeUnitKind = sdk.AIModel, sdk.ComputeUnitKind
        Profiler, SpecializationOptions = sdk.Profiler, sdk.SpecializationOptions
        if not SpecializationOptions.is_supported():
            raise EmbeddingError(ErrorCode.UNAVAILABLE, "OS Core AI specialization is unavailable")

        self._path = path
        self.compute_preference = preference
        self._profiler = Profiler(
            on_log_event=self._observe,
            on_log_event_begin=lambda event: (self._observe(event) or 0),
            on_log_event_end=lambda event, _interval: self._observe(event),
        )
        options = SpecializationOptions.cpu_only()
        if preference == "gpu":
            options = SpecializationOptions.from_preferred_compute_unit_kind(ComputeUnitKind.gpu())
        try:
            self._model = await AIModel.load(path, specialization_options=options)
            self._load_functions()
        except EmbeddingError:
            raise
        except RuntimeError as exc:
            if preference == "cpu":
                raise
            self.fallback_reason = str(exc)
            logger.warning("Core AI GPU load failed; using CPU-only specialization: %s", exc)
            self._model = await AIModel.load(path, specialization_options=SpecializationOptions.cpu_only())
            self._load_functions()
            self.compute_preference = "cpu"
        if self.compute_preference == "cpu":
            self.compute_evidence.add("SpecializationOptions.cpu_only")

    def _load_functions(self) -> None:
        required = {f"seq{size}" for size in BUCKETS}
        if not required.issubset(self._model.function_names):
            raise EmbeddingError(ErrorCode.ASSET_CORRUPT, "Model lacks required bucket functions")
        self._functions = {
            size: self._model.load_function(f"seq{size}", profiler=self._profiler)
            for size in BUCKETS
        }

    async def embed(self, encoded: EncodedInput) -> list[float]:
        """Validate shape, dtype, finiteness and norm before returning a vector."""
        NDArray = importlib.import_module("coreai.runtime").NDArray

        tensors = {
            "input_ids": NDArray(np.asarray([encoded.input_ids], dtype=np.int32)),
            "attention_mask": NDArray(np.asarray([encoded.attention_mask], dtype=np.int32)),
        }
        try:
            output = await self._functions[encoded.bucket](tensors)
        except RuntimeError as exc:
            if self.compute_preference != "gpu" or self._path is None:
                raise
            reason = str(exc)
            logger.warning("Core AI GPU inference failed; retrying CPU-only: %s", reason)
            await self.load(self._path, "cpu")
            self.fallback_reason = reason
            self.compute_evidence.clear()
            self.compute_evidence.add("SpecializationOptions.cpu_only")
            output = await self._functions[encoded.bucket](tensors)
        vector = np.asarray(output["embedding"].numpy())
        if vector.shape != (1, DIMENSION) or vector.dtype != np.float32:
            raise EmbeddingError(ErrorCode.INVALID_OUTPUT, "Expected float32 embedding [1,768]")
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(vector).all() or abs(norm - 1.0) > 0.01:
            raise EmbeddingError(ErrorCode.INVALID_OUTPUT, "Embedding is nonfinite or not normalized")
        if self.compute_preference == "cpu":
            self.observed_compute_unit = "cpu"
        return [float(value) for value in vector[0] / norm]
