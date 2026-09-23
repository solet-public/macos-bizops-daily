from abc import ABC, abstractmethod
from typing import ClassVar

from ananta.core.domain.types import ActionResult


class BlobStorageServiceInterface(ABC):
    """Service interface for blob storage operations.

    This interface follows the Facade Pattern established in StateManagementInterface.
    Plugins implementing this interface should:
    1. Define service_interfaces property returning tuple containing BlobStorageServiceInterface
    2. Define supported_interface_versions property with version mapping
    3. Accept namespace as first parameter for data isolation
    4. Return ActionResult TypedDict from all operations
    """

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def store_blob(
        self, namespace: str, content: bytes, metadata: dict[str, object]
    ) -> ActionResult:
        """Store blob with metadata, returns blob_id.

        Args:
            namespace: Namespace for data isolation (typically calling plugin name)
            content: Binary content to store
            metadata: Metadata key-value pairs

        Returns:
            ActionResult with blob_id in data field
        """
        ...

    @abstractmethod
    def retrieve_blob(self, blob_id: str) -> ActionResult:
        """Retrieve blob content and metadata by blob_id.

        Searches across all namespaces to find the blob.

        Args:
            blob_id: Unique blob identifier

        Returns:
            ActionResult with content and metadata in data field
        """
        ...

    @abstractmethod
    def delete_blob(self, namespace: str, blob_id: str) -> ActionResult:
        """Delete blob by blob_id.

        Args:
            namespace: Namespace for data isolation
            blob_id: Unique blob identifier

        Returns:
            ActionResult indicating success or failure
        """
        ...

    @abstractmethod
    def search_blobs(self, namespace: str, metadata_filters: dict[str, object]) -> ActionResult:
        """Search blobs by metadata filters.

        Args:
            namespace: Namespace for data isolation
            metadata_filters: Filter criteria for metadata search

        Returns:
            ActionResult with list of matching blob_ids and metadata
        """
        ...

    @abstractmethod
    def get_blob_metadata(self, namespace: str, blob_id: str) -> ActionResult:
        """Get metadata for a specific blob without retrieving content.

        Args:
            namespace: Namespace for data isolation
            blob_id: Unique blob identifier

        Returns:
            ActionResult with metadata in data field
        """
        ...

    @abstractmethod
    def update_blob_metadata(
        self, namespace: str, blob_id: str, metadata: dict[str, object]
    ) -> ActionResult:
        """Update metadata for an existing blob.

        Args:
            namespace: Namespace for data isolation
            blob_id: Unique blob identifier
            metadata: New metadata key-value pairs to merge/replace

        Returns:
            ActionResult indicating success or failure
        """
        ...

    @abstractmethod
    def resolve_blob_path(self, blob_url: str) -> str | None:
        """Resolve blob:// URL to filesystem path.

        Utility method that doesn't require namespace isolation.

        Args:
            blob_url: Blob URL in format blob://...

        Returns:
            Filesystem path or None if not found
        """
        ...

    @abstractmethod
    def file_command(self, command: str) -> ActionResult:
        """Execute Unix-style file commands on blob storage.

        Utility method that operates across all namespaces.

        Commands:
            ls [-l] [--type TYPE] [--sort FIELD] [--count]
            file ID

        Args:
            command: Command string to execute (e.g., 'ls', 'ls -l', 'file ID')

        Returns:
            ActionResult with formatted output in data field
        """
        ...
