#!/usr/bin/env python3
"""LED-01 smoke for the event-content embedding producer + search.

Coverage:

* Scope filter (``is_embeddable_event``): USER/ASSISTANT MESSAGE rows with
  inline content are in; tool traffic, system events, tool-role messages,
  blank content, and the Codex ``reasoning`` subtype are out; the Codex
  ``worklog`` subtype stays IN (user-visible narration, not internal
  thinking). The reasoning exclusion is the red-first pin — neutralizing
  the subtype check in ``is_embeddable_event`` flips it.
* Chunking (``chunk_event_content``): whole-text fast path, window+overlap
  layout, exact-boundary behavior, full coverage on reassembly.
* ``embed_event``: one batched embedding call; store into
  ``EVENT_VECTOR_NAMESPACE`` with deterministic ``{event_id}:{chunk_index}``
  external_ids, chunk 0 first, per-chunk metadata; same-id delete BEFORE
  store (idempotent-retry pin); out-of-scope rows refused; per-event chunk
  bound reported as ``truncated`` (loud, not silent); embedding-count
  mismatch refuses the store.
* ``embed_missing_events``: newest-first paged walk with the chunk-0
  presence discriminator (``find_missing_external_ids``); already-embedded
  and python-filtered rows counted, not embedded; ``batch_limit`` stops the
  walk (``exhausted=False``); short page ends it (``exhausted=True``);
  ``(event_at, id)`` cursor advances page-over-page.
* ``drain_missing_events`` (Lane-1): ASCending forward walk in ARRIVAL order
  (``imported_at``, not vendor ``event_at``) from the durable KV cursor — embeds
  the missing backlog, skips already-embedded rows, and advances + persists the
  cursor (a single arrival timestamp) so a caught-up drain restarts inclusively
  at the boundary and embeds nothing (O(new) steady state). A LATE HISTORICAL
  IMPORT (old ``event_at``, new ``imported_at`` arriving after the cursor) is
  still caught — the regression pin for the arrival-vs-vendor-time keying; an
  ``event_at`` frontier would strand it forever. A page whose embed RAISES halts
  the drain WITHOUT advancing the cursor, so the failing page is retried next
  fire rather than stranded (the red-first pin: advancing the cursor before the
  page embed flips it). The repository candidate read orders oldest-first by
  ``imported_at`` under ``order_column="imported_at", ascending=True``; the
  drain cursor round-trips through the KV store as an arrival timestamp.
* ``search``: production-shape ANN rows (``distance``-keyed) → similarity
  scores (1 - distance) DESC; external_id parsed back to event + chunk;
  join-back drops unfetchable hits; empty ANN → [].
* Repository reads: candidate query passes the exact SQL-leg filter
  (MESSAGE + role ANY(user, assistant) + content_text IS NOT NULL), the
  ``[[event_at, desc], [id, desc]]`` composite order, the ≤100 clamp, and
  the ``after`` row-value cursor; ``list_events_by_ids`` short-circuits on
  empty input and passes the ``= ANY`` + ``is_deleted`` filter.
* Service verbs: ``search_event_content`` clamps limit and maps the public
  envelope (``vendor`` ← ``session_vendor``); ``embed_missing_event_content``
  clamps ``batch_limit``; both fail closed (RuntimeError) when the
  embedding/vector bindings are absent.
* ``coverage`` / ``event_embedding_coverage``: the drain-frontier read —
  ``caught_up`` compares the durable cursor to the newest in-scope arrival
  (red-first: hardcode caught_up=True and the cursor-behind pin flips),
  ``embedded_chunk_count`` reads the live vector count (0 on an empty namespace
  via the narrow ValueError catch), and the read stays available WITHOUT vector
  bindings (degrades to 0), unlike search which fails closed.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/event_embeddings_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import (  # noqa: E402
    StubBlobStorageService,
    StubStateService,
)
from ananta.core.domain.types import ActionResult  # noqa: E402
from ananta.llm.session_ledger.event_embeddings import (  # noqa: E402
    _RECONCILE_EVERY_FIRES,
    EVENT_CHUNK_MAX_CHARS,
    EVENT_CHUNK_OVERLAP_CHARS,
    EVENT_MAX_CHUNKS,
    EventEmbeddingWriter,
    chunk_event_content,
    is_embeddable_event,
)
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import (  # noqa: E402
    EVENT_VECTOR_NAMESPACE,
    TABLE_EVENT,
)
from ananta.services.session_ledger_service.service import (  # noqa: E402
    SessionLedgerService,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _ok(payload: dict[str, Any]) -> ActionResult:
    """Service-plugin envelope: real plugins wrap their data under data.result.

    Mirrors ``pgvector_service_plugin._create_success_result`` — same
    divergence-guard rationale as ``summary_search_smoke._ok`` (the
    2026-06-01 wrong-shape lesson).
    """
    return ActionResult(
        action_status="completed",
        data={"result": payload},
        actions=[],
        error=None,
        timestamp=datetime.now(UTC).isoformat(),
    )


class _StubEmbeddingService:
    """Deterministic per-input 3-d vectors; records every batch."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.mismatch_next = False
        # When set, any input containing this marker raises — the drain-halt pin
        # (a transient embedder failure on one page).
        self.raise_on_input: str | None = None

    def generate_embeddings(
        self,
        inputs: list[str],
        model: str | None = None,
        input_type: str = "text",
    ) -> ActionResult:
        del model, input_type
        if self.raise_on_input is not None and any(
            self.raise_on_input in s for s in inputs
        ):
            raise RuntimeError(f"stub embedding failure on {self.raise_on_input!r}")
        self.batches.append(list(inputs))
        vectors = [[float(len(s)), 1.0, 0.0] for s in inputs]
        if self.mismatch_next:
            vectors = vectors[:-1]  # one embedding short → writer must refuse
        return _ok({"embeddings": vectors, "dimension": 3, "model": "stub"})


