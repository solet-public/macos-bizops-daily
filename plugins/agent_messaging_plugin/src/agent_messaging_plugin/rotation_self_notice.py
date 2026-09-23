"""L4c -- the rotation SELF-NOTICE leg: the half of the context-rotation
surface that reaches the MEASURED SESSION ITSELF rather than its steward.

Its two sibling legs (`sweep_rotation_due_sessions`, `sweep_gauge_coverage`)
live in `session_sweep.py` and enumerate the `managed_session` lifecycle
ledger, which structurally has no row for an operator-launched seat. This leg
scans `session_context_status` directly -- that table has no FK to the ledger,
so a `host=operator` row is representable -- and appends to the measured
session's own bridge.

WHY ITS OWN MODULE, given that all three legs share one rider. It was written
inside `session_sweep.py` and moved out on 2026-08-17 for a measured reason:
at ~474 lines it took that module's maintainability index from 18.59 to 8.88,
across the gate's B/C boundary at 10.00. The pre-move landing had passed at
10.86 -- a TRUE green with 0.86 of headroom, which is the least informative
kind of pass: nothing in a pass/fail gate distinguishes 10.86 from 18.59, so
"this file cannot absorb another fifteen lines" was invisible in a green
report. The next edit by ANY lane would have blown it.

Extracting rather than allowlisting fixes the metric on its own merits instead
of silencing it. The leg shares a rider with its siblings but shares no state,
no helpers and no data with them -- it was already a module, it just was not in
a file yet.

★ NOTICE, NEVER ACT. Nothing here clears anything. See
:data:`EVENT_ROTATION_SELF_NOTICE` and :func:`_notify_rotation_self` for the
standing ruling that no agent sits in the injection path for a context clear,
and for why this module deliberately does NOT import `drive_on_delivery`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from ananta.core.domain.timestamps import to_naive_utc
from ananta.llm.agent_messaging.models import PeerSendRequest, TextPart

from . import rotation_thresholds
from .constants import (
    SYSTEM_AGENT_ID,
    SYSTEM_ROTATION_NOTICE_ID,
    SYSTEM_ROTATION_NOTICE_LABEL,
)
from .rotation_notice_retention import prune_rotation_notices
from .session_context_status_store import list_session_context_statuses
from .session_sweep import GAUGE_COVERAGE_GRACE_S, live_lifecycle_rows_by_instance

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface
    from ananta.llm.agent_messaging.service import AgentMessagingService

    from .bridge_sessions import BridgeSessionManager
    from .models import BridgeBinding
    from .peer_registry import PeerRegistry

logger = logging.getLogger(__name__)


# L4c -- THE SELF-NOTICE LEG (2026-08-17). The half of the rotation surface
# that reaches the session ITSELF rather than its steward.
# ---------------------------------------------------------------------------

EVENT_ROTATION_SELF_NOTICE = "rotation_self_notice"
"""Notice delivered to a session's OWN bridge saying how big its context has
got and what the ratified bands say about it.

WHY A THIRD ROTATION EVENT RATHER THAN REUSING `rotation_due_notice`: that
event is addressed to a STEWARD and is written in the third person about
someone else's session ("<id> is at N tokens"). This one is addressed to the
session in the second person about itself. Same measurement, different
recipient and different grammar; sharing the name would make a reader who
filters on it unable to tell whether a given notice was about them or about a
worker they are responsible for.

NOTICE, NEVER ACT -- the same contract as `EVENT_TTL_OVERDUE_NOTICE`, and
here it is load-bearing rather than stylistic. This leg does NOT call
`drive_on_delivery`, unlike every other notify path in this module. That is
deliberate and must stay: driving a session's host driver INJECTS a turn, and
there is a standing ruling that no agent sits in the injection path for a
context clear. `append_event` alone lands the notice on the session's own
bridge, where it surfaces at the session's next natural boundary without
interrupting in-flight work -- which is exactly the non-interrupting surface
the operator asked for, and is already proven in production by the TTL leg.
"""

ROTATION_SELF_NOTICE_FLOOR_S: int = 1200
"""Minimum seconds before the SAME band is re-notified to the SAME session.

The operator's ask was "something scheduled to run once every 20 minutes".
This is that 20 minutes -- and it is a FLOOR ON REPETITION, not a period. No
scheduler is created: the sweeper already ticks every
`bridge_sweep_interval_seconds` (300s), so this leg runs on a tick that exists
and this constant governs only how often it may say the same thing twice.

Crossing into a NEW band notifies on the very next tick regardless of this
floor -- a band change is new information and delaying it by up to 20 minutes
would reintroduce, in miniature, the delivery lag this whole leg exists to
remove.
"""

SELF_NOTICE_STALENESS_S: float = 3600.0
"""How old a gauge row may be and still be treated as a LIVE session.

★ THIS IS A LIVENESS PROXY, NOT A FRESHNESS REQUIREMENT, and it is loose on
purpose. `session_context_status` is never pruned -- there is no reaper, no
`delete_state` against it, and nothing anywhere sets `is_deleted` (verified
across the repo 2026-08-17). So the table holds a row for EVERY
`agent_instance_id` that has ever reported, frozen at whatever value that
session ended on. Without this bound the leg does not enumerate live sessions,
it enumerates the entire history of sessions: a session that ended at 380,000
tokens sits permanently in `warm_immediate`, is permanently a candidate,
permanently resolves to no binding, and is therefore permanently counted -- on
every tick, forever, since `record_sent` never runs for it and the latch never
engages.

