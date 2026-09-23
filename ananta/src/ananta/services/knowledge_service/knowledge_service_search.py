"""Knowledge-service wrapper sub-mixin for KB search + retrieval-testing delegates (W5.S).

Delegates satisfying the W5.R-decomposed :class:`KnowledgeSearchInterface`:
``search``, ``search_planning_references``, ``test_retrieval``,
``audit_retrieval_corpus``, and its 2026-07-26 EDGE_SINK cron sibling
``audit_retrieval_corpus_cron``. The first four are lifted byte-for-byte from
the W5.S-pre-decomposition ``KnowledgeService.__init__.py``.

Surface-only note (per W5.R C5 + W5.S surface-only flag): the existing
``search_planning_references`` delegate forwards to ``self._get_backend().search(...)``
rather than ``self._get_backend().search_planning_references(...)``. W5.S preserves
this bypass byte-for-byte; flag for a separate cleanup workstream if the plugin's
``search_planning_references`` ever diverges from ``search`` behavior. Functionally
equivalent today post-W5.R because the plugin's ``search_planning_references`` is a
pure passthrough to ``search`` per the W5.R C1 fold.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ananta.interfaces.knowledge_service_interface_search import (
        KnowledgeSearchInterface,
    )


class KnowledgeSearchWrapper:
    """Search + retrieval-testing delegate methods. Inherited via MI."""

    if TYPE_CHECKING:
        def _get_backend(self) -> "KnowledgeSearchInterface": ...

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
        return self._get_backend().search(
            query=query, top_k=top_k, name=name, process_key=process_key, tags=tags,
            min_score=min_score,
            knowledge_layers=knowledge_layers,
            min_knowledge_layer=min_knowledge_layer,
            max_knowledge_layer=max_knowledge_layer,
            include_unlayered=include_unlayered,
        )

    def search_planning_references(
        self,
        query: str,
        top_k: int = 8,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().search(query=query, top_k=top_k, tags=tags)

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
        return self._get_backend().test_retrieval(
            article_path=article_path,
            target_queries=target_queries,
            forbidden_queries=forbidden_queries,
            min_rank=min_rank,
            forbidden_min_rank=forbidden_min_rank,
            active_knowledge_bases=active_knowledge_bases,
            process_key_assertions=process_key_assertions,
        )

    def audit_retrieval_corpus(
        self,
        corpus_root: str = "knowledge_bases",
        report_dir: str = "profile/data/kb_retrieval_audit_reports",
        active_knowledge_bases: list[str] | None = None,
        fail_fast: bool = False,
    ) -> dict[str, Any]:
        return self._get_backend().audit_retrieval_corpus(
            corpus_root=corpus_root,
            report_dir=report_dir,
            active_knowledge_bases=active_knowledge_bases,
            fail_fast=fail_fast,
        )

    def audit_retrieval_corpus_cron(self) -> dict[str, Any]:
        return self._get_backend().audit_retrieval_corpus_cron()
