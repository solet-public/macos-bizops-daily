#!/usr/bin/env python3
"""Unit smoke for the L4c SELF-NOTICE leg (`sweep_rotation_self_notice`,
`BandEdgeLatch`, the capacity band, and the TTL-aware break-even) --
2026-08-17, the landing that makes a session's own context size reach the
session itself rather than only its steward.

EVERY TEST BELOW NAMES THE MUTATION IT WOULD CATCH, because a green that
cannot name what it would have caught is decoration. The wave's standing rule
is to be able to finish "this number would also be this number if ______", and
where a single assertion could not exclude the alternative, a second assertion
that DOES exclude it sits beside it (see the blind-spot test, which pins both
instruments rather than only the one it wants).

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/rotation_self_notice_smoke.py
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.llm.agent_messaging.schema import TABLE_AGENT_MESSAGE  # noqa: E402

from agent_messaging_plugin import rotation_notice_retention as rnr  # noqa: E402
from agent_messaging_plugin import rotation_self_notice as rsn  # noqa: E402
from agent_messaging_plugin import rotation_thresholds as rt  # noqa: E402
from agent_messaging_plugin import session_context_status_store as scss  # noqa: E402
from agent_messaging_plugin import session_sweep as ss  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.rotation_notice_retention import (  # noqa: E402
    prune_rotation_notices,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_CONTEXT_STATUS,
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


# ---------------------------------------------------------------------------
# Fakes. Deliberately minimal and deliberately OBSERVABLE: the point of several
# tests below is what the leg DID NOT do, which a fake that only records
# successes cannot show.
# ---------------------------------------------------------------------------


class _FakeBinding:
    """The binding fields the leg actually reads, and no more.

    ★ IT GREW ON 2026-08-19 (GAU-06) AND THE GROWTH IS THE POINT. Until the
    durable half existed this fake needed one attribute, because the leg only
    ever asked for ``bridge_id``. The persist-first route addresses the
    recipient by identity, so a fake that still carried only a bridge id would
    have made every new test fail on AttributeError rather than on behaviour --
    or, worse, pass against a leg that had quietly stopped addressing anybody.
    """

    def __init__(
        self,
        bridge_id: str,
        *,
        agent_instance_id: str = "agi-recipient",
        agent_id: str = "claude_code",
        session_label: str = "lane-under-test",
        agent_session_id: str = "ases-recipient",
        created_at: str = "",
    ) -> None:
        self.bridge_id = bridge_id
        self.agent_instance_id = agent_instance_id
        self.agent_id = agent_id
        self.session_label = session_label
        self.agent_session_id = agent_session_id
        self.created_at = created_at

    @property
    def is_watcher(self) -> bool:
        """Mirrors ``BridgeBinding.is_watcher`` -- derived from the id, never set.

        Deriving it here rather than accepting a flag keeps the fake honest: a
        test cannot declare a session watcher-held while giving it a bridge-held
        id, which is a state the real registry cannot produce.
        """
        return self.agent_instance_id.startswith("agi-watch-")


class _FakeRegistry:
    """Resolves only the instance ids it was told about -- everything else
    misses, which is exactly the watcher-held-worker shape the leg must count
    rather than swallow."""

    def __init__(
        self,
        known: set[str],
        *,
        bindings: list[_FakeBinding] | None = None,
    ) -> None:
        self._known = known
        self._bindings = {binding.agent_instance_id: binding for binding in bindings or []}
        self.lookups: list[str] = []

    def resolve_by_agent_instance_id(self, agent_instance_id: str) -> _FakeBinding | None:
        self.lookups.append(agent_instance_id)
        if agent_instance_id in self._bindings:
            return self._bindings[agent_instance_id]
        if agent_instance_id in self._known:
            return _FakeBinding(
                f"bridge-for-{agent_instance_id}",
                agent_instance_id=agent_instance_id,
            )
        return None

    def list_agent_ids(self) -> dict[str, list[_FakeBinding]]:
        grouped: dict[str, list[_FakeBinding]] = {}
        for binding in self._bindings.values():
            grouped.setdefault(binding.agent_id, []).append(binding)
        return grouped


class _FakeBridgeManager:
    def __init__(self, *, order: list[str] | None = None, fail: bool = False) -> None:
        self.appended: list[tuple[str, str, str]] = []
        self._order = order
        self._fail = fail

    def append_event(
        self,
        bridge_id: str,
        event: str,
        prose: str,
        meta: dict[str, object],
    ) -> None:
        if self._order is not None:
            self._order.append("append")
        if self._fail:
            raise RuntimeError("bridge queue is full")
        self.appended.append((bridge_id, event, prose))


class _FakeMessagingService:
    """Records what the DURABLE half was asked to persist (GAU-06 G2).

    Records the request OBJECT, not a rendering of it: every assertion about
    sender identity, recipient addressing and the ``important`` flag reads the
    same value the real service would, so a field silently dropped at the call
    site cannot pass here by being absent from a summary string.
    """

    def __init__(self, *, order: list[str] | None = None, fail: bool = False) -> None:
        self.sent: list[Any] = []
        self._order = order
        self._fail = fail

    def peer_send(self, request: Any) -> object:
        if self._order is not None:
            self._order.append("persist")
        if self._fail:
            raise RuntimeError("durable store is unreachable")
        self.sent.append(request)
        # The result must carry a thread_id: the writer prunes the thread it
        # just wrote to, so a result object without one is not a stand-in for
        # what the service returns -- it is a fixture that cannot reach the
        # retention path at all.
        return SimpleNamespace(
            thread_id=f"agt-rotation-{request.peer_agent_instance_id}",
            message_id="agm-fake",
            cursor=1,
        )


class _FakeState:
    """Stands in for StateManagementInterface at the calls the leg makes.

    ★ TABLE-AWARE ON PURPOSE, and it did not start that way. Until GAU-01(c)
    the leg made exactly ONE query, so this fake could ignore the spec and hand
    back its gauge rows unconditionally. Then (c) added a SECOND query -- the
    live lifecycle rows -- and the old fake answered that one with gauge rows
    too: same list, wrong table, no error. Every test stayed GREEN, because a
    gauge row carries no `report_by`, so the liveness predicate read False and
    the bound behaved exactly as it had before. The suite would have gone on
    passing while testing nothing about the change.

    That is the fixture-degradation shape a widened interface produces: the fake
    does not fail, it just stops corresponding to the thing it stands in for.
    So this now DISPATCHES on the table and RAISES on any other -- a third query
    added later must break this fake loudly rather than be served a plausible
    wrong answer.
    """

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        lifecycle_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._rows = [dict(row) for row in rows]
        for index, row in enumerate(self._rows):
            row.setdefault("id", f"scs-{index:06d}")
            row.setdefault("created_at", f"2026-08-01T00:00:00.{index:06d}")
        self._lifecycle_rows = lifecycle_rows if lifecycle_rows is not None else []
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.deletes: list[tuple[Any, Any, Any]] = []
        self.context_query_state_reads = 0
        self.context_ordered_reads = 0

    def query_state(self, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
        # The real envelope shape, taken from `state_results.require_records`
        # rather than assumed: `action_status` (NOT `status`) must be the
        # literal "completed", and records live under `data.records`. An
        # earlier draft of this fake used {"status", "records"} and the leg
        # raised StateOperationError -- which is the correct fail-loud
        # behaviour, and is why these tests are run against the real reader
        # instead of a hand-rolled row list.
        table = spec.get("table")
        if table == TABLE_SESSION_CONTEXT_STATUS:
            self.context_query_state_reads += 1
            records = list(self._rows)
        elif table == TABLE_MANAGED_SESSION:
            # The `lifecycle_state` filter is APPLIED, not ignored. The caller
            # asks for `live` specifically, and a fake that handed back every
            # lifecycle row regardless would make "a non-live session does not
            # vouch for liveness" untestable -- the assertion would pass on a
            # fixture that never modelled the distinction.
            wanted = spec.get("filters", {}).get("lifecycle_state")
            records = [
                r
                for r in self._lifecycle_rows
                if wanted is None or r.get("lifecycle_state") == wanted
            ]
        else:
            msg = (
                f"_FakeState was asked for table {table!r}, which it does not "
                "model. Add it here deliberately -- serving another table's "
                "rows is how this fixture silently stopped testing the change "
                "it was written for."
            )
            raise AssertionError(msg)
        return {"action_status": "completed", "data": {"records": records}}

    # -- the notice thread, as a cursor-addressable store -------------------
    #
    # ★ MODELLED, NOT STUBBED. The retention path reads a bounded ordered page
    # and then deletes below the cursor it read, so a fake that returned a
    # canned page would let the prune "succeed" against rows it never touched.
    # This keeps real per-thread rows with real monotonic cursors, which is
    # also what makes the paging assertion below mean anything.

    def seed_thread(self, thread_id: str, cursors: list[int]) -> None:
        self.messages.setdefault(thread_id, []).extend(
            {"id": f"agm-{thread_id}-{c}", "thread_id": thread_id, "cursor": c} for c in cursors
        )

    def query_ordered(self, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
        table = spec.get("table")
        if table == TABLE_MANAGED_SESSION:
            return self._query_managed_sessions(spec)
        if table == TABLE_SESSION_CONTEXT_STATUS:
            self.context_ordered_reads += 1
            return self._query_context_statuses(spec)
        if table != TABLE_AGENT_MESSAGE:
            msg = f"_FakeState.query_ordered does not model table {table!r}"
            raise AssertionError(msg)
        thread_id = spec.get("filters", {}).get("thread_id")
        # ★ THE ORDER IS READ FROM THE SPEC, NOT ASSUMED. An earlier version of
        # this fake sorted descending unconditionally, which made the ordering
        # invisible to the tests: a mutation flipping the prune to keep the
        # OLDEST rows SURVIVED the battery, because the fixture handed back the
        # newest either way. A fake that ignores the argument under test is not
        # a smaller version of the real thing, it is a different one.
        order_by = spec.get("order_by") or [["cursor", "desc"]]
        column, direction = order_by[0][0], order_by[0][1]
        rows = sorted(
            self.messages.get(thread_id, []),
            key=lambda r: int(r[column]),
            reverse=direction == "desc",
        )
        limit = spec.get("limit")
        if isinstance(limit, int):
            rows = rows[:limit]
        return {"action_status": "completed", "data": {"records": rows}}

    def _query_managed_sessions(self, spec: dict[str, Any]) -> dict[str, Any]:
        records = self._select_managed_sessions(spec)
        records = self._page_managed_sessions(records, spec)
        return {"action_status": "completed", "data": {"records": records}}

    def _query_context_statuses(self, spec: dict[str, Any]) -> dict[str, Any]:
        filters = spec.get("filters", {})
        records = [
            row for row in self._rows
            if self._matches_context_filters(row, filters)
            and (bool(spec.get("include_deleted")) or int(row.get("is_deleted", 0)) == 0)
        ]
        return {
            "action_status": "completed",
            "data": {"records": self._page_created_rows(records, spec)},
        }

    @staticmethod
    def _matches_context_filters(row: dict[str, Any], filters: dict[str, Any]) -> bool:
        for column, wanted in filters.items():
            actual = row.get(column)
            if isinstance(wanted, dict):
                if wanted.get("op") != "gte" or "value" not in wanted:
                    raise AssertionError(f"unsupported context filter: {column}={wanted!r}")
                try:
                    matches = rsn.to_naive_utc(actual) >= rsn.to_naive_utc(wanted["value"])
                except (TypeError, ValueError):
                    # A DATETIME provider cannot return this shape through a
                    # range predicate; the active-id walk below still carries
                    # it to the production semantic recheck.
                    matches = False
                if not matches:
                    return False
            elif isinstance(wanted, list):
                if actual not in wanted:
                    return False
            elif actual != wanted:
                return False
        return True

    def _select_managed_sessions(
        self,
        spec: dict[str, Any],
    ) -> list[dict[str, Any]]:
        filters = spec.get("filters", {})
        return [
            row
            for row in self._lifecycle_rows
            if all(row.get(column) == wanted for column, wanted in filters.items())
            and (bool(spec.get("include_deleted")) or int(row.get("is_deleted", 0)) == 0)
        ]

    @staticmethod
    def _page_managed_sessions(
        records: list[dict[str, Any]],
        spec: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return _FakeState._page_created_rows(records, spec)

    @staticmethod
    def _page_created_rows(
        records: list[dict[str, Any]],
        spec: dict[str, Any],
    ) -> list[dict[str, Any]]:
        order_by = spec.get("order_by")
        expected_order = [["created_at", "asc"], ["id", "asc"]]
        if order_by != expected_order:
            msg = (
                "_FakeState.query_ordered models paged rows only in "
                f"its paged lifecycle order; got {order_by!r}"
            )
            raise AssertionError(msg)
        records.sort(key=lambda row: (str(row["created_at"]), str(row["id"])))
        after = spec.get("after")
        if after is not None:
            cursor = (str(after[0]), str(after[1]))
            records = [row for row in records if (str(row["created_at"]), str(row["id"])) > cursor]
        limit = spec.get("limit")
        if isinstance(limit, int):
            records = records[:limit]
        return records

    def delete_records(self, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
        table = spec.get("table")
        if table != TABLE_AGENT_MESSAGE:
            msg = f"_FakeState.delete_records does not model table {table!r}"
            raise AssertionError(msg)
        filters = spec.get("filters", {})
        thread_id = filters.get("thread_id")
        predicate = filters.get("cursor", {})
        cutoff = predicate.get("value") if isinstance(predicate, dict) else None
        kept = [
            r
            for r in self.messages.get(thread_id, [])
            if not (isinstance(cutoff, int) and int(r["cursor"]) < cutoff)
        ]
        deleted = len(self.messages.get(thread_id, [])) - len(kept)
        self.messages[thread_id] = kept
        self.deletes.append((thread_id, cutoff, spec.get("soft_delete")))
        # The real envelope nests the count under data.result.deleted -- taken
        # from state_results.require_deleted, not guessed. A flat
        # {"deleted": n} reads as ZERO through that helper, which would make a
        # prune that worked look like a prune that found nothing.
        return {
            "action_status": "completed",
            "data": {"result": {"deleted": deleted}},
        }

    def page_after(self, thread_id: str, after_cursor: int) -> list[int]:
        """What a client holding ``after_cursor`` sees next, oldest-first."""
        return sorted(
            int(r["cursor"])
            for r in self.messages.get(thread_id, [])
            if int(r["cursor"]) > after_cursor
        )


def _measured_age_or_none(row: dict[str, Any]) -> float | None:
    """`_measured_age_seconds` against the fixed sweep clock."""
    return rsn._measured_age_seconds(row, clock=_SWEEP_NOW)


def _first_prose(bridges: _FakeBridgeManager) -> str:
    """The first delivered notice's text, or "" when nothing was delivered.

    Deliberately NOT `_first_prose(bridges)`. A mutation that stops the leg
    delivering at all would make that raise IndexError, which aborts the run
    and hides every later test's verdict -- measured, while mutation-testing
    this very suite. A missing notice must read as a clean FAIL on the
    assertion that wanted it, not as a crash.
    """
    return bridges.appended[0][2] if bridges.appended else ""


def _row(
    agent_instance_id: str = "agi-seat",
    *,
    current_tokens: int,
    ceiling: int = 1_000_000,
    model: str = "claude-opus-5",
    cache_cold: int = 0,
    overage: int = 0,
    measured_at: str = "2026-08-17T22:59:00",
) -> dict[str, Any]:
    """A gauge row. `measured_at` defaults to one minute before `_SWEEP_NOW`
    so rows are LIVE by default; the staleness tests override it."""
    return {
        "agent_instance_id": agent_instance_id,
        "model": model,
        "current_tokens": current_tokens,
        "ceiling": ceiling,
        "cache_cold": cache_cold,
        "cache_overage_signature": overage,
        "measured_at": measured_at,
    }


_SWEEP_NOW = datetime(2026, 8, 17, 23, 0, 0, tzinfo=UTC)


def _sweep_full(
    rows: list[dict[str, Any]],
    *,
    known: set[str] | None = None,
    latch: rsn.BandEdgeLatch | None = None,
    now: datetime | None = None,
    lifecycle_rows: list[dict[str, Any]] | None = None,
    order: list[str] | None = None,
    persist_fails: bool = False,
    append_fails: bool = False,
    service: _FakeMessagingService | None = None,
    omit_service: bool = False,
    bindings: list[_FakeBinding] | None = None,
) -> tuple[rsn.SelfNoticeCounts, _FakeBridgeManager, _FakeMessagingService]:
    """One sweep with BOTH halves visible to the caller.

    ``omit_service`` exercises the unwired guard deliberately, so that the
    "returns an empty tally" contract is asserted by a test rather than
    discovered by a fixture that forgot an argument -- which would make every
    zero-expecting assertion in this file pass for the wrong reason.
    """
    registry = _FakeRegistry(
        known if known is not None else {r["agent_instance_id"] for r in rows},
        bindings=bindings,
    )
    bridges = _FakeBridgeManager(order=order, fail=append_fails)
    svc = service or _FakeMessagingService(order=order, fail=persist_fails)
    result = rsn.sweep_rotation_self_notice(
        _FakeState(rows, lifecycle_rows=lifecycle_rows),  # type: ignore[arg-type]
        now=now or _SWEEP_NOW,
        peer_registry=registry,  # type: ignore[arg-type]
        bridge_manager=bridges,  # type: ignore[arg-type]
        agent_messaging_service=None if omit_service else svc,  # type: ignore[arg-type]
        latch=latch,
    )
    return result, bridges, svc


def _sweep(
    rows: list[dict[str, Any]],
    *,
    known: set[str] | None = None,
    latch: rsn.BandEdgeLatch | None = None,
    now: datetime | None = None,
    lifecycle_rows: list[dict[str, Any]] | None = None,
) -> tuple[rsn.SelfNoticeCounts, _FakeBridgeManager]:
    counts, bridges, _ = _sweep_full(
        rows,
        known=known,
        latch=latch,
        now=now,
        lifecycle_rows=lifecycle_rows,
    )
    return counts, bridges


# ---------------------------------------------------------------------------


def test_the_300k_to_500k_blind_spot_is_covered() -> None:
    """CATCHES: keying the leg on `rotation_due`/fraction instead of the band.

    350,000 on a 1M ceiling is the defect's exact middle -- the band saturated
    at `warm_immediate` 50,000 tokens ago, while `is_rotation_due` is still
    False because 0.35 < 0.5. A leg gated on the fraction is SILENT here, and
    this is the range in which the 2026-08-17 seat burned its 300K.

    "This count would also be 1 if the leg keyed on the fraction" -- no, but
    THE INPUT THAT PROVED IT HAS MOVED (GAU-08, 2026-08-18). This test used to
    pin `is_rotation_due` False at this same 350,000 and rest on the two
    instruments disagreeing there. The union fixed `rotation_due`, so they now
    AGREE at 350,000 and that input can no longer tell a band-keyed leg from a
    rotation_due-keyed one. Leaving the old assertion would have left a test
    whose name still claimed a discrimination it had quietly stopped making --
    so the discriminator is re-established below on the one population where
    the two predicates still diverge, rather than deleted.

    They diverge on a SMALL CEILING, and only there. On a 1M ceiling the union
    reduces to the band (0.5 of 1M is 500,000, already deep inside
    `warm_immediate`). On a 200,000-token window the fraction fires at 100,000
    while the model-blind bands still say `warm_keep` -- so a leg keyed on
    `rotation_due` would notify there and a band-keyed leg must stay SILENT.
    That is now the assertion carrying this test's name.
    """
    counts, bridges = _sweep([_row(current_tokens=350_000)])
    _check(
        counts.appended == 1 and counts.unroutable == 0,
        "350K on a 1M ceiling notifies (the band says warm_immediate)",
    )
    _check(
        rt.is_rotation_due(model="claude-opus-5", current_tokens=350_000, cache_cold=False),
        "...and since GAU-08 `rotation_due` AGREES at that same 350K -- the "
        "gap this test was written against is closed, which is why it can no "
        "longer be the discriminator",
    )
    _check(
        "warm_immediate" in _first_prose(bridges), "the delivered notice names the band it fired on"
    )

    # ★ THE SURVIVING DISCRIMINATOR. rotation_due is True here and the band is
    # not actionable, so a leg keyed on rotation_due notifies and a band-keyed
    # leg does not. Nothing else in this file separates the two designs any
    # more.
    small_counts, small_bridges = _sweep(
        [
            _row(current_tokens=100_000, ceiling=200_000, model="claude-haiku-4-5"),
        ]
    )
    _check(
        rt.is_rotation_due(model="claude-haiku-4-5", current_tokens=100_000, cache_cold=False),
        "100K on a 200K ceiling IS rotation-due -- it is that model's own "
        "halfway point, and the union keeps the fraction reachable there",
    )
    _check(
        rt.rotation_band(100_000, cache_cold=False)[0] == "warm_keep",
        "...while the model-blind economics band there is still warm_keep",
    )
    _check(
        small_counts.appended == 0 and small_bridges.appended == [],
        "...and the leg stays SILENT -- nothing counted AND nothing appended "
        "to any bridge: it follows the BAND, not rotation_due. A leg keyed on "
        "rotation_due would have notified this session",
    )


def test_a_carry_on_verdict_says_nothing_at_all() -> None:
    """CATCHES: widening ROTATION_SELF_NOTICE_BANDS to include `warm_keep`.

    A channel is trained by its first message. A leg that fires on the
    keep-working verdict would open every session's channel with something it
    must not act on, and the latch would not save it -- the latch suppresses
    REPEATS, never the first delivery.
    """
    counts, bridges = _sweep([_row(current_tokens=120_000)])
    _check(
        counts == rsn.SelfNoticeCounts() and not bridges.appended,
        "a warm_keep session is not notified, and nothing is appended",
    )


def test_escalation_across_bands_is_never_suppressed() -> None:
    """CATCHES: swapping BandEdgeLatch for NoticeLatch (or keying the latch on
    the session alone).

    This is the mutation that matters most, because it fails in the direction
    that hurts: a session-keyed latch treats 150K -> 200K -> 300K as ONE
    unbroken episode, delivers the mildest notice, and then stays silent
    through every escalation. The suppressed notice would always be the more
    urgent one.
    """
    latch = rsn.BandEdgeLatch()
    t0 = datetime(2026, 8, 17, 23, 0, 0, tzinfo=UTC)
    first_counts, _ = _sweep([_row(current_tokens=180_000)], latch=latch, now=t0)
    # +2 min: well inside the 20-minute floor, but a DIFFERENT band.
    t1 = datetime(2026, 8, 17, 23, 2, 0, tzinfo=UTC)
    second_counts, bridges = _sweep([_row(current_tokens=260_000)], latch=latch, now=t1)
    _check(first_counts.appended == 1, "the warm_task_boundary crossing notifies")
    _check(
        second_counts.appended == 1,
        "the escalation to warm_safe_checkpoint notifies too, 2 minutes "
        "later and deep inside the repeat floor -- a new band is new "
        "information and the floor must not apply to it",
    )
    _check(
        "warm_safe_checkpoint" in _first_prose(bridges),
        "the second notice carries the NEW band, not the stale one",
    )


def test_the_same_band_is_floored_at_twenty_minutes() -> None:
    """CATCHES: dropping the floor (re-notifies every 300s tick, which trains
    the reader to filter the channel) and equally CATCHES making the floor a
    blanket time gate (which would have failed the previous test).

    The pair of tests is the point: neither alone pins the design. This one
    alone passes for a leg that never notifies twice at all.
    """
    latch = rsn.BandEdgeLatch()
    row = [_row(current_tokens=260_000)]
    t0 = datetime(2026, 8, 17, 23, 0, 0, tzinfo=UTC)
    first_counts, _ = _sweep(row, latch=latch, now=t0)
    under_counts, _ = _sweep(row, latch=latch, now=datetime(2026, 8, 17, 23, 5, 0, tzinfo=UTC))
    over_counts, _ = _sweep(row, latch=latch, now=datetime(2026, 8, 17, 23, 21, 0, tzinfo=UTC))
    _check(
        (first_counts.appended, under_counts.appended, over_counts.appended) == (1, 0, 1),
        "same band: notified, silent at +5min (inside the floor), notified "
        f"again at +21min (past ROTATION_SELF_NOTICE_FLOOR_S="
        f"{rsn.ROTATION_SELF_NOTICE_FLOOR_S}s)",
    )


def test_an_unresolvable_session_is_counted_not_swallowed() -> None:
    """CATCHES: `continue`-ing past a registry miss instead of counting it.

    The leg's own blind spot -- a watcher-held worker whose gauge row is keyed
    on its LEDGER id while its binding is keyed on its WATCH id -- must appear
    as a NUMBER. A silent skip reads identically to a healthy run, and this
    codebase has already paid for that once with two legs that ran never.

    "The notified count would also be 1 if the miss were swallowed" -- which is
    why the assertion pins `unreachable` too. The pair is the discriminator.
    """
    rows = [
        _row("agi-seat", current_tokens=350_000),
        _row("agi-ccafb17b9ad89af3fb8998081fbcbd23", current_tokens=350_000),
    ]
    counts, bridges = _sweep(rows, known={"agi-seat"})
    _check(
        (counts.appended, counts.unroutable) == (1, 1),
        "one notified, one counted unreachable -- the gap is a number, not a silence",
    )
    _check(len(bridges.appended) == 1, "and exactly one notice was actually delivered")


def test_a_failed_delivery_does_not_latch_the_episode() -> None:
    """CATCHES: latching on ATTEMPT rather than on success.

    An episode silenced by its own delivery failure is the worst available
    outcome: the condition persists, nothing is delivered, and the latch
    guarantees nothing ever will be. Same contract NoticeLatch states.
    """
    latch = rsn.BandEdgeLatch()
    row = [_row("agi-dark", current_tokens=350_000)]
    c1, _ = _sweep(row, known=set(), latch=latch)
    c2, bridges = _sweep(row, known={"agi-dark"}, latch=latch)
    _check(
        (c1.appended, c1.unroutable) == (0, 1), "first tick cannot deliver -- counted unroutable"
    )
    _check(
        (c2.appended, c2.unroutable) == (1, 0),
        "second tick, binding now resolvable, DELIVERS -- the failed "
        "attempt did not latch it into permanent silence",
    )
    _check(len(bridges.appended) == 1, "and the notice really was appended")


def test_capacity_binds_early_on_an_unrecognised_model() -> None:
    """CATCHES: a fail-OPEN tier default -- the exact failure a tier-scaling
    scheme would have introduced.

    An unrecognised model resolves to DEFAULT_CONSERVATIVE_CEILING (100,000),
    so 80,000 is 80% of its window while the economics band still reads
    `warm_keep`. The session must be LOUD, never quiet. The second assertion
    pins that economics alone would have said nothing, so this test cannot pass
    for a leg that simply notifies on everything.
    """
    ceiling = rt.resolve_ceiling("some-model-shipped-after-this-landing")
    _check(
        ceiling == rt.DEFAULT_CONSERVATIVE_CEILING,
        f"an unknown model still falls back to {rt.DEFAULT_CONSERVATIVE_CEILING:,}",
    )
    counts, bridges = _sweep(
        [
            _row(
                current_tokens=80_000,
                ceiling=ceiling,
                model="some-model-shipped-after-this-landing",
            )
        ]
    )
    _check(counts.appended == 1, "80K against a conservative 100K ceiling NOTIFIES")
    _check(
        rt.rotation_band(80_000, cache_cold=False)[0] == "warm_keep",
        "...while the ECONOMICS band alone would have said keep working -- "
        "so it is the capacity axis carrying this, not a leg that shouts at "
        "everything",
    )
    _check(
        "capacity_approaching" in _first_prose(bridges),
        "and the notice names capacity as the reason",
    )


def test_capacity_never_preempts_economics_on_a_1m_ceiling() -> None:
    """CATCHES: setting CAPACITY_BAND_* low enough to fire on 1M models.

    The capacity axis exists for small windows. If it started leading on 1M
    sessions it would relabel the ratified economics policy under a different
    name, and the bands people ratified would stop being the ones that speak.
    """
    for tokens in (150_000, 200_000, 300_000, 499_999, 750_000 - 1):
        verdict = rt.rotation_surface_verdict(
            current_tokens=tokens,
            ceiling=1_000_000,
            cache_cold=False,
        )
        _check(
            verdict.effective_band == verdict.economics_band,
            f"at {tokens:,} on a 1M ceiling the ECONOMICS band leads ({verdict.effective_band})",
        )


def test_the_break_even_is_tier_invariant() -> None:
    """CATCHES: reintroducing per-tier band scaling.

    The whole 2026-08-17 finding in one assertion: price cancels out of
    `C > H + kH/N`, so the same C and N give the same verdict on every model.
    A tier-scaled implementation makes these disagree by construction.
    """
    verdicts = {
        model: rt.rotation_surface_verdict(
            current_tokens=224_840,
            ceiling=rt.resolve_ceiling(model),
            cache_cold=False,
        ).economics_band
        for model in ("claude-fable-5", "claude-opus-5", "claude-sonnet-5")
    }
    _check(
        len(set(verdicts.values())) == 1,
        f"224,840 reads as the SAME economics band on every 1M-ceiling tier: {verdicts}",
    )
    _check(
        rt.clearing_wins(224_840, 30) is rt.clearing_wins(224_840, 30),
        "clearing_wins takes no model argument at all -- there is no tier "
        "for a future edit to scale",
    )


def test_the_overage_ttl_moves_the_break_even_and_moves_it_down() -> None:
    """CATCHES: wiring the overage premium backwards (12.5 vs 20).

    A collapsed cache TTL makes the rewrite CHEAPER, so clearing wins SOONER.
    Getting the direction wrong would silence sessions in overage -- the state
    in which context is most expensive to carry.

    "This would also pass if both multipliers were equal" -- no: the strict
    inequality on the horizon excludes that.
    """
    nominal = rt.break_even_horizon(300_000)
    overage = rt.break_even_horizon(300_000, overage=True)
    assert nominal is not None and overage is not None
    _check(
        overage < nominal,
        f"overage horizon {overage:.1f} < nominal {nominal:.1f} -- clearing "
        "wins sooner under a collapsed TTL, not later",
    )
    # ★ THE DISCRIMINATING SIZE, DERIVED (GAU-05, 2026-08-19). At N=25 the two
    # premiums put their thresholds at 1.5H (overage) and 1.8H (nominal), so
    # only a context strictly BETWEEN them can win under one and lose under the
    # other. This was the literal 167,000, correct against the then-H of
    # 110,702 by 947 tokens; the re-measurement to 146,139 moved both
    # thresholds up and left 167,000 BELOW BOTH, where it loses either way --
    # the assertion would have gone red, and "fix the number until it is green
    # again" would have re-stranded it at the next measurement. It would have,
    # too: the 2026-08-19 rehydration re-measurement (H 81,889) puts the pair at
    # 122,834 and 147,400, so 167,000 is now ABOVE BOTH and wins either way --
    # stranded a second time, in the opposite direction, inside the same day.
    # Derived from H so it travels with the constant instead.
    overage_threshold = rt.POLICY_H_TOKENS * 1.5
    nominal_threshold = rt.POLICY_H_TOKENS * 1.8
    discriminating = int((overage_threshold + nominal_threshold) / 2)
    _check(
        overage_threshold < discriminating < nominal_threshold,
        f"the probe {discriminating:,} lands strictly between the overage "
        f"threshold ({overage_threshold:,.0f}) and the nominal one "
        f"({nominal_threshold:,.0f}) -- without that window there is nothing "
        "to discriminate",
    )
    _check(
        rt.clearing_wins(discriminating, 25, overage=True)
        and not rt.clearing_wins(discriminating, 25),
        f"{discriminating:,} at N=25 wins under overage and loses at the "
        "1-hour TTL -- the two premiums are genuinely different thresholds",
    )
    _check(
        rt.write_premium_multiplier(overage=False) == 20.0,
        "the default premium is unchanged for every pre-existing caller",
    )


def test_the_notice_states_one_horizon_not_two() -> None:
    """CATCHES: the regression this landing already made once -- the band's
    embedded horizon computed at the nominal premium while the notice's own
    line used the overage-aware one, rendering "~4" and "~3" for one quantity.

    Two sources that can disagree about the same fact teach the reader to trust
    neither, so the assertion is on the RENDERED prose, not on the two
    functions agreeing in isolation.
    """
    row = _row(current_tokens=606_142, overage=1)
    verdict = rsn._gauge_verdict(row)
    assert verdict is not None, "a well-formed row must produce a verdict"
    prose = rsn._self_notice_prose(row, verdict)
    # ★ DERIVED, AND SCOPED TO THE BAND LINE (GAU-05, 2026-08-19). Two repairs
    # to one assertion, both of which the H re-measurement forced:
    #
    # 1. The expected figures are now COMPUTED from the same function the notice
    #    uses. They were the literals "~3" present / "~4" absent, correct at
    #    H=110,702 and simply wrong at 146,139 -- and "edit the literals until
    #    green" would have preserved a passing test that had stopped tracking
    #    the quantity it names.
    # 2. The scan is scoped to the BAND line. The old scan swept the whole
    #    notice for `~N` tokens, which also caught the TTL note's "~5 min" -- a
    #    DURATION, not a call horizon. That made the token set unreadable and,
    #    worse, meant a genuine second horizon rendering as "~5" would have been
    #    indistinguishable from the TTL prose. A test that cannot tell its
    #    subject from its surroundings is the failure this whole file is about.
    band_line = next(line for line in prose.splitlines() if line.startswith("Band:"))
    own = rt.break_even_horizon(606_142, overage=True)
    nominal = rt.break_even_horizon(606_142)
    assert own is not None and nominal is not None
    _check(
        f"~{own:.0f}" in band_line,
        f"the band line states THIS session's overage-aware horizon (~{own:.0f})",
    )
    _check(
        f"~{nominal:.0f}" not in band_line,
        f"...and NOT the nominal-premium figure (~{nominal:.0f}) -- one "
        "quantity, one number, one source",
    )


def test_the_notice_states_the_floor_beside_the_absolute_band() -> None:
    """GAU-14 (B2): the notice says how much of the number is H, so a reader
    can tell a session with real work behind it from one that was born near
    the band.

    The measured failure this closes: four fresh sessions on 2026-08-18/19 each
    crossed `warm_task_boundary` inside their first quarter hour, and the notice
    gave the recipient no way to distinguish that from 150,000 tokens of work.

    ★ THE ORIGINAL JUSTIFICATION HAS SINCE BEEN PARTLY INVALIDATED, and it is
    rewritten here rather than left standing, because a docstring whose premise
    is false teaches the next reader the wrong thing about why the code exists.
    This test used to argue: "H is 146,139 against a first band of 150,000 --
    3,861 tokens apart -- so the ambiguity is STRUCTURAL, not incidental." The
    2026-08-19 rehydration re-measurement put H at 81,889, i.e. 68,111 below the
    first band, and that sentence is now simply untrue -- a session is no longer
    born a hair under the band.

    The FEATURE survives; its ground is weaker and different. What justifies the
    floor line now is not that sessions are born next to the band, but that a
    reader handed one absolute number CANNOT DECOMPOSE IT unaided: 218,613 tells
    you nothing about how much is unshakeable prefix and how much is work you
    could rotate away. That is true at any H. The four fresh sessions still
    crossed the boundary early -- pickup workload, not H alone, took them there
    -- so the observation stands even though its former explanation does not.

    Asserted on the RENDERED prose and on the ARITHMETIC, not on the presence of
    a phrase: a line that printed the right words with the wrong subtraction
    would pass a substring check and mislead exactly the reader it is for.
    """
    row = _row(current_tokens=218_613)
    verdict = rsn._gauge_verdict(row)
    assert verdict is not None, "a well-formed row must produce a verdict"
    prose = rsn._self_notice_prose(row, verdict)
    floor = rt.POLICY_H_TOKENS
    _check(f"{floor:,}" in prose, f"the notice names H ({floor:,}) explicitly")
    _check(
        f"{218_613 - floor:,}" in prose,
        f"and names what rotating would actually shed ({218_613 - floor:,}) -- "
        "the subtraction, not just the constant",
    )
    _check(
        "clear" in prose and "re-write" in prose,
        "and says WHAT H is (the prefix a clear would have to re-write), so "
        "the number is not an unexplained second figure",
    )


def test_the_floor_line_never_claims_a_per_session_boot_measurement() -> None:
    """★ THE OVERCLAIM THIS LINE MUST NOT MAKE, pinned so a future edit cannot
    quietly introduce it.

    "Your boot floor was H" is FALSE for most sessions and was measured false:
    the lane that wrote this booted at ~44.7K, nowhere near H, and reached the
    band by reading its brief. H is the POST-ROTATION PREFIX -- well defined for
    every session however it got here -- and that is all the notice may assert.
    A notice that told a session what its own boot cost, without anyone having
    measured that session's boot, would be manufacturing a measurement, which is
    the exact pathology GAU-13 and GAU-12 were both filed for.
    """
    prose = rsn._self_notice_prose(
        _row(current_tokens=218_613),
        rsn._gauge_verdict(_row(current_tokens=218_613)),
    )
    lowered = prose.lower()
    for forbidden in ("your boot", "you booted", "boot floor", "born with"):
        _check(
            forbidden not in lowered,
            f"the notice does not claim {forbidden!r} -- no per-session boot "
            "measurement is asserted",
        )


def test_the_below_h_floor_branch_is_reachable_and_says_the_opposite() -> None:
    """★ A BRANCH NOTHING CAN REACH IS NOT A BRANCH. This one is reachable, and
    the test states the ONLY route so a future threshold change that closes it
    turns this red rather than leaving dead prose behind.

    Route: an UNRECOGNISED model resolves to DEFAULT_CONSERVATIVE_CEILING
    (100,000), so `capacity_approaching` fires at 75,000 -- below H (81,889).
    That is the two axes legitimately disagreeing: room is running out while the
    economics still say a clear cannot pay for itself. The floor line must say
    so rather than reporting a negative saving.

    ★ THIS ROUTE IS ONE RE-MEASUREMENT FROM CLOSING, AND THE THREAT IS THIS
    PROJECT'S OWN SUCCESS. The route exists only while `75,000 < H`. That
    margin was 71,139 at the GAU-05 value; the 2026-08-19 rehydration
    re-measurement cut it to 6,889. It is not a comfortable 6,889 either: the
    QUIETEST of the nine pickups that measurement averaged (v26, H_rehyd
    28,294) already implies an H of 71,768 -- BELOW 75,000 -- so a seat
    generation no busier than one we have already observed prices this branch
    dead. Driving rehydration down further is an active programme, not a
    hypothetical, which means the ordinary course of that work will invert this
    fence.

    So the margin is asserted EXPLICITLY below and prints its own distance,
    rather than being left implicit in an assertion that would quietly go red
    with no explanation of what it meant. When it does invert, the correct
    response is NOT to lower the probe until this file is green again: it is to
    decide, deliberately, whether the below-H branch of the notice still has a
    reachable route in production, and to delete the branch if it does not. A
    branch nothing can reach is not a branch -- which is the claim in this
    test's own name.
    """
    ceiling = rt.resolve_ceiling("a-model-nobody-added")
    _check(
        ceiling == rt.DEFAULT_CONSERVATIVE_CEILING,
        "the unknown model still resolves to the conservative ceiling",
    )
    row = _row(current_tokens=75_000, ceiling=ceiling, model="a-model-nobody-added")
    verdict = rsn._gauge_verdict(row)
    assert verdict is not None, "a well-formed row must produce a verdict"
    _check(
        verdict.effective_band in rsn.ROTATION_SELF_NOTICE_BANDS,
        f"this row actually NOTIFIES (band {verdict.effective_band}) -- "
        "otherwise the branch below is unreachable in production",
    )
    _margin = rt.POLICY_H_TOKENS - 75_000
    _check(
        _margin > 0,
        f"...and it is below H by {_margin:,} tokens, which is what makes "
        f"the branch live (H {rt.POLICY_H_TOKENS:,} vs the 75,000 "
        "capacity_approaching point on the conservative ceiling) -- if this "
        "ever reds, see the docstring: the fix is a decision about the "
        "branch, NOT a smaller probe",
    )
    prose = rsn._self_notice_prose(row, verdict)
    _check(
        "BELOW H" in prose,
        "the notice says the session is below H rather than reporting a negative saving",
    )
    _check("cost more than it could save" in prose, "and says what that means for a clear")


def test_the_notice_reports_both_clocks_and_the_lag_between_them() -> None:
    """GAU-14 (D3): the notice says when the reading was PRODUCED, when it was
    OBSERVED, and the gap — not one timestamp standing in for both.

    The measured defect: the seat's two notice paths reported 164,118 "measured
    01:19:31Z" and 153,682 "measured ~01:21Z" — later-but-LOWER. Both were real
    lines of the SAME strictly-monotone transcript in the CORRECT order
    (01:18:57.127Z and 01:17:10.380Z). Nothing disagreed about the measurement;
    each notice reported WHEN IT LOOKED and called that when it was measured.
    The fixture below reproduces that exact 34-second skew.
    """
    row = _row(current_tokens=218_613, measured_at="2026-08-19T01:19:31")
    row["reading_at"] = "2026-08-19T01:18:57"
    verdict = rsn._gauge_verdict(row)
    assert verdict is not None, "a well-formed row must produce a verdict"
    prose = rsn._self_notice_prose(row, verdict)
    _check("2026-08-19T01:18:57" in prose, "the notice names when the READING was produced")
    _check("2026-08-19T01:19:31" in prose, "and when it was OBSERVED")
    _check(
        "34s" in prose,
        f"and the LAG between them, computed not transcribed -- got: "
        f"{prose.splitlines()[-1][:120]}",
    )


def test_a_missing_reading_at_says_unknown_rather_than_implying_zero() -> None:
    """★ THE BRANCH MOST READERS WILL MEET FIRST, since every reporter
    generation predating the column sends nothing.

    Silence here would leave a lone timestamp and the same wrong inference the
    field exists to prevent. The notice must say the lag is UNKNOWN — and must
    not say, or let a reader infer, that it is zero.
    """
    row = _row(current_tokens=218_613, measured_at="2026-08-19T01:19:31")
    verdict = rsn._gauge_verdict(row)
    assert verdict is not None, "a well-formed row must produce a verdict"
    prose = rsn._self_notice_prose(row, verdict)
    _check("NOT REPORTED" in prose, "the notice says the reading's own time was NOT REPORTED")
    _check(
        "it is not zero" in prose,
        "...and says explicitly that the unknown lag is NOT zero, which is "
        "the inference a lone observer timestamp invites",
    )
    _check("Reading produced at" not in prose, "and does not claim a reading time it does not have")


def test_nothing_in_the_leg_can_drive_a_session() -> None:
    """CATCHES: a future edit 'fixing' the inconsistency by adding the
    `drive_on_delivery` call its three sibling notify paths all make.

    That call injects a turn into the recipient's host driver. There is a
    standing ruling that no agent sits in the injection path for a context
    clear, so its ABSENCE here is load-bearing rather than an oversight -- and
    an oversight is exactly what it looks like to someone tidying up.

    A behavioural property cannot be enumerated by scanning source, so this
    replaces the real function with one that RAISES and then runs the leg for
    real: if anything on the delivery path drives, the sweep cannot come back
    clean.
    """
    # ⚠️ THE PATCH TARGET MOVED WITH THE CODE, AND A STALE ONE PASSES SILENTLY.
    # Before the 2026-08-17 extraction this patched `ss.drive_on_delivery` and
    # ran the leg out of `session_sweep`. The leg now lives in its own module,
    # so patching `session_sweep`'s binding would affect NOTHING the code under
    # test resolves -- the sweep would run, nothing would explode, and this test
    # would report PASS while testing literally nothing. Moving code fails no
    # test, which is what makes that failure mode silent.
    #
    # Two independent guards replace it, because the leg does not import
    # `drive_on_delivery` at all and there is therefore no binding in its
    # namespace to poison:
    #
    #   1. STRUCTURAL — the leg's module has no such attribute. It cannot call
    #      what it never imported, and if a future edit adds the import to make
    #      the call, this assertion fails at that moment.
    #   2. BEHAVIOURAL — the DEFINING module's function is replaced by a
    #      landmine and the leg is then run for real, so any late/dynamic
    #      lookup (`session_lifecycle_verbs.drive_on_delivery(...)`) still
    #      detonates.
    #
    # Neither alone is sufficient: (1) is a source fact, and (2) cannot see a
    # from-import that binds a reference before the patch lands.
    _check(
        not hasattr(rsn, "drive_on_delivery"),
        "STRUCTURAL: the self-notice module has no drive_on_delivery "
        "binding at all -- it cannot inject a turn it never imported",
    )

    from agent_messaging_plugin import session_lifecycle_verbs as slv

    original = slv.drive_on_delivery

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the self-notice leg must never drive a session")

    slv.drive_on_delivery = _explode  # type: ignore[assignment]
    detonated: str | None = None
    try:
        counts, bridges = _sweep([_row(current_tokens=350_000)])
    except AssertionError as exc:
        # The landmine going off is a FINDING, not a crash. Left unhandled it
        # propagates out of main() and kills the run before the summary, which
        # hides every later test's verdict -- the same crash-is-not-a-failure-
        # report pattern this suite already fixed twice (the IndexError helper,
        # and MUT-8's TypeError). A mutation battery that dies early silently
        # narrows its own coverage, so the detonation is caught and REPORTED.
        detonated = str(exc)
        counts, bridges = rsn.SelfNoticeCounts(), _FakeBridgeManager()
    finally:
        slv.drive_on_delivery = original  # type: ignore[assignment]
    _check(
        detonated is None,
        "BEHAVIOURAL: the leg ran with drive_on_delivery replaced by a "
        "landmine at its DEFINITION site and never tripped it -- catches a "
        "late/dynamic lookup the structural check cannot see"
        + ("" if detonated is None else f" (TRIPPED: {detonated})"),
    )
    _check(
        detonated is None
        and (counts.appended, counts.unroutable) == (1, 0)
        and len(bridges.appended) == 1,
        "...and the notice was still delivered while that landmine was armed -- notice, never act",
    )


def test_an_unusable_row_is_skipped_rather_than_guessed() -> None:
    """CATCHES: defaulting a missing ceiling (e.g. to the conservative floor)
    inside the leg.

    A row with no ceiling means the reporter told us nothing; inventing one
    would answer a capacity question with a number nobody measured, and it
    would do so in the LOUD direction.
    """
    counts, _ = _sweep(
        [
            _row("agi-noceiling", current_tokens=350_000, ceiling=0),
            _row("agi-notokens", current_tokens=0),
        ]
    )
    _check(
        counts == rsn.SelfNoticeCounts(),
        "a row with an unusable ceiling or no token count is skipped "
        "silently -- not notified, and not counted as a coverage gap either",
    )


def test_the_rider_actually_invokes_this_leg() -> None:
    """★ THE REACHABILITY GUARD, and the reason it exists is written into the
    rider's own docstring as a ★.

    CATCHES: the leg being written, tested, and composed into NOTHING.

    This is not a hypothetical mutation. BOTH existing L4 legs landed exactly
    that way -- `sweep_rotation_due_sessions` and `sweep_gauge_coverage` were
    merged tested and invoked by nothing, so they ran never in production until
    a later change composed them. Every unit test above would pass, unchanged
    and green, against a build in which this leg is dead code. "The leg
    delivers notices" would then be a statement about capability, not about
    behaviour.

    So this drives the REAL `AgentMessagingPlugin._run_rotation_surface_sweep`
    against a duck-typed self -- the same wiring-guard shape
    `bridge_lifecycle_sweep_smoke` uses for `_on_sweep_tick` -- and asserts the
    self-notice leg is reached. Chained with that existing guard (which pins
    tick -> rider), this pins tick -> rider -> leg, which is the whole path a
    notice travels on a live sweep.

    It also pins the FAULT ISOLATION in the direction that matters: the two
    steward legs are made to raise, and the self-notice leg must still run.
    Composed inside a shared rider, an unisolated sibling fault would silence
    this leg every tick, and nothing else would report that it had.
    """
    reached: list[str] = []

    def _boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("steward leg fault -- must not cost the self leg its tick")

    # `plugin.py` does `from .rotation_self_notice import sweep_rotation_self_notice`,
    # so the binding the rider resolves lives in PLUGIN's namespace, not in the
    # defining module's. Patching the definer would not reach it. After the
    # extraction this is the ONLY target that works, and it is the same one it
    # was before -- the move did not change where the caller looks.
    import agent_messaging_plugin.plugin as plugin_mod

    p_due = plugin_mod.sweep_rotation_due_sessions
    p_dark = plugin_mod.sweep_gauge_coverage
    p_self = plugin_mod.sweep_rotation_self_notice
    p_stale = plugin_mod.sweep_gauge_staleness

    seen_kwargs: dict[str, object] = {}

    def _spy(*args: object, **kwargs: object) -> rsn.SelfNoticeCounts:
        reached.append("self_notice")
        seen_kwargs.update(kwargs)
        return rsn.SelfNoticeCounts()

    def _stale_spy(*args: object, **kwargs: object) -> int:
        reached.append("gauge_stale")
        return 0

    _sentinel_service = _FakeMessagingService()
    plugin_mod.sweep_rotation_due_sessions = _boom  # type: ignore[assignment]
    plugin_mod.sweep_gauge_coverage = _boom  # type: ignore[assignment]
    plugin_mod.sweep_rotation_self_notice = _spy  # type: ignore[assignment]
    plugin_mod.sweep_gauge_staleness = _stale_spy  # type: ignore[assignment]
    try:
        fake_self = SimpleNamespace(
            _log_self_notice_counts=AgentMessagingPlugin._log_self_notice_counts,  # noqa: SLF001
            _get_state_service=lambda: object(),
            _peer_registry=object(),
            _bridge_manager=object(),
            _rotation_due_latch=object(),
            _gauge_coverage_latch=object(),
            _gauge_stale_latch=object(),
            _rotation_self_latch=rsn.BandEdgeLatch(),
            # GAU-06: the rider's ONE-LINE BLOCKER, now supplied. The leg's
            # durable half has no other source for a messaging service --
            # neither session_sweep nor the leg itself holds one -- so this
            # attribute existing on the plugin is the whole reason the item
            # could be built.
            _require_service=lambda: _sentinel_service,
        )
        # The rider's OWN try/except around the steward legs is what should
        # absorb _boom. If it does not, this call raises and the test fails
        # loud rather than quietly reporting an unreached leg.
        AgentMessagingPlugin._run_rotation_surface_sweep(cast("Any", fake_self))  # noqa: SLF001
    finally:
        plugin_mod.sweep_rotation_due_sessions = p_due  # type: ignore[assignment]
        plugin_mod.sweep_gauge_coverage = p_dark  # type: ignore[assignment]
        plugin_mod.sweep_rotation_self_notice = p_self  # type: ignore[assignment]
        plugin_mod.sweep_gauge_staleness = p_stale  # type: ignore[assignment]

    _check(
        reached == ["self_notice", "gauge_stale"],
        "the REAL _run_rotation_surface_sweep invokes the self-notice leg "
        "AND the L4d gauge-staleness leg -- both with the two steward legs "
        "raising, so a sibling fault cannot silence either. The ORDER is "
        "asserted too: L4d runs after L4c, which is where the rider places "
        f"it. Got: {reached!r}",
    )
    _check(
        seen_kwargs.get("agent_messaging_service") is _sentinel_service,
        "★ and the rider PASSES THE MESSAGING SERVICE to the leg (GAU-06's "
        "one-line blocker). Without this assertion the wiring could be "
        "dropped and every self-notice test would still pass: the leg's "
        "unwired guard returns an EMPTY TALLY, which reads exactly like a "
        "quiet fleet -- a green that means the durable half never ran",
    )


def test_an_ended_session_is_neither_notified_nor_counted() -> None:
    """★ CATCHES: the unbounded scan. The blocking finding from diff review.

    `session_context_status` is NEVER PRUNED -- no reaper, no `delete_state`,
    nothing sets `is_deleted` anywhere in the repo. So the table holds a row for
    every session that has ever reported, frozen at whatever value it ended on.
    A session that ended at 380,000 tokens sits permanently in
    `warm_immediate`, is permanently a candidate, permanently resolves to no
    binding, and -- because `record_sent` never runs for it, so the latch never
    engages -- would be counted on EVERY TICK FOREVER.

    Nothing would have been spammed (a dead session has no binding), so this is
    invisible from the delivery side. The damage is to the instrument:
    `unroutable` would have been dominated by dead rows while its log prose
    blamed the watch-id join gap, and a reader watching that number climb would
    size a follow-on landing against a phantom.

    "This would also be (0,0) if the row were simply skipped for another
    reason" -- which is why the third assertion pins that the SAME row one
    minute inside the window IS notified. The pair isolates staleness as the
    cause rather than any other predicate.
    """
    stale = _row(
        "agi-ended",
        current_tokens=380_000,
        measured_at="2026-08-17T21:00:00",
    )
    counts, bridges = _sweep([stale])
    _check(counts.appended == 0, "an ended session is not notified")
    _check(
        counts.unroutable == 0 and counts.undeliverable == 0,
        "...and is NOT counted as a coverage gap either -- both numbers "
        "pinned, since notified==0 alone cannot tell 'skipped' from "
        "'counted as unroutable'",
    )
    _check(not bridges.appended, "and nothing was appended for it")

    fresh = _row("agi-ended", current_tokens=380_000, measured_at="2026-08-17T22:59:00")
    live_counts, _ = _sweep([fresh])
    _check(
        live_counts.appended == 1,
        "...while the SAME row one minute inside the window IS notified -- "
        "so it is staleness doing the work, not some other predicate",
    )


def test_the_staleness_window_is_loose_enough_for_a_long_tool_call() -> None:
    """CATCHES: reusing GAUGE_COVERAGE_GRACE_S (300.0) as the staleness bound.

    The reporter is a `PostToolUse` hook, so a session inside one long tool call
    is legitimately silent for minutes -- this lane's own gate battery runs ~7.
    A 300s bound would drop a live session mid-run, and it would drop it
    silently, since a dropped row is indistinguishable from a carry-on verdict.
    """
    _check(
        rsn.SELF_NOTICE_STALENESS_S > 300.0,
        f"the staleness bound ({rsn.SELF_NOTICE_STALENESS_S}s) is looser than "
        f"the startup grace ({ss.GAUGE_COVERAGE_GRACE_S}s) -- they are "
        "different quantities and must not share a constant",
    )
    mid_battery = _row(current_tokens=350_000, measured_at="2026-08-17T22:52:00")
    counts, _ = _sweep([mid_battery])
    _check(
        counts.appended == 1, "a session 8 minutes into one tool call is still LIVE and is notified"
    )


def test_measured_at_reads_back_naive_and_is_still_compared_correctly() -> None:
    """CATCHES: comparing the stored timestamp to an aware clock directly.

    The reporting hook writes `datetime.now(UTC).isoformat()` -- AWARE -- but
    the DATETIME column drops the offset, so state hands back a NAIVE string
    (measured: '2026-08-18T00:09:42.968903'). Subtracting that from an aware
    clock raises TypeError, and inside this rider's per-leg fault isolation it
    would have failed the leg SILENTLY on every tick: no notices, no counts, one
    exception line attributed to the leg rather than to the timestamp.

    Both forms are asserted, because handling only the naive one would break the
    day the column starts preserving offsets.
    """
    naive = _measured_age_or_none({"measured_at": "2026-08-17T22:59:00"})
    aware = _measured_age_or_none({"measured_at": "2026-08-17T22:59:00+00:00"})
    _check(
        naive is not None and abs(naive - 60.0) < 1.0,
        f"a NAIVE stored value is read as UTC and ages correctly ({naive}s)",
    )
    _check(
        aware is not None and abs(aware - 60.0) < 1.0,
        f"an AWARE value is honoured as-is and agrees ({aware}s)",
    )
    _check(
        _measured_age_or_none({"measured_at": "not-a-timestamp"}) is None
        and _measured_age_or_none({"measured_at": ""}) is None,
        "an unparseable or absent stamp is a distinct third answer, not a "
        "guess in either direction",
    )
    counts, _ = _sweep([_row(current_tokens=350_000, measured_at="not-a-timestamp")])
    _check(
        counts == rsn.SelfNoticeCounts(),
        "...and such a row is skipped entirely -- guessing FRESH would "
        "resurrect the unbounded scan, guessing STALE would silence a live "
        "session",
    )


def test_a_delivery_fault_is_not_reported_as_a_routing_gap() -> None:
    """CATCHES: collapsing 'no binding' and 'the durable write raised' into one.

    They have different causes and different owners, and the log prose names
    the routing gap specifically. A single failure count made every transport
    fault read as evidence for the watch-id join gap.

    GAU-06 moved WHICH fault this is. ``undeliverable`` now means the DURABLE
    write raised -- the half that leaves the session with nothing to read -- and
    the surface append has its own outcome, pinned by the test below. The
    discrimination the original test existed for (a transport fault is not a
    routing gap) is asserted here unchanged; a second discrimination is added
    rather than trading one for the other.
    """
    counts, bridges, _ = _sweep_full(
        [_row("agi-live", current_tokens=350_000)],
        known={"agi-live"},
        persist_fails=True,
    )
    _check(
        (counts.appended, counts.unroutable, counts.undeliverable) == (0, 0, 1),
        "a resolved binding whose DURABLE write raises counts as "
        "UNDELIVERABLE, never as unroutable -- the join gap is not blamed "
        "for a transport fault",
    )
    _check(
        bridges.appended == [],
        "and nothing was surfaced either: persist-first means a failed "
        "durable write stops the notice rather than showing a session "
        "something it can never retrieve",
    )


def test_a_surfaced_notice_the_session_can_still_read_is_not_undeliverable() -> None:
    """CATCHES: reporting a bridge-append fault as a lost notice (GAU-06).

    The durable row IS the delivery under the persist-first route. If the
    bridge append then fails, the notice is LATE -- it sits in that session's
    inbox until it next reads -- not LOST. Counting it ``undeliverable`` would
    send someone hunting for a message that is already there, and would also
    re-open the episode's latch and re-notify a session that has the notice.
    """
    latch = rsn.BandEdgeLatch()
    row = [_row("agi-live", current_tokens=350_000)]
    counts, bridges, service = _sweep_full(
        row,
        known={"agi-live"},
        append_fails=True,
        latch=latch,
    )
    _check(
        (counts.appended, counts.undeliverable) == (1, 0),
        "the durable half succeeded, so the notice counts as APPENDED even "
        "though the surface append raised",
    )
    _check(
        len(service.sent) == 1 and bridges.appended == [],
        "...and that is not a bookkeeping claim: the row was persisted and "
        "the bridge really did reject the event",
    )
    second, _, second_service = _sweep_full(row, known={"agi-live"}, latch=latch)
    _check(
        second.appended == 0 and second_service.sent == [],
        "the episode IS latched by the durable write -- a session that has "
        "the notice in its inbox is not told again at the same band edge",
    )


# ---------------------------------------------------------------------------
# GAU-06 (G2 + G1) -- the DURABLE half, and the drive call that must not happen.


def test_the_notice_is_persisted_before_it_is_surfaced() -> None:
    """★ CATCHES: surfacing first, persisting second (GAU-06 G2).

    The order is the whole guarantee. Append-then-persist means a crash between
    the two leaves a notice that was SHOWN and then LOST: the reader who saw it
    cannot retrieve it, and the reader who did not has no trace it happened.
    Persist-then-append means every notice a session was ever shown is one it
    can go back and read.

    Asserted on the OBSERVED SEQUENCE of the two collaborators, not on the
    order of statements in the source -- an ordering property is behavioural.
    """
    order: list[str] = []
    _sweep_full(
        [_row("agi-live", current_tokens=350_000)],
        known={"agi-live"},
        order=order,
    )
    _check(
        order == ["persist", "append"],
        f"the durable write happens BEFORE the surface append -- got {order!r}",
    )


def test_the_durable_notice_is_not_stamped_important() -> None:
    """CATCHES: routing through a helper that hardcodes ``important=True``.

    ``important`` is what peer_inbox's silent-only filters read, and it is the
    marker a reader treats as wake-bound. A machine-generated context
    measurement stamped IMPORTANT is precisely the training of the coordination
    inbox that GAU-06's noise half exists to prevent.
    """
    _, _, service = _sweep_full(
        [_row("agi-live", current_tokens=350_000)],
        known={"agi-live"},
    )
    _check(len(service.sent) == 1, "one durable notice was persisted")
    _check(
        service.sent[0].important is False,
        "and it is NOT stamped important -- this is a notice, not a wake",
    )


def test_the_sender_is_a_sentinel_with_its_own_thread_key() -> None:
    """CATCHES: sending the notice AS the measured session, or as the scheduler.

    Two distinct properties, both load-bearing:

    * peer threads key on ``(sender_bridge_id, peer_instance)``, so a dedicated
      sentinel gives rotation notices their own thread per recipient instead of
      interleaving them with scheduler coordination traffic. That separation IS
      the read-side half of the noise ruling -- one thread a drain can leave
      alone beats teaching every reader to filter by content.
    * the sender must not be the recipient. The service rejects a same-instance
      self-send outright, so a 'self'-notice addressed from the session to
      itself would raise on every tick -- and the honest description is anyway
      that the PLATFORM is telling the session about itself.
    """
    _, _, service = _sweep_full(
        [_row("agi-live", current_tokens=350_000)],
        known={"agi-live"},
    )
    sent = service.sent[0]
    _check(
        sent.sender_bridge_id == "system:rotation-notice"
        and sent.sender_agent_instance_id == "system:rotation-notice",
        f"the notice is sent under its own sentinel -- got "
        f"{sent.sender_bridge_id!r}/{sent.sender_agent_instance_id!r}",
    )
    _check(
        sent.sender_agent_instance_id != sent.peer_agent_instance_id,
        "and the sender is NOT the recipient -- a same-instance send is "
        "rejected by the service and would fault this leg every tick",
    )
    _check(
        sent.peer_agent_instance_id == "agi-live"
        and sent.peer_agent_session_id == "ases-recipient",
        "the recipient is addressed by BOTH keys, so the row stays visible "
        f"after that session's instance rotates -- got {sent!r}",
    )


def test_the_surface_event_keeps_its_own_name() -> None:
    """CATCHES: relabelling the notice as a generic peer message.

    The event name is the discriminator a read-side filter keys on. A route
    that appends ``peer_message`` instead would leave a drain no way to tell a
    context measurement from a colleague's message except by reading the prose.
    """
    _, bridges, _ = _sweep_full(
        [_row("agi-live", current_tokens=350_000)],
        known={"agi-live"},
    )
    _check(
        [event for _, event, _ in bridges.appended] == [rsn.EVENT_ROTATION_SELF_NOTICE],
        f"the surfaced event keeps its own name -- got {bridges.appended!r}",
    )


def test_a_watcher_held_session_is_counted_as_a_subset_not_an_extra() -> None:
    """CATCHES: reporting watcher-held sessions as a separate population, or
    not at all (GAU-06 G1).

    ``appended`` is the total; ``watcher_held`` is how many of THOSE are held by
    a no-MCP watcher. The two have different delivery stories -- a bridge-held
    session surfaces the notice at its next natural boundary, a watcher-held
    worker sees it only when it next looks -- and one averaged number supported
    neither. The pair below pins that watcher_held never exceeds appended and
    never double-counts.
    """
    counts, _, service = _sweep_full(
        [
            _row("agi-watch-abc", current_tokens=350_000),
            _row("agi-bridge-held", current_tokens=350_000),
        ],
        known={"agi-watch-abc", "agi-bridge-held"},
    )
    _check(
        counts.appended == 2 and len(service.sent) == 2,
        f"both sessions got a durable notice -- got {counts!r}",
    )
    _check(
        counts.watcher_held == 1,
        f"exactly ONE of them is watcher-held, counted as a subset of "
        f"appended rather than added to it -- got {counts!r}",
    )


def test_an_unwired_service_yields_an_empty_tally_not_a_surface_only_sweep() -> None:
    """★ CATCHES: the silent-green this whole change could have shipped with.

    If the leg accepted a missing messaging service and carried on appending
    surface events, it would report a perfectly healthy tally while the durable
    half -- the entire point of GAU-06 -- never ran, and a watcher drain would
    still eat every notice. The guard makes that state produce ZEROS AND NO
    SIDE EFFECTS, which is loud in the rider's all-clear line, and the rider
    test above separately pins that production really does pass the service.
    """
    counts, bridges, service = _sweep_full(
        [_row("agi-live", current_tokens=350_000)],
        known={"agi-live"},
        omit_service=True,
    )
    _check(
        (counts.appended, counts.unroutable, counts.undeliverable) == (0, 0, 0),
        f"an unwired leg tallies nothing -- got {counts!r}",
    )
    _check(
        bridges.appended == [] and service.sent == [],
        "and it appends NOTHING to any surface: a sweep that cannot persist "
        "must not deliver a notice that no watcher drain can survive",
    )


# ---------------------------------------------------------------------------
# GAU-06 retention -- the writer bounds its own thread.


def test_the_notice_thread_is_bounded_by_its_own_writer() -> None:
    """CATCHES: a durable notice with no bound -- a slower version of the same
    failure GAU-06 set out to fix.

    Asserts WHICH rows survive, not merely that something was deleted: keeping
    the wrong end would look identical in a count.
    """
    state = _FakeState([])
    state.seed_thread("agt-t", [1, 2, 3, 4, 5, 6, 7])
    deleted = prune_rotation_notices(state, thread_id="agt-t", keep=3)  # type: ignore[arg-type]
    survivors = sorted(int(r["cursor"]) for r in state.messages["agt-t"])
    _check(
        deleted == 4 and survivors == [5, 6, 7],
        f"the NEWEST three survive and the older four go -- got "
        f"deleted={deleted} survivors={survivors}",
    )
    # The label reads the recorded call SAFELY. An earlier version interpolated
    # `state.deletes[-1]` straight into the f-string, which is evaluated whether
    # or not the guard held -- so any mutation that stopped the prune issuing a
    # delete crashed this test with an IndexError instead of reporting a
    # finding, and a battery that dies early silently narrows its own coverage.
    last_delete = state.deletes[-1] if state.deletes else None
    _check(
        last_delete is not None and last_delete[2] is False,
        "and the delete is HARD -- a soft delete would keep every row "
        "forever behind a flag nothing on this platform reaps, so the "
        f"bound would be cosmetic. Got {last_delete!r}",
    )


def test_a_thread_below_the_bound_is_not_touched_at_all() -> None:
    """CATCHES: issuing a delete on every notice.

    The common case is a thread shorter than the bound. It must cost ONE
    bounded read and no write -- a delete that matches nothing is still a
    write against an append-only table, and this mechanism spends a narrowly
    ruled exception to that guarantee; spending it needlessly is how a narrow
    exception becomes a broad one.
    """
    state = _FakeState([])
    state.seed_thread("agt-short", [1, 2])
    deleted = prune_rotation_notices(state, thread_id="agt-short", keep=5)  # type: ignore[arg-type]
    _check(
        deleted == 0 and state.deletes == [],
        f"nothing deleted AND no delete issued -- got deleted={deleted}, "
        f"{len(state.deletes)} delete call(s)",
    )


def test_a_pruned_thread_still_pages_forward_from_a_stale_cursor() -> None:
    """★ CATCHES: renumbering cursors to close the gap a prune leaves.

    A reader holds an after-cursor from BEFORE the prune. Compacting or
    reusing cursors would make that stale cursor point into the middle of the
    surviving rows -- silently skipping notices, or replaying them -- and the
    damage would be invisible to any test that only counted rows.

    The guarantee is that a prune REMOVES rows and never RENUMBERS them: the
    cursor is per-thread monotonic and allocated once, so a client spanning the
    pruned region pages forward onto exactly the survivors.
    """
    state = _FakeState([])
    state.seed_thread("agt-page", [10, 11, 12, 13, 14, 15])
    before = state.page_after("agt-page", after_cursor=11)
    prune_rotation_notices(state, thread_id="agt-page", keep=2)  # type: ignore[arg-type]
    after = state.page_after("agt-page", after_cursor=11)
    _check(
        before == [12, 13, 14, 15], f"pre-prune, a cursor of 11 pages onto 12-15 -- got {before}"
    )
    _check(
        after == [14, 15],
        f"post-prune, the SAME cursor pages onto exactly the survivors, "
        f"with no renumbering and nothing skipped -- got {after}",
    )


def test_a_keep_of_zero_is_refused_rather_than_emptying_the_thread() -> None:
    """CATCHES: a caller bug becoming a deleted notice.

    keep=0 would delete the very row whose write triggered the prune. Refusing
    fails in the safe direction: the worst outcome becomes an unbounded table,
    never a session told nothing at all.
    """
    state = _FakeState([])
    state.seed_thread("agt-zero", [1, 2, 3])
    raised: Exception | None = None
    try:
        prune_rotation_notices(state, thread_id="agt-zero", keep=0)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — the TYPE is part of what is asserted
        raised = exc
    # Catching broadly and then asserting the type is deliberate: with the guard
    # removed this call dies on an IndexError off the end of an empty page, and
    # a narrow `except ValueError` would let that escape and kill the whole run
    # before the summary -- turning a finding into a crash that hides every
    # later test's verdict.
    _check(
        isinstance(raised, ValueError),
        f"keep=0 is REFUSED with a ValueError naming the bad input, not by "
        f"falling off the end of a page -- got {type(raised).__name__}: "
        f"{raised}",
    )
    _check(
        len(state.messages["agt-zero"]) == 3,
        f"...and the thread is untouched -- {len(state.messages['agt-zero'])} row(s) left",
    )


def test_the_writer_prunes_the_thread_it_just_wrote_to() -> None:
    """★ CATCHES: a retention mechanism nobody invokes.

    The prune is wired into the notify path itself -- no reaper, no schedule.
    Without this assertion the module could be perfect and never run, which is
    the same shipping failure as the uninvoked drive knob this change removed.

    Seeded ABOVE the bound on the thread the writer will address, so the prune
    has something to do and its having run is observable rather than inferred.
    """
    state = _FakeState([_row("agi-live", current_tokens=350_000)])
    thread_id = "agt-rotation-agi-live"
    state.seed_thread(thread_id, list(range(1, 61)))
    counts = rsn.sweep_rotation_self_notice(
        state,  # type: ignore[arg-type]
        now=_SWEEP_NOW,
        peer_registry=_FakeRegistry({"agi-live"}),  # type: ignore[arg-type]
        bridge_manager=_FakeBridgeManager(),  # type: ignore[arg-type]
        agent_messaging_service=_FakeMessagingService(),  # type: ignore[arg-type]
        latch=rsn.BandEdgeLatch(),
    )
    survivors = sorted(int(r["cursor"]) for r in state.messages[thread_id])
    _check(counts.appended == 1, f"the notice was persisted -- got {counts!r}")
    _check(
        [d[0] for d in state.deletes] == [thread_id],
        f"...and the writer pruned EXACTLY the thread it wrote to, no other "
        f"-- got {[d[0] for d in state.deletes]!r}",
    )
    _check(
        len(survivors) == rnr.ROTATION_NOTICE_RETENTION and survivors[:1] == [11],
        f"...down to the interim bound of {rnr.ROTATION_NOTICE_RETENTION}, "
        f"newest kept -- got {len(survivors)} row(s) starting at "
        f"{survivors[:1]}",
    )


def test_a_failed_prune_never_costs_the_session_its_notice() -> None:
    """CATCHES: raising out of storage hygiene and losing a delivered notice.

    The notice is already persisted when the prune runs. A prune fault is a
    growth problem; converting it into a delivery failure trades a bounded
    table for a session that was never told its context is large.
    """

    class _PruneExplodes(_FakeState):
        def query_ordered(self, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
            if spec.get("table") != TABLE_AGENT_MESSAGE:
                return super().query_ordered(namespace, spec)
            raise RuntimeError("the store is unreachable for the prune read")

    state = _PruneExplodes([_row("agi-live", current_tokens=350_000)])
    registry = _FakeRegistry({"agi-live"})
    bridges = _FakeBridgeManager()
    service = _FakeMessagingService()
    escaped: Exception | None = None
    counts = rsn.SelfNoticeCounts()
    try:
        counts = rsn.sweep_rotation_self_notice(
            state,  # type: ignore[arg-type]
            now=_SWEEP_NOW,
            peer_registry=registry,  # type: ignore[arg-type]
            bridge_manager=bridges,  # type: ignore[arg-type]
            agent_messaging_service=service,  # type: ignore[arg-type]
            latch=rsn.BandEdgeLatch(),
        )
    except Exception as exc:  # noqa: BLE001 — the escape IS the finding
        # Caught and REPORTED rather than allowed to propagate: an escaping
        # prune fault kills main() before the summary and hides every later
        # test's verdict, which is the crash-is-not-a-failure-report pattern
        # this suite has already fixed three times.
        escaped = exc
    _check(
        escaped is None,
        "the prune fault does NOT escape the notify path"
        + ("" if escaped is None else f" (ESCAPED: {escaped!r})"),
    )
    _check(
        (counts.appended, counts.undeliverable) == (1, 0),
        f"the notice still counts as APPENDED with the prune raising -- got {counts!r}",
    )
    _check(
        len(bridges.appended) == 1,
        "and it was still surfaced -- hygiene failing must not silence the "
        "notice it was cleaning up after",
    )


# ---------------------------------------------------------------------------
# GAU-02 -- the rider's own REACHABLE ALL-CLEAR.
#
# The three legs above report only when they have something to report, so a
# healthy tick that finds nobody is byte-for-byte identical in the log to a
# rider that never ran at all -- and the natural reading of that silence,
# especially on the first tick after a deploy, is "the landing failed". This is
# not an edge case: SELF_NOTICE_STALENESS_S excludes any session quiet for an
# hour, so ALL-ZERO IS THE NORMAL OVERNIGHT CASE. It cost a live verification
# twenty minutes on 2026-08-18, where "never ran", "ran and found nobody" and
# "ran, every leg healthy" were indistinguishable silences and the discriminator
# had to come from outside the instrument entirely.
#
# These tests are the reachable all-clear for the leg AND for the rider, which
# is the widened GAU-02 scope: the L4c early return was only the half that was
# found first.
# ---------------------------------------------------------------------------


class _ListHandler(logging.Handler):
    """Captures emitted LogRecords so a smoke can prove a path LOGGED, not stayed silent."""

    def __init__(self, sink: list[logging.LogRecord]) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.append(record)


def _records_from(emit: Callable[[], None]) -> list[logging.LogRecord]:
    """Run `emit` with a handler on the plugin logger and return what it logged.

    Forces `propagate` and a permissive level for the duration: a smoke that
    measured "nothing was logged" because the logger was configured off
    elsewhere in the process would be a false green on exactly the property
    under test -- silence is the defect here, so silence must never be the
    test's own default outcome.
    """
    records: list[logging.LogRecord] = []
    handler = _ListHandler(records)
    plugin_logger = logging.getLogger("agent_messaging_plugin.plugin")
    previous_level = plugin_logger.level
    plugin_logger.addHandler(handler)
    plugin_logger.setLevel(logging.DEBUG)
    try:
        emit()
    finally:
        plugin_logger.removeHandler(handler)
        plugin_logger.setLevel(previous_level)
    return records


def test_an_all_zero_sweep_still_says_the_leg_ran() -> None:
    """★ CATCHES: GAU-02 -- the early return that makes a healthy leg silent.

    The assertion is on the OBSERVABLE (a record was emitted), not on the
    absence of a `return` statement in the source: a behavioural property
    cannot be enumerated by reading the code that is supposed to have it.
    """
    records = _records_from(
        lambda: AgentMessagingPlugin._log_self_notice_counts(rsn.SelfNoticeCounts()),  # noqa: SLF001
    )
    _check(
        len(records) == 1,
        f"an all-zero L4c result emits exactly ONE line (got {len(records)}) -- "
        "a healthy leg that found nobody must not be indistinguishable from a "
        "leg that never ran",
    )
    if not records:
        return
    message = records[0].getMessage()
    _check(
        "0" in message and "appended" in message and "watcher-held" in message,
        f"...and that line PRINTS ITS DENOMINATORS rather than merely "
        f"asserting health -- got: {message!r}",
    )


def test_the_non_zero_line_is_unchanged_by_the_zero_case_fix() -> None:
    """★ CATCHES: buying the all-zero heartbeat by rewriting the line that
    already worked. The non-zero wording is what the 2026-08-18 verification
    quoted verbatim into a report, and all three counts must still be named
    separately -- `unroutable` and `undeliverable` have different causes and
    different owners, and collapsing them would re-attribute every transport
    fault to the join gap.
    """
    records = _records_from(
        lambda: AgentMessagingPlugin._log_self_notice_counts(  # noqa: SLF001
            rsn.SelfNoticeCounts(appended=4, unroutable=2, undeliverable=1),
        ),
    )
    _check(len(records) == 1, f"a non-zero L4c result still emits ONE line (got {len(records)})")
    if not records:
        return
    message = records[0].getMessage()
    _check(
        "4 session(s)" in message
        and "APPENDED" in message
        and "2 unroutable" in message
        and "1 undeliverable" in message,
        f"...naming all three counts separately, as before -- got: {message!r}",
    )
    _check(
        "0 of them watcher-held" in message and "subset" in message,
        "...and the watcher-held count is printed BESIDE appended and named a "
        f"SUBSET, so nobody adds it to the total -- got: {message!r}",
    )


def test_a_healthy_tick_of_the_whole_rider_is_not_silent() -> None:
    """★ CATCHES: the widened GAU-02 -- the rider level, not just the L4c leg.

    `_run_rotation_surface_sweep` logs only on faults, so with every leg healthy
    and empty the ENTIRE rider produced no output. This drives the REAL rider
    with all three legs stubbed to a clean empty result and asserts the tick
    announces itself. Without this, a quiet period and a dead rider are the same
    observation.
    """
    import agent_messaging_plugin.plugin as plugin_mod

    p_due = plugin_mod.sweep_rotation_due_sessions
    p_dark = plugin_mod.sweep_gauge_coverage
    p_self = plugin_mod.sweep_rotation_self_notice
    p_stale = plugin_mod.sweep_gauge_staleness
    plugin_mod.sweep_rotation_due_sessions = lambda *a, **k: 0  # type: ignore[assignment]
    plugin_mod.sweep_gauge_coverage = lambda *a, **k: 0  # type: ignore[assignment]
    plugin_mod.sweep_rotation_self_notice = lambda *a, **k: rsn.SelfNoticeCounts()  # type: ignore[assignment]
    plugin_mod.sweep_gauge_staleness = lambda *a, **k: 0  # type: ignore[assignment]
    fake_self = SimpleNamespace(
        _log_self_notice_counts=AgentMessagingPlugin._log_self_notice_counts,  # noqa: SLF001
        _get_state_service=lambda: object(),
        _peer_registry=object(),
        _bridge_manager=object(),
        _rotation_due_latch=object(),
        _gauge_coverage_latch=object(),
        _gauge_stale_latch=object(),
        _rotation_self_latch=rsn.BandEdgeLatch(),
    )
    try:
        records = _records_from(
            lambda: AgentMessagingPlugin._run_rotation_surface_sweep(cast("Any", fake_self)),  # noqa: SLF001
        )
    finally:
        plugin_mod.sweep_rotation_due_sessions = p_due  # type: ignore[assignment]
        plugin_mod.sweep_gauge_coverage = p_dark  # type: ignore[assignment]
        plugin_mod.sweep_rotation_self_notice = p_self  # type: ignore[assignment]
        plugin_mod.sweep_gauge_staleness = p_stale  # type: ignore[assignment]

    messages = [r.getMessage() for r in records]
    swept = [m for m in messages if "rotation surface" in m.lower()]
    _check(
        bool(swept),
        "a healthy all-zero tick of the WHOLE rider emits a 'rotation surface "
        f"swept' line -- got {messages!r}",
    )
    if not swept:
        return
    _check(
        all(leg in swept[0] for leg in ("L4a", "L4b", "L4c", "L4d")),
        f"...and that line NAMES WHICH LEGS RAN, so a rider that lost a leg is "
        f"distinguishable from one that ran them all -- got: {swept[0]!r}",
    )


def test_a_faulted_leg_is_named_in_the_all_clear_rather_than_omitted() -> None:
    """★ CATCHES: an all-clear that quietly shrinks when a leg dies.

    This is the assertion that decides whether the rider line is worth having.
    A summary built only from legs that SUCCEEDED reports "rotation surface
    swept" with one leg missing and looks healthy at a glance -- the same
    silence GAU-02 is about, moved into the line that was supposed to close it.
    Every leg is isolated (a fault in one does not skip the others), so a
    faulted leg is exactly the case where the tick continues and the reader
    most needs to be told.
    """
    import agent_messaging_plugin.plugin as plugin_mod

    def _boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("leg fault -- must still be NAMED in the all-clear")

    p_due = plugin_mod.sweep_rotation_due_sessions
    p_dark = plugin_mod.sweep_gauge_coverage
    p_self = plugin_mod.sweep_rotation_self_notice
    p_stale = plugin_mod.sweep_gauge_staleness
    plugin_mod.sweep_rotation_due_sessions = lambda *a, **k: 0  # type: ignore[assignment]
    plugin_mod.sweep_gauge_coverage = _boom  # type: ignore[assignment]
    plugin_mod.sweep_rotation_self_notice = lambda *a, **k: rsn.SelfNoticeCounts()  # type: ignore[assignment]
    plugin_mod.sweep_gauge_staleness = lambda *a, **k: 0  # type: ignore[assignment]
    fake_self = SimpleNamespace(
        _log_self_notice_counts=AgentMessagingPlugin._log_self_notice_counts,  # noqa: SLF001
        _get_state_service=lambda: object(),
        _peer_registry=object(),
        _bridge_manager=object(),
        _rotation_due_latch=object(),
        _gauge_coverage_latch=object(),
        _gauge_stale_latch=object(),
        _rotation_self_latch=rsn.BandEdgeLatch(),
    )
    try:
        records = _records_from(
            lambda: AgentMessagingPlugin._run_rotation_surface_sweep(cast("Any", fake_self)),  # noqa: SLF001
        )
    finally:
        plugin_mod.sweep_rotation_due_sessions = p_due  # type: ignore[assignment]
        plugin_mod.sweep_gauge_coverage = p_dark  # type: ignore[assignment]
        plugin_mod.sweep_rotation_self_notice = p_self  # type: ignore[assignment]
        plugin_mod.sweep_gauge_staleness = p_stale  # type: ignore[assignment]

    swept = [r.getMessage() for r in records if "rotation surface" in r.getMessage().lower()]
    _check(bool(swept), "the all-clear line is emitted even when a leg faulted")
    if not swept:
        return
    _check(
        "L4b=FAULTED" in swept[0],
        f"...and the FAULTED leg is NAMED in it rather than dropped, so the "
        f"line cannot shrink silently -- got: {swept[0]!r}",
    )
    _check(
        "L4a" in swept[0] and "L4c" in swept[0] and "L4d" in swept[0],
        f"...while the healthy legs still report their own counts alongside it "
        f"-- got: {swept[0]!r}",
    )


# ---------------------------------------------------------------------------
# GAU-01(c): the eligibility bound was on the GAUGE clock
#
# `SELF_NOTICE_STALENESS_S` is doing a real job -- the gauge table is never
# pruned, so without it this scan walks the entire history of every session that
# ever reported. But it was evaluated against `measured_at`, which is the GAUGE
# clock, so a session whose gauge had ARRESTED dropped out of the notifiable
# population after an hour. It stopped being told to rotate at exactly the
# moment it had been running longest without an operator prompt -- which is the
# scenario this leg exists for. The gauge going quiet was being read as the
# SESSION going quiet.
# ---------------------------------------------------------------------------


def _lifecycle(
    agent_instance_id: str = "agi-seat",
    *,
    report_by: str | None = None,
    lifecycle_state: str = "live",
) -> dict[str, Any]:
    """A lifecycle row as `live_lifecycle_rows_by_instance` reads it."""
    row: dict[str, Any] = {
        "created_at": "2026-08-17T20:00:00",
        "id": f"ams-{agent_instance_id}-{lifecycle_state}",
        "agent_instance_id": agent_instance_id,
        "lifecycle_state": lifecycle_state,
    }
    if report_by is not None:
        row["report_by"] = report_by
    return row


_LONG_STALE = "2026-08-17T21:00:00"  # two hours before _SWEEP_NOW; past the bound


def test_a_live_session_with_an_arrested_gauge_is_still_notified() -> None:
    """★ THE GAU-01(c) FIX. The row's gauge clock says two hours stale, so the
    old bound dropped it. Its LIFECYCLE row says it is inside its reporting
    window -- the platform's own verdict that it is live -- so it is exactly the
    session that most needs to hear its own number."""
    arrested = _row(current_tokens=350_000, measured_at=_LONG_STALE)
    counts, _ = _sweep(
        [arrested],
        lifecycle_rows=[_lifecycle(report_by="2026-08-17T23:05:00")],
    )
    _check(
        counts.appended == 1,
        "a session whose GAUGE arrested but which is demonstrably still "
        "reporting is notified, not silently dropped",
    )


def test_a_stale_row_with_no_live_session_is_still_dropped() -> None:
    """The unbounded-scan guard, preserved. The bound still fires -- it is the
    only thing keeping this leg off the whole history of the unpruned gauge
    table. Relaxing it unconditionally would have traded one defect for a scan
    that grows without limit."""
    ancient = _row(current_tokens=350_000, measured_at=_LONG_STALE)
    counts, _ = _sweep([ancient], lifecycle_rows=[])
    _check(
        counts.appended == 0,
        "a stale row with no live lifecycle row behind it is still excluded",
    )


def test_a_lapsed_report_by_does_not_vouch_for_liveness() -> None:
    """`report_by` in the PAST means the session has missed its own deadline --
    the very condition the D1 sweep flips to `overdue`. It must not extend
    eligibility, or the bound would be relaxed by exactly the rows it exists to
    keep out."""
    arrested = _row(current_tokens=350_000, measured_at=_LONG_STALE)
    counts, _ = _sweep(
        [arrested],
        lifecycle_rows=[_lifecycle(report_by="2026-08-17T22:00:00")],
    )
    _check(counts.appended == 0, "a session past its report_by does not vouch for itself")


def test_liveness_fails_closed_on_an_unreadable_report_by() -> None:
    """CATCHES: relaxing the bound on evidence that could not be read.

    A guard that fails OPEN on unparseable input is not a guard. Both shapes --
    no `report_by` at all, and one that will not parse -- must leave the bound
    in force."""
    arrested = _row(current_tokens=350_000, measured_at=_LONG_STALE)
    for lifecycle_rows, label in (
        ([_lifecycle()], "a lifecycle row with NO report_by"),
        ([_lifecycle(report_by="not-a-timestamp")], "an unparseable report_by"),
    ):
        counts, _ = _sweep([arrested], lifecycle_rows=lifecycle_rows)
        _check(counts.appended == 0, f"{label} does not extend eligibility")


def test_a_non_live_lifecycle_row_does_not_vouch_for_liveness() -> None:
    """Only rows the platform itself calls `live` are consulted. An `overdue`
    row with a future report_by is not a contradiction to resolve here -- it is
    the D1 sweep's business, and this leg must not overrule it."""
    arrested = _row(current_tokens=350_000, measured_at=_LONG_STALE)
    counts, _ = _sweep(
        [arrested],
        lifecycle_rows=[
            _lifecycle(report_by="2026-08-17T23:05:00", lifecycle_state="overdue"),
        ],
    )
    _check(counts.appended == 0, "an overdue lifecycle row does not vouch for liveness")


