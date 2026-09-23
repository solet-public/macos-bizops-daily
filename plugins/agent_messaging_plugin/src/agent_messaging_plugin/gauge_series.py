"""GAU-15 — read a session's gauge SERIES and classify what it means.

`session_context_status` answers "what is this session's occupancy now".
This module answers the question that one row structurally cannot: **has this
gauge stopped, or was nobody ever driving the session?** Those are different
facts with different fixes, and until 2026-08-19 they reached a steward as the
same single alarm — measured that night across four live lanes, two of which
were merely idle and two of which were healthy.

The classification is deliberately built from TWO independent clocks that are
written by TWO different hooks on the same completed tool call:

* the gauge series (`session_context_status_history`, this plugin's store), and
* the lifecycle row's last `report_alive`, via
  `session_sweep.last_report_alive` — the ONE copy of that identity, imported
  rather than re-derived, because two copies of a liveness rule drift silently.

Reading them together is what separates the states. A session whose gauge
stalled while `report_alive` kept landing is WORKING AND GAUGE-DARK: the GAU-01
class. A session where both stalled together is nobody's incident — it is idle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .context_status_verbs import VerbError, resolve_status_row
from .session_context_status_store import (
    GAUGE_HISTORY_RETENTION,
    AmbiguousAgentSessionIdError,
    read_session_context_status_history,
)
from .session_lifecycle_store import SessionNotFoundError, read_managed_session
from .session_sweep import last_report_alive

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .peer_registry import PeerRegistry

SERIES_HEALTHY = "healthy"
SERIES_STOPPED = "stopped"
SERIES_IDLE = "idle"
SERIES_ABSENT = "absent"
SERIES_NEVER_STARTED = "never_started"
SERIES_UNDETERMINED = "undetermined"

SERIES_STATES = (
    SERIES_HEALTHY,
    SERIES_STOPPED,
    SERIES_IDLE,
    SERIES_ABSENT,
    SERIES_NEVER_STARTED,
    SERIES_UNDETERMINED,
)

GAUGE_SERIES_STALL_S = 900.0
"""How long a series may go without a new row before it counts as STALLED.

