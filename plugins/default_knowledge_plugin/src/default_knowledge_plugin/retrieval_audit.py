"""Executor for ``service_interface::knowledge_service::audit_retrieval_corpus``.

Corpus-wide retrieval audit. Where ``retrieval_test`` evaluates ONE article,
this walker discovers every ``*.retrieval_test.yaml`` companion file in the
corpus, runs the per-article retrieval test against each, and aggregates the
findings into a single report (per ``workbench/
2026-05-26_coordinator_workflow_strategy.md`` §3.2).

Pure executor logic, decoupled from ``DefaultKnowledgePlugin`` so the plugin
method stays a thin wrapper and the aggregation can be smoked with a stubbed
per-article runner. The plugin injects the real ``run_single`` callable, which
delegates to ``DefaultKnowledgePlugin.test_retrieval`` (W2-D2 option (a): a
plain in-process method call, not a queue dispatch).

Finding semantics mirror ``retrieval_test`` exactly, reconstructed from the
structured per-article report rather than by parsing its human-readable
``failures`` strings, where rank is 1-indexed and smaller is better:

- DRIFT: a target query left the article absent or ranked worse than the
  article's ``min_rank``.
- OVERREACH: a forbidden query ranked the article better than its
  ``forbidden_min_rank``.
- STALE_PROCESS_KEY: an asserted process key no longer resolves in the
  live registry.
- LEGACY_KEY: the companion declares a retired key (``RETIRED_KEYS``, e.g.
  ``non_target_queries``) that nothing in this tolerant path reads -- its
  forbidden set is coerced to empty rather than raising, so this finding is
  what stops the companion from reporting PASSING with its overreach claims
  silently unenforced.

Scope semantics (``ScopePolicy`` / ``ScopePlan`` / ``RATIFIED_SCOPE_PLAN``) and
the retired-key vocabulary (``RETIRED_KEYS``) are defined here and imported by
``tools/run_retrieval_companions.py`` -- one definition of the ratified split,
two consumers that must never disagree about it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

from .retrieval_test import parse_article_path

TEST_FILE_GLOB = "*.retrieval_test.yaml"
TEST_FILE_GLOB_UNDERSCORE = "*_retrieval_test.yaml"
DEFAULT_MIN_RANK = 3
DEFAULT_FORBIDDEN_MIN_RANK = 4

# ``parse_test_file`` reads ``forbidden_queries`` only, so this spelling does not
# raise anywhere in the platform -- it silently yields an empty forbidden set and
# a companion that can no longer fail its overreach claims. Shared with
# ``tools/run_retrieval_companions.py``, which refuses it loud instead of
# tolerating it -- same retired-key vocabulary, two different responses to it.
RETIRED_KEYS: dict[str, str] = {
    "non_target_queries": "forbidden_queries",
}


class ScopePolicy(StrEnum):
    """Which knowledge bases a single check's search may draw results from.

    ``OWN_KB`` restricts the search to the article's own knowledge base;
    ``ALL_ACTIVE`` searches every active one. The choice is load-bearing in both
    directions, measured 2026-08-14 against
    ``24_operator_communication/06_fleet_launcher_session_configuration``:

    - Under ``ALL_ACTIVE``, three of its target queries rank 2 behind
      ``github_midwife_plugin/01_hydration_runbook``, which legitimately owns
      that vocabulary -- reported as DRIFT, though deferring to another KB's
      owner is correct behaviour, not drift.
    - Under ``OWN_KB``, all eleven targets rank 1, but two forbidden queries then
      rank 1 as well and report OVERREACH -- because the owners they must not
      outrank live in OTHER knowledge bases, and scoping the search removed them
      from the result set entirely. The check cannot mean anything with the
      competitor filtered out.

    Neither uniform policy is correct for both check kinds, which is why the plan
    is per-check-kind and why it is declared rather than defaulted.
    """

    OWN_KB = "own_kb"
    ALL_ACTIVE = "all_active"


@dataclass(frozen=True, slots=True)
class ScopePlan:
    """The scope policy to use for each kind of check."""

    target_scope: ScopePolicy
    forbidden_scope: ScopePolicy

    @property
    def needs_two_calls(self) -> bool:
        """Whether target and forbidden checks must be issued as separate calls.

        ``test_retrieval`` takes ONE ``active_knowledge_bases`` per invocation, so
        a plan whose two kinds disagree costs two calls per companion.
        """
        return self.target_scope is not self.forbidden_scope


# The ratified semantics (coordinator ruling, 2026-08-14), and the only plan either
# consumer of this module executes. Deliberately a module constant rather than
# a flag or a per-yaml key: a scope selector in the hands of the author being
# measured is a way to make a red go away without changing the article.
RATIFIED_SCOPE_PLAN: Final[ScopePlan] = ScopePlan(
    target_scope=ScopePolicy.OWN_KB,
    forbidden_scope=ScopePolicy.ALL_ACTIVE,
)

# (scope, target_queries, forbidden_queries) -> one test_retrieval-shaped report
# payload (the ``data`` dict test_retrieval returns). Abstracts over HOW the call
# is made -- in-process for the audit path, a subprocess solet call for the
# runner tool -- so the merge logic below serves both.
CaseCallFn = Callable[[ScopePolicy, list[str], list[str]], dict[str, Any]]

# (case) -> a RetrievalTestReport dict (the ``data`` payload of test_retrieval).
RunSingleFn = Callable[["AuditCase"], dict[str, Any]]


def run_case_under_scope_plan(
    plan: ScopePlan,
    target_queries: list[str],
    forbidden_queries: list[str],
    call: CaseCallFn,
) -> dict[str, Any]:
    """Execute one case's target/forbidden claims under ``plan``, merged into one payload.

    Issues ONE scoped call when ``plan`` uses the same scope for both kinds, TWO
    when it does not. ``process_key_freshness`` always comes from the call that
    carried the target queries -- never double-counted across both calls.
    """
    target_data = call(
        plan.target_scope,
        target_queries,
        [] if plan.needs_two_calls else forbidden_queries,
    )
    if not plan.needs_two_calls:
        return target_data
    forbidden_data = call(plan.forbidden_scope, [], forbidden_queries)
    return {**target_data, "forbidden_results": forbidden_data.get("forbidden_results", [])}


@dataclass(frozen=True)
class AuditCase:
    """One article's retrieval-test parameters parsed from a companion yaml."""

    article_path: str
    target_queries: list[str]
    forbidden_queries: list[str]
    min_rank: int
    forbidden_min_rank: int
    process_key_assertions: list[str]
    legacy_keys: tuple[str, ...]
    source_file: str

    @property
    def knowledge_base(self) -> str:
        """Knowledge-base name the article belongs to (``article_path`` part 1)."""
        return parse_article_path(self.article_path).knowledge_base


