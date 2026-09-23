"""Marketo error classification — envelope-first, not HTTP-status-first.

Unlike zuora_plugin (real HTTP status codes for every fault class), Marketo's
REST endpoints return **HTTP 200 with a JSON envelope**
(``{"success": false, "errors": [{"code": "...", "message": "..."}]}``) for
almost every API-level fault. Only the identity/token endpoint and true
transport faults (5xx, connection failures, non-JSON bodies) carry a
meaningful HTTP status. See :data:`constants.MARKETO_ERROR_CODE_MAP` for the
sourced code -> (our error code, retryable) table.

TOPOLOGY HYGIENE (mirrors zuora_plugin's posture): the classified message is
built from Marketo's own ``errors[].message`` text, which describes the
caller's request, not our instance host — never the raw response body or
request URL beyond that.
"""

from __future__ import annotations

from typing import Any

from .constants import ERROR_API_ERROR, MARKETO_ERROR_CODE_MAP


class MarketoServiceError(Exception):
    """A typed plugin-internal fault (e.g. blob storage unavailable at point-of-use)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MarketoEnvelopeError(Exception):
    """Carries a decoded ``success: false`` Marketo envelope for the plugin's classifier."""

    def __init__(self, payload: dict[str, Any]) -> None:
        errors = payload.get("errors")
        first = errors[0] if isinstance(errors, list) and errors else {}
        code = str(first.get("code", "")) if isinstance(first, dict) else ""
        message = str(first.get("message", "")) if isinstance(first, dict) else ""
        super().__init__(message or "Marketo request failed")
        self.payload = payload
        self.marketo_code = code
        self.marketo_message = message


class MarketoTransportError(Exception):
    """A real HTTP-level fault (5xx, non-JSON body) — not a decoded envelope."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Marketo transport fault (status {status_code})")
        self.status_code = status_code


def classify_marketo_envelope(exc: MarketoEnvelopeError) -> tuple[str, str]:
    """Return ``(marketo.* code, topology-safe message)`` for a ``success: false`` envelope."""
    mapped = MARKETO_ERROR_CODE_MAP.get(exc.marketo_code)
    our_code = mapped[0] if mapped is not None else ERROR_API_ERROR
    detail = exc.marketo_message
    prefix = f"Marketo rejected the request (code {exc.marketo_code or 'unknown'})"
    return our_code, f"{prefix}: {detail}" if detail else f"{prefix}."


def is_retryable_auth_code(code: str) -> bool:
    """True for the two codes (601/602) that warrant exactly one token re-mint + retry."""
    from .constants import MARKETO_AUTH_RETRY_CODES

    return code in MARKETO_AUTH_RETRY_CODES
