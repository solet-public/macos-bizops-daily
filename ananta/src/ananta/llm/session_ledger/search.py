"""Search domain mixin for the session-ledger repository.

Holds ``list_events_by_source_window`` — a source-kind/vendor + time-window
event list — plus the LED-01 event-embedding reads
(``list_event_embedding_candidates`` / ``list_events_by_ids``) that back the
:mod:`event_embeddings` policy layer.

SQL-lockdown Slice 7 (Architect-ruled denormalize): the verb's pre-migration
3-table JOIN (event → session → source) retired onto a single-table
``query_ordered`` read over ``__event``, reading the ``session_vendor`` +
``source_kind`` columns denormalized onto each event at append time. The
pure window-filter + projection live as module-level helpers
(:func:`select_events_in_window`) so they are testable offline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from ananta.llm.session_ledger.base import SessionLedgerRepositoryBase, _naive_utc
from ananta.llm.session_ledger.schema import TABLE_EVENT
from ananta.llm.session_ledger.types import EventType, MessageRole

RELOAD_SAFE = True

# The ``query_ordered`` primitive caps a page at 100 rows (Gap-C fail-loud over
# the cap, not a silent clamp), so the verb pre-clamps here. Mirrors every other
# ledger read's literal ``min(limit, 100)`` (e.g. ``read.py`` get_session_timeline
# / list_tool_calls). Pre-migration the raw-SQL path clamped at 200 with no live
# consumer; narrowed per the ledger ≤100-and-page convention.
_MAX_WINDOW_LIMIT = 100

# LED-01 Lane-1 durable drain cursor: a single GLOBAL key-value row holding the
# ``(event_at, id)`` frontier of the event-embedding backfill/steady-state
# walk. KV (not an owned table) keeps the cursor schema-free — one row, opaque
# string value, read/written once per drain page.
_EVENT_EMBED_CURSOR_KV_NAMESPACE = "session_ledger"
_EVENT_EMBED_CURSOR_KV_KEY = "event_embed_cursor"
# Durable fire counter driving the periodic reconciliation full-sweep (Codex
# Lane-1 blocker #2): imported_at is assigned in Python BEFORE the autocommit
# insert, so it is NOT commit-visibility-monotonic — a row can be assigned an
# imported_at that only becomes visible AFTER the incremental cursor has
# advanced past it, and the fast cursor would never re-read it. The periodic
# full find-missing sweep (every _RECONCILE_EVERY_FIRES drain fires) re-checks
# the WHOLE corpus and embeds any such straggler, making completeness a hard
# eventual guarantee rather than a probabilistic one.
_EVENT_EMBED_DRAIN_COUNTER_KV_KEY = "event_embed_drain_fires"


def _event_at_naive(value: object) -> datetime:
    """Parse a row's serialized ``event_at`` back to a naive-UTC datetime.

    ``query_ordered`` runs ``_serialize_for_json`` so the value arrives as a
    naive ISO string (the column is ``timestamp without time zone``); an
    offline shim may hand back a ``datetime`` directly. Either way the result
    is normalized to naive UTC so it compares against the ``_naive_utc``-
    normalized ``since`` bound naive-vs-naive — never the silent
    tz-aware-vs-naive mismatch (F1 seam).
    """
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


def project_event_window_row(row: dict[str, Any]) -> dict[str, Any]:
    """Narrow a raw ``__event`` row to the verb's public per-row envelope.

    The single-table ``query_ordered`` read returns ``SELECT *`` (every event
    column). This projects to the 8 keys the service layer consumes, renaming
    the row's ``id`` to ``event_id`` (the pre-migration SQL aliased
    ``e.id AS event_id``). ``session_vendor`` + ``source_kind`` are the Slice-7
    denormalized columns; the service maps ``session_vendor`` → its public
    ``vendor`` field. Public (LED-01): ``event_embeddings.search`` builds its
    per-hit envelope on the same projection so the two event-listing surfaces
    cannot drift.
    """
    return {
        "event_id": row.get("id"),
        "session_id": row.get("session_id"),
        "sequence": row.get("sequence"),
        "event_at": row.get("event_at"),
        "role": row.get("role"),
        "content_text": row.get("content_text"),
        "session_vendor": row.get("session_vendor"),
        "source_kind": row.get("source_kind"),
    }


def select_events_in_window(
    rows: list[dict[str, Any]],
    *,
    since_naive: datetime | None,
) -> list[dict[str, Any]]:
    """Apply the lower (``since``) window bound + project to the envelope.

    The single-table ``query_ordered`` read already applied the upper bound
    (``event_at <= until`` — the DESC anchor), the ``source_kind`` / vendor
    equality, and the limit. The flat filter grammar is one-condition-per-
    column, so the second ``event_at >= since`` bound CANNOT ride the same
    query; it is applied here on the returned page.

    FAITHFUL to the pre-migration ``WHERE event_at BETWEEN since AND until
    ORDER BY event_at DESC LIMIT n``: every in-window event is newer than every
    dropped ``< since`` event, so the in-window rows are a DESC-prefix of the
    page and the dropped rows are its oldest suffix — the result is exactly the
    newest ``min(n, in-window-count)`` in-window events. A ``< since`` row
    appears in the page ONLY when the in-window count is below ``n`` (otherwise
    the page fills with in-window rows first), so no in-window event is ever
    silently lost to the post-filter. ``since_naive`` is pre-normalized by the
    caller (F1 seam) so the compare is naive-vs-naive.
    """
    kept = (
        rows
        if since_naive is None
        else [row for row in rows if _event_at_naive(row["event_at"]) >= since_naive]
    )
    return [project_event_window_row(row) for row in kept]


class SessionLedgerSearchMixin(SessionLedgerRepositoryBase):
    """Search domain mixin."""

    __slots__ = ()

    def list_events_by_source_window(
        self,
        *,
        source_kind: str | None = None,
        since: datetime | None = None,
        until: datetime,
        limit: int,
        vendor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Single-table event list filtered by source kind / vendor + time window.

        SQL-lockdown Slice 7 (Architect-ruled denormalize): the pre-migration
        3-table JOIN (event → session → source) collapses to one
        ``query_ordered`` read over ``__event`` using the ``session_vendor`` +
        ``source_kind`` columns denormalized at append time (faithful-forever —
        both are INSERT-only on their source rows and events are never
        re-parented). The original ``s.is_deleted = 0`` / ``src.is_deleted = 0``
        join predicates are dropped because no code path soft-deletes a
        session/source/event row, so they were vacuously true; ``query_ordered``
        still applies the event-level ``is_deleted = 0`` by default.

        ``until`` (always supplied — the service wrapper defaults it to
        ``datetime.now(UTC)``) is the DESC anchor and the ONLY ``event_at`` bound
        expressible in the one-condition-per-column filter grammar; the optional
        lower bound ``since`` is applied as a Python post-filter on the page (see
        :func:`select_events_in_window` for the faithfulness proof). Both bounds
        are ``_naive_utc``-normalized (F1 seam — the column is naive-UTC
        ``timestamp without time zone``). ``limit`` is clamped to ``[1, 100]``
        (the ``query_ordered`` cap; pre-migration clamp was 200 — narrowed per
        the ledger ≤100-and-page convention, page via ``until`` if 200 is ever
        needed). The caller-side scope-is-intentional check (at least one of
        source_kind / vendor) lives in the service wrapper.
        """
        filters: dict[str, object] = {
            "event_at": {"op": "lte", "value": _naive_utc(until)},
        }
        if source_kind is not None:
            filters["source_kind"] = source_kind
        if vendor is not None:
            filters["session_vendor"] = vendor
        rows = self._query_ordered(
            TABLE_EVENT,
            filters=filters,
            order_by=[["event_at", "desc"], ["id", "desc"]],
            limit=max(1, min(int(limit), _MAX_WINDOW_LIMIT)),
        )
        since_naive = (
            cast("datetime", _naive_utc(since)) if since is not None else None
        )
        return select_events_in_window(rows, since_naive=since_naive)

    # ------------------------------------------------------------------
    # LED-01 — event-embedding reads
    # ------------------------------------------------------------------

    def list_event_embedding_candidates(
        self,
        *,
        limit: int,
        after: tuple[object, object] | None = None,
        order_column: str = "event_at",
        ascending: bool = False,
    ) -> list[dict[str, object]]:
        """One page of embed-scope candidate ``__event`` rows.

        Applies the SQL-expressible legs of the LED-01 scope filter
        (``event_type = MESSAGE``, ``role = ANY(user, assistant)``,
        ``content_text IS NOT NULL`` — blob-offloaded rows have a NULL
        ``content_text`` so they fall out here by construction); the
        JSON-subfield leg (the Codex ``reasoning`` subtype exclusion) is
        outside the flat filter grammar and lives in
        ``event_embeddings.is_embeddable_event``. ``after`` is the previous
        page's last ``(order_column, id)`` row-value cursor; the primitive
        continues STRICTLY in the chosen direction (``> after`` ascending,
        ``< after`` descending).

        Two orderings back the two LED-01 walks:
        * ``order_column="event_at"`` + ``ascending=False`` (default,
          newest-first by VENDOR wall-clock) backs the operator subset backfill.
        * ``order_column="imported_at"`` + ``ascending=True`` (oldest-first by
          ARRIVAL time) backs the Lane-1 drain's monotonic forward cursor. The
          drain MUST key on ``imported_at`` (platform receive time,
          per-row ``now()`` at append, index ``idx_event_imported_at``), NOT
          ``event_at``: ``event_at`` is vendor wall-clock, so a historical
          session imported AFTER the cursor advanced (a cloud/export/history
          backfill — the ledger's routine load) carries an OLD ``event_at`` and
          would be silently skipped forever by an ``event_at`` frontier. Arrival
          time is monotonic with insertion, so a late historical import always
          sorts AFTER the cursor and is caught.

        Returns full rows — the caller needs ``content_text`` + ``content_json``
        for the Python leg and the chunker, plus ``imported_at`` to advance the
        drain cursor — not just the window projection.
        """
        direction = "asc" if ascending else "desc"
        return self._query_ordered(
            TABLE_EVENT,
            filters={
                "event_type": EventType.MESSAGE.value,
                "role": [MessageRole.USER.value, MessageRole.ASSISTANT.value],
                "content_text": {"op": "is_not_null"},
            },
            order_by=[[order_column, direction], ["id", direction]],
            limit=max(1, min(int(limit), _MAX_WINDOW_LIMIT)),
            after=after,
        )

    # ------------------------------------------------------------------
    # LED-01 Lane-1 — durable drain cursor (KV-backed)
    # ------------------------------------------------------------------

    def get_event_embed_cursor(self) -> str | None:
        """Read the durable Lane-1 drain frontier: the ``imported_at`` (arrival
        time) at-or-before which every embeddable event is already embedded.

        The resume point for
        :meth:`event_embeddings.EventEmbeddingWriter.drain_missing_events`.
        ``None`` (never set) starts the forward walk from the oldest arrival.
        Stored as the raw ``imported_at`` ISO string. Keyed on ARRIVAL time (not
        vendor ``event_at``) so a historical import landing after the cursor is
        never stranded; the drain restarts INCLUSIVELY at this timestamp (via an
        empty-string id sentinel that sorts before every uuid) so same-arrival
        ties re-surface and ``find_missing`` skips the already-embedded ones.
        """
        result = self._state.get_key_value(
            namespace=_EVENT_EMBED_CURSOR_KV_NAMESPACE,
            key=_EVENT_EMBED_CURSOR_KV_KEY,
        )
        if result.get("action_status") != "completed":
            return None
        raw = (result.get("data") or {}).get("value")
        return raw if isinstance(raw, str) and raw else None

    def set_event_embed_cursor(self, imported_at: str) -> None:
        """Persist the drain frontier (an ``imported_at`` arrival timestamp) so a
        restarted drain resumes there instead of re-walking the embedded region."""
        self._state.set_key_value(
            namespace=_EVENT_EMBED_CURSOR_KV_NAMESPACE,
            key=_EVENT_EMBED_CURSOR_KV_KEY,
            value=imported_at,
        )

    def bump_event_embed_drain_counter(self) -> int:
        """Increment and return the durable drain-fire counter.

        Drives the periodic reconciliation full-sweep: when the returned count
        is a multiple of ``_RECONCILE_EVERY_FIRES`` the drain ignores the cursor
        and re-checks the whole corpus (see
        :meth:`event_embeddings.EventEmbeddingWriter.drain_missing_events`).
        Single-slot drain execution means at most one drainer bumps at a time,
        so the read-modify-write needs no compare-and-set.
        """
        result = self._state.get_key_value(
            namespace=_EVENT_EMBED_CURSOR_KV_NAMESPACE,
            key=_EVENT_EMBED_DRAIN_COUNTER_KV_KEY,
        )
        raw = (
            (result.get("data") or {}).get("value")
            if result.get("action_status") == "completed"
            else None
        )
        current = int(raw) if isinstance(raw, str) and raw.isdigit() else 0
        nxt = current + 1
        self._state.set_key_value(
            namespace=_EVENT_EMBED_CURSOR_KV_NAMESPACE,
            key=_EVENT_EMBED_DRAIN_COUNTER_KV_KEY,
            value=str(nxt),
        )
        return nxt

    def list_events_by_ids(self, event_ids: list[str]) -> list[dict[str, object]]:
        """Read live ``__event`` rows by id (the ANN join-back read).

        The raw ``id IN (...)`` becomes the sanctioned list → ``= ANY``
        grammar; ``is_deleted: 0`` is passed explicitly (``query_state``
        does not inject the soft-delete filter).
        """
        if not event_ids:
            return []
        return self._query(
            TABLE_EVENT,
            {"id": list(event_ids), "is_deleted": 0},
        )


__all__ = [
    "RELOAD_SAFE",
    "SessionLedgerSearchMixin",
    "project_event_window_row",
    "select_events_in_window",
]