def test_a_fresh_row_never_consults_the_lifecycle_table() -> None:
    """The ordering that keeps the change cheap: the timestamp test
    short-circuits, so the lifecycle lookup only decides rows the OLD bound
    would have dropped. A fresh row is notified with no lifecycle row present at
    all -- which also proves the new path cannot change the existing verdict."""
    fresh = _row(current_tokens=350_000)
    counts, _ = _sweep([fresh], lifecycle_rows=[])
    _check(counts.appended == 1, "a fresh row is unaffected by the liveness path")


def test_one_durability_notice_per_generation() -> None:
    """The self path shares the one derived boundary and never repeats it."""
    quiet, bridges = _sweep([_row(current_tokens=319_999)])
    _check(quiet.appended == 0 and not bridges.appended, "319,999 produces no self notice")
    latch = rsn.BandEdgeLatch()
    due_row = _row(current_tokens=320_000)
    first, first_bridges = _sweep([due_row], latch=latch)
    second, second_bridges = _sweep([due_row], latch=latch)
    _check(
        first.appended == 1
        and _first_prose(first_bridges)
        == ("context is 320,000 — make sure everything is durable."),
        "320,000 produces exactly the operator-supplied one-line notice",
    )
    _check(
        second.appended == 0 and not second_bridges.appended,
        "a second sweep in the same generation does not repeat the notice",
    )
    haiku, _ = _sweep(
        [
            _row(current_tokens=144_000, ceiling=200_000, model="claude-haiku-4-5"),
        ]
    )
    _check(haiku.appended == 1, "a 200K runtime window notices at the 144K compaction-bound point")


