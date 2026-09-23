"""Pure post-read helpers for the session-ledger read mixin.

The SQL-lockdown migration retires the read-side raw ``execute_sql`` calls onto
the typed ``query_state`` / ``query_ordered`` primitives + Python post-processing
(merges, folds, projections the flat filter grammar cannot express). That
post-processing lives here as module-level pure functions so
:class:`ananta.llm.session_ledger.read.SessionLedgerReadMixin` stays a focused,
sub-500-LOC query surface as each read migrates (the god-class coherence-over-
size budget): a migrated read is a thin mixin method that issues its
``query_state`` reads and delegates the Python shaping to a helper here.

Most helpers are pure functions over already-fetched dict rows — no DB handle, no
service reference — so each is unit-testable in isolation and reused unchanged
when a read's *fetch* mechanism later changes (e.g. a raw read becoming
read-then-route). A few (``fold_census_events``, ``build_census``) are read
*orchestration* that the heaviest migrations push down here for the god-class
budget: they DRIVE reads but still hold no DB handle or service reference — the
typed read seam (the mixin's bound ``_query`` / ``_query_ordered``) is passed in
as an injected callback, so they stay pure-by-injection and unit-testable with a
fake reader.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from ananta.llm.session_ledger.base import LedgerRepositoryError
from ananta.llm.session_ledger.schema import (
    TABLE_EVENT,
    TABLE_IMPORT_BATCH,
    TABLE_SESSION,
    TABLE_SESSION_SOURCE_KIND,
    TABLE_SOURCE,
    TABLE_TOOL_CALL,
)
from ananta.llm.session_ledger.types import (
    ImportBatchStatus,
    IngestSourceKind,
    SessionsOrderBy,
    SourceVendor,
)
from ananta.services.state_service.read_bounds import MAX_READ_ROWS

RELOAD_SAFE = True

# Operator ruling 2026-06-30: the M6 auto-summarizer summarizes real conversations
# only. ``agent_messaging`` sessions are 1-event peer-coordination chatter (not
# user/assistant conversation), so they are excluded from the quiescent selection
# entirely — otherwise newest-first wastes every pass marking the continuous
# stream of recent coordination noise trivial instead of summarizing conversations.
_NON_CONVERSATION_SOURCE_KINDS = frozenset({IngestSourceKind.AGENT_MESSAGING.value})


def _content_json_subtype(value: object) -> str | None:
    """Best-effort ``content_json->>'subtype'`` over a dict-or-JSON-str payload.

    Mirrors the Postgres ``->>'subtype'`` semantics the migrated
    :meth:`SessionLedgerReadMixin.find_latest_away_summary_for_session`
    replaced: a payload that is not a JSON object (array / scalar /
    unparseable) yields ``None`` rather than raising, so an unrelated SYSTEM
    event cannot crash the lookup. JSONB reads back as a ``dict`` on the
    ``query_state`` path, but some read paths hand back the raw JSON ``str`` —
    handle both (the JSONB-deserialization seam).
    """
    if isinstance(value, str):
        if not value:
            return None
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    if isinstance(value, dict):
        subtype = value.get("subtype")
        return subtype if isinstance(subtype, str) else None
    return None


def _pick_canonical_session_id(rows: list[dict[str, object]]) -> str | None:
    """Canonical-first / ``created_at`` ASC top-1 pick over rows sharing one external id.

    SQL-lockdown Slice 6: the migrated
    :meth:`SessionLedgerReadMixin.find_session_id_by_external_session_id`
    replaced an ``ORDER BY (canonical_external_session_id IS NOT NULL),
    created_at ASC LIMIT 1`` whose leading key is a computed boolean the flat
    ``query_state`` grammar cannot express. The sort key mirrors the SQL
    exactly: ``canonical_external_session_id is not None`` (``False`` < ``True``
    → canonical first, matching Postgres BOOLEAN FALSE < TRUE) then
    ``created_at`` ASC — a NOT NULL timestamp whose on-read form sorts
    chronologically as a string (the ``list_sources`` precedent). Returns the
    winning row's ``id``, or ``None`` for an empty input.
    """
    if not rows:
        return None
    rows.sort(
        key=lambda r: (
            r.get("canonical_external_session_id") is not None,
            str(r.get("created_at", "")),
        )
    )
    return str(rows[0]["id"])


def _select_latest_away_summary(rows: list[dict[str, object]]) -> str | None:
    """Most-recent live away_summary recap text from a session's SYSTEM events.

    SQL-lockdown Slice 6: the migrated
    :meth:`SessionLedgerReadMixin.find_latest_away_summary_for_session`
    replaced a ``content_json->>'subtype' = 'away_summary'`` JSONB-path filter
    (which the flat grammar cannot express) + ``ORDER BY event_at DESC,
    sequence DESC LIMIT 1``. ``rows`` is the SYSTEM-event set the equality /
    ``is_not_null`` query already narrowed; the subtype match + recency pick
    happen here. ``event_at`` is a NOT NULL timestamp (string-sortable
    chronologically); ``sequence`` is the NOT NULL int tie-break. Returns the
    trimmed recap text, or ``None`` when no away_summary event is present.
    """
    candidates = [
        row
        for row in rows
        if _content_json_subtype(row.get("content_json")) == "away_summary"
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda r: (str(r.get("event_at", "")), cast(int, r.get("sequence", 0))),
        reverse=True,
    )
    raw = candidates[0].get("content_text")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _merge_active_leases(
    leases: list[dict[str, object]],
    sessions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Inner-merge live leases with their live sessions, newest-expiry first.

    SQL-lockdown #11: the replacement shaping for the raw
    ``session INNER JOIN active_lease`` that :meth:`list_active_sessions`
    retired. ``leases`` / ``sessions`` are the two ``query_state`` reads the
    method issued (active leases by ``expires_at > now``; their sessions by
    ``id`` =ANY). Emits one row per lease whose session is live — the INNER-JOIN
    cardinality — projecting the session-sourced fields + the lease's
    ``last_seen_at`` / ``expires_at``, then sorts ``expires_at`` DESC with an
    ``id`` total-order tie-break (the old ``ORDER BY expires_at DESC`` left ties
    arbitrary — a faithful superset). Datetime columns return as ISO strings on
    both the old and new read paths (shared ``_serialize_for_json``); the sort
    parses ``expires_at`` so the order is chronological, not lexical-by-accident.
    """
    session_by_id = {str(row["id"]): row for row in sessions}
    merged: list[dict[str, object]] = []
    for lease in leases:
        session = session_by_id.get(str(lease["session_id"]))
        if session is None:
            continue
        merged.append(
            {
                "id": session["id"],
                "source_id": session["source_id"],
                "external_session_id": session["external_session_id"],
                "vendor": session["vendor"],
                "vendor_session_label": session["vendor_session_label"],
                "project_path": session["project_path"],
                "last_event_at": session["last_event_at"],
                "last_seen_at": lease["last_seen_at"],
                "expires_at": lease["expires_at"],
            }
        )
    merged.sort(
        key=lambda row: (
            datetime.fromisoformat(str(row["expires_at"])),
            str(row["id"]),
        ),
        reverse=True,
    )
    return merged


