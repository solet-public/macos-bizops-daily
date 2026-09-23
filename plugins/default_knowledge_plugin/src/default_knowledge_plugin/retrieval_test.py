"""Executor for ``service_interface::knowledge_service::test_retrieval``.

Pure retrieval-evaluation logic, decoupled from ``DefaultKnowledgePlugin`` so
the plugin method stays a thin wrapper and the evaluation can be smoked with
stubbed search + registry callables. The plugin injects the real callables.

Semantics (per ``workbench/2026-05-26_coordinator_workflow_strategy.md`` §2.4,
§3.2), where rank is 1-indexed and smaller is better:

- A target query PASSES when the article ranks at or above ``min_rank``. Absent,
  or ranked worse than ``min_rank`` -> DRIFT failure.
- A forbidden query PASSES when the article is absent or ranks no better than
  ``forbidden_min_rank``. Ranked better than that -> OVERREACH failure.
- A ``process_key`` assertion PASSES when the key resolves in the live registry;
  otherwise it is a STALE_PROCESS_KEY failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .constants import normalize_text_for_tag

# (query, top_k) -> ranked list of search-result dicts, best first.
SearchFn = Callable[[str, int], list[dict[str, Any]]]
# (query, top_k, kb_name | None) -> ranked results for one KB (all when None).
RawSearchFn = Callable[[str, int, str | None], list[dict[str, Any]]]
# (process_key) -> whether the key resolves in the live registry.
RegistryLookupFn = Callable[[str], bool]

KB_ROOT = "knowledge_bases"
DEFAULT_TOP_K_INTERNAL = 20
_TOP_PATHS_LIMIT = 3


@dataclass(frozen=True)
class ArticleLocator:
    """Knowledge-base name + KB-relative path parsed from a repo-relative path."""

    knowledge_base: str
    relative_path: str

    @property
    def display_path(self) -> str:
        """Repo-relative-style ``<kb>/<path>`` rendering for diagnostics."""
        return f"{self.knowledge_base}/{self.relative_path}"


def parse_article_path(article_path: str) -> ArticleLocator:
    """Parse a repo-relative article path into (kb, KB-relative path).

    ``knowledge_bases/`` is a symlink-aggregation directory, so a fixture's
    ``article_path`` may declare either the symlink-relative dialect
    (``knowledge_bases/<kb>/<rest>``) or one of the two real-path dialects the
    symlinks resolve to: ``ananta/knowledge_bases/<kb>/<rest>`` (ananta_platform)
    or ``plugins/<plugin>/knowledge_base/<rest>`` (a plugin's own KB, where the
    plugin directory name IS the KB name). All three normalize to the same
    ``ArticleLocator``.
    """
    parts = article_path.strip("/").split("/")
    if parts[0] == KB_ROOT and len(parts) >= 3:
        return ArticleLocator(knowledge_base=parts[1], relative_path="/".join(parts[2:]))
    if parts[:2] == ["ananta", KB_ROOT] and len(parts) >= 4:
        return ArticleLocator(knowledge_base=parts[2], relative_path="/".join(parts[3:]))
    if parts[0] == "plugins" and len(parts) >= 4 and parts[2] == "knowledge_base":
        return ArticleLocator(knowledge_base=parts[1], relative_path="/".join(parts[3:]))
    raise ValueError(
        "article_path must be repo-relative under "
        f"'{KB_ROOT}/<kb>/<path>', 'ananta/{KB_ROOT}/<kb>/<path>', or "
        f"'plugins/<plugin>/knowledge_base/<path>'; got {article_path!r}"
    )


def _result_path(result: dict[str, Any]) -> str:
    """Render a search result as ``<kb>/<file_path>`` for the top-paths list."""
    return f"{result.get('knowledge_base', '')}/{result.get('file_path', '')}"


def _result_score(result: dict[str, Any]) -> float:
    """Extract a sortable similarity score, defaulting to 0.0 when absent."""
    score = result.get("score")
    return float(score) if isinstance(score, int | float) else 0.0


def build_search_fn(
    raw_search: RawSearchFn,
    active_names: list[str],
    active_knowledge_bases: list[str] | None,
) -> SearchFn:
    """Build the ``(query, top_k) -> results`` closure for ``run_retrieval_test``.

    When ``active_knowledge_bases`` is None, one search spans all active KBs.
    Otherwise every requested KB must be active and results are merged by score
    across per-KB searches, reconstructing the ranking the article would have if
    only that subset were active (the search surface filters one KB per call).
    """
    if active_knowledge_bases is None:
        def search_all(query: str, top_k: int) -> list[dict[str, Any]]:
            return raw_search(query, top_k, None)
        return search_all

    missing = sorted(kb for kb in active_knowledge_bases if kb not in active_names)
    if missing:
        raise ValueError(f"Requested knowledge bases are not active: {missing}")
    names = list(active_knowledge_bases)

    def search_subset(query: str, top_k: int) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for name in names:
            merged.extend(raw_search(query, top_k, name))
        merged.sort(key=_result_score, reverse=True)
        return merged[:top_k]
    return search_subset


def _locate(
    locator: ArticleLocator, results: list[dict[str, Any]],
) -> tuple[int | None, float | None]:
    """Return the 1-indexed rank + score of the target article, or (None, None)."""
    # Search rows carry the indexer's underscore-flat file_path (see
    # constants.normalize_text_for_tag); apply the same normalization to the
    # locator before comparison.
    expected_file_path = normalize_text_for_tag(locator.relative_path)
    for rank, result in enumerate(results, start=1):
        if (
            result.get("knowledge_base") == locator.knowledge_base
            and result.get("file_path") == expected_file_path
        ):
            score = result.get("score")
            return rank, float(score) if isinstance(score, int | float) else None
    return None, None


def _observe(
    locator: ArticleLocator, query: str, results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the per-query observation block shared by target and forbidden checks."""
    rank, score = _locate(locator, results)
    return {
        "query": query,
        "observed_rank": rank,
        "score": score,
        "top_3_paths": [_result_path(r) for r in results[:_TOP_PATHS_LIMIT]],
    }


