"""GAU-15 item 4 — the gauge tamper canary: reporting tick, and the verifier.

A synthetic identity reports through the REAL gauge path on a schedule. An
audited verb arrests it for a bounded window. The detector, blind to all of it,
either alarms or does not — and a verifier that knows the canary out-of-band
checks BOTH edges against the durable alarm record.

★ HOW THE ARREST WORKS, and why it is arranged this way. A real gauge freeze is
a reporter that stopped writing while its session kept working. So the canary
reproduces exactly that: on every tick it re-arms the lifecycle clock through
the real `report_alive` verb, and it writes the gauge row through the real
`upsert_session_context_status` — UNLESS an audited arrest window is in force,
in which case it withholds only the gauge write. Nothing is injected downstream
and nothing is special-cased in the store: the freeze is produced the same way
production produces one, which is constraint (a). The lifecycle half must keep
advancing or the staleness leg cannot fire at all — its whole discriminator is
"still reporting, gauge stopped" — so a canary that went quiet on both clocks
would test nothing.

★ THE DETECTOR NEVER LEARNS ANY OF THIS (constraint (b)). The registry and the
tamper log are separate tables; the sweep's gauge legs read `managed_session`
and `session_context_status` and touch neither. To the detector a canary is an
ordinary session with an arrested gauge, which is the only way it can be a test
of the detector rather than a test of a special case.

★ THE VERIFIER READS THE DURABLE RECORD, NOT THE BRIDGE QUEUE (constraint (f)).
The sweep's notices are in-memory and their only reader CONSUMES them, so a
verifier polling that queue would race the steward and could swallow the very
alarm the steward needed. It reads GAU-21's `gauge_notice_record` instead,
which is why that work had to land first.

★ AND IT ESTABLISHES CAPABILITY BEFORE ASSERTING (constraint (a')). Expecting a
staleness alarm from a release whose sweep carries no staleness detector is a
test that fails for the wrong reason, and reporting that as a canary failure
would be worse than not running it. The verifier ABSTAINS in that case rather
than returning a verdict it cannot support.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .gauge_canary_store import (
    CanaryError,
    is_active_canary,
    read_canary,
    read_tamper_windows,
    record_tamper,
)
from .gauge_canary_store import retire_canary as retire_canary_mark
from .gauge_notice_record_store import read_gauge_notice_records
from .schema import WORK_CLASS_READ_ONLY
from .session_context_status_store import upsert_session_context_status
from .session_hosts import SYNTHETIC_HOST
from .session_lifecycle_store import (
    ManagedSessionSpec,
    SessionNotFoundError,
    insert_managed_session,
    read_managed_session,
)
from .session_lifecycle_verbs import report_alive
from .session_lifecycle_verbs import retire_session as retire_managed_session
from .session_sweep import EVENT_GAUGE_COVERAGE_NOTICE, EVENT_GAUGE_STALE_NOTICE

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

EXPECTABLE_NOTICE_TYPES = (EVENT_GAUGE_STALE_NOTICE, EVENT_GAUGE_COVERAGE_NOTICE)

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_ABSTAINED = "abstained"
VERDICT_NO_EVIDENCE = "no_evidence"
"""Nothing was exercised, so nothing was learned — never ``pass``.

★ THE DEFECT THIS EXISTS FOR, measured 2026-08-19 on this very file. With zero
windows and zero alarms the verifier returned PASS, reasoning "both edges hold:
0 closed arrest window(s) each produced their expected alarm" — a vacuous green
inside the instrument built to detect vacuous greens. An unexercised canary and
a detector that can no longer fire are IDENTICAL from the quiet edge; silence is
what they have in common, so silence alone can never justify a pass. Positive
evidence — at least one closed window that produced the alarm it asked for — is
what separates them, and it is now required.
"""

TICK_ARRESTED = "arrested"
TICK_REPORTED = "reported"

SYNTHETIC_HOST_HELP = """The host a canary's lifecycle row declares.

