"""External-id backfill mixin for the session-ledger repository.

GAP-5 idempotent-ingest slice 2. Slice 1 added the live ``external_id``
derivation (importer-computed) + the ``(session_id, external_id)`` unique INDEX
+ the ``append_event`` upsert-decouple. Legacy ``__event`` rows predate the
derivation and carry NULL ``external_id``; Postgres treats NULLs as DISTINCT so
they do not violate the unique, but the ``external_id`` NOT-NULL constraint — a
SEPARATE later landing (slice 2b, operator-gated on this backfill reaching 0
nulls) — needs every row stamped first. This one-shot operator backfill stamps
each legacy row with the SAME derivation the live importer uses
(``derive_event_external_id``) so historical re-ingest dedups (best-effort,
operator-ruled "no way to make perfect").

Design (mirrors ``event_source_denorm_backfill`` — no phases, no confirm-gate):

* **idempotent + fill-only.** Stamps only ``external_id IS NULL`` rows; a re-run
  re-derives the SAME id (deterministic) and the fill-only filter makes it a
  no-op once converged.
* **per-session source-order ordinal.** The null-vendor fallback id embeds the
  source-order OCCURRENCE-INDEX within each ``(event_type, role, content_key,
  event_at)`` group; ``sequence`` was allocated in source-order at first ingest,
  so ranking the session's rows by ``sequence`` reproduces the live importer's
  streaming occurrence-counter (which increments for null-vendor events ONLY —
  vendor-present rows take their ``vendor_event_id`` verbatim and consume no
  ordinal). Paged by SESSION keyset because the ordinal needs the per-session
  group context.
* **skip-and-count on a live-window collision.** The slice-1 unique index is
  already live, so a historical event re-ingested post-deploy inserts a NEW
  non-null row; stamping the legacy null row with the SAME derived id would
  collide on ``(session_id, external_id)``. Such a row is SKIPPED (it stays
  NULL) and counted in ``collisions_skipped`` rather than aborting the run or
  silently corrupting. ``collisions_skipped > 0`` is the operator's signal that
  stale null-duplicates remain to dispose of (a separate operator-gated delete
  pass, contract pending) before slice-2b's NOT-NULL can enforce. The main
  backfill stays non-destructive.

The derivation MIRRORS ``importer._event_external_id`` (same content_key /
group-key / ordinal rules over the SAME ``derive_event_external_id`` hash) so a
backfilled id equals what a live re-ingest computes; the round-trip live smoke
``event_external_id_backfill_live_smoke`` locks that agreement against drift.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from ananta.llm.session_ledger.base import (
    LedgerRepositoryError,
    SessionLedgerRepositoryBase,
)
from ananta.llm.session_ledger.schema import TABLE_EVENT, TABLE_SESSION
from ananta.llm.session_ledger.shared import _strip_nuls, derive_event_external_id

# Fetches a previously offloaded event's content_text by ``content_blob_id`` —
# the inverse of the importer's blob offload. Supplied by the SERVICE (which owns
# the blob adapter) so the repository stays state-only.
BlobTextFetcher = Callable[[str], str]

RELOAD_SAFE = True

logger = logging.getLogger(__name__)

# Session keyset page size — sessions are far fewer than events; the ≤100 cap
# matches every other ledger paged read.
_BACKFILL_SESSION_PAGE = 100
# Per-session event page size (sequence keyset) — one session is read as a unit
# but never materialized via a single unbounded query.
_BACKFILL_EVENT_PAGE = 500

# Source-order occurrence counter keyed by the importer's group tuple
# ``(session_id, event_type, role, content_key, event_at)`` — null-vendor rows
# only (MIRRORS ``importer._OrdinalCounter``).
_OrdinalCounter = dict[tuple[str, str, str | None, str, datetime], int]


def _parse_event_at(value: object, event_id: object) -> datetime:
    """Coerce a stored ``event_at`` back to a ``datetime`` for the derivation.

    The state read path SERIALIZES datetimes to ISO strings, so ``query_ordered``
    returns ``event_at`` as a string; ``derive_event_external_id`` canonicalizes a
    ``datetime``. ``fromisoformat`` is the exact inverse of the ``isoformat`` the
    column was serialized with, so the parsed value canonicalizes to the IDENTICAL
    string the live importer produced from ``normalized.event_at``. A direct
    ``datetime`` (other read paths) passes through. Fail-loud otherwise —
    ``event_at`` is a NOT-NULL TIMESTAMP column.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise LedgerRepositoryError(
        f"backfill_event_external_ids: event {event_id!r} has a non-datetime/str "
        f"event_at {value!r} (NOT-NULL TIMESTAMP column; data anomaly)",
    )