def _build_canonical_contributors_result(
    rows: list[dict[str, object]],
    *,
    session_id: str,
) -> dict[str, object]:
    """Project a ``(vendor, external_session_id)`` group into the W5.B C3 shape.

    ``rows`` is the group's live members (canonical-first, ``source_kind`` ASC)
    the ``list_canonical_contributors`` read already fetched. Builds the three
    explicit top-level fields (``canonical_session_id`` / ``…external_session_id``
    / ``vendor``) + the ``orphaned_canonical`` flag (``True`` when the canonical
    row is soft-deleted but siblings remain) + the per-contributor projection.
    Raises :class:`LedgerRepositoryError` on an empty group (input not found, or
    input + every contributor ``is_deleted=1``) — the fail-fast contract the
    verb has always carried.
    """
    if not rows:
        raise LedgerRepositoryError(
            f"list_canonical_contributors: session_id={session_id!r} "
            "not found or has no live members in its "
            "(vendor, external_session_id) group"
        )
    vendor_value = str(rows[0].get("vendor", ""))
    external_session_id_value = str(rows[0].get("external_session_id", ""))
    canonical_row = next((r for r in rows if r.get("is_canonical")), None)
    canonical_session_id: str | None
    orphaned_canonical: bool
    if canonical_row is not None:
        canonical_session_id = str(canonical_row.get("session_id", ""))
        orphaned_canonical = False
    else:
        canonical_session_id = None
        orphaned_canonical = True
    contributors: list[dict[str, object]] = []
    for r in rows:
        count_raw = r.get("contributed_event_count")
        count = int(count_raw) if isinstance(count_raw, int) else 0
        contributors.append(
            {
                "session_id": str(r.get("session_id", "")),
                "source_id": str(r.get("source_id", "")),
                "source_kind": str(r.get("source_kind", "")),
                "first_event_at": r.get("first_event_at"),
                "last_event_at": r.get("last_event_at"),
                "contributed_event_count": count,
                "is_canonical": bool(r.get("is_canonical")),
            }
        )
    return {
        "canonical_session_id": canonical_session_id,
        "canonical_external_session_id": external_session_id_value,
        "vendor": vendor_value,
        "orphaned_canonical": orphaned_canonical,
        "contributors": contributors,
    }


def build_canonical_contributors_via_group(
    query: _StateReader,
    *,
    session_id: str,
) -> dict[str, object]:
    """list_canonical_contributors read-then-route (SQL-lockdown — the LAST ledger _fetch_all).

    Replaces the CTE + INNER-JOIN-``__source`` raw SQL with three ``query_state``
    reads + a Python project/sort, feeding the unchanged
    :func:`_build_canonical_contributors_result`:

    1. Resolve the input's group key — ``query(session, {id, is_deleted: 0})``.
       Empty (input not found / soft-deleted) → ``[]`` → the builder's fail-fast
       raise (the verb's always-carried contract).
    2. Read the live group — ``query(session, {vendor: V, external_session_id: E,
       is_deleted: 0})``. Siblings carry the same ``(vendor, external_session_id)``
       as their canonical (the ``canonical_pointer_repair`` lift), so this is the
       full group for BOTH canonical-input and sibling-input.
    3. Resolve per-contributor ``source_kind`` — :func:`_source_kind_by_id`
       (``query(source, {id: =ANY(source_ids)})``, NO ``is_deleted`` filter,
       faithful to the predicate-less INNER JOIN).
    4. :func:`_project_contributors` — the 9-key shape + the canonical-first /
       ``source_kind`` ASC sort, byte-identical to the pre-migration ``ORDER BY
       (canonical_external_session_id IS NOT NULL), source_kind ASC``
       (``query_state`` has no ORDER BY).
    5. :func:`_build_canonical_contributors_result` unchanged.

    Datetime fidelity (the Architect's catch): the projected ``first_event_at`` /
    ``last_event_at`` are parsed back to NAIVE datetime in step 4 — ``query_state``
    serializes them to ISO strings (``_serialize_for_json``) but the unchanged
    builder + its downstream consumer expect the ``datetime`` the raw ``_fetch_all``
    returned.
    """
    input_rows = query(TABLE_SESSION, {"id": session_id, "is_deleted": 0})
    if not input_rows:
        return _build_canonical_contributors_result([], session_id=session_id)
    head = input_rows[0]
    group = query(
        TABLE_SESSION,
        {
            "vendor": head["vendor"],
            "external_session_id": head["external_session_id"],
            "is_deleted": 0,
        },
    )
    source_kind_by_id = _source_kind_by_id(query, group)
    projected = _project_contributors(group, source_kind_by_id)
    return _build_canonical_contributors_result(projected, session_id=session_id)


def _source_kind_by_id(
    query: _StateReader, group: list[dict[str, object]],
) -> dict[str, object]:
    """``{source_id: source_kind}`` for the group's sources (faithful to the INNER JOIN).

    Reads ``__source`` by id WITHOUT an ``is_deleted`` filter — the pre-migration
    INNER JOIN carried no ``src.is_deleted`` predicate, so a soft-deleted source
    still contributes its kind (``query_state`` does not auto-exclude is_deleted).
    The caller drops a group row whose ``source_id`` has no row here (the
    INNER-JOIN drop). Empty group → no read.
    """
    source_ids = sorted({str(row["source_id"]) for row in group})
    if not source_ids:
        return {}
    return {
        str(row["id"]): row["source_kind"]
        for row in query(TABLE_SOURCE, {"id": source_ids})
    }


def _project_contributors(
    group: list[dict[str, object]],
    source_kind_by_id: dict[str, object],
) -> list[dict[str, object]]:
    """Project + sort the group into the 9-key contributor rows for the builder.

    Drops a row whose ``source_id`` is absent from ``source_kind_by_id`` (the
    INNER-JOIN drop). Sort: canonical-first (``canonical_external_session_id IS
    NULL``) then ``source_kind`` ASC — byte-identical to the pre-migration
    ``ORDER BY (canonical_external_session_id IS NOT NULL), source_kind ASC``.
    ``first_event_at`` / ``last_event_at`` are parsed back to NAIVE datetime via
    :func:`_parse_session_dt` (the type the raw ``_fetch_all`` returned;
    ``query_state`` serialized them to ISO strings) — both are ``not_null``
    session columns, so a parse failure is a fail-loud contract violation.
    """
    projected: list[dict[str, object]] = []
    for row in group:
        source_id = str(row["source_id"])
        if source_id not in source_kind_by_id:
            continue
        projected.append(
            {
                "session_id": str(row["id"]),
                "source_id": source_id,
                "source_kind": source_kind_by_id[source_id],
                "vendor": row["vendor"],
                "external_session_id": row["external_session_id"],
                "first_event_at": _parse_session_dt(row["first_event_at"]),
                "last_event_at": _parse_session_dt(row["last_event_at"]),
                "contributed_event_count": row["event_count"],
                "is_canonical": row.get("canonical_external_session_id") is None,
            }
        )
    projected.sort(
        key=lambda r: (not bool(r["is_canonical"]), str(r["source_kind"])),
    )
    return projected


