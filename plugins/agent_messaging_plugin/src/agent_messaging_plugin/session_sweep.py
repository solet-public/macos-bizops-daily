"""D1 platform sweep (§3.4 / §6 rule 3 edge ownership) — an ``on_tick`` rider
that piggybacks the existing :class:`BridgeLifecycleSweeper` cadence (same
pattern as the INF-02 serve-timeout sweep / INF-06 forwarded-vertex re-drive /
REL-05 deaf-wake escalation composed in ``plugin.py``'s ``_on_sweep_tick``),
rather than growing a new thread.

Three responsibilities, all owned by "the platform sweep" per the design's own
edge-ownership table:

1. :func:`sweep_overdue_sessions` — mark ``live``/``idle`` rows ``overdue``
   once their ``report_by`` deadline has passed (the self-report contract),
   then best-effort notify each row's steward (``spawned_by_instance_id`` —
   D2-lane-tail follow-up #3, fixing a measured gap where the transition
   fired with no notification of any kind). A row with no ``report_by`` (an
   operator-hosted session with no such contract) is never swept — absence
   means no contract, not an expired one. Also sweeps ``spawning`` rows past
   their ``report_by`` deadline using native liveness. An observed-alive host
   earns an announced report rearm; unknown liveness leaves the row pending;
   only an observed-dead host permits termination. No age cap applies.
2. :func:`sweep_deadline_dependencies` — fire + deliver armed
   ``session_dependency`` rows whose ``condition_kind == 'deadline'`` and
   ``condition_ref`` (an ISO timestamp) has passed.
3. :func:`sweep_lane_closed_dependencies` — fire + deliver armed rows whose
   ``condition_kind == 'lane_closed'`` once every ``managed_session`` row
   carrying that ``condition_ref`` (a ``lane_id``) is terminal
   (``terminated``/``retired``). A lane with NO managed_session rows yet is
   NOT closed (no vacuous truth on an empty set — a lane that never spawned
   is unstarted, not finished).

Scope boundary (deliberate, not an oversight):

* ``condition_kind == 'session_terminal'`` is NOT handled here.
  ``terminate_session`` (``session_lifecycle_verbs.py``) fires + best-effort
  delivers those edges (guarded by ``fired_at IS NULL``); ``retire_session``
  composes ``terminate_session`` and no longer fires them itself. Two
  writers racing to fire the SAME guarded condition is the
  second-guard-makes-the-first-vacuous shape; owning it in exactly one place
  keeps that unambiguous. FULLY RESOLVED 2026-08-04 (acceptance Test C +
  fix-slice completion, coordinator-seat ruling) — this section used to carry TWO
  successive debt claims, both now closed: (1) firing itself never worked
  live — ``_fire_session_terminal_dependencies``' guard filters carried a
  bare ``None`` for ``fired_at`` instead of ``{"op": "is_null"}``, which the
  postgres provider compiles to an always-false ``col = NULL`` comparison,
  matching zero rows, always; fixed (both the query and the predicated
  update). (2) firing carried no delivery — fixed same slice:
  ``_fire_session_terminal_dependencies`` now calls
  :func:`session_lifecycle_verbs.drive_on_delivery` per fired edge,
  best-effort, same containment contract as ``_deliver_dependency_wake``
  above. Both debts closed; no ``session_terminal`` tracked debt remains.
* ``lane_closed`` replaced the original spec's ``lane_landed`` (Dawn ruling
  2026-08-03, arm-124065ee): no canonical "lane landed" observable exists
  platform-side (an ancestor check would bind the platform to a dev git
  checkout — seed instances are not checkouts). ``lane_closed`` is what the
  registry can actually observe; true work-product landing needs a lane
  ENTITY with an audited declaration, deferred to Phase C. No verb in this
  codebase arms a ``session_dependency`` row yet (there is no "create
  dependency" verb) — the evaluator exists so the FIRST caller that does
  isn't waiting on the sweep too.
* A ``deadline``/``lane_closed`` edge with an empty ``waiter_instance_id``
  (lane-scoped) is fired (state) but its wake delivery is a logged no-op —
  resolving "the session currently working lane L" has no defined mapping in
  this slice.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import (
    require_deleted,
    require_records,
    require_updated,
)
from ananta.services.state_service.bounded_read import iter_table_rows

from . import rotation_thresholds
from .gauge_notice_emit import deliver_and_record_gauge_notice
from .managed_dispatch import (
    EVENT_MANAGED_DISPATCH_NOTICE,
    sweep_managed_dispatches,
)
from .overdue_notice import EVENT_SESSION_OVERDUE_NOTICE, _notify_steward_of_overdue
from .peer_registry import PeerAmbiguousError, PeerSessionAmbiguousError, PeerUnreachableError
from .schema import (
    CONDITION_DEADLINE,
    CONDITION_LANE_CLOSED,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    TABLE_SESSION_DEPENDENCY,
    TABLE_SESSION_ROLE_CLAIM,
)
from .session_context_status_store import read_session_context_status
from .session_hosts import OPERATOR_HOST
from .session_lifecycle_store import (
    StaleLifecycleStateError,
    list_managed_sessions,
    mark_registration_overdue,
    transition_lifecycle_state,
)
from .session_lifecycle_verbs import (
    REPORT_BY_SOURCE_EXPLICIT_SELF_REPORT,
    REPORT_BY_SOURCE_OBSERVED_SPAWNING,
    VerbError,
    _rearm_report_by,
    _resolve_termination_driver,
    drive_on_delivery,
    terminate_session,
)
from .steward_notice_counts import StewardNoticeCounts, fill_counts
from .steward_resolution import managed_session_agent_id, resolve_steward_binding

if TYPE_CHECKING:
    from collections.abc import Callable

    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .bridge_sessions import BridgeSessionManager
    from .models import BridgeBinding
    from .peer_registry import PeerRegistry

logger = logging.getLogger(__name__)

# Longer than the binding-liveness window (90s, bridge_sessions.py's
# DEFAULT_BINDING_LIVENESS_WINDOW_S — 3x the long-poll cadence) and comfortably
# longer than a blue-green cutover's fleet-wide re-registration burst (the
# measured trap Architect ratification #3 names): a row absent for less than
# this is "mid-bounce", not stale.
DEFAULT_PRUNE_GRACE_WINDOW_S: float = 300.0

# Channel event type the deadline wake rides — mirrors REL-05's
# ``ESCALATION_EVENT_TYPE`` naming convention (a distinct type per concern,
# never the generic peer_message type, so a receiver can tell wake classes apart).
EVENT_SESSION_DEPENDENCY_WAKE = "session_dependency_wake"
# Same convention, the D2-lane-tail overdue-steward-notice fix (follow-up
# #3): the self-report contract's own steward notification, distinct from
# the session_dependency wake above even though both ride append_event.

# Same convention again, for the two L4 sweep legs (2026-08-17).
#
# WHY THESE LIVE IN THE SWEEP AND NOT IN A HOOK. The reporting hook binds
# PostToolUse, which fires AFTER a tool call, which follows a model call. So
# during an idle gap it does not tick at all, and by its first tick after
# resuming the full-price prefix rewrite has already been paid. It cannot warn
# before the cost, by construction. The sweep walks sessions on its OWN
# schedule without any session having to act first, which makes it the only
# surface that can observe "this session has been idle a while, its context is
# large, its cache is about to lapse" BEFORE the next call pays for it.
EVENT_ROTATION_DUE_NOTICE = "rotation_due_notice"

# The gauge-coverage leg. Signature it detects: the platform believes a session
# is LIVE (its lifecycle row says so, i.e. report_alive is landing) while NO
# session_context_status row exists for it -- hooks are running, the gauge
# write is not.
#
# This is a MISSING-row detector, not a stale-row detector: a session whose
# gauge froze while keeping an old row is a different case and is deliberately
# out of scope here rather than half-covered.
#
# It is state-based on purpose. The obvious alternative -- compare the hook's
# throttle-marker mtime against the absent row -- needs AGENT_HEARTBEAT_MARKER_DIR,
# which the spawn adapters export to Claude Code sessions and which the platform
# process running this sweep has no reason to hold. A detector that silently
# no-ops when its input is missing is the exact fail-open shape this leg exists
# to catch, so building it that way would put the bug inside its own alarm.
EVENT_GAUGE_COVERAGE_NOTICE = "gauge_coverage_notice"

# Minimum age a LIVE row must reach before its missing gauge row counts as
# DARKNESS rather than STARTUP (R4 lane, 2026-08-17).
#
# Measured cost of not having this: at 16:33:11Z the gauge leg fired for four
# sessions with the prose "the reporting path ... is failing SILENTLY". All four
# were ~2 minutes old, had simply not completed a first reporting tick, and every
# one of them reported normally minutes later. The alarm was false four times in
# its first live wave.
#
# A newly LIVE session is dark BY CONSTRUCTION until its first hook tick lands,
# so without an age predicate every spawn wave manufactures one false alarm per
# lane. The latch cannot absorb this: each wave is a new episode with new keys,
# so the suppression that stops a repeat does nothing about a fresh false
# positive. That makes it a code fix, not a tuning question — and the harm is the
# one NoticeLatch's own docstring names, a channel the reader learns to skim.
#
# THE COST OF THE FIX, STATED: a genuinely broken session is now surfaced up to
# this many seconds later than before. That is the trade — latency for precision
# — and it is cheap in the direction that matters, because the outage this leg
# was built for ran for an unknown number of HOURS before a person found it.
#
# GAU-13 (2026-08-18): 300.0 WAS SHORTER THAN THE BOOT IT EXISTS TO COVER, and
# the leg went on manufacturing the false alarm the grace was added to stop.
# Three measured data points, all spawned tmux workers:
#   * lane-gau10-stall-boolean — spawned 15:35:12Z, notice fired 15:38Z, row
#     landed HEALTHY at 15:43:22Z. Boot-to-first-tick ~8 min.
#   * lane-r2-holds-false — spawned 16:30:31Z, notice fired ~16:34Z, work
#     dispatch reached the lane ~16:38Z, so its first WORK turn began >=7.5 min
#     post-spawn.
#   * that same lane's row, checked later, was present and healthy once work
#     turns ticked — a confirming third point, not a new investigation.
# The gap is STRUCTURAL, not incidental: a managed worker goes spawn ->
# charter dispatch -> first WORK turn, and the bootstrap-ack turn lands no gauge
# tick, so the row cannot exist until a dispatch arrives. 600.0 is the worst
# measured (~480s) plus headroom, and the test names the measurement so the
# constant cannot drift back under it silently.
#
# WHY THE NUMBER IS NOT THE WHOLE FIX. A wall-clock grace can only ever be a
# guess about someone else's dispatch latency, so this leg no longer rests on it
# alone: :func:`_first_tick_landed` reads whether report_alive has landed since
# the row went live, which is DIRECT evidence about the thing the grace was
# proxying for. The grace remains as the floor for rows where that evidence is
# unavailable, and as the "has it had time at all" question the evidence does
# not answer on its own.
GAUGE_COVERAGE_GRACE_S: float = 600.0

# How far past ``last_transition_at`` a derived last-report_alive must land
# before it counts as a completed tick. The lifecycle row's report_by is armed
# at spawn and the spawning->live transition follows within milliseconds
# (measured 2026-08-18 across three live lanes: 285ms, 265ms, 270ms), so the
# two timestamps are all but identical for a session that has never reported.
# The margin keeps that millisecond gap from reading as a tick; it is not a
# tuning knob for how long a boot may take.
FIRST_TICK_EVIDENCE_MARGIN_S: float = 5.0

# GAU-01(b) (2026-08-18): the leg that catches a gauge row which STOPPED, as
# distinct from one that was never written.
#
# A FROZEN ROW READS AS COVERAGE TODAY. :func:`_gauge_dark_session` returns None
# the moment ``read_session_context_status`` finds ANY row, so the L4b leg is
# blind to the shape GAU-01 actually presented: a LIVE, working session whose
# gauge row sat unchanged for 85 minutes while it kept completing tool calls.
# Absence was detected; arrest was not.
EVENT_GAUGE_STALE_NOTICE = "gauge_stale_notice"

# How far the last report_alive may lead the gauge row's ``measured_at`` before
# the gauge counts as ARRESTED rather than merely between writes.
#
# THIS IS A LAG BETWEEN TWO CLOCKS, NOT AN AGE. An absolute "measured_at older
# than X" bound cannot work here and the reason is structural: both writers are
# PostToolUse hooks, so a session that is simply idle between turns writes
# NEITHER row, and its gauge goes stale while nothing whatsoever is wrong. Only
# the DIVERGENCE separates the two -- report_alive advancing while the gauge
# does not means the session is completing tool calls and exactly one of its two
# reporters is failing. That is the GAU-01 signature, and it is why this leg
# keys on the gap rather than on either timestamp alone.
#
# DERIVATION OF THE NUMBER, measured rather than chosen:
#   * The gauge reporter (``rotation_due_watch.py``) throttles at 120.0s and the
#     heartbeat (``heartbeat_report_alive.py``) at 180.0s -- both read out of the
#     hooks on 2026-08-18, not taken from a report.
#   * On a session completing tool calls, the gauge throttles FASTER than the
#     heartbeat, so the gauge is normally the FRESHER of the two.
#   * A FIRST DRAFT OF THIS COMMENT CLAIMED the healthy positive lag was
#     therefore bounded by the gauge's 120s throttle. THAT CLAIM IS FALSE and
#     was falsified by measurement rather than by review: a live negative
#     control over the running fleet on 2026-08-18T23:52Z recorded lags of
#     +19.2s, +65.0s and +178.8s, all on healthy lanes. The 178.8s breaks the
#     predicted ceiling outright. The clean model was wrong because report_alive
#     does NOT land only from the 180s-throttled hook -- an explicit
#     report_alive call (a worker reporting status, a registration path) advances
#     the lifecycle clock with no corresponding gauge write, so the two clocks
#     are not the symmetric pair the model assumed.
#   * What is therefore claimed here is only what was observed: across the
#     ~45-minute probe (+109.8s to -125.6s) and the live control (max +178.8s),
#     the widest healthy positive lag seen to date is ~180s. THE TAIL IS NOT
#     MEASURED -- no claim is made about the true maximum, and the bound below
#     is chosen for headroom over the observed range, not derived from a proven
#     ceiling.
#   * 900s is 7.5x the gauge throttle and 3 sweep ticks
#     (``bridge_sweep_interval_seconds``, 300s). It absorbs SEVEN consecutive
#     missed gauge writes before firing. One transient miss (the hook's solet
#     call has a 20s timeout and is non-fatal by design) adds up to one throttle
#     window; a threshold anywhere near 120s would fire on a single one.
#
# HEADROOM IS THE POINT, and it is GAU-13's lesson taken one leg over: that
# grace was set from a model that turned out to be one leg short of the real
# boot path, and it under-measured. The falsified 120s ceiling above is the same
# lesson arriving a second time inside this very constant's derivation, which is
# why the wrong model is left in the comment rather than quietly deleted: a
# reader tempted to tighten this number should see that the tidy argument for a
# smaller one has already been tried and measured false. The cost asymmetry here is steep and
# one-sided -- a late notice on an 85-minute freeze loses nothing, while a
# notice that fires on a healthy 130s skew trains the reader to skim the channel
# the REAL arrest arrives on. Erring long is the cheap direction.
GAUGE_STALE_LAG_S: float = 900.0

GAUGE_STALE_ROTATION_GRACE_S: float = 600.0
"""GAU-22(c) -- how long to hold fire on a gauge-stale finding when this
row's own ``last_transition_at`` is NEWER than the gauge's ``measured_at``
(a rotation-window signature, not a freeze). DERIVATION, NOT MEASUREMENT:
the register's own live specimens show the window's true duration varies
0-9+ minutes (an 8.9-minute specimen resolved 6 minutes short of the 900s
GAUGE_STALE_LAG_S eligibility bound; a second rotation closed in under a
minute) -- 600s covers the measured range with margin without approaching
GAUGE_STALE_LAG_S itself, so this grace is never the reason a genuine
freeze goes unreported."""


# Distinct from EVENT_SESSION_OVERDUE_NOTICE so a receiver can tell the two
# classes apart: "went quiet" (overdue, may self-heal) vs "never came up"
# (spawning row with confirmed host death — see sweep_overdue_sessions).
EVENT_SESSION_SPAWN_ORPHANED_NOTICE = "session_spawn_orphaned_notice"

# Observed-alive spawning rows retain the established announced report rearm.
# Native liveness is never a promise that work has finished; there is no age cap.
EVENT_SESSION_SPAWN_UNREGISTERED_NOTICE = "session_spawn_unregistered_notice"

# W4A (#8 §43.1): fourth spawn-notice class, and the one that ATTRIBUTES.
# "This row has not registered and by construction therefore has not run its
# registration hook" — a statement about the seam, not about any policy blob.
EVENT_SESSION_REGISTRATION_OVERDUE_NOTICE = "session_registration_overdue_notice"

# The registration bound, deliberately NOT report_by. Registration is a
# machine-speed event — a worker that is going to register does it seconds
# after its process comes up — so a bound measured in a couple of minutes is
# already generous, and it is SHORTER than DEFAULT_REPORT_BY_SECONDS (300) on
# purpose: the attribution has to land BEFORE the report-deadline supervision
# starts telling a different story about the same row.
DEFAULT_REGISTRATION_BOUND_S: float = 120.0


def _parse_iso(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp cell to an aware (UTC) datetime.

    Mirrors ``ananta.llm.agent_messaging.service._parse_iso`` exactly (state
    ``DATETIME`` columns read back offset-naive; live clocks are aware UTC —
    coerce once at this boundary so every comparison here is aware-vs-aware).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _managed_sessions_in_state(
    state: StateManagementInterface, lifecycle_state: str,
) -> list[dict[str, Any]]:
    return list_managed_sessions(state, {"lifecycle_state": lifecycle_state})


def live_lifecycle_rows_by_instance(
    state: StateManagementInterface,
) -> dict[str, dict[str, Any]]:
    """Every ``live`` lifecycle row, indexed by ``agent_instance_id``.

    ONE QUERY PER SWEEP, not one per candidate. The L4c leg scans the gauge
    table -- which is never pruned and therefore carries the whole history of
    every session that ever reported -- so a per-row lifecycle lookup would put
    an unbounded query count on a leg whose entire eligibility problem is that
    its input is unbounded. Indexing once and looking up in memory keeps the
    leg's cost proportional to the LIVE fleet rather than to history.

    Rows with no ``agent_instance_id`` are dropped rather than keyed on the
    empty string: an empty key would collapse every such row onto one entry and
    hand the last one out as though it were a match, which is the confident-
    wrong-row failure the gauge store's own session-id join guards against.
    """
    rows: dict[str, dict[str, Any]] = {}
    for row in _managed_sessions_in_state(state, "live"):
        agent_instance_id = str(row.get("agent_instance_id") or "")
        if agent_instance_id:
            rows[agent_instance_id] = row
    return rows


def _notify_steward_of_spawn_orphaned(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    row: dict[str, Any],
) -> None:
    """Best-effort steward notice for a ``spawning`` row whose spawn process
    never registered and whose host was observed dead. The distinct notice
    records native-death evidence rather than inferring it from a deadline."""
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not spawner_instance_id:
        return
    binding = resolve_steward_binding(
        state=state, peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        logger.warning(
            "session %s spawn-orphaned: spawner %s not resolvable to a live "
            "binding (checked the peer registry directly and via its "
            "managed_session row) — marked terminated, steward not notified",
            agent_instance_id, spawner_instance_id,
        )
        return
    prose = (
        f"session_spawn_orphaned_notice: {agent_instance_id} (lane_id="
        f"{row.get('lane_id')!r}) never registered; native host observation "
        "confirmed its process is dead, so it was marked terminated."
    )
    meta: dict[str, object] = {"flow_id": f"session-spawn-orphaned-{agent_instance_id}"}
    try:
        bridge_manager.append_event(
            binding.bridge_id, EVENT_SESSION_SPAWN_ORPHANED_NOTICE, prose, meta,
        )
    except Exception:  # noqa: BLE001 — best-effort notify; the row is already terminated
        logger.warning(
            "session %s spawn-orphaned notice append failed", agent_instance_id, exc_info=True,
        )
    drive_on_delivery(
        state, recipient_agent_instance_id=spawner_instance_id,
        sender_label="session_spawn_orphaned_notice",
    )


def _spawning_host_liveness(row: dict[str, Any]) -> bool | None:
    """Return native alive/dead evidence, or None when the host is unobservable.

    Operator-hosted rows have no native probe. Missing references, unavailable
    drivers, and probe faults are unknown; none authorizes termination.
    """
    host = str(row.get("host") or "")
    host_ref = str(row.get("host_ref") or "")
    if not host_ref or host == OPERATOR_HOST:
        return None
    agent_instance_id = str(row.get("agent_instance_id") or "")
    try:
        driver, _ = _resolve_termination_driver(row, agent_instance_id)
        return driver.alive(host_ref)
    except VerbError:
        return None
    except Exception:  # noqa: BLE001 — a probe that raises is a non-observation, not a sweep failure
        logger.warning(
            "spawning row %s host-liveness probe raised; treating as unobserved",
            agent_instance_id, exc_info=True,
        )
        return None


def _notify_steward_of_spawn_unregistered(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    row: dict[str, Any],
) -> None:
    """Steward notice for an observed-alive, never-registered spawning row
    whose deadline was just re-armed instead of reaped — mirrors
    :func:`_notify_steward_of_spawn_orphaned`'s resolve-then-append pattern.
    The ``flow_id`` is stable per row so repeated extensions thread rather
    than scatter."""
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not spawner_instance_id:
        return
    binding = resolve_steward_binding(
        state=state, peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        logger.warning(
            "session %s spawn-unregistered: spawner %s not resolvable to a live "
            "binding — deadline re-armed, steward not notified",
            agent_instance_id, spawner_instance_id,
        )
        return
    prose = (
        f"session_spawn_unregistered_notice: {agent_instance_id} (lane_id="
        f"{row.get('lane_id')!r}) is past its report_by deadline and has never "
        "registered, but its host process is OBSERVED ALIVE — its deadline was "
        "re-armed. Elapsed time never authorizes termination. The coordinator "
        "owes a registration/progress decision based on current evidence."
    )
    meta: dict[str, object] = {"flow_id": f"session-spawn-unregistered-{agent_instance_id}"}
    try:
        bridge_manager.append_event(
            binding.bridge_id, EVENT_SESSION_SPAWN_UNREGISTERED_NOTICE, prose, meta,
        )
    except Exception:  # noqa: BLE001 — best-effort notify; the deadline is already re-armed
        logger.warning(
            "session %s spawn-unregistered notice append failed", agent_instance_id, exc_info=True,
        )
    drive_on_delivery(
        state, recipient_agent_instance_id=spawner_instance_id,
        sender_label="session_spawn_unregistered_notice",
    )


def _extend_observed_alive_spawning_row(
    state: StateManagementInterface,
    row: dict[str, Any],
    *,
    agent_instance_id: str,
    peer_registry: PeerRegistry | None,
    bridge_manager: BridgeSessionManager | None,
) -> None:
    """The observed-alive branch of :func:`_mark_one_spawning_orphaned` —
    re-arm the row's deadline from its own window, then best-effort notify the
    steward. Split out to keep the caller under the radon cc threshold."""
    _rearm_report_by(
        state, agent_instance_id,
        report_by_seconds=int(row.get("report_by_seconds") or 0),
        source=REPORT_BY_SOURCE_OBSERVED_SPAWNING,
    )
    if peer_registry is not None and bridge_manager is not None:
        _notify_steward_of_spawn_unregistered(
            state=state, peer_registry=peer_registry,
            bridge_manager=bridge_manager, row=row,
        )


def _mark_one_spawning_orphaned(
    state: StateManagementInterface,
    row: dict[str, Any],
    *,
    clock: datetime,
    peer_registry: PeerRegistry | None,
    bridge_manager: BridgeSessionManager | None,
    host_alive_probe: Callable[[dict[str, Any]], bool] | None = None,
) -> bool:
    """Reconcile an overdue spawning row using native host evidence only.

    Preserve the announced observed-alive report rearm. Unknown liveness leaves
    the row for registration/watchdog review; only definitive death permits the
    existing termination path. Elapsed time alone never terminates a session.
    """
    report_by = _parse_iso(row.get("report_by"))
    if report_by is None or report_by >= clock:
        return False
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if not agent_instance_id:
        return False
    probe = host_alive_probe if host_alive_probe is not None else _spawning_host_liveness
    alive = probe(row)
    if alive is None:
        return False
    if alive:
        _extend_observed_alive_spawning_row(
            state, row, agent_instance_id=agent_instance_id,
            peer_registry=peer_registry, bridge_manager=bridge_manager,
        )
        return False
    return _terminate_observed_dead_spawn(
        state, row, agent_instance_id=agent_instance_id,
        peer_registry=peer_registry, bridge_manager=bridge_manager,
    )


def _terminate_observed_dead_spawn(
    state: StateManagementInterface,
    row: dict[str, Any],
    *,
    agent_instance_id: str,
    peer_registry: PeerRegistry | None,
    bridge_manager: BridgeSessionManager | None,
) -> bool:
    """Apply and announce termination after a native probe confirmed death."""
    try:
        terminate_session(state, agent_instance_id=agent_instance_id, directed_by="sweep:platform")
    except VerbError:
        logger.warning(
            "spawning row %s with a definitively dead host could not be "
            "terminated by the sweep", agent_instance_id, exc_info=True,
        )
        return False
    if peer_registry is not None and bridge_manager is not None:
        _notify_steward_of_spawn_orphaned(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager, row=row,
        )
    return True


def sweep_overdue_sessions(
    state: StateManagementInterface,
    *,
    now: datetime | None = None,
    peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None,
    host_alive_probe: Callable[[dict[str, Any]], bool] | None = None,
) -> int:
    """Mark ``live``/``idle`` managed_session rows ``overdue`` past ``report_by``,
    then best-effort notify each row's steward (D2-lane-tail follow-up #3 —
    previously this function transitioned the row and returned a count with
    NO notification of any kind; a missing feature, not a delivery fault).
    Overdue spawning rows use native liveness: observed-alive rearms their
    report deadline; definitive death permits termination; unknown remains
    pending registration/watchdog review. No patience limit terminates work.
    ``host_alive_probe`` is a test-only override of the real native probe.

    Uncapped ``query_state`` per lifecycle_state (equality filter — no
    op-grammar dependency), never ``query_ordered``'s capped page: a fleet-wide
    sweep must never silently skip an owed row past a page boundary. Returns
    the count actually transitioned; a row that loses a race (e.g. a
    concurrent ``report_alive`` moved it first) is skipped, not retried —
    the NEXT tick re-evaluates it under its new state.

    ``peer_registry``/``bridge_manager`` are OPTIONAL (unlike the sibling
    ``sweep_deadline_dependencies``/``sweep_lane_closed_dependencies``,
    which require them): the STATE TRANSITION must always run regardless of
    whether notification is possible (e.g. an early-boot tick before the
    bridge service is up) — only the notify step is skipped, silently, when
    either is absent. The caller in ``plugin.py`` passes both whenever they
    are available.
    """
    clock = now or datetime.now(UTC)
    marked = 0
    for lifecycle_state in (LIFECYCLE_LIVE, LIFECYCLE_IDLE):
        for row in _managed_sessions_in_state(state, lifecycle_state):
            if _mark_one_overdue(
                state, row, from_state=lifecycle_state, clock=clock,
                peer_registry=peer_registry, bridge_manager=bridge_manager,
            ):
                marked += 1
    for row in _managed_sessions_in_state(state, LIFECYCLE_SPAWNING):
        if _mark_one_spawning_orphaned(
            state, row, clock=clock,
            peer_registry=peer_registry, bridge_manager=bridge_manager,
            host_alive_probe=host_alive_probe,
        ):
            marked += 1
    return marked


def sweep_unregistered_spawning_sessions(
    state: StateManagementInterface,
    *,
    now: datetime | None = None,
    peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None,
    registration_bound_s: float = DEFAULT_REGISTRATION_BOUND_S,
) -> int:
    """W4A registration watchdog — surface a worker that came up DEAF.

    An adopter running an org-managed Claude Code policy carrying
    ``strictPluginOnlyCustomization: ["hooks"]`` spawned a worker that answered
    its first turn while NONE of its injected hooks ran: no hook events, empty
    session-mapping spool, ``lifecycle_state`` stuck at ``spawning``. We
    accepted the reporter's framing — **the defect is the silence, not the
    unsupported environment** — so this leg exists to make the row SAY so.

    WHY THIS IS NOT THE EXISTING ``spawning`` LEG, and why the two must not be
    merged (the next reader will otherwise try):
    :func:`sweep_overdue_sessions`' spawning leg is bounded by ``report_by``
    (the report obligation) and probes native host liveness; only confirmed
    death permits termination. An alive host receives an announced rearm. This leg is bounded by REGISTRATION latency and its remedy is
    ATTRIBUTION: it writes a field, logs loudly, and tells the steward what was
    observed. It transitions nothing and kills nothing.
    Concretely, the adopter's worker takes the observed-alive path over there:
    it answered its first turn, so it probes alive, so it is re-armed with a
    notice that announces a DEADLINE EXTENSION — true, and not the news that a
    worker's hooks never ran. That leg working exactly as designed is what
    produced the reported silence, which is why the fix is a separate
    observation rather than another branch inside it.

    MECHANISM-INDEPENDENT BY CONSTRUCTION. The trigger is "still ``spawning``
    past the bound" — the seam itself. A row in that condition has, by
    construction, not run ``capture_session_mapping``, whatever the cause:
    the policy shape in front of us, a policy shape nobody has reported yet, a
    crashed hook, a mis-wired plugin, a read-only spool. Nothing here reads a
    settings file or reasons about one; the managed-policy PREFLIGHT
    (``HeadlessHostDriver.verify_config``) is a separate, narrower thing that
    refuses a spawn it can prove is doomed. A check written from the one
    incident in front of us would miss the next one.

    Idempotent: a row already carrying ``registration_overdue_at`` is skipped,
    so the field records the FIRST observation and the steward is told once.
    Returns the number of rows newly marked.
    """
    clock = now or datetime.now(UTC)
    marked = 0
    for row in _managed_sessions_in_state(state, LIFECYCLE_SPAWNING):
        if _mark_one_registration_overdue(
            state, row, clock=clock, registration_bound_s=registration_bound_s,
            peer_registry=peer_registry, bridge_manager=bridge_manager,
        ):
            marked += 1
    return marked


def _mark_one_registration_overdue(
    state: StateManagementInterface,
    row: dict[str, Any],
    *,
    clock: datetime,
    registration_bound_s: float,
    peer_registry: PeerRegistry | None,
    bridge_manager: BridgeSessionManager | None,
) -> bool:
    """One row's worth of :func:`sweep_unregistered_spawning_sessions` — split
    out to keep the outer loop under the radon cc threshold, same shape as
    :func:`_mark_one_overdue`. Returns whether the row was newly marked."""
    if row.get("registration_overdue_at"):
        return False
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if not agent_instance_id:
        return False
    # Age from last_transition_at: it is the row's spawn timestamp and stays
    # unchanged while the row remains 'spawning'.
    spawned_at = _parse_iso(row.get("last_transition_at"))
    if spawned_at is None or (clock - spawned_at).total_seconds() < registration_bound_s:
        return False
    acknowledged = bool(row.get("degraded_hooks_acknowledged"))
    reason = (
        f"still 'spawning' {int((clock - spawned_at).total_seconds())}s after spawn "
        f"(registration bound {int(registration_bound_s)}s) — the spawned process has "
        "not registered, so its registration hook has not run. Its hooks may have been "
        "stripped by host policy, failed to load, or never fired."
    )
    if acknowledged:
        reason += " This spawn ACKNOWLEDGED degraded hooks, so this was an accepted risk."
    mark_registration_overdue(
        state, agent_instance_id=agent_instance_id, reason=reason, observed_at=clock,
    )
    logger.warning(
        "registration watchdog: session %s (lane_id=%r, host=%r) is %s. This row "
        "would otherwise sit in 'spawning' with nothing said about why.",
        agent_instance_id, row.get("lane_id"), row.get("host"), reason,
    )
    if peer_registry is not None and bridge_manager is not None:
        _notify_steward_of_registration_overdue(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager,
            row=row, reason=reason,
        )
    return True


def _notify_steward_of_registration_overdue(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    row: dict[str, Any],
    reason: str,
) -> None:
    """Best-effort steward notice for a registration-overdue row — mirrors
    :func:`_notify_steward_of_spawn_orphaned`'s resolve-then-append pattern.
    Distinct event type from the other three spawn notices so a receiver can
    tell "came up deaf" from "never came up" and from "alive but late"."""
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not spawner_instance_id:
        return
    binding = resolve_steward_binding(
        state=state, peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        logger.warning(
            "session %s registration-overdue: spawner %s not resolvable to a live "
            "binding — row marked, steward not notified",
            agent_instance_id, spawner_instance_id,
        )
        return
    prose = (
        f"session_registration_overdue_notice: {agent_instance_id} (lane_id="
        f"{row.get('lane_id')!r}, host={row.get('host')!r}) is {reason} The session "
        "may still be RUNNING and answering turns — a worker whose hooks did not "
        "run can look healthy from the outside while being invisible to the "
        "platform. Treat it as unmanaged until it registers."
    )
    meta: dict[str, object] = {"flow_id": f"session-registration-overdue-{agent_instance_id}"}
    try:
        bridge_manager.append_event(
            binding.bridge_id, EVENT_SESSION_REGISTRATION_OVERDUE_NOTICE, prose, meta,
        )
    except (PeerAmbiguousError, PeerSessionAmbiguousError, PeerUnreachableError):
        logger.warning(
            "session %s registration-overdue: steward notice could not be delivered",
            agent_instance_id, exc_info=True,
        )


def _mark_one_overdue(
    state: StateManagementInterface,
    row: dict[str, Any],
    *,
    from_state: str,
    clock: datetime,
    peer_registry: PeerRegistry | None,
    bridge_manager: BridgeSessionManager | None,
) -> bool:
    """One row's worth of :func:`sweep_overdue_sessions` — split out to keep
    the outer loop under the radon cc threshold. Returns whether the row was
    actually transitioned (the sweep's own count)."""
    report_by = _parse_iso(row.get("report_by"))
    if report_by is None or report_by >= clock:
        return False
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if not agent_instance_id:
        return False
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=from_state,
            to_state=LIFECYCLE_OVERDUE,
            directed_by="sweep:platform",
            reason="report_by deadline passed",
        )
    except StaleLifecycleStateError:
        return False
    if peer_registry is not None and bridge_manager is not None:
        _notify_steward_of_overdue(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager, row=row,
            clock=clock, report_by=report_by,
        )
    return True


