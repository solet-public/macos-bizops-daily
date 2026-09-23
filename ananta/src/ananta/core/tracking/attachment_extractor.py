"""Extract attachments from action results using blob_fields mapping.

This runs during tool completion, when blob data is available.
Fetches size_bytes and media_type from blob storage metadata.
"""

from typing import Any

from ananta.core.domain.types import ActionResult
from ananta.core.tracking.blob_field_types import LiteralValue
from ananta.interfaces.attachment_schema import AttachmentFields, MetadataFields
from ananta.services.blob_storage_service import BlobStorageService
from ananta.utils.naming import NamingError, normalize_name, parse_filename


class AttachmentExtractionError(Exception):
    """Raised when attachment cannot be extracted."""


# Type validation rules: (field_name, expected_type, error_message)
_STRING_FIELDS: tuple[tuple[str, str], ...] = (
    (AttachmentFields.BLOB_ID, "blob_id must be a string"),
    (AttachmentFields.NAMESPACE, "namespace must be a string"),
    (AttachmentFields.ARTIFACT_TYPE, "artifact_type must be a string"),
    (AttachmentFields.MEDIA_TYPE, "media_type must be a string"),
    (AttachmentFields.FILENAME, "filename must be a string"),
)


class AttachmentExtractor:
    """Extracts attachments from action results using blob_fields mapping.

    Fetches size_bytes and media_type from blob storage - tools don't need
    to include these in their results.

    blob_fields mapping:
    - Maps to TOP-LEVEL result keys only (no nested/list access)
    - Tools must expose direct blob_id fields, not output_blobs: [id]
    - Use blob_fields_list for multiple attachments

    Example tool result:
        {"audio_blob_id": "bmd-xxx", "filename": "output.wav"}
    Example blob_fields:
        {"blob_id": "audio_blob_id", "filename": "filename", ...}
    """

    METADATA_FIELDS: frozenset[str] = frozenset(
        {MetadataFields.NAME, MetadataFields.ORIGINAL_NAME, MetadataFields.SOURCE_ACTION}
    )

    # Fields fetched from blob storage, not tool results
    BLOB_METADATA_FIELDS: frozenset[str] = frozenset({AttachmentFields.SIZE_BYTES, AttachmentFields.MEDIA_TYPE})

    # Fields required in final attachment (after blob metadata fetch)
    REQUIRED_FIELDS: frozenset[str] = frozenset(
        {AttachmentFields.BLOB_ID, AttachmentFields.NAMESPACE, AttachmentFields.ARTIFACT_TYPE, AttachmentFields.MEDIA_TYPE, AttachmentFields.FILENAME, AttachmentFields.SIZE_BYTES}
    )

    def __init__(self, blob_storage_service: BlobStorageService) -> None:
        self._blob_storage = blob_storage_service

    def extract(
        self,
        action_result: dict[str, object],
        customizations: dict[str, object],
    ) -> list[dict[str, object]]:
        """Extract attachments from action result.

        Returns:
            List of attachment dicts ready for injection

        Raises:
            AttachmentExtractionError: If extraction fails
        """
        attachments: list[dict[str, object]] = []
        self._extract_from_blob_fields(action_result, customizations, attachments)
        self._extract_from_blob_fields_list(action_result, customizations, attachments)
        return attachments

    def _extract_from_blob_fields(
        self,
        action_result: dict[str, object],
        customizations: dict[str, object],
        attachments: list[dict[str, object]],
    ) -> None:
        """Extract single attachment from blob_fields if present."""
        blob_fields = customizations.get("blob_fields")
        if not blob_fields:
            return
        if not isinstance(blob_fields, dict):
            raise AttachmentExtractionError(
                f"blob_fields is not a dict: {type(blob_fields).__name__}"
            )
        attachments.append(self._extract_single(action_result, blob_fields))

    def _extract_from_blob_fields_list(
        self,
        action_result: dict[str, object],
        customizations: dict[str, object],
        attachments: list[dict[str, object]],
    ) -> None:
        """Extract multiple attachments from blob_fields_list if present."""
        blob_fields_list = customizations.get("blob_fields_list")
        if not blob_fields_list:
            return
        if not isinstance(blob_fields_list, list):
            raise AttachmentExtractionError(
                f"blob_fields_list is not a list: {type(blob_fields_list).__name__}"
            )
        for i, fields in enumerate(blob_fields_list):
            if not isinstance(fields, dict):
                raise AttachmentExtractionError(
                    f"blob_fields_list[{i}] is not a dict: {type(fields).__name__}"
                )
            attachments.append(self._extract_single(action_result, fields))

    def _extract_single(
        self,
        result: dict[str, object],
        blob_fields: dict[str, str | LiteralValue | dict[str, object]],
    ) -> dict[str, object]:
        """Extract single attachment from result."""
        data = self._get_result_data(result)
        attachment, metadata = self._extract_mapped_fields(data, blob_fields)
        blob_id, namespace = self._get_blob_refs(attachment)
        self._add_blob_metadata(attachment, namespace, blob_id)
        self._derive_name_and_external_id(attachment, metadata)
        attachment[AttachmentFields.ADDITIONAL_METADATA] = metadata
        self._validate_required_fields(attachment, metadata)
        return attachment

    def _get_result_data(self, result: dict[str, object]) -> dict[str, object]:
        """Extract and validate result['data']."""
        data = result.get("data")
        if not isinstance(data, dict):
            raise AttachmentExtractionError(
                f"action_result['data'] is not a dict: {type(data).__name__}"
            )
        return data

    def _extract_mapped_fields(
        self,
        data: dict[str, object],
        blob_fields: dict[str, str | LiteralValue | dict[str, object]],
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Extract mapped fields from result data."""
        attachment: dict[str, object] = {}
        metadata: dict[str, object] = {}

        for att_field, source in blob_fields.items():
            if att_field in self.BLOB_METADATA_FIELDS:
                continue  # Skip fields fetched from blob storage
            value = self._resolve_field_value(source, data, att_field)
            if att_field in self.METADATA_FIELDS:
                metadata[att_field] = value
            else:
                attachment[att_field] = value

        return attachment, metadata

    def _get_blob_refs(self, attachment: dict[str, object]) -> tuple[str, str]:
        """Validate and return blob_id and namespace."""
        blob_id = attachment.get(AttachmentFields.BLOB_ID)
        if not blob_id or not isinstance(blob_id, str):
            raise AttachmentExtractionError(
                f"blob_id is required and must be a string, got {type(blob_id).__name__}"
            )
        namespace = attachment.get(AttachmentFields.NAMESPACE)
        if not namespace or not isinstance(namespace, str):
            raise AttachmentExtractionError(
                f"namespace is required and must be a string, got {type(namespace).__name__}"
            )
        return blob_id, namespace

    def _add_blob_metadata(
        self, attachment: dict[str, object], namespace: str, blob_id: str
    ) -> None:
        """Fetch and add size/media_type from blob storage."""
        blob_metadata = self._fetch_blob_metadata(namespace, blob_id)
        attachment[AttachmentFields.SIZE_BYTES] = blob_metadata["size_bytes"]
        attachment[AttachmentFields.MEDIA_TYPE] = blob_metadata["media_type"]

    def _derive_name_and_external_id(
        self, attachment: dict[str, object], metadata: dict[str, object]
    ) -> None:
        """Derive name from filename if needed, then derive external_id."""
        if MetadataFields.NAME not in metadata:
            filename = attachment.get(AttachmentFields.FILENAME)
            if not filename:
                raise AttachmentExtractionError(
                    f"Cannot determine name: no '{MetadataFields.NAME}' in blob_fields "
                    f"and no '{AttachmentFields.FILENAME}' in attachment"
                )
            base_name, _ = parse_filename(str(filename))
            metadata[MetadataFields.NAME] = base_name

        name = metadata[MetadataFields.NAME]
        if not isinstance(name, str):
            raise AttachmentExtractionError(
                f"name must be a string, got {type(name).__name__}"
            )
        try:
            metadata[MetadataFields.EXTERNAL_ID] = normalize_name(name)
        except NamingError as e:
            raise AttachmentExtractionError(
                f"Cannot derive external_id from name '{name}': {e}"
            ) from e

    def _fetch_blob_metadata(self, namespace: str, blob_id: str) -> dict[str, Any]:
        """Fetch size and mime_type from blob storage."""
        result = self._blob_storage.get_blob_metadata(namespace, blob_id)
        if result.get("action_status") != "completed":
            error = result.get("error", "Unknown error")
            raise AttachmentExtractionError(
                f"Failed to fetch blob metadata for {blob_id}: {error}"
            )
        return self._extract_blob_metadata(result, blob_id)

    def _extract_blob_metadata(
        self, result: ActionResult, blob_id: str
    ) -> dict[str, Any]:
        """Extract size and mime_type from blob storage result."""
        data = result.get("data")
        if not isinstance(data, dict):
            raise AttachmentExtractionError(
                f"Blob result data is not a dict: {type(data).__name__}"
            )
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            raise AttachmentExtractionError(
                f"Blob metadata is not a dict: {type(metadata).__name__}"
            )
        size = metadata.get("size")
        if size is None:
            raise AttachmentExtractionError(f"Blob {blob_id} missing 'size' in metadata")
        mime_type = metadata.get("mime_type")
        if mime_type is None:
            raise AttachmentExtractionError(
                f"Blob {blob_id} missing 'mime_type' in metadata"
            )
        return {"size_bytes": size, "media_type": mime_type}

    def _resolve_field_value(
        self,
        source: str | LiteralValue | dict[str, object],
        data: dict[str, object],
        field_name: str,
    ) -> object:
        """Resolve a blob_fields value to actual data.

        Handles:
        - LiteralValue objects (in-memory)
        - {"__literal__": value} dicts (deserialized from JSON)
        - str field names (lookup in result data)
        """
        # Handle LiteralValue object (in-memory, not serialized)
        if isinstance(source, LiteralValue):
            return source.value
        # Handle serialized literal format {"__literal__": value}
        if isinstance(source, dict) and "__literal__" in source:
            return source["__literal__"]
        # Handle field name lookup
        if isinstance(source, str):
            if source not in data:
                raise AttachmentExtractionError(
                    f"Field '{source}' not found in action result data "
                    f"(required for attachment field '{field_name}'). "
                    f"Note: blob_fields only supports top-level keys."
                )
            return data[source]
        raise AttachmentExtractionError(
            f"Invalid blob_fields value for '{field_name}': expected str or LiteralValue, "
            f"got {type(source).__name__}"
        )

    def _validate_required_fields(
        self, attachment: dict[str, object], metadata: dict[str, object]
    ) -> None:
        """Validate all required fields are present with correct types."""
        self._validate_presence(attachment)
        self._validate_string_types(attachment)
        self._validate_size_bytes(attachment)
        self._validate_metadata(metadata)

    def _validate_presence(self, attachment: dict[str, object]) -> None:
        """Validate required fields are present."""
        for field in self.REQUIRED_FIELDS:
            if attachment.get(field) is None:
                raise AttachmentExtractionError(
                    f"Required attachment field '{field}' is missing"
                )

    def _validate_string_types(self, attachment: dict[str, object]) -> None:
        """Validate string-typed fields."""
        for field, error_msg in _STRING_FIELDS:
            if not isinstance(attachment.get(field), str):
                raise AttachmentExtractionError(error_msg)

    def _validate_size_bytes(self, attachment: dict[str, object]) -> None:
        """Validate size_bytes is an integer."""
        if not isinstance(attachment.get(AttachmentFields.SIZE_BYTES), int):
            raise AttachmentExtractionError("size_bytes must be an integer")

    def _validate_metadata(self, metadata: dict[str, object]) -> None:
        """Validate additional_metadata fields."""
        if not isinstance(metadata.get(MetadataFields.NAME), str):
            raise AttachmentExtractionError("additional_metadata.name must be a string")
        if not isinstance(metadata.get(MetadataFields.EXTERNAL_ID), str):
            raise AttachmentExtractionError(
                "additional_metadata.external_id must be a string"
            )