def test_registered_gauge_silence_is_noticed_after_grace() -> None:
    """CATCHES: scanning only existing gauge rows, which cannot see silence."""
    binding = _FakeBinding(
        "bridge-hand-launched",
        agent_instance_id="agi-hand-launched",
        agent_id="codex",
        created_at=(
            _SWEEP_NOW - timedelta(seconds=rsn.GAUGE_SILENT_REGISTRATION_GRACE_S + 1)
        ).isoformat(),
    )
    counts, bridges, service = _sweep_full([], bindings=[binding])
    _check(
        counts.gauge_silent == 1 and counts.appended == 1 and len(bridges.appended) == 1,
        "a registered session past grace with no gauge row is counted and persist-first noticed",
    )
    _check(
        "gauge_silent_since_registration" in _first_prose(bridges)
        and service.sent[0].peer_agent_instance_id == "agi-hand-launched",
        "the notice names the measured absence and addresses the registered session itself",
    )
    quiet_counts, quiet_bridges, _ = _sweep_full(
        [_row("agi-hand-launched", current_tokens=120_000)],
        bindings=[binding],
    )
    _check(
        quiet_counts.gauge_silent == 0 and quiet_bridges.appended == [],
        "a gauge row suppresses the silence rider even when its context band is quiet",
    )