@dataclass(frozen=True)
class AuditAggregate:
    """Aggregated outcome across every audited article."""

    total_articles_audited: int
    passed: int
    failed: int
    drifts: list[dict[str, Any]] = field(default_factory=list)
    overreaches: list[dict[str, Any]] = field(default_factory=list)
    stale_keys: list[dict[str, Any]] = field(default_factory=list)
    legacy_keys: list[dict[str, Any]] = field(default_factory=list)
    passing_articles: list[str] = field(default_factory=list)


def discover_test_files(corpus_root: Path) -> list[Path]:
    """Return every retrieval-test companion file under ``corpus_root``, path-sorted.

    Matches both the dot-separated glob (``*.retrieval_test.yaml``) and the
    underscore-separated variant three ``ananta_platform`` fixtures use
    (``*_retrieval_test.yaml``). ``recurse_symlinks=True`` is required because
    ``knowledge_bases/`` is a symlink-aggregation directory — the real content
    lives at ``ananta/knowledge_bases/ananta_platform/`` and each plugin's own
    ``knowledge_base/``, and ``Path.rglob`` does not traverse symlinked
    directories by default.
    """
    found = {
        path
        for glob in (TEST_FILE_GLOB, TEST_FILE_GLOB_UNDERSCORE)
        for path in corpus_root.rglob(glob, recurse_symlinks=True)
    }
    return sorted(found)


