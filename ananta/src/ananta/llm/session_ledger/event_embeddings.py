"""LED-01 event-content embedding policy layer.

Three operations live here, mirroring :mod:`summarization` (the M6 summary
policy layer) against the EVENT corpus instead of the summary corpus:

* :meth:`EventEmbeddingWriter.embed_event` — chunk one embeddable
  ``__event`` row's ``content_text``, generate embeddings via
  :meth:`EmbeddingServiceInterface.generate_embeddings`, and store them via
  :meth:`VectorServiceInterface.store_vectors` under
  ``EVENT_VECTOR_NAMESPACE`` with the deterministic
  ``external_id = f"{event_id}:{chunk_index}"``.
* :meth:`EventEmbeddingWriter.embed_missing_events` — the drain primitive:
  page not-yet-embedded candidates newest-first and embed up to a bounded
  batch. Serves the operator subset backfill NOW and the Lane-1 scheduled
  drain later (same primitive, cron-driven).
* :meth:`EventEmbeddingWriter.search` — embed the query, run ANN over the
  event namespace, and join back to ``session_ledger__event`` rows.

Scope filter (operator ruling 2026-07-06, Option B): embed ONLY content
intended for the user — ``event_type == MESSAGE`` AND ``role`` in
{user, assistant} AND NOT internal thinking. Claude ``thinking`` blocks are
already dropped at ingest (``vendor/claude_code.py`` ``_SKIP_BLOCK_KINDS``),
but Codex ``reasoning`` IS stored as an ASSISTANT MESSAGE with
``content_json={"subtype": "reasoning"}`` — the state-interface filter
grammar cannot reach a JSON subfield, so that exclusion happens here in
Python (:func:`is_embeddable_event`). Codex ``worklog`` messages share the
same storage shape but are user-visible activity narration, not internal
thinking, so they stay embeddable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ananta.llm.session_ledger.schema import EVENT_VECTOR_NAMESPACE
from ananta.llm.session_ledger.search import project_event_window_row
from ananta.llm.session_ledger.types import EventType, MessageRole

if TYPE_CHECKING:
    from ananta.interfaces.embedding_service_interface import EmbeddingServiceInterface
    from ananta.interfaces.vector_service_interface import VectorServiceInterface
    from ananta.llm.session_ledger.repository import SessionLedgerRepository

logger = logging.getLogger(__name__)

# Per-chunk character window. 99% of the live corpus fits in ONE window
# (p99 content_text length ≈ 7.3K chars, measured 2026-07-06); anything
# longer is chunked with overlap below. New ingest inlines only
# ≤ CONTENT_INLINE_TEXT_MAX_BYTES (4 KB) so multi-chunk events are almost
# exclusively legacy pre-blob-offload rows reached by the backfill.
EVENT_CHUNK_MAX_CHARS = 8192

# Overlap between consecutive chunks so a phrase straddling a window
# boundary still lands whole in at least one chunk.
EVENT_CHUNK_OVERLAP_CHARS = 512

# Upper bound on chunks per event (~123K chars of coverage). The corpus max
# is a 310K-char legacy blob; embedding its full tail is index pollution,
# not signal. Truncation is NEVER silent — ``embed_event`` logs it and
# reports it in its return envelope.
EVENT_MAX_CHUNKS = 16

# ``content_json.subtype`` values excluded from embedding: internal
# thinking that happens to share the ASSISTANT-MESSAGE storage shape.
# Codex ``worklog`` (same shape) is deliberately NOT here — it is
# user-visible activity narration.
EXCLUDED_MESSAGE_SUBTYPES = frozenset({"reasoning"})

# Every Nth drain fire is a RECONCILIATION full-sweep (cursor ignored, whole
# corpus re-checked) rather than an incremental forward pass. This is the
# guarantee that closes the imported_at visibility race: imported_at is
# assigned before the autocommit insert, so a row can commit into visibility
# BELOW the already-advanced fast cursor; a periodic full find-missing sweep
# re-reads it and embeds it. At the boot cadence (a drain every 10 min) 144
# fires ≈ one full reconciliation per day — cheap amortized (the sweep only
# EMBEDS the rare straggler; already-embedded rows are find_missing-skipped),
# and it bounds a race-skipped event's un-searchable window to ~a day.
_RECONCILE_EVERY_FIRES = 144

# The user-facing message roles (operator scope ruling 2026-07-06).
EMBEDDABLE_EVENT_ROLES = frozenset(
    {MessageRole.USER.value, MessageRole.ASSISTANT.value},
)


class EventEmbeddingServicesUnavailableError(Exception):
    """Raised when event embedding is invoked without embedding+vector bindings.

    Cloud profiles can ship without these bindings; the ledger service still
    loads so the M1-M5 surface remains available, but event-content
    embedding fails closed (same posture as M6's
    ``SummaryServicesUnavailableError``).
    """


def _content_json_subtype(content_json: object) -> str | None:
    """Extract ``content_json.subtype`` from a read-back ``__event`` row.

    The column is JSONB; psycopg returns it as a ``dict`` (or ``None``).
    Any other shape carries no subtype by definition.
    """
    if isinstance(content_json, dict):
        subtype = content_json.get("subtype")
        return subtype if isinstance(subtype, str) else None
    return None


def is_embeddable_event(row: dict[str, Any]) -> bool:
    """Apply the operator scope filter to one raw ``__event`` row.

    MESSAGE events from user/assistant with non-blank inline content, minus
    the internal-thinking subtypes (see module docstring). The event_type +
    role + content-presence legs are ALSO expressed in the candidate read's
    SQL filter; this predicate re-checks them so a caller holding an
    arbitrary row (e.g. ``embed_event`` invoked directly) gets the same
    scope, and adds the JSON-subfield leg SQL cannot express.
    """
    if row.get("event_type") != EventType.MESSAGE.value:
        return False
    if row.get("role") not in EMBEDDABLE_EVENT_ROLES:
        return False
    content = row.get("content_text")
    if not isinstance(content, str) or not content.strip():
        return False
    return _content_json_subtype(row.get("content_json")) not in EXCLUDED_MESSAGE_SUBTYPES


def chunk_event_content(text: str) -> list[str]:
    """Split ``content_text`` into embedding-sized chunks.

    Whole text when it fits one window; otherwise fixed windows advancing by
    ``window - overlap`` so consecutive chunks share
    ``EVENT_CHUNK_OVERLAP_CHARS``. The final window ends exactly at the text
    end (no pure-overlap tail chunk is emitted). The per-event chunk BOUND is
    applied by :meth:`EventEmbeddingWriter.embed_event`, which has the event
    context to log a truncation loudly; this function is pure.
    """
    if len(text) <= EVENT_CHUNK_MAX_CHARS:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + EVENT_CHUNK_MAX_CHARS, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - EVENT_CHUNK_OVERLAP_CHARS
    return chunks


def event_chunk_external_id(event_id: str, chunk_index: int) -> str:
    """The deterministic vector-store ``external_id`` for one event chunk.

    Chunk 0 doubles as the "event is embedded" presence discriminator used
    by :meth:`EventEmbeddingWriter.embed_missing_events` — every embedded
    event has a ``:0`` row, whatever its chunk count.
    """
    return f"{event_id}:{chunk_index}"


def build_event_embedding_coverage(
    *,
    repository: SessionLedgerRepository,
    vector_service: VectorServiceInterface | None,
) -> dict[str, Any]:
    """Read-only LED-01 embedding coverage: drain frontier + embedded-chunk count.

    Answers "is the event-content backfill caught up?" deterministically with NO
    O(N) corpus scan. ``caught_up`` compares the durable arrival-order drain
    cursor against the newest in-scope (SQL-leg) candidate's ``imported_at``,
    coerced through the SAME ``str(...)`` the drain applies when it persists the
    cursor, so the string frontier compares byte-for-byte. ``caught_up=True``
    means the arrival frontier has reached the newest in-scope event; a straggler
    that committed BELOW the advanced cursor (the ``imported_at`` pre-assign
    visibility race) is the periodic reconciliation sweep's job — that full
    ``find_missing`` sweep, not this read, is the completeness backstop.
    ``embedded_chunk_count`` is the live vector count in the event namespace
    (CHUNKS, not distinct events — an event spans 1..N chunks).
    """
    cursor = repository.get_event_embed_cursor()
    newest_rows = repository.list_event_embedding_candidates(
        limit=1, order_column="imported_at", ascending=False,
    )
    newest_imported_at = str(newest_rows[0]["imported_at"]) if newest_rows else None
    if newest_imported_at is None:
        caught_up = True
    elif cursor is None:
        caught_up = False
    else:
        caught_up = cursor >= newest_imported_at
    return {
        "caught_up": caught_up,
        "cursor_imported_at": cursor,
        "newest_in_scope_imported_at": newest_imported_at,
        "embedded_chunk_count": _embedded_chunk_count(vector_service),
    }


def _embedded_chunk_count(vector_service: VectorServiceInterface | None) -> int:
    """Live vector (chunk) count in the event namespace; 0 when empty/unbound.

    ``get_namespace_stats`` raises ``ValueError`` on an empty-or-absent namespace
    (nothing embedded yet); the honest coverage value there is 0, not an error. A
    non-completed envelope and an unbound ``vector_service`` are treated the same.
    """
    if vector_service is None:
        return 0
    try:
        result = vector_service.get_namespace_stats(namespace=EVENT_VECTOR_NAMESPACE)
    except ValueError:
        return 0
    if result.get("action_status") != "completed":
        return 0
    inner = _as_dict(_as_dict(result.get("data")).get("result"))
    raw = inner.get("vector_count")
    return int(raw) if isinstance(raw, int) else 0


class EventEmbeddingWriter:
    """All policy for event-content embeddings; the repository persists rows.

    Created by :class:`SessionLedgerService` once per construction and
    reused across calls — holds no per-call state (mirrors
    :class:`summarization.SummaryWriter`).
    """

    __slots__ = ("_repository", "_embedding_service", "_vector_service")

    def __init__(
        self,
        *,
        repository: SessionLedgerRepository,
        embedding_service: EmbeddingServiceInterface | None,
        vector_service: VectorServiceInterface | None,
    ) -> None:
        self._repository = repository
        self._embedding_service = embedding_service
        self._vector_service = vector_service

    # ------------------------------------------------------------------
    # Write — one event
    # ------------------------------------------------------------------

    def embed_event(self, event_row: dict[str, Any]) -> dict[str, Any]:
        """Chunk + embed + store one embeddable ``__event`` row.

        Idempotent under retry: events are append-only immutable, so the
        chunking is deterministic per event — the same-id delete before the
        store clears any partial residue from a previous failed attempt
        without needing to know its extent. Chunk 0 is stored FIRST so the
        presence discriminator only exists once at least one chunk landed.
        """
        self._require_services()
        event_id = str(event_row["id"])
        if not is_embeddable_event(event_row):
            raise ValueError(
                f"event {event_id} is outside the embeddable scope "
                "(MESSAGE + user/assistant + non-blank content, minus "
                f"excluded subtypes {sorted(EXCLUDED_MESSAGE_SUBTYPES)})",
            )
        content = str(event_row["content_text"])
        chunks = chunk_event_content(content)
        truncated = len(chunks) > EVENT_MAX_CHUNKS
        if truncated:
            logger.warning(
                "embed_event: event %s produced %d chunks; embedding first %d "
                "(~%d chars) and dropping the tail",
                event_id,
                len(chunks),
                EVENT_MAX_CHUNKS,
                EVENT_MAX_CHUNKS * (EVENT_CHUNK_MAX_CHARS - EVENT_CHUNK_OVERLAP_CHARS),
            )
            chunks = chunks[:EVENT_MAX_CHUNKS]
        vectors = self._generate_embeddings(chunks)
        external_ids = [
            event_chunk_external_id(event_id, index) for index in range(len(chunks))
        ]
        self._delete_existing_vectors(external_ids)
        records = self._build_vector_records(
            event_row=event_row,
            event_id=event_id,
            vectors=vectors,
            external_ids=external_ids,
        )
        self._store_vectors(records)
        return {
            "event_id": event_id,
            "chunks_stored": len(chunks),
            "external_ids": external_ids,
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # Write — drain the not-yet-embedded backlog (bounded batch)
    # ------------------------------------------------------------------

    def embed_missing_events(
        self, *, batch_limit: int, page_size: int = 100,
    ) -> dict[str, Any]:
        """Embed up to ``batch_limit`` not-yet-embedded events, newest first.

        Pages the candidate read (SQL legs of the scope filter), applies the
        Python leg (:func:`is_embeddable_event` — the reasoning-subtype
        exclusion), asks the vector store which chunk-0 external_ids are
        missing, and embeds those events. Newest-first so fresh conversation
        content becomes searchable before the historical backlog. Bounded
        per call — the caller (operator backfill now, the Lane-1 drain
        later) loops while ``exhausted`` is false.
        """
        self._require_services()
        if batch_limit < 1:
            raise ValueError("embed_missing_events batch_limit must be >= 1")
        tally = {
            "candidates_scanned": 0,
            "events_embedded": 0,
            "chunks_stored": 0,
            "events_skipped_existing": 0,
            "events_skipped_filtered": 0,
            "events_truncated": 0,
        }
        cursor: tuple[object, object] | None = None
        exhausted = False
        while tally["events_embedded"] < batch_limit:
            rows = self._repository.list_event_embedding_candidates(
                limit=page_size, after=cursor,
            )
            if not rows:
                exhausted = True
                break
            tally["candidates_scanned"] += len(rows)
            cursor = (rows[-1]["event_at"], rows[-1]["id"])
            self._embed_missing_in_page(rows, batch_limit=batch_limit, tally=tally)
            if len(rows) < page_size:
                exhausted = True
                break
        return {**tally, "exhausted": exhausted, "batch_limit": batch_limit}

    def _embed_missing_in_page(
        self,
        rows: list[dict[str, object]],
        *,
        batch_limit: int | None,
        tally: dict[str, int],
    ) -> None:
        """Embed the not-yet-embedded embeddable events of one candidate page.

        ``batch_limit=None`` embeds every missing event in the page with no cap
        — the drain path (:meth:`drain_missing_events`), which bounds work by
        the durable cursor, not a per-call count.
        """
        embeddable = [row for row in rows if is_embeddable_event(dict(row))]
        tally["events_skipped_filtered"] += len(rows) - len(embeddable)
        if not embeddable:
            return
        missing = self._find_missing_external_ids(
            [event_chunk_external_id(str(row["id"]), 0) for row in embeddable],
        )
        for row in embeddable:
            if batch_limit is not None and tally["events_embedded"] >= batch_limit:
                return
            if event_chunk_external_id(str(row["id"]), 0) not in missing:
                tally["events_skipped_existing"] += 1
                continue
            outcome = self.embed_event(dict(row))
            tally["events_embedded"] += 1
            tally["chunks_stored"] += int(outcome["chunks_stored"])
            tally["events_truncated"] += 1 if outcome["truncated"] else 0

    # ------------------------------------------------------------------
    # Write — Lane-1 heartbeat drain (durable cursor, drain-until-caught-up)
    # ------------------------------------------------------------------

    def drain_missing_events(self, *, page_size: int = 100) -> dict[str, Any]:
        """Embed every not-yet-embedded event in ARRIVAL order from the cursor.

        The Lane-1 steady-state drainer, distinct from
        :meth:`embed_missing_events` (the bounded, newest-first-by-vendor-time,
        restart-from-head operator/manual primitive). This walks the durable
        ``imported_at`` (arrival-time) frontier ASCending and persists it per
        page, giving five properties the manual primitive cannot:

        * **Reconciliation — hard completeness under concurrency.** Every
          ``_RECONCILE_EVERY_FIRES``-th fire (a durable KV counter) IGNORES the
          cursor and re-checks the whole corpus. This closes the visibility
          race: ``imported_at`` is assigned in Python before the autocommit
          insert, so it is not commit-visibility-monotonic — a row can become
          visible BELOW the already-advanced cursor and the incremental pass
          would never re-read it. The periodic full ``find_missing`` sweep
          re-reads it and embeds it, so completeness is a hard EVENTUAL
          guarantee (bound: one reconcile interval), not a probabilistic one.
        * **Arrival-order — correct under out-of-order imports.** Keying on
          ``imported_at`` (platform receive time, per-row ``now()`` at append),
          NOT vendor ``event_at``, means a historical session imported after the
          cursor advanced (a cloud/export/history backfill — the ledger's
          routine load) still sorts AFTER the cursor and is embedded. An
          ``event_at`` frontier would strand it in a permanent silent gap.
        * **O(N) backfill, O(new) steady state.** A caught-up fire reads only
          arrivals past the cursor, not a full-corpus rescan every fire.
        * **Restart-resumable + tie-safe.** The persisted cursor is a single
          arrival timestamp; the drain restarts INCLUSIVELY at it (the
          empty-string id sentinel sorts before every uuid, so ``> (cursor, "")``
          re-reads every row sharing the boundary ``imported_at`` and
          ``find_missing`` skips the embedded ones — closing the same-arrival
          uuid-tiebreak gap that a strict ``(imported_at, id)`` cursor leaves).
        * **Transient-safe / poison-loud.** A page whose embed raises does NOT
          advance the cursor — the fire halts and the next heartbeat retries the
          same page once the embedder recovers (idempotent skip-existing means
          no double work). A genuinely un-embeddable event therefore re-surfaces
          loudly every fire rather than being silently skipped into a gap.

        Runs on the single-slot drain thread (see
        ``SessionLedgerService.drain_event_embeddings``) so its synchronous
        embedder/vector calls cannot park the action queue. Returns the
        drain-wide tally plus ``halted_on_error`` and ``reconcile`` (True when
        this fire was a full-corpus reconciliation sweep).
        """
        self._require_services()
        if page_size < 1:
            raise ValueError("drain_missing_events page_size must be >= 1")
        # Every _RECONCILE_EVERY_FIRES-th fire is a RECONCILIATION full-sweep:
        # ignore the cursor and re-check the whole corpus so a row that
        # committed into visibility BELOW the advanced cursor (imported_at is
        # pre-assigned before the autocommit insert, so it is not
        # commit-visibility-monotonic) is still found and embedded. Incremental
        # fires stay O(new).
        reconcile = (
            self._repository.bump_event_embed_drain_counter()
            % _RECONCILE_EVERY_FIRES
            == 0
        )
        cursor_imported_at = (
            None if reconcile else self._repository.get_event_embed_cursor()
        )
        page_after: tuple[object, object] | None = (
            (cursor_imported_at, "") if cursor_imported_at is not None else None
        )
        tally: dict[str, int] = {
            "candidates_scanned": 0,
            "events_embedded": 0,
            "chunks_stored": 0,
            "events_skipped_existing": 0,
            "events_skipped_filtered": 0,
            "events_truncated": 0,
            "pages": 0,
        }
        halted_on_error = False
        while True:
            rows = self._repository.list_event_embedding_candidates(
                limit=page_size, after=page_after,
                order_column="imported_at", ascending=True,
            )
            if not rows:
                break
            tally["candidates_scanned"] += len(rows)
            try:
                self._embed_missing_in_page(rows, batch_limit=None, tally=tally)
            except Exception:
                logger.exception(
                    "drain_missing_events: page embed failed at imported_at>%s "
                    "(%d events embedded this drain); halting without advancing "
                    "the cursor — the next heartbeat resumes this page once the "
                    "embedder recovers",
                    cursor_imported_at, tally["events_embedded"],
                )
                halted_on_error = True
                break
            tally["pages"] += 1
            last_imported_at = str(rows[-1]["imported_at"])
            page_after = (last_imported_at, rows[-1]["id"])
            self._repository.set_event_embed_cursor(last_imported_at)
        return {**tally, "halted_on_error": halted_on_error, "reconcile": reconcile}

    # ------------------------------------------------------------------
    # Read — semantic search over event content
    # ------------------------------------------------------------------

    def search(self, *, query: str, limit: int) -> list[dict[str, Any]]:
        """Top-k event chunks joined back to their ``__event`` rows.

        Embeds the query, runs ANN over ``EVENT_VECTOR_NAMESPACE``, parses
        each hit's ``external_id`` back to ``(event_id, chunk_index)``, and
        joins to the event rows. Results carry the same event envelope as
        ``list_events_by_source_window`` plus ``chunk_index`` + ``score``
        (cosine similarity, descending).
        """
        self._require_services()
        if limit < 1:
            raise ValueError("search_event_content limit must be >= 1")
        query_vector = self._generate_embeddings([query])[0]
        ann_rows = self._vector_search(query_vector=query_vector, top_k=limit)
        hits = self._parse_ann_hits(ann_rows)
        if not hits:
            return []
        event_ids = list({event_id for event_id, _, _ in hits})
        rows_by_id = {
            str(row["id"]): row
            for row in self._repository.list_events_by_ids(event_ids)
        }
        envelopes = [
            {
                **project_event_window_row(rows_by_id[event_id]),
                "chunk_index": chunk_index,
                "score": score,
            }
            for event_id, chunk_index, score in hits
            if event_id in rows_by_id
        ]
        envelopes.sort(key=lambda envelope: float(envelope["score"]), reverse=True)
        return envelopes

    @staticmethod
    def _parse_ann_hits(
        ann_rows: list[dict[str, Any]],
    ) -> list[tuple[str, int, float]]:
        """Convert pgvector ANN rows into ``(event_id, chunk_index, similarity)``.

        pgvector returns cosine ``distance`` in [0, 2]; converted to
        similarity (``1 - distance``) to match the ledger search-score
        contract (same conversion as ``SummaryWriter._build_score_map``).
        Rows missing ``external_id``/``distance`` or with a malformed
        external_id are skipped — an ANN row that cannot be joined back
        carries no renderable result.
        """
        hits: list[tuple[str, int, float]] = []
        for row in ann_rows:
            external_id = row.get("external_id")
            distance = row.get("distance")
            if not isinstance(external_id, str) or distance is None:
                continue
            event_id, separator, chunk_part = external_id.rpartition(":")
            if not separator or not event_id or not chunk_part.isdigit():
                continue
            hits.append((event_id, int(chunk_part), 1.0 - float(distance)))
        return hits

    # ------------------------------------------------------------------
    # Internals — service-envelope seams
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Read — embedding coverage (LED-01 backfill frontier)
    # ------------------------------------------------------------------

    def coverage(self) -> dict[str, Any]:
        """Read-only LED-01 embedding coverage (frontier + chunk count).

        Thin delegator to the module-level fold
        :func:`build_event_embedding_coverage` (mirrors census's
        ``census_source_rows`` → ``build_census`` shape — the read logic lives
        outside the writer's write-focused surface).
        """
        return build_event_embedding_coverage(
            repository=self._repository, vector_service=self._vector_service,
        )

    def _require_services(self) -> None:
        if self._embedding_service is None or self._vector_service is None:
            raise EventEmbeddingServicesUnavailableError(
                "event-content embedding requires both embedding_service and "
                "vector_service bindings; this profile has not bound at "
                "least one of them.",
            )

    def _generate_embeddings(self, texts: list[str]) -> list[list[float]]:
        """One batched ``generate_embeddings`` call; one vector per input.

        Envelope reads follow the real provider shape
        ``data.result.embeddings`` — see the 2026-06-01 wrong-shape lesson
        pinned in ``summarization._generate_embedding``.
        """
        if self._embedding_service is None:  # pragma: no cover - guarded above
            raise EventEmbeddingServicesUnavailableError("embedding_service is None")
        result = self._embedding_service.generate_embeddings(
            inputs=texts, input_type="text",
        )
        if result.get("action_status") != "completed":
            raise RuntimeError(
                f"embedding_service.generate_embeddings failed: {result.get('error')!r}",
            )
        data = _as_dict(result.get("data"))
        inner = _as_dict(data.get("result"))
        embeddings = inner.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise RuntimeError(
                "embedding_service returned "
                f"{len(embeddings) if isinstance(embeddings, list) else 'no'} "
                f"embeddings for {len(texts)} inputs; refusing a partial store",
            )
        vectors: list[list[float]] = []
        for embedding in embeddings:
            if not isinstance(embedding, list) or not embedding:
                raise RuntimeError(
                    "embedding_service returned a malformed embedding entry",
                )
            vectors.append([float(value) for value in embedding])
        return vectors

    @staticmethod
    def _build_vector_records(
        *,
        event_row: dict[str, Any],
        event_id: str,
        vectors: list[list[float]],
        external_ids: list[str],
    ) -> list[dict[str, object]]:
        """Assemble the ``store_vectors`` records (chunk 0 first)."""
        event_at = event_row.get("event_at")
        return [
            {
                "external_id": external_ids[index],
                "vector": vector,
                "dimension": len(vector),
                "metadata": {
                    "event_id": event_id,
                    "session_id": str(event_row.get("session_id")),
                    "event_at": str(event_at) if event_at is not None else None,
                    "chunk_index": index,
                },
            }
            for index, vector in enumerate(vectors)
        ]

    def _store_vectors(self, records: list[dict[str, object]]) -> None:
        if self._vector_service is None:  # pragma: no cover - guarded above
            raise EventEmbeddingServicesUnavailableError("vector_service is None")
        result = self._vector_service.store_vectors(
            namespace=EVENT_VECTOR_NAMESPACE, vectors=records,
        )
        if result.get("action_status") != "completed":
            raise RuntimeError(
                f"vector_service.store_vectors failed: {result.get('error')!r}",
            )

    def _delete_existing_vectors(self, external_ids: list[str]) -> None:
        if self._vector_service is None:  # pragma: no cover - guarded above
            raise EventEmbeddingServicesUnavailableError("vector_service is None")
        result = self._vector_service.delete_by_external_ids(
            namespace=EVENT_VECTOR_NAMESPACE, external_ids=external_ids,
        )
        if result.get("action_status") != "completed":
            raise RuntimeError(
                f"vector_service.delete_by_external_ids failed: {result.get('error')!r}",
            )

    def _find_missing_external_ids(self, candidates: list[str]) -> set[str]:
        if self._vector_service is None:  # pragma: no cover - guarded above
            raise EventEmbeddingServicesUnavailableError("vector_service is None")
        result = self._vector_service.find_missing_external_ids(
            namespace=EVENT_VECTOR_NAMESPACE, candidate_external_ids=candidates,
        )
        if result.get("action_status") != "completed":
            raise RuntimeError(
                f"vector_service.find_missing_external_ids failed: "
                f"{result.get('error')!r}",
            )
        data = _as_dict(result.get("data"))
        inner = _as_dict(data.get("result"))
        missing = inner.get("missing")
        if not isinstance(missing, list):
            raise RuntimeError(
                "vector_service.find_missing_external_ids returned no "
                "'missing' list; refusing to guess the embedded set",
            )
        return {str(external_id) for external_id in missing}

    def _vector_search(
        self, *, query_vector: list[float], top_k: int,
    ) -> list[dict[str, Any]]:
        """ANN read with the real provider envelope shape (``data.result.results``)."""
        if self._vector_service is None:  # pragma: no cover - guarded above
            raise EventEmbeddingServicesUnavailableError("vector_service is None")
        result = self._vector_service.search_similar(
            namespace=EVENT_VECTOR_NAMESPACE,
            query_vector=query_vector,
            top_k=top_k,
        )
        if result.get("action_status") != "completed":
            raise RuntimeError(
                f"vector_service.search_similar failed: {result.get('error')!r}",
            )
        data = _as_dict(result.get("data"))
        inner = _as_dict(data.get("result"))
        rows = inner.get("results")
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]


def _as_dict(value: object) -> dict[str, Any]:
    """Coerce an unknown-shaped envelope layer into a dict (empty on miss)."""
    if isinstance(value, dict):
        return value
    return {}


__all__ = [
    "EMBEDDABLE_EVENT_ROLES",
    "EVENT_CHUNK_MAX_CHARS",
    "EVENT_CHUNK_OVERLAP_CHARS",
    "EVENT_MAX_CHUNKS",
    "EXCLUDED_MESSAGE_SUBTYPES",
    "EventEmbeddingServicesUnavailableError",
    "EventEmbeddingWriter",
    "chunk_event_content",
    "event_chunk_external_id",
    "is_embeddable_event",
]