def _paged_candidate_rows(cutoff: datetime) -> list[dict[str, Any]]:
    """Ancient history plus enough fresh rows to cross more than two pages."""
    ancient = [
        _row(f"agi-ancient-{index:03d}", current_tokens=350_000, measured_at=_LONG_STALE)
        for index in range(205)
    ]
    fresh = [
        _row(
            f"agi-fresh-{index:03d}",
            current_tokens=120_000,
            measured_at=(cutoff + timedelta(seconds=index + 1)).replace(tzinfo=None).isoformat(),
        )
        for index in range(201)
    ]
    boundary = _row(
        "agi-boundary", current_tokens=350_000, measured_at=cutoff.replace(tzinfo=None).isoformat(),
    )
    lifecycle_live = _row("agi-lifecycle-live", current_tokens=350_000, measured_at=_LONG_STALE)
    operator_live = _row("agi-operator-live", current_tokens=350_000, measured_at=_LONG_STALE)
    overlap = _row(
        "agi-overlap", current_tokens=120_000,
        measured_at=(cutoff + timedelta(seconds=1)).replace(tzinfo=None).isoformat(),
    )
    return [*ancient, *fresh, boundary, lifecycle_live, operator_live, overlap]


def _assert_paged_candidate_reader(
    rows: list[dict[str, Any]], cutoff: datetime,
) -> None:
    state = _FakeState(rows)
    candidates = scss.list_session_context_statuses(
        state,  # type: ignore[arg-type]
        measured_since=rsn.to_naive_utc(cutoff),
        active_agent_instance_ids={"agi-lifecycle-live", "agi-operator-live", "agi-overlap"},
    )
    candidate_ids = {str(row["agent_instance_id"]) for row in candidates}
    _check(
        len(candidates) == 205
        and "agi-fresh-200" in candidate_ids
        and {"agi-boundary", "agi-lifecycle-live", "agi-operator-live", "agi-overlap"}
        <= candidate_ids
        and not any(identifier.startswith("agi-ancient-") for identifier in candidate_ids),
        "L4c pages >2 fresh pages, keeps exact naive-UTC boundary and stale active rows, omits ancient dead history",
    )
    _check(
        state.context_ordered_reads >= 4 and state.context_query_state_reads == 0
        and sum(row["agent_instance_id"] == "agi-overlap" for row in candidates) == 1,
        "two candidate streams are paged, target query_state is never used, and overlap dedupes by row id",
    )


