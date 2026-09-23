"""Vector service interface for vector storage and similarity search.

This interface defines the contract for vector storage plugins that provide
vector embeddings storage and approximate nearest neighbor search capabilities.
"""

from abc import ABC, abstractmethod
from typing import ClassVar

from ananta.core.domain.enums import DistanceMetric
from ananta.core.domain.types import ActionResult


class VectorServiceInterface(ABC):
    """Service interface for vector storage and similarity search.

    Storage contract:
    - Standard fields (id, namespace, created_at, etc.) automatically added by platform
    - Vectors stored as binary blobs (provider handles encoding)
    - Metadata stored as TEXT containing JSON string
    - Uses {namespace}__{table} naming within vectors PostgreSQL schema

    Implementation Notes:
    - All methods return ActionResult with action_status, data, actions, error, timestamp
    - Platform automatically injects standard fields via SchemaStandardizer
    - Providers handle vector encoding/decoding to/from binary format
    - Approximate nearest neighbor search using HNSW or similar indexes
    """

    INTERFACE_VERSION: ClassVar[str] = "2.2.0"  # Added delete_all_in_namespace

    @abstractmethod
    def store_vectors(self, namespace: str, vectors: list[dict[str, object]]) -> ActionResult:
        """Store vectors with metadata.

        Args:
            namespace: Plugin namespace for data isolation
            vectors: List of vector records, each containing:
                {
                    "external_id": str,     # Optional business identifier
                    "vector": list[float],  # Embedding values
                    "dimension": int,       # Vector dimension (e.g., 384, 768)
                    "metadata": dict        # JSON-serializable metadata
                }

        Returns:
            ActionResult with inserted_ids and count
        """

    @abstractmethod
    def search_similar(
        self,
        namespace: str,
        query_vector: list[float],
        top_k: int = 10,
        filters: dict[str, object] | None = None,
        distance_metric: DistanceMetric = DistanceMetric.COSINE,
    ) -> ActionResult:
        """Search for similar vectors in a namespace.

        Args:
            namespace: Namespace to search
            query_vector: Query embedding
            top_k: Maximum results to return
            filters: Optional metadata filters {"metadata_key": "value"}
            distance_metric: Distance metric (COSINE, EUCLIDEAN, DOT_PRODUCT)

        Returns:
            ActionResult with results list and count
        """

    @abstractmethod
    def get_vector(self, namespace: str, vector_id: str) -> ActionResult:
        """Retrieve specific vector by platform-generated ID.

        Args:
            namespace: Plugin namespace
            vector_id: Platform-generated ID (e.g., "vec_123abc")

        Returns:
            ActionResult with vector data
        """

    @abstractmethod
    def delete_vectors(
        self,
        namespace: str,
        vector_ids: list[str] | None = None,
        filters: dict[str, object] | None = None,
    ) -> ActionResult:
        """Delete vectors by IDs or filters.

        Args:
            namespace: Plugin namespace
            vector_ids: Optional list of platform-generated IDs to delete
            filters: Optional metadata filters for bulk deletion

        Returns:
            ActionResult with deleted_count
        """

    @abstractmethod
    def delete_by_external_ids(
        self,
        namespace: str,
        external_ids: list[str],
    ) -> ActionResult:
        """Delete vectors by their external_id field.

        This is an internal API used by services (like memory) that store
        business identifiers in external_id. Not exposed as an AI-callable action.

        Args:
            namespace: Plugin namespace
            external_ids: List of external_id values to match

        Returns:
            ActionResult with deleted_count in data.result
        """

    @abstractmethod
    def find_missing_external_ids(
        self,
        namespace: str,
        candidate_external_ids: list[str],
    ) -> ActionResult:
        """Return the candidate external_ids that have no active vector.

        Read counterpart to :meth:`delete_by_external_ids`: an internal API
        used by services (like memory) that store business identifiers in
        external_id and need to reconcile which ones are still backed by a live
        vector. A single ``external_id = ANY(candidates)`` lookup over the
        active rows, then a set-difference (candidates − present). Soft-deleted
        vectors are excluded from the active read, so they are reported as
        missing. Not exposed as an AI-callable action.

        Args:
            namespace: Plugin namespace
            candidate_external_ids: external_id values to check for presence

        Returns:
            ActionResult with ``{"missing": [str]}`` in data.result
        """

    @abstractmethod
    def delete_all_in_namespace(self, namespace: str) -> ActionResult:
        """Hard-delete every vector in a namespace.

        Used for transient embedding stores (e.g., discovery_service's process
        index) that are rebuilt from scratch on startup. This is a hard delete
        that bypasses the soft-delete machinery — callers must understand the
        rows will not be recoverable.

        Args:
            namespace: Plugin namespace whose embeddings table should be cleared

        Returns:
            ActionResult with deleted_count in data
        """

    @abstractmethod
    def update_metadata(
        self, namespace: str, vector_id: str, metadata: dict[str, object]
    ) -> ActionResult:
        """Update metadata without changing vector or dimension.

        Args:
            namespace: Plugin namespace
            vector_id: Platform-generated ID
            metadata: New metadata dict (replaces existing metadata)

        Returns:
            ActionResult with update status
        """

    @abstractmethod
    def list_namespaces(self) -> ActionResult:
        """List all vector namespaces in system.

        Returns:
            ActionResult with namespaces list
        """

    @abstractmethod
    def get_namespace_stats(self, namespace: str) -> ActionResult:
        """Get statistics for a namespace.

        Args:
            namespace: Plugin namespace to analyze

        Returns:
            ActionResult with vector_count, dimensions, timestamps
        """

    @abstractmethod
    def is_ready(self) -> bool:
        """Check if the vector service implementation is ready for use."""
        ...

    @abstractmethod
    def get_readiness_error(self) -> str | None:
        """Get the error message if not ready, None if ready."""
        ...
