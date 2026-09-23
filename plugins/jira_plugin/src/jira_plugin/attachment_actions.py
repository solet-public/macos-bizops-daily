"""Attachment verb implementations — the blob-bridge (umbrella design §2.1).

Content flows in through ``blob_key`` params ONLY (resolved via the injected
``attachment_loader``) and out through the injected ``blob_writer`` ONLY. NO verb
here accepts a local filesystem path — an agent-invokable local-file read on a
verb that writes to an external Jira issue is a secret-exfiltration primitive
(the g_suite Phase-2 lesson). ``download_attachment`` returns an
``attachment_blob_key`` referencing the stored bytes.

Own-copy dataclass/type — this module defines its own ``OutgoingAttachment`` and
loader/writer types; it does NOT import from any other plugin.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from .constants import BLOB_NAMESPACE


@dataclass(frozen=True)
class OutgoingAttachment:
    """A blob resolved to bytes, ready to attach to a Jira issue."""

    filename: str
    mime_type: str
    content: bytes


# attachment_loader(blob_key) -> OutgoingAttachment (blob storage is the ONLY byte source)
AttachmentLoader = Callable[[str], OutgoingAttachment]
# blob_writer(content, filename, mime_type) -> blob_id (the returned attachment_blob_key)
BlobWriter = Callable[[bytes, str, str], str]


def download_attachment(
    client: Any,
    params: dict[str, Any],
    blob_writer: BlobWriter,
) -> dict[str, Any]:
    """Download a Jira attachment into blob storage; return its attachment_blob_key."""
    attachment_id = _require_str(params, "attachment_id")
    attachment = client.attachment(attachment_id)
    raw = _resource_raw(attachment)
    filename = _as_str(raw.get("filename")) or attachment_id
    mime = _as_str(raw.get("mimeType")) or "application/octet-stream"
    content = attachment.get()
    if not isinstance(content, (bytes, bytearray)):
        raise ValueError(f"Jira returned non-bytes content for attachment '{attachment_id}'")
    data = bytes(content)
    blob_key = blob_writer(data, filename, mime)
    return {
        "attachment_blob_key": blob_key,
        "namespace": BLOB_NAMESPACE,
        "filename": filename,
        "mime": mime,
        "size": len(data),
    }


def add_attachment(
    client: Any,
    params: dict[str, Any],
    attachment_loader: AttachmentLoader,
) -> dict[str, Any]:
    """Attach bytes from a blob (``blob_key``) to an issue.

    Content comes from blob storage ONLY — the verb deliberately does NOT read an
    arbitrary local filesystem path. To attach a local file, ingest it into blob
    storage first via ``blob_storage_service.store_blob_from_file`` and pass the
    resulting blob_key here.
    """
    key = _require_str(params, "key")
    blob_key = _require_str(params, "blob_key")
    attachment = attachment_loader(blob_key)
    filename = _as_str(params.get("filename")) or attachment.filename
    result = client.add_attachment(
        issue=key, attachment=BytesIO(attachment.content), filename=filename
    )
    return {"attachment_id": _as_str(getattr(result, "id", None)), "filename": filename}


# ---------------------------------------------------------------------------
# Param coercion + resource access
# ---------------------------------------------------------------------------


def _resource_raw(resource: Any) -> dict[str, Any]:
    raw = getattr(resource, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value
