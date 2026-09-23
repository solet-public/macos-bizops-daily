"""Vector Service Public API - AI-discoverable operations."""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import DistanceMetric
from ananta.core.domain.types import ActionResult
from ananta.core.services.service_interface_decorator import service_interface_process


class VectorServiceAPI(ABC):
    """Public vector operations - AI-discoverable via vector search."""

    @service_interface_process(
        name="store_vectors",
        provider="vector_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace for vectors", required=True, type=ParameterType.STRING
            ),
            "vectors": ParameterMetadata(
                description="Vector data with IDs and metadata",
                required=True,
                type=ParameterType.LIST,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Vector storage confirmation",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Storage results", required=False
                ),
            },
            usage_patterns=[
                "Store embeddings for semantic search",
                "Index vector data",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def store_vectors(self, namespace: str, vectors: list[dict[str, Any]]) -> ActionResult:
        """Store vector embeddings."""
        ...

    @service_interface_process(
        name="search_similar",
        provider="vector_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace to search", required=True, type=ParameterType.STRING
            ),
            "query_vector": ParameterMetadata(
                description="Query vector for similarity search",
                required=True,
                type=ParameterType.LIST,
            ),
            "top_k": ParameterMetadata(
                description="Maximum number of results to return",
                required=False,
                type=ParameterType.INTEGER,
                default=10,
            ),
            "filters": ParameterMetadata(
                description="Metadata filters", required=False, type=ParameterType.OBJECT
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Similar vectors ranked by similarity",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Search results", required=False
                ),
            },
            usage_patterns=[
                "Find similar content",
                "Semantic search",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def search_similar(
        self,
        namespace: str,
        query_vector: list[float],
        top_k: int = 10,
        filters: dict[str, object] | None = None,
        distance_metric: "DistanceMetric" = DistanceMetric.COSINE,
    ) -> ActionResult:
        """Search for similar vectors."""
        ...

    @service_interface_process(
        name="get_vector",
        provider="vector_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace containing vector", required=True, type=ParameterType.STRING
            ),
            "vector_id": ParameterMetadata(
                description="Vector ID to retrieve", required=True, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Retrieved vector with metadata",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Vector data", required=False
                ),
            },
            usage_patterns=[
                "Retrieve specific vectors",
                "Lookup vector data",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def get_vector(self, namespace: str, vector_id: str) -> ActionResult:
        """Get vector by ID."""
        ...

    @service_interface_process(
        name="delete_vectors",
        provider="vector_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace containing vectors", required=True, type=ParameterType.STRING
            ),
            "vector_ids": ParameterMetadata(
                description="Vector IDs to delete", required=False, type=ParameterType.LIST
            ),
            "filters": ParameterMetadata(
                description="Metadata filters for deletion",
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Deletion confirmation",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Deletion results", required=False
                ),
            },
            usage_patterns=[
                "Remove vectors",
                "Clean up vector storage",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def delete_vectors(
        self,
        namespace: str,
        vector_ids: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> ActionResult:
        """Delete vectors."""
        ...

    @service_interface_process(
        name="update_metadata",
        provider="vector_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace containing vectors", required=True, type=ParameterType.STRING
            ),
            "vector_id": ParameterMetadata(
                description="Vector ID to update", required=True, type=ParameterType.STRING
            ),
            "metadata": ParameterMetadata(
                description="New metadata values", required=True, type=ParameterType.OBJECT
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Metadata update confirmation",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Update results", required=False
                ),
            },
            usage_patterns=[
                "Update vector metadata",
                "Modify vector attributes",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def update_metadata(
        self, namespace: str, vector_id: str, metadata: dict[str, Any]
    ) -> ActionResult:
        """Update vector metadata."""
        ...

    @service_interface_process(
        name="list_namespaces",
        provider="vector_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="List of all vector namespaces",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Namespace list", required=False
                ),
            },
            usage_patterns=[
                "Discover vector namespaces",
                "List vector partitions",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def list_namespaces(self) -> ActionResult:
        """List all vector namespaces."""
        ...

    @service_interface_process(
        name="get_namespace_stats",
        provider="vector_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace to get stats for", required=True, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Namespace statistics and metadata",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Namespace statistics", required=False
                ),
            },
            usage_patterns=[
                "Check namespace statistics",
                "Monitor vector storage",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def get_namespace_stats(self, namespace: str) -> ActionResult:
        """Get namespace statistics."""
        ...
