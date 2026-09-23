"""Real external fetch adapters for the model capability catalog (iss_d136ae29).

Two genuinely independent, live, no-auth-required pages on artificialanalysis.ai
carry the Artificial Analysis Intelligence Index and cost-per-task figures the
catalog's `capability_score` / `cost_per_task_usd` columns are defined against
(`schema.py` column docs): the cross-model leaderboard table
(`/leaderboards/models`) and each model's own detail page
(`/models/<slug>`). Both are server-rendered (verified: the returned HTML
contains a real `<table>` with the reading already present, not a client-side
chart requiring JS execution) — a plain HTTP GET plus a deterministic parse
sees the same numbers a browser would, no headless browser needed.

These two pages are still the same publisher, so they are not evidence of a
*different organization's* measurement — but they are two separately-fetched,
separately-rendered artifacts that can genuinely diverge (a leaderboard
refresh that hasn't yet propagated to a detail page, a transient parse
failure on one but not the other), which is exactly what the reconciler in
`model_capability_reconcile.py` checks for. Vendor pricing docs
(`anthropic_pricing_page`, `openai_model_docs`) are a fast-follow, not
implemented here — they would corroborate cost plausibility and effort-tier
existence, not the Intelligence Index itself (no vendor publishes that
metric; it is Artificial Analysis's own benchmark).

Fetch is separated from parse everywhere so tests exercise the parser against
canned HTML fixtures with zero network access — `fetch_*_html` is the only
function here that touches the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import httpx

SOURCE_LEADERBOARD_HTML: Final[str] = "artificial_analysis_leaderboard_html"
SOURCE_MODEL_DETAIL_HTML: Final[str] = "artificial_analysis_model_detail_html"

_LEADERBOARD_URL: Final[str] = "https://artificialanalysis.ai/leaderboards/models"
_MODEL_DETAIL_URL_TEMPLATE: Final[str] = "https://artificialanalysis.ai/models/{slug}"
_USER_AGENT: Final[str] = "Mozilla/5.0 (compatible; model-capability-refresh/1.0)"
_FETCH_TIMEOUT_SECONDS: Final[float] = 20.0


class FetchError(Exception):
    """A fetch failed. `status` matches the observation table's `fetch_status` enum."""

    def __init__(self, status: str, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Reading:
    """One (provider, runtime, model, effort) x metric observation, not yet written."""

    source_id: str
    provider: str
    runtime: str
    model: str
    effort: str
    metric: str
    value_number: float | None
    value_text: str | None
    raw_excerpt: str
    fetch_status: str
    fetched_at: str


@dataclass(frozen=True, slots=True)
class _RosterEntry:
    """How one catalog (provider, runtime, model) maps onto Artificial Analysis's
    own display name and detail-page slug. Hand-verified against the live site
    2026-09-21 (workbench/2026-09-21_dispatch_model_capability_crosscheck_refresh_run.md's
    fast-follow); slugs re-verified live 2026-09-22, when the max-effort and
    "with fallback" slug rules were corrected (iss_b12b467e) and the three
    models released that day were added (iss_01506235)."""

    provider: str
    runtime: str
    model: str
    aa_display_name: str
    aa_slug_base: str
    efforts: tuple[str, ...]


# Bounded to the models the fleet actually dispatches -- not a general-purpose
# "every model AA lists" scraper. Adding a model here is a deliberate roster
# change.
# A model not on this roster can still be put in the catalog directly with
# record_model_capability_cell (operator ruling rul_9e7a67ba); the roster
# only decides which models the refresh keeps fresh automatically.
_ROSTER: Final[tuple[_RosterEntry, ...]] = (
    _RosterEntry("anthropic", "claude_code", "claude-haiku-4.5", "Claude 4.5 Haiku", "claude-4-5-haiku", ("non_reasoning",)),
    _RosterEntry("anthropic", "claude_code", "claude-sonnet-5", "Claude Sonnet 5", "claude-sonnet-5", ("low", "medium", "high", "xhigh", "max")),
    _RosterEntry("anthropic", "claude_code", "claude-opus-5", "Claude Opus 5", "claude-opus-5", ("low", "medium", "high", "xhigh", "max")),
    _RosterEntry("anthropic", "claude_code", "claude-opus-5-5", "Claude Opus 5.5", "claude-opus-5-5", ("low", "medium", "high", "xhigh", "max")),
    _RosterEntry("anthropic", "claude_code", "claude-fable-5-1", "Claude Fable 5.1", "claude-fable-5-1", ("low", "medium", "high", "xhigh", "max")),
    _RosterEntry("openai", "codex", "gpt-5.6-sol", "GPT-5.6 Sol", "gpt-5-6-sol", ("low", "medium", "high", "xhigh", "max")),
    _RosterEntry("openai", "codex", "gpt-5.6-terra", "GPT-5.6 Terra", "gpt-5-6-terra", ("low", "medium", "high", "xhigh", "max")),
    _RosterEntry("openai", "codex", "gpt-5.6-luna", "GPT-5.6 Luna", "gpt-5-6-luna", ("low", "medium", "high", "xhigh", "max")),
    _RosterEntry("openai", "codex", "gpt-6-astra", "GPT-6 Astra", "gpt-6-astra", ("low", "medium", "high", "xhigh", "max")),
    _RosterEntry("openai", "codex", "gpt-6-sol", "GPT-6 Sol", "gpt-6-sol", ("low", "medium", "high", "xhigh", "max")),
    _RosterEntry("openai", "codex", "gpt-6-luna", "GPT-6 Luna", "gpt-6-luna", ("low", "medium", "high", "xhigh", "max")),
)


def roster_cells() -> tuple[tuple[str, str, str], ...]:
    """Every (runtime, model, effort) this refresh run will attempt to observe."""
    return tuple((e.runtime, e.model, effort) for e in _ROSTER for effort in e.efforts)


# Matches one leaderboard row's flattened-text shape (see module docstring):
# "{Display Name}|{Context Window}|{Creator}|{Intelligence Index}|${Cost}|..."
# repeated once per row, terminated by the row's own "Model|Providers" action
# cell. Verified 2026-09-21 against a live fetch: every seed-table cell's
# (capability_score, cost_per_task_usd) pair round-tripped through this exact
# pattern with zero mismatches.
_ROW_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"([A-Za-z0-9][^|]*?)\|(?:\d+[kKmM]|--)\|([A-Za-z][^|]*?)\|(\d+|--)\|(\$[\d.]+|--|\*)\|"
    r"[^|]*\|[^|]*\|[^|]*\|Model\|Providers\|",
)

