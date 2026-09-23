"""Docs verb implementations — pure functions over a built Docs service.

Same shape as gmail_actions/drive_actions: take an already-built service client
plus a ``params`` dict, return plain result dicts. ``export_document`` takes
the **Drive** service, not the Docs service — Google-native docs are exported
via Drive's ``export_media``, not the product API (see
``drive_actions.export_media_to_blob``).

Invalid parameters raise ``ValueError`` (-> gsuite.invalid_params); Google API
errors propagate to the plugin's ``HttpError`` classifier.
"""

from __future__ import annotations

from typing import Any

from .constants import BLOB_NAMESPACE, DOCS_DEFAULT_EXPORT_FORMAT, DOCS_EXPORT_MIME_BY_FORMAT
from .drive_actions import BlobWriter, export_media_to_blob, resolve_export_mime

_INSERT_AT_START = 1


def create_document(docs: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create a new document with the given title and optional initial text."""
    title = _require_str(params, "title")
    created = docs.documents().create(body={"title": title}).execute()
    document_id = _as_str(created.get("documentId"))
    content = _as_str(params.get("content"))
    if content:
        insert_request = {
            "insertText": {"location": {"index": _INSERT_AT_START}, "text": content},
        }
        docs.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [insert_request]},
        ).execute()
    return {"id": document_id}


def get_document(docs: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch a document's title and its plain-text body content."""
    document_id = _require_str(params, "id")
    document = docs.documents().get(documentId=document_id).execute()
    return {
        "title": _as_str(document.get("title")),
        "body_text": _extract_body_text(document),
    }


def batch_update(docs: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Apply a list of raw Docs API batchUpdate request objects."""
    document_id = _require_str(params, "id")
    requests = _require_requests(params)
    response = docs.documents().batchUpdate(documentId=document_id, body={"requests": requests}).execute()
    return {"replies": response.get("replies") or []}


def export_document(drive: Any, params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """Export a document to pdf/docx/txt via Drive's export_media; return doc_blob_key."""
    document_id = _require_str(params, "id")
    fmt = _as_str(params.get("format")) or DOCS_DEFAULT_EXPORT_FORMAT
    mime = resolve_export_mime(fmt, DOCS_EXPORT_MIME_BY_FORMAT)
    filename = f"{document_id}.{fmt}"
    blob_key = export_media_to_blob(drive, document_id, mime, filename, blob_writer)
    return {"doc_blob_key": blob_key, "namespace": BLOB_NAMESPACE, "filename": filename}


def _extract_body_text(document: dict[str, Any]) -> str:
    body = document.get("body") or {}
    parts: list[str] = []
    for element in body.get("content") or []:
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for piece in paragraph.get("elements") or []:
            text_run = piece.get("textRun") or {}
            content = text_run.get("content")
            if isinstance(content, str):
                parts.append(content)
    return "".join(parts)


def _require_requests(params: dict[str, Any], key: str = "requests") -> list[dict[str, Any]]:
    value = params.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty list of request objects")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"'{key}' must be a list of request objects (dicts)")
    return value


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value
