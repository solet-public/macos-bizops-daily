"""OpenAI Embeddings Plugin - OpenAI-compatible embedding service.

Works with LM Studio, Ollama, OpenAI API, and any OpenAI-compatible endpoint.
Configuration is loaded from address book (not hardcoded).
"""

import logging
import threading
import time
from typing import Any, cast

import httpx
from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import EdgeProcessDefinition, EdgeProcessProvider
from ananta.interfaces.embedding_service_interface import EmbeddingServiceInterface

from .constants import ADDRESS_BOOK_ENTRY_NAME, PLUGIN_NAME, EntryField, ErrorCode
from .response_builders import error_result, success_result

logger = logging.getLogger(__name__)

_QUALIFICATION_RETRY_SECONDS = 2.0


class OpenAIEmbeddingsPlugin(PluginBase, EmbeddingServiceInterface, EdgeProcessProvider):
    """OpenAI-compatible embeddings plugin.

    Loads configuration from address book entry 'openai_embeddings':
    - base_url: API endpoint (e.g., http://localhost:1234/v1)
    - model: Default model name (e.g., nomic-embed-text-v1.5)
    - api_key: Optional API key (use vault::key_name for secrets)
    - timeout_seconds: Request timeout (optional, defaults to 30)
    """

    # Default dimension for the canonical local model (nomic-embed-text-v1.5).
    # Returned by get_default_dimensions() so the platform can declare the
    # discovery_processes__embeddings.embedding column shape at schema-init
    # time, before this plugin's prepare_for_readiness probes the server.
    # Override at config layer when binding a different default model.
    _DEFAULT_LOCAL_DIMENSIONS: int = 768

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize plugin."""
        super().__init__()
        self.name = PLUGIN_NAME
        self.config = config or {}
        self._initialized = False

        # Configuration from address book
        self._base_url: str = ""
        self._default_model: str = ""
        self._api_key: str | None = None
        self._timeout_seconds: int = 30
        self._default_dimensions: int = self._DEFAULT_LOCAL_DIMENSIONS

        # Service references
        self._address_book_service: Any = None
        self._qualification_complete = threading.Event()
        self._qualification_thread: threading.Thread | None = None
        self._qualification_lock = threading.Lock()
        self.qualification_status: dict[str, str] = {"state": "not_started"}

    @property
    def service_interfaces(self) -> tuple[type, ...]:
        """Declare that this plugin implements EmbeddingServiceInterface."""
        return (EmbeddingServiceInterface,)

    @property
    def supported_interface_versions(self) -> dict[type, str]:
        return {EmbeddingServiceInterface: EmbeddingServiceInterface.INTERFACE_VERSION}

    def prepare_for_readiness(self) -> None:
        """Initialize plugin by loading config from address book.

        Uses Service Registry pattern: plugin REQUESTS services from orchestrator.
        """
        if not self.orchestrator_ref:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")

        logger.debug(f"Initializing {self.name}")

        # Get address_book_service
        self._address_book_service = self.orchestrator_ref.get_service("address_book_service")
        if self._address_book_service is None:
            raise RuntimeError(f"{self.name}: address_book_service not available")
        logger.debug("address_book_service acquired from orchestrator")

        # Load configuration from address book
        self._load_config_from_address_book()

        self._initialized = True
        self.qualification_status = {"state": "pending"}
        self.set_ready()

    def start_post_registration_qualification(self) -> None:
        """Qualify the model only after this process is router-active."""
        with self._qualification_lock:
            if self._qualification_thread is not None and self._qualification_thread.is_alive():
                return
            self._qualification_thread = threading.Thread(
                target=self._qualify_until_ready,
                name=f"{self.name}-qualification",
                daemon=True,
            )
            self._qualification_thread.start()

    def wait_for_post_registration_qualification(self) -> None:
        """Wait in the post-registration worker until embedding qualification succeeds."""
        self._qualification_complete.wait()

    def _qualify_until_ready(self) -> None:
        while True:
            try:
                self._qualify_configured_model()
            except RuntimeError as exc:
                self.qualification_status = {"state": "pending", "error": str(exc)}
                logger.warning("%s embedding qualification pending: %s", self.name, exc)
                time.sleep(_QUALIFICATION_RETRY_SECONDS)
            else:
                self.qualification_status = {"state": "ready"}
                self._qualification_complete.set()
                logger.info("%s configured embedding model qualified after registration", self.name)
                return

    def _qualify_configured_model(self) -> None:
        """Prove the configured embedding binding can serve one request.

        Identity-memory seeding is a mandatory later startup step.  Qualifying
        the same configured model here keeps that dependency before lifecycle
        workers start, so a bad endpoint or model produces a supervised failed
        boot instead of a partial platform startup.
        """
        url = f"{self._base_url}/embeddings"
        payload = {"model": self._default_model, "input": ["startup readiness probe"]}
        response_data, api_error = self._call_embeddings_api(url, payload)
        if api_error is not None:
            error = api_error.get("error")
            message = (
                error.get("message", "unknown embedding API error")
                if isinstance(error, dict)
                else str(error)
            )
            raise RuntimeError(
                f"{self.name}: configured embedding model '{self._default_model}' "
                f"failed startup qualification: {message}"
            )
        if response_data is None:
            raise RuntimeError(
                f"{self.name}: configured embedding model '{self._default_model}' "
                "returned no response during startup qualification"
            )

        result = self._parse_embeddings_response(response_data, self._default_model)
        if result.get("action_status") != "completed":
            error = result.get("error")
            message = (
                error.get("message", "invalid embedding response")
                if isinstance(error, dict)
                else str(error)
            )
            raise RuntimeError(
                f"{self.name}: configured embedding model '{self._default_model}' "
                f"failed startup qualification: {message}"
            )

    def _load_config_from_address_book(self) -> None:
        """Load configuration from address book entry.

        Fails immediately if address book entry doesn't exist - no fallback.
        """
        result = self._address_book_service.resolve_with_secrets(ADDRESS_BOOK_ENTRY_NAME)
        entries = self._validate_address_book_response(result)
        self._extract_entries_config(entries)
        self._validate_required_config()

    def _validate_address_book_response(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        """Validate address book response and return entries list."""
        if result.get("action_status") != "completed":
            error = result.get("error", {})
            msg = error.get("message", "Unknown error") if isinstance(error, dict) else str(error)
            raise RuntimeError(
                f"{self.name}: Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' not found. "
                f"Create the entry before starting the solet. Error: {msg}"
            )

        data = result.get("data", {})
        if not isinstance(data, dict):
            raise RuntimeError(f"{self.name}: Invalid address book response")

        entries = data.get("entries", [])
        if not isinstance(entries, list):
            raise RuntimeError(f"{self.name}: No entries in address book entry")
        return entries

    def _extract_entries_config(self, entries: list[dict[str, Any]]) -> None:
        """Extract configuration values from entries list."""
        for entry in entries:
            if not isinstance(entry, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
                continue
            field_type = entry.get("field_type", "")
            value = entry.get("value", "")

            if field_type == EntryField.BASE_URL:
                self._base_url = str(value).rstrip("/")
            elif field_type == EntryField.MODEL:
                self._default_model = str(value)
            elif field_type == EntryField.API_KEY:
                self._api_key = str(value) if value else None
            elif field_type == EntryField.TIMEOUT_SECONDS:
                self._timeout_seconds = int(value) if value else 30

    def _validate_required_config(self) -> None:
        """Validate required configuration fields are present."""
        if not self._base_url:
            raise RuntimeError(
                f"{self.name}: Missing 'base_url' in address book entry '{ADDRESS_BOOK_ENTRY_NAME}'"
            )
        if not self._default_model:
            raise RuntimeError(
                f"{self.name}: Missing 'model' in address book entry '{ADDRESS_BOOK_ENTRY_NAME}'"
            )

    def _get_headers(self) -> dict[str, str]:
        """Build HTTP headers for API requests."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _resolve_model(self, model: str | None) -> str:
        """Resolve model name - use provided or default."""
        return model if model else self._default_model

    # ─────────────────────────────────────────────────────────────────────────
    # EmbeddingServiceInterface Implementation
    # ─────────────────────────────────────────────────────────────────────────

    def generate_embeddings(
        self,
        inputs: list[str],
        model: str | None = None,
        input_type: str = "text",
    ) -> ActionResult:
        """Generate embeddings via OpenAI-compatible API.

        Args:
            inputs: List of text strings to embed
            model: Model identifier (uses default if None)
            input_type: Type of input (only "text" supported)

        Returns:
            ActionResult with embeddings, dimension, and model name
        """
        validation_error = self._validate_embedding_inputs(inputs, input_type)
        if validation_error:
            return validation_error

        resolved_model = self._resolve_model(model)
        url = f"{self._base_url}/embeddings"
        payload = {"model": resolved_model, "input": inputs}

        response_data, api_error = self._call_embeddings_api(url, payload)
        if api_error:
            return api_error

        if response_data is None:
            return error_result(
                ErrorCode.INVALID_RESPONSE,
                "API returned no data",
            )

        return self._parse_embeddings_response(response_data, resolved_model)

    def _validate_embedding_inputs(self, inputs: list[str], input_type: str) -> ActionResult | None:
        """Validate embedding inputs. Returns error result or None if valid."""
        if not self._initialized:
            return error_result(
                ErrorCode.NOT_INITIALIZED,
                "Plugin not initialized. Call prepare_for_readiness() first.",
            )

        if input_type != "text":
            return error_result(
                ErrorCode.UNSUPPORTED_INPUT_TYPE,
                f"Unsupported input_type: {input_type}. Only 'text' is supported.",
                {"input_type": input_type, "supported": ["text"]},
            )

        if not inputs:
            return error_result(ErrorCode.EMPTY_INPUTS, "inputs cannot be empty")

        if not isinstance(inputs, list):  # pyright: ignore[reportUnnecessaryIsInstance]
            return error_result(ErrorCode.INVALID_INPUTS, "inputs must be a list of strings")

        for i, text in enumerate(inputs):
            if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance]
                return error_result(
                    ErrorCode.INVALID_INPUTS,
                    f"Input at index {i} is not a string: {type(text).__name__}",
                    {"index": i, "type": type(text).__name__},
                )
        return None

    def _call_embeddings_api(
        self, url: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, ActionResult | None]:
        """Call embeddings API. Returns (response_data, None) or (None, error_result)."""
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.post(url, json=payload, headers=self._get_headers())
                response.raise_for_status()
                return response.json(), None
        except httpx.ConnectError as e:
            return None, error_result(
                ErrorCode.CONNECTION_FAILED,
                f"Failed to connect to {url}: {e}",
                {"url": url},
            )
        except httpx.HTTPStatusError as e:
            return None, error_result(
                ErrorCode.API_ERROR,
                f"API returned error: {e.response.status_code} - {e.response.text}",
                {"status_code": e.response.status_code, "url": url},
            )
        except Exception as e:
            return None, error_result(
                ErrorCode.API_ERROR,
                f"API request failed: {e}",
                {"url": url},
            )

    def _parse_embeddings_response(
        self, response_data: dict[str, Any], resolved_model: str
    ) -> ActionResult:
        """Parse embeddings API response into ActionResult."""
        data_list = response_data.get("data", [])
        if not isinstance(data_list, list) or not data_list:
            return error_result(
                ErrorCode.INVALID_RESPONSE,
                "Invalid API response: missing or empty 'data' field",
                {"response": response_data},
            )

        sorted_data = sorted(data_list, key=lambda x: x.get("index", 0))
        embeddings: list[list[float]] = []

        for item in sorted_data:
            embedding = item.get("embedding", [])
            if not isinstance(embedding, list):
                return error_result(
                    ErrorCode.INVALID_RESPONSE,
                    "Invalid API response: embedding is not a list",
                    {"item": item},
                )
            embeddings.append(cast("list[float]", embedding))

        dimension = len(embeddings[0]) if embeddings else 0
        returned_model = response_data.get("model", resolved_model)

        return success_result(
            {"embeddings": embeddings, "dimension": dimension, "model": returned_model}
        )

    def get_default_dimensions(self) -> int:
        """Return the default model's output dimension (synchronous, no probe).

        Satisfies the platform's EmbeddingServiceInterface contract — must be
        callable at schema-init time, before this plugin's prepare_for_readiness
        runs (and before the OpenAI-compatible server is reached). Defaults to
        768 (nomic-embed-text-v1.5, the canonical local model). Override by
        binding a different model whose dimension differs at startup.
        """
        return self._default_dimensions

    def get_embedding_dimension(self, model: str | None = None) -> ActionResult:
        """Get embedding dimension for a model.

        Generates a single embedding to determine dimension.

        Args:
            model: Model identifier (uses default if None)

        Returns:
            ActionResult with dimension and model name
        """
        if not self._initialized:
            return error_result(
                ErrorCode.NOT_INITIALIZED,
                "Plugin not initialized. Call prepare_for_readiness() first.",
            )

        # Generate a single embedding to get dimension
        result = self.generate_embeddings(["dimension probe"], model=model)

        if result.get("action_status") != "completed":
            return result

        data = result.get("data", {})
        if isinstance(data, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
            result_data = data.get("result", {})
            if isinstance(result_data, dict):
                return success_result(
                    {
                        "dimension": result_data.get("dimension", 0),
                        "model": result_data.get("model", self._resolve_model(model)),
                    }
                )

        return error_result(
            ErrorCode.INVALID_RESPONSE,
            "Failed to determine embedding dimension",
        )

    def list_models(self) -> ActionResult:
        """List available embedding models.

        Queries the /v1/models endpoint and filters for embedding models.

        Returns:
            ActionResult with list of available models
        """
        if not self._initialized:
            return error_result(
                ErrorCode.NOT_INITIALIZED,
                "Plugin not initialized. Call prepare_for_readiness() first.",
            )

        url = f"{self._base_url}/models"
        response_data, result = self._call_models_api(url)
        if result:
            return result

        if response_data is None:
            return error_result(
                ErrorCode.INVALID_RESPONSE,
                "Models API returned no data",
            )

        models = self._parse_models_response(response_data)
        self._ensure_default_model_in_list(models)

        return success_result({"models": models})

    def _call_models_api(self, url: str) -> tuple[dict[str, Any] | None, ActionResult | None]:
        """Call models API. Returns (response_data, None) or (None, result)."""
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.get(url, headers=self._get_headers())
                response.raise_for_status()
                return response.json(), None
        except httpx.ConnectError as e:
            return None, error_result(
                ErrorCode.CONNECTION_FAILED,
                f"Failed to connect to {url}: {e}",
                {"url": url},
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Models endpoint not available: {e}")
            return None, success_result({"models": [self._build_default_model_entry()]})
        except Exception as e:
            return None, error_result(
                ErrorCode.API_ERROR,
                f"API request failed: {e}",
                {"url": url},
            )

    def _parse_models_response(self, response_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse models API response, filtering for embedding models."""
        models_data = response_data.get("data", [])
        if not isinstance(models_data, list):
            models_data = []

        models: list[dict[str, Any]] = []
        for model_info in models_data:
            if not isinstance(model_info, dict):
                continue
            model_id = model_info.get("id", "")
            model_type = model_info.get("type", "")
            if model_type in ("embedding", "") or "embed" in model_id.lower():
                models.append(
                    {
                        "name": model_id,
                        "dimension": None,
                        "max_input_length": None,
                        "input_types": ["text"],
                        "description": model_info.get("description"),
                    }
                )
        return models

    def _build_default_model_entry(self) -> dict[str, Any]:
        """Build entry dict for the default configured model."""
        return {
            "name": self._default_model,
            "dimension": None,
            "max_input_length": None,
            "input_types": ["text"],
            "description": "Configured default model",
        }

    def _ensure_default_model_in_list(self, models: list[dict[str, Any]]) -> None:
        """Ensure default model is in the models list."""
        if not any(m["name"] == self._default_model for m in models):
            models.insert(0, self._build_default_model_entry())

    def is_ready(self) -> bool:
        """Check if the embedding service is ready for use."""
        return self._initialized and bool(self._base_url) and bool(self._default_model)

    def get_readiness_error(self) -> str | None:
        """Get error message if not ready, None if ready."""
        if not self._initialized:
            return "Plugin not initialized. Call prepare_for_readiness() first."
        if not self._base_url:
            return f"Missing 'base_url' in address book entry '{ADDRESS_BOOK_ENTRY_NAME}'"
        if not self._default_model:
            return f"Missing 'model' in address book entry '{ADDRESS_BOOK_ENTRY_NAME}'"
        if self.qualification_status["state"] != "ready":
            return "Configured embedding model qualification pending after router registration"
        return None

    async def cleanup(self) -> None:
        """Cleanup resources on plugin shutdown."""
        logger.debug(f"{self.name} cleanup complete")

    def get_config_schema(self) -> dict[str, object]:
        """Declare configuration schema for the embeddings plugin.

        Returns JSON Schema for setup flow to generate UI/prompts.
        """
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "OpenAI Embeddings Plugin",
            "description": (
                "Configuration for OpenAI-compatible embedding service "
                "(works with LM Studio, Ollama, OpenAI API)"
            ),
            "type": "object",
            "required": ["base_url", "model"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "format": "uri",
                    "title": "API Base URL",
                    "description": "Base URL for the embedding API endpoint",
                    "default": "http://localhost:1234/v1",
                    "examples": [
                        "http://localhost:1234/v1",
                        "http://host.docker.internal:1234/v1",
                        "https://api.openai.com/v1",
                        "http://localhost:11434/v1",
                    ],
                    "x-group": "connection",
                    "x-order": 1,
                },
                "model": {
                    "type": "string",
                    "title": "Embedding Model",
                    "description": "Name of the embedding model to use",
                    "default": "text-embedding-nomic-embed-text-v1.5-embedding",
                    "examples": [
                        "text-embedding-nomic-embed-text-v1.5-embedding",
                        "text-embedding-3-small",
                        "all-MiniLM-L6-v2",
                        "nomic-embed-text",
                    ],
                    "x-group": "connection",
                    "x-order": 2,
                },
                "api_key": {
                    "type": "string",
                    "title": "API Key",
                    "description": (
                        "API key for authentication (required for OpenAI, "
                        "optional for local providers like LM Studio)"
                    ),
                    "x-secret": True,
                    "x-group": "security",
                    "x-order": 1,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "title": "Timeout",
                    "description": "Request timeout in seconds",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 300,
                    "x-group": "advanced",
                    "x-order": 1,
                },
            },
            "x-test-endpoint": "/v1/embeddings",
            "x-test-method": "probe_dimension",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Platform Process Methods (with @platform_process decorators)
    # ─────────────────────────────────────────────────────────────────────────

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/generate_embeddings_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="generate_embeddings_action",
        parameters={
            "inputs": ParameterMetadata(
                description="Text strings to generate embeddings for",
                required=True,
                type=ParameterType.LIST,
            ),
            "model": ParameterMetadata(
                description="Embedding model to use (uses default if not specified)",
                required=False,
                type=ParameterType.STRING,
            ),
            "input_type": ParameterMetadata(
                description="Type of input data (default: 'text')",
                required=False,
                type=ParameterType.STRING,
                default="text",
            ),
        },
        output_type="object",
        output_description="Vector embeddings with metadata",
        is_inference_capable=True,
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Vector embeddings with metadata",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Embeddings payload (embeddings array + dimension)",
                ),
            },
            usage_patterns=[
                "Generate embeddings for semantic search",
                "Create vector representations of text",
            ],
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def generate_embeddings_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Generate vector embeddings from text (platform process entry point)."""
        inputs = params.get("inputs", [])
        model = params.get("model")
        input_type = params.get("input_type", "text")
        return self.generate_embeddings(inputs, model, input_type)

    # Text fields (display_name, description, embedding_description) are defined
    # in knowledge_base/processes/get_embedding_dimension_action.json — the builder
    # merges them at startup, overwriting any values set here in the decorator.
    @platform_process(
        name="get_embedding_dimension_action",
        parameters={
            "model": ParameterMetadata(
                description="Embedding model name (uses default if not specified)",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="Embedding dimension for model",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Embedding dimension for model",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Model dimension payload (dimension integer)",
                ),
            },
            usage_patterns=[
                "Check embedding dimensions before storing vectors",
                "Validate vector sizes for compatibility",
            ],
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def get_embedding_dimension_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Get embedding vector dimension (platform process entry point)."""
        model = params.get("model")
        return self.get_embedding_dimension(model)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/list_models_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="list_models_action",
        parameters={},
        output_type="object",
        output_description="List of available embedding models",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="List of available embedding models",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Available models payload (models array with metadata)",
                ),
            },
            usage_patterns=[
                "Discover available embedding models",
                "Choose appropriate model for task",
            ],
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def list_models_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """List available embedding models (platform process entry point)."""
        return self.list_models()

    # =========================================================================
    # EdgeProcessProvider Implementation
    # =========================================================================

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """Return all edge process definitions for OpenAI embeddings plugin.

        Returns:
            Dictionary mapping process names to their EdgeProcessDefinition.
        """
        return {
            "generate_embeddings_action": EdgeProcessDefinition(
                name="generate_embeddings_action",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "get_embedding_dimension_action": EdgeProcessDefinition(
                name="get_embedding_dimension_action",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "list_models_action": EdgeProcessDefinition(
                name="list_models_action",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
        }
