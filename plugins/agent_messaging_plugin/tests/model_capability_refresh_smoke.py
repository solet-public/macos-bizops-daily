#!/usr/bin/env python3
"""End-to-end smoke for `refresh_model_capability_catalog` (iss_d136ae29):
verb -> fetch (mocked transport, real-shaped HTML fixtures) -> parse ->
reconcile -> state write -> `select_dispatch_tier` can serve the result.

No live network: `httpx.MockTransport` answers every request from fixture
HTML built from real numbers captured in a live spot-check 2026-09-21 (see
model_capability_fetch.py's module docstring), not fabricated. This proves
the FULL pipeline wires together correctly; `model_capability_reconcile_smoke.py`
covers the reconciler's decision logic exhaustively in isolation, and
`model_capability_fetch.py`'s parser was separately verified against the real
saved live pages (not part of this offline smoke).

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/model_capability_refresh_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import model_capability_fetch as fetch  # noqa: E402
from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin.model_capability_store import read_cells  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_CROSSCHECK_CONFLICT,
    CELL_ACCEPTANCE_PENDING_CROSSCHECK,
    TABLE_MODEL_CAPABILITY_REFRESH_RUN,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_passed = 0
_failed: list[str] = []
_NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)  # on or after the newest usage_economics effective_at

# Real numbers, captured live 2026-09-21 (see model_capability_fetch.py's
# module docstring for the corroborating manual spot-check).
_LEADERBOARD_FIXTURE = """<html><body><table><tbody>
<tr><td>Claude Sonnet 5 (max)</td><td>1M</td><td>Anthropic</td><td>38</td><td>$5.09</td><td>56</td><td>49.25</td><td>58.11</td><td>Model</td><td>Providers</td></tr>
<tr><td>Claude Opus 5 (low)</td><td>1M</td><td>Anthropic</td><td>39</td><td>$1.10</td><td>60</td><td>2.76</td><td>11.10</td><td>Model</td><td>Providers</td></tr>
<tr><td>GPT-6 Astra (max)</td><td>1M</td><td>OpenAI</td><td>53</td><td>$3.26</td><td>67</td><td>259.33</td><td>266.83</td><td>Model</td><td>Providers</td></tr>
<tr><td>Claude Fable 5.1 (max with fallback)</td><td>1M</td><td>Anthropic</td><td>53</td><td>$7.63</td><td>66</td><td>298.45</td><td>306.04</td><td>Model</td><td>Providers</td></tr>
</tbody></table></body></html>"""


def _detail_fixture(score: str, cost: str) -> str:
    return (
        "<html><body>Model summary|Intelligence|Updated|#|45| / |202|" + score
        + "|Artificial Analysis Intelligence Index|blah|" + cost + "|Cost per Intelligence Index task|blah</body></html>"
    ).replace("|", "<span></span>")  # tag-strip collapses these right back to '|' delimiters


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/leaderboards/models"):
        return httpx.Response(200, text=_LEADERBOARD_FIXTURE)
    # Exact slugs (iss_b12b467e): a max-effort page is the BARE model slug.
    if url.endswith("/models/gpt-6-astra"):
        return httpx.Response(200, text=_detail_fixture("53", "$3.26"))  # agrees with leaderboard -> accepted
    if url.endswith("/models/claude-opus-5-low"):
        return httpx.Response(200, text=_detail_fixture("50", "$1.10"))  # 11pt spread, past tolerance -> conflict
    if url.endswith("/models/claude-sonnet-5"):
        return httpx.Response(404, text="not found")  # fetch failure -> only 1 source -> pending
    return httpx.Response(200, text="<html><body>no data here</body></html>")  # parse_error -> contributes nothing


def _typed(fake: RealShapeState) -> StateManagementInterface:
    return cast("StateManagementInterface", fake)


def _run_refresh_fixture(state: RealShapeState) -> dict[str, Any]:
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    try:
        return verbs.refresh_model_capability_catalog(_typed(state), trigger="manual", client=client)
    finally:
        client.close()


def _check_run_level_reporting(result: dict[str, Any]) -> None:
    _check(result["trigger"] == "manual", "run records its own trigger")
    _check("artificial_analysis_leaderboard_html" in result["sources_ok"], "leaderboard source reported ok")
    _check(any("claude-sonnet-5" in f for f in result["sources_failed"]), "the 404'd detail fetch is named in sources_failed, not silently dropped")


def _find_cell(cells: list[dict[str, Any]], model: str, effort: str) -> dict[str, Any]:
    return next(c for c in cells if c["model"] == model and c["effort"] == effort)


def _check_astra_accepted(cells: list[dict[str, Any]]) -> None:
    astra_max = _find_cell(cells, "gpt-6-astra", "max")
    _check(astra_max["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "gpt-6-astra max: 2 agreeing sources -> accepted")
    _check(astra_max["capability_score"] == 53.0, "accepted score is the agreed value")
    _check(len(astra_max["agreeing_observation_ids"]) == 4, "agreeing_observation_ids names all 4 corroborating readings (score x2 + cost x2)")


def _check_opus_conflict(cells: list[dict[str, Any]]) -> None:
    opus_low = _find_cell(cells, "claude-opus-5", "low")
    _check(opus_low["acceptance"] == CELL_ACCEPTANCE_CROSSCHECK_CONFLICT, "opus-5 low: 39 vs 50 exceeds tolerance -> crosscheck_conflict")
    disagreement_note = opus_low["disagreement_note"]
    names_metric = isinstance(disagreement_note, str) and "intelligence_index" in disagreement_note
    _check(names_metric, "conflict note names the disagreeing metric")


def _check_sonnet_and_fable_pending(cells: list[dict[str, Any]]) -> None:
    sonnet_max = _find_cell(cells, "claude-sonnet-5", "max")
    fable_max = _find_cell(cells, "claude-fable-5-1", "max")
    _check(sonnet_max["acceptance"] == CELL_ACCEPTANCE_PENDING_CROSSCHECK, "sonnet-5 max: detail fetch 404'd, only 1 source -> pending_crosscheck")
    _check(fable_max["acceptance"] == CELL_ACCEPTANCE_PENDING_CROSSCHECK, "fable-5.1 max: generic handler gives no parseable detail data -> pending")


def _check_per_cell_reconciliation(state: RealShapeState) -> None:
    cells = read_cells(_typed(state))
    _check_astra_accepted(cells)
    _check_opus_conflict(cells)
    _check_sonnet_and_fable_pending(cells)


def _check_run_row_counts(state: RealShapeState) -> None:
    runs = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN)
    run_row = next(r for r in runs if r["trigger"] == "manual")
    _check(run_row["cells_accepted"] == 1, f"run row's cells_accepted matches (got {run_row['cells_accepted']})")
    _check(run_row["cells_conflicted"] == 1, f"run row's cells_conflicted matches (got {run_row['cells_conflicted']})")
    _check(any("claude-sonnet-5" in f for f in run_row["sources_failed"]), "run row's own sources_failed column carries the failure, not just the verb's return value")


def test_refresh_accepts_conflicts_and_pends_correctly() -> None:
    state = RealShapeState()
    result = _run_refresh_fixture(state)
    _check_run_level_reporting(result)
    _check_per_cell_reconciliation(state)
    _check_run_row_counts(state)


def test_accepted_cell_is_immediately_servable() -> None:
    """Closes the loop this whole fix exists for: a real refresh run's
    accepted cell is exactly what unblocks `select_dispatch_tier`."""
    state = RealShapeState()
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    try:
        verbs.refresh_model_capability_catalog(_typed(state), trigger="manual", client=client)
    finally:
        client.close()
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 30, "billing_objective": "metered_usd"}, now=_NOW)
    _check(picked["selected"]["model"] == "gpt-6-astra" and picked["selected"]["effort"] == "max", "the one real-refresh-accepted cell is exactly what the selector now serves")


def test_second_refresh_never_downgrades_a_prior_acceptance_on_thin_evidence() -> None:
    state = RealShapeState()
    client1 = httpx.Client(transport=httpx.MockTransport(_handler))
    try:
        verbs.refresh_model_capability_catalog(_typed(state), trigger="manual", client=client1)
    finally:
        client1.close()

    def _thin_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/leaderboards/models"):
            return httpx.Response(200, text=_LEADERBOARD_FIXTURE)  # gpt-6-astra max still listed, 1 source only this time
        return httpx.Response(404, text="not found")  # every detail page now fails

    client2 = httpx.Client(transport=httpx.MockTransport(_thin_handler))
    try:
        result = verbs.refresh_model_capability_catalog(_typed(state), trigger="manual", client=client2)
    finally:
        client2.close()

    cells = read_cells(_typed(state))
    astra_max = next(c for c in cells if c["model"] == "gpt-6-astra" and c["effort"] == "max")
    _check(astra_max["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "a thin-evidence rerun never downgrades a cell already accepted by a prior run")
    _check(result["cells_unchanged"] >= 1, "the preserved cell is counted as unchanged, not silently dropped from the report")


def test_detail_slugs_and_roster() -> None:
    """iss_b12b467e / iss_01506235, verified live 2026-09-22: max is the bare
    slug, "with fallback" is never part of a slug, and the three models
    released that day are on the roster."""
    roster = {entry.model: entry for entry in fetch._ROSTER}  # noqa: SLF001 -- pins the private URL rule the refresh depends on
    _check(fetch._detail_slug(roster["gpt-6-sol"], "max") == "gpt-6-sol", "max effort uses the bare slug")  # noqa: SLF001
    _check(fetch._detail_slug(roster["gpt-6-sol"], "high") == "gpt-6-sol-high", "other efforts append the effort")  # noqa: SLF001
    _check(fetch._detail_slug(roster["claude-fable-5-1"], "high") == "claude-fable-5-1-high", "fable slugs carry no -with-fallback suffix")  # noqa: SLF001
    _check(fetch._detail_slug(roster["claude-haiku-4.5"], "non_reasoning") == "claude-4-5-haiku", "non_reasoning uses the bare slug")  # noqa: SLF001
    _check({"gpt-6-sol", "gpt-6-luna", "claude-opus-5-5"} <= set(roster), "GPT-6 Sol, GPT-6 Luna and Claude Opus 5.5 are on the roster")
    opus_55 = fetch.parse_leaderboard_html(
        _LEADERBOARD_FIXTURE.replace("Claude Opus 5 (low)", "Claude Opus 5.5 (low with fallback)"), fetched_at="2026-09-22T00:00:00+00:00",
    )
    models = {(reading.model, reading.effort) for reading in opus_55}
    _check(("claude-opus-5-5", "low") in models and ("claude-opus-5", "low") not in models, "a Claude Opus 5.5 row is never read as Claude Opus 5")


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def main() -> int:
    print("model_capability_refresh_smoke")
    test_detail_slugs_and_roster()
    test_refresh_accepts_conflicts_and_pends_correctly()
    test_accepted_cell_is_immediately_servable()
    test_second_refresh_never_downgrades_a_prior_acceptance_on_thin_evidence()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
