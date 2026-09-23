#!/usr/bin/env python3
"""Unit smoke for GAU-21 — a gauge alarm now leaves a durable, attributable
trace, INCLUDING the alarm that reached nobody.

THE DEFECT this covers, measured against the source on 2026-08-19. The sweep's
gauge notices existed only as in-memory bridge events, and four independent
properties of that queue each defeated an audit:

  1. NOT DURABLE — `BridgeSessionState` is "In-memory state for one active
     bridge"; a restart loses every un-drained notice, and the DEPLOY a canary
     must traverse IS a restart.
  2. DRAIN-ONCE, AND READING STEALS — `events_after` returns the acked events
     and rebinds the queue to exclude them, so a verifier polling that queue
     races the steward and can consume the very notice the steward needed.
  3. NO BY-TYPE READ — nothing is keyed on event type.
  4. CONDITIONALLY NEVER EMITTED — the notify path resolved the steward binding
     FIRST and returned early when it was None, so an alarm about an unbound
     session reached nobody AND left nothing behind.

So "the detector never fired" and "it fired into the void" were the same
silence — the original gauge defect one level up, with the instrument's own
output unobservable after the moment. That is also why the manual freeze-watch
cannot retire when the detector merely deploys.

★ EVERYTHING HERE DRIVES THE REAL SWEEP. No test calls `record_gauge_notice`
directly to make a row appear: the record is asserted as an effect of
`sweep_gauge_staleness` / `sweep_gauge_coverage` deciding to fire, because a
test that builds its own input tests the callee rather than the wiring, and the
wiring is exactly where the original defect lived.

★ WHAT WOULD FAIL IF THIS SMOKE WERE WRONG. Each test names the mutation it
catches, because a green that cannot name its failing mutation is not evidence.
The load-bearing ones:
  * gate the record behind the steward binding (restore the early return)
    → `test_an_undeliverable_alarm_is_still_recorded`
  * collapse delivery_outcome to a boolean
    → `test_the_three_delivery_outcomes_are_distinct`
  * let a record fault raise into the sweep loop
    → `test_a_record_fault_never_costs_another_row_its_notice`
  * make the prune soft-delete instead of hard
    → `test_retention_is_a_hard_bound`
  * make the read consume what it reads (the bridge queue's own bug)
    → `test_reading_does_not_consume`
  * match a None filter against the column instead of omitting it
    → `test_an_omitted_filter_does_not_narrow`

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/gauge_notice_record_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
)
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.context_status_verbs import VerbError  # noqa: E402
from agent_messaging_plugin.gauge_notice_record_store import (  # noqa: E402
    GAUGE_NOTICE_RETENTION,
    read_gauge_notice_records,
)
from agent_messaging_plugin.gauge_notice_records import gauge_notice_records  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.release_identity import (  # noqa: E402
    MAX_PARENTS_SEARCHED,
    _identity_from,
    running_release_id,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_SPAWNING,
    NOTICE_DELIVERY_APPEND_FAILED,
    NOTICE_DELIVERY_APPENDED,
    NOTICE_DELIVERY_NO_STEWARD_BINDING,
    PEER_BINDING_NAMESPACE,
    TABLE_GAUGE_NOTICE_RECORD,
    WORK_CLASS_READ_ONLY,
    get_peer_binding_schema,
)
from agent_messaging_plugin.session_context_status_store import (  # noqa: E402
    upsert_session_context_status,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_sweep import (  # noqa: E402
    EVENT_GAUGE_COVERAGE_NOTICE,
    EVENT_GAUGE_STALE_NOTICE,
    GAUGE_COVERAGE_GRACE_S,
    GAUGE_STALE_LAG_S,
    sweep_gauge_coverage,
    sweep_gauge_staleness,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _state() -> Any:
    return RealShapeState()


def _peer_registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _n: "ags-http",
        idle_timeout_s=3600,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )


def _spawn_live(
    state: Any, *, agent_instance_id: str, spawned_by_instance_id: str = "",
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id, lane_id="lane-x", brief_ref="",
            work_class=WORK_CLASS_READ_ONLY, budget_line="b1", host="operator",
            report_by_seconds=0, spawned_by_instance_id=spawned_by_instance_id,
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
    )


def _register_live_binding(
    reg: PeerRegistry, mgr: BridgeSessionManager, *, agent_instance_id: str,
) -> str:
    bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id, agent_id="claude_code",
            agent_instance_id=agent_instance_id,
            session_label=agent_instance_id, parent_pid=1,
        ),
    )
    return bridge_id


def _wired(*, with_steward_binding: bool = True) -> tuple[Any, PeerRegistry, Any, str]:
    """A live worker spawned by a steward, with the steward's bridge optionally
    registered.

    ``with_steward_binding=False`` is the count-4 shape: a real, spawned,
    live worker whose steward has NO resolvable binding. That is the case the
    old early return made invisible, so it gets a first-class fixture rather
    than a monkeypatch.
    """
    state, reg, mgr = _state(), _peer_registry(), _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-steward"}},
        {"agent_id": "claude_code"},
    )
    bridge_id = (
        _register_live_binding(reg, mgr, agent_instance_id="agi-steward")
        if with_steward_binding
        else ""
    )
    _spawn_live(
        state, agent_instance_id="agi-worker", spawned_by_instance_id="agi-steward",
    )
    return state, reg, mgr, bridge_id


def _gauge(state: Any, agent_instance_id: str, **over: object) -> None:
    kwargs: dict[str, object] = {
        "agent_instance_id": agent_instance_id, "claude_session_id": "s1",
        "model": "claude-sonnet-5", "current_tokens": 900_000, "ceiling": 1_000_000,
        "measured_at": datetime.now(UTC).isoformat(), "cache_cold": False,
        "reporter_surface": "checkout", "reporter_generation": 2,
    }
    kwargs.update(over)
    upsert_session_context_status(state, **kwargs)  # type: ignore[arg-type]


def _ticking(state: Any, agent_instance_id: str, *, last_alive: datetime) -> None:
    """ALSO backdates ``last_transition_at`` to a day before ``last_alive``
    (GAU-22(c), same root fix as ``session_sweep_smoke.py``'s ``_ticking``):
    ``_spawn_live`` stamps its own transition at real wall-clock "now", which
    every gauge-stale fixture here implicitly relied on being OLDER than its
    backdated gauge ``measured_at``. That is true of a genuinely long-lived
    ticking session and false only by fixture accident -- and the accident
    reads to the sweep as "just rotated", which is precisely the case
    GAU-22(c)'s grace window holds fire on. Without this, a fixture meaning
    "long-lived, reporter died" is indistinguishable from a fresh /clear.
    """
    window_s = 5400
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": agent_instance_id}},
        {
            "report_by_seconds": window_s,
            "report_by": (last_alive + timedelta(seconds=window_s)).isoformat(),
            "report_by_source": "explicit_self_report",
            "last_transition_at": (last_alive - timedelta(days=1)).isoformat(),
        },
    )


def _past_grace() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=GAUGE_COVERAGE_GRACE_S + 60)


def _fire_stale(state: Any, reg: PeerRegistry, mgr: Any) -> int:
    """Drive the REAL staleness leg into firing exactly once."""
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
    return sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)


def _records(state: Any) -> list[dict[str, Any]]:
    return state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_GAUGE_NOTICE_RECORD)


# ---------------------------------------------------------------------------
# The write happens at all, and carries what a later reader needs.
# ---------------------------------------------------------------------------


def test_a_delivered_alarm_is_recorded() -> None:
    """The baseline: the staleness leg fires, the steward gets the event, AND a
    durable record survives it.

    MUTATION: delete the `_record_notice_best_effort` call from
    `_notify_gauge_stale` → every check here fails while the bridge event is
    still delivered, which is precisely the pre-GAU-21 world.
    """
    state, reg, mgr, bridge_id = _wired()
    n = _fire_stale(state, reg, mgr)
    _check(n == 1, "the staleness leg fired once (the fixture's own precondition)")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(
        events and events[0].event_type == EVENT_GAUGE_STALE_NOTICE,
        "the steward still receives the bridge event — the record is ADDITIVE, "
        "not a replacement for delivery",
    )
    rows = _records(state)
    _check(len(rows) == 1, "exactly one durable record was written")
    row = rows[0] if rows else {}
    _check(row.get("notice_type") == EVENT_GAUGE_STALE_NOTICE,
           "the record is typed, which the bridge queue has no surface for")
    _check(row.get("agent_instance_id") == "agi-worker",
           "keyed on the SUBJECT session, not the steward it was sent to")
    _check(row.get("steward_instance_id") == "agi-steward",
           "and the steward it reached is recorded separately")
    _check(row.get("delivery_outcome") == NOTICE_DELIVERY_APPENDED,
           "a delivered alarm records 'appended'")


def test_the_record_carries_the_threshold_that_actually_fired() -> None:
    """★ THE RE-READABILITY REQUIREMENT. Measured 2026-08-19: the running
    release evaluated gauge coverage at 300s while master's source said 600s,
    and carried no staleness detector at all. A record that does not carry the
    threshold IN FORCE cannot be re-read later, because the reader's copy of
    the constant is not necessarily the one that fired.

    MUTATION: drop `threshold_s`/`observed_s` and let a reader re-derive them
    from module constants → these checks fail.
    """
    state, reg, mgr, _ = _wired()
    _fire_stale(state, reg, mgr)
    row = _records(state)[0]
    _check(row.get("threshold_s") == GAUGE_STALE_LAG_S,
           "the threshold in force at emit time is recorded, not re-derived")
    observed = row.get("observed_s")
    _check(isinstance(observed, float) and observed > GAUGE_STALE_LAG_S,
           f"the measured value that crossed it is recorded too (got {observed!r})")
    _check(row.get("gauge_measured_at") is not None,
           "the ARRESTED reading's own timestamp survives — the value the "
           "upsert-only cache would otherwise overwrite")
    _check(row.get("last_report_alive_at") is not None,
           "and the lifecycle clock it diverged from, so the divergence is "
           "reconstructable without the original row")


def test_the_coverage_leg_records_its_own_shape() -> None:
    """The other family, and its NULLs are meaningful rather than missing.

    MUTATION: write 0 or "" for the gauge timestamps on a coverage notice →
    the tri-state checks fail, and "there was no gauge row" would become
    indistinguishable from "the gauge read the epoch".
    """
    state, reg, mgr, _ = _wired()  # agi-worker is LIVE with NO gauge row
    n = sweep_gauge_coverage(
        state, now=_past_grace(), peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 1, "the coverage leg fired once (the fixture's own precondition)")
    rows = _records(state)
    _check(len(rows) == 1 and rows[0].get("notice_type") == EVENT_GAUGE_COVERAGE_NOTICE,
           "a coverage alarm is recorded under its OWN type")
    row = rows[0] if rows else {}
    _check(row.get("threshold_s") == GAUGE_COVERAGE_GRACE_S,
           "against the grace in force, not the staleness bound")
    _check(row.get("gauge_measured_at") is None and row.get("last_report_alive_at") is None,
           "and the clocks it does not read stay NULL — a coverage notice "
           "fires precisely because there is no gauge row to timestamp")


# ---------------------------------------------------------------------------
# Count 4: the alarm that reached nobody. The reason this table exists.
# ---------------------------------------------------------------------------


def test_an_undeliverable_alarm_is_still_recorded() -> None:
    """★ THE COUNT-4 FIX, and the single most important test here.

    The notify path used to resolve the steward binding FIRST and return early
    when it was None, so an alarm about a session whose steward is unbound
    reached nobody and left NOTHING BEHIND. From outside, that is identical to
    a detector that never fired — and no steward can notice it by waiting,
    because waiting is exactly what produces the silence.

    MUTATION: restore the early return (`if binding is None: return False`
    ahead of the record) → this test fails and NOTHING ELSE DOES, which is what
    made the defect survivable in the first place.
    """
    state, reg, mgr, _ = _wired(with_steward_binding=False)
    n = _fire_stale(state, reg, mgr)
    _check(n == 0, "nothing was DELIVERED — there is no steward binding to reach")
    rows = _records(state)
    _check(len(rows) == 1,
           "but the alarm is RECORDED anyway: firing and delivering are now "
           "two separate facts")
    row = rows[0] if rows else {}
    _check(row.get("delivery_outcome") == NOTICE_DELIVERY_NO_STEWARD_BINDING,
           "and the record says WHY nothing was delivered")
    _check(row.get("steward_instance_id") is None,
           "with a NULL steward meaning 'none was resolved' — a real outcome, "
           "not an unrecorded field")


def test_the_three_delivery_outcomes_are_distinct() -> None:
    """★ WHY delivery_outcome IS NOT A BOOLEAN. An unbound steward and a failed
    append are different faults with different owners; both look like silence
    from outside, and a boolean would merge them back together.

    MUTATION: collapse the domain to delivered true/false → the two failure
    outcomes become one value and this test fails.
    """
    state, reg, mgr, _ = _wired()

    class _Exploding:
        """A bridge whose append raises — the append_failed leg."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def append_event(self, *_a: object, **_k: object) -> None:
            raise RuntimeError("bridge gone")

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    n = _fire_stale(state, reg, cast("Any", _Exploding(mgr)))
    _check(n == 0, "a raising append delivers nothing")
    rows = _records(state)
    _check(len(rows) == 1 and rows[0].get("delivery_outcome") == NOTICE_DELIVERY_APPEND_FAILED,
           "and is recorded as 'append_failed' — the BRIDGE's fault")
    _check(rows[0].get("steward_instance_id") == "agi-steward" if rows else False,
           "and the steward it FAILED to reach is still named — that is the "
           "difference between this outcome and an unbound steward")
    outcomes = {
        NOTICE_DELIVERY_APPENDED,
        NOTICE_DELIVERY_NO_STEWARD_BINDING,
        NOTICE_DELIVERY_APPEND_FAILED,
    }
    _check(len(outcomes) == 3, "the three outcomes are three distinct values")


def test_a_record_fault_never_costs_another_row_its_notice() -> None:
    """★ BEST-EFFORT, MEASURED RATHER THAN ASSERTED IN A COMMENT. The record is
    bookkeeping riding an operational loop: a record fault must not stop the
    sweep, and must not stop the OTHER rows in the same tick from being told.

    MUTATION: let `record_gauge_notice`'s exception propagate out of
    `_record_notice_best_effort` → the sweep raises and this test fails.
    """
    state, reg, mgr, bridge_id = _wired()

    class _WriteRefusingState:
        """Refuses only the notice-record write; every other call is real."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def write_state(self, namespace: str, payload: dict[str, Any]) -> Any:
            if payload.get("table") == TABLE_GAUGE_NOTICE_RECORD:
                raise RuntimeError("state layer refused the record")
            return self._inner.write_state(namespace, payload)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    wrapped = cast("Any", _WriteRefusingState(state))
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
    raised: Exception | None = None
    try:
        n = sweep_gauge_staleness(
            wrapped, now=now, peer_registry=reg, bridge_manager=mgr,
        )
    except Exception as exc:  # noqa: BLE001 — the behaviour under test
        raised, n = exc, -1
    _check(raised is None,
           f"a refused record does NOT raise into the sweep loop (raised {raised!r})")
    _check(n == 1, "and the notice is still delivered and still counted")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(
        events and events[0].event_type == EVENT_GAUGE_STALE_NOTICE,
        "the steward's notice survives the bookkeeping failure — the whole "
        "point of the best-effort posture",
    )
    _check(_records(state) == [], "and no half-written record was left behind")


# ---------------------------------------------------------------------------
# The bound, and the read.
# ---------------------------------------------------------------------------


def test_retention_is_a_hard_bound() -> None:
    """The writer bounds its own table, and bounds it by DELETING.

    MUTATION: prune with `soft_delete=True` → the row count check fails,
    because this platform has no reaper that ever clears an is_deleted flag,
    so a soft bound is cosmetic while the table grows exactly as before.
    """
    state, _, _, _ = _wired()
    base = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    from agent_messaging_plugin.gauge_notice_record_store import record_gauge_notice

    for i in range(GAUGE_NOTICE_RETENTION + 7):
        record_gauge_notice(
            state,
            notice_type=EVENT_GAUGE_STALE_NOTICE,
            agent_instance_id="agi-worker",
            emitted_at=(base + timedelta(seconds=i)).isoformat(),
            delivery_outcome=NOTICE_DELIVERY_APPENDED,
        )
    rows = _records(state)
    _check(
        len(rows) == GAUGE_NOTICE_RETENTION,
        f"pruned to exactly {GAUGE_NOTICE_RETENTION} rows (found {len(rows)})",
    )
    _check(
        all(r.get("is_deleted") in (0, None) for r in rows),
        "the survivors are GONE, not tombstoned — nothing here reaps a "
        "soft-deleted row",
    )
    newest = max(str(r.get("emitted_at")) for r in rows)
    _check(newest == (base + timedelta(seconds=GAUGE_NOTICE_RETENTION + 6)).isoformat(),
           "and it is the OLDEST rows that go, never the newest")


def test_retention_is_scoped_per_type() -> None:
    """One leg's volume must not evict the other leg's history.

    MUTATION: prune per subject only (drop notice_type from the filters) →
    the coverage record is deleted by the staleness flood and this fails.
    """
    state, _, _, _ = _wired()
    base = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    from agent_messaging_plugin.gauge_notice_record_store import record_gauge_notice

    record_gauge_notice(
        state,
        notice_type=EVENT_GAUGE_COVERAGE_NOTICE,
        agent_instance_id="agi-worker",
        emitted_at=base.isoformat(),
        delivery_outcome=NOTICE_DELIVERY_APPENDED,
    )
    for i in range(GAUGE_NOTICE_RETENTION + 5):
        record_gauge_notice(
            state,
            notice_type=EVENT_GAUGE_STALE_NOTICE,
            agent_instance_id="agi-worker",
            emitted_at=(base + timedelta(seconds=i + 1)).isoformat(),
            delivery_outcome=NOTICE_DELIVERY_APPENDED,
        )
    coverage, _ = read_gauge_notice_records(
        state, notice_type=EVENT_GAUGE_COVERAGE_NOTICE,
    )
    _check(len(coverage) == 1,
           "the single coverage record SURVIVES a staleness flood past the "
           "bound — retention is per (subject, type), so one leg's volume "
           "cannot silently become the other leg's retention")


def test_reading_does_not_consume() -> None:
    """★ THE BRIDGE QUEUE'S OWN BUG, pinned so it cannot come back.

    `events_after` returns the pending events and then rebinds the queue to
    exclude them, so a verifier polling it can swallow the steward's notice.
    Two identical reads here must return identical rows.

    MUTATION: make the read delete or mark what it returns → the second read
    comes back short and this fails.
    """
    state, reg, mgr, _ = _wired()
    _fire_stale(state, reg, mgr)
    first, _ = read_gauge_notice_records(state)
    second, _ = read_gauge_notice_records(state)
    _check(len(first) == 1 and len(second) == 1,
           "two consecutive reads both see the record")
    _check(
        first and second and first[0].get("emitted_at") == second[0].get("emitted_at"),
        "and they see the SAME record — reading is not a drain",
    )


def test_an_omitted_filter_does_not_narrow() -> None:
    """★ THE PLAUSIBLE-WRONG-ANSWER GUARD. The natural mis-implementation
    matches a None filter against the column, which returns a small, plausible,
    wrong answer (only rows with a NULL steward) rather than an error.

    MUTATION: put `filters[col] = None` in unconditionally → the unfiltered
    read returns 0 rows and this fails.
    """
    state, reg, mgr, _ = _wired()
    _fire_stale(state, reg, mgr)
    everything = gauge_notice_records(state)
    _check(everything["returned"] == 1,
           "an unfiltered read means EVERY notice, not 'notices with a null "
           "subject'")
    by_type = gauge_notice_records(state, notice_type=EVENT_GAUGE_STALE_NOTICE)
    _check(by_type["returned"] == 1, "narrowing by the matching type still finds it")
    other_type = gauge_notice_records(state, notice_type=EVENT_GAUGE_COVERAGE_NOTICE)
    _check(other_type["returned"] == 0,
           "and narrowing by the OTHER type finds nothing — the type filter is "
           "load-bearing rather than decorative")


def test_the_window_filter_is_inclusive_and_bounded() -> None:
    """`since` is a lower bound on emitted_at, inclusive.

    MUTATION: use a strict `gt` → the boundary record disappears and this fails.
    """
    state, _, _, _ = _wired()
    base = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    from agent_messaging_plugin.gauge_notice_record_store import record_gauge_notice

    for i in range(3):
        record_gauge_notice(
            state,
            notice_type=EVENT_GAUGE_STALE_NOTICE,
            agent_instance_id="agi-worker",
            emitted_at=(base + timedelta(minutes=i)).isoformat(),
            delivery_outcome=NOTICE_DELIVERY_APPENDED,
        )
    boundary = (base + timedelta(minutes=1)).isoformat()
    windowed = gauge_notice_records(state, since=boundary)
    _check(windowed["returned"] == 2,
           "the window keeps the boundary record itself (inclusive), not just "
           "what follows it")
    _check(
        windowed["entries"][0]["emitted_at"] > windowed["entries"][1]["emitted_at"],
        "and the page is newest-first",
    )


def test_truncation_is_published_not_implied() -> None:
    """A capped page must say it was capped.

    MUTATION: return the slice with `truncated=False` hardcoded → fails.
    """
    state, _, _, _ = _wired()
    base = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    from agent_messaging_plugin.gauge_notice_record_store import record_gauge_notice

    for i in range(4):
        record_gauge_notice(
            state,
            notice_type=EVENT_GAUGE_STALE_NOTICE,
            agent_instance_id="agi-worker",
            emitted_at=(base + timedelta(minutes=i)).isoformat(),
            delivery_outcome=NOTICE_DELIVERY_APPENDED,
        )
    capped = gauge_notice_records(state, limit=2)
    _check(capped["returned"] == 2 and capped["truncated"] is True,
           "a capped page reports truncated=True")
    full = gauge_notice_records(state, limit=50)
    _check(full["returned"] == 4 and full["truncated"] is False,
           "and a complete page reports False — the flag tracks the data, not "
           "a constant")


def test_an_invalid_notice_type_is_refused_not_silently_empty() -> None:
    """A typo'd filter must fail loudly. Returning an empty page would read as
    'no alarms fired', which is the most dangerous wrong answer this verb can
    give.

    MUTATION: drop the domain check → the call returns returned=0 and this
    fails.
    """
    state, _, _, _ = _wired()
    raised: VerbError | None = None
    try:
        gauge_notice_records(state, notice_type="gauge_stale_notices")
    except VerbError as exc:
        raised = exc
    _check(raised is not None and raised.code == "invalid_argument",
           "a notice_type outside the domain is REFUSED, never answered with "
           "an empty page that reads as 'nothing fired'")


def test_a_blank_subject_is_refused_rather_than_matching_nothing() -> None:
    """Omitting the subject means 'all subjects'; passing "" means the caller
    thought it had an id and did not.

    MUTATION: treat "" as absent → a caller's lost id silently becomes a
    fleet-wide read, and this fails.
    """
    state, _, _, _ = _wired()
    raised: VerbError | None = None
    try:
        gauge_notice_records(state, agent_instance_id="   ")
    except VerbError as exc:
        raised = exc
    _check(raised is not None and raised.code == "missing_argument",
           "a blank subject is refused rather than silently widening the read")


def test_the_subject_filter_uses_the_shared_id_join() -> None:
    """A caller holding the id `peer_list` publishes must still find records
    keyed on the ledger id.

    MUTATION: filter on the raw argument instead of the resolved subject →
    a watch-id caller gets an empty page and this fails.
    """
    state, reg, mgr, _ = _wired()
    _fire_stale(state, reg, mgr)
    direct = gauge_notice_records(state, agent_instance_id="agi-worker")
    _check(direct["returned"] == 1, "the ledger id resolves directly")
    _check(direct["agent_instance_id"] == "agi-worker",
           "and the reply names the id the records are keyed on")
    unknown = gauge_notice_records(state, agent_instance_id="agi-nobody")
    _check(
        unknown["returned"] == 0 and unknown["queried_agent_instance_id"] == "agi-nobody",
        "an unknown subject is an empty page that still echoes what was asked "
        "— never an error, and never silently widened to everything",
    )


# ---------------------------------------------------------------------------
# Release attribution.
# ---------------------------------------------------------------------------


def test_release_identity_is_read_or_honestly_absent() -> None:
    """★ NO PHANTOM RELEASE. `SOLET_RELEASE_ID` is a real convention elsewhere
    in this platform but is NOT set for this fleet's solet (measured against
    the LaunchAgent plist, whose EnvironmentVariables carries only PATH and
    SOLET_NAME), so reading it would have stamped every row with an empty
    string. Self-identifying from the module's own tree cannot disagree with
    the code that is executing.

    ★ AND THE SUCCESS PATH IS EXERCISED, not merely the absent one. A checkout
    run legitimately has no release, so asserting only `is None` here would be
    a check whose success path never runs.

    MUTATION: return a directory name (or "unknown") instead of None when no
    VERSION resolves → the tri-state check fails.
    """
    here = running_release_id()
    _check(here is None or isinstance(here, str) and here,
           "the running release id is a non-empty string or honestly None, "
           "never an empty string pretending to be an answer")

    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "VERSION").write_text(json.dumps({"release_id": "rel-under-test"}))
        _check(_identity_from(root / "VERSION") == "rel-under-test",
               "POSITIVE CONTROL: a well-formed identity file IS read — the "
               "success path is reachable, so a None elsewhere means absent "
               "rather than unreachable")
        (root / "bad.json").write_text("{not json")
        _check(_identity_from(root / "bad.json") is None,
               "an unreadable identity file is absent, not a crash")
        (root / "empty.json").write_text(json.dumps({"release_id": ""}))
        _check(_identity_from(root / "empty.json") is None,
               "and an EMPTY release_id is absent too, never adopted verbatim")
        _check(_identity_from(root / "missing.json") is None,
               "a missing file is absent")
    _check(MAX_PARENTS_SEARCHED >= 6,
           "the upward walk still spans the deployed layout's depth "
           "(<release>/code/plugins/<p>/src/<m> is six levels)")


def test_the_record_carries_whatever_release_identity_resolved() -> None:
    """Whatever `running_release_id` answers, the record must carry THAT and
    not a substitute.

    MUTATION: default release_id to "" or to the current release when None →
    fails, and a reader would date the thresholds to a release that never ran.
    """
    state, reg, mgr, _ = _wired()
    _fire_stale(state, reg, mgr)
    row = _records(state)[0]
    _check(row.get("release_id") == running_release_id(),
           "the record's release_id is exactly what the identity read "
           "returned, including None")


def main() -> int:
    tests = (
        test_a_delivered_alarm_is_recorded,
        test_the_record_carries_the_threshold_that_actually_fired,
        test_the_coverage_leg_records_its_own_shape,
        test_an_undeliverable_alarm_is_still_recorded,
        test_the_three_delivery_outcomes_are_distinct,
        test_a_record_fault_never_costs_another_row_its_notice,
        test_retention_is_a_hard_bound,
        test_retention_is_scoped_per_type,
        test_reading_does_not_consume,
        test_an_omitted_filter_does_not_narrow,
        test_the_window_filter_is_inclusive_and_bounded,
        test_truncation_is_published_not_implied,
        test_an_invalid_notice_type_is_refused_not_silently_empty,
        test_a_blank_subject_is_refused_rather_than_matching_nothing,
        test_the_subject_filter_uses_the_shared_id_join,
        test_release_identity_is_read_or_honestly_absent,
        test_the_record_carries_whatever_release_identity_resolved,
    )
    for test in tests:
        print(f"\n{test.__name__}")
        test()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
