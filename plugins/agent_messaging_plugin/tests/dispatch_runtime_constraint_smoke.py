#!/usr/bin/env python3
"""Proves a coordinator can select within one runtime and spawn on the resulting receipt.

On the seeded catalog a Codex cell dominates every Claude cell, so the
unconstrained selector can never return Claude. The Claude subscription pools
carry explicitly recorded current readings, as an operator may record them (none
is required; allowance_pool_reading_smoke proves it). The ``runtime`` parameter
filters the catalog before domination, the receipt records it as
``runtime_constraint``, and replay re-applies it.
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
from agent_messaging_plugin.schema import CELL_ACCEPTANCE_ACCEPTED, TABLE_MODEL_CAPABILITY_CELL  # noqa: E402

_passed = 0
_failed: list[str] = []
_NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
_SCORE = 40.0
_PARAMS: dict[str, Any] = {"required_score": _SCORE, "cost_tolerance": 0}


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _refusal(callback: Any) -> tuple[str, str]:
    try:
        callback()
    except (CatalogError, TierSelectionError) as exc:
        return exc.code, exc.message
    return "", ""


def _state(*, claude_score: float | None = None) -> StateManagementInterface:
    state = RealShapeState()
    typed = cast("StateManagementInterface", state)
    seed_catalog(typed, seed=load_seed_table())
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        row["acceptance"] = CELL_ACCEPTANCE_ACCEPTED
        row["accepted_at"] = _NOW.isoformat()
        row["measured_at"] = _NOW.isoformat()
        row["last_refresh_run_id"] = "runtime-constraint-fixture"
        if claude_score is not None and row["runtime"] == "claude_code":
            row["capability_score"] = claude_score
    _record_current_readings(typed)
    return typed


def _record_current_readings(state: StateManagementInterface) -> None:
    """Record an explicit current reading on every Claude subscription pool, as an operator would."""
    for pool in ("rolling-five-hour", "weekly-all-model", "weekly-model-family"):
        verbs.record_pool_reading(
            state,
            {
                "profile_id": "anthropic-max-20x-2026-08-22", "pool_id": pool, "consumed": 10,
                "as_of": (_NOW - timedelta(minutes=5)).isoformat(), "next_reset_at": (_NOW + timedelta(hours=2)).isoformat(),
                "source": "runtime constraint smoke fixture", "recorded_by": "smoke",
            },
            now=_NOW,
        )


def _verify(state: StateManagementInterface, receipt: object, tuple_: dict[str, str]) -> None:
    verbs.verify_selection_receipt(
        state, difficulty_score=_SCORE, selection_receipt=receipt, dispatch_kind="implement", scope_tags=(),
        agent_runtime=tuple_["runtime"], model=tuple_["model"], effort=tuple_["effort"], now=_NOW,
    )


def test_unconstrained_selection_is_unchanged() -> None:
    free = verbs.select_dispatch_tier(_state(), dict(_PARAMS), now=_NOW)
    _check(free["selected"]["runtime"] == "codex", "(a) no constraint: Codex dominates and is selected")
    _check(free["selection_receipt"].get("runtime_constraint", "absent") is None, "(a) unconstrained receipt carries runtime_constraint null")
    _check(
        any(pair["weaker"]["runtime"] == "claude_code" and pair["stronger"]["runtime"] == "codex" for pair in free["dominated"]),
        "(a) the fixture really has a Codex cell dominating a Claude cell",
    )


def test_constrained_selection_and_receipt() -> None:
    state = _state()
    constrained = verbs.select_dispatch_tier(state, {**_PARAMS, "runtime": "claude_code"}, now=_NOW)
    cell = constrained["selected"]
    _check(cell["runtime"] == "claude_code", "(b) runtime=claude_code selects a Claude cell")
    clearing = [c for c in constrained["frontier"] if c["capability_score"] >= _SCORE]
    _check(
        cell["runtime"] == "claude_code" and cell == min(clearing, key=lambda c: c["relative_cost_multiplier"]),
        "(b) it is the cheapest clearing Claude cell",
    )
    _check({c["runtime"] for c in constrained["frontier"]} == {"claude_code"}, "(b) no Codex cell reaches the frontier")
    _check(
        all(pair["stronger"]["runtime"] == "claude_code" for pair in constrained["dominated"]),
        "(b) domination is computed within the runtime",
    )
    receipt = constrained["selection_receipt"]
    _check(receipt.get("runtime_constraint") == "claude_code", "(c) receipt carries runtime_constraint")
    _check(
        receipt["selected"]["runtime"] == "claude_code" and _refusal(lambda: _verify(state, receipt, receipt["selected"])) == ("", ""),
        "(c) receipt verifies for the claude_code spawn tuple",
    )


def test_receipt_tampering_is_refused() -> None:
    state = _state()
    receipt = verbs.select_dispatch_tier(state, {**_PARAMS, "runtime": "claude_code"}, now=_NOW)["selection_receipt"]
    stripped = {**receipt, "runtime_constraint": None}
    _check(_refusal(lambda: _verify(state, stripped, stripped["selected"]))[0] == "selection_receipt_mismatch", "(d) constraint stripped: refused")
    changed = {**receipt, "runtime_constraint": "codex"}
    _check(_refusal(lambda: _verify(state, changed, changed["selected"]))[0] == "selection_receipt_mismatch", "(d) constraint changed: refused")
    missing = {key: value for key, value in receipt.items() if key != "runtime_constraint"}
    _check(_refusal(lambda: _verify(state, missing, missing["selected"]))[0] == "selection_receipt_invalid", "(e) receipt missing runtime_constraint: refused")
    codex_tuple = verbs.select_dispatch_tier(state, dict(_PARAMS), now=_NOW)["selection_receipt"]["selected"]
    _check(_refusal(lambda: _verify(state, receipt, codex_tuple))[0] == "selection_receipt_mismatch", "constrained receipt refuses a Codex spawn tuple")
    codex_bound = verbs.select_dispatch_tier(state, {**_PARAMS, "runtime": "codex"}, now=_NOW)["selection_receipt"]
    _check(_refusal(lambda: _verify(state, codex_bound, codex_bound["selected"])) == ("", ""), "control: a codex-constrained receipt verifies for its Codex tuple")
    _check(_refusal(lambda: _verify(state, codex_bound, receipt["selected"]))[0] == "selection_receipt_mismatch", "a codex-constrained receipt refuses a Claude spawn tuple")


def test_refusals_are_loud() -> None:
    weak_claude = _state(claude_score=10)
    code, message = _refusal(lambda: verbs.select_dispatch_tier(weak_claude, {**_PARAMS, "runtime": "claude_code"}, now=_NOW))
    _check(code == "no_cell_clears_threshold" and "runtime=claude_code" in message, "(f) no clearing Claude cell: no-clearing error naming the runtime")
    _check(verbs.select_dispatch_tier(weak_claude, dict(_PARAMS), now=_NOW)["selected"]["runtime"] == "codex", "(f) control: Codex still clears on the same catalog")
    code, message = _refusal(lambda: verbs.select_dispatch_tier(_state(), {**_PARAMS, "runtime": "gemini"}, now=_NOW))
    _check(code == "parameter_invalid" and "gemini" in message, "(g) unknown runtime: loud parameter_invalid")
    code, _ = _refusal(lambda: verbs.select_dispatch_tier(_state(), {**_PARAMS, "runtime": 7}, now=_NOW))
    _check(code == "parameter_invalid", "(g) non-string runtime: loud parameter_invalid")


if __name__ == "__main__":
    print(f"selector under test: {verbs.__file__}")
    test_unconstrained_selection_is_unchanged()
    test_constrained_selection_and_receipt()
    test_receipt_tampering_is_refused()
    test_refusals_are_loud()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    raise SystemExit(1 if _failed else 0)
