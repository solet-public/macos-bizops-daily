"""Nomic v1.5 synchronous embedding service for macOS 27."""

import logging
import threading
from pathlib import Path
from typing import Any

from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.embedding_service_interface import EmbeddingServiceInterface

from .contracts import DIMENSION, MODEL_ID, EmbeddingError, ErrorCode, failure, result
from .runtime import EmbeddingRuntime
from .tokenization import validate_inputs

logger = logging.getLogger(__name__)


class CoreAIEmbeddingsPlugin(PluginBase, EmbeddingServiceInterface):
    """Keep model availability truthful and permit explicit preparation after repair."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.name = "coreai_embeddings_plugin"
        self.config: dict[str, Any] = dict(config or {})
        self._runtime: EmbeddingRuntime | None = None
        self._lock = threading.RLock()
        self._last_error = EmbeddingError(ErrorCode.UNAVAILABLE, "Plugin not prepared")
        self.set_error(str(self._last_error))

    @property
    def service_interfaces(self) -> tuple[type, ...]:
        return (EmbeddingServiceInterface,)

    @property
    def supported_interface_versions(self) -> dict[type, str]:
        return {EmbeddingServiceInterface: EmbeddingServiceInterface.INTERFACE_VERSION}

    def initialize(self, config: dict[str, object]) -> None:
        """Adopt new configuration and re-prepare with it immediately.

        Fresh boot runs ``prepare_for_readiness()`` (platform Phase 1) before
        this method ever sees the real config, and this plugin is not
        ``LifecycleManaged`` so nothing downstream re-invokes
        ``prepare_for_readiness()`` on its behalf afterward — the same is
        true of ``reload_plugin_config``, which also calls only this method.
        Re-preparing here, rather than only invalidating, is what lets the
        plugin actually reach ready on the config this method received.
        """
        with self._lock:
            self.config = dict(config)
            self.prepare_for_readiness()

    def prepare_for_readiness(self) -> None:
        """Missing assets leave an honest warning and a retryable preparation path."""
        with self._lock:
            self._close()
            try:
                root, preference = self._settings()
                self._runtime = EmbeddingRuntime(root, preference)
                self._runtime.prepare()
            except EmbeddingError as exc:
                self._unavailable(exc)
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                self._unavailable(EmbeddingError(ErrorCode.UNAVAILABLE, str(exc)))
            else:
                self.set_ready()

    def _settings(self) -> tuple[Path, str]:
        root = self.config.get("asset_root")
        preference = self.config.get("compute_preference", "gpu")
        if not isinstance(root, str) or not root.strip() or not Path(root).is_absolute():
            raise EmbeddingError(ErrorCode.UNAVAILABLE, "asset_root must be an absolute directory path")
        if preference not in ("gpu", "cpu"):
            raise EmbeddingError(ErrorCode.UNAVAILABLE, "compute_preference must be gpu or cpu")
        return Path(root), preference

    def _unavailable(self, error: EmbeddingError) -> None:
        self._last_error = error
        self.set_error(str(error))
        self._close()
        logger.warning("Core AI embedding capability unavailable: %s. Repair assets/configuration and prepare again.", error)

    def _close(self) -> None:
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None

    def generate_embeddings(
        self, inputs: list[str], model: str | None = None, input_type: str = "text",
    ) -> ActionResult:
        """Preserve caller text and prefixes, and return no partial batch on error."""
        with self._lock:
            try:
                validate_inputs(inputs, model, input_type)
                if len(inputs) > 128:
                    raise EmbeddingError(ErrorCode.INVALID_INPUT, "Maximum batch size is 128")
                if not self.is_ready() or self._runtime is None:
                    raise self._last_error
                vectors = self._runtime.generate(inputs)
                return result({"embeddings": vectors, "dimension": DIMENSION, "model": MODEL_ID})
            except EmbeddingError as exc:
                if exc.code in (ErrorCode.UNAVAILABLE, ErrorCode.INVALID_OUTPUT):
                    self._unavailable(exc)
                return failure(exc)
            except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                error = EmbeddingError(ErrorCode.INFERENCE_FAILED, str(exc))
                self._unavailable(error)
                return failure(error)

    def get_default_dimensions(self) -> int:
        """Static schema-init metadata does not assert runtime availability."""
        return DIMENSION

    def get_embedding_dimension(self, model: str | None = None) -> ActionResult:
        if model is not None and model != MODEL_ID:
            return failure(EmbeddingError(ErrorCode.UNSUPPORTED_MODEL, f"Unsupported model: {model}"))
        return result({"dimension": DIMENSION, "model": MODEL_ID})

    def list_models(self) -> ActionResult:
        return result({"models": [{
            "name": MODEL_ID, "dimension": DIMENSION, "max_input_length": 2048,
            "input_types": ["text"], "description": "Pinned Nomic v1.5, normalized vectors",
            "available": self.is_ready(),
        }]})

    def get_runtime_status(self) -> dict[str, object]:
        """Installer/doctor can distinguish capability absence from readiness."""
        with self._lock:
            details = self._runtime.diagnostics() if self._runtime else {}
            return {
                "ready": self.is_ready(), "reason": self.get_readiness_error(),
                "severity": "ok" if self.is_ready() else "warning",
                "repair": "Verify/install pinned assets at asset_root, then prepare the plugin again",
                **details,
            }

    async def cleanup(self) -> None:
        """Retire the runtime; preparation may reopen it after cleanup."""
        with self._lock:
            self._close()
            self._last_error = EmbeddingError(ErrorCode.UNAVAILABLE, "Plugin closed")
            self.set_error(str(self._last_error))

    def get_config_schema(self) -> dict[str, object]:
        return {
            "type": "object", "additionalProperties": False, "required": ["asset_root"],
            "properties": {
                "asset_root": {"type": "string", "description": "Absolute installed asset root"},
                "compute_preference": {"type": "string", "enum": ["gpu", "cpu"], "default": "gpu"},
            },
        }
