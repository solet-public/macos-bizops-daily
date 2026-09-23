#!/usr/bin/env python3
"""Spec §17.6 smoke for ``search_sessions`` over pushed summary chunks.

Coverage:

* Clean-path push: a SecretGate-clean summary chunk lands in
  ``session_ledger__summary``; an embedding is generated; a vector is
  stored; the returned envelope carries summary_id + embedding_vector_id.
* Ranking: ``search`` returns envelopes ordered by descending score.
* top_k respected: ``limit`` caps the result set.
* Empty-corpus: ``search`` against an empty ANN result returns ``[]``.
* Defense-in-depth re-scan on read (spec §10.10.3): a row whose
  ``summary_text`` trips the SecretGate at read time is surfaced as
  ``quarantined_in_result`` with ``summary_text=None`` and a
  ``quarantine_detector`` annotation, while clean rows pass through
  unchanged.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/summary_search_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import StubStateService  # noqa: E402
from ananta.core.domain.types import ActionResult  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import TABLE_SUMMARY  # noqa: E402
from ananta.llm.session_ledger.summarization import (  # noqa: E402
    VECTOR_NAMESPACE,
    SummaryWriter,
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

    Pre-2026-06-01 fix this stub returned ``data=payload`` directly. Both
    the summarization read path AND this stub matched the same wrong shape,
    so search_sessions returned empty in production while the smoke passed.
    Now the stub mirrors the real envelope from
    ``plugins/pgvector_service_plugin/.../plugin.py:_create_success_result``
    so production-shape divergence cannot recur.
    """
    return ActionResult(
        action_status="completed",
        data={"result": payload},
        actions=[],
        error=None,
        timestamp=datetime.now(UTC).isoformat(),
    )