def _resolve_dependency_waiter(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    waiter_instance_id: str,
) -> BridgeBinding | None:
    """Resolve a dependency edge's waiter to a live binding.

    Primary path: a direct ``resolve_by_agent_instance_id`` reverse lookup —
    works for ANY registered waiter regardless of transport, including a
    watch-registered session with no ``managed_session`` row of its own.
    Falls back to the ``managed_session``-row detour (``agent_id`` lookup +
    ``PeerRegistry.resolve``) only on a direct-lookup miss, mirroring
    :func:`_resolve_steward_via_managed_session`'s identical fix for the
    overdue-notice path."""
    binding = peer_registry.resolve_by_agent_instance_id(waiter_instance_id)
    if binding is not None:
        return binding
    agent_id = managed_session_agent_id(state, waiter_instance_id)
    if not agent_id:
        return None
    try:
        return peer_registry.resolve(agent_id, waiter_instance_id)
    except (PeerUnreachableError, PeerAmbiguousError):
        return None


def _deliver_dependency_wake(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    edge: dict[str, Any],
) -> None:
    """Best-effort wake delivery for one already-fired edge.

    Mirrors ``direct_wake_reconcile._notify_sender``'s exact
    resolve-then-append pattern: the edge is already marked fired (state),
    so a delivery fault here must never raise back into the sweep loop and
    must never block firing/marking the OTHER armed edges in this tick.

    Delivery must reach ANY resolvable peer binding, not just a managed
    one (phase-2 scope ruling, the phase-1 unified finding): a
    watch-transport or otherwise unmanaged waiter is a legal arm target
    (``arm_session_dependency`` has no waiter-existence check) and must not
    be silently dropped just because it has no ``managed_session`` row.
    """
    waiter_instance_id = str(edge.get("waiter_instance_id") or "")
    if not waiter_instance_id:
        logger.warning(
            "session_dependency %s is lane-scoped (waiter_lane_id=%r): "
            "lane-scoped wake delivery has no defined mapping in this "
            "slice — the edge is fired but nobody was notified",
            edge.get("id"), edge.get("waiter_lane_id"),
        )
        return
    binding = _resolve_dependency_waiter(
        state=state, peer_registry=peer_registry, waiter_instance_id=waiter_instance_id,
    )
    if binding is None:
        logger.warning(
            "session_dependency %s: waiter %s not resolvable to a live "
            "binding (checked the peer registry directly and via its "
            "managed_session row) — condition fired, nobody woken",
            edge.get("id"), waiter_instance_id,
        )
        return
    prose = (
        f"session_dependency fired: condition_kind={edge.get('condition_kind')} "
        f"condition_ref={edge.get('condition_ref')!r} is now satisfied."
    )
    meta: dict[str, object] = {"flow_id": f"session-dependency-{edge.get('id')}"}
    try:
        bridge_manager.append_event(
            binding.bridge_id, EVENT_SESSION_DEPENDENCY_WAKE, prose, meta,
        )
    except Exception:  # noqa: BLE001 — best-effort notify; the edge is already fired
        logger.warning(
            "session_dependency %s wake append failed", edge.get("id"), exc_info=True,
        )
    # Drive-on-delivery (2026-08-04, slice 2): ALONGSIDE the append_event
    # above, never instead of it — a managed waiter's driver channel gets an
    # extra best-effort nudge; drive_on_delivery no-ops silently for an
    # unmanaged waiter (the SessionNotFoundError path), so this is safe to
    # call unconditionally for every resolved binding.
    drive_on_delivery(
        state, recipient_agent_instance_id=waiter_instance_id,
        sender_label="session_dependency wake",
    )