def select_quiescent_sessions(
    candidates: list[dict[str, object]],
    *,
    summarized_session_ids: set[str],
    source_kind_by_id: dict[str, object],
    trivial_sentinel: str,
    limit: int,
) -> list[dict[str, object]]:
    """Read-then-route shaping for ``list_quiescent_sessions`` (M6 auto-summarize).

    ``candidates`` are the canonical, live, past-cutoff (``last_event_at <=
    cutoff``) sessions the ``query_state`` read already narrowed. The original's
    ``NOT EXISTS __summary`` anti-join, its ``(summary_text IS NULL OR !=
    sentinel)`` disjunction, and its ``last_event_at`` ASC + LIMIT applied AFTER
    those filters are all shapes the flat grammar cannot express — so they happen
    here:

    * EXCLUDE a session with a live ``__summary`` row (``summarized_session_ids``
      — the real idempotency seam: "done" == embedded, NOT
      ``summary_text``-populated, per the 2026-06-01 Bug-1 ruling that un-broke
      custom_title-seeded claude_code sessions);
    * EXCLUDE a session whose ``summary_text`` is the trivial sentinel (the
      marked-and-skipped rows the cron must not re-pick);
    * EXCLUDE a session whose source row is absent — the original
      ``INNER JOIN __source`` drop; a present source contributes ``source_kind``
      (faithfully WITHOUT an ``is_deleted`` filter, matching the original join,
      so a soft-deleted source's sessions are still summarized);
    * EXCLUDE non-conversation source kinds (``agent_messaging``) — operator
      2026-06-30: summarize real user/assistant conversations, not the continuous
      stream of peer-coordination chatter that otherwise dominates newest-first;
    * sort ``last_event_at`` DESC (newest-quiescent-first → recent sessions
      become searchable within ~a day rather than waiting behind the full
      historical import backlog) and return at most ``limit`` rows.

    Each survivor is the SELECT-``*`` session row + the projected ``source_kind``
    (the D9 widening; ``summary_text`` is already a session column the caller's
    custom_title-seed branch reads). ``last_event_at`` is a NOT NULL timestamp
    parsed (not lexical-sorted) for the chronological order.
    """
    survivors: list[dict[str, object]] = []
    for row in candidates:
        if str(row["id"]) in summarized_session_ids:
            continue
        if row.get("summary_text") == trivial_sentinel:
            continue
        source_id = str(row["source_id"])
        if source_id not in source_kind_by_id:
            continue
        source_kind = source_kind_by_id[source_id]
        if source_kind in _NON_CONVERSATION_SOURCE_KINDS:
            continue  # agent_messaging = peer-coordination noise, not a conversation
        survivors.append({**row, "source_kind": source_kind})
    # Newest-quiescent-FIRST (``reverse=True``, changed 2026-06-30 from ASC):
    # recent sessions become summarized — and therefore searchable via
    # ``search_sessions`` — within ~a day, instead of waiting behind the full
    # historical import backlog (~15k rows / many days at the cron cadence).
    # The operator's goal is finding RECENT conversations; the historical
    # backlog still drains underneath as newer rows are exhausted.
    survivors.sort(
        key=lambda row: datetime.fromisoformat(str(row["last_event_at"])),
        reverse=True,
    )
    return survivors[:limit]


# Census fingerprint: a deterministic, order-independent 64-bit XOR fold over
# per-event ``(id, content_blob_id)`` identity tuples under two well-separated
# seeds. Replaces the retired Postgres ``bit_xor(hashtextextended(...))``
# (SQL-lockdown GAP-1; operator D1 ruling 2026-06-20 — Python-fold + re-baseline,
# NO aggregate primitive). ``hashtextextended`` is a PG-internal hash with no
# faithful Python reproduction; the census is regenerable (no cross-era
# comparison against a stored old-method value), so the fingerprint is recomputed
# in Python on both diff sides. ``blake2b`` — NOT the builtin ``hash()``, which
# is process-salted (``PYTHONHASHSEED``) and therefore NOT reproducible across
# runs, defeating the determinism the fingerprint's whole purpose depends on. The
# 8-byte digest matches the retired bigint fingerprint width.
_FINGERPRINT_DIGEST_BYTES = 8
_FINGERPRINT_PERSON_BYTES = 16  # == hashlib.blake2b.PERSON_SIZE


def _fingerprint_component(identity: str, seed: int) -> int:
    """Deterministic 64-bit hash of a row-identity string under ``seed``."""
    digest = hashlib.blake2b(
        identity.encode("utf-8"),
        digest_size=_FINGERPRINT_DIGEST_BYTES,
        person=seed.to_bytes(_FINGERPRINT_PERSON_BYTES, "little"),
    ).digest()
    return int.from_bytes(digest, "little")


@dataclass
class _SessionTally:
    """Per-source session counts — total + canonical/sibling split."""

    session_count: int = 0
    canonical_count: int = 0
    sibling_count: int = 0


@dataclass
class _BatchTally:
    """Per-source import-batch health — running-batch split + oldest start."""

    owned_running: int = 0
    unclaimed_route: int = 0
    min_started: datetime | None = None


