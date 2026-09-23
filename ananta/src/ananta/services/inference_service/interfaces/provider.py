"""InferenceProvider — narrow provider contract for LLM inference.

A replacement inference plugin must implement these methods.
All transaction orchestration (plan advancement, prompt assembly,
action parsing, event persistence) lives in ``InferenceService``.

Signatures match the ``DefaultInferencePlugin`` implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ananta.core.domain.types import ActionResult
from ananta.interfaces.inference_service_interface import InferenceRequest


@dataclass(frozen=True, slots=True)
class InferenceDefaults:
    """Provider-configured inference defaults.

    Returned by ``InferenceProvider.get_inference_defaults``.
    The platform uses these for inference parameter resolution —
    the provider owns the values, the platform owns the logic.
    """

    temperature: float
    max_tokens: int
    action_vertex_temperature: float
    action_vertex_max_tokens: int


@runtime_checkable
class InferenceProvider(Protocol):
    """Provider contract for LLM inference.

    Methods a replacement inference plugin must provide.
    Everything else is platform-owned via ``InferenceService``.
    """

    def generate_completion(self, request: InferenceRequest) -> ActionResult: ...
    def validate_availability(self) -> ActionResult: ...
    def get_model_info(self) -> ActionResult: ...
    def get_configured_model_name(self) -> str: ...
    def get_inference_defaults(self) -> InferenceDefaults: ...
    def propose_name(
        self, params: dict[str, Any], state: dict[str, Any],
    ) -> ActionResult: ...
    def is_ready(self) -> bool: ...
    def get_readiness_error(self) -> str | None: ...


# Set outside class body so it is NOT included in __protocol_attrs__
# (Protocol isinstance checks all names defined in the body).
InferenceProvider.INTERFACE_VERSION = "1.0.0"  # type: ignore[attr-defined]