def _assert_candidate_sweep_semantics(rows: list[dict[str, Any]]) -> None:
    operator_binding = _FakeBinding(
        "bridge-operator", agent_instance_id="agi-operator-live", created_at="2026-08-17T20:00:00",
    )
    missing_binding = _FakeBinding(
        "bridge-missing", agent_instance_id="agi-gauge-missing",
        created_at="2026-08-17T20:00:00",
    )
    counts, _bridges, _service = _sweep_full(
        rows,
        known={"agi-boundary", "agi-lifecycle-live"},
        lifecycle_rows=[_lifecycle("agi-lifecycle-live", report_by="2026-08-17T23:05:00")],
        bindings=[operator_binding, missing_binding],
    )
    _check(
        counts.appended == 3 and counts.gauge_silent == 1,
        "aware caller clock preserves boundary/live/operator candidates and ancient history does not suppress gauge_silent",
    )


def test_candidate_reader_pages_fresh_or_active_rows_at_the_naive_utc_boundary() -> None:
    """L4c uses two paged provider reads and never a target ``query_state``."""
    cutoff = _SWEEP_NOW - timedelta(seconds=rsn.SELF_NOTICE_STALENESS_S)
    rows = _paged_candidate_rows(cutoff)
    _assert_paged_candidate_reader(rows, cutoff)
    _assert_candidate_sweep_semantics(rows)


