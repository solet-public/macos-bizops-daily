"""Embedding service interface for embedding generation.

This interface defines the contract for embedding generation plugins that
convert text, images, or other inputs into vector embeddings.
"""

from abc import ABC, abstractmethod
from typing import ClassVar

from ananta.core.domain.types import ActionResult


class EmbeddingServiceInterface(ABC):
    """Service interface for embedding generation.

    Stateless service - no database dependency.
    Plugins may use local models, API calls, or other embedding providers.

    Implementation Notes:
    - All methods return ActionResult with action_status, data, actions, error, timestamp
    - Plugins handle model loading, caching, and inference
    - No state storage required (stateless service)
    - Support for multiple input types (text, image, audio)
    - Model-specific configuration via plugin config files
    """

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def generate_embeddings(
        self, inputs: list[str], model: str | None = None, input_type: str = "text"
    ) -> ActionResult:
        """Generate embeddings for inputs.

        Args:
            inputs: List of text strings, image URLs, or audio URLs
            model: Model identifier (provider-specific, optional)
                   If None, uses default model from plugin configuration
            input_type: Type of input data
                - "text": Text strings (default)
                - "image": Image URLs or paths
                - "audio": Audio URLs or paths

        Returns:
            ActionResult with:
            {
                "action_status": "completed",
                "data": {
                    "result": {
                        "embeddings": list[list[float]],  # One embedding per input
                        "dimension": int,                 # Embedding dimension
                        "model": str                      # Model used for generation
                    }
                },
                "actions": [],
                "error": None,
                "timestamp": str
            }

        Examples:
            >>> result = plugin.generate_embeddings(
            ...     inputs=["Hello world", "How are you?"],
            ...     model="all-MiniLM-L6-v2",
            ...     input_type="text"
            ... )
            >>> embeddings = result["data"]["result"]["embeddings"]
            >>> len(embeddings)  # 2 embeddings
            2
            >>> len(embeddings[0])  # 384 dimensions for MiniLM
            384

        Error Cases:
            Returns action_status="error" if:
            - Model not supported by plugin
            - Input type not supported by plugin
            - Input too long for model (exceeds max_input_length)
            - Model loading failure
            - Inference failure
        """

    @abstractmethod
    def get_embedding_dimension(self, model: str | None = None) -> ActionResult:
        """Get embedding dimension for a model.

        Args:
            model: Model identifier (uses default if None)

        Returns:
            ActionResult with:
            {
                "action_status": "completed",
                "data": {
                    "result": {
                        "dimension": int,
                        "model": str  # Model name used
                    }
                },
                "actions": [],
                "error": None,
                "timestamp": str
            }

        Examples:
            >>> result = plugin.get_embedding_dimension("all-MiniLM-L6-v2")
            >>> result["data"]["result"]["dimension"]
            384

        Error Cases:
            Returns action_status="error" if:
            - Model not supported
            - Model information unavailable
        """

    @abstractmethod
    def list_models(self) -> ActionResult:
        """List available embedding models.

        Returns:
            ActionResult with:
            {
                "action_status": "completed",
                "data": {
                    "result": {
                        "models": [
                            {
                                "name": str,                    # Model identifier
                                "dimension": int,               # Embedding dimension
                                "max_input_length": int | None, # Max tokens/chars (None if unlimited)
                                "input_types": list[str],       # Supported input types
                                "description": str | None       # Optional model description
                            }
                        ]
                    }
                },
                "actions": [],
                "error": None,
                "timestamp": str
            }

        Examples:
            >>> result = plugin.list_models()
            >>> models = result["data"]["result"]["models"]
            >>> models[0]["name"]
            'all-MiniLM-L6-v2'
            >>> models[0]["dimension"]
            384
            >>> models[0]["input_types"]
            ['text']

        Error Cases:
            Returns action_status="error" if model listing fails.
        """

    @abstractmethod
    def is_ready(self) -> bool:
        """Check if the embedding service implementation is ready for use."""
        ...

    @abstractmethod
    def get_readiness_error(self) -> str | None:
        """Get the error message if not ready, None if ready."""
        ...
