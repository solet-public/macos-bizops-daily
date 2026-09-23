from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ananta.services.embedding_service import EmbeddingService


class EmbeddingAwarePlugin:
    """Interface for plugins that need access to the embedding service.

    Plugins implementing this interface will have the embedding service
    injected during initialization via set_embedding_service().

    This follows the same pattern as StateAwarePlugin for service injection.
    """

    def set_embedding_service(self, embedding_service: "EmbeddingService") -> None:
        """Inject the embedding service into the plugin.

        Args:
            embedding_service: The embedding service instance to inject
        """
        self._embedding_service = embedding_service
