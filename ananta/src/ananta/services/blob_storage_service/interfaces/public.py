"""Blob Storage Service Public API - AI-discoverable operations."""

from abc import ABC, abstractmethod

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.types import ActionResult
from ananta.core.services.service_interface_decorator import service_interface_process
from ananta.core.tracking.blob_field_types import LiteralValue


class BlobStorageAPI(ABC):
    """Public blob storage operations - AI-discoverable via vector search."""

    @service_interface_process(
        name="store_blob",
        is_discoverable=True,
        provider="blob_storage_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace for data isolation (typically calling plugin/service name)",
                required=True,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description="Binary content to store (base64-encoded or raw bytes)",
                required=True,
                type=ParameterType.STRING,
            ),
            "metadata": ParameterMetadata(
                description="Metadata key-value pairs for searchability and organization",
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Storage result with blob_id",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Result data containing blob_id",
                    required=False,
                ),
            },
            usage_patterns=[
                "Store binary files (audio, images, documents)",
                "Save generated content to blob storage",
                "Persist large binary data with metadata",
            ],
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def store_blob(
        self, namespace: str, content: bytes, metadata: dict[str, object]
    ) -> ActionResult:
        """Store blob with metadata."""
        ...

    @service_interface_process(
        name="store_blob_from_file",
        is_discoverable=True,
        provider="blob_storage_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace for data isolation (typically calling plugin/service name)",
                required=True,
                type=ParameterType.STRING,
            ),
            "file_path": ParameterMetadata(
                description="Absolute path to the file to ingest as a blob",
                required=True,
                type=ParameterType.STRING,
            ),
            "filename": ParameterMetadata(
                description=(
                    "Display filename for blob metadata; "
                    "defaults to the basename of file_path when omitted"
                ),
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "mime_type": ParameterMetadata(
                description=(
                    "MIME type for the blob; "
                    "if omitted, inferred from the file extension via mimetypes.guess_type"
                ),
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "metadata": ParameterMetadata(
                description=(
                    "Additional metadata key-value pairs "
                    "(merged with auto-derived filename/mime_type/source_path/byte_count)"
                ),
                required=False,
                type=ParameterType.OBJECT,
                default={},
            ),
            "artifact_type": ParameterMetadata(
                description=(
                    "Semantic type tag (e.g. 'audio', 'image'); "
                    "defaults to the MIME type's primary class when omitted"
                ),
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Storage result with blob_id",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Result data containing blob_id, namespace, metadata",
                    required=False,
                ),
            },
            usage_patterns=[
                "Ingest a finished render (FLAC, M4A, MP4) from disk",
                "Move large binary artifacts into blob storage without round-tripping bytes",
                "Stage cover art (JPEG, PNG) for downstream upload steps",
            ],
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def store_blob_from_file(
        self,
        namespace: str,
        file_path: str,
        filename: str | None = None,
        mime_type: str | None = None,
        metadata: dict[str, object] | None = None,
        artifact_type: str | None = None,
    ) -> ActionResult:
        """Ingest a file from disk as a blob.

        The agent supplies an absolute file_path; the platform reads the bytes,
        derives metadata (filename, mime_type, source_path, byte_count), and
        delegates to the same internal store path used by store_blob.
        """
        ...

    @service_interface_process(
        name="retrieve_blob_by_name",
        is_discoverable=True,
        provider="blob_storage_service",
        parameters={
            "filename": ParameterMetadata(
                description="Filename to retrieve (e.g., 'tremolo_audio.wav', 'my_image.png')",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Retrieved blob with content and metadata",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Blob content and metadata",
                    required=False,
                ),
            },
            usage_patterns=[
                "Retrieve stored binary files",
                "Load content by filename",
                "Access blob metadata and content",
            ],
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            blob_fields={
                "blob_id": "blob_id",
                "namespace": "namespace",
                "artifact_type": LiteralValue("file"),
                "filename": "filename",
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def retrieve_blob_by_name(self, filename: str) -> ActionResult:
        """Retrieve blob content and metadata by filename."""
        ...

    @service_interface_process(
        name="delete_blob",
        is_discoverable=True,
        provider="blob_storage_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace for data isolation", required=True, type=ParameterType.STRING
            ),
            "blob_id": ParameterMetadata(
                description="Unique blob identifier to delete",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Deletion confirmation",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Deletion result", required=False
                ),
            },
            usage_patterns=[
                "Delete temporary files",
                "Clean up old blobs",
                "Remove obsolete content",
            ],
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def delete_blob(self, namespace: str, blob_id: str) -> ActionResult:
        """Delete blob by blob_id."""
        ...

    @service_interface_process(
        name="search_blobs",
        is_discoverable=True,
        provider="blob_storage_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace for data isolation", required=True, type=ParameterType.STRING
            ),
            "metadata_filters": ParameterMetadata(
                description="Filter criteria for metadata search (key-value pairs)",
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Search results with matching blobs",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Search results with list of matching blobs",
                    required=False,
                ),
            },
            usage_patterns=[
                "Find blobs by metadata criteria",
                "Search for files with specific properties",
                "Filter blobs by tags or attributes",
            ],
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def search_blobs(self, namespace: str, metadata_filters: dict[str, object]) -> ActionResult:
        """Search blobs by metadata filters."""
        ...

    @service_interface_process(
        name="get_blob_metadata",
        is_discoverable=True,
        provider="blob_storage_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace for data isolation", required=True, type=ParameterType.STRING
            ),
            "blob_id": ParameterMetadata(
                description="Unique blob identifier", required=True, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Blob metadata without content",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Blob metadata", required=False
                ),
            },
            usage_patterns=[
                "Check blob properties without downloading",
                "Inspect metadata before retrieval",
                "Verify blob existence and attributes",
            ],
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def get_blob_metadata(self, namespace: str, blob_id: str) -> ActionResult:
        """Get metadata for a blob without retrieving content."""
        ...

    @service_interface_process(
        name="file_command",
        is_discoverable=True,
        provider="blob_storage_service",
        parameters={
            "command": ParameterMetadata(
                description="Unix-style command: 'ls' (list files), 'ls -l' (detailed), 'ls --type audio' (filter by type), 'file ID' (inspect file)",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Command output with file listing or file details",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Command output",
                    required=False,
                ),
            },
            usage_patterns=[
                "List all files in blob storage",
                "View detailed file information",
                "Filter files by type (audio, image, text)",
                "Inspect specific file properties",
            ],
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def file_command(self, command: str) -> ActionResult:
        """Execute Unix-style file commands on blob storage."""
        ...