def _as_str_list(value: Any) -> list[str]:
    """Coerce a yaml scalar/sequence into a list of strings; [] when absent."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def parse_test_file(path: Path) -> AuditCase:
    """Parse one ``*.retrieval_test.yaml`` into an ``AuditCase``.

    Raises ``ValueError`` when the file is not a mapping or omits the
    required ``article_path`` field. A retired key (``RETIRED_KEYS``) is NOT
    raised here -- the audit path stays tolerant, coercing it to an empty
    forbidden set exactly as before -- but it is recorded on ``legacy_keys`` so
    the caller can surface it as a finding instead of losing it silently.
    """
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Retrieval-test file is not a mapping: {path}")
    article_path = loaded.get("article_path")
    if not isinstance(article_path, str) or not article_path.strip():
        raise ValueError(f"Retrieval-test file missing 'article_path': {path}")
    return AuditCase(
        article_path=article_path.strip(),
        target_queries=_as_str_list(loaded.get("target_queries")),
        forbidden_queries=_as_str_list(loaded.get("forbidden_queries")),
        min_rank=int(loaded.get("min_rank", DEFAULT_MIN_RANK)),
        forbidden_min_rank=int(
            loaded.get("forbidden_min_rank", DEFAULT_FORBIDDEN_MIN_RANK)
        ),
        process_key_assertions=_as_str_list(loaded.get("process_key_assertions")),
        legacy_keys=tuple(sorted(key for key in RETIRED_KEYS if key in loaded)),
        source_file=str(path),
    )


def filter_cases_to_active(
    cases: list[AuditCase],
    active_names: list[str],
    requested: list[str] | None,
) -> list[AuditCase]:
    """Restrict cases to active knowledge bases.

    ``requested`` None audits every active KB's articles. A supplied subset
    must be a subset of the active set; otherwise ``ValueError`` is raised,
    mirroring ``retrieval_test.build_search_fn``.
    """
    active_set = set(active_names)
    if requested is None:
        allowed = active_set
    else:
        missing = sorted(kb for kb in requested if kb not in active_set)
        if missing:
            raise ValueError(f"Requested knowledge bases are not active: {missing}")
        allowed = set(requested)
    return [case for case in cases if case.knowledge_base in allowed]


def _classify_drifts(
    case: AuditCase, target_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reconstruct DRIFT findings from a case's target-query observations."""
    drifts: list[dict[str, Any]] = []
    for observation in target_results:
        rank = observation.get("observed_rank")
        if rank is None or int(rank) > case.min_rank:
            drifts.append({
                "article_path": case.article_path,
                "query": observation.get("query"),
                "observed_rank": rank,
                "min_rank": case.min_rank,
            })
    return drifts