def main() -> int:
    print("rotation self-notice (L4c) smoke\n")
    test_a_live_session_with_an_arrested_gauge_is_still_notified()
    test_a_stale_row_with_no_live_session_is_still_dropped()
    test_a_lapsed_report_by_does_not_vouch_for_liveness()
    test_liveness_fails_closed_on_an_unreadable_report_by()
    test_a_non_live_lifecycle_row_does_not_vouch_for_liveness()
    test_a_fresh_row_never_consults_the_lifecycle_table()
    test_registered_gauge_silence_is_noticed_after_grace()
    test_candidate_reader_pages_fresh_or_active_rows_at_the_naive_utc_boundary()
    test_one_durability_notice_per_generation()
    test_a_carry_on_verdict_says_nothing_at_all()
    test_an_unresolvable_session_is_counted_not_swallowed()
    test_a_failed_delivery_does_not_latch_the_episode()
    test_capacity_never_preempts_economics_on_a_1m_ceiling()
    test_the_break_even_is_tier_invariant()
    test_the_overage_ttl_moves_the_break_even_and_moves_it_down()
    test_nothing_in_the_leg_can_drive_a_session()
    test_an_unusable_row_is_skipped_rather_than_guessed()
    test_an_ended_session_is_neither_notified_nor_counted()
    test_the_staleness_window_is_loose_enough_for_a_long_tool_call()
    test_measured_at_reads_back_naive_and_is_still_compared_correctly()
    test_a_delivery_fault_is_not_reported_as_a_routing_gap()
    test_a_surfaced_notice_the_session_can_still_read_is_not_undeliverable()
    test_the_notice_is_persisted_before_it_is_surfaced()
    test_the_durable_notice_is_not_stamped_important()
    test_the_sender_is_a_sentinel_with_its_own_thread_key()
    test_the_surface_event_keeps_its_own_name()
    test_a_watcher_held_session_is_counted_as_a_subset_not_an_extra()
    test_an_unwired_service_yields_an_empty_tally_not_a_surface_only_sweep()
    test_the_notice_thread_is_bounded_by_its_own_writer()
    test_a_thread_below_the_bound_is_not_touched_at_all()
    test_a_pruned_thread_still_pages_forward_from_a_stale_cursor()
    test_a_keep_of_zero_is_refused_rather_than_emptying_the_thread()
    test_the_writer_prunes_the_thread_it_just_wrote_to()
    test_a_failed_prune_never_costs_the_session_its_notice()
    test_the_rider_actually_invokes_this_leg()
    test_an_all_zero_sweep_still_says_the_leg_ran()
    test_the_non_zero_line_is_unchanged_by_the_zero_case_fix()
    test_a_healthy_tick_of_the_whole_rider_is_not_silent()
    test_a_faulted_leg_is_named_in_the_all_clear_rather_than_omitted()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