def _fire_armed_dependencies(
    state: StateManagementInterface,
    *,
    condition_kind: str,
    is_due: Callable[[dict[str, Any]], bool],
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    now: datetime,
) -> int:
    """Shared fire-then-deliver loop for one ``condition_kind`` — the common
    machinery behind :func:`sweep_deadline_dependencies` and
    :func:`sweep_lane_closed_dependencies`: only the due-ness predicate
    differs per kind.

    ``fired_at`` is set via a predicated ``update_state`` (guarded on
    ``fired_at IS NULL``) BEFORE delivery is attempted — Architect ratification
    #1's no-fall-through discipline: a lost predicate here means another
    tick/process already claimed this row, so the loop moves on rather than
    delivering a second wake for a row it no longer owns.
    """
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_DEPENDENCY,
            # {"op": "is_null"} -- NEVER a bare None. A bare None filter
            # value compiles to SQL `col = NULL`, which the postgres
            # provider's own placeholder binding renders as a literal NULL
            # comparison -- always UNKNOWN/false in SQL, so it matches ZERO
            # rows, silently, forever (measured live 2026-08-04: an armed,
            # past-due deadline edge never fired -- the query above found
            # nothing because "fired_at": None matched no row, not because
            # none were due).
            "filters": {"condition_kind": condition_kind, "fired_at": {"op": "is_null"}},
        },
    )
    fired = 0
    for edge in require_records(result):
        if not is_due(edge):
            continue
        update_result = state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_DEPENDENCY,
                "filters": {"id": edge["id"], "fired_at": {"op": "is_null"}},
            },
            {"fired_at": now.isoformat()},
        )
        if require_updated(update_result) == 0:
            continue
        fired += 1
        _deliver_dependency_wake(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager, edge=edge,
        )
    return fired


