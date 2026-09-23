"""Role-inbox section assembly: global k-way merge + threadless projection.

The role section of ``peer_inbox`` is a single globally-ordered, globally-
limited stream across every role a holder currently holds — NOT a per-role
concatenation (which would dup/skip at page boundaries). All role rows live in
the one ``core__agent_role_message`` table, so ``id`` is table-global and
``(created_at, id)`` totally orders rows across roles; the next page re-runs
each per-role query with ``after = next_role_cursor`` and merges again, with no
cross-role dup or skip (v9-B3).

Each role row is threadless (the role send creates NO ``agent_thread`` row), so
it projects to a :class:`PeerInboxEntry` straight from the envelope columns:
``thread_id`` is the synthetic ``"role:{recipient_key}"`` handle (display
only), the projected ``AgentMessageRow.id`` is the envelope ``message_id``, and
``cursor`` is the ``0`` sentinel (role rows never live in ``core__agent_message``
and never page by ``message.cursor``, so the sentinel cannot collide).
"""

from __future__ import annotations

import json
from datetime import datetime

from ananta.services.state_service.ordered_query import normalize_sort_value

from .models import (
    AgentMessageRow,
    MessageContent,
    MessageKind,
    MessageRole,
    PeerInboxEntry,
    TextPart,
)
from .role_cursor import RoleCursorScope, encode_role_cursor, encode_role_history_cursor

_COL_CREATED_AT = "created_at"
_COL_ID = "id"

# Pull-surface boundary design §5b.v: picked at build time. ~4 KB/entry
# measured (422,513 chars / 50+50 entries) means this binds after roughly 50
# entries in the worst case — above the common-case limit=50 row cap (which
# still binds first in the ordinary case) but low enough that a handful of
# wide entries (the measured 108,741-char single-page specimen) still trips
# it well under a context-blowing page. Page-level only (R4): bounds a whole
# page's serialized size, never an individual entry.
ROLE_SECTION_BYTE_CEILING = 200_000


def build_role_section(
    per_role_records: list[list[dict[str, object]]],
    *,
    scope: RoleCursorScope,
    limit: int,
    any_floor_truncated: bool = False,
    resume_seed: tuple[str, str] | None = None,
) -> tuple[tuple[PeerInboxEntry, ...], str | None, bool, str | None]:
    """Merge per-role pages into the global top-``limit`` + the next cursor.

    ``per_role_records`` is one ``query_ordered`` result list per held role
    (each already ``(created_at, id)``-descending, capped at ``limit + 1`` for
    truthful global exhaustion, and —
    per the caller's own floor step — already excludes any row at/below that
    role's ``role_covered_mark`` unless the caller echoed back a history
    cursor). Returns ``(entries, next_role_cursor, role_floor_applied,
    role_history_cursor)``.

    Three DISTINCT mint outcomes (design §3/§4, R2/R4), checked in this
    priority order so they stay distinguishable rather than conflated:

    1. **Byte-stop** — the byte-aware merge truncated the page short of
       ``limit`` rows. Mints a REAL continuation cursor from the last emitted
       row (there is unambiguously more to fetch).
    2. **Lookahead-proven row limit** — one or more rows remain after the
       emitted ``limit`` rows, with no byte truncation. Mints a REAL
       continuation cursor. An exact-limit page with no lookahead is exhausted
       and therefore returns ``None``.
    3. **Floor-stop** — no byte or row truncation, but the floor removed at
       least one already-covered row from some role's fetch this call. Mints
       ``next_role_cursor=None`` (the default drain genuinely IS complete)
       plus the caller-supplied ``role_floor_applied=True`` and a
       server-minted history cursor seeded at ``resume_seed`` (the MAX/newest
       mark across held roles — over-page-safe per §12.3, see design §5b.iii)
       so a caller that wants the pre-mark backlog has a named, unforgeable
       route (R2) rather than an unconstructable "paging past the mark stays
       available forever" claim. Disclosed edge (design §5b.vii): the
       history cursor reveals rows STRICTLY OLDER than the mark, never the
       mark's own row — that row was already delivered to whichever session
       set the mark (attestation only ever follows processing).

    Otherwise (genuine exhaustion, no floor involvement): ``next_role_cursor
    =None``, ``role_floor_applied=False``, no history cursor — unchanged from
    today.
    """
    flat = [record for records in per_role_records for record in records]
    flat.sort(key=_merge_sort_key, reverse=True)
    merged, byte_truncated = _take_within_byte_ceiling(flat, limit)
    entries = tuple(project_role_entry(record) for record in merged)

    has_more = len(flat) > len(merged)
    if byte_truncated or has_more:
        last = merged[-1]
        next_cursor = encode_role_cursor(
            scope,
            created_at_iso=normalize_sort_value(last.get(_COL_CREATED_AT)),
            row_id=str(last.get(_COL_ID, "")),
        )
        return entries, next_cursor, any_floor_truncated, None

    if any_floor_truncated and resume_seed is not None:
        history_cursor = encode_role_history_cursor(
            scope, created_at_iso=resume_seed[0], row_id=resume_seed[1],
        )
        return entries, None, True, history_cursor

    return entries, None, any_floor_truncated, None


