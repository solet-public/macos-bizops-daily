#!/usr/bin/env python3
"""Smoke: the flow-scoped counters survive the 100-row bound.

## Why this exists

`rel-20260816T033201Z-151fc1321` deployed with `MAX_READ_ROWS = 100` and
immediately began failing:

    FrameworkError: Failed to query pending token count for flow
                    flow-ledger-periodic-poll

`get_pending_token_count` read every matching row and returned `len(rows)`.
Measured on that release: `flow-ledger-periodic-poll` held 25,339 rows, **129 of
them non-terminal** — so the unbounded read was refused, 29 rows past the cap.
It is reached from `complete_token` -> `_check_flow_completion`, i.e. **on every
action completion**, so it was not one broken flow but a broken completion path.

Both sites were found and classified HOT by the read-cap census. They were then
parked in a tier-2 list whose predicates were *sampled* rather than measured, and
the sample was chosen by eye: `{flow_id_trace, state}` reads like a narrow key,
so it looked selective. It matched 129. **A selective-LOOKING predicate cleared
at triage is the same defect as one cleared at scan time — the judgement just
moved somewhere less visible.**

## What is pinned, and why each check would fail on the obvious revert

* `get_pending_token_count` issues a **`count`**, never a row read. The repair is
  not a bound: the caller wanted a number, and `count` runs the aggregate inside
  the owner plugin and ships a scalar, so it is outside the row cap entirely and
  cannot regress at any table size.
* It still returns the right number when the matching set **exceeds the cap** —
  against a fake that refuses an unbounded row read exactly as the provider does.
  Without that refusal the smoke would go green against the reverted code, which
  is precisely how the defect reached production.
* A malformed or non-completed `count` envelope **raises**. This is the check
  that matters most: the scalar is nested at `data.result.value`, and reading
  `data.value` would yield `None` on a healthy response. Coerced to 0 that means
  "no pending tokens" — which marks a live flow COMPLETE. A wrong count here is
  worse than an error.
* `get_pending_tokens` walks **past a page boundary** and returns every row in
  `(created_at, id)` order. Its fixture is 250 rows: a fixture under 100 cannot
  reach the boundary and would pass against a walk that stops after page one.

PURE UNIT: no DB, no platform. Pages are served by the real ordered-query
primitives, so an over-cap page size or a non-composite order_by fails here
exactly as the provider would fail it.

Run:
    .venv/bin/python3 ananta/tests/core/state/flow_counter_bound_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.state.flow_runtime_graph import (  # noqa: E402
    NON_TERMINAL_STATES,
    FlowRuntimeGraph,
)
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.services.state_service.ordered_query import (  # noqa: E402
    apply_ordered_query_in_memory,
    parse_ordered_query,
)
from ananta.services.state_service.read_bounds import MAX_READ_ROWS  # noqa: E402

_passed = 0
_failed: list[str] = []

_FLOW = "flow-under-test"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _token(n: int, *, flow: str = _FLOW, state: str = "pending") -> dict[str, Any]:
    """A standardizer-shaped token row with an order-stable cursor pair."""
    return {
        "id": f"tok-{n:05d}",
        "created_at": f"2026-08-15T00:00:00.{n:06d}",
        "flow_id_trace": flow,
        "core__flows_id": flow,
        "owner_type": "vertex",
        "owner_ref": f"vx-{n:05d}",
        "state": state,
        "process_key": "svc::do_thing",
        "parent_token_id": None,
        "metadata": "{}",
        "is_deleted": 0,
    }


class _CountingState:
    """State fake that models the provider's REFUSAL of an unbounded row read.

    `query_state` is deliberately hostile: past `MAX_READ_ROWS` it returns the
    real `query.unbounded_read_over_cap` error envelope rather than the rows. A
    permissive fake here would serve 129 rows happily and green the exact code
    that cannot run in production — the failure mode this file was written for.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.count_calls: list[dict[str, Any]] = []
        self.query_state_calls: list[dict[str, Any]] = []
        self.ordered_calls: list[dict[str, Any]] = []
        self.count_envelope: dict[str, Any] | None = None

    def _matching(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for row in self.rows:
            ok = True
            for key, want in filters.items():
                cell = row.get(key)
                if isinstance(want, list):
                    if cell not in want:
                        ok = False
                elif cell != want:
                    ok = False
            if ok:
                out.append(row)
        return out

    def count(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        self.count_calls.append(dict(data))
        if self.count_envelope is not None:
            return self.count_envelope
        n = len(self._matching(dict(data.get("filters", {}))))
        return {"action_status": "completed", "data": {"result": {"value": n}}}

    def query_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        self.query_state_calls.append(dict(data))
        matched = self._matching(dict(data.get("filters", {})))
        if data.get("limit") is None and not data.get("unbounded"):
            if len(matched) > MAX_READ_ROWS:
                return {
                    "action_status": "error",
                    "error": {
                        "code": "query.unbounded_read_over_cap",
                        "message": (
                            f"read_state on table {data.get('table')!r} returned more "
                            f"than the {MAX_READ_ROWS}-row cap for a query with no "
                            f"explicit 'limit'. Refused rather than truncated."
                        ),
                    },
                    "data": {},
                }
        return {"action_status": "completed", "data": {"records": matched}}

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        self.ordered_calls.append(dict(data))
        spec = parse_ordered_query(data)
        selected = apply_ordered_query_in_memory([dict(r) for r in self.rows], spec)
        return {
            "action_status": "completed",
            "data": {"records": [dict(r) for r in selected]},
        }


def _frg(state: _CountingState) -> FlowRuntimeGraph:
    return FlowRuntimeGraph(cast("Any", state))


def _count_checks() -> None:
    print("A. the count is a scalar aggregate, not a row read")
    # 129 is the live figure that broke production: 29 past the cap.
    rows = [_token(n) for n in range(129)]
    state = _CountingState(rows)
    frg = _frg(state)

    got = frg.get_pending_token_count(_FLOW)
    _check(got == 129, f"returns the true count PAST the cap (got {got}, want 129)")
    _check(
        len(state.count_calls) == 1,
        f"issues exactly one count call (got {len(state.count_calls)})",
    )
    _check(
        state.query_state_calls == [],
        "issues NO query_state — the rows are never shipped, so the site is "
        "outside the row cap entirely rather than bounded within it",
    )
    _check(
        state.ordered_calls == [],
        "and no query_ordered either — a paginated COUNT would be correct and "
        "still the wrong repair",
    )
    sent = state.count_calls[0]
    _check(sent.get("table") == "flow_tokens", "counts the flow_tokens table")
    filters = cast("dict[str, Any]", sent.get("filters", {}))
    _check(
        filters.get("flow_id_trace") == _FLOW,
        "scoped to the flow",
    )
    _check(
        set(cast("list[str]", filters.get("state", [])))
        == {s.value for s in NON_TERMINAL_STATES},
        "and to the non-terminal states, unchanged from the read it replaced",
    )

    print("\nB. terminal tokens are excluded; an unknown flow is zero")
    mixed = _CountingState(
        [_token(n) for n in range(5)]
        + [_token(100 + n, state="completed") for n in range(7)],
    )
    _check(_frg(mixed).get_pending_token_count(_FLOW) == 5, "counts only non-terminal")
    _check(
        _frg(_CountingState([])).get_pending_token_count("flow-none") == 0,
        "an unknown flow is 0, not an error",
    )


def _envelope_checks() -> None:
    print("\nC. a bad count envelope RAISES — it must never read as zero")
    # This is the dangerous one. 0 pending tokens means "flow complete", so a
    # count that degrades to 0 marks live flows finished.
    for label, envelope in [
        ("non-completed status", {"action_status": "error", "data": {}}),
        ("value nested wrongly (data.value)", {"action_status": "completed", "data": {"value": 3}}),
        ("value missing", {"action_status": "completed", "data": {"result": {}}}),
        ("value not an int", {"action_status": "completed", "data": {"result": {"value": "3"}}}),
        ("value is a bool", {"action_status": "completed", "data": {"result": {"value": True}}}),
        ("not a dict at all", cast("Any", "boom")),
    ]:
        state = _CountingState([_token(0)])
        state.count_envelope = cast("Any", envelope)
        raised = False
        try:
            _frg(state).get_pending_token_count(_FLOW)
        except FrameworkError:
            raised = True
        _check(raised, f"raises on {label} (never coerces to 0)")


def _walk_checks() -> None:
    print("\nD. get_pending_tokens walks PAST a page boundary")
    # 250 rows = three pages. A fixture under 100 cannot reach the boundary, and
    # would pass against a walk that stops after the first page.
    rows = [_token(n) for n in range(250)]
    rows += [_token(500 + n, state="completed") for n in range(10)]
    rows += [_token(900 + n, flow="other-flow") for n in range(10)]
    state = _CountingState(rows)

    tokens = _frg(state).get_pending_tokens(_FLOW)
    _check(len(tokens) == 250, f"returns every non-terminal token (got {len(tokens)})")
    ids = [str(t["id"]) for t in tokens]
    _check(len(set(ids)) == len(ids), "no token is returned twice — the cursor advances")
    _check(ids == sorted(ids), "in (created_at, id) order, from the read not a re-sort")
    _check(
        len(state.ordered_calls) >= 3,
        f"paged at least three times (got {len(state.ordered_calls)})",
    )
    _check(
        state.query_state_calls == [],
        "and never fell back to an unbounded query_state",
    )
    _check(
        all(str(t["id"]).startswith("tok-0") for t in tokens),
        "no completed token and no other flow's token leaked in",
    )


def _breaker_checks() -> None:
    """The sibling in ``action_queue_poller``, which is worse and quieter.

    ``_get_flow_error_count`` feeds a CIRCUIT BREAKER — the caller terminates a
    flow once the count reaches ``_max_flow_errors`` (3). Its record extractor
    graceful-degrades a refused read to ``[]``, so the old unbounded read did not
    raise; it returned **0 errors for a flow with 2,902**, and ``0 >= 3`` is
    False. The breaker silently stopped firing, on the one path that is only ever
    taken because a flow is ALREADY going wrong.
    """
    from ananta.core.actions.action_queue_poller import ActionQueuePoller

    print("\nE. the flow-error breaker counts past the cap, and says so when it cannot")
    events = [
        {**_token(n), "status": "failed"} for n in range(2902)
    ] + [{**_token(90000 + n), "status": "completed"} for n in range(20)]
    state = _CountingState(events)
    poller = cast("Any", object.__new__(ActionQueuePoller))
    poller.state_service = state

    got = poller._get_flow_error_count(_FLOW)  # noqa: SLF001
    _check(got == 2902, f"returns the true failed count past the cap (got {got})")
    _check(
        state.query_state_calls == [],
        "via count, not a row read — 2,902 rows were being shipped to compute "
        "one integer that gates flow termination",
    )
    _check(got >= 3, "so the breaker's threshold comparison can actually fire")

    print("\nF. an unreadable breaker input fails OPEN but never SILENT")
    broken = _CountingState([{**_token(0), "status": "failed"}])
    broken.count_envelope = cast("Any", {"action_status": "error", "data": {}})
    poller2 = cast("Any", object.__new__(ActionQueuePoller))
    poller2.state_service = broken
    _check(
        poller2._get_flow_error_count(_FLOW) == 0,  # noqa: SLF001
        "returns 0 on an unreadable envelope — a transient blip must not "
        "terminate a healthy flow, and the poll loop must keep draining",
    )
    _check(
        poller2._query_scalar(cast("Any", {"action_status": "completed", "data": {"value": 7}}))  # noqa: SLF001
        is None,
        "the scalar extractor returns None (not 0) for a wrongly-nested value — "
        "0 and 'unknown' are the same number here and opposite facts",
    )
    # Calibration found this branch uncovered: mutating `return None` to
    # `return 0` on the NON-COMPLETED path left the whole file green, because
    # every other check fed it a *completed* envelope. Restoring 0 there would
    # re-silence the breaker without failing anything — the precise shape of the
    # original defect, reintroduced one layer down.
    for label, envelope in [
        ("a failed envelope", {"action_status": "error", "data": {}}),
        ("a missing status", {"data": {"result": {"value": 4}}}),
    ]:
        _check(
            poller2._query_scalar(cast("Any", envelope)) is None,  # noqa: SLF001
            f"and returns None (not 0) for {label} — the caller must be able to "
            f"tell 'no errors' from 'could not count', or the breaker goes quiet",
        )


def main() -> int:
    _count_checks()
    _envelope_checks()
    _walk_checks()
    _breaker_checks()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
