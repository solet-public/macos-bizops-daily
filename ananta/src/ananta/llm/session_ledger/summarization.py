"""M6 semantic-search policy layer (spec §17.6).

Two operations live here:

* :meth:`SummaryWriter.push_summary_chunk` — generates an embedding via
  :meth:`EmbeddingServiceInterface.generate_embeddings`, stores it via
  :meth:`VectorServiceInterface.store_vectors`, then inserts the
  ``session_ledger__summary`` row with the returned
  ``embedding_vector_id``.

* :meth:`SummaryWriter.search` — embeds the query, runs ANN search against
  the same vector namespace, and joins back to ``session_ledger__summary``
  + ``session_ledger__session``.

Full content reaches the LLM unconditionally — no content filtering at
ingest, no read-time scan. Future secret-identification is a
periodic-search problem per the 2026-06-14 eradication design memo at
``workbench/2026-06-14_secretgate_full_eradication_design.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ananta.llm.session_ledger.schema import SUMMARY_VECTOR_NAMESPACE

if TYPE_CHECKING:
    from ananta.interfaces.embedding_service_interface import EmbeddingServiceInterface
    from ananta.interfaces.vector_service_interface import VectorServiceInterface
    from ananta.llm.session_ledger.repository import SessionLedgerRepository

logger = logging.getLogger(__name__)

# Vector store namespace for summary chunks. Re-exports the canonical
# constant from :mod:`ananta.llm.session_ledger.schema` so the namespace
# string used at runtime cannot drift from the namespace declared in the
# SchemaDefinition that creates the backing pgvector table
# (``<schema>.session_ledger_summary__embeddings``). Drift here is what caused
# the 2026-05-31 ``relation does not exist`` symptom on every
# ``search_sessions`` call.
VECTOR_NAMESPACE = SUMMARY_VECTOR_NAMESPACE


class SummaryServicesUnavailableError(Exception):
    """Raised when M6 is invoked but embedding+vector bindings are missing.

    Cloud profiles can ship without these bindings; the service still loads
    so that M1-M5 surface remains available, but M6 fails closed.
    """


@dataclass(frozen=True, slots=True)
class SearchResultEnvelope:
    """One row of the ``search_sessions`` envelope (see :meth:`SummaryWriter.search`)."""

    session_id: str
    chunk_index: int
    summary_text: str | None
    score: float
    session: dict[str, Any] | None


class SummaryWriter:
    """All policy for M6 lives here; the repository just persists rows.

    Instances are created by :class:`SessionLedgerService` once per
    construction and reused across calls — they hold no per-call state.
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
    # Write — push_session_summary_chunk
    # ------------------------------------------------------------------

    def push_summary_chunk(
        self,
        *,
        session_id: str,
        chunk_index: int,
        summary_text: str,
        generated_by_client_id: str,
    ) -> dict[str, Any]:
        """Persist one summary chunk + its embedding."""
        self._require_services()
        embedding = self._generate_embedding(summary_text)
        vector_id = self._store_vector(
            embedding=embedding,
            session_id=session_id,
            chunk_index=chunk_index,
            generated_by_client_id=generated_by_client_id,
        )
        summary_id = self._repository.persist_summary(
            session_id=session_id,
            chunk_index=chunk_index,
            summary_text=summary_text,
            embedding_vector_id=vector_id,
            generated_by_client_id=generated_by_client_id,
            generated_at=datetime.now(UTC),
        )
        return {
            "summary_id": summary_id,
            "embedding_vector_id": vector_id,
            "chunk_index": chunk_index,
        }

    # ------------------------------------------------------------------
    # Read — search_sessions
    # ------------------------------------------------------------------

    def search(
        self,
        *,
        query: str,
        limit: int,
    ) -> list[SearchResultEnvelope]:
        """Top-k summary chunks joined to sessions."""
        self._require_services()
        if limit < 1:
            raise ValueError("search_sessions limit must be >= 1")
        query_vector = self._generate_embedding(query)
        ann_rows = self._vector_search(query_vector=query_vector, top_k=limit)
        score_by_external_id = self._build_score_map(ann_rows)
        if not score_by_external_id:
            return []
        summary_rows = self._repository.list_summaries_by_external_ids(
            list(score_by_external_id.keys()),
        )
        session_by_id = self._build_session_lookup(summary_rows)
        envelopes = [
            self._build_envelope(row, score_by_external_id, session_by_id)
            for row in summary_rows
        ]
        envelopes.sort(key=lambda e: e.score, reverse=True)
        return envelopes

    @staticmethod
    def _build_score_map(ann_rows: list[dict[str, Any]]) -> dict[str, float]:
        """Convert pgvector ANN rows into ``{external_id: similarity}``.

        pgvector returns cosine ``distance`` in [0, 2] (1 - cosine_similarity).
        Convert to similarity in [-1, 1] so the envelope's ``score`` matches
        the public-API contract ("ordered by similarity score descending";
        see ``interfaces/public.py`` search_sessions return_value_schema).
        Rows missing ``external_id`` or ``distance`` are silently skipped.
        """
        return {
            str(r["external_id"]): 1.0 - float(r["distance"])
            for r in ann_rows
            if r.get("external_id") is not None and r.get("distance") is not None
        }

    def _build_session_lookup(
        self, summary_rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Load the session rows referenced by ``summary_rows`` and key by id."""
        session_ids = {str(r["session_id"]) for r in summary_rows}
        session_rows = self._repository.list_sessions_by_ids(list(session_ids))
        return {str(r["id"]): r for r in session_rows}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_envelope(
        self,
        row: dict[str, Any],
        score_by_external_id: dict[str, float],
        session_by_id: dict[str, dict[str, Any]],
    ) -> SearchResultEnvelope:
        external_id = str(row.get("embedding_vector_id"))
        summary_text = str(row.get("summary_text", ""))
        return SearchResultEnvelope(
            session_id=str(row["session_id"]),
            chunk_index=int(row["chunk_index"]),
            summary_text=summary_text,
            score=score_by_external_id.get(external_id, 0.0),
            session=session_by_id.get(str(row["session_id"])),
        )

    def _require_services(self) -> None:
        if self._embedding_service is None or self._vector_service is None:
            raise SummaryServicesUnavailableError(
                "M6 requires both embedding_service and vector_service bindings; "
                "this profile has not bound at least one of them.",
            )

    def _generate_embedding(self, text: str) -> list[float]:
        if self._embedding_service is None:  # pragma: no cover - guarded above
            raise SummaryServicesUnavailableError("embedding_service is None")
        result = self._embedding_service.generate_embeddings(
            inputs=[text], input_type="text",
        )
        if result.get("action_status") != "completed":
            raise RuntimeError(
                f"embedding_service.generate_embeddings failed: {result.get('error')!r}",
            )
        data = _as_dict(result.get("data"))
        inner = _as_dict(data.get("result"))
        embeddings = inner.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings:
            raise RuntimeError(
                "embedding_service returned no embeddings; refusing to persist a summary",
            )
        vector = embeddings[0]
        if not isinstance(vector, list) or not vector:
            raise RuntimeError("embedding_service returned a malformed first embedding")
        return [float(v) for v in vector]

    def _store_vector(
        self,
        *,
        embedding: list[float],
        session_id: str,
        chunk_index: int,
        generated_by_client_id: str,
    ) -> str:
        if self._vector_service is None:  # pragma: no cover - guarded above
            raise SummaryServicesUnavailableError("vector_service is None")
        external_id = f"{session_id}:{chunk_index}"
        record: dict[str, object] = {
            "external_id": external_id,
            "vector": embedding,
            "dimension": len(embedding),
            "metadata": {
                "session_id": session_id,
                "chunk_index": chunk_index,
                "generated_by_client_id": generated_by_client_id,
            },
        }
        result = self._vector_service.store_vectors(
            namespace=VECTOR_NAMESPACE, vectors=[record],
        )
        if result.get("action_status") != "completed":
            raise RuntimeError(
                f"vector_service.store_vectors failed: {result.get('error')!r}",
            )
        # Return the deterministic external_id. search_sessions's ANN path
        # returns rows keyed by ``external_id`` (the pgvector ``external_id``
        # column, not the pgvector-generated internal ``id``), and
        # ``list_summaries_by_external_ids`` looks up summary rows whose
        # ``embedding_vector_id`` matches that string. If we returned
        # ``inserted_ids[0]`` (pgvector's internal id), the summary row's
        # ``embedding_vector_id`` would never match the ANN's ``external_id``
        # and search_sessions would silently return [] for every query — even
        # one whose embedding sat directly under the summary's vector.
        # Kara-keen-keeper 2026-06-10 confirmed empirically.
        return external_id

    def _vector_search(
        self, *, query_vector: list[float], top_k: int,
    ) -> list[dict[str, Any]]:
        if self._vector_service is None:  # pragma: no cover - guarded above
            raise SummaryServicesUnavailableError("vector_service is None")
        result = self._vector_service.search_similar(
            namespace=VECTOR_NAMESPACE,
            query_vector=query_vector,
            top_k=top_k,
        )
        if result.get("action_status") != "completed":
            raise RuntimeError(
                f"vector_service.search_similar failed: {result.get('error')!r}",
            )
        # Real provider envelope: action_status / data: {result: {results,
        # count, namespaces_searched}}. Pre-2026-06-01 fix, the wrong-shape
        # read at data.get('results') returned None and search_sessions
        # ALWAYS returned [] in production — even though 41 codex embeddings
        # were sitting in the table. Smoke didn't catch it because its _ok
        # stub mirrored the same wrong shape.
        data = _as_dict(result.get("data"))
        inner = _as_dict(data.get("result"))
        rows = inner.get("results")
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]


def _as_dict(value: object) -> dict[str, Any]:
    """Coerce an unknown-shaped value into a dict (empty on miss)."""
    if isinstance(value, dict):
        return value
    return {}


__all__ = [
    "VECTOR_NAMESPACE",
    "SearchResultEnvelope",
    "SummaryServicesUnavailableError",
    "SummaryWriter",
]
