#!/usr/bin/env python3
"""Unit smoke for GAU-15 item 4 — the gauge TAMPER CANARY, against its six
binding constraints.

WHAT A CANARY IS FOR. The GAU-01(b) staleness detector has never caught a live
freeze; it was validated against synthetic fixtures only. A canary proves the
instrument CAN catch, by arresting a synthetic gauge on the real write path and
checking the detector alarms — and by checking it stays quiet when nothing is
wrong, because an instrument that always alarms is not an instrument.

★ WHAT IT IS NOT, stated here because the distinction is easy to lose: this is
not a live catch, and must never be recorded as one. The live-catch obligation
is a separate evidence class and stays open.

THE SIX CONSTRAINTS, and where each is pinned:
  (a) real write path — `test_the_arrest_produces_the_real_signature` drives
      the actual sweep over rows written by the actual store functions;
      nothing is injected downstream.
  (b) detection path BLIND — `test_the_detector_is_blind_to_the_canary`: the
      sweep alarms on a canary exactly as on any session, and no canary table
      is reachable from its read.
  (c) provenance at the store plane — `test_the_mark_is_not_on_the_gauge_row`:
      the mark is its own table, joinable by operational consumers, absent from
      the row the detector reads.
  (d) audited tamper, no ambient mode — `test_every_tamper_is_attributable`
      and the refusal tests around it.
  (e) BOTH edges on independent evidence —
      `test_the_armed_edge`, `test_the_quiet_edge`, and
      `test_one_edge_alone_is_not_a_test`.
  (f) verifier consumes the sweep's OUTPUT — the durable notice record, never
      the consuming bridge queue: `test_the_verifier_reads_the_durable_record`.

★ MUTATIONS THIS CATCHES (a green that cannot name its failing mutation is not
evidence):
  * let the canary withhold its lifecycle tick too → the arrest stops looking
    like a freeze and the detector correctly ignores it
    → `test_the_arrest_produces_the_real_signature`
  * attribute an alarm by TIME alone, ignoring the expected type
    → `test_a_wrong_type_alarm_is_not_absorbed`
  * let a tamper target an unregistered session → `test_a_tamper_may_only_target_a_canary`
  * treat an undeployed detector as a FAIL rather than abstaining
    → `test_an_undeployed_detector_abstains`
  * make the arrest window closed at both ends → `test_the_window_is_half_open`

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/gauge_canary_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from _recorded_lane_worktree_fixture import RecordedLaneWorktreeFixture  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
)
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.gauge_canary import (  # noqa: E402
    SYNTHETIC_HOST,
    TICK_ARRESTED,
    TICK_REPORTED,
    VERDICT_ABSTAINED,
    VERDICT_FAIL,
    VERDICT_NO_EVIDENCE,
    VERDICT_PASS,
    arrest_in_force,
    canary_tick,
    direct_canary_arrest,
    register_synthetic_session,
    retire_gauge_canary,
    verify_canary,
)
from agent_messaging_plugin.gauge_canary_store import (  # noqa: E402
    CanaryError,
    is_active_canary,
    list_canaries,
    register_canary,
    retire_canary,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    PEER_BINDING_NAMESPACE,
    TABLE_GAUGE_CANARY_REGISTRY,
    TABLE_GAUGE_CANARY_TAMPER,
    TABLE_SESSION_CONTEXT_STATUS,
    WORK_CLASS_READ_ONLY,
    get_peer_binding_schema,
)
from agent_messaging_plugin.session_hosts import (  # noqa: E402
    HostMechanismMissingError,
    resolve_host_driver,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_sweep import (  # noqa: E402
    EVENT_GAUGE_COVERAGE_NOTICE,
    EVENT_GAUGE_STALE_NOTICE,
    last_report_alive,
    sweep_gauge_staleness,
)

CANARY = "agi-7f2c91"
"""The canary's identity, deliberately ORDINARY-LOOKING.