def _take_within_byte_ceiling(
    flat: list[dict[str, object]], limit: int,
) -> tuple[list[dict[str, object]], bool]:
    """Row-limit cap, THEN a page-level byte ceiling (design §4/R4).

    R4's edges: page-level only (never per-entry truncation), and a page
    always admits AT LEAST ONE entry — a single over-ceiling entry ships
    alone rather than starving the walk or minting a zero-row page.
    ``byte_truncated`` is True only when the byte ceiling — not the row
    limit — is what stopped the page short; the two are kept distinguishable
    because the mint predicate above branches on which one fired.
    """
    row_capped = flat[:limit]
    kept: list[dict[str, object]] = []
    running_bytes = 0
    for record in row_capped:
        size = len(json.dumps(record, default=str))
        if kept and running_bytes + size > ROLE_SECTION_BYTE_CEILING:
            return kept, True
        kept.append(record)
        running_bytes += size
    return kept, False


def _merge_sort_key(record: dict[str, object]) -> tuple[str, str]:
    return (
        normalize_sort_value(record.get(_COL_CREATED_AT)),
        normalize_sort_value(record.get(_COL_ID)),
    )


def merge_undelivered_oldest_first(
    per_role_records: list[list[dict[str, object]]], limit: int,
) -> list[dict[str, object]]:
    """Global OLDEST-first ``(created_at, id)`` merge of per-role undelivered
    pages, capped at ``limit`` — the repair-drain page (Control #5).

    The drain emits this page, flips each ``delivered=true``, and re-queries
    (the flipped rows drop out of the ``delivered=false`` filter) until empty,
    so it needs no cursor — just the oldest ``limit`` owed rows each pass. FIFO
    oldest-first means an old row's wait is bounded by its position and never
    starves under newer arrivals.
    """
    flat = [record for records in per_role_records for record in records]
    flat.sort(key=_merge_sort_key)  # ascending == oldest-first (no reverse)
    return flat[:limit]


def project_role_entry(record: dict[str, object]) -> PeerInboxEntry:
    """Project one ``agent_role_message`` envelope row to a ``PeerInboxEntry``."""
    sender_agent_id = str(record.get("sender_agent_id", ""))
    sender_agent_instance_id = str(record.get("sender_agent_instance_id", ""))
    sender_session_label = str(record.get("sender_session_label") or "")
    sender_principal_kind = str(record.get("sender_principal_kind") or "unknown")
    sender_transport_principal = str(
        record.get("sender_transport_principal") or "unknown",
    )
    sender_identity_trust = str(record.get("sender_identity_trust") or "unknown")
    thread_id = str(record.get("thread_id", ""))
    message = AgentMessageRow(
        id=str(record.get("message_id", "")),
        thread_id=thread_id,
        cursor=0,
        role=MessageRole.ORIGINATOR,
        kind=MessageKind.MESSAGE,
        content=_deserialize_content(record.get("content")),
        created_at=_coerce_datetime(record.get(_COL_CREATED_AT)),
        metadata={
            "peer": True,
            "sender_agent_id": sender_agent_id,
            "sender_agent_instance_id": sender_agent_instance_id,
            "sender_session_label": sender_session_label,
            "sender_principal_kind": sender_principal_kind,
            "sender_transport_principal": sender_transport_principal,
            "sender_identity_trust": sender_identity_trust,
            "important": bool(record.get("important", False)),
            # These are envelope facts, not client-supplied pagination data.
            # Wake reconciliation needs the exact durable row identity.
            "recipient_key": str(record.get("recipient_key", "")),
            "delivery_external_id": str(record.get("external_id", "")),
            "role_created_at": normalize_sort_value(record.get(_COL_CREATED_AT)),
            "role_row_id": str(record.get(_COL_ID, "")),
        },
    )
    return PeerInboxEntry(
        thread_id=thread_id,
        sender_agent_id=sender_agent_id,
        sender_agent_instance_id=sender_agent_instance_id,
        sender_session_label=sender_session_label,
        message=message,
    )


def _deserialize_content(raw: object) -> MessageContent:
    """Rebuild the typed message parts from the JSON-stored envelope content."""
    if not isinstance(raw, list):
        return []
    parts: list[TextPart] = []
    for item in raw:
        if isinstance(item, dict):
            parts.append(
                TextPart(
                    type=str(item.get("type", "text")),
                    text=str(item.get("text", "")),
                ),
            )
    return parts


def _coerce_datetime(value: object) -> datetime:
    """Coerce a stored ``created_at`` (postgres ISO string or bootstrap datetime).

    The standardizer always populates ``created_at``, so an unparseable value
    is a genuine data anomaly — fail loud rather than silently fabricate a
    timestamp (Codex acceptance check #4: handle missing/invalid fields
    explicitly).
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"agent_role_message.created_at is not ISO-8601: {value!r}",
            ) from exc
    raise ValueError(
        f"agent_role_message.created_at has unexpected type "
        f"{type(value).__name__}: {value!r}",
    )


__all__ = [
    "build_role_section",
    "merge_undelivered_oldest_first",
    "project_role_entry",
]
