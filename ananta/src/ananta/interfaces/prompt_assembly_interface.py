"""Prompt Assembly Service Interface.

Platform-owned service for prompt assembly.  Routes prompt assembly
requests through the pipeline factory without requiring callers to
resolve concrete plugin instances.

Gate A introduces this interface so the thinking plugin can assemble
prompts via ``get_service(ServiceName.PROMPT_ASSEMBLY_SERVICE)`` instead
of ``get_plugin("default_inference_plugin")``.
"""

from __future__ import annotations

from typing import Protocol

from ananta.services.inference_service.assembly_types import (
    PromptAssemblyRequest,
    PromptAssemblyResult,
)


class PromptAssemblyServiceInterface(Protocol):
    """Protocol for prompt assembly services.

    Any service implementing this protocol can assemble prompts from
    a ``PromptAssemblyRequest`` into a ``PromptAssemblyResult``.
    Currently implemented by ``InferenceService``.
    """

    def assemble_prompt(
        self,
        request: PromptAssemblyRequest,
    ) -> PromptAssemblyResult:
        """Assemble a prompt via the pipeline factory.

        Args:
            request: Assembly request specifying profile, identity,
                and optional pre-built messages.

        Returns:
            Structured assembly result with serialized messages,
            output schema, and assembly manifest.
        """
        ...
