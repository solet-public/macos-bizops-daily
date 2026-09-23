"""DefaultKnowledgePlugin search + retrieval-quality sub-Mixin (W5.T).

Five KSI search/retrieval-quality methods plus one private helper:
search / search_planning_references / test_retrieval / audit_retrieval_corpus /
audit_retrieval_corpus_cron / _run_single_audit. The first four (plus the
helper) are lifted byte-for-byte from the W5.T-pre-decomposition
``DefaultKnowledgePlugin``. Inherited via MI from the residual class.

Audit-report path helpers (_AUDIT_REPORT_DIR, _AUDIT_REPORT_STEM_FORMAT,
_resolve_repo_relative) are co-located with the audit_retrieval_corpus method
that uses them — Search-only per the W5.T inventory cross-mixin scan.

``audit_retrieval_corpus_cron`` (2026-07-26, B-M6) is the EDGE_SINK
scheduler-fired sibling: it submits the corpus walk to the plugin's
single-slot background executor (``self._kb_audit_executor``, a
``BoundedSummaryExecutor`` owned by ``DefaultKnowledgePlugin.__init__``) and
returns a started/already-running receipt in milliseconds, so a cron fire
never parks the serial action-queue poll loop for the walk's multi-minute
duration (KB ``21_scheduling_service/02_action_queue_fast_return_contract.md``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .constants import PLUGIN_NAME
from .kb_process_registry import registry_has_process_key
from .kb_search import _SEARCH_MIN_TOP_K, apply_min_score, collect_tiered_results, get_active_names
from .layer_filter import build_layer_constraint
from .retrieval_audit import (
    RATIFIED_SCOPE_PLAN,
    AuditCase,
    ScopePolicy,
    aggregate_audit,
    discover_test_files,
    filter_cases_to_active,
    parse_test_file,
    render_markdown,
    run_case_under_scope_plan,
)
from .retrieval_test import build_search_fn, run_retrieval_test

_AUDIT_REPORT_DIR = "profile/data/kb_retrieval_audit_reports"
_AUDIT_REPORT_STEM_FORMAT = "%Y-%m-%dT%H-%M-%SZ"


def _resolve_repo_relative(value: str, repo_root: Path) -> Path:
    """Resolve a path string against repo_root when relative, absolute as-is.

    The solet runs from profile/, so a bare ``Path("knowledge_bases")`` resolves
    against the wrong CWD; the audit's schema-documented defaults must be
    resolved against the repo root.
    """
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


class KnowledgeSearchPluginMixin:
    """KB search + retrieval-quality verb implementations. Inherited via MI."""

    if TYPE_CHECKING:
        from ananta.services.session_ledger_service.summary_executor import (
            SummaryExecutor,
        )

        # Service-state attributes owned by DefaultKnowledgePlugin.__init__ + prepare_for_readiness.
        # orchestrator_ref is inherited from PluginBase on the residual class.
        _kb_root: Path | None
        _memory_service: Any
        _state_service: Any
        _kb_audit_executor: SummaryExecutor
        orchestrator_ref: Any

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
        """Tiered search: process_key match → tag filter → semantic similarity."""
        layer_constraint = build_layer_constraint(
            knowledge_layers=knowledge_layers,
            min_knowledge_layer=min_knowledge_layer,
            max_knowledge_layer=max_knowledge_layer,
            include_unlayered=include_unlayered,
        )

        active_names_all = get_active_names(self._state_service)
        if name is not None:
            active_names = [name] if name in active_names_all else []
        else:
            active_names = active_names_all

        if not active_names:
            return {"status": "success", "data": {"results": [], "count": 0}}

        effective_top_k = max(top_k, _SEARCH_MIN_TOP_K)
        results = collect_tiered_results(
            query, active_names, effective_top_k, process_key, tags,
            self._memory_service,
            honor_exclusions=(name is None),
            layer_constraint=layer_constraint,
        )
        final = apply_min_score(results, min_score)
        if layer_constraint.active:
            final = [r for r in final if layer_constraint.matches(r.get("knowledge_layer"))]
        return {"status": "success", "data": {"results": final, "count": len(final)}}

    def search_planning_references(
        self,
        query: str,
        top_k: int = 8,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Layered planning-references search across active KB content.

        Per W5.R (C1 drift closure): satisfies the
        ``KnowledgeSearchInterface.search_planning_references`` abstract method
        so ``DefaultKnowledgePlugin`` remains instantiable post-W5.R. Body
        currently forwards to ``self.search(...)`` to mirror the existing
        ``KnowledgeService`` wrapper behavior at
        ``services/knowledge_service/__init__.py``; a separate workstream
        (C5, flagged in W5.R review) replaces both forwards with a genuine
        layered planning-references implementation.
        """
        return self.search(query=query, top_k=top_k, tags=tags)

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
        """Evaluate retrieval quality for an article."""
        def _run_search(
            query: str, top_k: int, kb_name: str | None,
        ) -> list[dict[str, Any]]:
            response = self.search(query, top_k=top_k, name=kb_name)
            results = response.get("data", {}).get("results", [])
            return [r for r in results if isinstance(r, dict)]

        search_fn = build_search_fn(
            _run_search,
            get_active_names(self._state_service),
            active_knowledge_bases,
        )
        report = run_retrieval_test(
            article_path=article_path,
            target_queries=target_queries,
            forbidden_queries=forbidden_queries,
            min_rank=min_rank,
            forbidden_min_rank=forbidden_min_rank,
            process_key_assertions=process_key_assertions or [],
            search_fn=search_fn,
            registry_has_key=lambda pk: registry_has_process_key(pk, self.orchestrator_ref),
        )
        return {"status": "success", "data": report}

    def audit_retrieval_corpus(
        self,
        corpus_root: str = "knowledge_bases",
        report_dir: str = _AUDIT_REPORT_DIR,
        active_knowledge_bases: list[str] | None = None,
        fail_fast: bool = False,
    ) -> dict[str, Any]:
        """Walk every ``*.retrieval_test.yaml`` under ``corpus_root``, run the
        per-article retrieval test against each under the ratified split scope
        (``retrieval_audit.RATIFIED_SCOPE_PLAN``), aggregate DRIFT, OVERREACH,
        STALE_PROCESS_KEY, and LEGACY_KEY findings, and write a Markdown report
        under ``report_dir``.
        """
        if self._kb_root is None:
            raise RuntimeError(f"{PLUGIN_NAME}: knowledge_base_root not configured")
        repo_root = self._kb_root.parent
        corpus_path = _resolve_repo_relative(corpus_root, repo_root)
        report_path_base = _resolve_repo_relative(report_dir, repo_root)
        cases = [
            parse_test_file(path)
            for path in discover_test_files(corpus_path)
        ]
        total_fixtures_discovered = len(cases)
        cases = filter_cases_to_active(
            cases, get_active_names(self._state_service), active_knowledge_bases,
        )

        started = time.monotonic()
        aggregate = aggregate_audit(
            cases=cases, run_single=self._run_single_audit, fail_fast=fail_fast,
        )
        duration_seconds = time.monotonic() - started

        now = datetime.now(UTC)
        report_path = (
            report_path_base / f"{now.strftime(_AUDIT_REPORT_STEM_FORMAT)}.md"
        ).resolve()
        ran_at = now.isoformat()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            render_markdown(
                aggregate=aggregate,
                ran_at=ran_at,
                corpus_root=str(corpus_path),
                report_path=str(report_path),
                duration_seconds=duration_seconds,
                total_fixtures_discovered=total_fixtures_discovered,
            ),
            encoding="utf-8",
        )

        return {
            "status": "success",
            "data": {
                "ran_at": ran_at,
                "corpus_root": str(corpus_path),
                "total_fixtures_discovered": total_fixtures_discovered,
                "total_articles_audited": aggregate.total_articles_audited,
                "passed": aggregate.passed,
                "failed": aggregate.failed,
                "drifts": aggregate.drifts,
                "overreaches": aggregate.overreaches,
                "stale_keys": aggregate.stale_keys,
                "legacy_keys": aggregate.legacy_keys,
                "report_path": str(report_path),
                "duration_seconds": duration_seconds,
            },
        }

    def audit_retrieval_corpus_cron(self) -> dict[str, Any]:
        """Scheduler-fired EDGE_SINK sibling of ``audit_retrieval_corpus``.

        Submits the default-corpus walk to the single-slot background
        executor and returns immediately; the walk itself runs
        ``audit_retrieval_corpus`` with its schema defaults on the daemon
        worker thread. A fire that lands while a prior pass is still running
        is a no-op (``already_running``) — see ``BoundedSummaryExecutor``.
        """
        accepted = self._kb_audit_executor.submit(self.audit_retrieval_corpus)
        return {
            "status": "success",
            "data": {"audit": "started" if accepted else "already_running"},
        }

    def _run_single_audit(self, case: AuditCase) -> dict[str, Any]:
        """Run one article's retrieval test under the ratified split-scope plan.

        ``min_rank``/DRIFT is judged within the article's own knowledge base;
        forbidden/OVERREACH is judged across every active one
        (``RATIFIED_SCOPE_PLAN``, ``retrieval_audit.ScopePolicy`` -- the same
        plan ``run_retrieval_companions.py`` executes, one definition for both).
        """
        def _call(
            scope: ScopePolicy, target_queries: list[str], forbidden_queries: list[str],
        ) -> dict[str, Any]:
            response = self.test_retrieval(
                article_path=case.article_path,
                target_queries=target_queries,
                forbidden_queries=forbidden_queries,
                min_rank=case.min_rank,
                forbidden_min_rank=case.forbidden_min_rank,
                active_knowledge_bases=(
                    [case.knowledge_base] if scope is ScopePolicy.OWN_KB else None
                ),
                process_key_assertions=case.process_key_assertions,
            )
            data = response.get("data", {})
            return data if isinstance(data, dict) else {}

        return run_case_under_scope_plan(
            RATIFIED_SCOPE_PLAN, case.target_queries, case.forbidden_queries, _call,
        )