def _evaluate_target(
    locator: ArticleLocator,
    query: str,
    results: list[dict[str, Any]],
    min_rank: int,
) -> tuple[dict[str, Any], str | None]:
    """Observe a target query; return (observation, DRIFT failure or None)."""
    observation = _observe(locator, query, results)
    rank = observation["observed_rank"]
    if rank is None:
        return observation, (
            f"DRIFT: target query {query!r} did not surface "
            f"{locator.display_path} in top-{len(results)}"
        )
    if rank > min_rank:
        return observation, (
            f"DRIFT: target query {query!r} ranked {locator.display_path} "
            f"at {rank}, expected <= {min_rank}"
        )
    return observation, None


def _evaluate_forbidden(
    locator: ArticleLocator,
    query: str,
    results: list[dict[str, Any]],
    forbidden_min_rank: int,
) -> tuple[dict[str, Any], str | None]:
    """Observe a forbidden query; return (observation, OVERREACH failure or None)."""
    observation = _observe(locator, query, results)
    rank = observation["observed_rank"]
    if rank is not None and rank < forbidden_min_rank:
        return observation, (
            f"OVERREACH: forbidden query {query!r} ranked {locator.display_path} "
            f"at {rank}, expected absent or >= {forbidden_min_rank}"
        )
    return observation, None


def _check_process_keys(
    keys: list[str], registry_has_key: RegistryLookupFn,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve each asserted key; return (freshness records, failure strings)."""
    freshness: list[dict[str, Any]] = []
    failures: list[str] = []
    for key in keys:
        exists = registry_has_key(key)
        freshness.append({"referenced_key": key, "exists_in_registry": exists})
        if not exists:
            failures.append(f"STALE_PROCESS_KEY: {key} not found in process registry")
    return freshness, failures


def run_retrieval_test(
    *,
    article_path: str,
    target_queries: list[str],
    forbidden_queries: list[str],
    min_rank: int,
    forbidden_min_rank: int,
    process_key_assertions: list[str],
    search_fn: SearchFn,
    registry_has_key: RegistryLookupFn,
    top_k_internal: int = DEFAULT_TOP_K_INTERNAL,
) -> dict[str, Any]:
    """Run the full retrieval test, returning a RetrievalTestReport dict."""
    locator = parse_article_path(article_path)
    failures: list[str] = []

    target_results: list[dict[str, Any]] = []
    for query in target_queries:
        observation, failure = _evaluate_target(
            locator, query, search_fn(query, top_k_internal), min_rank,
        )
        target_results.append(observation)
        if failure is not None:
            failures.append(failure)

    forbidden_results: list[dict[str, Any]] = []
    for query in forbidden_queries:
        observation, failure = _evaluate_forbidden(
            locator, query, search_fn(query, top_k_internal), forbidden_min_rank,
        )
        forbidden_results.append(observation)
        if failure is not None:
            failures.append(failure)

    freshness, key_failures = _check_process_keys(
        process_key_assertions, registry_has_key,
    )
    failures.extend(key_failures)

    return {
        "passed": not failures,
        "target_results": target_results,
        "forbidden_results": forbidden_results,
        "process_key_freshness": freshness,
        "failures": failures,
    }
