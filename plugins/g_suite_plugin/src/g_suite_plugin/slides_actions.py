"""Slides verb implementations — pure functions over a built Slides service.

Same shape as gmail_actions/drive_actions: take an already-built service client
plus a ``params`` dict, return plain result dicts. ``export_presentation``
takes the **Drive** service, not the Slides service — Google-native docs are
exported via Drive's ``export_media``, not the product API (see
``drive_actions.export_media_to_blob``).

Invalid parameters raise ``ValueError`` (-> gsuite.invalid_params); Google API
errors propagate to the plugin's ``HttpError`` classifier.
"""

from __future__ import annotations

from typing import Any

from .constants import BLOB_NAMESPACE, SLIDES_DEFAULT_EXPORT_FORMAT, SLIDES_EXPORT_MIME_BY_FORMAT
from .drive_actions import BlobWriter, export_media_to_blob, resolve_export_mime


def create_presentation(slides: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create a new presentation with the given title; return its id."""
    title = _require_str(params, "title")
    created = slides.presentations().create(body={"title": title}).execute()
    return {"id": _as_str(created.get("presentationId"))}


def get_presentation(slides: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch a presentation's slide list (object id + page-element count per slide)."""
    presentation_id = _require_str(params, "id")
    presentation = slides.presentations().get(presentationId=presentation_id).execute()
    rows = [_slide_row(slide) for slide in presentation.get("slides") or []]
    return {"slides": rows, "count": len(rows)}


def batch_update(slides: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Apply a list of raw Slides API batchUpdate request objects."""
    presentation_id = _require_str(params, "id")
    requests = _require_requests(params)
    response = (
        slides.presentations()
        .batchUpdate(presentationId=presentation_id, body={"requests": requests})
        .execute()
    )
    return {"replies": response.get("replies") or []}


def export_presentation(drive: Any, params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """Export a presentation to pdf/pptx via Drive's export_media; return deck_blob_key."""
    presentation_id = _require_str(params, "id")
    fmt = _as_str(params.get("format")) or SLIDES_DEFAULT_EXPORT_FORMAT
    mime = resolve_export_mime(fmt, SLIDES_EXPORT_MIME_BY_FORMAT)
    filename = f"{presentation_id}.{fmt}"
    blob_key = export_media_to_blob(drive, presentation_id, mime, filename, blob_writer)
    return {"deck_blob_key": blob_key, "namespace": BLOB_NAMESPACE, "filename": filename}


def _slide_row(slide: dict[str, Any]) -> dict[str, Any]:
    return {
        "object_id": _as_str(slide.get("objectId")),
        "element_count": len(slide.get("pageElements") or []),
    }


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
