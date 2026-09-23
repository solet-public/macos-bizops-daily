"""Opaque, scope-bound pagination cursor for the role-inbox section (Control #1a).

The role section of ``peer_inbox`` is a global ``(created_at, id)`` k-way merge
across every role a holder currently holds. Its cursor must be:

* **opaque + versioned** — callers echo it back verbatim, never construct it;
* **scope-bound** — it encodes the visibility mode (``include_important``) and a
  hash of the SORTED held-role set at issue time. If either changes during deep
  pagination, the stale cursor is *reset* (page 1 re-served) rather than used,
  so a newly-visible row is never silently skipped (Codex acceptance check #3);
* **fail-closed** — a malformed / wrong-version / non-decodable token is
  rejected (:class:`RoleCursorRejectedError`), never silently downgraded to page 1.

No HMAC/signature is needed for authorization: the server-side role-visibility
filters (enumerated from ``agent_role_binding`` by the injected
``agent_instance_id``) remain authoritative, so a forged or replayed cursor
cannot widen visibility — it can at worst force a reset.

The wire format is base64 over a pipe-delimited, fixed-field string
``v1|{important}|{roles_hash}|{created_at_iso}|{id}``. None of the fields can
contain ``|`` (role names are hashed, the timestamp is ISO-8601, the id is a
prefixed token), so parsing is an exact 5-field split. The decoded
``created_at`` is normalized to naive UTC so the SQL row-value comparison binds
a type-matched ``timestamp without time zone`` param (the storage seam).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto

_CURSOR_VERSION = "v2"
# Pull-surface boundary (design §5b.ii): a SECOND cursor kind, wire-distinct
# from an ordinary continuation cursor, seeded at a role_covered_mark instead
# of "now". Echoing this token back is the only way to disable the default
# drain's floor for a deliberate pre-mark read (R2) — there is no separate
# caller-supplied boolean, so an accidental deep read is unconstructable.
_HISTORY_CURSOR_VERSION = "v2h"
_FIELD_SEP = "|"
_FIELD_COUNT = 6  # version | important | roles_hash | instance_hash | created_at_iso | id
_ROLE_JOIN = "\n"  # role names cannot contain a newline
_ROLES_HASH_LEN = 16  # truncated sha256 hex — collision-irrelevant (not a secret)


class RoleCursorRejectedError(ValueError):
    """A ``role_after`` token was malformed / forged / wrong-version.

    Raised so the caller fails closed (rejects the request) rather than
    silently restarting at page 1 on a garbage token.
    """


class RoleCursorOutcome(Enum):
    """How a decoded cursor relates to the current pagination scope."""

    VALID = auto()
    SCOPE_CHANGED = auto()


@dataclass(frozen=True, slots=True)
class RoleCursorScope:
    """The scope a role cursor is bound to.

    ``include_important`` is the visibility mode; ``held_roles`` is the set of
    roles the holder held when the cursor was issued. The hash sorts the roles
    first, so ordering is irrelevant — only membership change resets the cursor.
    """

    include_important: bool
    held_roles: tuple[str, ...]
    agent_instance_id: str = ""

    def roles_hash(self) -> str:
        joined = _ROLE_JOIN.join(sorted(self.held_roles))
        digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
        return digest[:_ROLES_HASH_LEN]

    def instance_hash(self) -> str:
        return hashlib.sha256(self.agent_instance_id.encode("utf-8")).hexdigest()[:_ROLES_HASH_LEN]


@dataclass(frozen=True, slots=True)
class RoleCursorDecoded:
    """Result of decoding a ``role_after`` token against the current scope.

    ``VALID`` carries ``(created_at, row_id)`` — the tie-safe ``after`` cursor
    to feed ``query_ordered``. ``SCOPE_CHANGED`` means the held-role set or the
    visibility mode changed since the token was issued, so the caller resets to
    page 1 (``after=None``) — no silent skip of newly-visible rows.
    """

    outcome: RoleCursorOutcome
    created_at: datetime | None = None
    # Preserve the encoded database value for the subsequent state query.
    # ``created_at`` is the typed naïve-UTC view needed by existing callers;
    # this raw ISO form keeps offset-bearing in-memory rows tie-comparable.
    created_at_iso: str | None = None
    row_id: str | None = None
    is_history_token: bool = False


def encode_role_cursor(
    scope: RoleCursorScope, *, created_at_iso: str, row_id: str,
) -> str:
    """Encode the last-emitted ``(created_at, id)`` plus the issuing scope.

    ``created_at_iso`` is the canonical ISO-8601 string of the row's
    ``created_at`` (use :func:`ananta.services.state_service.ordered_query.normalize_sort_value`
    so a postgres ISO-string row and a bootstrap ``datetime`` row encode the
    same way).
    """
    return _encode(_CURSOR_VERSION, scope, created_at_iso=created_at_iso, row_id=row_id)


def encode_role_history_cursor(
    scope: RoleCursorScope, *, created_at_iso: str, row_id: str,
) -> str:
    """Encode a pull-surface-boundary HISTORY cursor, seeded at a mark.

    Wire-distinct from :func:`encode_role_cursor` (``v1h`` vs ``v1``).
    Echoing this token back as ``role_after`` is the only way a caller
    disables the default drain's floor (design §5b.ii) — it is a deliberate
    deep read, never an accidental one, because the token is server-minted
    and opaque like any other role cursor.
    """
    return _encode(
        _HISTORY_CURSOR_VERSION, scope, created_at_iso=created_at_iso, row_id=row_id,
    )


def _encode(
    version: str, scope: RoleCursorScope, *, created_at_iso: str, row_id: str,
) -> str:
    important_flag = "1" if scope.include_important else "0"
    raw = _FIELD_SEP.join((version, important_flag, scope.roles_hash(), scope.instance_hash(), created_at_iso, row_id))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_role_cursor(token: str, scope: RoleCursorScope) -> RoleCursorDecoded:
    """Decode ``token`` against ``scope``; fail closed on anything malformed.

    Returns :class:`RoleCursorDecoded` with ``SCOPE_CHANGED`` when the encoded
    visibility mode or held-role-set hash differs from ``scope`` (reset to page
    1 — floor RE-ENABLED, since a scope change also invalidates history-token
    status). Raises :class:`RoleCursorRejectedError` for a non-decodable,
    wrong-arity, wrong-version, or non-ISO token. ``is_history_token`` is
    ``True`` only for a ``VALID`` outcome decoded from a ``v1h`` token.
    """
    version, important_flag, roles_hash, instance_hash, created_at_iso, row_id = _decode_parts(token)
    if version not in (_CURSOR_VERSION, _HISTORY_CURSOR_VERSION) or important_flag not in (
        "0", "1",
    ):
        raise RoleCursorRejectedError(f"unsupported role cursor: {token!r}")
    issued_important = important_flag == "1"
    if (issued_important != scope.include_important or roles_hash != scope.roles_hash()
            or instance_hash != scope.instance_hash()):
        return RoleCursorDecoded(outcome=RoleCursorOutcome.SCOPE_CHANGED)
    if not row_id:
        raise RoleCursorRejectedError("role cursor is missing the row id")
    return RoleCursorDecoded(
        outcome=RoleCursorOutcome.VALID,
        created_at=_parse_iso(created_at_iso),
        created_at_iso=created_at_iso,
        row_id=row_id,
        is_history_token=version == _HISTORY_CURSOR_VERSION,
    )


def _decode_parts(token: str) -> list[str]:
    if not token:
        raise RoleCursorRejectedError("role cursor must be a non-empty string")
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise RoleCursorRejectedError(f"role cursor is not valid base64: {token!r}") from exc
    parts = raw.split(_FIELD_SEP)
    if len(parts) != _FIELD_COUNT:
        raise RoleCursorRejectedError(
            f"role cursor must have {_FIELD_COUNT} fields: {token!r}",
        )
    return parts


def _parse_iso(created_at_iso: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(created_at_iso)
    except ValueError as exc:
        raise RoleCursorRejectedError(
            f"role cursor created_at is not ISO-8601: {created_at_iso!r}",
        ) from exc
    # Normalize to naive UTC so the SQL row-value comparison binds a
    # type-matched ``timestamp without time zone`` param (the storage seam).
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


__all__ = [
    "RoleCursorDecoded",
    "RoleCursorOutcome",
    "RoleCursorRejectedError",
    "RoleCursorScope",
    "decode_role_cursor",
    "encode_role_cursor",
    "encode_role_history_cursor",
]
