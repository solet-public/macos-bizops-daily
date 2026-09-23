"""Gmail verb implementations — pure functions over a built Gmail service.

These functions take an already-built ``gmail`` service client (from
:class:`oauth.service_factory.GoogleServiceFactory`) plus a ``params`` dict, and
return plain result dicts. Blob I/O is kept OUT of this module: ``send_message``
receives an ``attachment_loader`` callable so the plugin owns all blob-storage
coupling while this module owns Gmail + MIME.

Invalid parameters raise ``ValueError``; the plugin's safe-call wrapper maps
that to ``gsuite.invalid_params``. Google API errors propagate to the plugin's
``HttpError`` classifier.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from .constants import GMAIL_DEFAULT_MAX_RESULTS, GMAIL_MAX_RESULTS_CAP


@dataclass(frozen=True)
class OutgoingAttachment:
    """A blob resolved to bytes, ready to attach to an outgoing message."""

    filename: str
    mime_type: str
    content: bytes


AttachmentLoader = Callable[[str], OutgoingAttachment]


def list_messages(gmail: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List message ids matching a Gmail search query (newest first)."""
    query = _as_str(params.get("query"))
    max_results = _clamp_max(params.get("max"))
    response = (
        gmail.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    messages = response.get("messages") or []
    rows = [{"id": m.get("id"), "thread_id": m.get("threadId")} for m in messages]
    return {"messages": rows, "count": len(rows)}


def get_message(gmail: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch one message: headers, plain-text body, and attachment metadata."""
    message_id = _require_str(params, "id")
    message = gmail.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = message.get("payload") or {}
    body_text, attachments = _walk_parts(payload)
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "snippet": _as_str(message.get("snippet")),
        "headers": _extract_headers(payload),
        "body_text": body_text,
        "attachments": attachments,
    }


def send_message(
    gmail: Any,
    params: dict[str, Any],
    attachment_loader: AttachmentLoader,
) -> dict[str, Any]:
    """Send a plain-text message with optional blob-backed attachments."""
    to_addr = _require_str(params, "to")
    subject = _as_str(params.get("subject"))
    body = _as_str(params.get("body"))
    blob_ids = _as_str_list(params.get("attachments"))
    attachments = [attachment_loader(blob_id) for blob_id in blob_ids]
    mime = _build_mime(to_addr, subject, body, attachments)
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    sent = gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"id": sent.get("id"), "thread_id": sent.get("threadId")}


# ---------------------------------------------------------------------------
# MIME helpers
# ---------------------------------------------------------------------------


def _build_mime(
    to_addr: str,
    subject: str,
    body: str,
    attachments: list[OutgoingAttachment],
) -> EmailMessage:
    message = EmailMessage()
    message["To"] = to_addr
    message["Subject"] = subject
    message.set_content(body)
    for attachment in attachments:
        maintype, _, subtype = attachment.mime_type.partition("/")
        message.add_attachment(
            attachment.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return message


def _extract_headers(payload: dict[str, Any]) -> dict[str, str]:
    wanted = {"from", "to", "cc", "subject", "date"}
    headers: dict[str, str] = {}
    for header in payload.get("headers") or []:
        name = _as_str(header.get("name")).lower()
        if name in wanted:
            headers[name] = _as_str(header.get("value"))
    return headers


def _walk_parts(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Recursively collect the first text/plain body and all attachment metadata."""
    body_text = ""
    attachments: list[dict[str, Any]] = []

    def visit(part: dict[str, Any]) -> None:
        nonlocal body_text
        mime_type = _as_str(part.get("mimeType"))
        body = part.get("body") or {}
        filename = _as_str(part.get("filename"))
        if filename and body.get("attachmentId"):
            attachments.append(
                {
                    "attachment_id": _as_str(body.get("attachmentId")),
                    "name": filename,
                    "mime": mime_type,
                    "size": int(body.get("size") or 0),
                }
            )
        elif mime_type == "text/plain" and not body_text:
            body_text = _decode_body_data(_as_str(body.get("data")))
        for child in part.get("parts") or []:
            visit(child)

    visit(payload)
    return body_text, attachments


def _decode_body_data(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Param coercion
# ---------------------------------------------------------------------------


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _clamp_max(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return GMAIL_DEFAULT_MAX_RESULTS
    return max(1, min(GMAIL_MAX_RESULTS_CAP, value))