class _StubVectorService:
    """Records stores/deletes; canned ANN + missing-set responses."""

    def __init__(self) -> None:
        self.ops: list[str] = []
        self.store_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.find_missing_calls: list[list[str]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.search_response: list[dict[str, Any]] = []
        # external_ids reported MISSING (i.e. not yet embedded). Defaults to
        # "everything is missing" so embed-path tests run unconditionally.
        self.missing_all = True
        self.present_external_ids: set[str] = set()

    def store_vectors(
        self, namespace: str, vectors: list[dict[str, object]]
    ) -> ActionResult:
        self.ops.append("store")
        self.store_calls.append({"namespace": namespace, "vectors": vectors})
        # Stored chunks become PRESENT — so a subsequent find_missing on a
        # re-read boundary row (the inclusive-cursor restart) correctly skips
        # it instead of re-embedding (drain steady-state fidelity).
        for record in vectors:
            external_id = record.get("external_id")
            if isinstance(external_id, str):
                self.present_external_ids.add(external_id)
        return _ok({"inserted_ids": [f"emb_{i}" for i in range(len(vectors))]})

    def search_similar(
        self,
        namespace: str,
        query_vector: list[float],
        top_k: int = 10,
        filters: dict[str, object] | None = None,
        distance_metric: object = None,
    ) -> ActionResult:
        del query_vector, filters, distance_metric
        self.search_calls.append({"namespace": namespace, "top_k": top_k})
        return _ok({"results": list(self.search_response[:top_k])})

    def delete_by_external_ids(
        self, namespace: str, external_ids: list[str]
    ) -> ActionResult:
        self.ops.append("delete")
        self.delete_calls.append(
            {"namespace": namespace, "external_ids": list(external_ids)}
        )
        return _ok({"deleted_count": 0})

    def find_missing_external_ids(
        self, namespace: str, candidate_external_ids: list[str]
    ) -> ActionResult:
        del namespace
        self.find_missing_calls.append(list(candidate_external_ids))
        missing = [
            external_id
            for external_id in candidate_external_ids
            if self.missing_all or external_id not in self.present_external_ids
        ]
        return _ok({"missing": missing})

    def get_namespace_stats(self, namespace: str) -> ActionResult:
        # Mirror the pgvector provider: an EMPTY namespace RAISES ValueError
        # ("nothing embedded yet"); a populated one returns vector_count over the
        # stored chunk external_ids.
        del namespace
        if not self.present_external_ids:
            raise ValueError("Namespace is empty: stub")
        return _ok(
            {
                "vector_count": len(self.present_external_ids),
                "dimensions": [3],
                "oldest_created": None,
                "newest_created": None,
            }
        )


def _event_row(
    *,
    row_id: str = "evt_1",
    session_id: str = "les_1",
    event_type: str = "MESSAGE",
    role: str | None = "assistant",
    content_text: str | None = "hello from the assistant",
    content_json: dict[str, Any] | None = None,
    event_at: str = "2026-07-06T01:00:00",
    imported_at: str | None = None,
    sequence: int = 1,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "session_id": session_id,
        "sequence": sequence,
        "event_type": event_type,
        "role": role,
        "content_text": content_text,
        "content_json": content_json,
        "event_at": event_at,
        # Arrival time defaults to event_at for the non-drain tests; the drain
        # tests set it explicitly (it is the Lane-1 cursor key).
        "imported_at": imported_at if imported_at is not None else event_at,
        "session_vendor": "codex",
        "source_kind": "codex_local",
    }


def _build_writer() -> tuple[
    EventEmbeddingWriter, StubStateService, _StubEmbeddingService, _StubVectorService
]:
    state = StubStateService()
    repo = SessionLedgerRepository(state_service=state)  # type: ignore[arg-type]
    embed = _StubEmbeddingService()
    vec = _StubVectorService()
    writer = EventEmbeddingWriter(
        repository=repo,
        embedding_service=embed,  # type: ignore[arg-type]
        vector_service=vec,  # type: ignore[arg-type]
    )
    return writer, state, embed, vec


# ─── Scope filter ─────────────────────────────────────────────────────────


def test_scope_filter() -> None:
    _check(
        is_embeddable_event(_event_row(role="user", content_text="a question")),
        "[1] USER MESSAGE with content is embeddable",
    )
    _check(
        is_embeddable_event(_event_row(role="assistant")),
        "[1] ASSISTANT MESSAGE with content is embeddable",
    )
    _check(
        not is_embeddable_event(
            _event_row(content_json={"subtype": "reasoning"}),
        ),
        "[1] Codex reasoning subtype is EXCLUDED (red-first pin: neutralize "
        "the subtype check in is_embeddable_event and this fails)",
    )
    _check(
        is_embeddable_event(_event_row(content_json={"subtype": "worklog"})),
        "[1] Codex worklog subtype stays IN (user-visible narration)",
    )
    _check(
        not is_embeddable_event(_event_row(event_type="TOOL_RESULT", role="tool")),
        "[1] TOOL_RESULT is excluded",
    )
    _check(
        not is_embeddable_event(_event_row(event_type="SYSTEM", role="system")),
        "[1] SYSTEM is excluded",
    )
    _check(
        not is_embeddable_event(_event_row(role="tool")),
        "[1] tool-role MESSAGE is excluded",
    )
    _check(
        not is_embeddable_event(_event_row(content_text=None)),
        "[1] NULL content_text (blob-offloaded) is excluded",
    )
    _check(
        not is_embeddable_event(_event_row(content_text="   \n ")),
        "[1] blank content_text is excluded",
    )


# ─── Chunking ─────────────────────────────────────────────────────────────


def test_chunking() -> None:
    short = "x" * EVENT_CHUNK_MAX_CHARS
    _check(
        chunk_event_content(short) == [short],
        "[2] text at exactly the window is ONE chunk",
    )
    text = "".join(chr(ord("a") + (i % 26)) for i in range(EVENT_CHUNK_MAX_CHARS + 1))
    chunks = chunk_event_content(text)
    _check(len(chunks) == 2, "[2] window+1 chars → two chunks")
    _check(
        chunks[0][-EVENT_CHUNK_OVERLAP_CHARS:] == chunks[1][:EVENT_CHUNK_OVERLAP_CHARS],
        "[2] consecutive chunks share the overlap",
    )
    step = EVENT_CHUNK_MAX_CHARS - EVENT_CHUNK_OVERLAP_CHARS
    reassembled = chunks[0] + "".join(c[EVENT_CHUNK_OVERLAP_CHARS:] for c in chunks[1:])
    _check(reassembled == text, "[2] chunks cover the full text (no gap, no loss)")
    long_text = "y" * (step * 5 + EVENT_CHUNK_OVERLAP_CHARS + 7)
    long_chunks = chunk_event_content(long_text)
    tail = long_chunks[-1]
    _check(
        all(len(c) == EVENT_CHUNK_MAX_CHARS for c in long_chunks[:-1])
        and 0 < len(tail) <= EVENT_CHUNK_MAX_CHARS
        and len(tail) > EVENT_CHUNK_OVERLAP_CHARS,
        "[2] interior chunks are full windows; the tail is never pure overlap",
    )


# ─── embed_event ──────────────────────────────────────────────────────────


def test_embed_event_single_chunk() -> None:
    writer, _, embed, vec = _build_writer()
    outcome = writer.embed_event(_event_row())
    _check(
        outcome
        == {
            "event_id": "evt_1",
            "chunks_stored": 1,
            "external_ids": ["evt_1:0"],
            "truncated": False,
        },
        "[3] single-chunk envelope: deterministic external_id evt_1:0",
    )
    _check(len(embed.batches) == 1, "[3] ONE batched generate_embeddings call")
    _check(
        vec.store_calls[0]["namespace"] == EVENT_VECTOR_NAMESPACE,
        f"[3] store namespace is {EVENT_VECTOR_NAMESPACE!r}",
    )
    record = vec.store_calls[0]["vectors"][0]
    _check(
        record["dimension"] == 3
        and record["metadata"]["event_id"] == "evt_1"
        and record["metadata"]["session_id"] == "les_1"
        and record["metadata"]["chunk_index"] == 0
        and record["metadata"]["event_at"] == "2026-07-06T01:00:00",
        "[3] vector record carries dimension + event/session/event_at/chunk metadata",
    )
    _check(
        vec.ops == ["delete", "store"]
        and vec.delete_calls[0]["external_ids"] == ["evt_1:0"],
        "[3] same-id delete precedes the store (idempotent-retry pin: drop "
        "the _delete_existing_vectors call and this fails)",
    )


def test_embed_event_multi_chunk_and_bounds() -> None:
    writer, _, embed, vec = _build_writer()
    step = EVENT_CHUNK_MAX_CHARS - EVENT_CHUNK_OVERLAP_CHARS
    # Chunk k covers [k*step, k*step + window); n chunks ⇔
    # (n-2)*step + window < len <= (n-1)*step + window. For n=3 pick
    # len just past one step + one full window.
    outcome = writer.embed_event(
        _event_row(row_id="evt_2", content_text="z" * (step + EVENT_CHUNK_MAX_CHARS + 128)),
    )
    _check(
        outcome["chunks_stored"] == 3
        and outcome["external_ids"] == ["evt_2:0", "evt_2:1", "evt_2:2"]
        and outcome["truncated"] is False,
        "[4] multi-chunk event stores sequential chunk external_ids",
    )
    stored = vec.store_calls[-1]["vectors"]
    _check(
        [r["external_id"] for r in stored] == ["evt_2:0", "evt_2:1", "evt_2:2"],
        "[4] chunk 0 is stored FIRST (presence-discriminator ordering)",
    )
    _check(
        len(embed.batches[-1]) == 3,
        "[4] all chunks embedded in one batched call",
    )
    giant = "w" * (step * (EVENT_MAX_CHUNKS + 3))
    outcome = writer.embed_event(_event_row(row_id="evt_3", content_text=giant))
    _check(
        outcome["chunks_stored"] == EVENT_MAX_CHUNKS and outcome["truncated"] is True,
        f"[4] chunk bound applies at {EVENT_MAX_CHUNKS} and is REPORTED "
        "(loud truncation, not silent)",
    )


def test_embed_event_refusals() -> None:
    writer, _, embed, _ = _build_writer()
    try:
        writer.embed_event(_event_row(content_json={"subtype": "reasoning"}))
        _check(False, "[5] out-of-scope row refused")
    except ValueError:
        _check(True, "[5] out-of-scope row refused (ValueError, nothing stored)")
    embed.mismatch_next = True
    try:
        writer.embed_event(_event_row(row_id="evt_4"))
        _check(False, "[5] embedding-count mismatch refused")
    except RuntimeError:
        _check(True, "[5] embedding-count mismatch refuses the store (RuntimeError)")


# ─── embed_missing_events ─────────────────────────────────────────────────


class _PagedCandidatesRepo(SessionLedgerRepository):
    """Real repository with ONLY the candidate read overridden to serve
    deterministic pages (signature-locked by the subclass relationship —
    the B3 ``_CapturingStore`` fake-fidelity discipline)."""

    __slots__ = ("pages", "after_cursors")

    def __init__(self, state_service: Any, pages: list[list[dict[str, object]]]) -> None:
        super().__init__(state_service)
        self.pages = pages
        self.after_cursors: list[tuple[object, object] | None] = []

    def list_event_embedding_candidates(
        self,
        *,
        limit: int,
        after: tuple[object, object] | None = None,
        order_column: str = "event_at",
        ascending: bool = False,
    ) -> list[dict[str, object]]:
        del limit, order_column, ascending
        self.after_cursors.append(after)
        page_index = len(self.after_cursors) - 1
        if page_index < len(self.pages):
            return list(self.pages[page_index])
        return []


def _paged_writer(
    pages: list[list[dict[str, object]]],
) -> tuple[EventEmbeddingWriter, _PagedCandidatesRepo, _StubVectorService]:
    state = StubStateService()
    repo = _PagedCandidatesRepo(state, pages)
    vec = _StubVectorService()
    writer = EventEmbeddingWriter(
        repository=repo,
        embedding_service=_StubEmbeddingService(),  # type: ignore[arg-type]
        vector_service=vec,  # type: ignore[arg-type]
    )
    return writer, repo, vec


def test_embed_missing_walk() -> None:
    # Page of 3: one already embedded, one reasoning-filtered, one missing.
    page = [
        _event_row(row_id="evt_a", event_at="2026-07-06T03:00:00"),
        _event_row(
            row_id="evt_b",
            event_at="2026-07-06T02:00:00",
            content_json={"subtype": "reasoning"},
        ),
        _event_row(row_id="evt_c", event_at="2026-07-06T01:00:00"),
    ]
    writer, repo, vec = _paged_writer([page])
    vec.missing_all = False
    vec.present_external_ids = {"evt_a:0"}
    outcome = writer.embed_missing_events(batch_limit=10, page_size=100)
    _check(
        outcome
        == {
            "candidates_scanned": 3,
            "events_embedded": 1,
            "chunks_stored": 1,
            "events_skipped_existing": 1,
            "events_skipped_filtered": 1,
            "events_truncated": 0,
            "exhausted": True,
            "batch_limit": 10,
        },
        "[6] walk tallies embedded/existing/filtered and exhausts on a short page",
    )
    _check(
        vec.find_missing_calls == [["evt_a:0", "evt_c:0"]],
        "[6] chunk-0 presence discriminator queried ONLY for python-passed rows",
    )
    _check(
        repo.after_cursors == [None],
        "[6] first page requested with no cursor",
    )


def test_embed_missing_batch_limit_and_cursor() -> None:
    page1 = [
        _event_row(row_id=f"evt_{i}", event_at=f"2026-07-06T0{9 - i}:00:00")
        for i in range(3)
    ]
    page2 = [_event_row(row_id="evt_9", event_at="2026-07-06T00:30:00")]
    writer, _, vec = _paged_writer([page1, page2])
    outcome = writer.embed_missing_events(batch_limit=2, page_size=3)
    _check(
        outcome["events_embedded"] == 2 and outcome["exhausted"] is False,
        "[7] batch_limit stops the walk mid-corpus (exhausted=False)",
    )
    _check(len(vec.store_calls) == 2, "[7] exactly batch_limit events stored")
    # Fresh writer/repo: the paged fake serves pages by call index, so a
    # fresh walk starts from page 1 again.
    writer2, repo2, _ = _paged_writer([page1, page2])
    outcome = writer2.embed_missing_events(batch_limit=10, page_size=3)
    _check(
        outcome["exhausted"] is True and outcome["events_embedded"] == 4,
        "[7] fresh walk drains both pages to exhaustion",
    )
    _check(
        repo2.after_cursors == [None, ("2026-07-06T07:00:00", "evt_2")],
        "[7] page-2 request carries page-1's (event_at, id) tail as the cursor",
    )


# ─── drain_missing_events (Lane-1 durable-cursor drain) ───────────────────


class _DrainRepo(SessionLedgerRepository):
    """Real repository with ONLY the candidate read overridden to serve a fixed
    corpus paged forward past the cursor, honoring ``order_column`` (sort +
    filter) so a regression to vendor ``event_at`` keying is observable —
    exercises the real drain loop AND the real KV-backed cursor persistence
    (via ``StubStateService``). Signature-locked by the subclass relationship
    (the B3 fake-fidelity discipline)."""

    __slots__ = ("corpus", "read_calls")

    def __init__(
        self, state_service: Any, corpus: list[dict[str, object]]
    ) -> None:
        super().__init__(state_service)
        self.corpus = list(corpus)
        self.read_calls: list[dict[str, Any]] = []

    def list_event_embedding_candidates(
        self,
        *,
        limit: int,
        after: tuple[object, object] | None = None,
        order_column: str = "event_at",
        ascending: bool = False,
    ) -> list[dict[str, object]]:
        self.read_calls.append(
            {"after": after, "order_column": order_column, "ascending": ascending}
        )
        keyed = sorted(
            self.corpus, key=lambda r: (str(r[order_column]), str(r["id"])),
        )
        if not ascending:
            keyed = list(reversed(keyed))
        if after is not None:
            key = (str(after[0]), str(after[1]))
            keyed = [
                r
                for r in keyed
                if ((str(r[order_column]), str(r["id"])) > key) == ascending
                and (str(r[order_column]), str(r["id"])) != key
            ]
        return [dict(r) for r in keyed[:limit]]


def test_drain_forward_cursor() -> None:
    rows = [
        _event_row(row_id="evt_old", imported_at="2026-07-06T01:00:00"),
        _event_row(row_id="evt_mid", imported_at="2026-07-06T02:00:00"),
        _event_row(row_id="evt_new", imported_at="2026-07-06T03:00:00"),
    ]
    state = StubStateService()
    repo = _DrainRepo(state, rows)
    vec = _StubVectorService()
    vec.missing_all = False
    vec.present_external_ids = {"evt_old:0"}  # oldest already embedded
    writer = EventEmbeddingWriter(
        repository=repo,
        embedding_service=_StubEmbeddingService(),  # type: ignore[arg-type]
        vector_service=vec,  # type: ignore[arg-type]
    )
    outcome = writer.drain_missing_events(page_size=2)
    _check(
        outcome["events_embedded"] == 2
        and outcome["events_skipped_existing"] == 1
        and outcome["halted_on_error"] is False,
        "[14] drain embeds the missing backlog and skips the already-embedded row",
    )
    _check(
        all(
            c["order_column"] == "imported_at" and c["ascending"]
            for c in repo.read_calls
        )
        and repo.read_calls[0]["after"] is None,
        "[14] drain walks ASCending by imported_at (arrival order) from the "
        "unset cursor",
    )
    _check(
        repo.get_event_embed_cursor() == "2026-07-06T03:00:00",
        "[14] durable cursor advances to (and persists) the newest ARRIVAL "
        "timestamp",
    )
    # Steady state: the second drain restarts INCLUSIVELY at the cursor, so it
    # re-reads only the boundary arrival and embeds nothing (find_missing skips
    # it) — O(new), not a full rescan.
    before = len(vec.store_calls)
    outcome2 = writer.drain_missing_events(page_size=2)
    _check(
        outcome2["events_embedded"] == 0
        and outcome2["events_skipped_existing"] == 1
        and len(vec.store_calls) == before,
        "[14] a caught-up drain re-reads only the boundary arrival and embeds "
        "nothing (inclusive-cursor steady state, not a full rescan)",
    )


def test_drain_catches_late_historical_import() -> None:
    # The Codex-blocker regression: a historical session imported AFTER a recent
    # arrival carries an OLD event_at but a NEW imported_at. An event_at frontier
    # would strand it forever; the arrival-order (imported_at) cursor catches it.
    rows = [
        _event_row(
            row_id="evt_recent",
            event_at="2026-07-06T05:00:00", imported_at="2026-07-06T05:00:01",
        ),
        _event_row(
            row_id="evt_historical",
            event_at="2024-01-01T00:00:00", imported_at="2026-07-06T05:00:02",
        ),
    ]
    state = StubStateService()
    repo = _DrainRepo(state, rows)
    vec = _StubVectorService()
    vec.missing_all = False
    vec.present_external_ids = {"evt_recent:0"}  # recent one already embedded
    writer = EventEmbeddingWriter(
        repository=repo,
        embedding_service=_StubEmbeddingService(),  # type: ignore[arg-type]
        vector_service=vec,  # type: ignore[arg-type]
    )
    # A prior drain already caught up to the recent arrival.
    repo.set_event_embed_cursor("2026-07-06T05:00:01")
    writer.drain_missing_events(page_size=10)
    embedded = {
        str(record["external_id"])
        for call in vec.store_calls
        for record in call["vectors"]
    }
    _check(
        "evt_historical:0" in embedded,
        "[17] a late historical import (OLD event_at 2024, NEW imported_at after "
        "the cursor) IS embedded — the drain keys on ARRIVAL time, not vendor "
        "event_at (red-first: point drain order_column at 'event_at' and the "
        "2024 event_at sorts below the cursor → skipped forever)",
    )
    _check(
        repo.get_event_embed_cursor() == "2026-07-06T05:00:02",
        "[17] cursor advances to the historical row's ARRIVAL timestamp",
    )


def test_drain_halts_without_advancing_past_a_failed_page() -> None:
    rows = [
        _event_row(
            row_id="evt_ok", imported_at="2026-07-06T01:00:00",
            content_text="fine content",
        ),
        _event_row(
            row_id="evt_bad", imported_at="2026-07-06T02:00:00",
            content_text="POISON content",
        ),
    ]
    state = StubStateService()
    repo = _DrainRepo(state, rows)
    embed = _StubEmbeddingService()
    embed.raise_on_input = "POISON"
    vec = _StubVectorService()  # missing_all=True → both need embedding
    writer = EventEmbeddingWriter(
        repository=repo,
        embedding_service=embed,  # type: ignore[arg-type]
        vector_service=vec,  # type: ignore[arg-type]
    )
    outcome = writer.drain_missing_events(page_size=1)
    _check(
        outcome["halted_on_error"] is True and outcome["events_embedded"] == 1,
        "[15] a page embed failure halts the drain after the last good page",
    )
    _check(
        repo.get_event_embed_cursor() == "2026-07-06T01:00:00",
        "[15] cursor advances only past SUCCESSFUL pages (the arrival time of "
        "evt_ok) — the failing page is NOT skipped (red-first: move "
        "set_event_embed_cursor BEFORE the page embed and this fails — the "
        "poison page's cursor would persist and strand evt_bad in a gap)",
    )


def _set_drain_counter(state: StubStateService, value: int) -> None:
    state.key_values[("session_ledger", "event_embed_drain_fires", "GLOBAL")] = str(
        value
    )


def test_drain_periodic_reconcile_catches_below_cursor_straggler() -> None:
    # Codex Lane-1 blocker #2: imported_at is pre-assigned before the autocommit
    # insert, so a row can commit into visibility BELOW the advanced cursor. The
    # incremental pass never re-reads it; the periodic reconciliation full-sweep
    # (every _RECONCILE_EVERY_FIRES-th fire) does.
    rows = [
        _event_row(row_id="evt_seen", imported_at="2026-07-06T05:00:00"),
        _event_row(row_id="evt_straggler", imported_at="2026-07-06T04:00:00"),
    ]
    state = StubStateService()
    repo = _DrainRepo(state, rows)
    vec = _StubVectorService()
    vec.missing_all = False
    vec.present_external_ids = {"evt_seen:0"}  # the seen row already embedded
    writer = EventEmbeddingWriter(
        repository=repo,
        embedding_service=_StubEmbeddingService(),  # type: ignore[arg-type]
        vector_service=vec,  # type: ignore[arg-type]
    )
    repo.set_event_embed_cursor("2026-07-06T05:00:00")  # advanced past evt_seen

    # (a) An INCREMENTAL fire (counter NOT at the reconcile boundary) never
    # re-reads the below-cursor straggler.
    _set_drain_counter(state, 1)  # next bump → 2, not a reconcile fire
    out_inc = writer.drain_missing_events(page_size=10)
    embedded_inc = {
        str(record["external_id"])
        for call in vec.store_calls
        for record in call["vectors"]
    }
    _check(
        out_inc["reconcile"] is False and "evt_straggler:0" not in embedded_inc,
        "[18] an INCREMENTAL fire never re-reads a straggler that committed "
        "below the cursor (the imported_at pre-assign visibility race)",
    )

    # (b) A RECONCILIATION fire (counter hits the boundary) ignores the cursor,
    # re-checks the whole corpus, and embeds the straggler.
    _set_drain_counter(state, _RECONCILE_EVERY_FIRES - 1)  # next bump → boundary
    out_rec = writer.drain_missing_events(page_size=10)
    embedded_rec = {
        str(record["external_id"])
        for call in vec.store_calls
        for record in call["vectors"]
    }
    _check(
        out_rec["reconcile"] is True and "evt_straggler:0" in embedded_rec,
        "[18] a RECONCILIATION fire (every Nth) ignores the cursor, re-checks "
        "the whole corpus, and embeds the below-cursor straggler — Codex "
        "blocker #2 fix (red-first: drop the reconcile branch and it stays "
        "stranded because the cursor never re-reads below itself)",
    )


# ─── search ───────────────────────────────────────────────────────────────


def test_search_joins_and_ranks() -> None:
    writer, state, _, vec = _build_writer()
    state.add_query_response(
        TABLE_EVENT,
        [
            _event_row(row_id="evt_a", session_id="les_a", sequence=4),
            _event_row(row_id="evt_b", session_id="les_b", sequence=9),
        ],
    )
    # Production field names: external_id + cosine DISTANCE (not score).
    vec.search_response = [
        {"external_id": "evt_b:1", "distance": 0.12},  # similarity 0.88
        {"external_id": "evt_a:0", "distance": 0.31},  # similarity 0.69
        {"external_id": "evt_gone:0", "distance": 0.40},  # row not in ledger
        {"external_id": "malformed", "distance": 0.05},  # unparseable id
    ]
    hits = writer.search(query="what did we decide", limit=4)
    _check(
        [h["event_id"] for h in hits] == ["evt_b", "evt_a"],
        "[8] hits join back to __event rows and rank by similarity DESC "
        "(unjoinable + malformed ANN rows dropped)",
    )
    _check(
        abs(hits[0]["score"] - 0.88) < 1e-9 and hits[0]["chunk_index"] == 1,
        "[8] score is similarity (1 - distance) and chunk_index is parsed "
        "from the external_id",
    )
    _check(
        hits[0]["session_id"] == "les_b"
        and hits[0]["content_text"] == "hello from the assistant"
        and hits[0]["session_vendor"] == "codex"
        and hits[0]["source_kind"] == "codex_local",
        "[8] hit carries the event-window projection fields",
    )
    _check(
        vec.search_calls[-1]["namespace"] == EVENT_VECTOR_NAMESPACE
        and vec.search_calls[-1]["top_k"] == 4,
        "[8] ANN runs in the event namespace with top_k=limit",
    )


def test_search_empty() -> None:
    writer, _, _, vec = _build_writer()
    vec.search_response = []
    _check(
        writer.search(query="anything", limit=5) == [],
        "[9] empty ANN result → []",
    )


# ─── repository reads ─────────────────────────────────────────────────────


def test_repository_candidate_read() -> None:
    state = StubStateService()
    repo = SessionLedgerRepository(state_service=state)  # type: ignore[arg-type]
    repo.list_event_embedding_candidates(
        limit=500, after=("2026-07-06T01:00:00", "evt_5"),
    )
    call = state.query_ordered_calls[-1]
    _check(
        call.table == TABLE_EVENT
        and call.filters
        == {
            "event_type": "MESSAGE",
            "role": ["user", "assistant"],
            "content_text": {"op": "is_not_null"},
        },
        "[10] candidate read passes the exact SQL-leg scope filter",
    )
    _check(
        call.order_by == [["event_at", "desc"], ["id", "desc"]]
        and call.limit == 100
        and call.after == ["2026-07-06T01:00:00", "evt_5"],
        "[10] newest-first composite order, ≤100 clamp, row-value cursor "
        "passed through",
    )
    repo.list_event_embedding_candidates(
        limit=50, after=None, order_column="imported_at", ascending=True,
    )
    asc_call = state.query_ordered_calls[-1]
    _check(
        asc_call.order_by == [["imported_at", "asc"], ["id", "asc"]],
        "[10] the Lane-1 drain read orders oldest-first by imported_at (arrival "
        "time), NOT vendor event_at",
    )


def test_repository_cursor_kv() -> None:
    state = StubStateService()
    repo = SessionLedgerRepository(state_service=state)  # type: ignore[arg-type]
    _check(
        repo.get_event_embed_cursor() is None,
        "[16] an unset drain cursor reads None (walk starts from the oldest arrival)",
    )
    repo.set_event_embed_cursor("2026-07-06T05:00:00")
    _check(
        repo.get_event_embed_cursor() == "2026-07-06T05:00:00",
        "[16] the drain cursor round-trips through the KV store as an arrival "
        "(imported_at) timestamp",
    )
    _check(
        repo.bump_event_embed_drain_counter() == 1
        and repo.bump_event_embed_drain_counter() == 2,
        "[16] the drain-fire counter increments durably (drives the periodic "
        "reconciliation sweep)",
    )


def test_repository_events_by_ids() -> None:
    state = StubStateService()
    repo = SessionLedgerRepository(state_service=state)  # type: ignore[arg-type]
    _check(
        repo.list_events_by_ids([]) == [] and not state.query_state_calls,
        "[11] empty id list short-circuits (no query)",
    )
    repo.list_events_by_ids(["evt_1", "evt_2"])
    call = state.query_state_calls[-1]
    _check(
        call.table == TABLE_EVENT
        and call.filters == {"id": ["evt_1", "evt_2"], "is_deleted": 0},
        "[11] join-back read uses = ANY(ids) + explicit is_deleted 0",
    )


# ─── service verbs ────────────────────────────────────────────────────────


class _StubPluginManager:
    def __init__(self) -> None:
        self.plugins: dict[str, object] = {}


def _make_service(
    *, with_vector_bindings: bool = True,
) -> tuple[SessionLedgerService, _StubVectorService | None, StubStateService]:
    state = StubStateService()
    vec = _StubVectorService() if with_vector_bindings else None
    embed = _StubEmbeddingService() if with_vector_bindings else None
    service = SessionLedgerService(
        state_service=state,  # type: ignore[arg-type]
        blob_storage_service=StubBlobStorageService(),  # type: ignore[arg-type]
        plugin_manager=_StubPluginManager(),  # type: ignore[arg-type]
        embedding_service=embed,  # type: ignore[arg-type]
        vector_service=vec,  # type: ignore[arg-type]
    )
    return service, vec, state


def test_service_search_event_content() -> None:
    service, vec, _ = _make_service()
    assert vec is not None
    vec.search_response = [{"external_id": "evt_x:0", "distance": 0.2}]
    envelope = service.search_event_content(query="find me", limit=999)
    _check(
        vec.search_calls[-1]["top_k"] == 50,
        "[12] limit clamps to 50 before reaching the ANN",
    )
    _check(
        envelope == {"results": []},
        "[12] unjoinable hit yields an empty results envelope (not an error)",
    )
    service2, vec2, state2 = _make_service()
    assert vec2 is not None
    vec2.search_response = [{"external_id": "evt_y:2", "distance": 0.1}]
    state2.add_query_response(
        TABLE_EVENT,
        [_event_row(row_id="evt_y", session_id="les_y", sequence=7)],
    )
    envelope = service2.search_event_content(query="find me")
    result = envelope["results"][0]
    _check(
        result["vendor"] == "codex"
        and "session_vendor" not in result
        and result["event_id"] == "evt_y"
        and result["chunk_index"] == 2
        and abs(result["score"] - 0.9) < 1e-9,
        "[12] public envelope maps vendor ← session_vendor and carries "
        "chunk_index + score",
    )


def test_service_embed_missing_clamp_and_fail_closed() -> None:
    service, vec, _ = _make_service()
    assert vec is not None
    outcome = service.embed_missing_event_content(batch_limit=99999)
    _check(
        outcome["batch_limit"] == 200 and outcome["exhausted"] is True,
        "[13] batch_limit clamps to 200; empty corpus exhausts immediately",
    )
    bare_service, _, _ = _make_service(with_vector_bindings=False)
    try:
        bare_service.search_event_content(query="anything")
        _check(False, "[13] search fails closed without bindings")
    except RuntimeError:
        _check(True, "[13] search fails closed without bindings (RuntimeError)")
    try:
        bare_service.embed_missing_event_content()
        _check(False, "[13] producer fails closed without bindings")
    except RuntimeError:
        _check(True, "[13] producer fails closed without bindings (RuntimeError)")


# ─── coverage (LED-01 backfill frontier) ──────────────────────────────────


def test_coverage_frontier_and_count() -> None:
    rows = [
        _event_row(row_id="evt_old", imported_at="2026-07-06T01:00:00"),
        _event_row(row_id="evt_new", imported_at="2026-07-06T03:00:00"),
    ]
    state = StubStateService()
    repo = _DrainRepo(state, rows)
    vec = _StubVectorService()
    vec.present_external_ids = {"evt_old:0", "evt_new:0", "evt_new:1"}  # 3 chunks
    writer = EventEmbeddingWriter(
        repository=repo,
        embedding_service=_StubEmbeddingService(),  # type: ignore[arg-type]
        vector_service=vec,  # type: ignore[arg-type]
    )
    repo.set_event_embed_cursor("2026-07-06T02:00:00")  # BEHIND the newest arrival
    cov = writer.coverage()
    _check(
        cov["caught_up"] is False
        and cov["cursor_imported_at"] == "2026-07-06T02:00:00"
        and cov["newest_in_scope_imported_at"] == "2026-07-06T03:00:00",
        "[19] coverage: cursor BEHIND the newest in-scope arrival → caught_up=False "
        "(red-first: hardcode caught_up=True and this flips)",
    )
    _check(
        cov["embedded_chunk_count"] == 3,
        "[19] coverage: embedded_chunk_count is the live vector (CHUNK) count from "
        "get_namespace_stats (3 chunks across 2 events)",
    )
    repo.set_event_embed_cursor("2026-07-06T03:00:00")  # AT the newest arrival
    _check(
        writer.coverage()["caught_up"] is True,
        "[19] coverage: cursor AT/past the newest in-scope arrival → caught_up=True",
    )


def test_coverage_edges() -> None:
    # No in-scope candidates → caught_up True; empty namespace → 0 (narrow catch).
    writer = EventEmbeddingWriter(
        repository=_DrainRepo(StubStateService(), []),
        embedding_service=_StubEmbeddingService(),  # type: ignore[arg-type]
        vector_service=_StubVectorService(),  # type: ignore[arg-type]  empty namespace
    )
    cov = writer.coverage()
    _check(
        cov["caught_up"] is True
        and cov["newest_in_scope_imported_at"] is None
        and cov["embedded_chunk_count"] == 0,
        "[20] coverage: no in-scope events → caught_up=True + newest=None; an empty "
        "namespace → embedded_chunk_count=0 (get_namespace_stats ValueError caught, "
        "not a crash)",
    )
    # Candidates exist but the drain never ran (cursor None) → NOT caught up.
    vec2 = _StubVectorService()
    vec2.present_external_ids = {"evt_x:0"}
    writer2 = EventEmbeddingWriter(
        repository=_DrainRepo(
            StubStateService(),
            [_event_row(row_id="evt_1", imported_at="2026-07-06T01:00:00")],
        ),
        embedding_service=_StubEmbeddingService(),  # type: ignore[arg-type]
        vector_service=vec2,  # type: ignore[arg-type]
    )
    _check(
        writer2.coverage()["caught_up"] is False,
        "[20] coverage: candidates exist but the cursor never advanced (None) → "
        "caught_up=False (the drain has not started)",
    )


def test_service_event_embedding_coverage() -> None:
    service, vec, _ = _make_service()
    assert vec is not None
    vec.present_external_ids = {"evt_a:0", "evt_a:1"}  # 2 chunks
    # No cursor set + no planted TABLE_EVENT candidate → caught_up True, newest None.
    cov = service.event_embedding_coverage()
    _check(
        cov["caught_up"] is True
        and cov["cursor_imported_at"] is None
        and cov["newest_in_scope_imported_at"] is None
        and cov["embedded_chunk_count"] == 2,
        "[21] service verb event_embedding_coverage delegates the writer coverage "
        "envelope (caught_up/cursor/newest/embedded_chunk_count)",
    )
    # Coverage stays AVAILABLE without vector bindings (degrades to 0) — contrast
    # search_event_content, which fails closed.
    bare_service, _, _ = _make_service(with_vector_bindings=False)
    cov_bare = bare_service.event_embedding_coverage()
    _check(
        cov_bare["caught_up"] is True and cov_bare["embedded_chunk_count"] == 0,
        "[21] coverage stays available WITHOUT vector bindings (embedded=0), unlike "
        "search which fails closed",
    )


def main() -> int:
    print("=== event_embeddings_smoke ===")
    test_scope_filter()
    test_chunking()
    test_embed_event_single_chunk()
    test_embed_event_multi_chunk_and_bounds()
    test_embed_event_refusals()
    test_embed_missing_walk()
    test_embed_missing_batch_limit_and_cursor()
    test_drain_forward_cursor()
    test_drain_catches_late_historical_import()
    test_drain_halts_without_advancing_past_a_failed_page()
    test_drain_periodic_reconcile_catches_below_cursor_straggler()
    test_search_joins_and_ranks()
    test_search_empty()
    test_repository_candidate_read()
    test_repository_cursor_kv()
    test_repository_events_by_ids()
    test_service_search_event_content()
    test_service_embed_missing_clamp_and_fail_closed()
    test_coverage_frontier_and_count()
    test_coverage_edges()
    test_service_event_embedding_coverage()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