An earlier draft used "agi-canary" and this file's own blindness test caught it:
the staleness notice quotes the subject's instance id, so an identity named for
what it is announces itself in every alarm it provokes. Same deployment
constraint as the lane label below — the canary must be indistinguishable from a
real session in every field the detector can echo, or constraint (b) holds in
the code and leaks in the prose.
"""

STEWARD = "agi-steward"

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
        session_id_factory=lambda _n: "ags-http", idle_timeout_s=3600,
        max_pending_events=50, long_poll_timeout_s=1,
    )


def _spawn_live(state: Any, *, agent_instance_id: str, spawned_by: str = "") -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id, lane_id="lane-x", brief_ref="",
            work_class=WORK_CLASS_READ_ONLY, budget_line="b1", host="operator",
            report_by_seconds=5400, spawned_by_instance_id=spawned_by,
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
    )


def _wired() -> tuple[Any, PeerRegistry, Any, str]:
    """A registered canary, live, spawned by a steward with a real bridge."""
    state, reg, mgr = _state(), _peer_registry(), _bridge_manager()
    _spawn_live(state, agent_instance_id=STEWARD)
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": STEWARD}},
        {"agent_id": "claude_code"},
    )
    bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id, agent_id="claude_code", agent_instance_id=STEWARD,
            session_label=STEWARD, parent_pid=1,
        ),
    )
    register_canary(
        state, agent_instance_id=CANARY,
        purpose="prove the staleness detector can catch",
        registered_by="role:lane-gau-store",
    )
    # ★ THE FIXTURE NO LONGER BUILDS ITS OWN INPUT AT THIS SEAM. Until
    # 2026-08-19 this line was `_spawn_live(CANARY)` — the smoke calling
    # `insert_managed_session` directly, which is the ONE seam no verb exposed.
    # 51 checks passed while the canary was unexercisable in production, because
    # every one of them tested the callee instead of the wiring. It now goes
    # through the same verb an operator has, so a gap there fails HERE.
    register_synthetic_session(
        state, agent_instance_id=CANARY, lane_id="lane-x",
        spawned_by_instance_id=STEWARD, directed_by="role:lane-gau-store",
        report_by_seconds=5400,
    )
    # GAU-22(c): AGE the registration. `register_synthetic_session` stamps
    # `last_transition_at` at real wall-clock "now", while every exercise below
    # ticks the gauge at a synthetic PAST time — so the row reads "transitioned
    # just now, gauge already stale", which is exactly a /clear rotation and
    # exactly what the grace window correctly holds fire on. A canary that has
    # been standing watch is not one that just rotated; model that. This ages
    # an existing row, it does not bypass the registration verb the comment
    # above insists on.
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": CANARY}},
        {"last_transition_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
    )
    return state, reg, mgr, bridge_id


def _tick(state: Any, *, now: datetime, tokens: int = 120_000) -> dict[str, Any]:
    """Tick the canary at a SYNTHETIC time, keeping the row's clocks coherent.

    GAU-22(c): the tick writes the gauge at ``now`` (which callers set in the
    past), but the lifecycle transition it drives stamps ``last_transition_at``
    at REAL wall-clock now. That leaves the row reading "transitioned this
    instant, gauge already hours old" — the exact rotation signature the
    grace window holds fire on, and a state no real canary can be in: a
    session that last ticked 5400s ago transitioned at or before then.
    Ageing the transition to match the synthetic tick is what keeps this
    fixture's two clocks on the same timeline. Production is unaffected —
    only ``session_lifecycle_store``'s insert and ``transition_lifecycle_state``
    ever write this column, never an ordinary gauge update.
    """
    result = canary_tick(
        state, agent_instance_id=CANARY, current_tokens=tokens, ceiling=1_000_000,
        model="claude-sonnet-5", claude_session_id="c-canary",
        directed_by="role:lane-gau-store", now=now,
    )
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": CANARY}},
        {"last_transition_at": (now - timedelta(days=1)).isoformat()},
    )
    return result


def _stale_lifecycle(state: Any, *, last_alive: datetime) -> None:
    """Push the canary's lifecycle clock back so it reads STALE.

    ``report_alive`` re-arms ``report_by`` from the real wall clock, so a
    fixture that only ticks at a synthetic past time still leaves the lifecycle
    half fresh. Ageing it explicitly is what makes the next tick's lifecycle
    write observable.
    """
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": CANARY}},
        {
            "report_by_seconds": 5400,
            "report_by": (last_alive + timedelta(seconds=5400)).isoformat(),
            # GAU-22(c), same root fix as session_sweep_smoke.py's _ticking:
            # the spawn helper stamps last_transition_at at real wall-clock
            # "now", so a backdated gauge would read as "transitioned just
            # now, gauge already stale" — indistinguishable from a /clear
            # rotation, which the grace window correctly holds fire on. A
            # genuinely long-lived canary transitioned long ago; model that.
            "last_transition_at": (last_alive - timedelta(days=1)).isoformat(),
        },
    )


def _derived_last_alive(state: Any) -> datetime:
    """The canary's last ``report_alive``, derived the way production derives
    it — through the sweep's own one copy of that identity, never re-computed
    here."""
    row = read_managed_session(state, CANARY)
    derived = last_report_alive(row)
    if derived is None:
        raise AssertionError("the fixture's lifecycle row carries no window")
    return derived


def _arrest(state: Any, *, start: datetime, end: datetime,
            expected: str = EVENT_GAUGE_STALE_NOTICE) -> dict[str, Any]:
    return direct_canary_arrest(
        state, agent_instance_id=CANARY, directed_by="role:lane-gau-store",
        arrest_from=start.isoformat(), arrest_until=end.isoformat(),
        expected_notice_type=expected, reason="scheduled canary exercise",
    )


# ---------------------------------------------------------------------------
# (a) the real write path, and the signature it must produce.
# ---------------------------------------------------------------------------


def test_the_arrest_produces_the_real_signature() -> None:
    """★ CONSTRAINT (a). The arrest withholds ONLY the gauge write; the
    lifecycle clock keeps advancing through the real `report_alive`. That
    combination IS the GAU-01 signature, and it is what the detector keys on.

    MUTATION: withhold the lifecycle tick as well → both clocks stop together,
    the sweep correctly reads IDLE, no alarm fires, and this fails. That
    mutation is the tempting simplification ("just don't report"), and it would
    have produced a canary that silently tested nothing.
    """
    state, reg, mgr, _ = _wired()
    now = datetime.now(UTC)
    healthy = _tick(state, now=now - timedelta(seconds=5400))
    _check(healthy["outcome"] == TICK_REPORTED,
           "an untampered tick REPORTS through the real store path")
    # ★ AGE THE LIFECYCLE CLOCK BEFORE THE ARREST. Without this the healthy
    # tick above has already armed report_by against the REAL wall clock, so
    # the lifecycle half reads fresh no matter what the arrested tick does —
    # and a mutation that withheld the lifecycle tick too would pass unnoticed.
    # It did exactly that until this line was added: the mutation battery, not
    # review, is what found it.
    _stale_lifecycle(state, last_alive=now - timedelta(seconds=5400))
    _check(_derived_last_alive(state) < now - timedelta(seconds=3600),
           "the lifecycle clock is STALE going into the arrest, so only the "
           "arrested tick itself can refresh it")
    _arrest(state, start=now - timedelta(seconds=3600), end=now + timedelta(seconds=3600))
    arrested = _tick(state, now=now)
    _check(arrested["outcome"] == TICK_ARRESTED,
           "a tick inside the window withholds the gauge write")
    _check(arrested["measured_at"] is None, "and writes no reading at all")
    _check(_derived_last_alive(state) > now - timedelta(seconds=60),
           "★ but the arrested tick STILL REPORTS ALIVE — withholding only the "
           "gauge is what separates a freeze from an idle session, and it is "
           "the half a naive 'just don't report' canary would drop")
    fired = sweep_gauge_staleness(
        state, now=now, peer_registry=reg, bridge_manager=mgr,
    )
    _check(fired == 1,
           "so the REAL sweep alarms — lifecycle advancing, gauge frozen, the "
           "GAU-01 signature reproduced rather than simulated")


def test_the_detector_is_blind_to_the_canary() -> None:
    """★ CONSTRAINT (b). The sweep must treat a canary as an ordinary session.

    Pinned by evidence rather than by assertion: the alarm the detector emits
    about a canary carries nothing distinguishing it from one about a real
    session, because the detector has no way to know.

    ★ ONE REAL LEAK PATH, found by this test failing on its own fixture. The
    staleness notice echoes the session's ``lane_id`` verbatim, so a canary
    deployed into a lane NAMED for what it is would announce itself in every
    alarm — not a detector defect (the detector is faithfully reporting a field
    it was given) but a deployment constraint: the canary's lane label is
    operator-chosen and must be as ordinary as the rest of its identity. The
    fixture below uses a neutral lane for exactly that reason.
    """
    state, reg, mgr, bridge_id = _wired()
    now = datetime.now(UTC)
    _tick(state, now=now - timedelta(seconds=5400))
    _arrest(state, start=now - timedelta(seconds=3600), end=now + timedelta(seconds=3600))
    sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _, events = mgr.get(bridge_id).events_after(-1)
    body = events[0].content if events else ""
    _check(bool(body), "the detector emitted its notice (fixture precondition)")
    for word in ("canary", "tamper", "synthetic", "arrest"):
        _check(word not in body.lower(),
               f"the notice says nothing about {word!r} — the detector cannot "
               "know, so it cannot say")


def test_the_mark_is_not_on_the_gauge_row() -> None:
    """★ CONSTRAINT (c), and (b) binding harder than it. The provenance mark is
    a separate table; the row the DETECTOR reads carries no canary column.

    MUTATION: add an `is_canary` column to session_context_status → this fails,
    and the detector would then have a flag it could learn to read.
    """
    state, _, _, _ = _wired()
    _tick(state, now=datetime.now(UTC))
    rows = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_SESSION_CONTEXT_STATUS)
    _check(len(rows) == 1, "the canary wrote a gauge row (fixture precondition)")
    keys = set(rows[0]) if rows else set()
    _check(not any("canary" in k or "synthetic" in k or "tamper" in k for k in keys),
           "the gauge row the detector reads carries NO canary mark of any kind")
    marked = list_canaries(state)
    _check(len(marked) == 1 and marked[0]["agent_instance_id"] == CANARY,
           "while operational consumers CAN identify it, by joining the "
           "registry table deliberately")
    _check(is_active_canary(state, CANARY) and not is_active_canary(state, STEWARD),
           "and the join distinguishes canary from real session")


def test_a_retired_canary_still_explains_its_history() -> None:
    """Retire rather than delete: a stood-down canary still has to account for
    the alarms it produced while it ran."""
    state, _, _, _ = _wired()
    retire_canary(state, agent_instance_id=CANARY)
    _check(not is_active_canary(state, CANARY), "a retired canary is not active")
    _check(len(list_canaries(state)) == 0,
           "and drops out of the operational filter list")
    _check(len(list_canaries(state, include_retired=True)) == 1,
           "but is still on the record for anyone reading its old alarms")


def test_retire_gauge_canary_tears_down_the_ledger_row_too() -> None:
    """★ GAU-24, THE LEAK THIS CLOSES. Before this fix `retire_session` on a
    canary's ledger row raised `unsupported_on_host`, because `synthetic` had
    no host driver — the store-level `retire_canary` above only ever touched
    the registry mark, never the ledger row, so every exercise left one
    permanently-`live` `managed_session` row nothing could retire.

    This drives the row through `register_synthetic_session` + a tick (the
    same path `test_the_synthetic_session_is_visible_to_the_detector` proves
    the detector sees), then calls the new orchestration verb and checks BOTH
    halves land: the ledger row reaches `retired`, and the registry mark is
    stamped, in the ledger-first order the function's docstring commits to.

    MUTATION: revert `SyntheticHostDriver.terminate` to not exist at all
    (i.e. the host resolves to nothing) → `resolve_host_driver` raises
    `HostMechanismMissingError`, `retire_session` raises `unsupported_on_host`
    inside `retire_gauge_canary`, and this test's first `_check` goes red —
    the exact regression GAU-24 exists to prevent.
    """
    with tempfile.TemporaryDirectory() as raw:
        with RecordedLaneWorktreeFixture(Path(raw)) as fixture:
            state, _, _, _ = _wired()
            now = datetime.now(UTC)
            _tick(state, now=now)
            _check(read_managed_session(state, CANARY)["lifecycle_state"] == LIFECYCLE_LIVE,
                   "fixture precondition: the canary's ledger row is live before retiring")
            result = retire_gauge_canary(
                state, agent_instance_id=CANARY, directed_by="role:lane-canary-retire",
            )
            _check(result["session_row_existed"] is True,
                   "the verb found the ledger row minted by register_synthetic_session")
            _check(
                read_managed_session(state, CANARY)["lifecycle_state"] == LIFECYCLE_RETIRED,
                "★ and the no-op synthetic host driver let retire_session reach "
                "'retired' instead of raising unsupported_on_host — the ledger-side "
                "half of the leak is closed",
            )
            _check(not is_active_canary(state, CANARY),
                   "the registry mark was stamped too, ledger-first as documented")
            _check(
                fixture.has_recorded_provisioning() is False and bool(fixture.retirement_calls),
                "canary retirement is recorded through a temp-root-contained fixture",
            )


def test_retiring_a_canary_with_no_ledger_row_only_touches_the_registry() -> None:
    """A canary registered but never given a synthetic session (no ledger row
    yet) must still retire cleanly — the ledger step is conditional, not
    assumed."""
    state = _state()
    register_canary(
        state, agent_instance_id=CANARY, purpose="never spawned",
        registered_by="role:lane-canary-retire",
    )
    result = retire_gauge_canary(
        state, agent_instance_id=CANARY, directed_by="role:lane-canary-retire",
    )
    _check(result["session_row_existed"] is False,
           "no managed_session row existed, so retire_session was never called")
    _check(not is_active_canary(state, CANARY), "the registry mark is still retired")


def test_synthetic_is_a_declared_no_op_not_a_resolution_bypass() -> None:
    """★ NEGATIVE CONTROL for GAU-24's central safety claim: registering a
    no-op driver for the literal name 'synthetic' must not widen into "any
    host that fails to resolve gets a free pass". A host name that is
    genuinely unregistered — a typo, a future host not yet built — must still
    raise `HostMechanismMissingError`, exactly as before this fix.

    MUTATION: collapse `_driver_for` to return the synthetic driver for ANY
    unrecognised host (e.g. `_REGISTRY.get(key, _REGISTRY[SYNTHETIC_HOST])`)
    → this test's `HostMechanismMissingError` never raises and it fails,
    catching the exact regression this control exists to prevent: a real
    orphaned session on an unresolvable host getting silently marked
    retirable without anything being torn down.
    """
    _check(resolve_host_driver(SYNTHETIC_HOST, "claude_code")[1] == SYNTHETIC_HOST,
           "the declared literal 'synthetic' resolves to a registered driver")
    raised: HostMechanismMissingError | None = None
    try:
        resolve_host_driver("gau24-typo-host-never-registered", "claude_code")
    except HostMechanismMissingError as exc:
        raised = exc
    _check(raised is not None,
           "a genuinely unregistered host name still raises "
           "HostMechanismMissingError — the no-op path did not widen")


# ---------------------------------------------------------------------------
# (d) audited tamper — attribution, and the refusals that protect it.
# ---------------------------------------------------------------------------


def test_every_tamper_is_attributable() -> None:
    """★ CONSTRAINT (d). Who ordered it, the bounded window, and the alarm
    expected — all recorded BEFORE the outcome is known."""
    state, _, _, _ = _wired()
    now = datetime.now(UTC)
    row = _arrest(state, start=now, end=now + timedelta(seconds=600))
    _check(row["directed_by"] == "role:lane-gau-store",
           "the tamper records WHO directed it")
    _check(row["expected_notice_type"] == EVENT_GAUGE_STALE_NOTICE,
           "and which alarm it expects, recorded at arrest time so the "
           "verifier cannot grade its own expectations afterwards")
    logged = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_GAUGE_CANARY_TAMPER)
    _check(len(logged) == 1, "and it is on the audit log, not in an env var")


def test_a_tamper_may_only_target_a_canary() -> None:
    """The one mistake here that corrupts live data: tampering with a REAL
    session would manufacture a genuine-looking alarm about a real lane.

    MUTATION: drop the registration check → the tamper against the steward
    succeeds and this fails.
    """
    state, _, _, _ = _wired()
    now = datetime.now(UTC)
    raised: CanaryError | None = None
    try:
        direct_canary_arrest(
            state, agent_instance_id=STEWARD, directed_by="role:lane-gau-store",
            arrest_from=now.isoformat(),
            arrest_until=(now + timedelta(seconds=60)).isoformat(),
            expected_notice_type=EVENT_GAUGE_STALE_NOTICE, reason="should refuse",
        )
    except CanaryError as exc:
        raised = exc
    _check(raised is not None and raised.code == "not_a_canary",
           "a tamper against a REAL session is refused at the door")
    raised2: CanaryError | None = None
    try:
        _tick(state, now=now)
        canary_tick(
            state, agent_instance_id=STEWARD, current_tokens=1, ceiling=2,
            model="m", claude_session_id="c", directed_by="d", now=now,
        )
    except CanaryError as exc:
        raised2 = exc
    _check(raised2 is not None and raised2.code == "not_a_canary",
           "and so is writing a synthetic reading against a real session's row")


def test_an_unbounded_or_backwards_window_is_refused() -> None:
    """An arrest that never ends makes its alarms attributable forever, which
    is the same as not being attributable at all."""
    state, _, _, _ = _wired()
    now = datetime.now(UTC)
    for label, start, end in (
        ("backwards", now, now - timedelta(seconds=60)),
        ("empty", now, now),
    ):
        raised: CanaryError | None = None
        try:
            _arrest(state, start=start, end=end)
        except CanaryError as exc:
            raised = exc
        _check(raised is not None and raised.code == "invalid_window",
               f"a {label} arrest window is refused")
    raised3: CanaryError | None = None
    try:
        direct_canary_arrest(
            state, agent_instance_id=CANARY, directed_by="role:lane-gau-store",
            arrest_from=now.isoformat(),
            arrest_until=(now + timedelta(seconds=60)).isoformat(),
            expected_notice_type="not_a_real_notice", reason="x",
        )
    except CanaryError as exc:
        raised3 = exc
    _check(raised3 is not None and raised3.code == "invalid_expected_notice",
           "and an expected alarm outside the domain is refused — it could "
           "never be matched, so the arrest would be unverifiable by "
           "construction")


def test_the_window_is_half_open() -> None:
    """``[from, until)``. Closing both ends would make back-to-back windows
    overlap at their shared boundary, and an alarm there would be attributable
    to either — the one thing the audit log exists to prevent.

    MUTATION: use `start <= at <= end` → the boundary check fails.
    """
    now = datetime.now(UTC)
    window = [{
        "arrest_from": now.isoformat(),
        "arrest_until": (now + timedelta(seconds=60)).isoformat(),
        "expected_notice_type": EVENT_GAUGE_STALE_NOTICE,
    }]
    _check(arrest_in_force(window, at=now) is not None, "the start instant is INSIDE")
    _check(arrest_in_force(window, at=now + timedelta(seconds=59)) is not None,
           "an instant within is inside")
    _check(arrest_in_force(window, at=now + timedelta(seconds=60)) is None,
           "and the end instant is already RELEASED, so two adjacent windows "
           "never both claim it")
    _check(arrest_in_force([{"arrest_from": "nonsense", "arrest_until": "also"}],
                           at=now) is None,
           "an unreadable window is SKIPPED, never treated as covering all "
           "time — 'excused' is the answer that would hide a real fault")


# ---------------------------------------------------------------------------
# (e) + (f) both edges, judged against the durable record.
# ---------------------------------------------------------------------------


def _run_scheduled_exercise(state: Any, reg: Any, mgr: Any, *, now: datetime) -> None:
    """Healthy tick, then an arrest window that the sweep alarms inside."""
    _tick(state, now=now - timedelta(seconds=5400))
    _arrest(state, start=now - timedelta(seconds=600), end=now + timedelta(seconds=600))
    sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)


def test_the_verifier_reads_the_durable_record() -> None:
    """★ CONSTRAINT (f). The verdict is built from the durable notice record,
    NOT from the bridge queue — whose only reader consumes what it reads and
    would race the steward for the very alarm it is checking.

    Pinned by consequence: the steward's own queue is drained here FIRST, and
    the verifier still sees the alarm.
    """
    state, reg, mgr, bridge_id = _wired()
    now = datetime.now(UTC)
    _run_scheduled_exercise(state, reg, mgr, now=now)
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(len(events) == 1, "the steward's bridge queue held the notice")
    # MEASURED, not assumed: events_after(after) ACKS everything at or before
    # `after` and drops it. Reading with -1 acks nothing, which is why an
    # earlier draft of this test wrongly read as non-consuming. Acking with the
    # event's own cursor -- what a real consumer does -- destroys it.
    mgr.get(bridge_id).events_after(events[0].cursor)
    _, again = mgr.get(bridge_id).events_after(-1)
    _check(len(again) == 0,
           "and once a consumer ACKS it, the queue no longer has it AT ALL — "
           "drain-once, which is exactly why a verifier must never read the "
           "steward's queue to find out whether an alarm fired")
    result = verify_canary(
        state, agent_instance_id=CANARY, detector_deployed=True,
        now=now + timedelta(seconds=1200),
    )
    _check(result["alarms_examined"] == 1,
           "the verifier still sees the alarm, because it reads the durable "
           "record the drain cannot touch")


def test_the_armed_edge() -> None:
    """Edge 1: it ALARMED when tampered."""
    state, reg, mgr, _ = _wired()
    now = datetime.now(UTC)
    _run_scheduled_exercise(state, reg, mgr, now=now)
    result = verify_canary(
        state, agent_instance_id=CANARY, detector_deployed=True,
        now=now + timedelta(seconds=1200),
    )
    _check(result["verdict"] == VERDICT_PASS, f"verdict is pass ({result['verdict_reason']})")
    _check(result["windows_with_expected_alarm"] == 1,
           "the closed arrest window produced its expected alarm")
    _check(result["scheduled_alarms"] == 1 and result["unattributed_alarms"] == 0,
           "and the alarm is attributed to the schedule, not left unexplained")


def test_the_quiet_edge() -> None:
    """Edge 2: it was QUIET when healthy — and an alarm outside every window is
    reported as UNATTRIBUTED rather than absorbed."""
    state, reg, mgr, _ = _wired()
    now = datetime.now(UTC)
    # A healthy, advancing canary: no arrest at all, sweep sees nothing wrong.
    _tick(state, now=now - timedelta(seconds=60))
    fired = sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _check(fired == 0, "a healthy canary provokes NO alarm")
    quiet = verify_canary(state, agent_instance_id=CANARY, detector_deployed=True, now=now)
    _check(quiet["alarms_examined"] == 0 and quiet["unattributed_alarms"] == 0,
           "and the verifier sees a clean quiet edge")
    # Now an alarm with NO window covering it: a real fault, not ours.
    _tick(state, now=now - timedelta(seconds=5400))
    sweep_gauge_staleness(
        state, now=now, peer_registry=reg, bridge_manager=mgr,
    )
    unscheduled = verify_canary(
        state, agent_instance_id=CANARY, detector_deployed=True, now=now,
    )
    _check(unscheduled["unattributed_alarms"] == 1,
           "an alarm outside every logged window is UNATTRIBUTED")
    _check(unscheduled["verdict"] == VERDICT_FAIL,
           "which fails the quiet edge rather than being dismissed as canary "
           "noise — unattributed means real fault or a gap in the audit log, "
           "and both need a reader")


def test_one_edge_alone_is_not_a_test() -> None:
    """★ WHY (e) DEMANDS BOTH. A canary that never alarms passes the quiet edge
    and fails the armed one; the two are not each other's complement.

    MUTATION: judge only the armed edge → the always-alarming case passes.
    """
    state, _, _, _ = _wired()
    now = datetime.now(UTC)
    # An arrest window that CLOSED with no alarm at all: silent instrument.
    _arrest(state, start=now - timedelta(seconds=1200), end=now - timedelta(seconds=600))
    result = verify_canary(
        state, agent_instance_id=CANARY, detector_deployed=True, now=now,
    )
    _check(result["verdict"] == VERDICT_FAIL and result["silent_windows"] == 1,
           "a closed window with NO alarm fails the armed edge — the detector "
           "was deployed, the gauge was arrested, and nothing fired")


def test_an_open_window_is_not_yet_judged() -> None:
    """A window still open has not had its chance; counting it as a missing
    alarm would fail the canary for being asked too early — and it must not
    count as a PASS either, because nothing has been graded yet."""
    state, _, _, _ = _wired()
    now = datetime.now(UTC)
    _arrest(state, start=now - timedelta(seconds=60), end=now + timedelta(seconds=600))
    result = verify_canary(
        state, agent_instance_id=CANARY, detector_deployed=True, now=now,
    )
    _check(result["closed_windows"] == 0, "an open window is examined but not judged")
    _check(result["verdict"] == VERDICT_NO_EVIDENCE,
           "and the verdict is NO_EVIDENCE — not fail (it was asked too early) "
           "and not pass (nothing has been graded)")


def test_no_evidence_is_never_a_pass() -> None:
    """★ THE EMPTY CASE — the assertion this suite did not have.

    Every edge test above supplied at least one window, so the one input that
    exercises nothing was never run. With zero windows and zero alarms the
    verifier returned PASS, reading "0 closed windows each produced their
    expected alarm" as both edges holding: a vacuous green inside the instrument
    built to detect vacuous greens.

    ★ MUTATION, NAMED AND RUN (a green that names a mutation it never ran is
    not evidence — this suite made that mistake once already): make
    `_decide_verdict` return PASS when there is no evidence at all, i.e. delete
    the `if not matched_windows` branch. Both checks below go RED. The mutation
    is executed under `--mutate` at the bottom of this file rather than
    described, so the claim is measured on every run.
    """
    state, _, _, _ = _wired()
    now = datetime.now(UTC)
    result = verify_canary(
        state, agent_instance_id=CANARY, detector_deployed=True, now=now,
    )
    _check(result["windows_examined"] == 0 and result["alarms_examined"] == 0,
           "nothing was exercised: no windows, no alarms (the empty case)")
    _check(result["verdict"] == VERDICT_NO_EVIDENCE,
           "★ and the verdict is NO_EVIDENCE, never PASS — an unexercised "
           "canary and a detector that can no longer fire are identical from "
           "the quiet edge, so silence alone can never justify a pass")
    _check(result["windows_with_expected_alarm"] == 0,
           "the window count travels WITH the verdict, so a reader can tell "
           "whether the green was earned (the fleet-wide interim convention)")
    # The same emptiness, one step in: a canary that ticked healthily and was
    # never arrested. Quiet, correct, and still not a pass.
    _tick(state, now=now - timedelta(seconds=60))
    healthy = verify_canary(
        state, agent_instance_id=CANARY, detector_deployed=True, now=now,
    )
    _check(healthy["verdict"] == VERDICT_NO_EVIDENCE,
           "a healthy unexercised canary is UNTESTED, not passing — this is "
           "exactly the state the pipeline was in when it was called 'live and "
           "unfalsified' rather than proven")


def test_positive_evidence_is_what_earns_a_pass() -> None:
    """The discriminator, stated as its own check: PASS requires at least one
    CLOSED window that produced the alarm it asked for.

    Without this, `test_no_evidence_is_never_a_pass` could be satisfied by a
    verifier that never passes at all — which would be the opposite defect and
    just as useless.
    """
    state, reg, mgr, _ = _wired()
    now = datetime.now(UTC)
    _run_scheduled_exercise(state, reg, mgr, now=now)
    result = verify_canary(
        state, agent_instance_id=CANARY, detector_deployed=True,
        now=now + timedelta(seconds=1200),
    )
    _check(result["verdict"] == VERDICT_PASS and result["windows_with_expected_alarm"] >= 1,
           "one closed window that produced its expected alarm IS positive "
           "evidence, and it earns the pass")


def test_a_wrong_type_alarm_is_not_absorbed() -> None:
    """★ ATTRIBUTION REQUIRES TIME **AND** TYPE. A genuine coverage fault that
    happens to land during a staleness arrest must not be absorbed as "the
    alarm we asked for".

    MUTATION: attribute by time alone → the coverage alarm counts as scheduled,
    the window reads answered, and a real finding silently becomes a pass.
    """
    state, _, _, _ = _wired()
    now = datetime.now(UTC)
    from agent_messaging_plugin.gauge_notice_record_store import record_gauge_notice

    _arrest(state, start=now - timedelta(seconds=600), end=now - timedelta(seconds=60),
            expected=EVENT_GAUGE_STALE_NOTICE)
    record_gauge_notice(
        state, notice_type=EVENT_GAUGE_COVERAGE_NOTICE, agent_instance_id=CANARY,
        emitted_at=(now - timedelta(seconds=300)).isoformat(),
        delivery_outcome="appended",
    )
    result = verify_canary(
        state, agent_instance_id=CANARY, detector_deployed=True, now=now,
    )
    _check(result["unattributed_alarms"] == 1,
           "an alarm of the WRONG TYPE inside the window is not absorbed")
    _check(result["silent_windows"] == 1,
           "and the window still counts as unanswered — the arrest asked for a "
           "staleness alarm and never got one")
    _check(result["verdict"] == VERDICT_FAIL, "so the verdict is fail, not pass")


def test_an_undeployed_detector_abstains() -> None:
    """★ CONSTRAINT (a'). Expecting an alarm from a release whose sweep has no
    detector is a test that fails for the wrong reason, and reporting it as a
    canary failure would be worse than not running.

    MUTATION: treat detector_deployed False/None as a FAIL → this fails, and
    the canary would produce a false accusation about a working instrument.
    """
    state, _, _, _ = _wired()
    now = datetime.now(UTC)
    _arrest(state, start=now - timedelta(seconds=1200), end=now - timedelta(seconds=600))
    for deployed in (False, None):
        result = verify_canary(
            state, agent_instance_id=CANARY,
            detector_deployed=cast("Any", deployed), now=now,
        )
        _check(result["verdict"] == VERDICT_ABSTAINED,
               f"detector_deployed={deployed!r} ABSTAINS rather than accusing "
               "an instrument that is not running")
        _check("not running" in result["verdict_reason"]
               or "ABSENT" in result["verdict_reason"]
               or "not established" in result["verdict_reason"],
               f"and says why (detector_deployed={deployed!r})")


def test_the_named_mutation_goes_red() -> None:
    """★ THE MUTATION, EXECUTED — not described.

    `_decide_verdict` is replaced in-process with the exact mutant named above:
    the no-evidence branch deleted, so an unexercised run falls through to PASS.
    The empty case is re-run against it and must come back PASS, which is the
    proof that `test_no_evidence_is_never_a_pass` would go RED under this
    mutation rather than passing for a reason unrelated to the fix.

    A suite that names five mutations and runs four is how the fifth stays
    unmeasured — this file paid that price on 2026-08-19, so its own mutation
    now runs on every invocation.
    """
    import agent_messaging_plugin.gauge_canary as gc

    original = gc._decide_verdict

    def mutant(**kwargs: Any) -> tuple[str, str]:
        """The pre-fix rule: capability, armed edge, quiet edge, then PASS."""
        if kwargs["detector_deployed"] is not True:
            return VERDICT_ABSTAINED, "mutant: not deployed"
        if kwargs["silent_windows"]:
            return VERDICT_FAIL, "mutant: silent window"
        if kwargs["unattributed"]:
            return VERDICT_FAIL, "mutant: unattributed alarm"
        return VERDICT_PASS, "mutant: both edges hold (vacuously)"

    state, _, _, _ = _wired()
    now = datetime.now(UTC)
    gc._decide_verdict = mutant  # noqa: SLF001 — the mutation IS the measurement
    try:
        mutated = verify_canary(
            state, agent_instance_id=CANARY, detector_deployed=True, now=now,
        )
    finally:
        gc._decide_verdict = original
    _check(mutated["verdict"] == VERDICT_PASS,
           "under the mutation the empty case returns PASS — so the fix's "
           "assertions are load-bearing, not incidentally true")
    restored = verify_canary(
        state, agent_instance_id=CANARY, detector_deployed=True, now=now,
    )
    _check(restored["verdict"] == VERDICT_NO_EVIDENCE,
           "and the restored code answers NO_EVIDENCE on the same input — the "
           "discriminator survived its own mutation test")


# ---------------------------------------------------------------------------
# The exercisability gap: a canary the detector can actually see.
# ---------------------------------------------------------------------------


def test_the_synthetic_session_is_visible_to_the_detector() -> None:
    """★ THE GAP THIS CLOSES. The staleness leg inspects LIVE managed_session
    rows; `insert_managed_session` had one caller, inside `spawn_session`, which
    always dispatches a real process. A canary therefore had no reachable way to
    acquire the row the detector reads — landed, deployed, unexercisable.

    This drives the WHOLE chain through verbs only: register the canary,
    register its synthetic session, tick it (which promotes it to `live` through
    the real `report_alive`), arrest it, and let the REAL sweep judge.

    MUTATION: have `register_synthetic_session` leave `spawned_by_instance_id`
    empty → the leg skips the row for want of a steward and `fired` is 0.
    """
    state, reg, mgr = _state(), _peer_registry(), _bridge_manager()
    _spawn_live(state, agent_instance_id=STEWARD)
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": STEWARD}},
        {"agent_id": "claude_code"},
    )
    bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id, agent_id="claude_code", agent_instance_id=STEWARD,
            session_label=STEWARD, parent_pid=1,
        ),
    )
    register_canary(
        state, agent_instance_id=CANARY, purpose="exercise the detector",
        registered_by="role:lane-gau-store",
    )
    minted = register_synthetic_session(
        state, agent_instance_id=CANARY, lane_id="lane-x",
        spawned_by_instance_id=STEWARD, directed_by="role:lane-gau-store",
        report_by_seconds=5400,
    )
    _check(minted["lifecycle_state"] == LIFECYCLE_SPAWNING,
           "the row is minted in SPAWNING, exactly where spawn_session leaves "
           "it — no bespoke promotion rule is invented here")
    _check(minted["host"] == SYNTHETIC_HOST,
           "declaring a host whose driver is registered but DEGENERATE "
           "(GAU-24), so spawn/drive/clear still refuse this row loudly "
           "instead of pointing a keystroke at a pane that does not exist — "
           "only terminate/retire get a no-op path, see "
           "test_retire_gauge_canary_tears_down_the_ledger_row_too")
    now = datetime.now(UTC)
    _tick(state, now=now - timedelta(seconds=5400))
    _check(read_managed_session(state, CANARY)["lifecycle_state"] == LIFECYCLE_LIVE,
           "★ and the first tick promotes it to LIVE through the real "
           "report_alive — production's own transition, which is what makes it "
           "a row the detector will inspect")
    _stale_lifecycle(state, last_alive=now - timedelta(seconds=5400))
    _arrest(state, start=now - timedelta(seconds=3600), end=now + timedelta(seconds=3600))
    _tick(state, now=now)
    fired = sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _check(fired == 1,
           "★ the REAL sweep alarms on a session that was never spawned as a "
           "process — the canary is exercisable end-to-end, through verbs only")
    verdict = verify_canary(
        state, agent_instance_id=CANARY, detector_deployed=True,
        now=now + timedelta(seconds=7200),
    )
    _check(verdict["verdict"] == VERDICT_PASS
           and verdict["windows_with_expected_alarm"] == 1,
           "and the verifier grades it on POSITIVE evidence: 1 closed window "
           f"that produced its expected alarm ({verdict['verdict']}, "
           f"{verdict['windows_with_expected_alarm']} window(s))")


def test_a_synthetic_session_may_only_be_minted_for_a_canary() -> None:
    """★ THE INVERSE OF `not_a_canary`, and the reason this verb is safe to
    exist at all. It mints a lifecycle row with no process behind it — which is
    the tamper path the canary exists to detect if it can be pointed anywhere
    else.

    MUTATION: drop the registration check → a real (or arbitrary) identity gets
    a fabricated live session and this fails.
    """
    state, _, _, _ = _wired()
    for label, target in (("a real session", STEWARD), ("an unknown id", "agi-nobody")):
        raised: CanaryError | None = None
        try:
            register_synthetic_session(
                state, agent_instance_id=target, lane_id="lane-x",
                spawned_by_instance_id=STEWARD, directed_by="role:lane-gau-store",
                report_by_seconds=5400,
            )
        except CanaryError as exc:
            raised = exc
        _check(raised is not None and raised.code == "not_a_canary",
               f"minting a synthetic session for {label} is refused at the door")


def test_the_refusals_reproduce_the_detectors_own_skips() -> None:
    """Two refusals that look like parameter validation and are not: each one
    reproduces, loudly, a skip the staleness leg performs SILENTLY.

    A canary registered without a steward, or with no report window, is not
    merely misconfigured — it is invisible to the instrument it exists to
    exercise, and its later silence would be misread as the detector failing.
    That is a false RED on a working instrument, which is the same class of
    error as the false GREEN the no_evidence fix removes.
    """
    state, _, _, _ = _wired()
    retire_canary(state, agent_instance_id=CANARY)
    register_canary(state, agent_instance_id="agi-4b81de", purpose="p",
                    registered_by="role:lane-gau-store")
    cases = (
        ("missing_steward", {"spawned_by_instance_id": "  "}),
        ("missing_report_window", {"report_by_seconds": 0}),
        ("missing_directed_by", {"directed_by": ""}),
    )
    for code, override in cases:
        kwargs: dict[str, Any] = {
            "agent_instance_id": "agi-4b81de", "lane_id": "lane-x",
            "spawned_by_instance_id": STEWARD, "directed_by": "role:lane-gau-store",
            "report_by_seconds": 5400,
        }
        kwargs.update(override)
        raised: CanaryError | None = None
        try:
            register_synthetic_session(state, **kwargs)
        except CanaryError as exc:
            raised = exc
        _check(raised is not None and raised.code == code,
               f"{code}: refused loudly rather than minting a canary the "
               "detector would silently skip")


def test_a_self_announcing_identity_is_refused() -> None:
    """★ CONSTRAINT (b) LEAKING IN PROSE — the trap this lane paid for once.
    The staleness notice quotes the subject's `agent_instance_id` and `lane_id`
    verbatim, so a canary named for what it is announces itself in every alarm.

    Checked on exactly the two fields the notice echoes, and on no others:
    `host` is `synthetic` by design and no notice quotes it.

    MUTATION: drop the check → `lane-canary` mints happily and the blindness
    test's own leak path reopens.
    """
    state, _, _, _ = _wired()
    register_canary(state, agent_instance_id="agi-canary-9", purpose="p",
                    registered_by="role:lane-gau-store")
    register_canary(state, agent_instance_id="agi-5d20ab", purpose="p",
                    registered_by="role:lane-gau-store")
    for aid, lane in (("agi-canary-9", "lane-x"), ("agi-5d20ab", "lane-tamper-probe")):
        raised: CanaryError | None = None
        try:
            register_synthetic_session(
                state, agent_instance_id=aid, lane_id=lane,
                spawned_by_instance_id=STEWARD, directed_by="role:lane-gau-store",
                report_by_seconds=5400,
            )
        except CanaryError as exc:
            raised = exc
        _check(raised is not None and raised.code == "self_announcing_identity",
               f"({aid}, {lane}) is refused — the alarm would name the canary")


def test_one_canary_gets_one_session() -> None:
    """A second lifecycle row for one canary would give it two identities in
    the ledger, and its alarms two rows to be attributed to."""
    state, _, _, _ = _wired()
    raised: CanaryError | None = None
    try:
        register_synthetic_session(
            state, agent_instance_id=CANARY, lane_id="lane-x",
            spawned_by_instance_id=STEWARD, directed_by="role:lane-gau-store",
            report_by_seconds=5400,
        )
    except CanaryError as exc:
        raised = exc
    _check(raised is not None and raised.code == "session_exists",
           "a canary that already has a managed_session row is refused a second")


def test_the_registry_refuses_a_silent_reregistration() -> None:
    """Provenance that can be overwritten without trace is not provenance."""
    state, _, _, _ = _wired()
    raised: CanaryError | None = None
    try:
        register_canary(state, agent_instance_id=CANARY, purpose="second",
                        registered_by="someone-else")
    except CanaryError as exc:
        raised = exc
    _check(raised is not None and raised.code == "already_registered",
           "a re-registration under a different owner is refused")
    rows = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_GAUGE_CANARY_REGISTRY)
    _check(len(rows) == 1 and rows[0]["registered_by"] == "role:lane-gau-store",
           "and the original ownership record is intact")


def main() -> int:
    tests = (
        test_the_arrest_produces_the_real_signature,
        test_the_detector_is_blind_to_the_canary,
        test_the_mark_is_not_on_the_gauge_row,
        test_a_retired_canary_still_explains_its_history,
        test_retire_gauge_canary_tears_down_the_ledger_row_too,
        test_retiring_a_canary_with_no_ledger_row_only_touches_the_registry,
        test_synthetic_is_a_declared_no_op_not_a_resolution_bypass,
        test_every_tamper_is_attributable,
        test_a_tamper_may_only_target_a_canary,
        test_an_unbounded_or_backwards_window_is_refused,
        test_the_window_is_half_open,
        test_the_verifier_reads_the_durable_record,
        test_the_armed_edge,
        test_the_quiet_edge,
        test_one_edge_alone_is_not_a_test,
        test_an_open_window_is_not_yet_judged,
        test_no_evidence_is_never_a_pass,
        test_positive_evidence_is_what_earns_a_pass,
        test_the_named_mutation_goes_red,
        test_the_synthetic_session_is_visible_to_the_detector,
        test_a_synthetic_session_may_only_be_minted_for_a_canary,
        test_the_refusals_reproduce_the_detectors_own_skips,
        test_a_self_announcing_identity_is_refused,
        test_one_canary_gets_one_session,
        test_a_wrong_type_alarm_is_not_absorbed,
        test_an_undeployed_detector_abstains,
        test_the_registry_refuses_a_silent_reregistration,
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