A canary has no process. ``spawn_session``, ``drive_session``, and
``clear_session`` still refuse this row LOUDLY — their driver is registered
but DEGENERATE (:class:`SyntheticHostDriver`, ``session_hosts.py``, GAU-24),
so they raise the same way :data:`OPERATOR_HOST`'s driver does for a row it
never spawned. GAU-24 registered that driver so ``terminate_session`` /
``retire_session`` could reach this row too: before that fix, the row's
host resolved to NOTHING (``HostMechanismMissingError``), and a canary
exercise permanently leaked one live ``managed_session`` row nothing could
retire. Registering a no-op driver is distinct from resolution failure — a
host name that genuinely has no driver in this build still raises
``HostMechanismMissingError`` via ``resolve_host_driver``; only this one
literal, explicitly-declared name gets the no-op path.

The alternative — borrowing an existing host name — was rejected on measurement,
not taste. ``operator`` would have been the closest fit (its driver is the
declared "no automation" one) but it asserts a human launched this, which is
false and would mislead the next person triaging the fleet; ``tmux`` or
``headless`` would point a kill or a keystroke at a pane that does not exist,
which is the failure worth the most care to avoid.

WHY THIS IS SAFE FOR CONSTRAINT (b) even though the detector reads the same
table: the gauge legs read ``agent_instance_id``, ``spawned_by_instance_id``,
``report_by``/``report_by_seconds`` and ``lane_id``, and no leg branches on
``host``. The value is provenance for humans and for the operational surfaces
that list sessions — a plane the detector never consults — which is exactly
where constraint (c) wants a synthetic mark to live.
"""

#: Substrings refused in the two identity fields the staleness ALARM echoes.
#: Constraint (b) can hold perfectly in code and leak in prose: the notice
#: quotes the subject's ``agent_instance_id`` and its ``lane_id`` verbatim, so a
#: canary named for what it is announces itself in every alarm it provokes. This
#: was found by a blindness test failing on its own fixture, and it is checked
#: here rather than left to the naming convention that already failed once.
SELF_ANNOUNCING_TOKENS = ("canary", "tamper", "synthetic", "fixture")


def _parse(value: object) -> datetime | None:
    """A timestamp, or ``None`` when it cannot be read.

    Unreadable is ``None`` rather than an exception or a substituted epoch: an
    arrest window whose bounds cannot be parsed must not silently become a
    window covering all time, which is what an epoch default would do.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def arrest_in_force(
    windows: list[dict[str, Any]], *, at: datetime,
) -> dict[str, Any] | None:
    """The arrest window covering ``at``, or ``None``.

    Half-open ``[from, until)``: an instant exactly at ``arrest_until`` is
    already released. Closing both ends would make two back-to-back windows
    overlap at their shared boundary, and an alarm there would be attributable
    to either — which is the one thing the audit log exists to prevent.

    A window whose bounds do not parse is SKIPPED rather than treated as
    covering everything: an unreadable window must not silently excuse an
    alarm, because "excused" is the answer that hides a real fault.
    """
    for window in windows:
        start = _parse(window.get("arrest_from"))
        end = _parse(window.get("arrest_until"))
        if start is None or end is None:
            continue
        if start <= at < end:
            return window
    return None


