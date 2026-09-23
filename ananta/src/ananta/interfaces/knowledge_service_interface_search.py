"""Knowledge-base search + retrieval-quality plugin-contract methods (W5.R).

Live semantic search (``search``), planning-reference search
(``search_planning_references`` — W5.R drift-closure addition),
single-article retrieval test (``test_retrieval``), and whole-corpus
retrieval audit (``audit_retrieval_corpus``).
"""

from abc import ABC, abstractmethod
from typing import Any


class KnowledgeSearchInterface(ABC):
    """KB search + retrieval-quality abstract methods."""

    @abstractmethod
    def search(
        self,
        query: str,
        top_k: int = 8,
        name: str | None = None,
        process_key: str | None = None,
        tags: list[str] | None = None,
        min_score: float | None = None,
        knowledge_layers: list[int] | None = None,
        min_knowledge_layer: int | None = None,
        max_knowledge_layer: int | None = None,
        include_unlayered: bool = False,
    ) -> dict[str, Any]:
        """Search indexed content.
        Pre-filter: only active KBs (is_active=1 in knowledge_install).
        Deduplicate by memory_id. Returns chunks with provenance.
        When min_score is set, results below that similarity threshold are excluded.

        Layer-related arguments restrict results to chunks at the
        specified knowledge abstraction layers. ``knowledge_layers`` is
        an exact list (e.g. ``[1, 3]``); ``min_knowledge_layer`` and
        ``max_knowledge_layer`` define an inclusive range. The two
        forms are mutually exclusive — pass one or the other, never
        both. When any layer argument is set, chunks without a layer
        annotation are excluded by default; pass ``include_unlayered=
        True`` to include them. Layer constraints are strict across
        every retrieval tier (process-key, user-tag, semantic, and
        diversity-fill); unfiltered semantic fallback is not allowed
        when a layer filter is active.
        """
        ...

    @abstractmethod
    def search_planning_references(
        self,
        query: str,
        top_k: int = 8,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Layered planning-references search across active KB content.

        Mirrors ``search`` but scoped to the planning-references corpus and
        applies the platform's per-stage layer policy (Layer-1 binding
        contracts for WBS authoring, Layers 2-3 for design + brief work,
        Layers 2-4 for discovery / scoping). Returns the same chunk-with-
        provenance shape as ``search``.
        """
        ...

    @abstractmethod
    def test_retrieval(
        self,
        article_path: str,
        target_queries: list[str],
        forbidden_queries: list[str],
        min_rank: int = 3,
        forbidden_min_rank: int = 4,
        active_knowledge_bases: list[str] | None = None,
        process_key_assertions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Evaluate retrieval quality for a single article.

        For each target query the article must rank at or above ``min_rank``
        (1-indexed, smaller is better); for each forbidden query it must be
        absent or rank no better than ``forbidden_min_rank``. Every key in
        ``process_key_assertions`` must resolve in the live process registry.
        ``article_path`` is repo-relative starting at ``knowledge_bases/``;
        ``active_knowledge_bases`` defaults to all currently active KBs.
        Returns a RetrievalTestReport wrapped in ``{"status", "data"}``.
        """
        ...

    @abstractmethod
    def audit_retrieval_corpus(
        self,
        corpus_root: str = "knowledge_bases",
        report_dir: str = "profile/data/kb_retrieval_audit_reports",
        active_knowledge_bases: list[str] | None = None,
        fail_fast: bool = False,
    ) -> dict[str, Any]:
        """Audit retrieval quality across the whole corpus.

        Walks every ``*.retrieval_test.yaml`` under ``corpus_root``, runs the
        per-article retrieval test against each, aggregates DRIFT, OVERREACH,
        and STALE_PROCESS_KEY findings, and writes a timestamped Markdown
        report under ``report_dir``. Only active knowledge bases are audited;
        ``active_knowledge_bases`` narrows that to a subset of the active set.
        ``fail_fast`` stops after the first failing article. Returns an
        AuditReport wrapped in ``{"status", "data"}``.
        """
        ...

    @abstractmethod
    def audit_retrieval_corpus_cron(self) -> dict[str, Any]:
        """Scheduler-fired EDGE_SINK sibling of ``audit_retrieval_corpus``.

        Submits the full corpus walk (default ``corpus_root``/``report_dir``)
        to a single-slot background executor and returns immediately with a
        started/already-running receipt — running the walk inline on the
        action-queue thread would park the serial poll loop for its whole
        multi-minute duration (the fast-return-contract violation class).
        Cron-only wiring (``is_discoverable=False``); not surfaced via
        ``process_search``. The model-callable equivalent is
        ``audit_retrieval_corpus``.
        """
        ...

