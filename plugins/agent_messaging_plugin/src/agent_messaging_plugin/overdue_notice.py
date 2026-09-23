"""The platform sweep's session-overdue steward notice (GAU-28).

One leg, in its own module for two reasons that are the same reason. It is
where the report-or-die contract's promise is actually kept — "the platform
sweep marks overdue rows overdue and notifies the steward through normal
messaging" — so it is worth reading on its own; and ``session_sweep`` had
0.005 MI of margin above the maintainability gate's C boundary once this
leg's fix landed inside it, which is not margin at all.

The notice's event type is defined HERE and re-exported by ``session_sweep``,
so every existing import path keeps working while the definition sits next to
the only code that emits it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .gauge_notice_emit import deliver_and_record_gauge_notice
from .session_lifecycle_verbs import drive_on_delivery
from .steward_resolution import resolve_steward_binding

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .bridge_sessions import BridgeSessionManager
    from .peer_registry import PeerRegistry

logger = logging.getLogger(__name__)

EVENT_SESSION_OVERDUE_NOTICE = "session_overdue_notice"
"""Bridge event type for the steward notice this module delivers."""

EVENT_SESSION_OVERDUE_QUIET_NOTICE = "session_overdue_quiet_notice"
"""Visible, low-priority line for a deadline lapse with fresh heartbeat evidence."""

# INTERIM, iss_8126960b / D-4.8 (2026-09-04): the report_by state remains
# authoritative and is NEVER re-armed by a heartbeat.  Until R3-U2 supplies
# its produced-work predicate, only this NOTICE consumer distinguishes a
# lapsed row whose server-stamped heartbeat is fresh from one whose heartbeat
# is stale.  The 180s threshold is the shipped hook's own
# ``heartbeat_report_alive.py::_THROTTLE_SECONDS`` cadence, not a new policy
# knob or a second deadline.
HEARTBEAT_FRESHNESS_SECONDS = 180.0


def _heartbeat_is_fresh(row: dict[str, Any], *, clock: datetime) -> bool:
    """Whether the server's latest heartbeat stamp is inside the hook cadence."""
    raw_heartbeat_at = row.get("last_heartbeat_at")
    if not isinstance(raw_heartbeat_at, str) or not raw_heartbeat_at:
        return False
    try:
        heartbeat_at = datetime.fromisoformat(raw_heartbeat_at)
    except ValueError:
        return False
    if heartbeat_at.tzinfo is None:
        heartbeat_at = heartbeat_at.replace(tzinfo=UTC)
    return (clock - heartbeat_at).total_seconds() <= HEARTBEAT_FRESHNESS_SECONDS