_EFFORT_SUFFIX: Final[re.Pattern[str]] = re.compile(r"\(([a-z]+)(?: with fallback)?\)\s*$")


_SCRIPT_OR_STYLE: Final[re.Pattern[str]] = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _strip_tags(html: str) -> str:
    """Tag-strip to a `|`-delimited flat text. Bundled `<script>`/`<style>`
    content is dropped bodily first -- these pages ship megabytes of JS
    around a few KB of real table data, and running the row/field regexes
    over that JS text (rather than just past its tags) is slow enough to
    look like a hang."""
    without_scripts = _SCRIPT_OR_STYLE.sub("", html)
    text = re.sub(r"<[^>]+>", "|", without_scripts)
    return re.sub(r"\|+", "|", text)


def _table_region(html: str) -> str:
    """The first `<table>...</table>` block only. The leaderboard page ships
    several megabytes of unrelated bundled JS in `<script>` tags around the
    one real data table; running the row regex over the whole document makes
    the engine attempt a match at every letter in that JS, which is slow
    enough to look like a hang. Scoping to the table first keeps the parse
    to the ~1MB of actual row content and is linear."""
    start = html.find("<table")
    if start == -1:
        return ""
    end = html.find("</table>", start)
    return html[start : end + len("</table>")] if end != -1 else html[start:]


def _parse_effort(display_name: str, roster: _RosterEntry) -> str | None:
    """Extract the effort tag from an AA display name against one roster entry's
    known base name; None when this row isn't this entry at all."""
    base = roster.aa_display_name
    if not display_name.startswith(base):
        return None
    remainder = display_name[len(base):].strip()
    if not remainder:
        return "non_reasoning" if "non_reasoning" in roster.efforts else None
    match = _EFFORT_SUFFIX.match(remainder)
    if not match:
        return None
    effort = match.group(1)
    return effort if effort in roster.efforts else None


def parse_leaderboard_html(html: str, *, fetched_at: str) -> list[Reading]:
    """Deterministic parse: every roster (provider, runtime, model, effort) this
    page's table actually lists becomes 2 Readings (intelligence_index,
    cost_per_task_usd where priced). A roster cell this fetch never finds a
    row for is simply absent from the result -- the reconciler treats an
    absent reading as `not_listed`, not a silent zero."""
    text = _strip_tags(_table_region(html))
    readings: list[Reading] = []
    for match in _ROW_PATTERN.finditer(text):
        display_name, creator, score_raw, cost_raw = match.groups()
        display_name = display_name.strip()
        creator = creator.strip().lower()
        for roster in _ROSTER:
            if roster.provider != creator:
                continue
            effort = _parse_effort(display_name, roster)
            if effort is None:
                continue
            excerpt = match.group(0)[:512]
            if score_raw != "--":
                readings.append(
                    Reading(
                        source_id=SOURCE_LEADERBOARD_HTML, provider=roster.provider, runtime=roster.runtime,
                        model=roster.model, effort=effort, metric="intelligence_index",
                        value_number=float(score_raw), value_text=None, raw_excerpt=excerpt, fetch_status="ok",
                        fetched_at=fetched_at,
                    ),
                )
            if cost_raw not in ("--", "*"):
                readings.append(
                    Reading(
                        source_id=SOURCE_LEADERBOARD_HTML, provider=roster.provider, runtime=roster.runtime,
                        model=roster.model, effort=effort, metric="cost_per_task_usd",
                        value_number=float(cost_raw.lstrip("$")), value_text=None, raw_excerpt=excerpt, fetch_status="ok",
                        fetched_at=fetched_at,
                    ),
                )
            break
    return readings


