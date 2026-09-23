"""Knowledge-base search + retrieval-quality service-interface verbs
(W5.Q decomposition).

Live semantic search (``search``), planning-reference search
(``search_planning_references``), single-article retrieval test
(``test_retrieval``), and whole-corpus retrieval audit
(``audit_retrieval_corpus``). Lifted byte-for-byte from the
W5.Q-pre-decomposition ``KnowledgeServiceAPI``.
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process


class KnowledgeSearchAPI(ABC):
    """Knowledge-base search + retrieval-quality verbs — search / search_planning_references / test_retrieval / audit_retrieval_corpus."""

    @service_interface_process(
        name="search",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "query": ParameterMetadata(
                description="Search query text",
                required=True,
                type=ParameterType.STRING,
            ),
            "top_k": ParameterMetadata(
                description="Maximum results to return",
                required=False,
                type=ParameterType.INTEGER,
                default=8,
            ),
            "name": ParameterMetadata(
                description=(
                    "Scope the search to a single knowledge base by name. "
                    "Required to reach knowledge bases that are excluded from "
                    "default semantic search (e.g. workbench): an explicit name "
                    "bypasses the default-scope exclusion, while an unscoped "
                    "search never returns those KBs' semantic results."
                ),
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "process_key": ParameterMetadata(
                description=(
                    "Tier-1 exact process-key match: restrict results to chunks "
                    "tagged with this process key, e.g. to find the knowledge "
                    "article(s) bound to a specific verb/process. Runs ahead of "
                    "the tag and semantic tiers and (like tags) is not subject to "
                    "the default-scope KB exclusion."
                ),
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "tags": ParameterMetadata(
                description="Filter by tags (tier-2 tag match)",
                required=False,
                type=ParameterType.LIST,
                default=None,
            ),
            "min_score": ParameterMetadata(
                description="Minimum similarity score threshold (0.0-1.0). Results below this are excluded.",
                required=False,
                type=ParameterType.FLOAT,
                default=None,
            ),
            "knowledge_layers": ParameterMetadata(
                description=(
                    "Restrict results to chunks at these knowledge abstraction "
                    "layers (exact list, e.g. [1, 3]). Lower numbers are closer "
                    "to execution. Mutually exclusive with min_knowledge_layer "
                    "and max_knowledge_layer. "
                    "Canonical policy by planning stage: "
                    "WBS authoring uses max_knowledge_layer=1; "
                    "PipelineSpec authoring uses [1, 2]; "
                    "Composition Design Document authoring uses [2, 3]; "
                    "Brief authoring uses [2, 3]; "
                    "Discovery / scoping uses [2, 3, 4]. "
                    "See knowledge_bases/ananta_platform/"
                    "14_knowledge_retrieval/knowledge_layer_registry.md for "
                    "the layer taxonomy."
                ),
                required=False,
                type=ParameterType.LIST,
                default=None,
                validation={
                    "items": {"type": "integer", "minimum": 1},
                    "minItems": 1,
                    "maxItems": 16,
                },
            ),
            "min_knowledge_layer": ParameterMetadata(
                description=(
                    "Lower bound (inclusive) of the allowed knowledge-layer "
                    "range. Must be paired with max_knowledge_layer; mutually "
                    "exclusive with knowledge_layers."
                ),
                required=False,
                type=ParameterType.INTEGER,
                default=None,
            ),
            "max_knowledge_layer": ParameterMetadata(
                description=(
                    "Upper bound (inclusive) of the allowed knowledge-layer "
                    "range. Required when min_knowledge_layer is set. The "
                    "range span is capped at 16 layers per query. "
                    "Use max_knowledge_layer=1 alone (no min) at WBS "
                    "authoring and render-execution time to retrieve only "
                    "Layer-1 binding contracts (process registry articles, "
                    "tool argument shapes, frame articles, drivers)."
                ),
                required=False,
                type=ParameterType.INTEGER,
                default=None,
            ),
            "include_unlayered": ParameterMetadata(
                description=(
                    "When any layer filter is supplied, unlabeled chunks are "
                    "excluded by default. Set this true to include them — "
                    "useful during the layer-annotation migration when not "
                    "every article has been classified yet."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Search results with provenance",
            type=ParameterType.OBJECT,
            properties={
                "results": ParameterMetadata(
                    type=ParameterType.LIST, description="Search result chunks with provenance"
                ),
                "count": ParameterMetadata(type=ParameterType.INTEGER, description="Result count"),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
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
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="search_planning_references",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "query": ParameterMetadata(
                description="Search query (typically the user's topic or operation)",
                required=True,
                type=ParameterType.STRING,
            ),
            "top_k": ParameterMetadata(
                description="Maximum results to return",
                required=False,
                type=ParameterType.INTEGER,
                default=8,
            ),
            "tags": ParameterMetadata(
                description="Tag filters for article selection",
                required=False,
                type=ParameterType.LIST,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Planning reference articles with provenance",
            type=ParameterType.OBJECT,
            properties={
                "results": ParameterMetadata(
                    type=ParameterType.LIST, description="Planning reference chunks with provenance"
                ),
                "count": ParameterMetadata(type=ParameterType.INTEGER, description="Result count"),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def search_planning_references(
        self,
        query: str,
        top_k: int = 8,
        tags: list[str] | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="test_retrieval",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "article_path": ParameterMetadata(
                description=(
                    "Repo-relative path to the article under test, starting at "
                    "'knowledge_bases/' (e.g. "
                    "knowledge_bases/ananta_platform/13_solet_setup/"
                    "04_when_to_split_a_service_plugin.md)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "target_queries": ParameterMetadata(
                description="Queries that must surface this article within min_rank",
                required=True,
                type=ParameterType.LIST,
            ),
            "forbidden_queries": ParameterMetadata(
                description=(
                    "Queries that must NOT surface this article better than "
                    "forbidden_min_rank (owned by other articles)"
                ),
                required=True,
                type=ParameterType.LIST,
            ),
            "min_rank": ParameterMetadata(
                description="Best acceptable 1-indexed rank for a target query (smaller is stricter)",
                required=False,
                type=ParameterType.INTEGER,
                default=3,
            ),
            "forbidden_min_rank": ParameterMetadata(
                description=(
                    "A forbidden query fails if the article ranks better than "
                    "this 1-indexed position"
                ),
                required=False,
                type=ParameterType.INTEGER,
                default=4,
            ),
            "active_knowledge_bases": ParameterMetadata(
                description=(
                    "Optional subset of active knowledge bases to evaluate "
                    "against; default is all currently active KBs"
                ),
                required=False,
                type=ParameterType.LIST,
                default=None,
            ),
            "process_key_assertions": ParameterMetadata(
                description=(
                    "Process keys referenced by the article body that must "
                    "still resolve in the live registry"
                ),
                required=False,
                type=ParameterType.LIST,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Retrieval test report with verdict and per-check diagnostics",
            type=ParameterType.OBJECT,
            properties={
                "passed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when every target, forbidden, and key check passed",
                ),
                "target_results": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Per-target-query observed rank, score, and top-3 paths",
                ),
                "forbidden_results": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Per-forbidden-query observed rank, score, and top-3 paths",
                ),
                "process_key_freshness": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Per-asserted-key registry existence records",
                ),
                "failures": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Human-readable DRIFT, OVERREACH, and STALE_PROCESS_KEY findings",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
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
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="audit_retrieval_corpus",
        is_discoverable=True,
        provider="knowledge_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "corpus_root": ParameterMetadata(
                description=(
                    "Repo-relative directory to walk for "
                    "'*.retrieval_test.yaml' companion files"
                ),
                required=False,
                type=ParameterType.STRING,
                default="knowledge_bases",
            ),
            "report_dir": ParameterMetadata(
                description=(
                    "Repo-relative directory the timestamped Markdown report "
                    "is written into"
                ),
                required=False,
                type=ParameterType.STRING,
                default="profile/data/kb_retrieval_audit_reports",
            ),
            "active_knowledge_bases": ParameterMetadata(
                description=(
                    "Optional subset of active knowledge bases whose articles "
                    "are audited; default is every active KB"
                ),
                required=False,
                type=ParameterType.LIST,
                default=None,
            ),
            "fail_fast": ParameterMetadata(
                description="Stop after the first article that produces any finding",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Corpus audit report with aggregated retrieval findings",
            type=ParameterType.OBJECT,
            properties={
                "ran_at": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="ISO 8601 timestamp the audit ran at",
                ),
                "corpus_root": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The corpus root that was walked",
                ),
                "total_articles_audited": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of articles run through the retrieval test",
                ),
                "passed": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Articles with no findings",
                ),
                "failed": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Articles with at least one finding",
                ),
                "drifts": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="DRIFT findings: article_path, query, observed_rank, min_rank",
                ),
                "overreaches": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "OVERREACH findings: article_path, query, observed_rank, "
                        "forbidden_min_rank"
                    ),
                ),
                "stale_keys": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="STALE_PROCESS_KEY findings: article_path, process_key",
                ),
                "report_path": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Absolute path of the Markdown report written",
                ),
                "duration_seconds": ParameterMetadata(
                    type=ParameterType.FLOAT,
                    description="Wall-clock seconds the audit loop took",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def audit_retrieval_corpus(
        self,
        corpus_root: str = "knowledge_bases",
        report_dir: str = "workbench/kb_retrieval_audit_reports",
        active_knowledge_bases: list[str] | None = None,
        fail_fast: bool = False,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="audit_retrieval_corpus_cron",
        is_discoverable=False,  # cron-fired only; not model-discoverable
        provider="knowledge_service",
        # EDGE_SINK terminal-action shape — action_queue_poller short-circuits
        # at the EDGE_SINK_SKIP branch (result_processor_kind is None and
        # result_processor is None -> no dispatch). No inference scaffold fires.
        # The submit-to-background-executor body additionally satisfies the
        # fast-return contract (21_scheduling_service/02_action_queue_fast_return_
        # contract.md): the actual corpus walk (~14s/article, several minutes for
        # the full corpus) runs on a daemon thread, off the queue.
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Background-submission receipt",
            type=ParameterType.OBJECT,
            properties={
                "audit": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'started' if this fire began a new background "
                    "audit pass, 'already_running' if a prior pass is still in "
                    "flight (no-op)",
                ),
            },
        ),
        # No result_processor_customizations / error_processor_customizations
        # per the canonical EDGE_SINK contract.
    )
    @abstractmethod
    def audit_retrieval_corpus_cron(self) -> dict[str, Any]: ...