def _as_naive_datetime(value: object) -> datetime | None:
    """A ``min_value`` timestamp scalar as a NAIVE datetime, or ``None``.

    The aggregate primitives return raw scalars with the same type-fidelity as a
    row cell (the F1 TZ seam): a ``TIMESTAMP`` column yields a naive datetime,
    while a JSON-serialized path yields a naive ISO string. Both normalize here,
    because ``_batch_age_seconds`` subtracts this from a naive-UTC ``now`` and a
    mixed-awareness subtraction raises.

    NOTE a small, deliberate widening: the row-walking code this replaced parsed
    ``started_at`` only when it was a ``str`` (``if isinstance(started, str)``),
    so a datetime cell would have been skipped and the source would have reported
    no oldest batch at all. Accepting both is intent-preserving — the intent is
    plainly "the oldest running batch's start" — and is disclosed rather than
    folded in silently.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is None else (
            value.astimezone(UTC).replace(tzinfo=None)
        )
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is None else parsed.astimezone(UTC).replace(tzinfo=None)
    return None


def _batch_age_seconds(batch: _BatchTally | None, now: datetime) -> int | None:
    """Whole-second age of the oldest running batch, or ``None`` when none run.

    Mirrors the retired ``extract(epoch FROM now() - min(started_at))::bigint``:
    ``now`` is the repository clock normalized to naive UTC by the caller (the
    F1 seam — matching the naive-UTC ``started_at`` parsed from its ISO string),
    and ``int(...)`` truncates toward zero exactly as ``::bigint`` does for the
    non-negative durations census produces.
    """
    if batch is None or batch.min_started is None:
        return None
    return int((now - batch.min_started).total_seconds())


class _CensusAggregator:
    """Pure per-source census fold — the D1 Python replacement for ``_CENSUS_SQL``.

    Holds the per-source session / tool_call / import-batch tallies (folded once
    from the bounded full reads) plus the order-independent event-row
    fingerprint, which is fed PAGE-BY-PAGE via :meth:`fold_event_page` so the
    ~1M-row ``__event`` scan never materializes the whole (inline-content-bearing)
    corpus on the shared state-service process — the memory-bounded keyset paging
    the SELECT-``*`` ``query_state`` path cannot express. The XOR fold is
    order-independent, so the keyset page order is irrelevant to the result.

    Faithful to the retired multi-CTE join semantics: events and tool_calls are
    attributed to their *session's* ``source_id`` and DROPPED when that session
    is not live (the old ``JOIN __session s ON … AND s.is_deleted = 0`` of the
    ``ev`` / ``tc`` CTEs); the output rows come ``FROM`` the live sources (the
    base of the old LEFT JOIN), so tallies under a missing/deleted source are
    silently absent; a source with no live events carries ``None`` fingerprints
    (the old LEFT JOIN NULL), distinct from a present-but-zero XOR.
    """

    def __init__(
        self,
        *,
        sources: list[dict[str, object]],
        sessions: Iterable[dict[str, object]],
        fingerprint_seeds: tuple[int, int],
    ) -> None:
        self._sources = sources
        self._seed_a, self._seed_b = fingerprint_seeds
        # Live-session id -> source_id. An event / tool_call whose session is
        # absent here (soft-deleted or never present) is dropped from the
        # per-source tally — the INNER-JOIN ``s.is_deleted = 0`` semantics.
        self._session_source: dict[str, str] = {}
        # ``sessions`` is consumed EXACTLY ONCE. It used to be walked twice — a
        # dict comprehension for ``_session_source``, then ``_tally_sessions`` —
        # which is harmless for a list and silently wrong for the paged
        # generators these are now fed (2026-08-16, lane-ak): the second pass
        # would see an exhausted iterator and every session tally would come back
        # empty, with no error. Merged into one loop so the parameter can honestly
        # be an ``Iterable``.
        #
        # ORDERING IS LOAD-BEARING: ``_tally_tool_calls`` and ``fold_event_page``
        # both look rows up in ``_session_source``, so the session stream must be
        # fully drained before either runs.
        self._sessions_by_source: dict[str, list[str]] = {}
        self._sessions = self._tally_sessions_and_index(sessions)
        # Filled by count_tool_calls_via / tally_batches_via, which CANNOT run
        # before the index above exists because they are methods on the object
        # that owns it. That is the point: the ordering used to be a comment.
        self._tool_calls: dict[str, int] = {}
        self._batches: dict[str, _BatchTally] = {}
        self._event_count: dict[str, int] = {}
        self._fingerprint_a: dict[str, int] = {}
        self._fingerprint_b: dict[str, int] = {}

    def _tally_sessions_and_index(
        self, sessions: Iterable[dict[str, object]],
    ) -> dict[str, _SessionTally]:
        """Per-source session tallies AND the id->source_id index, in one pass.

        One loop rather than two so ``sessions`` can be a paged generator; the
        arithmetic is identical to the two passes it replaces.
        """
        tally: dict[str, _SessionTally] = {}
        for row in sessions:
            source_id = str(row["source_id"])
            self._session_source[str(row["id"])] = source_id
            self._sessions_by_source.setdefault(source_id, []).append(str(row["id"]))
            entry = tally.setdefault(source_id, _SessionTally())
            entry.session_count += 1
            if row.get("canonical_external_session_id") is None:
                entry.canonical_count += 1
            else:
                entry.sibling_count += 1
        return tally

    def count_tool_calls_via(self, count: _Counter) -> None:
        """Per-source ``__tool_call`` counts, WITHOUT reading a tool_call row.

        Read-cap sweep, 2026-08-16 (lane-ak). This replaced a whole-table walk of
        ``__tool_call`` — **637,496 rows, 6,375 pages** — that existed only to
        increment a per-source counter. The rows were shipped across the process
        boundary and then discarded; the caller needed the EFFECT, not the rows.

        A tool_call carries ``session_id``, not ``source_id``, so the count is
        keyed on each source's own session ids. Chunked at the cap because a
        ``= ANY`` list is bounded by the caller's list, and one source holds
        3,552 sessions (measured 2026-08-16).

        **This is a method, not a free function taking the index, on purpose.**
        It cannot run before ``_session_source`` is built, because it belongs to
        the object that builds it. The ordering used to be a comment saying
        "drain sessions first" — and a comment is what someone reorders.
        """
        for source_id, session_ids in self._sessions_by_source.items():
            total = 0
            for start in range(0, len(session_ids), MAX_READ_ROWS):
                chunk = session_ids[start : start + MAX_READ_ROWS]
                total += count(
                    TABLE_TOOL_CALL, {"session_id": chunk, "is_deleted": 0},
                )
            self._tool_calls[source_id] = total

    def tally_batches_via(self, count: _Counter, min_value: _MinValue) -> None:
        """Per-source import-batch health, WITHOUT reading an import_batch row.

        Replaced a whole-table walk of ``__import_batch`` — **284,759 rows, 2,848
        pages** — that produced two counters and one minimum. Each is a scalar
        aggregate the provider can compute directly, and each was verified
        against the live ledger before this was written:

            count(status=running, polling_lease_token IS NOT NULL)  -> owned_running
            count(status=running, polling_lease_token IS NULL)      -> unclaimed_route
            min(started_at) WHERE status=running                    -> min_started

        The equivalence was cross-checked against a completed census run's own
        output for the busiest source (94 owned-running, oldest batch 2026-07-09)
        rather than assumed from the filter grammar.

        ``__import_batch`` carries ``source_id`` directly, so unlike tool_calls
        this needs no session join and no chunking.
        """
        # Iterate the SOURCE rows, not the sessions index. The row-walking code
        # this replaced keyed batches off ``import_batch.source_id`` directly, so
        # a source with running batches but NO sessions still got a tally.
        # Deriving the source list from ``_sessions_by_source`` instead would
        # silently drop exactly those sources — caught by the smoke's src-B/src-C
        # cases, which is what they are for.
        for source_row in self._sources:
            source_id = str(source_row["id"])
            running: dict[str, object] = {
                "source_id": source_id,
                "status": ImportBatchStatus.RUNNING.value,
                "is_deleted": 0,
            }
            owned = count(
                TABLE_IMPORT_BATCH,
                {**running, "polling_lease_token": {"op": "is_not_null"}},
            )
            unclaimed = count(
                TABLE_IMPORT_BATCH,
                {**running, "polling_lease_token": {"op": "is_null"}},
            )
            if not owned and not unclaimed:
                continue
            oldest = min_value(TABLE_IMPORT_BATCH, "started_at", running)
            self._batches[source_id] = _BatchTally(
                owned_running=owned,
                unclaimed_route=unclaimed,
                min_started=_as_naive_datetime(oldest),
            )

    def fold_event_page(self, page: list[dict[str, object]]) -> None:
        """XOR-fold one page of live ``__event`` rows into the per-source fingerprint.

        Order-independent (the XOR), so the keyset page order does not matter.
        Events whose session is not live are dropped (the retired ``ev`` CTE's
        ``JOIN __session … AND s.is_deleted = 0``).
        """
        for row in page:
            source_id = self._session_source.get(str(row["session_id"]))
            if source_id is None:
                continue
            blob = row.get("content_blob_id")
            identity = f"{row['id']}\x1f{blob if blob is not None else ''}"
            self._event_count[source_id] = self._event_count.get(source_id, 0) + 1
            self._fingerprint_a[source_id] = self._fingerprint_a.get(
                source_id, 0,
            ) ^ _fingerprint_component(identity, self._seed_a)
            self._fingerprint_b[source_id] = self._fingerprint_b.get(
                source_id, 0,
            ) ^ _fingerprint_component(identity, self._seed_b)

    def result(self, *, now: datetime) -> list[dict[str, object]]:
        """Assemble per-source census rows, ordered ``(source_kind, source_id)``.

        Preserves the retired ``ORDER BY src.source_kind, src.id`` and the exact
        output keys the ``service.census`` consumer reads. ``fingerprint_a`` /
        ``fingerprint_b`` are ``None`` for a source with no live events (the old
        LEFT JOIN NULL); a present XOR value of ``0`` is a real fingerprint.
        """
        rows: list[dict[str, object]] = []
        for source in self._sources:
            source_id = str(source["id"])
            sessions = self._sessions.get(source_id, _SessionTally())
            batch = self._batches.get(source_id)
            rows.append(
                {
                    "source_id": source_id,
                    "source_kind": source.get("source_kind"),
                    "root_uri": source.get("root_uri"),
                    "session_count": sessions.session_count,
                    "canonical_count": sessions.canonical_count,
                    "sibling_count": sessions.sibling_count,
                    "event_count": self._event_count.get(source_id, 0),
                    "fingerprint_a": self._fingerprint_a.get(source_id),
                    "fingerprint_b": self._fingerprint_b.get(source_id),
                    "tool_call_count": self._tool_calls.get(source_id, 0),
                    "owned_running_batches": batch.owned_running if batch else 0,
                    "unclaimed_route_batches": (
                        batch.unclaimed_route if batch else 0
                    ),
                    "oldest_running_batch_age_seconds": _batch_age_seconds(
                        batch, now,
                    ),
                }
            )
        rows.sort(key=lambda row: (str(row["source_kind"]), str(row["source_id"])))
        return rows


class _StateReader(Protocol):
    """The mixin's bound ``_query`` read seam (``query_state``), as a callback."""

    def __call__(
        self, table: str, filters: dict[str, object],
    ) -> list[dict[str, object]]: ...


