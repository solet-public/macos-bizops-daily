"""VectorService - Service wrapper for vector storage and similarity search operations.

This service provides a stable interface for vector storage, allowing the underlying
vector provider plugin to be swapped without breaking consumer code.

Bootstrap Mode: NOT SUPPORTED (vector storage not needed during system startup)
Plugin Mode: Wraps pgvector_service_plugin (or configured alternative via env)
"""

import logging

from ananta.constants import DEFAULT_VECTOR_PLUGIN as DEFAULT_VECTOR_PLUGIN
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.error_handling import FrameworkError
from ananta.interfaces.bootstrappable_service_interface import BootstrappableServiceInterface
from ananta.interfaces.vector_service_interface import VectorServiceInterface

logger = logging.getLogger(__name__)


class VectorService(BootstrappableServiceInterface):
    """Service wrapper for vector storage plugin providers.

    Provides stable interface for vector storage and similarity search operations,
    enabling provider swapping (pgvector, pinecone, weaviate, etc.) without
    breaking consumer code.

    This is a "simple wrapper" - no bootstrap mode, no complex business logic,
    just provider abstraction for swappability.

    Note: VectorService requires StateService for schema management in some plugins.
    """

    def __init__(
        self,
        plugin_manager: PluginManager | None = None,
        vector_plugin_name: str | None = None,
        state_service: "BootstrappableServiceInterface | None" = None,
    ):
        """Initialize VectorService.

        Args:
            plugin_manager: Plugin manager instance (REQUIRED)
            vector_plugin_name: Override plugin name (default: from constants)
            state_service: StateService instance for schema management (optional, plugin-dependent)

        Raises:
            FrameworkError: If plugin_manager is None
        """
        if plugin_manager is None:
            raise FrameworkError(
                "VectorService requires plugin_manager. "
                "Bootstrap mode not supported for vector storage operations."
            )

        # Validation: plugin_manager is not None, so vector_plugin_name must be provided
        if vector_plugin_name is None:
            # Try environment variable set by launch script
            import os

            vector_plugin_name = os.environ.get("ANANTA_VECTOR_PLUGIN")

        if vector_plugin_name is None:
            raise ValueError(
                "vector_plugin_name must be provided when using plugin mode. "
                "Set ANANTA_VECTOR_PLUGIN environment variable or pass vector_plugin_name parameter."
            )

        # NO FALLBACK - fail fast if not provided
        # In bootstrap mode (plugin_manager=None), vector_plugin_name can be None
        self._vector_plugin_name = vector_plugin_name
        self._vector_plugin: VectorServiceInterface | None = None
        self._state_service = state_service

        # Initialize via BootstrappableServiceInterface pattern
        super().__init__(plugin_manager)

        # Override plugin_manager type annotation for mypy
        self.plugin_manager: PluginManager = plugin_manager

    def _init_bootstrap(self) -> None:
        """Bootstrap mode not supported for vector service.

        Raises:
            FrameworkError: Always (bootstrap mode not supported)
        """
        raise FrameworkError(
            "VectorService does not support bootstrap mode. "
            "Vector storage operations require plugin provider."
        )

    def _init_plugin(self) -> None:
        """Initialize plugin mode - validation deferred until first use."""
        logger.debug(f"VectorService initializing with plugin: {self._vector_plugin_name}")
        # Plugin initialization happens in _validate_vector_plugin() - interface notification happens there

    def _validate_vector_plugin(self) -> VectorServiceInterface:
        """Validate that vector plugin exists and is available.

        Returns:
            The vector plugin typed as VectorServiceInterface

        Raises:
            FrameworkError: If plugin not found or doesn't implement interface
        """
        if self._vector_plugin is None:
            plugin = self.plugin_manager.get_plugin(self._vector_plugin_name)

            if not isinstance(plugin, VectorServiceInterface):
                raise FrameworkError(
                    f"Vector plugin '{self._vector_plugin_name}' does not implement VectorServiceInterface. "
                    f"Plugin type: {type(plugin)}"
                )

            # isinstance check narrows type, no cast needed
            self._vector_plugin = plugin

            # CRITICAL: Notify plugin it's an active interface provider
            setter = getattr(plugin, "set_as_active_provider", None)
            if callable(setter):
                setter("VectorServiceInterface")
                logger.debug(
                    f"Notified {self._vector_plugin_name} that it's active VectorServiceInterface provider"
                )

        return self._vector_plugin

    def _ensure_ready(self) -> VectorServiceInterface:
        """Ensure vector plugin exists, implements interface, and is ready.

        Returns:
            The vector plugin typed as VectorServiceInterface

        Raises:
            FrameworkError: If plugin not found, doesn't implement interface, or not ready
        """
        plugin = self._validate_vector_plugin()

        # READINESS CONTRACT: Verify plugin is ready before use
        if not plugin.is_ready():
            error = plugin.readiness_error or "Unknown readiness error"
            raise FrameworkError(f"Vector plugin '{self._vector_plugin_name}' not ready: {error}")

        return plugin

    def store_vectors(self, namespace: str, vectors: list[dict[str, object]]) -> ActionResult:
        """Store vectors with metadata via configured vector provider.

        Args:
            namespace: Plugin namespace for data isolation
            vectors: List of vector records with external_id, vector, dimension, metadata

        Returns:
            ActionResult with inserted_ids and count in data.result field

        Raises:
            FrameworkError: If plugin not available or request fails
        """
        plugin = self._ensure_ready()

        return plugin.store_vectors(namespace=namespace, vectors=vectors)

    def search_similar(
        self,
        namespace: str,
        query_vector: list[float],
        top_k: int = 10,
        filters: dict[str, object] | None = None,
    ) -> ActionResult:
        """Search for similar vectors using approximate nearest neighbor search.

        Args:
            namespace: Namespace to search within
            query_vector: Query embedding (dimension must match stored vectors)
            top_k: Maximum number of results to return
            filters: Optional metadata filters

        Returns:
            ActionResult with search results in data.result field

        Raises:
            FrameworkError: If plugin not available or request fails
        """
        plugin = self._ensure_ready()

        return plugin.search_similar(
            namespace=namespace, query_vector=query_vector, top_k=top_k, filters=filters
        )

    def get_vector(self, namespace: str, vector_id: str) -> ActionResult:
        """Retrieve a specific vector by ID.

        Args:
            namespace: Plugin namespace
            vector_id: Platform-generated vector ID

        Returns:
            ActionResult with vector data in data.result field

        Raises:
            FrameworkError: If plugin not available or request fails
        """
        plugin = self._ensure_ready()

        return plugin.get_vector(namespace=namespace, vector_id=vector_id)

    def delete_vectors(self, namespace: str, vector_ids: list[str]) -> ActionResult:
        """Delete vectors by IDs.

        Args:
            namespace: Plugin namespace
            vector_ids: List of platform-generated vector IDs to delete

        Returns:
            ActionResult with deletion count in data.result field

        Raises:
            FrameworkError: If plugin not available or request fails
        """
        plugin = self._ensure_ready()

        return plugin.delete_vectors(namespace=namespace, vector_ids=vector_ids)

    def delete_by_external_ids(
        self,
        namespace: str,
        external_ids: list[str],
    ) -> ActionResult:
        """Delete vectors by their external_id field.

        Internal API for services that use external_id for cross-referencing.

        Args:
            namespace: Plugin namespace
            external_ids: List of external_id values to delete

        Returns:
            ActionResult with deleted_count in data.result
        """
        plugin = self._ensure_ready()

        return plugin.delete_by_external_ids(
            namespace=namespace,
            external_ids=external_ids,
        )

    def find_missing_external_ids(
        self,
        namespace: str,
        candidate_external_ids: list[str],
    ) -> ActionResult:
        """Return the candidate external_ids that have no active vector.

        Read counterpart to :meth:`delete_by_external_ids`. Internal API for
        services that reconcile their business identifiers against the vector
        store; non-discoverable, external_id-keyed. Soft-deleted vectors count
        as missing.

        Args:
            namespace: Plugin namespace
            candidate_external_ids: external_id values to check for presence

        Returns:
            ActionResult with ``{"missing": [str]}`` in data.result
        """
        plugin = self._ensure_ready()

        return plugin.find_missing_external_ids(
            namespace=namespace,
            candidate_external_ids=candidate_external_ids,
        )

    def delete_all_in_namespace(self, namespace: str) -> ActionResult:
        """Hard-delete every vector in a namespace via configured provider.

        Used for transient stores (e.g., discovery_service process index) that
        are rebuilt from scratch on startup.

        Args:
            namespace: Plugin namespace whose embeddings table should be cleared

        Returns:
            ActionResult with deleted_count
        """
        plugin = self._ensure_ready()

        return plugin.delete_all_in_namespace(namespace=namespace)

    def update_metadata(
        self, namespace: str, vector_id: str, metadata: dict[str, object]
    ) -> ActionResult:
        """Update metadata for a specific vector.

        Args:
            namespace: Plugin namespace
            vector_id: Platform-generated vector ID
            metadata: New metadata dictionary (replaces existing)

        Returns:
            ActionResult with update confirmation in data.result field

        Raises:
            FrameworkError: If plugin not available or request fails
        """
        plugin = self._ensure_ready()

        return plugin.update_metadata(namespace=namespace, vector_id=vector_id, metadata=metadata)

    def list_namespaces(self) -> ActionResult:
        """List all available vector namespaces.

        Returns:
            ActionResult with namespaces list in data.result field

        Raises:
            FrameworkError: If plugin not available or request fails
        """
        plugin = self._ensure_ready()

        return plugin.list_namespaces()

    def get_namespace_stats(self, namespace: str) -> ActionResult:
        """Get statistics for a vector namespace.

        Args:
            namespace: Plugin namespace

        Returns:
            ActionResult with namespace statistics in data.result field

        Raises:
            FrameworkError: If plugin not available or request fails
        """
        plugin = self._ensure_ready()

        return plugin.get_namespace_stats(namespace=namespace)

    def _capture_bootstrap_state(self) -> dict[str, object]:
        """No bootstrap state to capture (bootstrap mode not supported)."""
        return {}

    def _restore_bootstrap_data(self, data: dict[str, object]) -> None:
        """No bootstrap data to restore (bootstrap mode not supported)."""
        pass
