"""Shared attachment schema for IO interfaces and plugins.

Provides TypedDict definitions and field constants to eliminate magic strings
and ensure mypy --strict compliance across attachment handling code.
"""

from typing import NotRequired, Required, TypedDict


class AttachmentFields:
    """Constants for attachment field names."""

    BLOB_ID = "blob_id"
    NAMESPACE = "namespace"
    ARTIFACT_TYPE = "artifact_type"
    MEDIA_TYPE = "media_type"
    FILENAME = "filename"
    SIZE_BYTES = "size_bytes"
    CAPTION = "caption"
    ADDITIONAL_METADATA = "additional_metadata"
    # LLM output field for discretionary attachment selection (list of name strings)
    # Also the post_message parameter name (list of full attachment objects after resolution)
    ATTACHMENTS = "attachments"
    # Injected into result data to tell LLM which attachment names are available
    AVAILABLE_ATTACHMENTS = "available_attachments"


class MetadataFields:
    """Constants for additional_metadata field names."""

    NAME = "name"
    EXTERNAL_ID = "external_id"
    ORIGINAL_NAME = "original_name"
    SOURCE_ACTION = "source_action"


class ProcessKeys:
    """Constants for process key matching."""

    POST_MESSAGE = "post_message"


class AttachmentMetadata(TypedDict, total=False):
    """Metadata stored in additional_metadata.

    name and external_id are required but marked NotRequired here because
    they are derived during attachment construction, not provided by callers.
    """

    name: NotRequired[str]
    external_id: NotRequired[str]
    original_name: NotRequired[str]
    source_action: NotRequired[str]


class InternalAttachment(TypedDict, total=False):
    """Internal attachment format (between result processor and IO plugins).

    Required fields are enforced at runtime by validate_attachment().
    TypedDict total=False allows incremental construction.
    """

    blob_id: Required[str]
    namespace: Required[str]
    artifact_type: Required[str]
    media_type: Required[str]
    filename: Required[str]
    size_bytes: Required[int]
    caption: NotRequired[str]
    additional_metadata: Required[AttachmentMetadata]


class ConsumerAttachment(TypedDict):
    """Consumer-safe attachment format (REST/JSON-RPC responses).

    All fields are required (total=True by default).
    blob_id is excluded - never exposed to consumers.
    """

    name: str
    filename: str
    artifact_type: str
    media_type: str
    size_bytes: int
    download_url: str
