#!/usr/bin/env python3
"""Proves allowance-pool readings are optional and only a known exhausted pool excludes Claude (iss_c6fbf9ae, iss_3ff06701).

Runs against the SHIPPED usage_economics profile, unmodified: its Claude
subscription pools are all unknown, and an unknown quota never excludes a cell,
so with no reading a claude_code cell is selectable. A current reading showing
a pool used up excludes the cells it covers, even beside an unknown pool; an
expired or retracted reading excludes nothing, and a reading is never inferred
from another pool's.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin.dispatch_tier_selection import TierSelectionError  # noqa: E402
from agent_messaging_plugin.model_capability_store import CatalogError, load_seed_table, seed_catalog  # noqa: E402
from agent_messaging_plugin.schema import CELL_ACCEPTANCE_ACCEPTED, TABLE_ALLOWANCE_POOL_READING, TABLE_MODEL_CAPABILITY_CELL  # noqa: E402

_passed = 0
_failed: list[str] = []
_NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
_PROFILE = "anthropic-max-20x-2026-08-22"
_ALL_POOLS = ("rolling-five-hour", "weekly-all-model", "weekly-model-family")
_SELECT: dict[str, Any] = {"required_score": 40.0, "cost_tolerance": 0, "runtime": "claude_code"}
_RULING = "rul_1edbcfc7-c2a4-4176-96ae-c51c90df3a21"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _state() -> StateManagementInterface:
    state = RealShapeState()
    typed = cast("StateManagementInterface", state)
    seed_catalog(typed, seed=load_seed_table())
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        row["acceptance"] = CELL_ACCEPTANCE_ACCEPTED
        row["accepted_at"] = _NOW.isoformat()
        row["measured_at"] = _NOW.isoformat()
        row["last_refresh_run_id"] = "allowance-reading-fixture"
    return typed


def _reading(pool: str, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "profile_id": _PROFILE, "pool_id": pool, "consumed": 12.5,
        "as_of": (_NOW - timedelta(minutes=5)).isoformat(),
        "next_reset_at": (_NOW + timedelta(hours=2)).isoformat(),
        "source": f"operator reading, Claude Settings > Usage; {_RULING}", "recorded_by": "Coordinator",
    }
    return {**params, **overrides}


def _refusal(callback: Any) -> tuple[str, str]:
    try:
        callback()
    except (CatalogError, TierSelectionError) as exc:
        return exc.code, exc.message
    return "", ""


def _stored(state: StateManagementInterface) -> int:
    return len(cast("RealShapeState", state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_ALLOWANCE_POOL_READING))


def _record(state: StateManagementInterface, pools: tuple[str, ...], **overrides: Any) -> None:
    for pool in pools:
        verbs.record_pool_reading(state, _reading(pool, **overrides), now=_NOW)


def test_no_reading_does_not_exclude_claude() -> None:
    state = _state()
    selection = verbs.select_dispatch_tier(state, dict(_SELECT), now=_NOW)
    cell = selection["selected"]
    _check(cell["runtime"] == "claude_code", "no reading at all: runtime=claude_code selects a fresh accepted Claude cell (refused before iss_3ff06701)")
    _check("quota_unknown" not in selection["excluded"], "an unknown quota is no longer an exclusion class")
    verified = _refusal(lambda: verbs.verify_selection_receipt(
        state, difficulty_score=40.0, selection_receipt=selection["selection_receipt"], dispatch_kind="implement", scope_tags=(),
        agent_runtime=cell["runtime"], model=cell["model"], effort=cell["effort"], now=_NOW,
    ))
    _check(verified == ("", ""), "the receipt of a reading-free Claude selection verifies for its spawn tuple")
    sonnet = verbs.select_dispatch_tier(state, {**_SELECT, "required_score": 24.0}, now=_NOW)
    _check(sonnet["selected"]["model"].startswith("claude-sonnet"), "no reading: a Sonnet cell is selectable, which no pool reading could ever make true before")
    free = verbs.select_dispatch_tier(state, {"required_score": 40.0, "cost_tolerance": 0}, now=_NOW)
    _check(free["selected"]["runtime"] == "codex" and free["expired_allowance_readings"] == [], "no reading: unconstrained selection still picks Codex")
    _check(_stored(state) == 0, "selecting never writes a reading")


def test_recorded_reading_makes_claude_selectable() -> None:
    state = _state()
    _record(state, _ALL_POOLS)
    selection = verbs.select_dispatch_tier(state, dict(_SELECT), now=_NOW)
    cell = selection["selected"]
    receipt = selection["selection_receipt"]
    _check(cell["runtime"] == "claude_code", "current readings on the applicable pools: runtime=claude_code selects a Claude cell")
    verified = _refusal(lambda: verbs.verify_selection_receipt(
        state, difficulty_score=40.0, selection_receipt=receipt, dispatch_kind="implement", scope_tags=(),
        agent_runtime=cell["runtime"], model=cell["model"], effort=cell["effort"], now=_NOW,
    ))
    _check(cell["runtime"] == "claude_code" and verified == ("", ""), "the receipt verifies for the claude_code spawn tuple")
    _check(selection["expired_allowance_readings"] == [], "no expired readings reported while all are current")


def test_reading_is_never_inferred_across_pools() -> None:
    state = _state()
    _record(state, ("weekly-model-family",), consumed=100)
    low = {**_SELECT, "required_score": 24.0, "cost_tolerance": 0}
    selection = verbs.select_dispatch_tier(state, dict(_SELECT), now=_NOW)
    _check(
        selection["selected"]["runtime"] == "claude_code" and not selection["selected"]["model"].startswith("claude-sonnet"),
        "only the Sonnet family pool exhausted: non-Sonnet Claude cells stay selectable beside two unknown pools",
    )
    _check(selection["excluded"]["quota_exhausted"] > 0, "the exhausted weekly-model-family pool excludes Sonnet cells even though the other pools are unknown")
    _check(not verbs.select_dispatch_tier(state, low, now=_NOW)["selected"]["model"].startswith("claude-sonnet"), "an exhausted Sonnet pool keeps the cheap Sonnet cell out")
    _record(state, ("weekly-model-family",), consumed=10)
    _check(verbs.select_dispatch_tier(state, low, now=_NOW)["selected"]["model"].startswith("claude-sonnet"), "a current non-exhausted reading makes the cheap Sonnet cell selectable")
    other = _state()
    _record(other, ("rolling-five-hour",), consumed=100)
    code, _ = _refusal(lambda: verbs.select_dispatch_tier(other, dict(_SELECT), now=_NOW))
    _check(code == "no_cell_clears_threshold", "the five-hour pool exhausted excludes every Claude cell, the other two pools being unknown")


def test_expiry_makes_pools_unknown_again() -> None:
    state = _state()
    _record(state, _ALL_POOLS, consumed=100)
    _check(_refusal(lambda: verbs.select_dispatch_tier(state, dict(_SELECT), now=_NOW))[0] == "no_cell_clears_threshold", "control: while current, the exhausted readings exclude claude_code")
    later = _NOW + timedelta(hours=3)
    _check(verbs.select_dispatch_tier(state, dict(_SELECT), now=later)["selected"]["runtime"] == "claude_code", "past next_reset_at: an expired exhausted reading no longer excludes")
    code, message = _refusal(lambda: verbs.select_dispatch_tier(state, {**_SELECT, "required_score": 1000.0}, now=later))
    _check(code == "no_cell_clears_threshold" and "Expired allowance readings" in message and "rolling-five-hour" in message, "a refusal names the expired readings")
    free = verbs.select_dispatch_tier(state, {"required_score": 40.0, "cost_tolerance": 0}, now=later)
    _check(len(free["expired_allowance_readings"]) == 3, "a successful selection still lists the expired readings")
    _record(state, ("rolling-five-hour",), next_reset_at=None, expires_at=(_NOW + timedelta(minutes=30)).isoformat())
    earlier = verbs.read_pool_readings_view(state, {}, now=_NOW + timedelta(hours=1))
    states = {item["pool_id"]: item["state"] for item in earlier["readings"]}
    _check(states["rolling-five-hour"] == "expired" and states["weekly-all-model"] == "current", "an explicit expires_at expires its own pool only")
    both = verbs.record_pool_reading(
        state, _reading("weekly-all-model", expires_at=(_NOW + timedelta(minutes=10)).isoformat()), now=_NOW,
    )
    _check(both["effective_expiry"] == (_NOW + timedelta(minutes=10)).isoformat(), "with both bounds the earlier one governs")


def test_exhausted_reading_excludes_claude() -> None:
    state = _state()
    _record(state, _ALL_POOLS, consumed=100)
    code, _ = _refusal(lambda: verbs.select_dispatch_tier(state, dict(_SELECT), now=_NOW))
    _check(code == "no_cell_clears_threshold", "a fully consumed window excludes the pool as exhausted, not available")
    _record(state, _ALL_POOLS, consumed=None, remaining=100)
    _check(verbs.select_dispatch_tier(state, dict(_SELECT), now=_NOW)["selected"]["runtime"] == "claude_code", "re-recording replaces the reading")
    _check(_stored(state) == 3, "one row per pool after re-recording")


def test_invalid_records_are_refused_and_write_nothing() -> None:
    state = _state()
    pool = "rolling-five-hour"
    cases: list[tuple[str, dict[str, Any]]] = [
        ("wrong pool", _reading("no-such-pool")),
        ("wrong profile", _reading(pool, profile_id="openai-metered-gpt-6-sol-2026-09-22")),
        ("negative percent", _reading(pool, consumed=-1)),
        ("over 100 percent", _reading(pool, consumed=100.5)),
        ("remaining over 100", _reading(pool, consumed=None, remaining=101)),
        ("boolean percent", _reading(pool, consumed=True)),
        ("no percent at all", _reading(pool, consumed=None)),
        ("inconsistent consumed and remaining", _reading(pool, consumed=10, remaining=10)),
        ("missing source", _reading(pool, source="")),
        ("missing recorded_by", _reading(pool, recorded_by=" ")),
        ("no expiry", _reading(pool, next_reset_at=None)),
        ("expiry already past", _reading(pool, next_reset_at=(_NOW - timedelta(minutes=1)).isoformat())),
        ("as_of in the future", _reading(pool, as_of=(_NOW + timedelta(minutes=1)).isoformat())),
        ("as_of without timezone", _reading(pool, as_of="2026-09-24T11:55:00")),
        ("missing as_of", _reading(pool, as_of=None)),
    ]
    for label, params in cases:
        code, _ = _refusal(lambda params=params: verbs.record_pool_reading(state, params, now=_NOW))
        _check(code == "parameter_invalid", f"invalid record refused loudly: {label}")
    _check(_stored(state) == 0, "no refused record wrote a row")
    _check(verbs.select_dispatch_tier(state, dict(_SELECT), now=_NOW)["selected"]["runtime"] == "claude_code", "refused records changed nothing: claude_code still selectable")


def test_read_and_retract() -> None:
    state = _state()
    _record(state, _ALL_POOLS)
    listing = verbs.read_pool_readings_view(state, {}, now=_NOW)
    _check(listing["current_readings"] == 3 and len(listing["readings"]) == 3, "read lists three current readings")
    first = listing["readings"][0]
    _check(first["consumed"] + first["remaining"] == 100.0 and first["recorded_by"] == "Coordinator", "a reading carries both percents and the recording actor")
    _check(len(verbs.read_pool_readings_view(state, {"profile_id": "nope"}, now=_NOW)["readings"]) == 0, "read filters by profile_id")
    gone = verbs.retract_pool_reading(state, {"profile_id": _PROFILE, "pool_id": "weekly-all-model"})
    _check(gone["retracted"] == 1 and _stored(state) == 2, "retract deletes exactly one reading")
    _check(_refusal(lambda: verbs.retract_pool_reading(state, {"profile_id": _PROFILE, "pool_id": "weekly-all-model"}))[0] == "reading_not_found", "retracting a missing reading is loud")
    _record(state, ("weekly-all-model",), consumed=100)
    _check(_refusal(lambda: verbs.select_dispatch_tier(state, dict(_SELECT), now=_NOW))[0] == "no_cell_clears_threshold", "control: an exhausted reading excludes claude_code")
    verbs.retract_pool_reading(state, {"profile_id": _PROFILE, "pool_id": "weekly-all-model"})
    _check(verbs.select_dispatch_tier(state, dict(_SELECT), now=_NOW)["selected"]["runtime"] == "claude_code", "a retracted exhausted pool reads unknown, which excludes nothing")


if __name__ == "__main__":
    print(f"selector under test: {verbs.__file__}")
    test_no_reading_does_not_exclude_claude()
    test_recorded_reading_makes_claude_selectable()
    test_reading_is_never_inferred_across_pools()
    test_expiry_makes_pools_unknown_again()
    test_exhausted_reading_excludes_claude()
    test_invalid_records_are_refused_and_write_nothing()
    test_read_and_retract()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    raise SystemExit(1 if _failed else 0)