class _TableWalker(Protocol):
    """The repository's paged whole-table walk seam, as a callback contract.

    Added 2026-08-16 (lane-ak) so ``build_census`` can stream the three hot
    tables it previously read whole. Mirrors ``base.walk_table``'s signature; the
    binding lives in ``read.py`` where ``self._state`` is in scope.
    """

    def __call__(
        self,
        table: str,
        filters: dict[str, object],
        *,
        ceiling: int,
        reason: str,
    ) -> Iterator[dict[str, object]]: ...


class _Counter(Protocol):
    """Scalar ``COUNT(*)`` seam — the no-rows-shipped read.

    Added 2026-08-16 (lane-ak) so the census can COUNT the two tables it was only
    ever counting. ``count`` materializes nothing, so it is outside the read cap
    entirely and cannot be refused at any table size.
    """

    def __call__(self, table: str, filters: dict[str, object]) -> int: ...


class _MinValue(Protocol):
    """Scalar ``MIN(column)`` seam, for the oldest-running-batch timestamp."""

    def __call__(
        self, table: str, column: str, filters: dict[str, object],
    ) -> object: ...


class _MembershipReader(Protocol):
    """The repository's chunked ``col = ANY(values)`` seam, as a callback.

    Added 2026-08-16 (lane-ak, wave 2b). A membership read's result size is
    bounded by the CALLER's list length, not by anything the query states — so
    when that list comes from a whole-table scan it blows the row cap. Chunking
    makes every query bounded BY CONSTRUCTION. The binding lives in ``read.py``
    where ``base._query_membership_chunked`` is in scope.
    """

    def __call__(
        self,
        table: str,
        *,
        column: str,
        values: list[str],
        extra_filters: dict[str, object] | None = None,
    ) -> list[dict[str, object]]: ...


class _OrderedReader(Protocol):
    """The mixin's bound ``_query_ordered`` read seam, as a callback contract.

    ``after`` is the primitive's native row-value cursor — a tuple matching
    ``order_by`` in arity and direction. Declared here (2026-08-16, lane-ak)
    because :func:`walk_sessions_page` pages with it; the existing
    :func:`fold_census_events` caller does not pass it and is unaffected.
    """

    def __call__(
        self,
        table: str,
        *,
        filters: dict[str, object],
        order_by: list[list[str]],
        limit: int,
        after: tuple[object, ...] | None = None,
    ) -> list[dict[str, object]]: ...


def fold_census_events(
    aggregator: _CensusAggregator,
    *,
    query_ordered: _OrderedReader,
    table: str,
    page_size: int,
) -> None:
    """Page the live ``__event`` scan into ``aggregator`` via an ``id``-keyset cursor.

    Drives ``query_ordered`` with a Gap-A ``id > last_id`` lower bound over the
    unique PK, ordered ``(id, created_at)`` ASC — the ≥ 2-column composite
    ``query_ordered`` requires, with ``id`` already a total order so
    ``created_at`` is an inert tie-break (the ``get_session_timeline``
    filter-predicate paging precedent). ``is_deleted = 0`` is the primitive
    default. Each page is folded then DISCARDED, so peak memory is one page —
    never the ~1M-row corpus the SELECT-``*`` ``query_state`` path would
    materialize (operator D1 2026-06-20). The XOR fold is order-independent, so
    the ``id``-order scan is faithful. Pure orchestration: the DB read lives in
    the injected ``query_ordered`` (the mixin's bound ``_query_ordered``), the
    fold in the DB-free :class:`_CensusAggregator`. Stops on the first
    short / empty page.
    """
    last_id: str | None = None
    while True:
        filters: dict[str, object] = (
            {"id": {"op": "gt", "value": last_id}} if last_id is not None else {}
        )
        page = query_ordered(
            table,
            filters=filters,
            order_by=[["id", "asc"], ["created_at", "asc"]],
            limit=page_size,
        )
        if not page:
            return
        aggregator.fold_event_page(page)
        if len(page) < page_size:
            return
        last_id = str(page[-1]["id"])


#: Ceilings for the three census walks. NOT claims that these tables are small —
#: they are the largest in the ledger. They are claims about THIS call site: the
#: census is a diagnostic fold, and a fold that has consumed this many rows
#: without finishing is reporting on a ledger that has outgrown a whole-table
#: census entirely. Measured 2026-08-15 PDT / 2026-08-16 UTC: session 27,208,
#: tool_call 637,496, import_batch 284,787.
_CENSUS_SESSION_CEILING = 5_000_000
_CENSUS_TOOL_CALL_CEILING = 20_000_000
_CENSUS_IMPORT_BATCH_CEILING = 10_000_000


def build_census(
    *,
    query: _StateReader,
    query_ordered: _OrderedReader,
    walk: _TableWalker,
    count: _Counter,
    min_value: _MinValue,
    now: datetime,
    page_size: int,
    fingerprint_seeds: tuple[int, int],
) -> list[dict[str, object]]:
    """Compose the per-source ledger census — SQL-lockdown GAP-1 composition root.

    Reads ``__source`` whole via ``query`` (21 rows, measured), **streams**
    ``__session`` via ``walk``, **counts** ``__tool_call`` and ``__import_batch``
    with scalar aggregates instead of reading them at all, pages the ~2M-row
    ``__event`` scan via ``query_ordered`` (:func:`fold_census_events`), folds
    everything in the DB-free :class:`_CensusAggregator`, and returns the
    per-source rows. Operator
    D1 ruling (2026-06-20): Python-fold + re-baseline, NO aggregate primitive.
    The read seams are dependency-injected (no held service reference), so this
    is the full census composition the mixin delegates to in one thin method —
    keeping ``SessionLedgerReadMixin`` a focused query surface (the god-class
    budget). ``now`` is the repository clock pre-normalized to naive UTC by the
    caller (the F1 batch-age seam).

    Read-cap sweep, 2026-08-16 (lane-ak). ``session``, ``tool_call`` and
    ``import_batch`` were read WHOLE through ``query``. Measured on the serving
    release, ``census`` is **dead** — it refuses on ``session`` with
    ``cap_rows: 100`` before it ever reaches the larger two. The verb was ranked
    as a pagination job on the strength of its 637k-row ``tool_call`` read; it
    actually dies earlier, so it was unusable rather than merely at risk.

    **Why streaming and not ``list()``.** Materializing a walk would clear the
    cap and still hold the whole table on the shared process at once. The fold
    needs every row but never needs two at the same time — so the rows stream and
    only the per-source tallies are retained. That is the same shape
    :func:`fold_census_events` already uses for events, one function below.

    **2026-08-16, count-not-walk (lane-ak): CENSUS GOT 29% CHEAPER — IT IS NOT
    FIXED.** ``__tool_call`` (637,496 rows / 6,375 pages) and ``__import_batch``
    (284,759 / 2,848) were walked ONLY to be counted, so they became scalar
    ``count`` / ``min_value`` calls: ~333 aggregate calls replacing 9,223 paged
    round trips, and two more whole-table reads deleted on the needs-rows-or-
    effect question. Measured budget: **30,267 -> 21,377 round trips (-29%)**.

    **The floor is unchanged and is the real problem.** The ``__event`` fold
    alone is 20,771 pages — **97% of what remains** — so this stays a
    multi-minute call that parks the serial action queue while it runs. That
    head-of-line behaviour is a filed platform-level issue, not something this
    change addresses, and whether the per-source fingerprint must re-read ~2M
    events on every census is an Architect-parked design question. Do not report
    this verb as repaired.

    **Why ``walk`` and not a bespoke keyset.** Unlike ``list_sessions`` and the
    per-session event walk, this caller has NO required order — the tallies are
    counts and the fingerprint is an order-independent XOR — so
    ``bounded_read.iter_table_rows``' fixed ``(created_at, id)`` cursor is
    exactly right. This is the case where the shared helper IS the correct tool.
    """
    aggregator = _CensusAggregator(
        # __source stays a whole read: 21 rows, and it is iterated again in
        # result(). Streaming it would buy nothing and cost a second walk.
        sources=query(TABLE_SOURCE, {"is_deleted": 0}),
        sessions=walk(
            TABLE_SESSION, {},
            ceiling=_CENSUS_SESSION_CEILING,
            reason="one row per ingested session (measured 27,208 on 2026-08-16).",
        ),
        fingerprint_seeds=fingerprint_seeds,
    )
    # Both of these need the session index the constructor just built, and both
    # are methods on the object that owns it — so the ordering cannot be got
    # wrong by reordering these two lines.
    aggregator.count_tool_calls_via(count)
    aggregator.tally_batches_via(count, min_value)
    fold_census_events(
        aggregator,
        query_ordered=query_ordered,
        table=TABLE_EVENT,
        page_size=page_size,
    )
    return aggregator.result(now=now)


