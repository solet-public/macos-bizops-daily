#!/usr/bin/env python3
"""Phase 0 freeze — queue ``completed`` = DISPATCH, not task completion (no pytest).

Protects contract (1) of the Phase 0 "freeze current contracts" work
(``workbench/2026-07-01_claude_coding_agent_substrate_architecture_and_planning_v2.md``
PART VI + PART II): the action queue is a *dispatch log*. An action's
``completed`` status means it was dispatched to its plugin/interface — NOT that
the business work finished. Self-completing seams (inference / claude_code)
return ``COMPLETED`` immediately and deliver the real result later on a
background path.

Faithful assertions on the real ``ActionQueuePoller`` methods:

* ``_update_action_status_to_completed`` sets ``status=completed`` on
  ``core.action_events`` keyed on the action id alone — no result / task
  outcome is consulted.
* ``_mark_action_completed`` sets the status to completed FIRST, before (and
  independent of) result retrieval: with no stored details it still marks
  completed. A regression that made completion wait on the task result would
  set the flag only after the result landed, and this assertion would fail.
* ``_handles_own_completion`` / ``_should_skip_result_processor`` recognise the
  fire-and-forget inference dispatch and skip synchronous result processing —
  the mechanism by which dispatch-completion is decoupled from task completion.

Offline: the poller is built via ``object.__new__`` with only a recording
state-service wired (the seams under test touch nothing else). No live solet /
LM Studio / Postgres.

Run:
    .venv/bin/python3 \\
      ananta/tests/core/substrate_contracts/queue_completion_is_dispatch_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from substrate_contract_fixtures import (  # noqa: E402
    RECORD_STEP_KEY,
    Checker,
    RecordingStateService,
    build_offline_poller,
)

# The two self-completing PATTERN-STRINGS ``_handles_own_completion`` matches as
# substrings (``action_queue_poller.py`` ``self_completing_patterns``). These are
# not registered process keys — ``claude_code_plugin`` is the coding-agent bridge
# identity (no registered plugin) and ``inference_service`` has no
# ``inference_request`` verb (removed, no back-compat). They MUST carry those
# substrings to exercise the matcher, so they carry the ``# wint:negative-fixture``
# inline marker — the recognized whole-tree-gate negative-fixture convention
# (was: per-key C3.1 allowlist entries; migrated to the marker 2026-07-02).
_INFERENCE_DISPATCH = "plugin::claude_code_plugin::inference_request"  # wint:negative-fixture
_BARE_INFERENCE = "service_interface::inference_service::inference_request"  # wint:negative-fixture
# An ordinary, REAL registered verb (not self-completing): matches neither pattern.
_BLOCKING_KEY = RECORD_STEP_KEY


def test_status_update_is_keyed_on_action_id_alone(c: Checker) -> None:
    state = RecordingStateService()
    poller = build_offline_poller(state)
    poller._update_action_status_to_completed("ae-1")
    c.check(len(state.update_calls) == 1, "exactly one status write issued")
    if state.update_calls:
        call = state.update_calls[0]
        query = call["query"]
        c.check(
            call["namespace"] == "core" and query.get("table") == "action_events",
            "status write targets core.action_events",
        )
        c.check(
            query.get("filters") == {"id": "ae-1"},
            "status write is filtered by action id alone (no task outcome)",
        )
        c.check(
            call["updates"] == {"status": "completed", "error_message": None},
            "status write sets status=completed and clears error_message "
            "(dispatch flag; neither field is a task outcome)",
        )


def test_mark_completed_sets_status_before_result_handling(c: Checker) -> None:
    """Completion is set at dispatch: marked even when no result detail exists."""
    state = RecordingStateService()  # query_state returns no rows -> early return
    poller = build_offline_poller(state)
    poller._mark_action_completed("ae-2", {})
    marked = [
        call
        for call in state.update_calls
        if call["query"].get("table") == "action_events"
        and call["updates"] == {"status": "completed", "error_message": None}
    ]
    c.check(
        len(marked) == 1 and marked[0]["query"].get("filters") == {"id": "ae-2"},
        "status is marked completed even with no stored result (completion at dispatch)",
    )
    # The contract frozen here is that the status write precedes RESULT
    # handling, not that it is the first database call of any kind. Since
    # adopter issue #9 the completion path reads the row once BEFORE the write,
    # to capture the error_message that the write is about to clear — the
    # double-execution detector. That read is not a result lookup, and the
    # ordering the contract exists to protect is unchanged: a regression that
    # made completion WAIT on the task result would move "update" after the
    # trailing "query" and still redden this check.
    c.check(
        state.events[:3] == ["query", "update", "query"],
        "status write precedes the result lookup (dispatch before result "
        "handling), after the pre-clear read",
    )


def test_self_completing_dispatch_is_recognised(c: Checker) -> None:
    state = RecordingStateService()
    poller = build_offline_poller(state)
    c.check(
        poller._handles_own_completion(_INFERENCE_DISPATCH) is True,
        "claude_code inference dispatch handles its own completion (fire-and-forget)",
    )
    c.check(
        poller._handles_own_completion(_BARE_INFERENCE) is True,
        "inference_request handles its own completion",
    )
    c.check(
        poller._handles_own_completion(_BLOCKING_KEY) is False,
        "an ordinary process does NOT self-complete (blocking dispatch)",
    )


def test_self_completing_dispatch_defers_result_processing(c: Checker) -> None:
    """Dispatch != task done: the self-completing seam skips synchronous result work."""
    state = RecordingStateService()
    poller = build_offline_poller(state)
    poller._async_process_cache[_BLOCKING_KEY] = False  # avoid a registry read
    c.check(
        poller._should_skip_result_processor(_INFERENCE_DISPATCH) is True,
        "inference dispatch skips synchronous result processing (result arrives later)",
    )
    c.check(
        poller._should_skip_result_processor(_BLOCKING_KEY) is False,
        "an ordinary process runs result processing (not self-completing)",
    )


def main() -> int:
    c = Checker("Queue completed = dispatch, not task completion (Phase 0 contract 1)")
    print(f"=== {c.title} ===")
    test_status_update_is_keyed_on_action_id_alone(c)
    test_mark_completed_sets_status_before_result_handling(c)
    test_self_completing_dispatch_is_recognised(c)
    test_self_completing_dispatch_defers_result_processing(c)
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