def sweep_deadline_dependencies(
    state: StateManagementInterface,
    *,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    now: datetime | None = None,
) -> int:
    """Fire + deliver armed ``deadline`` dependency edges past their timestamp."""
    clock = now or datetime.now(UTC)

    def _is_due(edge: dict[str, Any]) -> bool:
        deadline = _parse_iso(edge.get("condition_ref"))
        return deadline is not None and deadline < clock

    return _fire_armed_dependencies(
        state, condition_kind=CONDITION_DEADLINE, is_due=_is_due,
        peer_registry=peer_registry, bridge_manager=bridge_manager, now=clock,
    )


def _lane_is_closed(state: StateManagementInterface, lane_id: str) -> bool:
    """True iff at least one ``managed_session`` row carries ``lane_id`` AND
    every such row is terminal (``terminated``/``retired``). No vacuous truth
    on an empty set — a lane with zero spawned sessions is unstarted, not
    closed."""
    rows = list_managed_sessions(state, {"lane_id": lane_id})
    if not rows:
        return False
    return all(
        row.get("lifecycle_state") in (LIFECYCLE_TERMINATED, LIFECYCLE_RETIRED)
        for row in rows
    )


def sweep_lane_closed_dependencies(
    state: StateManagementInterface,
    *,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    now: datetime | None = None,
) -> int:
    """Fire + deliver armed ``lane_closed`` dependency edges (Dawn ruling
    2026-08-03, arm-124065ee) — ``condition_ref`` is a ``lane_id``; the
    condition is due once every ``managed_session`` row carrying that lane_id
    is terminal. See the module docstring for why this replaced the
    unbuildable ``lane_landed`` spec kind."""
    clock = now or datetime.now(UTC)

    def _is_due(edge: dict[str, Any]) -> bool:
        return _lane_is_closed(state, str(edge.get("condition_ref") or ""))

    return _fire_armed_dependencies(
        state, condition_kind=CONDITION_LANE_CLOSED, is_due=_is_due,
        peer_registry=peer_registry, bridge_manager=bridge_manager, now=clock,
    )


