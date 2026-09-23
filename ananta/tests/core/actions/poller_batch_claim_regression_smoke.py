#!/usr/bin/env python3
"""Regression smoke for the ActionQueuePoller batch-claim starvation defect.

``_poll_once`` used to mark an entire fetched batch ``processing`` before it
entered the first handler.  A handler that stalled left every later row in the
batch claimed even though its handler had never run; green's
``start_interface`` action was one such victim during the 2026-09-13 swap.

This drives the shipped ``_poll_once`` with a genuinely stalled first handler.
The timeout is the fixture's stand-in for a dead or indefinitely blocked
handler; it cancels the poll cycle only after the first handler has entered.
The load-bearing assertion is durable queue state: later rows must remain
``queued`` and therefore claimable by a healthy poller.  The test also pins the
per-row compare-and-set and the raised-handler failure path.

Run:

    .venv/bin/python3 ananta/tests/core/actions/poller_batch_claim_regression_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.actions.action_queue_poller import (  # noqa: E402
    ActionQueuePoller,
    QueuedAction,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _QueueState:
    """In-memory state-interface seam with the production CAS filter shape."""

    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = dict(statuses)
        self.claim_queries: list[dict[str, object]] = []

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
    ) -> dict[str, object]:
        del namespace
        self.claim_queries.append(query)
        filters = query.get("filters")
        if not isinstance(filters, dict):
            raise AssertionError(f"missing filters: {query!r}")
        action_id = filters.get("id")
        expected_status = filters.get("status")
        if not isinstance(action_id, str) or not isinstance(expected_status, str):
            raise AssertionError(f"claim is not id/status guarded: {query!r}")
        updated = int(self.statuses.get(action_id) == expected_status)
        if updated:
            status = updates.get("status")
            if not isinstance(status, str):
                raise AssertionError(f"claim wrote no status: {updates!r}")
            self.statuses[action_id] = status
        return {
            "action_status": "completed",
            "data": {"result": {"updated": updated}},
        }


def _action(action_id: str) -> QueuedAction:
    return QueuedAction(
        id=action_id,
        process_key="plugin::agent_messaging_plugin::start_interface",
        parameters="{}",
        notes="",
        created_at="2026-09-13T00:00:00Z",
    )


def _poller(state: _QueueState, actions: list[QueuedAction]) -> ActionQueuePoller:
    """Real poller with only queue-read and handler seams replaced."""
    poller = object.__new__(ActionQueuePoller)
    poller.state_service = state
    poller.total_actions_processed = 0
    poller._last_observed_queue_depth = len(actions)

    async def get_queued_actions() -> list[QueuedAction]:
        return actions

    poller._get_queued_actions = get_queued_actions
    return poller


def test_stalled_first_handler_leaves_later_actions_queued() -> None:
    """The incident reproduction: a stall cannot pre-claim its batch siblings."""
    print("\n[1] stalled first handler does not strand later batch rows")
    actions = [_action("first"), _action("start-interface"), _action("third")]
    state = _QueueState({action.id: "queued" for action in actions})
    poller = _poller(state, actions)
    entered: list[str] = []
    never = asyncio.Event()

    async def stall_first(action: QueuedAction) -> None:
        entered.append(action.id)
        await never.wait()

    poller._process_action = stall_first

    async def drive() -> bool:
        try:
            await asyncio.wait_for(poller._poll_once(), timeout=0.05)  # noqa: SLF001
        except TimeoutError:
            return True
        return False

    _check(asyncio.run(drive()), "fixture advanced: first handler genuinely stalled")
    _check(entered == ["first"], f"only first handler entered before timeout: {entered}")
    _check(state.statuses["first"] == "processing", "stalled action is its own processing row")
    _check(
        state.statuses["start-interface"] == "queued" and state.statuses["third"] == "queued",
        "later actions remain queued rather than claimed-and-abandoned",
    )
    _check(
        len(state.claim_queries) == 1,
        f"only entered work was claimed (claim count={len(state.claim_queries)})",
    )


def test_lost_compare_and_set_claim_is_never_executed() -> None:
    """A stale fetched row is skipped when another poller won its transition."""
    print("\n[2] conditional claim prevents duplicate execution")
    actions = [_action("already-claimed"), _action("ours")]
    state = _QueueState({"already-claimed": "processing", "ours": "queued"})
    poller = _poller(state, actions)
    entered: list[str] = []

    async def record(action: QueuedAction) -> None:
        entered.append(action.id)

    poller._process_action = record
    asyncio.run(poller._poll_once())  # noqa: SLF001

    _check(entered == ["ours"], f"lost CAS row was not executed: {entered}")
    _check(
        all(
            query.get("filters") == {"id": action_id, "status": "queued"}
            for query, action_id in zip(
                state.claim_queries, ["already-claimed", "ours"], strict=True
            )
        ),
        "every claim carries both id and queued-status predicates",
    )


def test_raised_handler_is_failed_before_the_next_action_runs() -> None:
    """A raised handler is terminally marked and cannot leave itself processing."""
    print("\n[3] raised handler is failed and does not block the next row")
    actions = [_action("raises"), _action("next")]
    state = _QueueState({action.id: "queued" for action in actions})
    poller = _poller(state, actions)
    entered: list[str] = []

    async def raises_once(action: QueuedAction) -> None:
        entered.append(action.id)
        if action.id == "raises":
            raise RuntimeError("intentional handler failure")

    def mark_failed(action_id: str, _message: str, *_args: Any, **_kwargs: Any) -> None:
        state.statuses[action_id] = "failed"

    poller._process_action = raises_once
    poller._mark_action_failed = mark_failed
    asyncio.run(poller._poll_once())  # noqa: SLF001

    _check(state.statuses["raises"] == "failed", "raised action was marked failed")
    _check(entered == ["raises", "next"], f"next row still ran after failure: {entered}")


def main() -> None:
    print("ActionQueuePoller per-row claim regression smoke")
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"\n{_passed} passed; {len(_failed)} failed")
    if _failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