def canary_tick(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    current_tokens: int,
    ceiling: int,
    model: str,
    claude_session_id: str,
    directed_by: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One canary reporting tick through the REAL write path.

    The lifecycle clock is re-armed FIRST and unconditionally — that is what
    makes an arrest look like the GAU-01 signature (a session that is still
    working while its gauge has stopped) rather than like an idle session,
    which the detector correctly ignores.

    Refuses to run for an identity that is not an ACTIVE registered canary. A
    tick that would write a synthetic reading against a real session's row is
    the one mistake here that corrupts live fleet data, so it is refused at the
    door rather than guarded by convention.

    ★ GAU-24, 2026-08-19 — DELIBERATELY NOT WIRED TO ANY VERB. This function
    is dead code: no verb dispatches it, and its only repo-wide caller is a
    smoke test driving an in-memory fake, so it has never run against the
    live database. It must NOT be wired as-is: the gauge write below calls
    ``upsert_session_context_status`` DIRECTLY, while production's real
    reporting hook dispatches the VERB ``report_context_status`` — a direct
    store call is the store-plane injection the 2026-08-19 02:02Z ruling item
    4 disallowed. Wiring this correctly means dispatching that verb instead
    (with a ``reporter_surface`` it actually accepts — "canary" is not in its
    allowed set) and then OBSERVING the arrest→withhold behaviour live, not
    inferring it from source; that observation is out of scope for the lane
    that found this. CONSEQUENCE WHILE THIS STAYS UNWIRED: ``arrest_gauge_canary``
    only RECORDS a window — the withhold logic below is the only code that
    reads a window and acts on it, so an arrest currently changes NOTHING
    about any gauge, and the arrest record is attribution metadata only,
    never a behavioural effect. An operator reading an arrest as "the gauge
    write was withheld" would be wrong until this is wired.
    """
    if not is_active_canary(state, agent_instance_id):
        raise CanaryError(
            "not_a_canary",
            f"{agent_instance_id!r} is not an active registered canary; "
            "refusing to write a synthetic gauge reading against it.",
        )
    clock = now or datetime.now(UTC)
    report_alive(
        state,
        agent_instance_id=agent_instance_id,
        status="working",
        directed_by=directed_by,
        status_note="gauge canary tick",
    )
    windows, _ = read_tamper_windows(state, agent_instance_id=agent_instance_id)
    arrest = arrest_in_force(windows, at=clock)
    if arrest is not None:
        return {
            "outcome": TICK_ARRESTED,
            "agent_instance_id": agent_instance_id,
            "measured_at": None,
            "arrest_until": arrest.get("arrest_until"),
            "expected_notice_type": arrest.get("expected_notice_type"),
        }
    upsert_session_context_status(
        state,
        agent_instance_id=agent_instance_id,
        claude_session_id=claude_session_id,
        model=model,
        current_tokens=current_tokens,
        ceiling=ceiling,
        measured_at=clock.isoformat(),
        reporter_surface="canary",
        reporter_generation=3,
    )
    return {
        "outcome": TICK_REPORTED,
        "agent_instance_id": agent_instance_id,
        "measured_at": clock.isoformat(),
        "arrest_until": None,
        "expected_notice_type": None,
    }


def _refuse_self_announcing(agent_instance_id: str, lane_id: str) -> None:
    """Refuse an identity that would announce the canary in its own alarm.

    Only the two fields the staleness notice ECHOES are checked, and that
    narrowness is the point: this guards a measured leak path, not a naming
    aesthetic. ``host`` is deliberately excluded — :data:`SYNTHETIC_HOST` says
    exactly what the row is, and no notice quotes it.
    """
    for field, value in (("agent_instance_id", agent_instance_id), ("lane_id", lane_id)):
        lowered = value.lower()
        for token in SELF_ANNOUNCING_TOKENS:
            if token in lowered:
                raise CanaryError(
                    "self_announcing_identity",
                    f"{field}={value!r} contains {token!r}; the staleness notice "
                    f"quotes {field} verbatim, so this canary would announce "
                    "itself in every alarm it provokes and the detector's "
                    "blindness would hold in code while leaking in prose. Use an "
                    "ordinary-looking identity.",
                )


def register_synthetic_session(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    lane_id: str,
    spawned_by_instance_id: str,
    directed_by: str,
    report_by_seconds: int,
    brief_ref: str = "",
    budget_line: str = "",
    model: str = "",
    work_class: str = WORK_CLASS_READ_ONLY,
) -> dict[str, Any]:
    """Give an already-registered canary the lifecycle row the detector needs —
    and dispatch no process at all.

    ★ THE EXERCISABILITY GAP THIS CLOSES, measured 2026-08-19. The canary
    landed, deployed, and could not be run. The staleness leg only inspects LIVE
    ``managed_session`` rows; ``insert_managed_session`` had exactly one caller,
    inside ``spawn_session``; and ``spawn_session`` always dispatches
    ``driver.spawn(...)``, launching a real process. So a synthetic identity was
    invisible to the detector and could never alarm — while 51 checks passed,
    because the smoke inserted that row itself, at the exact seam only
    production surfaces. A test that builds its own input tests the callee, not
    the wiring.

    ★ THE INVERSE OF ``not_a_canary``, and why that inversion is the whole
    safety story. Every other verb here refuses to act ON a canary's opposite;
    this one refuses to act on anything BUT a canary. Without that, a verb that
    mints live lifecycle rows without a process behind them is precisely the
    tamper path the canary exists to detect — it could fabricate a session that
    looks alive to every fleet surface. It is registered-canary-only, and a
    non-canary identity is refused at the door rather than by convention.

    ★ TWO REFUSALS THAT LOOK LIKE PARAMETER VALIDATION AND ARE NOT. Both
    reproduce, at the door, a skip the detector performs silently:

    * no ``spawned_by_instance_id`` — the staleness leg drops any row without a
      spawner (there would be no steward to notify), so a canary registered
      without one is invisible to the instrument it exists to exercise, and its
      later silence would read as a detector failure;
    * ``report_by_seconds <= 0`` — ``last_report_alive`` returns ``None`` for a
      row with no window, and that ``None`` is "no evidence", not "not
      advancing", so the leg skips the row rather than judging it. A canary
      arrested under a zero window produces silence that means nothing.

    Refusing both loudly here is the difference between a canary that cannot
    fire and a canary that cannot fire FOR A STATED REASON.

    The row is inserted in ``spawning``, exactly as ``spawn_session`` leaves it:
    promotion to ``live`` happens on the first :func:`canary_tick`, through the
    real ``report_alive`` verb, on production's own transition. No second
    promotion rule is minted here — a synthetic session becomes live the same
    way every real one does, by reporting that it is.
    """
    if not is_active_canary(state, agent_instance_id):
        raise CanaryError(
            "not_a_canary",
            f"{agent_instance_id!r} is not an active registered canary; a "
            "lifecycle row may be minted here for a canary and nothing else. "
            "Register the canary first — a verb that mints live sessions for "
            "arbitrary identities would be the tamper path this canary exists "
            "to detect.",
        )
    _refuse_self_announcing(agent_instance_id, lane_id)
    if not spawned_by_instance_id.strip():
        raise CanaryError(
            "missing_steward",
            "spawned_by_instance_id is required: the staleness leg skips every "
            "row without a spawner, so this canary would be invisible to the "
            "detector it exists to exercise and its silence would be "
            "misreadable as the detector failing.",
        )
    if report_by_seconds <= 0:
        raise CanaryError(
            "missing_report_window",
            "report_by_seconds must be positive: with no window, the derived "
            "last report_alive is None — which means NO EVIDENCE, not 'not "
            "advancing' — and the staleness leg skips the row instead of "
            "judging it.",
        )
    try:
        existing = read_managed_session(state, agent_instance_id)
    except SessionNotFoundError:
        existing = None
    if existing is not None:
        raise CanaryError(
            "session_exists",
            f"{agent_instance_id!r} already has a managed_session row "
            f"(lifecycle_state={existing.get('lifecycle_state')!r}); refusing "
            "rather than minting a second identity for one canary.",
        )
    if not directed_by.strip():
        raise CanaryError(
            "missing_directed_by",
            "a synthetic session must record WHO directed it; an unattributable "
            "synthetic identity in the fleet ledger is the thing this verb must "
            "never create.",
        )
    row = insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id,
            lane_id=lane_id,
            brief_ref=brief_ref,
            work_class=work_class,
            budget_line=budget_line,
            host=SYNTHETIC_HOST,
            spawned_by_instance_id=spawned_by_instance_id,
            model=model,
            report_by_seconds=report_by_seconds,
            directed_by=directed_by,
        ),
    )
    return {
        "agent_instance_id": agent_instance_id,
        "lane_id": lane_id,
        "host": SYNTHETIC_HOST,
        "lifecycle_state": str(row.get("lifecycle_state") or ""),
        "spawned_by_instance_id": spawned_by_instance_id,
        "report_by_seconds": row.get("report_by_seconds"),
        "report_by": row.get("report_by"),
        "promoted_by": (
            "the first canary_tick's report_alive — production's own "
            "spawning -> live transition, not a second rule minted here."
        ),
    }


def retire_gauge_canary(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    directed_by: str,
) -> dict[str, Any]:
    """GAU-24 — stand a canary down all the way: the ledger row (if it has
    one) AND the registry mark, in that fixed order.

    ★ WHY THIS EXISTS. Before this fix, ``retire_canary`` (the store
    primitive) was exported but wired to no verb, and the ledger row was
    stuck a second, independent way: ``retire_session`` refused a canary
    row with ``unsupported_on_host`` because ``synthetic`` had no host
    driver. Every canary exercise therefore leaked one active canary mark
    plus one permanently-``live`` ``managed_session`` row that nothing
    could retire — standing debt, accumulating per exercise. GAU-24 closed
    the ledger side by registering a no-op :class:`SyntheticHostDriver`
    (``session_hosts.py``); this function is what makes retiring the
    ledger row and the registry mark reachable from one call.

    ★ TWO ACTS, LEDGER FIRST — AND WHY THAT ORDER. If a ``managed_session``
    row exists for this identity, ``retire_session`` runs FIRST. Only once
    that succeeds (or there was never a row to retire) does the registry
    mark get stamped ``retired_at``. This makes the registry mark mean
    what it says: "this canary's lifecycle is actually finished," never
    "someone asked to retire it." If the ledger step raises (a real
    lifecycle conflict, or a host that still cannot be reached), the
    registry mark is untouched — the canary stays active, honestly,
    rather than recording a retirement that didn't happen.

    ★ THE PARTIAL-FAILURE CASE the brief asked to be named explicitly: if
    the ledger step SUCCEEDS and the registry-mark write then fails (a
    state-layer fault), this function has torn down the ledger row but
    left the registry mark active. That is safely re-drivable, not wedged:
    re-running this function calls ``retire_session`` again, which is
    idempotent on an already-``retired`` row (returns
    ``already_retired: True`` rather than re-transitioning), and then
    retries the registry-mark write, which is an unconditional predicated
    UPDATE — safe to repeat, and simply re-stamps ``retired_at`` with a
    fresh timestamp on a retry.
    """
    if read_canary(state, agent_instance_id) is None:
        raise CanaryError(
            "not_a_canary", f"{agent_instance_id!r} is not a registered canary.",
        )
    try:
        existing_session = read_managed_session(state, agent_instance_id)
    except SessionNotFoundError:
        existing_session = None
    session_result: dict[str, Any] | None = None
    if existing_session is not None:
        session_result = retire_managed_session(
            state, agent_instance_id=agent_instance_id, directed_by=directed_by,
        )
    retire_canary_mark(state, agent_instance_id=agent_instance_id)
    return {
        "agent_instance_id": agent_instance_id,
        "session_row_existed": existing_session is not None,
        "session_retire_result": session_result,
        "canary_mark_retired": True,
    }


def direct_canary_arrest(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    directed_by: str,
    arrest_from: str,
    arrest_until: str,
    expected_notice_type: str,
    reason: str,
) -> dict[str, Any]:
    """Order an audited, BOUNDED arrest of one canary's gauge.

    Every refusal below is a way an unattributable alarm could otherwise be
    produced, which is the failure mode constraint (d) names:

    * an unregistered subject — a tamper against a real session, which would
      manufacture a genuine-looking alarm about a real lane;
    * an unbounded or backwards window — an arrest that never ends makes its
      alarms attributable forever, which is indistinguishable from not being
      attributable at all;
    * an expected alarm outside the domain — a verifier could never match it,
      so the arrest would be unverifiable by construction.
    """
    if not is_active_canary(state, agent_instance_id):
        raise CanaryError(
            "not_a_canary",
            f"{agent_instance_id!r} is not an active registered canary; a "
            "tamper may only ever target a canary, never a real session.",
        )
    if expected_notice_type not in EXPECTABLE_NOTICE_TYPES:
        raise CanaryError(
            "invalid_expected_notice",
            f"expected_notice_type must be one of {list(EXPECTABLE_NOTICE_TYPES)}, "
            f"got {expected_notice_type!r}.",
        )
    start, end = _parse(arrest_from), _parse(arrest_until)
    if start is None or end is None:
        raise CanaryError(
            "invalid_window",
            "arrest_from and arrest_until must both be readable timestamps; an "
            "unreadable bound would be skipped at attribution time, silently "
            "turning a scheduled alarm into an unattributed one.",
        )
    if end <= start:
        raise CanaryError(
            "invalid_window",
            f"the arrest window must be non-empty and forward-going: "
            f"{arrest_from!r} -> {arrest_until!r}.",
        )
    if not directed_by.strip():
        raise CanaryError(
            "missing_directed_by",
            "a tamper must record WHO directed it; an unattributable tamper is "
            "the failure this audit log exists to make impossible.",
        )
    return record_tamper(
        state,
        agent_instance_id=agent_instance_id,
        directed_by=directed_by,
        arrest_from=arrest_from,
        arrest_until=arrest_until,
        expected_notice_type=expected_notice_type,
        reason=reason,
    )


def _classify_alarms(
    alarms: list[dict[str, Any]], windows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(scheduled, unattributed)`` — the mechanical attribution rule.

    An alarm whose `emitted_at` falls inside a logged window AND matches that
    window's expected type is SCHEDULED. Everything else is UNATTRIBUTED, and
    unattributed means REAL — never "probably ours". Requiring the type to
    match as well as the time is what stops a genuine coverage fault during a
    staleness arrest from being quietly absorbed as expected.
    """
    scheduled: list[dict[str, Any]] = []
    unattributed: list[dict[str, Any]] = []
    for alarm in alarms:
        at = _parse(alarm.get("emitted_at"))
        window = None if at is None else arrest_in_force(windows, at=at)
        if window is not None and window.get("expected_notice_type") == alarm.get(
            "notice_type",
        ):
            scheduled.append(alarm)
        else:
            unattributed.append(alarm)
    return scheduled, unattributed


def _closed_windows(
    windows: list[dict[str, Any]], *, now: datetime,
) -> list[dict[str, Any]]:
    """Windows that have ENDED — the only ones whose alarm can be judged.

    A window still open has not had its chance yet, so counting it as a missing
    alarm would fail the canary for the crime of being asked too early.
    """
    closed = []
    for window in windows:
        end = _parse(window.get("arrest_until"))
        if end is not None and end <= now:
            closed.append(window)
    return closed


def _window_was_answered(
    window: dict[str, Any], alarms: list[dict[str, Any]],
) -> bool:
    """Whether any alarm fell inside this window AND matched its expected type.

    Both conditions, deliberately. Matching on time alone would let a genuine
    coverage fault that happened to land during a staleness arrest be absorbed
    as "the alarm we asked for", which converts a real finding into a pass.
    """
    expected = window.get("expected_notice_type")
    for alarm in alarms:
        at = _parse(alarm.get("emitted_at"))
        if at is None:
            continue
        if alarm.get("notice_type") == expected and arrest_in_force([window], at=at):
            return True
    return False


def _decide_verdict(
    *,
    detector_deployed: bool | None,
    silent_windows: list[dict[str, Any]],
    unattributed: list[dict[str, Any]],
    matched_windows: list[dict[str, Any]],
    unscheduled_count: int,
) -> tuple[str, str]:
    """``(verdict, reason)`` from the two edges and the capability input.

    Order matters and is not arbitrary. Capability is checked FIRST because a
    verdict about an instrument that is not running is a false accusation
    whichever way it lands. The armed edge is checked before the quiet edge
    because a silent detector explains any absence of unattributed alarms,
    while the reverse is not true. The no-evidence check is LAST of the four,
    and that position is load-bearing in both directions: an unattributed alarm
    IS positive evidence of a fault and must fail even with no window to its
    name, while an absence of everything must never be read as an absence of
    faults. Only a run that produced something can be graded.
    """
    if detector_deployed is not True:
        measured = "measured as ABSENT" if detector_deployed is False else "not established"
        return VERDICT_ABSTAINED, (
            "the staleness detector's presence in the RUNNING release was "
            f"{measured}, so no alarm was expectable and neither edge can be "
            "judged. A verdict here would be a claim about an instrument that "
            "is not running."
        )
    if silent_windows:
        return VERDICT_FAIL, (
            f"{len(silent_windows)} closed arrest window(s) produced no matching "
            "alarm: the detector was deployed, the gauge was arrested on the "
            "real write path, and nothing fired. That is the armed edge failing."
        )
    if unattributed:
        return VERDICT_FAIL, (
            f"{len(unattributed)} alarm(s) fell outside every logged arrest "
            "window. The quiet edge failed — and note these are NOT canary "
            "noise to dismiss: an unattributed alarm on a canary is either a "
            "real fault or a gap in the audit log, and both need a reader."
        )
    if not matched_windows:
        return VERDICT_NO_EVIDENCE, (
            "no closed arrest window produced its expected alarm, because "
            "there was no closed arrest window to produce one: the canary was "
            "never exercised in this range. UNTESTED, not passing — a quiet "
            "detector and a quiet unexercised canary are indistinguishable "
            "from here, and reading the silence as a pass is the vacuous green "
            "this verifier exists to refuse."
        )
    return VERDICT_PASS, (
        f"both edges hold: {len(matched_windows)} closed arrest window(s) each "
        f"produced their expected alarm, and {unscheduled_count} alarms arrived "
        "outside a window."
    )


def verify_canary(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    detector_deployed: bool | None,
    since: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check BOTH edges for one canary: it alarmed when tampered, and it was
    quiet when healthy (constraint (e)).

    ``detector_deployed`` is the capability input from the release probe, and
    passing ``None`` (meaning "not established") ABSTAINS rather than guessing.
    Abstention is a first-class verdict here: a canary that reports FAIL because
    the detector was never deployed has produced a false accusation about a
    working instrument, which is a worse outcome than reporting nothing.

    The two edges are read from independent evidence and are not each other's
    complement: the armed edge asks whether every CLOSED arrest window produced
    its expected alarm, and the quiet edge asks whether any alarm arrived
    outside every window. A canary that never alarms passes the second and
    fails the first, and one that alarms constantly does the reverse — which is
    exactly why one edge alone is not a test.

    A run that exercised NOTHING returns :data:`VERDICT_NO_EVIDENCE`, never
    ``pass``. Quote this verdict with ``closed_windows`` and
    ``windows_with_expected_alarm`` whenever it is reported: the count is what
    tells a reader whether the green was earned, and a verdict quoted without
    it is the same silence wearing a better word.
    """
    clock = now or datetime.now(UTC)
    windows, windows_truncated = read_tamper_windows(
        state, agent_instance_id=agent_instance_id, since=since,
    )
    alarms, alarms_truncated = read_gauge_notice_records(
        state, agent_instance_id=agent_instance_id, since=since,
    )
    scheduled, unattributed = _classify_alarms(alarms, windows)
    closed = _closed_windows(windows, now=clock)
    matched_windows = [w for w in closed if _window_was_answered(w, alarms)]
    silent_windows = [w for w in closed if not _window_was_answered(w, alarms)]
    verdict, why = _decide_verdict(
        detector_deployed=detector_deployed,
        silent_windows=silent_windows,
        unattributed=unattributed,
        matched_windows=matched_windows,
        unscheduled_count=len(alarms) - len(scheduled),
    )
    return {
        "verdict": verdict,
        "verdict_reason": why,
        "agent_instance_id": agent_instance_id,
        "detector_deployed": detector_deployed,
        "windows_examined": len(windows),
        "closed_windows": len(closed),
        "windows_with_expected_alarm": len(matched_windows),
        "silent_windows": len(silent_windows),
        "alarms_examined": len(alarms),
        "scheduled_alarms": len(scheduled),
        "unattributed_alarms": len(unattributed),
        "truncated": windows_truncated or alarms_truncated,
        "since": since,
    }


__all__ = [
    "EXPECTABLE_NOTICE_TYPES",
    "SELF_ANNOUNCING_TOKENS",
    "SYNTHETIC_HOST",
    "TICK_ARRESTED",
    "TICK_REPORTED",
    "VERDICT_ABSTAINED",
    "VERDICT_FAIL",
    "VERDICT_NO_EVIDENCE",
    "VERDICT_PASS",
    "arrest_in_force",
    "canary_tick",
    "direct_canary_arrest",
    "register_synthetic_session",
    "retire_gauge_canary",
    "verify_canary",
]