# ─── list_sessions read-then-route fold (SQL-lockdown junction migration) ─────
# The source_kind EXISTS-over-canonical-group subquery retires onto a junction
# read (canonical_ids) + an UNCAPPED query_state session read; the two two-sided
# event_at windows (last_event_at since/until + first_event_at first_since/
# first_until) + the configurable SessionsOrderBy sort + the limit are not
# expressible in the one-condition-per-column grammar, so they are applied here
# over the full candidate set (the #11 list_active_sessions / Slice-1 pattern).

# SessionsOrderBy → (column, reverse) for the Python sort. Mirrors the
# pre-migration raw-SQL ORDER BY fragment ("<col> DESC/ASC").
_SESSIONS_ORDER_KEY: dict[SessionsOrderBy, tuple[str, bool]] = {
    SessionsOrderBy.LAST_EVENT_AT_DESC: ("last_event_at", True),
    SessionsOrderBy.LAST_EVENT_AT_ASC: ("last_event_at", False),
    SessionsOrderBy.FIRST_EVENT_AT_DESC: ("first_event_at", True),
    SessionsOrderBy.FIRST_EVENT_AT_ASC: ("first_event_at", False),
}

# The pre-migration verb's exact SELECT projection (10 columns); the uncapped
# query_state read returns SELECT *, so the fold narrows to these.
_LIST_SESSIONS_PROJECTION: tuple[str, ...] = (
    "id", "source_id", "external_session_id", "vendor", "vendor_session_label",
    "project_path", "first_event_at", "last_event_at", "event_count",
    "canonical_external_session_id",
)


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """The two two-sided event_at windows, pre-normalized to naive UTC.

    All bounds are ``_naive_utc``-applied by the caller (F1 seam) so the compare
    is naive-vs-naive against the parsed ISO row values. ``None`` = unbounded.
    """

    since: datetime | None
    until: datetime | None
    first_event_since: datetime | None
    first_event_until: datetime | None


def _parse_session_dt(value: object) -> datetime:
    """Parse a session row's ISO ``*_event_at`` to a naive-UTC datetime.

    ``query_state`` serializes datetimes to naive ISO strings (the columns are
    ``timestamp without time zone``); an offline shim may hand back a datetime.
    Either way the result is naive UTC so it compares against the window bounds.
    """
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _session_in_window(row: dict[str, object], window: SessionWindow) -> bool:
    """True iff the row satisfies BOTH two-sided event_at windows."""
    last = _parse_session_dt(row["last_event_at"])
    if window.since is not None and last < window.since:
        return False
    if window.until is not None and last > window.until:
        return False
    first = _parse_session_dt(row["first_event_at"])
    if window.first_event_since is not None and first < window.first_event_since:
        return False
    return not (
        window.first_event_until is not None and first > window.first_event_until
    )


def _project_session_row(row: dict[str, object]) -> dict[str, object]:
    """Narrow a SELECT-* session row to the verb's 10-column envelope."""
    return {key: row.get(key) for key in _LIST_SESSIONS_PROJECTION}


def select_sessions_page(
    candidates: list[dict[str, object]],
    *,
    window: SessionWindow,
    order_by: SessionsOrderBy,
    limit: int,
) -> list[dict[str, object]]:
    """Apply the two windows + the SessionsOrderBy sort + the limit; project.

    ``candidates`` is the full uncapped ``query_state`` session read (the source
    plugins / equality filters already applied in-query). The two-sided windows
    can't ride the flat filter grammar, so they are applied here, then the
    configurable sort, then the limit (the #11/Slice-1 uncapped + Python
    pattern). The ``(sort-col, id)`` key is total-order DETERMINISTIC where the
    pre-migration ``ORDER BY <col>`` left ties arbitrary (so a tie at the limit
    boundary no longer returns a non-deterministic subset).
    """
    kept = [row for row in candidates if _session_in_window(row, window)]
    column, reverse = _SESSIONS_ORDER_KEY[order_by]
    kept.sort(
        key=lambda row: (_parse_session_dt(row[column]), str(row.get("id", ""))),
        reverse=reverse,
    )
    return [_project_session_row(row) for row in kept[:limit]]


#: Page size for the session walk — the ``query_ordered`` Gap-C ceiling.
_SESSION_PAGE_ROWS = 100

#: How many session rows :func:`walk_sessions_page` will examine before refusing.
#: NOT a claim that ``__session`` is small — it is 27,208 rows (measured
#: 2026-08-15 PDT / 2026-08-16 UTC). It is a claim about this call site: a page
#: read that has scanned half a million rows without filling a <=200-row page has
#: a predicate so unselective that walking further is the wrong answer, and the
#: caller should be narrowing its window instead.
_SESSION_WALK_CEILING = 500_000


def _pushable_window_filters(window: SessionWindow) -> dict[str, object]:
    """The half of the window the flat filter grammar CAN carry.

    The grammar is **one condition per column** (``ordered_query._filter_matches``:
    a column's value is one scalar, one list, or ONE ``{"op", "value"}`` spec). A
    two-sided window is two conditions on one column, so it cannot be expressed —
    which is exactly what ``list_sessions``' original docstring said, and it was
    right. Gap-A added half-open comparators, not two-sided ones.

    What IS expressible is ONE side per column, and pushing it down is always
    sound: it can only remove rows that :func:`_session_in_window` would reject
    anyway, and that function still re-checks both sides. The lower bound is
    preferred because both sort orders are recency-based, so ``>= since`` is the
    side that usually eliminates most of the table.

    This is a narrowing, not the predicate. The caller must still post-filter.
    """
    filters: dict[str, object] = {}
    if window.since is not None:
        filters["last_event_at"] = {"op": "gte", "value": window.since}
    elif window.until is not None:
        filters["last_event_at"] = {"op": "lte", "value": window.until}
    if window.first_event_since is not None:
        filters["first_event_at"] = {"op": "gte", "value": window.first_event_since}
    elif window.first_event_until is not None:
        filters["first_event_at"] = {"op": "lte", "value": window.first_event_until}
    return filters


