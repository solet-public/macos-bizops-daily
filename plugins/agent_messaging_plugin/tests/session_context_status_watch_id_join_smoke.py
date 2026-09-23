#!/usr/bin/env python3
"""Unit smoke for GAU-07 — ``session_context_status`` must resolve the id
``peer_list`` actually publishes for a watcher-held session.

THE DEFECT, measured 2026-08-18 on three independent live sessions
(``lane-rotation-notice``, ``lane-gau10-stall-boolean``, and this lane on
itself): a watcher-held worker's gauge row exists and is fresh, but ONLY under
its LEDGER id, while ``peer_list`` publishes its WATCH id. So anyone
enumerating fleet health through ``peer_list`` -- the obvious and documented
way -- sees every watcher-held session as gauge-less and would wrongly
conclude its reporting path is broken. Measured on this lane at 22:23Z:
``resolved=True`` (99,266 tokens) under ledger ``agi-17aae791...`` and
``resolved=False`` under watch ``agi-watch-ecd5edd6...``, in the same tick.
Three of five ``claude_code`` instances in the live fleet were watcher-held.

★ THIS IS A JOIN ERROR IN THE READER, deliberately kept separate from GAU-06
(a surfacing loss, where a notice never reaches the model). Different defect,
different fix; conflating them would send someone to the watcher surface to
repair a lookup bug.

THE FIX SHAPE, per the 2026-08-18 seat ruling: change the READ SURFACE so the
verb accepts EITHER id, resolving internally, and leave the ``peer_list``
envelope untouched -- every existing consumer's join starts working with zero
client changes, and the widely-consumed published envelope stays stable. The
standing rule that fleet lifecycle verbs key on the LEDGER id is unchanged,
because the resolution happens inside the verb.

★★ NO ID IS EVER DERIVED FROM ANOTHER. The chain is
watch id -> ``peer_binding.agent_session_id`` -> the gauge row carrying that
same stable session id; BOTH hops are STORED. This is load-bearing, not
stylistic: two session-id minting schemes are live simultaneously -- measured
the same day, a spawned worker's id reads ``ases-agi-<ledger id>``
(spawned-worker launcher) while an operator seat's reads
``ases-<epoch>-<pid>-<n>`` (seat launcher). Any prefix-slice derivation would
pass its tests against the first shape and route to nothing on the second. ``test_a_derivation_would_have_been_
wrong`` below pins exactly that.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/session_context_status_watch_id_join_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_messaging_plugin.context_status_verbs import (  # noqa: E402
    report_context_status,
    session_context_status,
)
from agent_messaging_plugin.schema import PEER_BINDING_TABLE  # noqa: E402
from agent_messaging_plugin.session_lifecycle_verbs import VerbError  # noqa: E402

_passed = 0
_failed: list[str] = []

# The real shapes, copied from the live fleet rather than invented, so the
# fixture cannot quietly agree with a convention the production data breaks.
LEDGER_ID = "agi-17aae791f6085536abd69afe7f00a83a"
WATCH_ID = "agi-watch-ecd5edd68448084b8b789930"
WORKER_SESSION_ID = "ases-agi-17aae791f6085536abd69afe7f00a83a"

SEAT_LEDGER_ID = "agi-6be1383613fbd0ec10874571e89956e1"
SEAT_WATCH_ID = "agi-watch-000000000000000000000000"
SEAT_SESSION_ID = "ases-1786663089-37639-3748"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _TwoTableState:
    """A state double over BOTH tables this join spans.

    Routes on the requested table, and supports filtering the gauge by
    ``agent_session_id`` as well as by ``agent_instance_id`` -- the whole
    point of the fix is a lookup on a NON-key column, so a fake that only
    ever honours the primary key would make the new path untestable and
    would pass regardless of what the verb did.
    """

    def __init__(self) -> None:
        self.gauge: dict[str, dict[str, Any]] = {}
        self.bindings: list[dict[str, Any]] = []
        self.gauge_queries: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []

    def upsert_state(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = dict(payload["record"])
        self.gauge[record["agent_instance_id"]] = record
        return {"action_status": "completed", "data": {}}

    def bind(self, *, agent_instance_id: str, agent_session_id: str) -> None:
        self.bindings.append(
            {"agent_instance_id": agent_instance_id, "agent_session_id": agent_session_id},
        )

    def query_state(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        table = payload.get("table")
        filters = dict(payload["filters"])
        filters.pop("is_deleted", None)
        if table == PEER_BINDING_TABLE:
            rows: list[dict[str, Any]] = self.bindings
        else:
            self.gauge_queries.append(filters)
            rows = list(self.gauge.values())
        matched = [
            row for row in rows
            if all(row.get(key) == value for key, value in filters.items())
        ]
        return {"action_status": "completed", "data": {"records": matched}}

    # -- GAU-15 (2026-08-19): the gauge write also appends a history row now.
    # Kept minimal ON PURPOSE -- this double exists to prove the WATCH-ID JOIN,
    # and the history table plays no part in that. These three keep the write
    # path callable without giving the join anything new to resolve against.

    def write_state(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.history.append(dict(payload["record"]))
        return {"action_status": "completed", "data": {"result": {"id": "h"}}}

    def query_ordered(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        wanted = payload["filters"]["agent_instance_id"]
        rows = [r for r in self.history if r.get("agent_instance_id") == wanted]
        rows.sort(key=lambda r: str(r.get("recorded_at") or ""), reverse=True)
        limit = int(payload.get("limit", len(rows)))
        return {
            "action_status": "completed",
            "data": {"records": rows[:limit], "count": min(limit, len(rows))},
        }

    def delete_records(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        cutoff = str(payload["filters"]["recorded_at"]["value"])
        kept = [
            r for r in self.history
            if not (
                r.get("agent_instance_id") == payload["filters"]["agent_instance_id"]
                and str(r.get("recorded_at") or "") < cutoff
            )
        ]
        deleted = len(self.history) - len(kept)
        self.history = kept
        return {
            "action_status": "completed",
            "data": {"result": {"deleted": deleted, "soft_delete": False}},
        }


def _report(state: _TwoTableState, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "agent_instance_id": LEDGER_ID,
        "runtime_session_id": "616f70d9",
        "provider": "anthropic",
        "runtime": "claude_code",
        "model": "claude-opus-5",
        "effort": "high",
        "current_tokens": 99_266,
        "ceiling": 1_000_000,
        "measured_at": "2026-08-18T22:21:55+00:00",
        "agent_session_id": WORKER_SESSION_ID,
    }
    kwargs.update(overrides)
    return report_context_status(state, **kwargs)


# ---------------------------------------------------------------------------
# THE RED PIN.
# ---------------------------------------------------------------------------

def test_watch_id_resolves_to_the_ledger_keyed_row() -> None:
    """THE DEFECT: keyed on the id ``peer_list`` publishes, this returned NO
    ROW while the session was healthy and reporting."""
    state = _TwoTableState()
    _report(state)
    state.bind(agent_instance_id=WATCH_ID, agent_session_id=WORKER_SESSION_ID)

    out = session_context_status(state, agent_instance_id=WATCH_ID)
    _check(out["resolved"] is True, "the WATCH id resolves to the session's gauge row")
    _check(
        out["current_tokens"] == 99_266,
        "...and carries the real measurement, not a placeholder",
    )


def test_direct_ledger_lookup_is_unchanged() -> None:
    """The existing path must not regress: this is the case that already
    worked, and the fix must not reroute it through the new hop."""
    state = _TwoTableState()
    _report(state)
    out = session_context_status(state, agent_instance_id=LEDGER_ID)
    _check(out["resolved"] is True, "the LEDGER id still resolves directly")
    _check(
        out["id_resolution"] == "direct",
        "a direct hit is REPORTED as direct, not silently laundered through the join",
    )


def test_resolved_row_is_labelled_with_the_rows_own_id() -> None:
    """★ The echo trap. The verb used to return the caller's ARGUMENT as
    ``agent_instance_id``, which was harmless while the only possible hit was
    a direct one. Under this fix it becomes load-bearing: echoing the watch id
    back onto a ledger-keyed row would hand the caller a row labelled with an
    id it is not keyed on -- seeding the NEXT join error while claiming to fix
    this one. The row's own id is returned, and the queried id is reported
    separately so nothing is lost."""
    state = _TwoTableState()
    _report(state)
    state.bind(agent_instance_id=WATCH_ID, agent_session_id=WORKER_SESSION_ID)

    out = session_context_status(state, agent_instance_id=WATCH_ID)
    _check(
        out["agent_instance_id"] == LEDGER_ID,
        "the resolved row is labelled with its OWN ledger id, never the queried watch id",
    )
    _check(
        out["queried_agent_instance_id"] == WATCH_ID,
        "the id the caller actually passed is still reported, so the join is traceable",
    )
    _check(
        out["id_resolution"] == "resolved_via_binding",
        "the response says HOW it resolved, so a direct hit and a join are distinguishable",
    )


def test_an_unknown_id_still_reports_the_honest_gap() -> None:
    """``resolved=False`` must survive as the honest, stable shape for a
    session that genuinely has no report -- the fix must not convert a real
    gap into a confident wrong answer."""
    state = _TwoTableState()
    _report(state)
    out = session_context_status(state, agent_instance_id="agi-watch-nosuchsession")
    _check(out["resolved"] is False, "an id with no binding and no row stays unresolved")
    _check(
        out["id_resolution"] == "unresolved",
        "...and says so explicitly rather than implying a failed join succeeded",
    )
    _check(
        bool(out["resolution_error"]),
        "the honest gap still carries its resolution_error, never an estimated number",
    )


def test_a_binding_without_a_session_id_never_matches_everything() -> None:
    """★ THE NULL TRAP, and the reason this is not merely a filter.

    ``agent_session_id`` is nullable and unindexed on the gauge table: any
    reporter of generation <= 2 wrote NULL there. If an EMPTY join key were
    passed through to a filter, it would match every pre-generation-3 row at
    once -- turning a benign coverage gap into a confident WRONG answer, which
    is strictly worse than the defect being fixed. An empty key must
    short-circuit to no-match before any query runs.
    """
    state = _TwoTableState()
    _report(state, agent_session_id=None)          # a stale reporter: NULL join key
    state.bind(agent_instance_id=WATCH_ID, agent_session_id="")  # and an empty binding

    out = session_context_status(state, agent_instance_id=WATCH_ID)
    _check(
        out["resolved"] is False,
        "an EMPTY join key resolves to nothing — it never matches a NULL-keyed row",
    )
    # ★ THE DISCRIMINATING ASSERTION, added after the short-circuit-removal
    # mutation SURVIVED the outcome check above. Asserting only the OUTCOME is
    # decorative here: this fake compares values in Python, where "" never
    # equals None, so it reports "no match" whether or not the guard exists.
    # The guard's actual contract is that an empty key NEVER REACHES A QUERY,
    # which is observable and backend-independent. Counting the queries tests
    # the guard; checking the result tests only this fake's equality operator.
    by_session = [q for q in state.gauge_queries if "agent_session_id" in q]
    _check(
        by_session == [],
        "an empty join key is short-circuited BEFORE any query is issued "
        "(kills the removed-guard mutation)",
    )


def test_ambiguous_join_fails_loud_instead_of_picking_one() -> None:
    """★ There is NO uniqueness constraint on the join column: the gauge
    table's only unique index is on ``agent_instance_id``. So uniqueness is
    not a property the schema provides, and the code must not assume it.
    Picking one of two rows would answer confidently with a coin flip."""
    state = _TwoTableState()
    _report(state)
    _report(state, agent_instance_id="agi-duplicate-claimant")  # same session id
    state.bind(agent_instance_id=WATCH_ID, agent_session_id=WORKER_SESSION_ID)

    raised: VerbError | None = None
    try:
        session_context_status(state, agent_instance_id=WATCH_ID)
    except VerbError as exc:
        raised = exc
    _check(raised is not None, "two rows sharing one session id FAIL LOUD, never coin-flip")
    _check(
        raised is not None and raised.code == "ambiguous_agent_session_id",
        "the ambiguity carries its own stable error token",
    )


def test_a_derivation_would_have_been_wrong() -> None:
    """★ THE DECOY the fix must not take, pinned with the real seat shape.

    This lane's session id happens to read ``"ases-" + ledger_id``, so code
    that sliced the prefix would look correct here and pass its tests. The
    SEAT's id does not: ``ases-1786663089-37639-3748`` contains no ledger id
    at all. This case therefore only resolves if the mapping is genuinely
    READ from storage. A prefix-derivation implementation fails this test and
    passes every other one in this file -- which is precisely why it exists.
    """
    state = _TwoTableState()
    _report(
        state, agent_instance_id=SEAT_LEDGER_ID, agent_session_id=SEAT_SESSION_ID,
        current_tokens=202_243,
    )
    state.bind(agent_instance_id=SEAT_WATCH_ID, agent_session_id=SEAT_SESSION_ID)

    out = session_context_status(state, agent_instance_id=SEAT_WATCH_ID)
    _check(
        out["resolved"] is True,
        "a session id that ENCODES NO LEDGER ID still resolves (storage read, not derivation)",
    )
    _check(
        out["agent_instance_id"] == SEAT_LEDGER_ID,
        "...to the correct ledger-keyed row",
    )
    _check(
        out["current_tokens"] == 202_243,
        "...carrying that row's own measurement",
    )


def test_unresolved_shape_still_carries_every_key() -> None:
    """The pre-existing contract: a caller must not KeyError its way through
    a legitimate ``resolved: false``. The two new keys must be present on
    BOTH shapes or the fix breaks that guarantee."""
    state = _TwoTableState()
    _report(state)
    resolved = session_context_status(state, agent_instance_id=LEDGER_ID)
    unresolved = session_context_status(state, agent_instance_id="agi-absent")
    _check(
        set(resolved) == set(unresolved),
        "the resolved and unresolved shapes still carry an IDENTICAL key set",
    )


def main() -> int:
    print("session_context_status watch-id join smoke (GAU-07)")
    for fn in (
        test_watch_id_resolves_to_the_ledger_keyed_row,
        test_direct_ledger_lookup_is_unchanged,
        test_resolved_row_is_labelled_with_the_rows_own_id,
        test_an_unknown_id_still_reports_the_honest_gap,
        test_a_binding_without_a_session_id_never_matches_everything,
        test_ambiguous_join_fails_loud_instead_of_picking_one,
        test_a_derivation_would_have_been_wrong,
        test_unresolved_shape_still_carries_every_key,
    ):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