The delivery path was never at risk (a dead session has no binding, so nothing
is spammed). The damage was to the INSTRUMENT: `unreachable` would have been
dominated by dead sessions while its log prose named the watch-id join gap as
the cause, so a reader watching that number climb would size a follow-on
landing against a phantom. A true number attached to the wrong noun, in the
one field added specifically so a gap would surface as a number rather than a
silence.

WHY 3600s AND NOT `GAUGE_COVERAGE_GRACE_S` (300.0). That constant is a STARTUP
grace for a session that has not reported YET -- a different quantity that
merely happens to be a duration, and reusing it would couple two unrelated
policies through one symbol. 300s is also far too tight here: the reporter is a
`PostToolUse` hook, so a session inside a single long tool call is legitimately
silent for longer than that (this lane's own gate battery runs ~7 minutes) and
must not be dropped mid-run. An hour is generous against every observed
reporting cadence while still excluding sessions that ended, which is the only
distinction this constant has to make.
"""

GAUGE_SILENT_REGISTRATION_GRACE_S: float = GAUGE_COVERAGE_GRACE_S
"""How long a registered session may lack its first context-gauge row.

This is the same startup question and the same measured 600-second window as
the managed-row gauge-coverage leg. Reusing its declared policy avoids making
an operator inventory row silently obey a second, competing boot deadline.
"""

ROTATION_SELF_NOTICE_BANDS: frozenset[str] = frozenset(
    {
        "notice_due",
    }
)
"""The effective bands worth interrupting nobody for.

`warm_keep`, `cold_below_h` and `capacity_ok` are the "carry on" verdicts, and
a notice that fires on them would deliver a message on every tick of every
session's entire life. The latch would suppress the repeats, but the FIRST one
would still arrive -- teaching every session that this channel opens with
something it does not need to act on. A channel is trained on its first
message.
"""


@dataclass(frozen=True)
class SelfNoticeCounts:
    """What one self-notice sweep actually did, as three distinguishable numbers.

    A single "failed" count would conflate a ROUTING gap (neither the row's
    own key nor its stored session-id join resolves to a live binding) with a
    DELIVERY fault (the binding resolved and `append_event` raised). Those have
    different causes, different owners and different fixes, and one of this
    leg's own arguments is that a number which cannot discriminate is the thing
    this codebase keeps paying for. Splitting them costs one field.

    Since 2026-08-18 `unroutable` counts a REPORTER-VERSION gap (a row predating
    the `agent_session_id` join column, which carries NULL), not the structural
    watch-id gap it originally counted. It should decay to zero as reporters
    upgrade; one that does not is a new fault, not the old known one.

    ★ GAU-06 (G1), 2026-08-19: `notified` WAS THE WRONG WORD AND IT WAS HIDING A
    QUESTION. It named neither what this leg does nor what the session gets. A
    session is "notified" only if something surfaced it to a reader, and this leg
    cannot know that -- what it knows is that the durable row was accepted. So:

    * `appended` -- the DURABLE peer-message row was persisted for that session.
      That is a transport fact this leg can actually assert, and the one that
      matters, because the durable row is what survives a watcher drain.
    * `watcher_held` -- how many of those `appended` sessions are WATCHER-HELD,
      a SUBSET and never an additional population. It is reported because the two
      have materially different delivery stories: a bridge-held session's event
      surfaces at its next natural boundary, while a watcher-held worker sees it
      only when it next looks, and no turn starts either way. A single number
      averaged those two into a claim neither of them supports.

    So `appended - watcher_held` is the bridge-held count; no field is a total of
    the others, and the two failure counts remain disjoint from both.
    """

    appended: int = 0
    watcher_held: int = 0
    unroutable: int = 0
    undeliverable: int = 0
    gauge_silent: int = 0


class BandEdgeLatch:
    """One-notice-per-BAND-EDGE gate, with a floor on repeating the same band.

    :class:`NoticeLatch` is not enough here and the difference is not a
    refinement. That latch answers "has this session been told about this
    EPISODE", keyed on the session alone, and it releases only when the
    condition clears entirely. A session that crosses 150K, then 200K, then
    300K is ONE unbroken episode by that definition -- so a `NoticeLatch` would
    deliver the 150K notice and then stay silent through both escalations,
    which is the failure mode that matters most: the band that gets suppressed
    is always the more urgent one.

    Keying on ``(session, band)`` instead makes each crossing its own edge. The
    floor then handles the opposite problem: a session that sits in
    `warm_immediate` for three hours is still in one band, and without a
    time bound it would be re-notified only never, or (if released) every 300s.
    Neither is what the operator asked for.

    So the rule is exactly two lines:
      * band CHANGED  -> notify now, floor does not apply (new information).
      * band SAME     -> notify only if `floor_seconds` have passed.

    In-memory and process-lifetime bounded, the same stated trade
    :class:`NoticeLatch` makes and for the same reason: a solet restart re-arms
    every session and each gets at most one extra notice. Restarts are rare and
    ticks are every five minutes, so the error is self-limiting in the
    direction that matters.
    """

    def __init__(self, floor_seconds: int = ROTATION_SELF_NOTICE_FLOOR_S) -> None:
        self._floor_seconds = floor_seconds
        self._sent: dict[str, tuple[str, datetime]] = {}

    def suppressed(self, key: str, band: str, *, now: datetime) -> bool:
        """True after this session generation has received its one notice."""
        previous = self._sent.get(key)
        if previous is None:
            return False
        return True

    def record_sent(self, key: str, band: str, *, now: datetime) -> None:
        """Latch ``key`` at ``band`` -- call only after delivery succeeded.

        Same posture as :meth:`NoticeLatch.record_sent`: a failed delivery
        leaves the entry untouched so the next tick retries. An episode must
        never be silenced by its own delivery failure.
        """
        self._sent[key] = (band, now)

    def retain_active(self, active: set[str]) -> None:
        """Forget every session that is no longer in a notifiable band.

        A session that rotates back down to `warm_keep` drops out here, so if
        it later climbs back into the same band that is a NEW episode and
        notifies immediately rather than waiting out a floor it started before
        it rotated.
        """
        self._sent = {key: value for key, value in self._sent.items() if key in active}


def _measured_age_seconds(row: dict[str, Any], *, clock: datetime) -> float | None:
    """Seconds since this row's `measured_at`, or None when it cannot be established.

    ⚠️ `measured_at` IS WRITTEN AWARE AND READS BACK NAIVE. The reporting hook
    writes `datetime.now(UTC).isoformat()`, but the `DATETIME` column drops the
    offset on the round-trip, so what comes out of state is naive-UTC
    (measured, not assumed: a stored value read back as
    '2026-08-18T00:09:42.968903'). Subtracting that from an aware `clock`
    raises `TypeError` -- which, inside this rider's per-leg fault isolation,
    would have failed the leg SILENTLY on every tick.

    So the naive case is handled explicitly and its contract is stated here
    rather than inherited by coincidence, exactly as
    :class:`rotation_thresholds.TimestampAwarenessError` argues each parser
    should. An already-aware value is honoured as-is so this keeps working if
    the column ever preserves the offset.

    None (unparseable or absent) is a genuine third answer, not an error to
    swallow: `measured_at` is `not_null` and written by our own hook, so a value
    that will not parse is a reporting-path defect. It is surfaced by the
    caller as its own skip rather than being guessed in either direction --
    guessing FRESH resurrects the unbounded-scan bug for malformed rows, and
    guessing STALE silences a live session.
    """
    raw = str(row.get("measured_at") or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (clock - stamp).total_seconds()


def _gauge_verdict(row: dict[str, Any]) -> rotation_thresholds.RotationNoticeDecision | None:
    """The two-axis verdict for one gauge row, or ``None`` when the row cannot
    support one.

    Every ``None`` is a deliberate skip of a row this leg has nothing true to
    say about, never a swallowed failure: an unusable ceiling (the same guard
    :func:`_rotation_due_row` applies) or a missing token count. Notably it
    does NOT skip on a missing `cache_cold` -- that column is nullable and a
    NULL means NOT REPORTED, so it is passed through as the warm default and
    the notice says so rather than silently asserting a measurement.
    """
    ceiling = int(row.get("ceiling") or 0)
    current = int(row.get("current_tokens") or 0)
    if ceiling <= 0 or current <= 0:
        return None
    return rotation_thresholds.rotation_notice_verdict(
        model=str(row.get("model") or ""),
        effort=str(row.get("effort") or ""),
        current_tokens=current,
        runtime_window_tokens=ceiling,
    )


def _self_notice_prose(
    row: dict[str, Any],
    verdict: rotation_thresholds.RotationNoticeDecision,
) -> str:
    """Return the sole durability notice text for a due session."""
    del verdict
    current = int(row["current_tokens"])
    return f"context is {current:,} — make sure everything is durable."


def _resolve_self_binding(
    peer_registry: PeerRegistry,
    *,
    row: dict[str, Any],
    agent_instance_id: str,
) -> BridgeBinding | None:
    """The measured session's LIVE bridge binding, or None if nothing routes.

    TWO KEYS, TRIED IN ORDER, because a session's gauge row and its live
    binding are not always keyed on the same id:

    1. ``agent_instance_id`` -- this row's own key. Correct and sufficient for
       a bridge-held session (every operator-launched seat), where the ledger
       id and the binding key are the same string.
    2. ``agent_session_id`` -- the stored stable session id, reverse-resolved
       through the registry. This is the watcher-held-worker case: the gauge
       row keys on the LEDGER id (``agi-<hash>``, what ``$AGENT_INSTANCE_ID``
       carries) while the live binding keys on the WATCH id
       (``agi-watch-<hash>``). Different strings, same session, and before this
       column existed nothing related them -- so a worker could report its own
       context every two minutes and remain permanently unreachable from the
       row it had just written. Measured 2026-08-17 on the live table: 3 of 4
       lanes unroutable, and the one that resolved was the bridge-held seat.

    Order matters and this order is the cheap one: the instance lookup is a
    single indexed read that succeeds for the common case, so the join is only
    attempted for rows it can actually help.

    ★ THE JOIN IS RESOLVED THROUGH THE REGISTRY, NEVER DERIVED FROM THE ID.
    The stored value currently looks like ``"ases-" + agent_instance_id``, and
    reconstructing it from the ledger id would pass every test that exists,
    including a live one, for exactly as long as that convention holds. It is
    one launcher's formatting choice, not a join. A test asserting the string
    shape would verify the convention and never once exercise the routing, and
    the failure -- routing to nowhere, or to the wrong session -- would surface
    only once some other launcher minted a session id another way. So: no
    ``startswith``, no prefix slicing, no reconstruction. If the registry
    cannot resolve it, it does not resolve.

    A NULL join column is NOT REPORTED (a reporter predating the widening),
    which is why a miss here is counted rather than raised: it is a coverage
    gap that heals when the reporter deploys, not a fault.

    ``PeerSessionAmbiguousError`` is deliberately NOT caught. A session holds
    at most one live bridge, so two bindings for one session id is a corrupt
    registry, and this leg's whole job is delivering a message to the right
    session. Guessing between two candidates could deliver a private context
    measurement to the wrong one. The rider's per-leg ``try/except`` turns this
    into a logged leg fault that costs the other legs nothing -- loud, bounded,
    and recoverable -- which is the correct handling for "the data says
    something impossible".
    """
    binding = peer_registry.resolve_by_agent_instance_id(agent_instance_id)
    if binding is not None:
        return binding
    agent_session_id = row.get("agent_session_id")
    if not agent_session_id:
        return None
    return peer_registry.resolve_by_agent_session_id(str(agent_session_id))


def _prune_notice_thread(
    state: StateManagementInterface,
    *,
    thread_id: str,
) -> None:
    """Bound this recipient's notice thread, without letting it fail the notice.

    THE PRUNE RUNS AFTER THE WRITE IT IS BOUNDING, by the writer, per the
    ruling -- see :mod:`.rotation_notice_retention` for why a reaper was not
    chosen.

    A FAILED PRUNE IS LOGGED, NEVER RAISED, and the direction matters: the
    notice is already persisted and readable, so raising here would convert a
    storage-hygiene problem into a lost delivery. The cost of swallowing it is
    an unbounded thread, which is what the WARNING is for -- it names the thread
    so the growth is attributable rather than mysterious.
    """
    try:
        prune_rotation_notices(state, thread_id=thread_id)
    except Exception:  # noqa: BLE001 — hygiene must never fail a delivered notice
        logger.warning(
            "rotation self-notice thread %s could not be pruned; the notice "
            "itself was delivered and this thread is now unbounded until the "
            "next successful prune",
            thread_id,
            exc_info=True,
        )


def _notify_self_binding(
    *,
    state: StateManagementInterface,
    agent_messaging_service: AgentMessagingService,
    bridge_manager: BridgeSessionManager,
    binding: BridgeBinding,
    agent_instance_id: str,
    prose: str,
) -> tuple[Literal["appended", "unroutable", "undeliverable"], bool]:
    """Best-effort notice to the session itself, PERSIST-FIRST (GAU-06 G2).

    Returns ``(outcome, watcher_held)``. ``unroutable`` (no binding for this
    instance id) and ``undeliverable`` (binding resolved, the durable write
    raised) have different causes and different owners, and a single False
    collapsed them under log prose that named only the first.

    ★ WHY A DURABLE ROW AND NOT JUST THE BRIDGE EVENT (GAU-06). A bridge event
    is a QUEUE entry: a watcher's drain consumes it, and after that it is gone
    with no cursor-addressable trace. So the one population this leg exists for
    -- a worker running autonomously, whose watcher drains its channel without a
    model turn -- could have its context notice consumed and never see it, which
    is the same delivery failure this leg was built to fix, one layer down. The
    peer-message row is the half that survives that: it is cursor-addressable,
    it is readable from the session's own inbox at any later time, and it does
    not depend on anyone being at the surface when it arrives.

    ★ PERSIST FIRST, THEN SURFACE, and the order is the point. If the append
    went first, a crash between the two would leave a notice that was surfaced
    and then lost -- the worst of both, because the reader who saw it has no way
    to retrieve it and the reader who did not has no trace that it happened.
    Persisting first means every notice a session was ever surfaced is also a
    notice it can go back and read.

    ★ SERVICE-LEVEL ``peer_send``, NEVER ``dispatch_peer_send``, and this is not
    a style preference -- it was measured on 2026-08-19 and each item is
    independent of the others:

    1. ``dispatch_peer_send`` hardcodes ``important=True``. Every self-notice
       would be stamped wake-bound, which is exactly the training of the
       coordination inbox that GAU-06's noise half exists to prevent.
    2. It appends ``EVENT_PEER_MESSAGE``, destroying the event-name
       discriminator a read-side filter needs.
    3. It wraps the prose in a ``[peer:<sender> instance=...]`` envelope with a
       reply hint -- a session's own context measurement presented to it as mail
       from somebody else.
    4. It calls the registered NATIVE WAKE ADAPTER for ``claude_code``. That
       adapter is not a turn injection (it appends on the same bridge queue as
       ``queued_notification``), so the standing no-injection ruling survives it
       -- but it RAISES when the recipient has no ``parent_pid`` or no open
       bridge, and ``dispatch_peer_send`` also raises ``PeerUnreachableError``
       for a stale binding. This leg counts those per session and keeps
       sweeping; routed through the helper, ONE dead binding would fault the
       pass for every other session in it.

    A ``drive=False`` parameter was built on that helper for this caller and is
    being REMOVED in the same change, because it addresses only item 4's nudge
    and none of 1-3. The trap it was written to disarm is worth keeping in
    words, though, since it is invisible where anyone would test it:
    ``drive_on_delivery`` NO-OPS for a session with no managed row -- every
    operator-present seat -- and fires on every managed watcher-held worker,
    which is precisely the population this leg serves. A self-notice routed
    through the default path therefore looks perfectly well behaved when
    hand-tested on a seat.

    ★ STILL NO ``drive_on_delivery`` CALL ON THIS PATH, by construction now
    rather than by discipline: this module does not import it, the service-level
    ``peer_send`` cannot reach it, and the smoke asserts no drive call occurs.
    Driving a host driver injects a turn, and the standing ruling is that no
    agent sits in the injection path for a context clear.

    ★ THE SENDER IS A SENTINEL, NOT A SESSION. ``SYSTEM_ROTATION_NOTICE_ID``
    gives these notices their own peer thread per recipient (threads key on
    ``(sender_bridge_id, peer_instance)``), which is what lets a coordination
    drain leave them alone. It also keeps the service's same-instance self-send
    rejection satisfied honestly: the sender is the platform, not the measured
    session talking to itself.
    """
    watcher_held = binding.is_watcher
    try:
        persisted = agent_messaging_service.peer_send(
            PeerSendRequest(
                sender_bridge_id=SYSTEM_ROTATION_NOTICE_ID,
                sender_agent_id=SYSTEM_AGENT_ID,
                sender_agent_instance_id=SYSTEM_ROTATION_NOTICE_ID,
                sender_session_label=SYSTEM_ROTATION_NOTICE_LABEL,
                peer_agent_id=binding.agent_id,
                peer_agent_instance_id=binding.agent_instance_id,
                peer_session_label=binding.session_label,
                peer_agent_session_id=binding.agent_session_id,
                content=[TextPart(type="text", text=prose)],
                # NOT important: this is a notice, not a wake. The flag is what
                # peer_inbox's silent-only filters read, and stamping a machine
                # -generated measurement as wake-bound is the noise GAU-06 is
                # about.
                important=False,
            ),
        )
    except Exception:  # noqa: BLE001 — best-effort notify, never fails the sweep
        logger.warning(
            "session %s rotation self-notice durable write failed",
            agent_instance_id,
            exc_info=True,
        )
        return "undeliverable", watcher_held
    _prune_notice_thread(state, thread_id=str(persisted.thread_id))
    try:
        bridge_manager.append_event(
            binding.bridge_id,
            EVENT_ROTATION_SELF_NOTICE,
            prose,
            {"flow_id": f"rotation-self-{agent_instance_id}"},
        )
    except Exception:  # noqa: BLE001 — the durable half already succeeded
        # NOT `undeliverable`, and the distinction is deliberate: the row this
        # session can read exists. What failed is the surface that would have
        # shown it sooner, so the notice is late rather than lost -- a different
        # fault with a different owner, and reporting it as a delivery failure
        # would send someone looking for a message that is sitting in the inbox.
        logger.warning(
            "session %s rotation self-notice persisted but its bridge append "
            "failed; the notice is readable from that session's inbox",
            agent_instance_id,
            exc_info=True,
        )
    return "appended", watcher_held


def _notify_rotation_self(
    *,
    state: StateManagementInterface,
    agent_messaging_service: AgentMessagingService,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    row: dict[str, Any],
    agent_instance_id: str,
    verdict: rotation_thresholds.RotationNoticeDecision,
) -> tuple[Literal["appended", "unroutable", "undeliverable"], bool]:
    """Persist and surface one context-band notice when its binding resolves."""
    binding = _resolve_self_binding(
        peer_registry,
        row=row,
        agent_instance_id=agent_instance_id,
    )
    if binding is None:
        return "unroutable", False
    return _notify_self_binding(
        state=state,
        agent_messaging_service=agent_messaging_service,
        bridge_manager=bridge_manager,
        binding=binding,
        agent_instance_id=agent_instance_id,
        prose=_self_notice_prose(row, verdict),
    )


def _registration_age_seconds(binding: BridgeBinding, *, clock: datetime) -> float | None:
    """Seconds since the live binding registered, or ``None`` if unreadable."""
    raw = binding.created_at.strip()
    if not raw:
        return None
    try:
        registered_at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if registered_at.tzinfo is None:
        registered_at = registered_at.replace(tzinfo=UTC)
    return (clock - registered_at).total_seconds()


def _gauge_silent_prose(*, agent_instance_id: str, age_seconds: float) -> str:
    """State only the registered-and-gauge-silent fact the rider measured."""
    return (
        f"gauge_silent_since_registration: {agent_instance_id} has no "
        f"session_context_status row {age_seconds:,.0f}s after registration "
        f"(grace is {GAUGE_SILENT_REGISTRATION_GRACE_S:,.0f}s)."
    )


def _gauge_silent_bindings(
    peer_registry: PeerRegistry,
    *,
    reported_instance_ids: set[str],
    clock: datetime,
) -> list[tuple[BridgeBinding, float]]:
    """Live bindings past grace whose first gauge row has not landed."""
    candidates: list[tuple[BridgeBinding, float]] = []
    for bindings in peer_registry.list_agent_ids().values():
        for binding in bindings:
            if binding.agent_instance_id in reported_instance_ids:
                continue
            age_seconds = _registration_age_seconds(binding, clock=clock)
            if age_seconds is None or age_seconds < GAUGE_SILENT_REGISTRATION_GRACE_S:
                continue
            candidates.append((binding, age_seconds))
    return candidates


def _session_still_live(
    lifecycle_row: dict[str, Any] | None,
    *,
    clock: datetime,
) -> bool:
    """Whether this session is currently MEETING its reporting obligation.

    ★ GAU-01(c)'s instrument, and the reason it is the lifecycle row rather
    than a second wall-clock number. The bound this replaces was evaluated
    against ``measured_at`` -- the GAUGE clock -- so a session whose gauge had
    arrested dropped out of the notifiable population after an hour. It stopped
    being told to rotate at exactly the moment it had been running longest
    without an operator prompt, which is the scenario this leg exists for. The
    gauge going quiet was being read as the SESSION going quiet, and those are
    the two things GAU-01 is about not confusing.

    NO NEW THRESHOLD IS INVENTED HERE, deliberately. ``report_by`` is the
    platform's OWN declared deadline for this session -- re-armed on every
    report_alive, and the very field the D1 sweep uses to flip a row to
    ``overdue``. A session inside its window is one the platform itself
    currently considers live; asking the same question with a fresh constant
    would mean maintaining a second opinion that can disagree with the first,
    and the disagreement would be silent.

    ``report_by`` in the FUTURE is the whole test. It is not "recently
    reported": a session may legitimately go a long quiet stretch inside a
    generous window, and the platform grants that window on purpose.

    Absence is NOT liveness. No row (the session is not in ``live`` at all) and
    an unreadable ``report_by`` both return False, so this can only ever EXTEND
    eligibility on positive evidence -- never grant it on a timestamp that
    could not be read. The bound it relaxes is protecting an unbounded scan, and
    a guard that fails open on unreadable input is not a guard.
    """
    if lifecycle_row is None:
        return False
    report_by = _parse_report_by(lifecycle_row)
    if report_by is None:
        return False
    return report_by >= clock


def _parse_report_by(lifecycle_row: dict[str, Any]) -> datetime | None:
    """``report_by`` as an aware UTC datetime, or ``None`` if unreadable.

    Same naive-reads-back coercion as :func:`_measured_age_seconds` documents
    for ``measured_at``: state ``DATETIME`` columns drop the offset on the
    round-trip, and subtracting a naive value from an aware ``clock`` raises
    ``TypeError`` -- which inside this rider's per-leg fault isolation would
    fail the leg silently on every tick.
    """
    raw = str(lifecycle_row.get("report_by") or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def _self_notice_candidate(
    row: dict[str, Any],
    *,
    clock: datetime,
    lifecycle: dict[str, dict[str, Any]],
) -> tuple[str, rotation_thresholds.RotationNoticeDecision] | None:
    """``(agent_instance_id, verdict)`` for a row worth notifying, else None.

    Split out of :func:`sweep_rotation_self_notice`'s loop so that loop reads
    as "for each notifiable session, deliver unless latched" and the decision
    of what counts as NOTIFIABLE lives in one place -- the same separation
    :func:`_rotation_due_row` makes for the steward leg, and the same reason:
    a sweep loop that also adjudicates eligibility is a loop whose delivery
    policy cannot be read without reading its threshold policy too.

    Each ``None`` is a deliberate skip, never a swallowed failure: a row with
    no instance id (nothing to route to), a row too STALE to be a live session
    (see :data:`SELF_NOTICE_STALENESS_S` -- the gauge table is never pruned, so
    without this the scan walks the entire history of sessions), a row whose
    age cannot be established at all, a row that cannot support a verdict (see
    :func:`_gauge_verdict`), or a session whose effective band is one of the
    carry-on verdicts.

    The staleness gate lives HERE rather than in
    :func:`list_session_context_statuses` on purpose: that function's docstring
    promises it filters nothing beyond ``is_deleted`` so a future consumer
    asking a different question of the same rows does not have to defeat its
    opinion first -- and keeping every "reason a row is not a candidate" in one
    place is what lets a reader check the list is complete.
    """
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if not agent_instance_id:
        return None
    age_seconds = _measured_age_seconds(row, clock=clock)
    if age_seconds is None:
        return None
    if age_seconds > SELF_NOTICE_STALENESS_S and not _session_still_live(
        lifecycle.get(agent_instance_id),
        clock=clock,
    ):
        # ★ GAU-01(c). The bound still fires -- it is the only thing keeping
        # this scan off the unpruned gauge table's entire history -- but it is
        # no longer decided by the gauge clock ALONE. A stale measured_at now
        # excludes a row only when the LIFECYCLE row also fails to vouch for
        # the session, so an arrested gauge no longer silences a session that
        # is demonstrably still reporting. Order matters: the cheap timestamp
        # test short-circuits, so the lifecycle lookup runs only for the rows
        # the old bound would have dropped.
        return None
    verdict = _gauge_verdict(row)
    if verdict is None:
        return None
    if not verdict.due:
        return None
    return (agent_instance_id, verdict)


def _consider_one_row(
    row: dict[str, Any],
    *,
    clock: datetime,
    lifecycle: dict[str, dict[str, Any]],
    gate: BandEdgeLatch,
    state: StateManagementInterface,
    agent_messaging_service: AgentMessagingService,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
) -> tuple[str, tuple[Literal["appended", "unroutable", "undeliverable"], bool] | None] | None:
    """One gauge row's whole decision, in the three outcomes it really has.

    * ``None`` -- not a candidate at all (stale, below band, ended session).
    * ``(instance_id, None)`` -- a candidate the latch is currently suppressing.
      The id still comes back because :meth:`BandEdgeLatch.retain_active` must
      count it: a suppressed session is NOTIFIABLE, and dropping it from that
      set would expire its own latch entry and re-notify it next tick.
    * ``(instance_id, (outcome, watcher_held))`` -- a notice was attempted.

    ★ EXTRACTED FOR THE COMPLEXITY GATE (2026-08-19), and the split is chosen so
    the caller keeps only the ACCUMULATION. GAU-06's durable half took the
    sweep to cyclomatic C(11) against an A/B-only gate. Decomposed rather than
    allowlisted: the allowlist is a register of debt the operator deferred, not
    a place for work landing tonight to park itself.

    The latch is recorded HERE rather than by the caller because the record
    belongs with the decision that earned it -- the durable write, not the
    surface append. The notice a session can read exists at that point, so
    repeating it at the same band edge would be a duplicate whether or not the
    bridge accepted the event.
    """
    candidate = _self_notice_candidate(row, clock=clock, lifecycle=lifecycle)
    if candidate is None:
        return None
    agent_instance_id, verdict = candidate
    if gate.suppressed(agent_instance_id, "notice_due", now=clock):
        return agent_instance_id, None
    outcome, is_watcher_held = _notify_rotation_self(
        state=state,
        agent_messaging_service=agent_messaging_service,
        peer_registry=peer_registry,
        bridge_manager=bridge_manager,
        row=row,
        agent_instance_id=agent_instance_id,
        verdict=verdict,
    )
    if outcome == "appended":
        gate.record_sent(agent_instance_id, "notice_due", now=clock)
    return agent_instance_id, (outcome, is_watcher_held)


def _sweep_context_band_notices(
    context_rows: list[dict[str, Any]],
    *,
    clock: datetime,
    lifecycle: dict[str, dict[str, Any]],
    gate: BandEdgeLatch,
    state: StateManagementInterface,
    agent_messaging_service: AgentMessagingService,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
) -> tuple[dict[str, int], int, set[str]]:
    """Deliver the existing context-band notices and retain their latch keys."""
    tally = {"appended": 0, "unroutable": 0, "undeliverable": 0}
    watcher_held = 0
    notifiable: set[str] = set()
    for row in context_rows:
        considered = _consider_one_row(
            row,
            clock=clock,
            lifecycle=lifecycle,
            gate=gate,
            state=state,
            agent_messaging_service=agent_messaging_service,
            peer_registry=peer_registry,
            bridge_manager=bridge_manager,
        )
        if considered is None:
            continue
        agent_instance_id, delivered = considered
        notifiable.add(agent_instance_id)
        if delivered is None:
            continue
        outcome, is_watcher_held = delivered
        tally[outcome] += 1
        if outcome == "appended":
            watcher_held += int(is_watcher_held)
    return tally, watcher_held, notifiable


def _sweep_gauge_silent_notices(
    peer_registry: PeerRegistry,
    *,
    reported_instance_ids: set[str],
    clock: datetime,
    gate: BandEdgeLatch,
    state: StateManagementInterface,
    agent_messaging_service: AgentMessagingService,
    bridge_manager: BridgeSessionManager,
) -> tuple[dict[str, int], int, int, set[str]]:
    """Deliver the registered-but-gauge-silent rider and retain its latch keys."""
    tally = {"appended": 0, "unroutable": 0, "undeliverable": 0}
    watcher_held = 0
    detected = 0
    notifiable: set[str] = set()
    for binding, age_seconds in _gauge_silent_bindings(
        peer_registry,
        reported_instance_ids=reported_instance_ids,
        clock=clock,
    ):
        latch_key = f"gauge-silent:{binding.agent_instance_id}"
        notifiable.add(latch_key)
        if gate.suppressed(latch_key, "gauge_silent", now=clock):
            continue
        detected += 1
        outcome, is_watcher_held = _notify_self_binding(
            state=state,
            agent_messaging_service=agent_messaging_service,
            bridge_manager=bridge_manager,
            binding=binding,
            agent_instance_id=binding.agent_instance_id,
            prose=_gauge_silent_prose(
                agent_instance_id=binding.agent_instance_id,
                age_seconds=age_seconds,
            ),
        )
        tally[outcome] += 1
        if outcome == "appended":
            watcher_held += int(is_watcher_held)
            gate.record_sent(latch_key, "gauge_silent", now=clock)
    return tally, watcher_held, detected, notifiable


def _active_agent_instance_ids(
    lifecycle: dict[str, dict[str, Any]],
    peer_registry: PeerRegistry,
) -> set[str]:
    """The identities whose stale gauges remain L4c candidates."""
    active_ids = set(lifecycle)
    registry_snapshot = peer_registry.list_agent_ids()
    active_ids.update(
        binding.agent_instance_id
        for bindings in registry_snapshot.values()
        for binding in bindings
        if binding.agent_instance_id
    )
    return active_ids


def sweep_rotation_self_notice(
    state: StateManagementInterface,
    *,
    now: datetime | None = None,
    peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None,
    agent_messaging_service: AgentMessagingService | None = None,
    latch: BandEdgeLatch | None = None,
) -> SelfNoticeCounts:
    """Tell each session ITSELF how big its context has got.

    ★ THE GAP THIS CLOSES, stated plainly because two sessions took personal
    responsibility for it before it was understood as a wiring fact. The
    context gauge is MEASURED by a `PostToolUse` hook and SURFACED to the seat
    by a `UserPromptSubmit` hook. So detection is continuous while delivery is
    gated on the operator typing -- and a session running autonomously for
    hours, which is precisely the condition that runs context up, is never
    shown its own number. The only surface that would tell it is silent exactly
    when the problem occurs, because it fires on the event whose ABSENCE causes
    the problem. Two context overruns (300K->559K on 2026-08-16, and a rotation
    at 606,142 on 2026-08-17) were both recorded as discipline failures. They
    were delivery failures. This leg is the delivery path that does not depend
    on anyone typing.

    ★ KEYED ON THE BAND, NEVER ON `rotation_due`, and this is not a preference.
    `rotation_due` is `fraction >= ROTATION_THRESHOLD_FRACTION` (0.5), which on
    a 1M-ceiling model is 500,000, while `rotation_band` saturates at 300,000.
    Every 1M-ceiling session therefore has a permanent 300K-500K window in
    which the band says "rotate immediately" and the fraction gate says
    nothing -- and `sweep_rotation_due_sessions` gates on the fraction. A leg
    built on `rotation_due` would have stayed silent through the ENTIRE range
    in which the 2026-08-17 seat burned its 300K. Measured live while this leg
    was being written: the dispatching seat sat at 184,680 tokens in band
    `warm_task_boundary` with `rotation_due` False.

    ★ IT SCANS `session_context_status`, NOT `managed_session`. Its two sibling
    legs enumerate the lifecycle ledger, which structurally has no row for a
    `host=operator` seat -- so no amount of fixing their thresholds could ever
    have reached one. See :func:`list_session_context_statuses`.

    ★ THE WATCH-ID COVERAGE GAP IS CLOSED (2026-08-18). It was this leg's
    largest known blind spot and is worth keeping the shape of, because the
    number it produced was itself misleading in an instructive way. A gauge row
    is keyed on the session's LEDGER instance id (what `$AGENT_INSTANCE_ID`
    carries), while a watcher-held session's live peer binding is keyed on its
    WATCH instance id (`agi-watch-<hash>`). For a bridge-held session --
    including every operator-launched seat, the case this leg was built for --
    those are the same string and resolution always succeeded. For a
    watcher-held worker they differ, and with no stored join the leg could not
    route to a session that had written the very row it was reading. Measured
    live 2026-08-17: 3 of 4 lanes unroutable, the sole success being the
    bridge-held seat. :func:`_resolve_self_binding` now falls back to the
    stored `agent_session_id` through `resolve_by_agent_session_id`, so a
    watcher-held worker is reachable from its own gauge row.

    WHAT `unroutable` MEANS NOW, which is NOT what it meant before. It is no
    longer a structural gap that no amount of correct behaviour could close; it
    is a REPORTER-VERSION gap. A row written before the join column shipped
    carries NULL, and NULL is NOT REPORTED -- so the count decays to zero on
    its own as reporters upgrade, and a count that STAYS non-zero after a full
    deploy cycle means something new is wrong. That is a different signal with
    a different response, and the two must not be read as the same number.
    Every count is logged by the caller together: an unresolved count on its
    own reads identically to a healthy run, which is the discriminator failure
    this codebase keeps paying for.

    The count is trustworthy ONLY because :data:`SELF_NOTICE_STALENESS_S`
    excludes ended sessions first. Before that bound existed the same field
    read 27, of which 24 were dead sessions and 3 were the join gap -- a true
    number attached to the wrong noun, overstating the cause the prose named by
    ~9x, in the one field added so a gap would surface as a number rather than
    a silence.

    ★ THE JOIN IS RESOLVED, NEVER DERIVED. See :func:`_resolve_self_binding`:
    the stored value currently looks like `"ases-" + ledger id`, and that is
    one launcher's convention, not a join. Reconstructing it would pass every
    test including a live one, right up until a session id is minted some other
    way.

    ``latch`` gates repetition per BAND EDGE (see :class:`BandEdgeLatch`).
    Passing None means "notify every call", right for a one-shot or a test and
    WRONG for a repeating tick -- the composed production caller always
    supplies one.
    """
    if peer_registry is None or bridge_manager is None or agent_messaging_service is None:
        # UNWIRED, not broken: the composed production caller passes all three,
        # and a test or one-shot that passes none gets an empty tally rather
        # than an import-time dependency. ``agent_messaging_service`` joins the
        # guard rather than being defaulted (GAU-06 G2) because the durable
        # write is not optional decoration -- a sweep that quietly ran without
        # it would append surface events that a watcher drain can still eat,
        # which is the exact failure this leg was extended to close, reported
        # as a healthy tally.
        return SelfNoticeCounts()
    clock = now or datetime.now(UTC)
    gate = latch if latch is not None else BandEdgeLatch()
    lifecycle = live_lifecycle_rows_by_instance(state)
    context_rows = list_session_context_statuses(
        state,
        measured_since=to_naive_utc(
            clock - timedelta(seconds=SELF_NOTICE_STALENESS_S),
        ),
        active_agent_instance_ids=_active_agent_instance_ids(lifecycle, peer_registry),
    )
    context_tally, context_watcher_held, context_notifiable = _sweep_context_band_notices(
        context_rows,
        clock=clock,
        lifecycle=lifecycle,
        gate=gate,
        state=state,
        agent_messaging_service=agent_messaging_service,
        peer_registry=peer_registry,
        bridge_manager=bridge_manager,
    )
    reported_instance_ids = {
        str(row.get("agent_instance_id") or "")
        for row in context_rows
        if str(row.get("agent_instance_id") or "")
    }
    silent_tally, silent_watcher_held, gauge_silent, silent_notifiable = (
        _sweep_gauge_silent_notices(
            peer_registry,
            reported_instance_ids=reported_instance_ids,
            clock=clock,
            gate=gate,
            state=state,
            agent_messaging_service=agent_messaging_service,
            bridge_manager=bridge_manager,
        )
    )
    gate.retain_active(context_notifiable | silent_notifiable)
    return SelfNoticeCounts(
        appended=context_tally["appended"] + silent_tally["appended"],
        watcher_held=context_watcher_held + silent_watcher_held,
        unroutable=context_tally["unroutable"] + silent_tally["unroutable"],
        undeliverable=context_tally["undeliverable"] + silent_tally["undeliverable"],
        gauge_silent=gauge_silent,
    )


__all__ = [
    "EVENT_ROTATION_SELF_NOTICE",
    "ROTATION_SELF_NOTICE_BANDS",
    "ROTATION_SELF_NOTICE_FLOOR_S",
    "GAUGE_SILENT_REGISTRATION_GRACE_S",
    "SELF_NOTICE_STALENESS_S",
    "BandEdgeLatch",
    "SelfNoticeCounts",
    "sweep_rotation_self_notice",
]
