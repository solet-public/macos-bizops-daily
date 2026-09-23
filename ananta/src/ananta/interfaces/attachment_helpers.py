"""Shared attachment formatting and validation helpers.

Keeps attachment logic out of io_interface_service, REST plugin, and JSON-RPC plugin
to maintain Single Responsibility Principle and complexity A/B ratings.

All functions follow fail-fast: raise on any missing required field.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from ananta.interfaces.attachment_schema import (
    AttachmentFields,
    ConsumerAttachment,
    MetadataFields,
)


def _require_str(
    value: Any, field_name: str, context: str | None = None
) -> str:
    """Validate a required string field.

    Raises:
        ValueError: If value is missing/falsy or not a string
    """
    if not value:
        ctx = f": {context}" if context else ""
        raise ValueError(f"Attachment missing {field_name}{ctx}")
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be str, got {type(value).__name__}")
    return value


def _require_dict(
    value: Any, field_name: str, context: str | None = None
) -> dict[str, Any]:
    """Validate a required dict field.

    Raises:
        ValueError: If value is missing/falsy or not a dict
    """
    if not value:
        ctx = f": {context}" if context else ""
        raise ValueError(f"Attachment missing {field_name}{ctx}")
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be dict, got {type(value).__name__}")
    return value


def _require_int(
    value: Any, field_name: str, context: str | None = None
) -> int:
    """Validate a required int field.

    Raises:
        ValueError: If value is None or not an int
    """
    if value is None:
        ctx = f": {context}" if context else ""
        raise ValueError(f"Attachment missing {field_name}{ctx}")
    if not isinstance(value, int):
        raise ValueError(f"{field_name} must be int, got {type(value).__name__}")
    return value


def validate_attachment(
    att: Mapping[str, Any],
) -> tuple[str, str, str, str, str, str, int]:
    """Validate attachment has ALL required fields with correct types.

    Returns:
        Tuple of (name, external_id, namespace, filename, artifact_type, media_type, size_bytes)

    Raises:
        ValueError: If any required field is missing or has wrong type
    """
    blob_id = _require_str(att.get(AttachmentFields.BLOB_ID), "blob_id")
    namespace = _require_str(att.get(AttachmentFields.NAMESPACE), "namespace", blob_id)
    metadata = _require_dict(
        att.get(AttachmentFields.ADDITIONAL_METADATA), "additional_metadata", blob_id
    )

    external_id = _require_str(
        metadata.get(MetadataFields.EXTERNAL_ID), "external_id", blob_id
    )
    name = _require_str(metadata.get(MetadataFields.NAME), "name", external_id)

    filename = _require_str(
        att.get(AttachmentFields.FILENAME), "filename", external_id
    )
    artifact_type = _require_str(
        att.get(AttachmentFields.ARTIFACT_TYPE), "artifact_type", external_id
    )
    media_type = _require_str(
        att.get(AttachmentFields.MEDIA_TYPE), "media_type", external_id
    )
    size_bytes = _require_int(
        att.get(AttachmentFields.SIZE_BYTES), "size_bytes", external_id
    )

    return name, external_id, namespace, filename, artifact_type, media_type, size_bytes


def to_consumer_attachment(
    att: Mapping[str, Any], download_url_prefix: str = "/api/v1/blobs"
) -> ConsumerAttachment:
    """Convert internal attachment to consumer format.

    Raises:
        ValueError: If any required field is missing
    """
    (
        name,
        _external_id,
        _namespace,
        filename,
        artifact_type,
        media_type,
        size_bytes,
    ) = validate_attachment(att)

    # Use blob_id in URL for security (non-guessable), filename in Content-Disposition
    blob_id = _require_str(att.get(AttachmentFields.BLOB_ID), "blob_id")
    # Strip blob:// prefix if present (added internally for plugin resolution)
    if blob_id.startswith("blob://"):
        blob_id = blob_id[7:]

    return {
        "name": name,
        "filename": filename,
        "artifact_type": artifact_type,
        "media_type": media_type,
        "size_bytes": size_bytes,
        "download_url": f"{download_url_prefix}/{blob_id}",
    }


def prepare_consumer_attachments(
    attachments: Sequence[Mapping[str, Any]],
    download_url_prefix: str = "/api/v1/blobs",
) -> list[ConsumerAttachment]:
    """Transform list of internal attachments to consumer format.

    Raises:
        ValueError: If any attachment is missing required fields
    """
    return [to_consumer_attachment(att, download_url_prefix) for att in attachments]