def _notify_steward_of_overdue(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    row: dict[str, Any],
    clock: datetime,
    report_by: datetime,
) -> None:
    """Best-effort steward notice for one just-marked-overdue row (D2-lane-
    tail follow-up #3 — the report-or-die contract's own promise: "the
    platform sweep marks overdue rows overdue and notifies the steward
    (spawner) through normal messaging"). Mirrors
    :func:`_deliver_dependency_wake`'s exact resolve-then-append pattern —
    the row is already transitioned (state), so a delivery fault here must
    never raise back into the sweep loop and must never block marking the
    OTHER overdue rows in this tick.

    Absent ``spawned_by_instance_id`` (an operator-launched row, or a row
    with no recorded spawner) is silently skipped, not warned — that is a
    session with no steward to notify by construction, not a fault.

    Resolves the steward straight from the peer registry by instance id
    (``PeerRegistry.resolve_by_agent_instance_id`` — a direct lookup, no
    ``agent_id`` needed up front): the ``managed_session``-row detour this
    used to require as its ONLY path fails for the dominant case, an
    operator-launched seat (e.g. the primary seat) that spawned the overdue
    session directly — that seat has no ``managed_session`` row of its own,
    so the notice silently never fired (measured live, 2026-08-04 13:13:01Z,
    session_sweep.py:175). The old managed_session-based resolution stays as
    a fallback for a registry-lookup miss, not the primary path.

    ★ GAU-28: THE ATTEMPT IS RECORDED WHETHER OR NOT ANYONE RECEIVES IT. This
    leg used to return on ``binding is None`` before writing anything at all,
    so an undelivered overdue alarm's only trace was a WARNING in a rotating
    log file — and a trace that ages out is not a record. Every audit of alarm
    loss that reads the notice store therefore under-counted this leg to
    exactly zero, no matter how many real alarms were lost. The record now runs
    through the same :func:`deliver_and_record_gauge_notice` composition the
    gauge legs use, with ``no_steward_binding`` as a RECORDED OUTCOME rather
    than an early return, so GAU-26's resolver fix cannot regress silently
    afterwards.

    ★ WHAT THE TWO NUMBERS MEAN HERE, because they do not mean what they mean
    for a gauge notice. ``threshold_s`` is 0.0 and ``observed_s`` is seconds
    PAST ``report_by``: this leg's bound is the deadline itself, so any
    lateness violates it. The alternative pairing — the ``report_by_seconds``
    contract window against elapsed-since-last-``report_alive`` — is more
    informative but is underivable for a row carrying no window, and a column
    that changes meaning per row is worse than one that is always defined.

    ``report_by`` is a PARAMETER rather than re-parsed from ``row``: the caller
    has already parsed it and already established that it is in the past —
    that comparison is what decided this row was overdue at all. Re-deriving it
    here would be a second copy of the same rule, free to drift, and would need
    a defensive branch for a case the caller has ruled out."""
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not spawner_instance_id:
        return
    binding = resolve_steward_binding(
        state=state,
        peer_registry=peer_registry,
        spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        logger.warning(
            "session %s overdue: spawner %s not resolvable to a live binding (checked the peer registry directly and via its managed_session row) — marked overdue, steward not notified; recorded as no_steward_binding",
            agent_instance_id,
            spawner_instance_id,
        )
    heartbeat_fresh = _heartbeat_is_fresh(row, clock=clock)
    notice_type = EVENT_SESSION_OVERDUE_QUIET_NOTICE if heartbeat_fresh else EVENT_SESSION_OVERDUE_NOTICE
    delivered = deliver_and_record_gauge_notice(
        state,
        bridge_manager=bridge_manager,
        binding=binding,
        agent_instance_id=agent_instance_id,
        notice_type=notice_type,
        prose=lambda: _overdue_notice_prose(
            agent_instance_id=agent_instance_id,
            row=row,
            heartbeat_fresh=heartbeat_fresh,
        ),
        flow_id=f"session-overdue-{agent_instance_id}",
        clock=clock,
        threshold_s=0.0,
        observed_s=(clock - report_by).total_seconds(),
    )
    if binding is None or heartbeat_fresh or not delivered:
        return
    # Drive-on-delivery (2026-08-04, slice 2): ALONGSIDE the append_event
    # above, never instead of it. The steward is usually an operator-
    # launched, UNMANAGED session (the seat) — drive_on_delivery's own
    # SessionNotFoundError no-op covers that path byte-unchanged; a managed
    # steward (a spawned session that itself spawned the overdue worker)
    # gets the extra nudge.
    drive_on_delivery(
        state,
        recipient_agent_instance_id=spawner_instance_id,
        sender_label="session_overdue_notice",
    )


def _overdue_notice_prose(
    *,
    agent_instance_id: str,
    row: dict[str, Any],
    heartbeat_fresh: bool,
) -> str:
    """Keep the interim's quiet and alarm routes explicit to the steward."""
    prefix = f"session_overdue_notice: {agent_instance_id} (lane_id={row.get('lane_id')!r}) "
    if heartbeat_fresh:
        return (
            prefix + "missed report_by and remains marked overdue, but its server-stamped "
            f"heartbeat is fresh within the {HEARTBEAT_FRESHNESS_SECONDS:.0f}s hook cadence. "
            "INTERIM under D-4.8 / iss_8126960b: this quiet low-priority line is "
            "visible but sends no direct wake; heartbeat never re-arms report_by. "
            "R3-U2's produced-work predicate replaces this routing distinction."
        )
    return prefix + "missed its report_by deadline and was marked overdue while its heartbeat is stale. This is the report-or-die contract firing — silence is detectable by construction. Check the session's status; direct a recovery report, park, or terminate it."


__all__: list[str] = [
    "EVENT_SESSION_OVERDUE_NOTICE",
    "EVENT_SESSION_OVERDUE_QUIET_NOTICE",
    "HEARTBEAT_FRESHNESS_SECONDS",
    "_notify_steward_of_overdue",
]
