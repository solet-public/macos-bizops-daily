"""Inference Service Interface.

Single provider per implementation. No routing, no fallback.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from ananta.core.domain.types import ActionResult

if TYPE_CHECKING:
    from ananta.services.context_management.compaction_types import (
        CompactionRequest,
        WarmingRequest,
    )


class InferenceRequest:
    """Structured inference request."""

    # Standard output schema for all inference actions
    # Uses structured process objects, not process_key strings
    STANDARD_OUTPUT_SCHEMA: dict[str, Any] = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["reasoning", "actions"],
        "properties": {
            "reasoning": {
                "type": "string",
                "description": "Brief explanation of the decision or action taken",
            },
            "actions": {
                "type": "array",
                "minItems": 0,
                "maxItems": 5,
                "description": "Actions to execute",
                "items": {
                    "type": "object",
                    "required": ["process", "reason", "arguments"],
                    "properties": {
                        "process": {
                            "type": "object",
                            "description": "Structured process identifier - copy values exactly from discovery results",
                            "required": ["provider_type", "provider", "function_name"],
                            "properties": {
                                "provider_type": {
                                    "type": "string",
                                    "enum": ["plugin", "service_interface"],
                                    "description": "Provider type - COPY from discovery results (plugin for plugins, service_interface for core services)",
                                },
                                "provider": {
                                    "type": "string",
                                    "description": "Provider name - COPY from discovery results",
                                },
                                "function_name": {
                                    "type": "string",
                                    "description": "Function name - COPY from discovery results",
                                },
                            },
                            "additionalProperties": False,
                        },
                        "reason": {"type": "string", "description": "Why this action is needed"},
                        "arguments": {
                            "type": "object",
                            "description": "Arguments to pass to the process",
                        },
                    },
                },
            },
        },
        "additionalProperties": False,
    }

    def __init__(
        self,
        prompt: str | list[dict[str, str]],
        *,
        system_prompt: str | None = None,
        temperature: float,  # Required - comes from inference plugin config
        max_tokens: int,  # Required - comes from inference plugin config
        stop_sequences: list[str] | None = None,
        context_metadata: dict[str, Any] | None = None,
        response_schema: dict[str, Any] | None = None,
        use_structured_output: bool = True,
        hide_from_context: bool = False,
    ):
        # Normalize to messages format
        if isinstance(prompt, str):
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            self.messages = messages
        else:
            self.messages = prompt

        self.temperature = temperature
        self.max_tokens = max_tokens
        self.stop_sequences = stop_sequences
        self.context_metadata = context_metadata or {}
        self.response_schema = response_schema
        self.use_structured_output = use_structured_output
        self.hide_from_context = hide_from_context

    def to_openai_format(self) -> dict[str, Any]:
        """Convert to OpenAI chat completion format with structured output support."""
        payload: dict[str, Any] = {
            "messages": self.messages,
            "temperature": self.temperature,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if self.stop_sequences:
            payload["stop"] = self.stop_sequences

        # Add structured output via response_format (LM Studio / OpenAI compatible)
        if self.use_structured_output:
            schema = self.response_schema or self.STANDARD_OUTPUT_SCHEMA
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "action_response", "strict": True, "schema": schema},
            }

        return payload


class InferenceServiceInterface(ABC):
    """Service interface for inference providers.

    Rule: One provider per implementation. No routing, no fallback.

    Provides two public inference methods with semantic purposes:
    - process_error: Error-recovery inference (no discovery)
    - process_results: Result-processing inference (output-focused discovery)

    Note: APP_HOME is obtained internally from the orchestrator reference,
    not passed as a parameter to these methods.
    """

    INTERFACE_VERSION: ClassVar[str] = "1.5.0"

    @abstractmethod
    def process_error(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Error-context inference: handle action errors and provide recovery.

        Args:
            params: Parameters dict containing prompt and optional model config
            state: State dict for plugin execution

        Returns:
            ActionResult with error analysis and recovery suggestions
        """
        pass

    @abstractmethod
    def process_results(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Result-processing inference: format results and determine next steps.

        Uses result-focused discovery to provide output-relevant process information.

        Args:
            params: Parameters dict containing prompt and optional model config
            state: State dict for plugin execution

        Returns:
            ActionResult with formatted output and next-step actions
        """
        pass

    @abstractmethod
    def propose_name(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Naming inference: propose a human-friendly name for a file or artifact.

        Used when user request is vague and a descriptive name must be derived.

        Args:
            params: Parameters dict containing intent_text, artifact_type, input_filename
            state: State dict for plugin execution

        Returns:
            ActionResult with proposed display_name, extension, confidence, and flags
        """
        pass

    @abstractmethod
    def generate_completion(self, request: InferenceRequest) -> ActionResult:
        """Generate completion from inference model (low-level provider method)."""
        pass

    @abstractmethod
    def validate_availability(self) -> ActionResult:
        """Check if inference service available (<2s)."""
        pass

    @abstractmethod
    def get_model_info(self) -> ActionResult:
        """Get model information."""
        pass

    @abstractmethod
    def is_ready(self) -> bool:
        """Check if the inference service implementation is ready for use."""
        ...

    @abstractmethod
    def get_readiness_error(self) -> str | None:
        """Get the error message if not ready, None if ready."""
        ...

    @abstractmethod
    def get_configured_model_name(self) -> str:
        """Get the configured model name for this inference provider.

        Returns:
            The model name configured in the plugin (e.g., 'meta-llama-3.1-8b-instruct')

        Raises:
            FrameworkError: If no model is configured
        """
        ...

    @abstractmethod
    def generate_compaction_summary(
        self,
        request: "CompactionRequest",
    ) -> str:
        """Generate summary for context compaction.

        Used by context management service to summarize older events
        during compaction. Returns plain text summary.

        Args:
            request: Compaction request with messages and config.

        Returns:
            Summary text for the snapshot.

        Raises:
            PluginError: If summary generation fails.
        """
        ...

    @abstractmethod
    def warm_cache(
        self,
        request: "WarmingRequest",
    ) -> bool:
        """Warm KV cache with context. Required if warming_enabled=True.

        Used after compaction to pre-populate the LLM's context cache
        with recent events, reducing latency for subsequent calls.

        Args:
            request: Warming request with messages and config.

        Returns:
            True if warming succeeded.

        Raises:
            PluginError: If warming fails.
        """
        ...