def _row_external_id(
    session_id: str,
    row: dict[str, object],
    ordinals: _OrdinalCounter,
    fetch_blob_text: BlobTextFetcher,
) -> str:
    """The stored event row's ``external_id`` — MIRRORS ``importer._event_external_id``.

    ``vendor_event_id`` verbatim when present (consumes NO ordinal); else the
    ``derv:`` hash over the source-order occurrence ordinal within the row's
    ``(event_type, role, content_key, event_at)`` group. ``content_key`` =
    ``_strip_nuls(text) or ""`` where ``text`` is the stored inline
    ``content_text`` OR — when the row is OFFLOADED (``content_blob_id`` set,
    stored ``content_text`` NULL) — the FETCHED blob content. The live importer
    hashes ``_strip_nuls(normalized.content_text)`` (content-addressed; the blob
    is stored as the raw ``normalized.content_text``), so fetching the blob and
    NUL-stripping it reproduces the IDENTICAL content_key — forward dedup of
    offloaded legacy rows holds. The hash itself is the shared
    ``derive_event_external_id`` — one source of truth for live + backfill.
    """
    vendor_event_id = row.get("vendor_event_id")
    if vendor_event_id is not None:
        return str(vendor_event_id)
    event_type = str(row["event_type"])
    role = str(row["role"]) if row.get("role") is not None else None
    content_blob_id = row.get("content_blob_id")
    if content_blob_id is not None:
        text: str | None = fetch_blob_text(str(content_blob_id))
    else:
        stored = row.get("content_text")
        text = stored if isinstance(stored, str) else None
    content_key = _strip_nuls(text) or ""
    event_at = _parse_event_at(row.get("event_at"), row.get("id"))
    key = (session_id, event_type, role, content_key, event_at)
    ordinal = ordinals.get(key, 0)
    ordinals[key] = ordinal + 1
    return derive_event_external_id(
        vendor_event_id=None,
        session_id=session_id,
        event_type=event_type,
        role=role,
        content_key=content_key,
        event_at=event_at,
        ordinal=ordinal,
    )


class SessionLedgerEventExternalIdBackfillMixin(SessionLedgerRepositoryBase):
    """Slice-2 external_id backfill mixin (see module docstring)."""

    __slots__ = ()

    def backfill_event_external_ids(
        self, *, fetch_blob_text: BlobTextFetcher,
    ) -> dict[str, int]:
        """Stamp ``external_id`` on every legacy null-``external_id`` ``__event`` row.

        Idempotent (deterministic re-derivation + fill-only), session-keyset
        paged, skip-and-count on a live-window collision. ``fetch_blob_text``
        recovers an OFFLOADED row's content_text by ``content_blob_id`` (the
        service supplies it from its blob adapter) so offloaded rows derive the
        content-addressed id the live importer uses. Returns
        ``{"sessions_scanned", "events_stamped", "collisions_skipped"}`` — re-run
        to convergence (a clean run reports ``events_stamped`` 0); a non-zero
        ``collisions_skipped`` flags stale null-duplicates the operator must
        dispose of before slice-2b's NOT-NULL.
        """
        now = self._clock()
        sessions_scanned = 0
        events_stamped = 0
        collisions_skipped = 0
        cursor = ""
        while True:
            page = self._query_ordered(
                TABLE_SESSION,
                filters={"id": {"op": "gt", "value": cursor}},
                order_by=[["id", "asc"], ["created_at", "asc"]],
                limit=_BACKFILL_SESSION_PAGE,
            )
            if not page:
                break
            for session in page:
                sessions_scanned += 1
                stamped, skipped = self._stamp_session(
                    str(session["id"]), now=now, fetch_blob_text=fetch_blob_text,
                )
                events_stamped += stamped
                collisions_skipped += skipped
            cursor = str(page[-1]["id"])
        logger.info(
            "backfill_event_external_ids: scanned %d session(s), stamped %d "
            "event(s), skipped %d live-window collision(s)",
            sessions_scanned, events_stamped, collisions_skipped,
        )
        return {
            "sessions_scanned": sessions_scanned,
            "events_stamped": events_stamped,
            "collisions_skipped": collisions_skipped,
        }

    def _stamp_session(
        self, session_id: str, *, now: datetime, fetch_blob_text: BlobTextFetcher,
    ) -> tuple[int, int]:
        """Stamp one session's null-``external_id`` rows in source order; skip-and-count.

        Reads ALL the session's events ordered by ``sequence`` (source order),
        seeds the live ``existing`` set from the already-stamped rows (the
        complete set must be known BEFORE any update — a colliding live row can
        sort AFTER the legacy null row by ``sequence``), then derives + stamps
        each null row, SKIPPING any whose derived id already lives in the session
        (a live-window duplicate). Returns ``(stamped, collisions_skipped)``.
        """
        events = self._read_session_events(session_id)
        existing = {
            str(e["external_id"]) for e in events if e.get("external_id") is not None
        }
        ordinals: _OrdinalCounter = {}
        stamped = 0
        skipped = 0
        for row in events:
            if row.get("external_id") is not None:
                continue
            external_id = _row_external_id(session_id, row, ordinals, fetch_blob_text)
            if external_id in existing:
                skipped += 1
                continue
            self._update(
                TABLE_EVENT,
                {"id": str(row["id"]), "external_id": {"op": "is_null"}},
                {"external_id": external_id, "updated_at": now},
            )
            existing.add(external_id)
            stamped += 1
        return stamped, skipped

    def _read_session_events(self, session_id: str) -> list[dict[str, object]]:
        """All of a session's events ordered by ``sequence`` (source order), paged.

        One session is read as a unit — the ordinal counter and the collision
        set both need the whole session — but sequence-keyset paged so a single
        ``query_ordered`` never materializes a multi-thousand-event session in
        one shot.
        """
        out: list[dict[str, object]] = []
        cursor = -1
        while True:
            page = self._query_ordered(
                TABLE_EVENT,
                filters={
                    "session_id": session_id,
                    "sequence": {"op": "gt", "value": cursor},
                },
                order_by=[["sequence", "asc"], ["id", "asc"]],
                limit=_BACKFILL_EVENT_PAGE,
            )
            if not page:
                break
            out.extend(page)
            cursor = int(str(page[-1]["sequence"]))
        return out


__all__ = [
    "RELOAD_SAFE",
    "SessionLedgerEventExternalIdBackfillMixin",
]