class SessionRoleClaimPruner:
    """Architect ratification #3 — prunes ``session_role_claim`` rows whose
    session maps to no live/managed session, WITHOUT the one-sided-guard flaw
    the ratification named ("session id maps to no managed or registered
    session" pruned on instantaneous absence — but a blue-green cutover
    bounces bridges into a fleet-wide re-registration burst, so a genuinely
    LIVE session can transiently map to nothing; the recorded 156-phantom-
    orphans shape).

    A row is pruned ONLY when:

    1. a ``managed_session`` row exists for its ``agent_session_id`` and is
       terminal (``terminated``/``retired``) — ledger-authoritative, no grace
       window needed; or
    2. NO ``managed_session`` row and NO live registry binding exist for it,
       AND that absence has persisted past :data:`DEFAULT_PRUNE_GRACE_WINDOW_S`
       — never on one missed lookup.

    Absence duration is tracked in-memory (first-observed-absent timestamp
    per ``agent_session_id``) rather than a new schema column: a process
    restart resets tracking, which conservatively RESTARTS the grace window
    instead of risking a premature prune from stale duration data recovered
    from disk. Stateful (unlike the two module-level sweep functions above)
    because the grace-window clock needs to survive across ticks within one
    process lifetime — mirrored the same config+clock-holding shape the
    now-retired ``DirectWakeReconciler`` (A4, 2026-08-04) used.
    """

    def __init__(
        self,
        *,
        grace_window_s: float = DEFAULT_PRUNE_GRACE_WINDOW_S,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._grace_window_s = grace_window_s
        self._clock = clock or (lambda: datetime.now(UTC))
        self._first_absent_at: dict[str, datetime] = {}

    def sweep(
        self, state: StateManagementInterface, *, peer_registry: PeerRegistry,
    ) -> int:
        now = self._clock()
        pruned = 0
        seen: set[str] = set()
        for row in iter_table_rows(
            state,
            namespace=AGENT_ROLE_BINDING_NAMESPACE,
            table=TABLE_SESSION_ROLE_CLAIM,
            filters={},
            ceiling=1_000_000,
            reason=(
                "one live role-claim row per session is required by the pruner; "
                "135 rows measured on 2026-09-14"
            ),
        ):
            agent_session_id = str(row.get("agent_session_id") or "")
            if not agent_session_id:
                continue
            seen.add(agent_session_id)
            if self._should_prune(state, peer_registry, agent_session_id, now):
                deleted = state.delete_records(
                    AGENT_ROLE_BINDING_NAMESPACE,
                    {
                        "table": TABLE_SESSION_ROLE_CLAIM,
                        "filters": {"agent_session_id": agent_session_id},
                        "soft_delete": False,
                    },
                )
                if require_deleted(deleted):
                    pruned += 1
                self._first_absent_at.pop(agent_session_id, None)
        # Forget grace-window tracking for rows no longer present at all
        # (already pruned, or the row vanished by some other path) so the
        # dict does not grow unboundedly across a long process lifetime.
        for stale_key in set(self._first_absent_at) - seen:
            self._first_absent_at.pop(stale_key, None)
        return pruned

    def _should_prune(
        self,
        state: StateManagementInterface,
        peer_registry: PeerRegistry,
        agent_session_id: str,
        now: datetime,
    ) -> bool:
        managed_rows = list_managed_sessions(state, {"agent_session_id": agent_session_id})
        if managed_rows:
            if managed_rows[0].get("lifecycle_state") in (
                LIFECYCLE_TERMINATED, LIFECYCLE_RETIRED,
            ):
                return True
            # A non-terminal managed_session row is ledger-authoritative
            # "alive" — never absent, regardless of registry state.
            self._first_absent_at.pop(agent_session_id, None)
            return False
        try:
            binding = peer_registry.resolve_by_agent_session_id(agent_session_id)
        except PeerSessionAmbiguousError:
            # Ambiguous is "more than one live binding", i.e. present — not absence.
            self._first_absent_at.pop(agent_session_id, None)
            return False
        if binding is not None:
            self._first_absent_at.pop(agent_session_id, None)
            return False
        first_seen = self._first_absent_at.setdefault(agent_session_id, now)
        return (now - first_seen).total_seconds() > self._grace_window_s


class NoticeLatch:
    """One-notice-per-EPISODE gate for a sweep notice with no state edge behind
    it.

    The overdue notice does not need this: it rides the ``live -> overdue``
    lifecycle transition, so the row itself remembers that the notice was sent
    and the edge cannot be taken twice. The L4 notices have no such edge — a
    gauge row stays above the rotation threshold until the session actually
    rotates, and a session with no gauge row stays dark. Composed onto a tick
    without a latch, both would re-deliver the identical notice every
    ``bridge_sweep_interval_seconds`` for as long as the condition holds, which
    trains the reader to ignore the channel. An ignored warning is worse than
    no warning, so the latch is part of making these notices live, not a
    refinement of it.

    Three properties, each deliberate:

    * **Latched on SUCCESSFUL delivery only** (:meth:`record_sent` is called by
      the caller after the append returns True). A failed append leaves the key
      un-latched so the next tick retries — an episode must never be silenced
      by its own delivery failure.
    * **Re-arms when the condition CLEARS** (:meth:`retain_active`). A second
      episode is a second notice; only repetition WITHIN an episode is
      suppressed.
    * **In-memory, so it is bounded by the process lifetime.** A solet restart
      re-arms every episode and each live episode gets one more notice. That is
      a stated bound rather than an oversight: the alternative is a durable
      suppression table, and one repeat per restart is a cheaper failure than a
      schema for it. It is also self-limiting in the direction that matters —
      restarts are rare, ticks are every 5 minutes.
    """

    def __init__(self) -> None:
        self._latched: set[str] = set()

    def suppressed(self, key: str) -> bool:
        """True when ``key``'s episode has already been notified."""
        return key in self._latched

    def record_sent(self, key: str) -> None:
        """Latch ``key`` — call only after delivery actually succeeded."""
        self._latched.add(key)

    def retain_active(self, active: set[str]) -> None:
        """Release every latched key whose condition no longer holds, so the
        next episode notifies again."""
        self._latched &= active


def _latch_or_transient(latch: NoticeLatch | None) -> NoticeLatch:
    """``latch``, or a throwaway one for a caller that passed none.

    A fresh latch is EXACTLY equivalent to no latch for a single sweep — each
    key is visited once per call, so nothing it records can suppress anything
    within that call — which is what lets every leg below read as if a latch is
    always present. The alternative (``if latch is not None`` at each of the
    three use sites) is the same behaviour spelled three times, and it is the
    spelling in which one forgotten branch is a silent repeat-every-tick.
    """
    return NoticeLatch() if latch is None else latch


def _rotation_due_sessions(
    state: StateManagementInterface,
) -> list[tuple[str, str, dict[str, Any]]]:
    """``(agent_instance_id, steward_instance_id, enriched gauge row)`` for
    every live/idle session that is currently rotation-due.

    Splitting the SCAN from the NOTIFY keeps the sweep's loop about delivery
    policy (latch, count) and leaves "what counts as due" where it already
    lives, in :func:`_rotation_due_row`.
    """
    found: list[tuple[str, str, dict[str, Any]]] = []
    for lifecycle_state in ("live", "idle"):
        for row in _managed_sessions_in_state(state, lifecycle_state):
            enriched = _rotation_due_row(state, row)
            if enriched is not None:
                found.append(
                    (str(row["agent_instance_id"]), str(row["spawned_by_instance_id"]), enriched),
                )
    return found


def _within_startup_grace(row: dict[str, Any], *, clock: datetime) -> bool:
    """Whether this LIVE row is still too YOUNG for a missing gauge row to mean
    anything (see :data:`GAUGE_COVERAGE_GRACE_S`).

    ``last_transition_at`` is the row's move INTO ``live``, which is the moment
    from which a first reporting tick becomes possible at all.

    Grace is an exception to a notification, not authority to terminate. It
    may only apply on positive evidence that the row is young: a row with no
    parseable transition timestamp is NOT granted grace and is still reported.
    Suppressing an alarm on a timestamp we could not read is how a detector goes
    quiet for a reason nobody chose.
    """
    became_live = _parse_iso(row.get("last_transition_at"))
    if became_live is None:
        return False
    return (clock - became_live).total_seconds() < GAUGE_COVERAGE_GRACE_S


def _first_tick_landed(row: dict[str, Any]) -> bool | None:
    """Whether ``report_alive`` has landed for this row SINCE it went live —
    ``None`` when the row carries no evidence either way.

    ★ WHY THIS EXISTS, and why it is the better half of the GAU-13 fix. The two
    writers involved are BOTH ``PostToolUse`` hooks firing on the same completed
    tool call: ``heartbeat_report_alive.py`` re-arms this row's ``report_by``,
    and ``rotation_due_watch.py`` writes the gauge row. So "report_alive has
    landed since this row went live" is DIRECT evidence that the session
    completes tool calls and that its ``solet`` path resolves — exactly what a
    wall-clock grace was only ever guessing at.

    THE DERIVATION, and it is an identity rather than an estimate:
    ``_rearm_report_by`` writes ``report_by = <the moment of the call> +
    report_by_seconds`` on EVERY report_alive, so ``report_by -
    report_by_seconds`` IS the last report_alive's timestamp. Verified live
    2026-08-18 against three running lanes, where the derived value matched each
    row's ``updated_at`` to sub-millisecond (22:20:31.681054 vs .681320, and two
    more). At spawn the same subtraction returns the row's creation moment,
    which precedes ``last_transition_at`` — so a never-reported row derives to
    BEFORE its transition and a reported one to comfortably after.

    TRI-STATE ON PURPOSE. ``None`` is not ``False``: a row with no report_by
    window (``report_by_seconds`` of 0, which a spawn may legitimately request)
    carries no evidence, and absence of the WINDOW is not evidence of absence of
    a TICK. Collapsing the two would let a missing column speak as if it were a
    measurement — the failure mode this whole entry is about.
    """
    last_alive = last_report_alive(row)
    became_live = _parse_iso(row.get("last_transition_at"))
    if last_alive is None or became_live is None:
        return None
    return (last_alive - became_live).total_seconds() > FIRST_TICK_EVIDENCE_MARGIN_S


def last_report_alive(row: dict[str, Any]) -> datetime | None:
    """The moment ``report_alive`` last landed for this lifecycle row, or
    ``None`` when the row carries no evidence either way.

    ★ THE ONE COPY OF THE IDENTITY, and the reason it is a module-level public
    name rather than three private re-derivations. ``_rearm_report_by`` writes
    ``report_by = <the moment of the call> + report_by_seconds`` on EVERY
    report_alive, so ``report_by - report_by_seconds`` IS the last call's
    timestamp -- an identity, not an estimate. Verified live 2026-08-18 against
    three running lanes, where the derived value matched each row's
    ``updated_at`` to sub-millisecond (22:20:31.681054 derived vs .681320
    stored, and two more).

    THREE CALLERS NOW WANT IT, which is exactly when a shared rule has to stop
    being copied: :func:`_first_tick_landed` (has it ticked AT ALL since going
    live), :func:`_gauge_stale_session` (has it ticked SINCE its gauge row was
    written), and the L4c self-notice leg's eligibility bound. Two copies of a
    liveness rule is how the legs that depend on it drift apart, and the drift
    is silent -- each copy keeps returning a plausible answer.

    TRI-STATE PRESERVED AT THE SOURCE. ``None`` is not "never reported": a row
    whose ``report_by_seconds`` is 0 -- which a spawn may legitimately request,
    and which ``_spawn_live`` DEFAULTS to -- carries no window, and absence of
    the WINDOW is not evidence of absence of a TICK. Returning a datetime there
    would let a missing column speak as though it were a measurement, which is
    the failure mode this whole entry is about. Callers must keep the
    distinction; none of them may treat ``None`` as "did not tick".
    """
    if row.get("report_by_source") != REPORT_BY_SOURCE_EXPLICIT_SELF_REPORT:
        return None
    report_by = _parse_iso(row.get("report_by"))
    window_s = row.get("report_by_seconds")
    if report_by is None or not isinstance(window_s, (int, float)):
        return None
    if window_s <= 0:
        return None
    return report_by - timedelta(seconds=float(window_s))


def _live_age_seconds(row: dict[str, Any], *, clock: datetime) -> float | None:
    """Seconds this row has been in ``live``, or ``None`` if unreadable."""
    became_live = _parse_iso(row.get("last_transition_at"))
    if became_live is None:
        return None
    return (clock - became_live).total_seconds()


def _gauge_dark_session(
    state: StateManagementInterface, row: dict[str, Any], *, clock: datetime,
) -> tuple[str, str] | None:
    """``(agent_instance_id, steward_instance_id)`` when this LIVE row has no
    gauge row at all, else ``None``.

    Same "every ``None`` is a deliberate skip" idiom as
    :func:`_rotation_due_row`: no identity, no steward to tell, a session that is
    reporting perfectly well — or, since the R4 lane, one that has not yet had
    time to report at all (:func:`_within_startup_grace`).
    """
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not agent_instance_id or not spawner_instance_id:
        return None
    if read_session_context_status(state, agent_instance_id) is not None:
        return None
    if _within_startup_grace(row, clock=clock):
        return None
    return agent_instance_id, spawner_instance_id


def _rotation_prose(agent_instance_id: str, row: dict[str, Any]) -> str:
    """Return the sole durability notice text for a due session."""
    del agent_instance_id
    current_tokens = int(row["current_tokens"])
    return f"context is {current_tokens:,} — make sure everything is durable."


def _notify_rotation_due(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    row: dict[str, Any],
    agent_instance_id: str,
    spawner_instance_id: str,
) -> bool:
    """Best-effort steward notice for one rotation-due session.

    Mirrors :func:`_notify_steward_of_overdue`'s resolve-then-append shape, and
    inherits its posture: a delivery fault must never raise back into the sweep
    loop or block the OTHER rows in this tick.

    ★ THIS LEG STILL DOES NOT REACH AN OPERATOR-PRESENT SEAT, and that is
    structural rather than a gap to fix HERE. It is reached from
    ``spawned_by_instance_id`` on a ``managed_session`` row; a seat has neither.
    ``drive_on_delivery`` also no-ops for exactly this case (no
    ``managed_session`` row, degenerate ``operator`` host driver). So this leg
    serves MANAGED WORKERS' stewards and that is the whole of its job.

    WHAT CHANGED 2026-08-17, so the next reader does not build around a
    limitation that has since been covered elsewhere: the sentence that used to
    end this docstring said the seat "is served by a separate surface... and
    until that lands this notice fires into a void for seats." That surface has
    landed — :func:`sweep_rotation_self_notice`, the third leg on the same
    rider, which scans the gauge table directly and appends to the measured
    session's own bridge. The void is closed, but it was closed BESIDE this
    function, not inside it. Nothing about this leg's own reach changed.
    """
    binding = resolve_steward_binding(
        state=state, peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        logger.warning(
            "session %s is rotation-due: steward %s not resolvable to a live binding "
            "-- notice not delivered",
            agent_instance_id, spawner_instance_id,
        )
        return False
    meta: dict[str, object] = {"flow_id": f"rotation-due-{agent_instance_id}"}
    # Compose before delivery so composition errors remain visible —
    # and this one was found by that fix's blast radius, not by a separate
    # investigation. The guard below is for DELIVERY faults; with the prose
    # inside it, a bug in _rotation_prose (which does arithmetic on stored gauge
    # values) would be caught by the same broad `except Exception`, logged as
    # "append failed", and the notice would silently vanish while the log named
    # the wrong cause. A notice family whose whole purpose is to be the thing
    # that speaks up must not be able to eat its own message bug.
    prose = _rotation_prose(agent_instance_id, row)
    try:
        bridge_manager.append_event(
            binding.bridge_id,
            EVENT_ROTATION_DUE_NOTICE,
            prose,
            meta,
        )
    except Exception:  # noqa: BLE001 — best-effort notify, never fails the sweep
        logger.warning(
            "session %s rotation-due notice append failed", agent_instance_id, exc_info=True,
        )
        return False
    drive_on_delivery(
        state, recipient_agent_instance_id=spawner_instance_id,
        sender_label=EVENT_ROTATION_DUE_NOTICE,
    )
    return True


def _rotation_notice_subject(row: dict[str, Any]) -> tuple[str, str] | None:
    """Return the session/steward pair only when both durable keys exist."""
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not agent_instance_id or not spawner_instance_id:
        return None
    return agent_instance_id, spawner_instance_id


def _rotation_notice_gauge(
    state: StateManagementInterface, agent_instance_id: str,
) -> tuple[dict[str, Any], int, int] | None:
    """Read one usable gauge and its denominator, without guessing either."""
    gauge = read_session_context_status(state, agent_instance_id)
    if gauge is None:
        return None
    current = int(gauge.get("current_tokens") or 0)
    ceiling = int(gauge.get("ceiling") or 0)
    if ceiling <= 0:
        return None
    return gauge, current, ceiling


def _rotation_due_row(
    state: StateManagementInterface, row: dict[str, Any],
) -> dict[str, Any] | None:
    """Return one due gauge, enriched only with centralized notice evidence."""
    subject = _rotation_notice_subject(row)
    if subject is None:
        return None
    gauge_values = _rotation_notice_gauge(state, subject[0])
    if gauge_values is None:
        return None
    gauge, current, ceiling = gauge_values
    # Derived ONCE, as the full decomposition, and used for the verdict, the
    # band, AND the two axis flags that ride into the notice (GAU-12), so
    # `_rotation_prose` cannot name a band the decision did not use, nor claim
    # an axis fired that did not. Before GAU-08 this leg DECIDED on the
    # fraction and PRINTED the band alone, which meant a 1M-ceiling session
    # anywhere in 300,000-500,000 was skipped while sitting in the saturated
    # `warm_immediate` band the notice would have quoted. GAU-08 fixed the
    # decision; GAU-12 carries the same decomposition into the prose so a
    # small-ceiling fraction-only firing no longer prints an unqualified
    # keep-working band under an event typed rotation_due_notice.
    verdict = rotation_thresholds.rotation_notice_verdict(
        model=str(gauge.get("model") or ""),
        effort=str(gauge.get("effort") or ""),
        current_tokens=current,
        runtime_window_tokens=ceiling,
    )
    if not verdict.due:
        return None
    enriched = dict(gauge)
    enriched.update({
        "fraction": current / ceiling,
        "rotation_notice_at_tokens": verdict.notice_at_tokens,
        "rotation_notice_window_source": verdict.window_source,
        "rotation_notice_unrecognized_model": verdict.unrecognized_model,
    })
    return enriched


def sweep_rotation_due_sessions(
    state: StateManagementInterface,
    *,
    peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None,
    latch: NoticeLatch | None = None,
    counts: StewardNoticeCounts | None = None,
) -> int:
    """Notify stewards of managed sessions whose gauge says rotation is due.

    THE PRE-COST SURFACE. A ``PostToolUse`` hook cannot warn before the cost —
    it does not tick during the idle gap in which the cache lapses, so its
    first tick after resuming is already downstream of the full-price rewrite.
    This sweep runs on its own schedule and needs no session to act first,
    which is what lets a warning precede the spend.

    Reads the snapshots ``report_context_status`` already writes rather than
    measuring anything itself, and carries the MEASURED number and band into
    the notice (see :func:`_rotation_prose` for the two qualifications that
    ride with it).

    Returns the count actually notified. Notification requires both a peer
    registry and a bridge manager; absent either, this returns 0 without
    raising, matching :func:`sweep_overdue_sessions`' posture that an
    early-boot tick must not fail.

    ``latch`` gates repetition (see :class:`NoticeLatch`). Passing NONE means
    "notify every call", which is right for a one-shot invocation or a test and
    WRONG for a repeating tick — the composed production caller
    (``plugin.py``'s rotation-surface rider) always supplies one, because a
    rotation-due condition persists across ticks and an unlatched notice would
    re-deliver every ``bridge_sweep_interval_seconds`` until the session
    rotates.

    ``counts``, when supplied, is filled in with this leg's DETECTED /
    DELIVERED / UNDELIVERED breakdown (:class:`StewardNoticeCounts`, GAU-25).
    The ``int`` return is unchanged and still means DELIVERED ALONE, so a caller
    that wants to know whether the DETECTOR fired must pass a sink -- the return
    value cannot answer that and never could.
    """
    if peer_registry is None or bridge_manager is None:
        return 0
    gate = _latch_or_transient(latch)
    notified = 0
    undelivered = 0
    due: set[str] = set()
    for agent_instance_id, spawner_instance_id, enriched in _rotation_due_sessions(state):
        due.add(agent_instance_id)
        if gate.suppressed(agent_instance_id):
            continue
        if _notify_rotation_due(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager,
            row=enriched, agent_instance_id=agent_instance_id,
            spawner_instance_id=spawner_instance_id,
        ):
            notified += 1
            gate.record_sent(agent_instance_id)
        else:
            undelivered += 1
    gate.retain_active(due)
    fill_counts(counts, detected=len(due), delivered=notified, undelivered=undelivered)
    return notified


def _gauge_coverage_prose(
    *, agent_instance_id: str, row: dict[str, Any], clock: datetime,
) -> str:
    """The notice text: WHAT WAS MEASURED, and nothing this leg cannot see.

    ★ GAU-13(b). The previous wording told the reader the dark session was
    "past the startup grace, so this is not a session that simply has not
    reported yet" — and on 2026-08-18 the row it said that about landed healthy
    five minutes later. It WAS a session that simply had not reported yet. The
    leg is entitled to report an absence it measured; it is not entitled to rule
    out the explanation that turned out to be the right one, and a notice whose
    central claim the next tick can falsify is one the reader learns to skim.
    That cost is not paid here — it is paid by the REAL gauge-blindness this leg
    exists to catch, which arrives on the same channel.

    So: the measurement leads, in numbers. The cause is DIAGNOSED only as far as
    :func:`_first_tick_landed` actually establishes it, and the three branches
    are genuinely different findings rather than three wordings of one guess:

    * ``True`` — report_alive HAS landed since this row went live, so this
      session's PostToolUse path fires and its solet path resolves, and there is
      STILL no gauge row. This is the 2026-08-16 signature, now evidenced rather
      than assumed, and it points at one specific hook.
    * ``False`` — neither reporter has produced anything since the transition.
      A worker that has not been handed work yet looks exactly like this and is
      not broken, so the notice says so instead of accusing it.
    * ``None`` — no report_by window on the row, so which reporter is failing is
      not established. Said plainly rather than defaulted to the confident
      branch.
    """
    age_s = _live_age_seconds(row, clock=clock)
    measured = (
        f"{age_s:,.0f}s after it went live"
        if age_s is not None
        else "for an unknown duration (its transition timestamp is unreadable)"
    )
    lead = (
        f"gauge_coverage_notice: {agent_instance_id} is LIVE and has NO "
        f"session_context_status row at all, {measured} "
        f"(the startup grace is {GAUGE_COVERAGE_GRACE_S:,.0f}s)."
    )
    ticked = _first_tick_landed(row)
    if ticked is True:
        detail = (
            " report_alive HAS landed for it since it went live, so its "
            "PostToolUse hooks do fire and its solet path does resolve — which "
            "narrows this to the gauge reporter specifically. Check that "
            "session's rotation_due_watch hook: its transcript_path, its marker "
            "directory, and its solet invocation."
        )
    elif ticked is False:
        detail = (
            " There is also no report_alive from it since it went live, so this "
            "session has produced NO reporter output at all. A spawned worker "
            "whose first WORK turn has not arrived yet looks exactly like this "
            "and is not faulty — check whether it has been given work before "
            "reading this as a fault."
        )
    else:
        detail = (
            " Whether report_alive has landed since then is not determinable "
            "from this row (it carries no report_by window), so which reporter "
            "is failing is not established here."
        )
    return lead + detail + (
        " Both reporters are non-fatal by design and warn only to stderr, so "
        "neither can surface its own failure; this notice reports the absence "
        "it measured, not a cause it inferred."
    )


def _notify_gauge_coverage(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    agent_instance_id: str,
    spawner_instance_id: str,
    row: dict[str, Any],
    clock: datetime,
) -> bool:
    """Tell the steward one live session is producing no gauge row."""
    became_live = _parse_iso(row.get("last_transition_at"))
    return deliver_and_record_gauge_notice(
        state,
        bridge_manager=bridge_manager,
        binding=resolve_steward_binding(
            state=state, peer_registry=peer_registry,
            spawner_instance_id=spawner_instance_id,
        ),
        agent_instance_id=agent_instance_id,
        notice_type=EVENT_GAUGE_COVERAGE_NOTICE,
        prose=lambda: _gauge_coverage_prose(
            agent_instance_id=agent_instance_id, row=row, clock=clock,
        ),
        flow_id=f"gauge-coverage-{agent_instance_id}",
        clock=clock,
        threshold_s=GAUGE_COVERAGE_GRACE_S,
        observed_s=(
            0.0 if became_live is None else (clock - became_live).total_seconds()
        ),
    )


def sweep_gauge_coverage(
    state: StateManagementInterface,
    *,
    now: datetime | None = None,
    peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None,
    latch: NoticeLatch | None = None,
    counts: StewardNoticeCounts | None = None,
) -> int:
    """Notify stewards of LIVE sessions that have no gauge row at all.

    The signature this catches was measured on 2026-08-16: the reporting hook's
    ``solet`` invocation could fail silently (a bare binary name, an
    ``OSError`` caught and warned to stderr nobody reads), while the throttle
    marker — written BEFORE the report — kept updating. From outside, a session
    reporting perfectly and a session whose every report was discarded looked
    identical. Four sessions were dark for an unknown period and nothing
    surfaced it; it was found by a person going looking.

    The hook cannot catch this: its non-fatal-by-design contract requires it to
    swallow its own failures, and a component cannot report the failure of its
    own reporting path. The session cannot: it has no idea the write failed.
    The sweep can, because it sees BOTH facts — a lifecycle row saying live and
    an absent gauge row — and neither side can see the other.

    Deliberately generic in what it keys on: any future silent failure between
    tick and write (a renamed process key, a permissions change, a verb
    rejecting an argument) produces the same signature. Keying on the mismatch
    rather than on any particular cause is what makes it outlive the bug that
    motivated it.

    ``latch`` gates repetition exactly as in :func:`sweep_rotation_due_sessions`
    — a dark session stays dark until someone fixes it, so an unlatched notice
    would repeat every tick for the whole outage. The latch releases the key
    when a gauge row appears, which makes a RELAPSE (reporting recovered, then
    broke again) a fresh notice rather than a silence.

    A row younger than :data:`GAUGE_COVERAGE_GRACE_S` is skipped: it is dark
    because it is NEW, not because anything is broken. The latch cannot cover
    that case — every spawn wave is a fresh episode with fresh keys — so the
    grace is a predicate here rather than a suppression there.

    ``counts``, when supplied, is filled in with this leg's DETECTED /
    DELIVERED / UNDELIVERED breakdown (:class:`StewardNoticeCounts`, GAU-25).
    The ``int`` return is unchanged and still means DELIVERED ALONE, so a caller
    that wants to know whether the DETECTOR fired must pass a sink -- the return
    value cannot answer that and never could.
    """
    if peer_registry is None or bridge_manager is None:
        return 0
    clock = now or datetime.now(UTC)
    gate = _latch_or_transient(latch)
    notified = 0
    dark: set[str] = set()
    undelivered = 0
    for row in _managed_sessions_in_state(state, "live"):
        pair = _gauge_dark_session(state, row, clock=clock)
        if pair is None:
            continue
        agent_instance_id, spawner_instance_id = pair
        dark.add(agent_instance_id)
        if gate.suppressed(agent_instance_id):
            continue
        if _notify_gauge_coverage(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager,
            agent_instance_id=agent_instance_id, spawner_instance_id=spawner_instance_id,
            row=row, clock=clock,
        ):
            notified += 1
            gate.record_sent(agent_instance_id)
        else:
            undelivered += 1
    gate.retain_active(dark)
    fill_counts(counts, detected=len(dark), delivered=notified, undelivered=undelivered)
    return notified


def _in_rotation_grace(
    row: dict[str, Any], *, measured_at: datetime, clock: datetime,
) -> bool:
    """GAU-22(c) -- split out of :func:`_gauge_stale_session` to keep it
    under the radon cc threshold. True iff this row's ``last_transition_at``
    is NEWER than the gauge's ``measured_at`` (the gauge predates the row's
    latest transition -- most often a ``/clear`` rotation onto the SAME
    instance id) and that transition happened within
    :data:`GAUGE_STALE_ROTATION_GRACE_S`. See :func:`_gauge_stale_session`'s
    own docstring for why this is a hold-fire, not a suppression."""
    last_transition_at = _parse_iso(row.get("last_transition_at"))
    if last_transition_at is None or last_transition_at <= measured_at:
        return False
    transition_age_s = (clock - last_transition_at).total_seconds()
    return transition_age_s <= GAUGE_STALE_ROTATION_GRACE_S


def _gauge_stale_session(
    state: StateManagementInterface, row: dict[str, Any], *, clock: datetime,
) -> tuple[str, str, datetime, datetime] | None:
    """``(agent_instance_id, steward_instance_id, last_alive, measured_at)`` when
    this LIVE row is TICKING while its gauge row has ARRESTED, else ``None``.

    ★ GAU-01(b). The discriminator, stated as the table it implements:

    ======================  ============  ==========================================
    lifecycle advancing?    gauge fresh?  verdict
    ======================  ============  ==========================================
    yes                     yes           healthy -- no finding
    **yes**                 **stale**     **ALIVE and gauge-dark -- THIS leg fires**
    no                      stale         quiet or dead -- the D1 sweep's ``overdue``
                                          job, deliberately NOT a gauge finding
    no                      fresh         not reachable (same hook family)
    ======================  ============  ==========================================

    WHY THE SECOND CLOCK IS LOAD-BEARING rather than belt-and-braces. Both
    writers are ``PostToolUse`` hooks, so an idle session writes NEITHER row --
    its gauge goes stale with nothing wrong. Firing on gauge age alone would
    therefore alarm on every session between turns, which is most of a small
    fleet most of the night. The lifecycle clock is what separates "stopped
    reporting" from "stopped being asked to do anything", and only the first is
    a defect.

    EVERY ``None`` IS A DELIBERATE SKIP, same idiom as its siblings, and the
    order matters:

    * no identity, or no steward to tell -- nothing routable;
    * NO GAUGE ROW AT ALL -- that is :func:`_gauge_dark_session`'s finding, not
      this one. Two legs must never both fire on one condition, or the steward
      gets two notices naming different causes for one session;
    * ``last_report_alive`` returning ``None`` -- NO EVIDENCE, which is not
      "not advancing". A row with no report_by window cannot support this
      leg's premise, and inferring arrest from a missing column is precisely
      the move :func:`last_report_alive`'s tri-state exists to block;
    * report_alive NOT advancing past the gauge -- the third table row. A
      session whose both clocks stopped is quiet or dead, and saying "your
      gauge reporter is broken" about a session that stopped calling tools
      would be a confident wrong diagnosis;
    * a lag inside :data:`GAUGE_STALE_LAG_S` -- normal throttle skew;
    * GAU-22(c) -- ``last_transition_at`` NEWER than the gauge's own
      ``measured_at``, inside :data:`GAUGE_STALE_ROTATION_GRACE_S`. This row
      transitioned (most often a ``/clear`` rotation onto the SAME instance
      id) more recently than its gauge last wrote, so the gauge predates the
      successor entirely -- it is not that a reporter STOPPED, it is that
      the successor's own first gauge write has not landed YET. Firing here
      would report a rotation as a freeze, which is the L4d false-positive
      GAU-22 measured live (an 8.9-minute specimen that resolved 6 minutes
      short of :data:`GAUGE_STALE_LAG_S` eligibility on its own). Past the
      grace window this check no longer applies and a genuinely stuck
      rotation is free to alarm through the ordinary lag check below.
    """
    inputs = _gauge_stale_inputs(state, row)
    if inputs is None:
        return None
    agent_instance_id, spawner_instance_id, measured_at = inputs
    last_alive = last_report_alive(row) or _heartbeat_liveness_after_gauge(row, measured_at)
    if last_alive is None:
        return None
    if _in_rotation_grace(row, measured_at=measured_at, clock=clock):
        return None
    lag_s = (last_alive - measured_at).total_seconds()
    if lag_s <= GAUGE_STALE_LAG_S:
        return None
    return agent_instance_id, spawner_instance_id, last_alive, measured_at


def _gauge_stale_inputs(
    state: StateManagementInterface,
    row: dict[str, Any],
) -> tuple[str, str, datetime] | None:
    """Return the validated row identity and gauge clock, or no evidence."""
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not agent_instance_id or not spawner_instance_id:
        return None
    gauge = read_session_context_status(state, agent_instance_id)
    measured_at = _parse_iso(gauge.get("measured_at")) if gauge is not None else None
    if measured_at is None:
        return None
    return agent_instance_id, spawner_instance_id, measured_at


def _heartbeat_liveness_after_gauge(
    row: dict[str, Any],
    measured_at: datetime,
) -> datetime | None:
    """The D-5.3 exception to the explicit-report identity.

    A successful passive heartbeat carrying one or more failed predecessor
    attempts is liveness evidence with a different diagnosis.  It must be
    newer than the frozen gauge row; otherwise it says nothing about whether
    the gauge has since arrested.
    """
    heartbeat_at = _parse_iso(row.get("last_heartbeat_at"))
    failures = int(row.get("heartbeat_failures_since_last") or 0)
    if failures <= 0 or heartbeat_at is None or heartbeat_at <= measured_at:
        return None
    return heartbeat_at


def _gauge_stale_prose(
    *,
    agent_instance_id: str,
    row: dict[str, Any],
    last_alive: datetime,
    measured_at: datetime,
    clock: datetime,
) -> str:
    """The notice text: BOTH timestamps, the gap between them, and no cause.

    ★ ITS OWN PROSE, NEVER THE MISSING-ROW LEG'S -- and this is a correctness
    requirement, not a style one. "No row at all" and "a row that stopped" have
    different causes and different fixes: the first points at a reporter that
    never ran for this session, the second at one that ran and then stopped
    while its sibling kept running. Reusing the L4b wording would re-introduce
    exactly the conflation GAU-13 was landed to remove, one leg over.

    WHAT IT MAY AND MAY NOT ASSERT, per the GAU-13 rule. It states the two
    timestamps it read and the arithmetic between them -- all measured. It says
    which reporter is implicated, because the DIVERGENCE genuinely establishes
    that (report_alive landed; the gauge did not; both fire on the same
    completed tool call). It does NOT name a cause: as of 2026-08-18 the
    leading candidate is ``_resolve_firing_context`` returning None silently on
    a payload carrying no transcript_path/session_id, with a per-surface
    vendored-copy variant behind it -- but the leg cannot see which, and a
    notice asserting one would be a guess wearing a measurement's clothes.

    THE DETECTOR OUTLIVES THE DIAGNOSIS, which is why the cause is left out
    rather than merely hedged. This keys on the divergence between two
    independently-written rows, so it fires whatever froze the gauge -- a hook
    resolution failure, a surface divergence, or something not yet imagined.
    Pinning the prose to today's leading candidate would date the leg to today's
    understanding.
    """
    lag_s = (last_alive - measured_at).total_seconds()
    minutes = lag_s / 60.0
    heartbeat_failures = int(row.get("heartbeat_failures_since_last") or 0)
    if heartbeat_failures:
        liveness_evidence = (
            f"Its last successful passive heartbeat landed at {last_alive.isoformat()}, "
            f"after carrying forward {heartbeat_failures} heartbeat failure(s) "
            f"since {row.get('heartbeat_failure_first_at')!r}; this is a "
            "heartbeat-FAILING diagnosis, not evidence that the gauge reporter alone froze."
        )
    else:
        liveness_evidence = (
            f"Its last report_alive derives to {last_alive.isoformat()}; the "
            "independently-written liveness row advanced while the gauge did not, "
            "narrowing this to that session's gauge reporter."
        )
    return (
        f"gauge_stale_notice: {agent_instance_id} (lane_id={row.get('lane_id')!r}) "
        f"is LIVE and STILL REPORTING, but its context-gauge row has STOPPED "
        f"ADVANCING. {liveness_evidence} "
        f"while its session_context_status row was last measured at "
        f"{measured_at.isoformat()} — a gap of {lag_s:,.0f}s ({minutes:,.1f} "
        f"minutes), against a tolerance of {GAUGE_STALE_LAG_S:,.0f}s. Now is "
        f"{clock.isoformat()}. Both numbers are measured; the second is the gauge "
        "row's own measured_at. "
        "This notice does NOT identify why it stopped; "
        "the divergence is what was measured, and the cause is not visible from "
        "here. NOTE the reader will want: the latest gauge cache row is upsert-only, "
        "but GAU-15 retains the accepted series (up to 64 rows): query "
        "plugin::agent_messaging_plugin::session_context_status_history before "
        "concluding when the gauge stopped."
    )


def _notify_gauge_stale(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    agent_instance_id: str,
    spawner_instance_id: str,
    row: dict[str, Any],
    last_alive: datetime,
    measured_at: datetime,
    clock: datetime,
) -> bool:
    """Tell the steward one live session's gauge has arrested."""
    return deliver_and_record_gauge_notice(
        state,
        bridge_manager=bridge_manager,
        binding=resolve_steward_binding(
            state=state, peer_registry=peer_registry,
            spawner_instance_id=spawner_instance_id,
        ),
        agent_instance_id=agent_instance_id,
        notice_type=EVENT_GAUGE_STALE_NOTICE,
        prose=lambda: _gauge_stale_prose(
            agent_instance_id=agent_instance_id, row=row,
            last_alive=last_alive, measured_at=measured_at, clock=clock,
        ),
        flow_id=f"gauge-stale-{agent_instance_id}",
        clock=clock,
        threshold_s=GAUGE_STALE_LAG_S,
        observed_s=(last_alive - measured_at).total_seconds(),
        last_report_alive_at=last_alive,
        gauge_measured_at=measured_at,
    )


def sweep_gauge_staleness(
    state: StateManagementInterface,
    *,
    now: datetime | None = None,
    peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None,
    latch: NoticeLatch | None = None,
    counts: StewardNoticeCounts | None = None,
) -> int:
    """Notify stewards of LIVE sessions whose gauge row has STOPPED advancing.

    ★ THE DEFECT THIS EXISTS FOR, measured on 2026-08-18. A lane
    (``lane-rotation-notice``) sat with a frozen gauge row for 85 minutes while
    it was alive and working. Nothing surfaced it: the L4b leg checks whether a
    gauge row EXISTS, and this one existed -- it just never changed again. A
    frozen row read as coverage, so the session most in need of a rotation
    signal was the one the surface had stopped measuring.

    WHY THIS IS A SEPARATE LEG rather than another branch inside
    :func:`sweep_gauge_coverage`. The two answer different questions about
    different evidence and produce different remedies, and folding them would
    force one count and one log line to stand for both -- the reader could no
    longer tell "four sessions never reported" from "four sessions stopped
    reporting", which are opposite operational situations. Separate legs also
    buy separate fault isolation, on the rider's own stated principle that one
    read-only notice's fault must not cost another its tick.

    ``latch`` gates repetition exactly as in its siblings: an arrested gauge
    stays arrested until someone fixes it or the session rotates, so unlatched
    this would re-deliver every 300s for the whole outage. The latch releases
    when the row starts advancing again, which makes a RELAPSE a fresh notice
    rather than a silence -- and a relapse is a real shape here, since a hook
    that fails on one payload may succeed on the next.

    NO STARTUP GRACE, and its absence is deliberate rather than overlooked. The
    grace L4b needs exists because a young row has had no TIME to be written;
    this leg's premise already requires a report_alive to have landed AFTER the
    gauge's measured_at, which cannot be true of a session that has not reported
    yet. The condition is self-gating on evidence, so a wall-clock floor would
    add nothing but a second number to keep true.

    ``counts``, when supplied, is filled in with this leg's DETECTED /
    DELIVERED / UNDELIVERED breakdown (:class:`StewardNoticeCounts`, GAU-25).
    The ``int`` return is unchanged and still means DELIVERED ALONE, so a caller
    that wants to know whether the DETECTOR fired must pass a sink -- the return
    value cannot answer that and never could.
    """
    if peer_registry is None or bridge_manager is None:
        return 0
    clock = now or datetime.now(UTC)
    gate = _latch_or_transient(latch)
    notified = 0
    undelivered = 0
    stale: set[str] = set()
    for row in _managed_sessions_in_state(state, "live"):
        found = _gauge_stale_session(state, row, clock=clock)
        if found is None:
            continue
        agent_instance_id, spawner_instance_id, last_alive, measured_at = found
        stale.add(agent_instance_id)
        if gate.suppressed(agent_instance_id):
            continue
        if _notify_gauge_stale(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager,
            agent_instance_id=agent_instance_id, spawner_instance_id=spawner_instance_id,
            row=row, last_alive=last_alive, measured_at=measured_at, clock=clock,
        ):
            notified += 1
            gate.record_sent(agent_instance_id)
        else:
            undelivered += 1
    gate.retain_active(stale)
    fill_counts(counts, detected=len(stale), delivered=notified, undelivered=undelivered)
    return notified


# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_PRUNE_GRACE_WINDOW_S",
    "DEFAULT_REGISTRATION_BOUND_S",
    "GAUGE_COVERAGE_GRACE_S",
    "EVENT_GAUGE_COVERAGE_NOTICE",
    "EVENT_MANAGED_DISPATCH_NOTICE",
    "EVENT_ROTATION_DUE_NOTICE",
    "EVENT_SESSION_DEPENDENCY_WAKE",
    "EVENT_SESSION_OVERDUE_NOTICE",
    "EVENT_SESSION_REGISTRATION_OVERDUE_NOTICE",
    "NoticeLatch",
    "SessionRoleClaimPruner",
    "StewardNoticeCounts",
    "sweep_deadline_dependencies",
    "sweep_gauge_coverage",
    "sweep_lane_closed_dependencies",
    "sweep_managed_dispatches",
    "sweep_overdue_sessions",
    "sweep_rotation_due_sessions",
    "sweep_unregistered_spawning_sessions",
]