class _StubEmbeddingService:
    """Returns a deterministic 3-d embedding indexed by hash of the input string."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate_embeddings(
        self,
        inputs: list[str],
        model: str | None = None,
        input_type: str = "text",
    ) -> ActionResult:
        del model, input_type
        for inp in inputs:
            self.calls.append(inp)
        # Trivial deterministic 3-d vector — distinguishability doesn't matter
        # for the search smoke because ANN ranking comes from the stub vector
        # service's pre-arranged result set.
        vectors = [[float(len(s)), 0.0, 0.0] for s in inputs]
        # _ok adds the service-plugin ``{"result": ...}`` wrap; this stub
        # returns the inner payload only. Pre-2026-06-01 fix this stub
        # pre-wrapped manually because ``_ok`` did not wrap at all; that
        # had the same defect described on ``_ok``.
        return _ok({"embeddings": vectors, "dimension": 3, "model": "stub"})

    def get_embedding_dimension(self, model: str | None = None) -> ActionResult:
        del model
        return _ok({"dimension": 3})

    def list_models(self) -> ActionResult:
        return _ok({"models": []})

    def is_ready(self) -> bool:
        return True

    def get_readiness_error(self) -> str | None:
        return None


class _StubVectorService:
    """Records every store + serves canned ``search_similar`` responses by external_id."""

    def __init__(self) -> None:
        self.store_calls: list[dict[str, Any]] = []
        # external_id -> internal vec_id assigned at store time
        self._external_to_vec_id: dict[str, str] = {}
        self._next_vec = 0
        self.search_response: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    def store_vectors(
        self, namespace: str, vectors: list[dict[str, object]]
    ) -> ActionResult:
        self.store_calls.append({"namespace": namespace, "vectors": vectors})
        ids: list[str] = []
        for vec in vectors:
            self._next_vec += 1
            vec_id = f"vec_{self._next_vec:04d}"
            external = vec.get("external_id")
            if isinstance(external, str):
                self._external_to_vec_id[external] = vec_id
            ids.append(vec_id)
        return _ok({"inserted_ids": ids})

    def search_similar(
        self,
        namespace: str,
        query_vector: list[float],
        top_k: int = 10,
        filters: dict[str, object] | None = None,
        distance_metric: object = None,
    ) -> ActionResult:
        del namespace, query_vector, filters, distance_metric
        self.search_calls.append({"top_k": top_k})
        return _ok({"results": list(self.search_response[:top_k])})

    def get_vector(self, namespace: str, vector_id: str) -> ActionResult:  # pragma: no cover
        del namespace, vector_id
        return _ok({})

    def delete_vectors(self, *args: object, **kwargs: object) -> ActionResult:
        del args, kwargs
        return _ok({})

    def delete_by_external_ids(self, *args: object, **kwargs: object) -> ActionResult:
        del args, kwargs
        return _ok({})

    def delete_all_in_namespace(self, namespace: str) -> ActionResult:
        del namespace
        return _ok({})

    def update_metadata(self, *args: object, **kwargs: object) -> ActionResult:
        del args, kwargs
        return _ok({})

    def list_namespaces(self) -> ActionResult:
        return _ok({})

    def get_namespace_stats(self, namespace: str) -> ActionResult:
        del namespace
        return _ok({})

    def is_ready(self) -> bool:
        return True

    def get_readiness_error(self) -> str | None:
        return None


def _build_writer() -> tuple[
    SummaryWriter, StubStateService, _StubEmbeddingService, _StubVectorService
]:
    state = StubStateService()
    repo = SessionLedgerRepository(state_service=state)  # type: ignore[arg-type]
    embed = _StubEmbeddingService()
    vec = _StubVectorService()
    # SecretGate v1 was ripped 2026-06-11 — SummaryWriter no longer accepts
    # a ``secret_gate=`` kwarg and the read path no longer re-scans content;
    # ``quarantined_in_result`` + ``quarantine_detector`` fields were removed
    # from ``SearchResultEnvelope`` accordingly.
    writer = SummaryWriter(
        repository=repo,
        embedding_service=embed,  # type: ignore[arg-type]
        vector_service=vec,  # type: ignore[arg-type]
    )
    return writer, state, embed, vec


# ─── Clean-path push ──────────────────────────────────────────────────────


def test_clean_path_push_writes_row_and_vector() -> None:
    writer, state, embed, vec = _build_writer()
    result = writer.push_summary_chunk(
        session_id="les_abc",
        chunk_index=0,
        summary_text="A nuanced discussion of the rebrand strategy.",
        generated_by_client_id="client_alpha",
    )
    _check(
        result.get("summary_id", "").startswith("sum_"),
        "clean-path push returns sum_-prefixed summary_id",
    )
    # Post-kara-2026-06-10 fix: ``_store_vector`` returns the deterministic
    # ``external_id`` (``session_id:chunk_index``) rather than pgvector's
    # internal pgvector id. The summary row's ``embedding_vector_id``
    # column stores this string so the ANN's ``external_id`` matches at
    # search time. See ``summarization._store_vector`` for the rationale.
    _check(
        result.get("embedding_vector_id") == "les_abc:0",
        f"clean-path push returns the deterministic external_id "
        f"({result.get('embedding_vector_id')!r})",
    )
    inserts = [w for w in state.writes if w.table == TABLE_SUMMARY]
    _check(len(inserts) == 1, "exactly one write_state into session_ledger__summary")
    _check(len(embed.calls) == 1, "embedding_service.generate_embeddings called once")
    _check(len(vec.store_calls) == 1, "vector_service.store_vectors called once")
    _check(
        vec.store_calls[0]["namespace"] == VECTOR_NAMESPACE,
        f"vector namespace = {VECTOR_NAMESPACE!r}",
    )


# ─── Search ranking + top_k ───────────────────────────────────────────────


def _push_chunks(
    writer: SummaryWriter, items: list[tuple[str, int, str]]
) -> None:
    for session_id, chunk_index, text in items:
        writer.push_summary_chunk(
            session_id=session_id,
            chunk_index=chunk_index,
            summary_text=text,
            generated_by_client_id="client_alpha",
        )


def test_search_orders_by_score_and_respects_top_k() -> None:
    writer, state, _, vec = _build_writer()
    # Pre-arrange three pushed summaries to set up the stubbed ANN response.
    pushed = [
        ("les_a", 0, "summary text alpha"),
        ("les_b", 0, "summary text beta beta"),
        ("les_c", 0, "summary text gamma gamma gamma"),
    ]
    _push_chunks(writer, pushed)
    # Map external_id → row read-back so the repository.list_summaries_by_external_ids
    # returns deterministic rows for assertion. The stub state_service requires
    # us to wire SELECT responses keyed by SQL substring.
    state.add_select_response(
        "FROM session_ledger__summary WHERE embedding_vector_id IN",
        [
            {
                "id": "sum_a",
                "session_id": "les_a",
                "chunk_index": 0,
                "summary_text": "summary text alpha",
                "embedding_vector_id": "vec_0001",
                "generated_at": datetime(2026, 5, 31, 1, 0, tzinfo=UTC),
                "generated_by_client_id": "client_alpha",
            },
            {
                "id": "sum_b",
                "session_id": "les_b",
                "chunk_index": 0,
                "summary_text": "summary text beta beta",
                "embedding_vector_id": "vec_0002",
                "generated_at": datetime(2026, 5, 31, 1, 1, tzinfo=UTC),
                "generated_by_client_id": "client_alpha",
            },
            {
                "id": "sum_c",
                "session_id": "les_c",
                "chunk_index": 0,
                "summary_text": "summary text gamma gamma gamma",
                "embedding_vector_id": "vec_0003",
                "generated_at": datetime(2026, 5, 31, 1, 2, tzinfo=UTC),
                "generated_by_client_id": "client_alpha",
            },
        ],
    )
    state.add_select_response(
        "FROM session_ledger__session WHERE id IN",
        [
            {"id": "les_a", "external_session_id": "ext_a"},
            {"id": "les_b", "external_session_id": "ext_b"},
            {"id": "les_c", "external_session_id": "ext_c"},
        ],
    )
    # Stub vector service ranks: B highest, A second, C lowest.
    # Real pgvector envelope carries cosine ``distance`` (range [0, 2]); the
    # summarization read path converts distance → similarity (1 - distance).
    # Mirror the production field name so a future field-name drift can never
    # mask itself behind a stub that pre-shapes the right value under the
    # wrong key — same divergence-guard pattern as the inline comments at
    # summarization.py:314-319 / 341-346 (envelope-shape fix 2026-06-01).
    vec.search_response = [
        {"external_id": "vec_0002", "distance": 0.09},  # similarity 0.91
        {"external_id": "vec_0001", "distance": 0.26},  # similarity 0.74
        {"external_id": "vec_0003", "distance": 0.45},  # similarity 0.55
    ]
    envelopes = writer.search(query="strategy session", limit=2)
    _check(
        vec.search_calls and vec.search_calls[-1]["top_k"] == 2,
        "limit=2 is passed through to vector_service.search_similar as top_k",
    )
    _check(
        envelopes[0].session_id == "les_b" and envelopes[0].score >= envelopes[1].score,
        "results ordered by score DESC; first result is the top-scoring summary",
    )
    _check(
        all(env.summary_text for env in envelopes),
        "clean rows surface their summary_text",
    )
    # Task #20 dispatch criteria (2026-06-01) — guard against the pre-fix
    # field-name mismatch where ``r.get("score", 0.0)`` against pgvector's
    # ``distance``-keyed rows made every envelope score=0.0.
    # Scope the score-positivity check to the ANN-result rows. The stub
    # ``list_summaries_by_external_ids`` returns the full row set under test
    # regardless of the IN filter, so an envelope outside the ANN top-k
    # legitimately falls back to score=0.0; the production repo's SQL IN
    # filter never lets that happen. Top-2 came back from limit=2.
    ann_envelopes = envelopes[:2]
    _check(
        all(env.score > 0.0 for env in ann_envelopes),
        "every ANN-in envelope carries score > 0 (not the 0.0 fallback)",
    )
    _check(
        len({env.score for env in ann_envelopes}) == len(ann_envelopes),
        "scores are distinguishable across results (not all equal)",
    )
    _check(
        abs(ann_envelopes[0].score - 0.91) < 1e-9
        and abs(ann_envelopes[1].score - 0.74) < 1e-9,
        "scores are similarity (1 - distance), not raw distance",
    )


def test_search_empty_corpus_returns_empty_list() -> None:
    writer, _, _, vec = _build_writer()
    vec.search_response = []
    envelopes = writer.search(query="anything", limit=5)
    _check(envelopes == [], "empty ANN result → empty envelope list")


# ─── Defense-in-depth re-scan on read (RETIRED 2026-06-11 SecretGate v1 rip-out)
# The ``test_search_rescan_quarantines_newly_tripping_content`` smoke
# verified that a row whose ``summary_text`` tripped a SecretGate detector
# on read was surfaced via ``quarantined_in_result=True`` + dropped text.
# Per the 2026-06-11 SecretGate v1 rip-out the read-time re-scan no longer
# runs, ``SearchResultEnvelope`` no longer carries ``quarantined_in_result``
# or ``quarantine_detector`` fields, and the test premise is no longer
# expressible. Test retired as dead-code per Boy Scout drawdown.


def main() -> int:
    print("=== summary_search_smoke ===")
    test_clean_path_push_writes_row_and_vector()
    test_search_orders_by_score_and_respects_top_k()
    test_search_empty_corpus_returns_empty_list()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