def _classify_overreaches(
    case: AuditCase, forbidden_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reconstruct OVERREACH findings from a case's forbidden-query observations."""
    overreaches: list[dict[str, Any]] = []
    for observation in forbidden_results:
        rank = observation.get("observed_rank")
        if rank is not None and int(rank) < case.forbidden_min_rank:
            overreaches.append({
                "article_path": case.article_path,
                "query": observation.get("query"),
                "observed_rank": rank,
                "forbidden_min_rank": case.forbidden_min_rank,
            })
    return overreaches


def _classify_stale(
    case: AuditCase, freshness: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reconstruct STALE_PROCESS_KEY findings from a case's freshness records."""
    return [
        {"article_path": case.article_path, "process_key": record.get("referenced_key")}
        for record in freshness
        if not record.get("exists_in_registry", False)
    ]


def _classify_legacy_keys(case: AuditCase) -> list[dict[str, Any]]:
    """Reconstruct LEGACY_KEY findings from a case's retired-key declarations.

    Independent of the live report: a legacy key is a shape defect visible from
    the companion yaml alone, so this reads ``case.legacy_keys`` rather than any
    field of ``run_single``'s return.
    """
    return [
        {
            "article_path": case.article_path,
            "legacy_key": key,
            "replacement": RETIRED_KEYS[key],
        }
        for key in case.legacy_keys
    ]


def aggregate_audit(
    *, cases: list[AuditCase], run_single: RunSingleFn, fail_fast: bool,
) -> AuditAggregate:
    """Run each case through ``run_single`` and aggregate the findings.

    With ``fail_fast`` the walk stops after the first article that produces
    any finding; that article's findings are still included. A LEGACY_KEY
    finding fails its article exactly like DRIFT/OVERREACH/STALE_PROCESS_KEY --
    a companion still declaring a retired key never reports PASSING, even when
    every live query it can still assert passes.
    """
    drifts: list[dict[str, Any]] = []
    overreaches: list[dict[str, Any]] = []
    stale_keys: list[dict[str, Any]] = []
    legacy_keys: list[dict[str, Any]] = []
    passing_articles: list[str] = []
    passed = 0
    failed = 0
    for case in cases:
        report = run_single(case)
        case_drifts = _classify_drifts(case, report.get("target_results", []))
        case_over = _classify_overreaches(case, report.get("forbidden_results", []))
        case_stale = _classify_stale(case, report.get("process_key_freshness", []))
        case_legacy = _classify_legacy_keys(case)
        if case_drifts or case_over or case_stale or case_legacy:
            failed += 1
            drifts.extend(case_drifts)
            overreaches.extend(case_over)
            stale_keys.extend(case_stale)
            legacy_keys.extend(case_legacy)
            if fail_fast:
                break
        else:
            passed += 1
            passing_articles.append(case.article_path)
    return AuditAggregate(
        total_articles_audited=passed + failed,
        passed=passed,
        failed=failed,
        drifts=drifts,
        overreaches=overreaches,
        stale_keys=stale_keys,
        legacy_keys=legacy_keys,
        passing_articles=passing_articles,
    )


def _render_drift_lines(drifts: list[dict[str, Any]]) -> list[str]:
    """Markdown lines for the DRIFT section."""
    lines = ["## DRIFT", ""]
    if not drifts:
        lines.append("None.")
        return lines
    for drift in drifts:
        lines.append(
            f"- `{drift['article_path']}` — query {drift['query']!r} "
            f"ranked {drift['observed_rank']} (expected <= {drift['min_rank']})"
        )
    return lines


def _render_overreach_lines(overreaches: list[dict[str, Any]]) -> list[str]:
    """Markdown lines for the OVERREACH section."""
    lines = ["## OVERREACH", ""]
    if not overreaches:
        lines.append("None.")
        return lines
    for over in overreaches:
        lines.append(
            f"- `{over['article_path']}` — forbidden query {over['query']!r} "
            f"ranked {over['observed_rank']} (expected absent or "
            f">= {over['forbidden_min_rank']})"
        )
    return lines


def _render_stale_lines(stale_keys: list[dict[str, Any]]) -> list[str]:
    """Markdown lines for the STALE_PROCESS_KEY section."""
    lines = ["## STALE_PROCESS_KEY", ""]
    if not stale_keys:
        lines.append("None.")
        return lines
    for stale in stale_keys:
        lines.append(
            f"- `{stale['article_path']}` — `{stale['process_key']}` "
            "not found in process registry"
        )
    return lines


def _render_legacy_lines(legacy_keys: list[dict[str, Any]]) -> list[str]:
    """Markdown lines for the LEGACY_KEY section."""
    lines = ["## LEGACY_KEY", ""]
    if not legacy_keys:
        lines.append("None.")
        return lines
    for entry in legacy_keys:
        lines.append(
            f"- `{entry['article_path']}` — declares retired key "
            f"`{entry['legacy_key']}`; rename to `{entry['replacement']}`"
        )
    return lines


def _render_passing_lines(passing_articles: list[str]) -> list[str]:
    """Markdown lines for the PASSING articles section."""
    lines = ["## PASSING articles", ""]
    if not passing_articles:
        lines.append("None.")
        return lines
    lines.extend(f"- `{article}`" for article in passing_articles)
    return lines


def render_markdown(
    *,
    aggregate: AuditAggregate,
    ran_at: str,
    corpus_root: str,
    report_path: str,
    duration_seconds: float,
    total_fixtures_discovered: int,
) -> str:
    """Render the aggregate as a Markdown audit report.

    ``total_fixtures_discovered`` is reported alongside ``total_articles_audited``
    (the post active-KB-filter count that actually ran) so a partial-corpus run
    is visible in the report itself rather than presented as the whole corpus.
    """
    header = [
        "# Knowledge Base Retrieval Audit",
        "",
        f"- Ran at: {ran_at}",
        f"- Corpus root: `{corpus_root}`",
        f"- Fixtures discovered: {total_fixtures_discovered}",
        f"- Articles audited: {aggregate.total_articles_audited}",
        f"- Passed: {aggregate.passed}",
        f"- Failed: {aggregate.failed}",
        f"- Duration: {duration_seconds:.3f}s",
        f"- Report path: `{report_path}`",
        "",
    ]
    sections = [
        _render_drift_lines(aggregate.drifts),
        _render_overreach_lines(aggregate.overreaches),
        _render_stale_lines(aggregate.stale_keys),
        _render_legacy_lines(aggregate.legacy_keys),
        _render_passing_lines(aggregate.passing_articles),
    ]
    lines = list(header)
    for section in sections:
        lines.extend(section)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
