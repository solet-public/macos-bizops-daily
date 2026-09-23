"""Embedding Service Public API - AI-discoverable operations."""

from abc import ABC, abstractmethod

from ananta.core.actions.action_metadata import (
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.types import ActionResult
from ananta.core.services.service_interface_decorator import service_interface_process


class EmbeddingServiceAPI(ABC):
    """Public embedding operations - AI-discoverable via vector search."""

    @service_interface_process(
        name="generate_embeddings",
        provider="embedding_service",
        parameters={
            "inputs": ParameterMetadata(
                description="Text strings to generate embeddings for",
                required=True,
                type=ParameterType.LIST,
            ),
            "model": ParameterMetadata(
                description="Embedding model to use", required=False, type=ParameterType.STRING
            ),
            "input_type": ParameterMetadata(
                description="Type of input data (default: 'text')",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Vector embeddings with metadata",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Generated embeddings payload (embeddings array + dimension)",
                    required=False,
                ),
            },
            usage_patterns=[
                "Generate embeddings for semantic search",
                "Create vector representations of text",
            ],
        ),
        is_inference_capable=True,
    )
    @abstractmethod
    def generate_embeddings(
        self, inputs: list[str], model: str | None = None, input_type: str = "text"
    ) -> ActionResult:
        """Generate vector embeddings from text."""
        ...

    @service_interface_process(
        name="get_embedding_dimension",
        provider="embedding_service",
        parameters={
            "model": ParameterMetadata(
                description="Embedding model name", required=False, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Embedding dimension for model",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Model dimension payload (dimension integer)",
                    required=False,
                ),
            },
            usage_patterns=[
                "Check embedding dimensions",
                "Validate vector sizes",
            ],
        ),
    )
    @abstractmethod
    def get_embedding_dimension(self, model: str | None = None) -> ActionResult:
        """Get embedding vector dimension."""
        ...

    @service_interface_process(
        name="list_models",
        provider="embedding_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="List of available embedding models",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Available models payload (models array with metadata)",
                    required=False,
                ),
            },
            usage_patterns=[
                "Discover available embedding models",
                "Choose appropriate model for task",
            ],
        ),
    )
    @abstractmethod
    def list_models(self) -> ActionResult:
        """List available embedding models."""
        ...
