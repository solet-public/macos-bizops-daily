"""Opaque ``(created_at, id)`` pagination cursor for ``list_threads``.

The thread enumeration orders by the tie-safe composite ``(created_at, id)``;
its page cursor encodes exactly that pair as an opaque, versioned token the
caller echoes back verbatim (never constructs). Unlike the role-inbox cursor
(:mod:`role_cursor`) it carries NO visibility scope — ``list_threads`` is a
global, unscoped substrate enumeration, so there is nothing to bind.

The wire format is base64 over a pipe-delimited fixed-field string
``v1|{created_at_iso}|{id}``. Neither the ISO-8601 timestamp nor the prefixed
id can contain ``|``, so parsing is an exact 3-field split. A malformed /
wrong-version / non-decodable token is REJECTED
(:class:`ThreadCursorRejectedError`), never silently restarted at the beginning.
The decoded ``created_at`` is normalized to naive UTC so the SQL row-value
comparison binds a type-matched ``timestamp without time zone`` param (the
storage seam).
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime

_CURSOR_VERSION = "v1"
_FIELD_SEP = "|"
_FIELD_COUNT = 3  # version | created_at_iso | id


class ThreadCursorRejectedError(ValueError):
    """A ``list_threads`` cursor token was malformed / wrong-version.

    Raised so the caller fails closed (rejects the request) rather than
    silently restarting the enumeration on a garbage token.
    """


def encode_thread_cursor(*, created_at_iso: str, row_id: str) -> str:
    """Encode the last-emitted ``(created_at, id)`` as an opaque token.

    ``created_at_iso`` is the canonical ISO-8601 string of the row's
    ``created_at`` (use
    :func:`ananta.services.state_service.ordered_query.normalize_sort_value`
    so a postgres ISO-string row and a bootstrap ``datetime`` row encode the
    same way).
    """
    raw = _FIELD_SEP.join((_CURSOR_VERSION, created_at_iso, row_id))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_thread_cursor(token: str) -> tuple[datetime, str]:
    """Decode ``token`` to the ``(created_at, row_id)`` ``after`` cursor.

    Fail-closed: raises :class:`ThreadCursorRejectedError` on a non-decodable,
    wrong-arity, wrong-version, missing-id, or non-ISO token.
    """
    version, created_at_iso, row_id = _decode_parts(token)
    if version != _CURSOR_VERSION:
        raise ThreadCursorRejectedError(f"unsupported thread cursor: {token!r}")
    if not row_id:
        raise ThreadCursorRejectedError("thread cursor is missing the row id")
    return _parse_iso(created_at_iso), row_id


def _decode_parts(token: str) -> list[str]:
    if not token:
        raise ThreadCursorRejectedError("thread cursor must be a non-empty string")
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ThreadCursorRejectedError(
            f"thread cursor is not valid base64: {token!r}",
        ) from exc
    parts = raw.split(_FIELD_SEP)
    if len(parts) != _FIELD_COUNT:
        raise ThreadCursorRejectedError(
            f"thread cursor must have {_FIELD_COUNT} fields: {token!r}",
        )
    return parts


def _parse_iso(created_at_iso: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(created_at_iso)
    except ValueError as exc:
        raise ThreadCursorRejectedError(
            f"thread cursor created_at is not ISO-8601: {created_at_iso!r}",
        ) from exc
    # Normalize to naive UTC so the SQL row-value comparison binds a
    # type-matched ``timestamp without time zone`` param (the storage seam).
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


__all__ = [
    "ThreadCursorRejectedError",
    "decode_thread_cursor",
    "encode_thread_cursor",
]