_DETAIL_SCORE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"Intelligence\|Updated\|#\|\d+\| / \|\d+\|(\d+)\|Artificial Analysis Intellig",
)
_DETAIL_COST_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\$[\d.]+)\|Cost per Intelligence Index [Tt]ask")


def parse_model_detail_html(html: str, *, roster: _RosterEntry, effort: str, fetched_at: str) -> list[Reading]:
    """One model+effort's own detail page: same two metrics, independently
    rendered. Missing/unparseable fields come back as `parse_error` readings
    rather than being silently dropped, so a reconciler run can see the
    difference between "this source disagreed" and "this source's page
    changed shape and the parser needs updating."""
    text = _strip_tags(html)
    excerpt = text[:512]
    readings: list[Reading] = []
    score_match = _DETAIL_SCORE_PATTERN.search(text)
    if score_match:
        readings.append(
            Reading(
                source_id=SOURCE_MODEL_DETAIL_HTML, provider=roster.provider, runtime=roster.runtime,
                model=roster.model, effort=effort, metric="intelligence_index",
                value_number=float(score_match.group(1)), value_text=None, raw_excerpt=excerpt, fetch_status="ok",
                fetched_at=fetched_at,
            ),
        )
    else:
        readings.append(
            Reading(
                source_id=SOURCE_MODEL_DETAIL_HTML, provider=roster.provider, runtime=roster.runtime,
                model=roster.model, effort=effort, metric="intelligence_index",
                value_number=None, value_text=None, raw_excerpt=excerpt, fetch_status="parse_error",
                fetched_at=fetched_at,
            ),
        )
    cost_match = _DETAIL_COST_PATTERN.search(text)
    if cost_match:
        readings.append(
            Reading(
                source_id=SOURCE_MODEL_DETAIL_HTML, provider=roster.provider, runtime=roster.runtime,
                model=roster.model, effort=effort, metric="cost_per_task_usd",
                value_number=float(cost_match.group(1).lstrip("$")), value_text=None, raw_excerpt=excerpt, fetch_status="ok",
                fetched_at=fetched_at,
            ),
        )
    return readings


def _detail_slug(roster: _RosterEntry, effort: str) -> str:
    """Artificial Analysis detail-page slug for one roster cell.

    Verified live 2026-09-22 (iss_b12b467e): the max-effort page is the BARE
    model slug (``/models/gpt-6-sol`` is titled "GPT-6 Sol (max)"; ``-max``
    404s for every model), and "with fallback" appears only in page titles,
    never in a slug (``/models/claude-fable-5-1-high`` is titled "Claude Fable
    5.1 (high with fallback)"; ``...-with-fallback`` 404s).
    """
    if effort in ("non_reasoning", "max"):
        return roster.aa_slug_base
    return f"{roster.aa_slug_base}-{effort}"


def fetch_leaderboard_html(client: httpx.Client) -> str:
    try:
        response = client.get(_LEADERBOARD_URL, headers={"User-Agent": _USER_AGENT}, timeout=_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise FetchError("http_error", f"{_LEADERBOARD_URL}: HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise FetchError("http_error", f"{_LEADERBOARD_URL}: {exc}") from exc
    return response.text


def fetch_model_detail_html(client: httpx.Client, roster: _RosterEntry, effort: str) -> str:
    url = _MODEL_DETAIL_URL_TEMPLATE.format(slug=_detail_slug(roster, effort))
    try:
        response = client.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise FetchError("http_error", f"{url}: HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise FetchError("http_error", f"{url}: {exc}") from exc
    return response.text


def fetch_all_readings(client: httpx.Client) -> tuple[list[Reading], list[tuple[str, str]]]:
    """Every reading this run can gather, plus (source_id, reason) failures.
    A per-model-detail failure never aborts the whole run -- one bad slug
    (an AA URL naming convention this roster guessed wrong) must not blank
    out every other model's leaderboard-sourced reading."""
    now = datetime.now(UTC).isoformat()
    readings: list[Reading] = []
    failures: list[tuple[str, str]] = []
    try:
        leaderboard_html = fetch_leaderboard_html(client)
        readings.extend(parse_leaderboard_html(leaderboard_html, fetched_at=now))
    except FetchError as exc:
        failures.append((SOURCE_LEADERBOARD_HTML, f"{exc.status}: {exc.message}"))
    for roster in _ROSTER:
        for effort in roster.efforts:
            try:
                detail_html = fetch_model_detail_html(client, roster, effort)
                readings.extend(parse_model_detail_html(detail_html, roster=roster, effort=effort, fetched_at=now))
            except FetchError as exc:
                failures.append((f"{SOURCE_MODEL_DETAIL_HTML}:{roster.model}@{effort}", f"{exc.status}: {exc.message}"))
    return readings, failures


__all__ = [
    "SOURCE_LEADERBOARD_HTML",
    "SOURCE_MODEL_DETAIL_HTML",
    "FetchError",
    "Reading",
    "fetch_all_readings",
    "fetch_leaderboard_html",
    "fetch_model_detail_html",
    "parse_leaderboard_html",
    "parse_model_detail_html",
    "roster_cells",
]