def walk_sessions_page(
    query_ordered: _OrderedReader,
    *,
    filters: dict[str, object],
    window: SessionWindow,
    order_by: SessionsOrderBy,
    limit: int,
) -> list[dict[str, object]]:
    """One page of ``list_sessions``, without reading the table to build it.

    Read-cap sweep, 2026-08-15 PDT / 2026-08-16 UTC (lane-ak). This replaces an
    UNBOUNDED ``query_state`` that read **14,412 rows to return 50** and then
    applied the windows, the sort and the limit in Python. Measured live, that
    read is refused on the currently-serving release at the OLD 10,000-row cap —
    the public ``list_sessions`` verb has been dead, not merely at risk.

    **Why this is a walk and not simply a bounded read.** The obvious repair —
    push ``order_by`` and ``limit`` into ``query_ordered`` and keep the Python
    window — is WRONG, and wrong in a worse way than the bug it replaces. The
    provider would return the top ``limit`` rows in sort order, the Python window
    would then drop some of them, and the caller would receive fewer rows than
    qualify with no indication anything was dropped: a silent under-return.
    Post-filtering a truncated set is unfaithful, which is precisely what the
    original docstring warned about.

    So the walk pages *in the caller's sort order* and post-filters each page,
    accumulating until ``limit`` rows have SURVIVED the window or the source is
    exhausted. Every qualifying row is examined; none is truncated away before
    being filtered. Only one page is ever in memory.

    In practice it stops almost immediately: both sort orders are recency-based
    and the windows are recency windows, so the qualifying rows are at the head
    of the scan. The ceiling exists for the case where they are not.

    The cursor is ``(sort-column, id)`` — a bespoke keyset rather than
    ``bounded_read.iter_table_rows``, for the same reason as
    ``read.iter_events_by_sequence``: the helper's cursor is fixed at
    ``(created_at, id)`` and this caller chooses its order at runtime (four
    columns x two directions). The ``id`` tie-break makes the order total, so a
    tie at a page boundary cannot drop its remainder.

    Args:
        query_ordered: the repository's ``_query_ordered`` seam.
        filters: equality/``is_null`` predicates already pushed down by the
            caller. ``is_deleted`` must NOT be included — ``query_ordered``
            applies ``is_deleted = 0`` by default, and passing both is the way to
            get this wrong.
        window: the two two-sided windows, applied in Python (see above).
        order_by: the caller's sort choice.
        limit: how many surviving rows to return.

    Returns:
        Up to ``limit`` projected session rows, in ``order_by`` order.

    Raises:
        LedgerRepositoryError: the walk passed ``_SESSION_WALK_CEILING`` rows.
    """
    column, reverse = _SESSIONS_ORDER_KEY[order_by]
    direction = "desc" if reverse else "asc"
    composite = [[column, direction], ["id", direction]]
    scan_filters = {**filters, **_pushable_window_filters(window)}

    kept: list[dict[str, object]] = []
    after: tuple[object, ...] | None = None
    scanned = 0
    while len(kept) < limit:
        page = query_ordered(
            TABLE_SESSION,
            filters=scan_filters,
            order_by=composite,
            limit=_SESSION_PAGE_ROWS,
            after=after,
        )
        if not page:
            break
        scanned += len(page)
        if scanned > _SESSION_WALK_CEILING:
            raise LedgerRepositoryError(
                f"list_sessions walked more than {_SESSION_WALK_CEILING} "
                f"__session rows without filling a {limit}-row page. The scan was "
                f"refused rather than continued: at this point the window "
                f"predicate is matching almost nothing and the work belongs in a "
                f"narrower query, not a longer walk."
            )
        for row in page:
            if _session_in_window(row, window):
                kept.append(_project_session_row(row))
                if len(kept) == limit:
                    break
        if len(page) < _SESSION_PAGE_ROWS:
            break
        last = page[-1]
        after = (last[column], last["id"])
    return kept


#: How many junction rows one ``source_kind`` may have before the walk refuses.
#: NOT a claim the junction is small: measured 2026-08-16, ``claude_code_local``
#: alone has 4,005 rows and the table holds 8,055. It is a claim about this call
#: site — past this, the id list it produces is too large to be a useful filter
#: and the query wants narrowing, not a longer walk.
_JUNCTION_WALK_CEILING = 2_000_000

#: Rows one chunk of group-membership ids may yield before the walk refuses.
#: Unlike an id read this is NOT bounded by the chunk length — see
#: :func:`_walk_membership_chunked`.
_GROUP_MEMBER_WALK_CEILING = 2_000_000


def _junction_canonical_ids(
    walk: _TableWalker, source_kind: IngestSourceKind,
) -> list[str]:
    """Canonical session ids whose group has a contributor of ``source_kind``.

    Reads the ``session_source_kind`` junction. BOTH attach paths populate it — a
    new canonical writes ``(its id, its kind)`` and a demoted sibling writes
    ``(the canonical's id, the SIBLING's kind)`` (``ingest.py`` lines 333-368) — so
    a hit means the canonical's ``(vendor, external_session_id)`` group has ANY
    contributor of that kind, byte-equivalent to the pre-migration
    EXISTS-over-the-group.

    Read-cap sweep wave 2b, 2026-08-16 (lane-ak). This was an unbounded
    ``query_state``. **Measured: 4,005 rows for ``claude_code_local``** — and the
    live refusal on the serving release names THIS table, ``session_source_kind``,
    not ``session``, so it is the first thing ``list_sessions(source_kind=…)``
    hits. It pages now.

    The result is a set of ids with no meaningful order, so
    ``bounded_read.iter_table_rows``' fixed cursor is the right tool — the same
    reasoning that made it right for the census walks and wrong for the two
    order-carrying walks. ``is_deleted`` is absent from the filter because the
    paged walk applies it by default.
    """
    return [
        str(row["canonical_session_id"])
        for row in walk(
            TABLE_SESSION_SOURCE_KIND,
            {"source_kind": source_kind.value},
            ceiling=_JUNCTION_WALK_CEILING,
            reason=(
                "one junction row per (canonical session, contributor kind) "
                "(measured 4,005 for claude_code_local, 8,055 total, 2026-08-16)."
            ),
        )
    ]


def _walk_membership_chunked(
    walk: _TableWalker,
    table: str,
    *,
    column: str,
    values: list[str],
    extra_filters: dict[str, object],
    ceiling: int,
    reason: str,
) -> list[dict[str, object]]:
    """A ``col = ANY(values)`` read that is bounded even when ``col`` is NOT unique.

    ``base._query_membership_chunked`` chunks the VALUES list at the cap and says
    a chunk "matches at most its own length". **That is true only for a UNIQUE
    column.** For ``__session.id`` it holds. For ``external_session_id`` it does
    not, and cannot: a canonical and its siblings deliberately SHARE that value,
    so one chunk of 100 ids matched 200 rows and the read was refused — found by
    this wave's smoke, whose fake enforces the cap the way the provider does.

    So this chunks the values (bounding the ``= ANY`` parameter list) **and pages
    each chunk** (bounding the rows). The row count is then independent of the
    column's cardinality, which is the property the caller actually needs.

    Chunk size is derived from the cap so the two cannot drift.
    """
    out: list[dict[str, object]] = []
    for start in range(0, len(values), MAX_READ_ROWS):
        chunk = values[start : start + MAX_READ_ROWS]
        out.extend(
            walk(table, {**extra_filters, column: chunk}, ceiling=ceiling, reason=reason)
        )
    return out