Deliberately the same number as ``session_sweep.GAUGE_STALE_LAG_S``, and
deliberately not imported from it: these are two independent judgements that
happen to agree today (a detector's alarm threshold, and a reader's "is this
series still moving"). Binding them together would make a future tightening of
one silently retune the other. DERIVATION, NOT MEASUREMENT: 7.5x the reporting
hook's 120s throttle. A live control on 2026-08-18 measured healthy lags up to
+178.8s, which is the only reason this is not smaller -- the tidy argument for
a tighter bound has already been tried and measured false.
"""


def _parse(value: object) -> datetime | None:
    """An ISO timestamp as an AWARE datetime, or ``None`` if unreadable.

    Naive input is READ AS UTC rather than rejected: the DATETIME columns this
    module reads drop their offset on the way in, so every stored stamp comes
    back naive, and refusing them would make this classifier permanently
    undetermined against its own store.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _lifecycle_tick(
    state: StateManagementInterface, agent_instance_id: str,
) -> tuple[datetime | None, bool, str]:
    """``(last report_alive, lifecycle row was readable, agent_session_id)``.

    The bool matters on its own: "no lifecycle row" and "a lifecycle row that
    carries no report_by window" both yield ``None`` for the timestamp and are
    NOT the same finding, and a classifier that collapsed them would report a
    session it could not find as one that never ticked.

    The DURABLE ``agent_session_id`` rides along (empty string when the row
    is unreadable or the field is unset) because GAU-18's watcher-presence
    join must key on it, never on ``agent_instance_id`` -- GAU-26's own
    ruling: a watcher registers its presence under a DERIVED
    ``agi-watch-...`` id, and the ledger ``agent_instance_id`` this
    function's caller already has does not match it.
    """
    try:
        row = read_managed_session(state, agent_instance_id)
    except SessionNotFoundError:
        return (None, False, "")
    return (last_report_alive(row), True, str(row.get("agent_session_id") or ""))


def classify_gauge_series(
    *,
    newest_recorded_at: datetime | None,
    last_alive: datetime | None,
    lifecycle_readable: bool,
    now: datetime,
    watcher_present: bool | None = None,
) -> tuple[str, str]:
    """``(state, why)`` for one session's gauge series.

    THE TRUTH TABLE, and every branch names the evidence it used rather than
    the conclusion it prefers:

    * no series at all -> NEVER_STARTED. Nothing has ever been written for this
      session; whether that is a fault depends on whether it was ever driven,
      which the caller can see in ``last_alive``.
    * series moving inside the stall bound -> HEALTHY.
    * series stalled, ``report_alive`` still advancing -> STOPPED. The session
      completes tool calls and its gauge does not. This is GAU-01.
    * series stalled, ``report_alive`` stalled with it -> IDLE or ABSENT
      (GAU-18), decided by ``watcher_present`` -- a THIRD, independent clock
      (watcher-presence ``updated_at``, ~7s cadence) that answers
      PROCESS-ALIVE rather than TAKING-TURNS:
        - ``watcher_present`` not supplied (``None``, the caller opted out of
          the join) -> IDLE, with the ORIGINAL (pre-GAU-18) reason text.
        - ``True`` -> IDLE. The process is confirmed alive, but IDLE STILL
          COVERS TWO SITUATIONS -- undriven, or deep inside one long tool
          call -- and NOTHING SEPARATES THEM: no hook fires at tool START,
          so this classifier cannot tell which. Ruled binding (the
          coordinating seat, 2026-08-20): do not let the label imply a
          precision it does not have.
        - ``False`` -> ABSENT. No live watcher-presence row at all --
          the process itself appears gone, a materially different fact
          from "alive but undriven".
    * anything unreadable -> UNDETERMINED, said plainly. A classifier that
      guesses here is worse than one that abstains, because its guess is
      indistinguishable from a measurement.
    """
    if newest_recorded_at is None:
        return (
            SERIES_NEVER_STARTED,
            "no history row has ever been written for this session",
        )
    series_age_s = (now - newest_recorded_at).total_seconds()
    if series_age_s <= GAUGE_SERIES_STALL_S:
        return (
            SERIES_HEALTHY,
            f"the newest history row is {series_age_s:,.0f}s old, inside the "
            f"{GAUGE_SERIES_STALL_S:,.0f}s stall bound",
        )
    if not lifecycle_readable:
        return (
            SERIES_UNDETERMINED,
            f"the series has been stalled {series_age_s:,.0f}s and no "
            "managed_session row could be read, so whether this session is "
            "still working is not established here",
        )
    if last_alive is None:
        return (
            SERIES_UNDETERMINED,
            f"the series has been stalled {series_age_s:,.0f}s and the "
            "lifecycle row carries no report_by window, so its last "
            "report_alive is not derivable -- absence of the WINDOW is not "
            "evidence of absence of a TICK",
        )
    alive_age_s = (now - last_alive).total_seconds()
    if alive_age_s <= GAUGE_SERIES_STALL_S:
        return (
            SERIES_STOPPED,
            f"the series has been stalled {series_age_s:,.0f}s while "
            f"report_alive landed {alive_age_s:,.0f}s ago -- this session works "
            "and its gauge is dark",
        )
    if watcher_present is None:
        return (
            SERIES_IDLE,
            f"the series has been stalled {series_age_s:,.0f}s and report_alive "
            f"is {alive_age_s:,.0f}s old -- both clocks stopped together, so "
            "nobody is driving this session; it is not faulty",
        )
    if watcher_present:
        return (
            SERIES_IDLE,
            f"the series has been stalled {series_age_s:,.0f}s and report_alive "
            f"is {alive_age_s:,.0f}s old, but a live watcher-presence row "
            "confirms the PROCESS is alive -- this reads IDLE, not ABSENT. "
            "IDLE STILL COVERS TWO SITUATIONS: undriven (nobody is at the "
            "wheel) or mid-call (deep inside one long tool call). Nothing "
            "separates them -- no hook fires at tool START, so this "
            "classifier cannot tell which.",
        )
    return (
        SERIES_ABSENT,
        f"the series has been stalled {series_age_s:,.0f}s, report_alive is "
        f"{alive_age_s:,.0f}s old, AND no live watcher-presence row was "
        "found -- this reads ABSENT: the process itself appears gone, a "
        "materially different fact from alive-but-undriven.",
    )


GAUGE_STOPPED_CONFIRM_GAP_S = 60.0
"""GAU-19 -- the minimum real-time gap between two reads before a STOPPED
verdict may be confirmed (never escalate on ONE sample). DERIVATION, NOT
MEASUREMENT: the register's own ruled fix shape names ~60s. Every live
inter-hook race window measured so far resolved well inside it (03:30:52Z ->
fresh 45s later; 12:36:11Z -> fresh 12:36:31Z, 20s later; 1,281s stale against
the 900s bound, resolved 20s after the read), so a genuine race always
resolves before the confirm gap elapses, while a true GAU-01 freeze
(report_alive landing, gauge dark) stays STOPPED across it."""


def confirm_gauge_stopped(
    *,
    first_newest_recorded_at: datetime | None,
    first_last_alive: datetime | None,
    first_lifecycle_readable: bool,
    first_now: datetime,
    second_newest_recorded_at: datetime | None,
    second_last_alive: datetime | None,
    second_lifecycle_readable: bool,
    second_now: datetime,
    min_confirm_gap_s: float = GAUGE_STOPPED_CONFIRM_GAP_S,
) -> tuple[bool, str]:
    """GAU-19 -- never escalate a STOPPED verdict on ONE sample.

    :func:`classify_gauge_series` is a SINGLE-sample classifier by
    construction, and that single sample can land inside the inter-hook race
    window: ``rotation_due_watch.py`` (gauge write) and
    ``heartbeat_report_alive.py`` (report_alive write) are two independent
    PostToolUse subprocesses that complete on the same tool call in
    unguaranteed order, so a sample taken between their two completions reads
    a healthy session's textbook STOPPED signature -- report_alive just
    landed, the gauge still carrying the PREVIOUS write. Raising the stall
    bound cannot escape this: the register's own live specimen read 1,281s
    stale against a 900s bound and resolved 20s later, far past any
    plausible threshold bump.

    The only escape is a SECOND read, taken >= ``min_confirm_gap_s`` after the
    first, that is STILL classified STOPPED. A caller supplies both samples'
    RAW evidence -- never a memoized verdict -- because each half is
    independently reclassified here, so a caller cannot short-circuit past
    the confirmation by pre-computing one side. Escalation additionally
    requires ``report_alive`` to have ADVANCED between the two reads: a
    session whose report_alive did NOT move has not proven itself
    alive-and-gauge-dark -- it may simply have gone IDLE in the interval
    (GAU-18 territory, not GAU-19's), and conflating the two would
    reintroduce exactly the false alarm this function exists to prevent.
    """
    if second_now <= first_now:
        raise VerbError(
            "non_monotonic_reads",
            f"second read ({second_now.isoformat()}) is not after the first "
            f"({first_now.isoformat()}) -- confirmation requires two reads "
            "separated in real time, not two labels on the same moment.",
        )
    gap_s = (second_now - first_now).total_seconds()
    first_state, first_why = classify_gauge_series(
        newest_recorded_at=first_newest_recorded_at,
        last_alive=first_last_alive,
        lifecycle_readable=first_lifecycle_readable,
        now=first_now,
    )
    if first_state != SERIES_STOPPED:
        return (
            False,
            f"first read classified {first_state!r}, not STOPPED -- nothing "
            "to confirm",
        )
    if gap_s < min_confirm_gap_s:
        return (
            False,
            f"only {gap_s:.0f}s between reads, below the "
            f"{min_confirm_gap_s:.0f}s confirm gap -- inconclusive, re-check "
            "later rather than escalate on a too-fast re-read",
        )
    second_state, second_why = classify_gauge_series(
        newest_recorded_at=second_newest_recorded_at,
        last_alive=second_last_alive,
        lifecycle_readable=second_lifecycle_readable,
        now=second_now,
    )
    if second_state != SERIES_STOPPED:
        return (
            False,
            f"second read ({gap_s:.0f}s later) classified {second_state!r} "
            f"({second_why}) -- the first STOPPED reading was the "
            "inter-hook race window, not a freeze",
        )
    if (
        first_last_alive is None
        or second_last_alive is None
        or second_last_alive <= first_last_alive
    ):
        return (
            False,
            "report_alive did not advance between reads -- this session may "
            "have gone IDLE rather than stayed working-and-gauge-dark; a "
            "confirmed STOPPED requires report_alive to keep landing across "
            "both reads",
        )
    return (
        True,
        f"STOPPED confirmed across two reads {gap_s:.0f}s apart: the series "
        f"was stalled at both reads ({first_why}) ({second_why}), while "
        f"report_alive advanced from {first_last_alive.isoformat()} to "
        f"{second_last_alive.isoformat()} -- the session is working and its "
        "gauge is genuinely dark",
    )


WATCHER_PRESENCE_FRESH_S = 30.0
"""GAU-18 -- how recently a watcher's own presence row must have been
touched to count as PROCESS-ALIVE. DERIVATION, NOT MEASUREMENT: roughly 4x
the ~7s watcher-presence touch cadence (Session-Start Orientation KB),
enough margin for ordinary jitter without treating a watcher that stopped
touching half a minute ago as still present."""


def _watcher_presence(
    peer_registry: PeerRegistry | None,
    agent_session_id: str,
    *,
    now: datetime,
    fresh_s: float = WATCHER_PRESENCE_FRESH_S,
) -> bool | None:
    """Whether a LIVE watcher-presence binding exists for this session.

    Resolved via the DURABLE ``agent_session_id`` -- never a derived id.
    GAU-26's own ruling: a watcher registers its presence under a derived
    ``agi-watch-...`` id, which the ledger ``agent_instance_id`` this
    module's other lookups key on will not match, so
    ``resolve_by_agent_session_id`` is the only join that reaches a
    watcher-held session's actual presence row.

    Returns ``None`` when the caller opted out (no registry supplied) or the
    session carries no ``agent_session_id`` (registration never completed)
    -- both are "not checked", not "checked and absent", and
    :func:`classify_gauge_series` treats that distinction as load-bearing
    (``None`` preserves the pre-GAU-18 IDLE behaviour rather than reading as
    ABSENT). Returns ``False`` only when a registry WAS consulted and
    genuinely found nothing fresh.
    """
    if peer_registry is None or not agent_session_id:
        return None
    binding = peer_registry.resolve_by_agent_session_id(agent_session_id)
    if binding is None:
        return False
    updated = _parse(binding.updated_at)
    if updated is None:
        return False
    return (now - updated).total_seconds() <= fresh_s


def _entry(row: dict[str, Any]) -> dict[str, Any]:
    """One history row as the verb publishes it.

    A PROJECTION, not the raw row: the stored record also carries standardizer
    bookkeeping (``id``, ``namespace``, ``is_deleted``, ...) that no consumer
    should learn to depend on. Every published field is copied as stored --
    nothing is defaulted, and a NULL stays NULL, because on this table a NULL
    is a positive finding about the REPORTER rather than a missing value.
    """
    return {
        "recorded_at": str(row.get("recorded_at") or ""),
        "measured_at": str(row.get("measured_at") or ""),
        "reading_at": row.get("reading_at"),
        "current_tokens": row.get("current_tokens"),
        "ceiling": row.get("ceiling"),
        "model": str(row.get("model") or ""),
        "claude_session_id": str(row.get("claude_session_id") or ""),
        "agent_session_id": row.get("agent_session_id"),
        "cache_read_tokens": row.get("cache_read_tokens"),
        "cache_cold": row.get("cache_cold"),
        "cache_overage_signature": row.get("cache_overage_signature"),
        "reporter_surface": row.get("reporter_surface"),
        "reporter_generation": row.get("reporter_generation"),
    }


def _rotations(entries: list[dict[str, Any]]) -> int:
    """How many `/clear` boundaries this page spans.

    A ROTATION IS A SPLICE, not a gap: the instance id survives a `/clear` and
    the Claude session id does not, so consecutive rows carrying different
    `claude_session_id` values are two contexts' readings in one series.
    Counting them is what stops a reader treating the 487,777 -> 253,247 step
    of the original GAU-01 incident as a measurement rather than a new session.
    """
    seen = [e["claude_session_id"] for e in entries if e["claude_session_id"]]
    return sum(1 for a, b in zip(seen, seen[1:], strict=False) if a != b)


def _series_identity(
    state: StateManagementInterface, agent_instance_id: str,
) -> tuple[str, str]:
    """``(the id the series is keyed on, how it was reached)``.

    Delegates to ``resolve_status_row`` -- the ONE copy of the GAU-07 join --
    and falls back to the id as given when no cache row resolves, so a session
    that has a series and somehow no cache row is still readable rather than
    silently empty.
    """
    try:
        row, id_resolution = resolve_status_row(state, agent_instance_id)
    except AmbiguousAgentSessionIdError as exc:
        raise VerbError("ambiguous_agent_session_id", str(exc)) from exc
    keyed_id = str(row.get("agent_instance_id") or "") if row else ""
    return (keyed_id or agent_instance_id, id_resolution)


def _gap_reason(agent_instance_id: str) -> str:
    """The honest-gap sentence, which must say WHICH lookups were tried.

    "No series on file" alone would leave a reader unable to tell a genuine
    reporting gap from the GAU-07 lookup miss that made healthy watcher-held
    sessions read as gauge-less for a day.
    """
    return (
        f"no session_context_status history on file for {agent_instance_id!r}. "
        "This id was ALSO resolved through the peer binding (the watch-id "
        "join) and still matched no series."
    )


def session_context_status_history(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    limit: int = GAUGE_HISTORY_RETENTION,
    now: datetime | None = None,
    peer_registry: PeerRegistry | None = None,
) -> dict[str, Any]:
    """The bounded gauge series for one session, newest first, classified.

    ``resolved=False`` is the honest-gap shape, never a raised error, and it
    carries the SAME key set as a resolved read so a caller cannot ``KeyError``
    its way through a legitimate gap -- the same contract
    ``session_context_status`` already holds.

    ``truncated`` is published rather than implied. A reader asking "when did
    this stop" is asking about the OLDEST row it can see, and a silently capped
    page answers that with a boundary the reader picked by accident.

    ``peer_registry`` is OPTIONAL (GAU-18): omitted, this verb's IDLE
    classification is UNCHANGED from before GAU-18 -- no ABSENT split. A
    caller that supplies it gets the watcher-presence join and the
    IDLE/ABSENT split; this keeps every existing call site working with zero
    changes while making the split available to a new one.
    """
    if not agent_instance_id.strip():
        raise VerbError(
            "missing_argument",
            "session_context_status_history requires a non-empty agent_instance_id.",
        )
    clock = now or datetime.now(UTC)
    series_id, id_resolution = _series_identity(state, agent_instance_id)
    rows, truncated = read_session_context_status_history(state, series_id, limit=limit)
    entries = [_entry(r) for r in rows]
    newest = _parse(entries[0]["recorded_at"]) if entries else None
    last_alive, lifecycle_readable, session_id = _lifecycle_tick(state, series_id)
    watcher_present = _watcher_presence(peer_registry, session_id, now=clock)
    series_state, why = classify_gauge_series(
        newest_recorded_at=newest,
        last_alive=last_alive,
        lifecycle_readable=lifecycle_readable,
        now=clock,
        watcher_present=watcher_present,
    )
    return {
        "resolved": bool(entries),
        "resolution_error": _gap_reason(agent_instance_id) if not entries else None,
        "agent_instance_id": series_id,
        "queried_agent_instance_id": agent_instance_id,
        "id_resolution": id_resolution,
        "series_state": series_state,
        "series_state_reason": why,
        "last_report_alive": last_alive.isoformat() if last_alive else None,
        "entries": entries,
        "returned": len(entries),
        "truncated": truncated,
        "retention": GAUGE_HISTORY_RETENTION,
        "rotation_boundaries": _rotations(entries),
    }


__all__ = [
    "GAUGE_SERIES_STALL_S",
    "GAUGE_STOPPED_CONFIRM_GAP_S",
    "SERIES_ABSENT",
    "SERIES_HEALTHY",
    "SERIES_IDLE",
    "SERIES_NEVER_STARTED",
    "SERIES_STATES",
    "SERIES_STOPPED",
    "SERIES_UNDETERMINED",
    "WATCHER_PRESENCE_FRESH_S",
    "classify_gauge_series",
    "confirm_gauge_stopped",
    "session_context_status_history",
]