def _read_full_group_membership(
    membership: _MembershipReader,
    walk: _TableWalker,
    *,
    canonical_ids: list[str],
    vendor: SourceVendor | None,
    project_path: str | None,
) -> list[dict[str, object]]:
    """Expand canonical ids to their groups' FULL membership (canonical + siblings).

    The ``include_siblings=True`` + ``source_kind`` path. The junction yields
    canonical SESSION ids, but a sibling links to its canonical via the SHARED
    ``external_session_id`` — NOT the session id (its
    ``canonical_external_session_id`` column holds the canonical's EXTERNAL id,
    ``ingest.py`` lines 357-359), so a direct ``id = ANY(canonical_ids)`` read
    drops siblings. Faithful expansion (byte-equivalent to the pre-migration
    EXISTS-over-canonical-group with ``include_siblings``):

    1. Read the qualifying canonicals (``id = ANY(canonical_ids)``) → their
       ``external_session_id`` values + ``(vendor, external_session_id)`` group keys.
    2. Read every session sharing those ``external_session_id`` values
       (``external_session_id = ANY(ext_ids)``) — captures canonical AND siblings
       of each group, because both carry the same ``external_session_id`` BY
       CONSTRUCTION (the demote fires on ``ON CONFLICT (vendor,
       external_session_id)``, ``ingest.py`` lines 324-332, so the sibling was
       inserted with the canonical's value).
    3. Python-filter to ``(vendor, external_session_id) ∈`` the qualifying group
       keys — restores the EXISTS's ``s2.vendor = s.vendor AND s2.external_session_id
       = s.external_session_id``, dropping a cross-vendor same-external-id
       over-match (practically impossible — external ids are vendor-namespaced —
       but ``pairs`` closes it provably; we just got burned on an "unreachable"
       assumption, so belt-and-suspenders).

    Robust to junction staleness: a stale ``canonical_id`` that now points at a
    demoted (now-sibling) row still yields the correct group key, since that row
    shares its group's ``(vendor, external_session_id)``.

    The ``vendor`` / ``project_path`` equality filters apply to the MEMBER read
    (the outer row's own columns, as the pre-migration EXISTS outer WHERE did);
    ``pairs`` handles group qualification.
    """
    # Wave 2b (2026-08-16, lane-ak): BOTH reads below are ``col = ANY(values)``
    # whose result size is bounded by the CALLER's list, not by the query — and
    # ``canonical_ids`` comes from the junction walk, measured at 4,005 for
    # claude_code_local. Chunked at the cap, so each query is bounded BY
    # CONSTRUCTION and the bound holds however long the list grows.
    canonicals = membership(
        TABLE_SESSION, column="id", values=canonical_ids,
        extra_filters={"is_deleted": 0},
    )
    if not canonicals:
        return []
    ext_ids = sorted({str(row["external_session_id"]) for row in canonicals})
    pairs = {
        (str(row["vendor"]), str(row["external_session_id"]))
        for row in canonicals
    }
    member_filters: dict[str, object] = {"is_deleted": 0}
    if project_path is not None:
        member_filters["project_path"] = project_path
    if vendor is not None:
        member_filters["vendor"] = vendor.value
    # PAGED, not merely chunked: ``external_session_id`` is non-unique BY
    # DESIGN — the canonical and every sibling share it — so a chunk of N ids
    # matches well over N rows and input-chunking alone does not bound the read.
    return [
        row
        for row in _walk_membership_chunked(
            walk, TABLE_SESSION,
            column="external_session_id", values=ext_ids,
            extra_filters={k: v for k, v in member_filters.items() if k != "is_deleted"},
            ceiling=_GROUP_MEMBER_WALK_CEILING,
            reason=(
                "sessions sharing an external_session_id with a qualifying "
                "canonical — one canonical plus its siblings per group."
            ),
        )
        if (str(row["vendor"]), str(row["external_session_id"])) in pairs
    ]


def list_sessions_via_junction(
    query_ordered: _OrderedReader,
    walk: _TableWalker,
    membership: _MembershipReader,
    *,
    window: SessionWindow,
    project_path: str | None,
    vendor: SourceVendor | None,
    source_kind: IngestSourceKind | None,
    order_by: SessionsOrderBy,
    limit: int,
    include_siblings: bool,
) -> list[dict[str, object]]:
    """list_sessions junction read-then-route (Architect ruling 2026-06-22).

    * **source_kind via the ``session_source_kind`` junction.** Replaces the
      EXISTS-over-canonical-group subquery: ``query(junction, {source_kind: K})``
      → the canonical ids whose ``(vendor, external_session_id)`` group has a
      contributor of kind K → restrict the session read to that group; no
      junction match → ``[]``.
    * **Uncapped ``query_state`` + Python fold.** The two two-sided ``event_at``
      windows + the configurable ``SessionsOrderBy`` sort + ``limit`` are not
      expressible in the one-condition-per-column grammar, so the session read is
      UNCAPPED and :func:`select_sessions_page` applies the windows + sort + limit
      over the full candidate set (the #11 / Slice-1 pattern; query_ordered's cap
      would make the second window's post-filter unfaithful). Window bounds are
      ``_naive_utc``-normalized by the caller (F1 seam).
    * **Canonical-only by default** unless ``include_siblings=True``.

    ``include_siblings=True`` + ``source_kind`` returns each matching group's FULL
    membership (canonical + siblings), faithful to the pre-migration EXISTS. The
    junction yields canonical ids, but siblings link via the SHARED
    ``external_session_id`` (not the session id), so this combo expands via
    :func:`_read_full_group_membership` rather than the direct ``id = ANY`` read.
    ``include_siblings=False`` keeps the ``canonical_external_session_id IS NULL``
    filter — which also drops a stale junction id that now points at a demoted
    sibling (the blessed union-on-attach / recompute-on-repair staleness handling:
    the canonical-IS-NULL filter drops a non-canonical id).
    """
    if source_kind is not None and include_siblings:
        canonical_ids = _junction_canonical_ids(walk, source_kind)
        if not canonical_ids:
            return []
        return select_sessions_page(
            _read_full_group_membership(
                membership,
                walk,
                canonical_ids=canonical_ids,
                vendor=vendor,
                project_path=project_path,
            ),
            window=window,
            order_by=order_by,
            limit=limit,
        )
    # ``is_deleted: 0`` is ABSENT deliberately: the walk reads through
    # ``query_ordered``, which applies that predicate by default. The original
    # passed it explicitly because ``query_state`` applies none. Passing both is
    # the way to get this wrong; the two predicates are identical (each excludes
    # a NULL ``is_deleted``), so the swap is semantics-preserving.
    filters: dict[str, object] = {}
    if not include_siblings:
        filters["canonical_external_session_id"] = {"op": "is_null"}
    if project_path is not None:
        filters["project_path"] = project_path
    if vendor is not None:
        filters["vendor"] = vendor.value

    if source_kind is None:
        return walk_sessions_page(
            query_ordered,
            filters=filters,
            window=window,
            order_by=order_by,
            limit=limit,
        )

    # ── source_kind path — repaired in wave 2b (2026-08-16, lane-ak) ──
    #
    # This branch was over the cap TWICE, and the live refusal on the serving
    # release named the FIRST of the two, which is why the junction is repaired
    # first:
    #
    #   _junction_canonical_ids {source_kind=claude_code_local}  = 4,005 rows
    #   ...then `id = ANY(those 4,005)` on __session                (measured)
    #
    # The junction read pages (see :func:`_junction_canonical_ids`); the id read
    # below is chunked at the cap, so it is bounded BY CONSTRUCTION rather than
    # by a promise about how many ids the junction returns.
    #
    # The windows + sort + limit stay a Python fold here, unlike the default
    # path's :func:`walk_sessions_page`. That is deliberate: this branch's
    # candidate set is already restricted to the junction's ids, so it is bounded
    # by that list rather than by the table — and a keyset walk cannot page an
    # `= ANY` filter of arbitrary length anyway. The fold is over a set whose
    # size the caller's own filter chose.
    canonical_ids = _junction_canonical_ids(walk, source_kind)
    if not canonical_ids:
        return []
    return select_sessions_page(
        membership(
            TABLE_SESSION, column="id", values=canonical_ids,
            extra_filters={**filters, "is_deleted": 0},
        ),
        window=window,
        order_by=order_by,
        limit=limit,
    )


__all__ = [
    "RELOAD_SAFE",
    "SessionWindow",
    "_CensusAggregator",
    "_build_canonical_contributors_result",
    "_content_json_subtype",
    "_fingerprint_component",
    "_merge_active_leases",
    "_pick_canonical_session_id",
    "_select_latest_away_summary",
    "build_canonical_contributors_via_group",
    "build_census",
    "fold_census_events",
    "list_sessions_via_junction",
    "select_quiescent_sessions",
    "select_sessions_page",
    "walk_sessions_page",
]
