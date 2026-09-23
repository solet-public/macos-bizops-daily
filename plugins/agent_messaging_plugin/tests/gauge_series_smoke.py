#!/usr/bin/env python3
"""Unit smoke for GAU-15 — the gauge SERIES: it is written, it is bounded, and
it separates three states that used to be one alarm.

THE DEFECT this covers. `session_context_status` is a CACHE: one row per
`agent_instance_id`, conflict on that column alone, last write wins. So the
readings on either side of a freeze are overwritten by the subject's own next
write, and a `/clear` keeps the instance id — which is why the 85-minute gauge
freeze of 2026-08-18 (487,777 tokens frozen at 00:50:56Z) could not be analysed
afterwards at all: by the time anyone looked, the successor context had already
overwritten the row. A defect whose entire signature is "a timestamp stopped
advancing" was being detected against a store that kept no record of it
advancing.

AND THE SECOND DEFECT, measured 2026-08-19 across four live lanes: STOPPED,
IDLE and NEVER-STARTED all present as "no recent write", so a steward saw one
alarm for three different facts — two of those lanes were merely undriven and
were reported identically to a genuine gauge failure. The classification here
is what separates them, and it separates them using TWO independent clocks
(the series, and the lifecycle row's own last `report_alive`) rather than one.

★ WHAT WOULD FAIL IF THIS SMOKE WERE WRONG. Each test below names the mutation
it catches, because a green that cannot name its failing mutation is not
evidence. The load-bearing ones:
  * delete the history append from the store → `test_the_write_path_appends`
  * make the prune soft-delete instead of hard → `test_retention_is_a_hard_bound`
  * swallow the append failure instead of raising → `test_a_split_write_is_named`
  * collapse idle into stopped (the tempting simplification, since both look
    like a stalled series) → `test_the_two_clocks_separate_idle_from_stopped`

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/gauge_series_smoke.py
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

from agent_messaging_plugin.context_status_verbs import VerbError  # noqa: E402
from agent_messaging_plugin.gauge_series import (  # noqa: E402
    GAUGE_SERIES_STALL_S,
    GAUGE_STOPPED_CONFIRM_GAP_S,
    SERIES_ABSENT,
    SERIES_HEALTHY,
    SERIES_IDLE,
    SERIES_NEVER_STARTED,
    SERIES_STOPPED,
    SERIES_UNDETERMINED,
    classify_gauge_series,
    confirm_gauge_stopped,
    session_context_status_history,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_CONTEXT_STATUS_HISTORY,
)
from agent_messaging_plugin.session_context_status_store import (  # noqa: E402
    GAUGE_HISTORY_RETENTION,
    GaugeHistoryAppendError,
    read_session_context_status,
    read_session_context_status_history,
    upsert_session_context_status,
)

_passed = 0
_failed: list[str] = []

LEDGER_ID = "agi-73ba7ce552285765b4716a1059326da0"
SESSION_ID = "ases-agi-73ba7ce552285765b4716a1059326da0"
NOW = datetime(2026, 8, 19, 3, 0, 0, tzinfo=UTC)


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


def _write(state: Any, *, tokens: int = 131_979, claude_session_id: str = "c-1") -> None:
    """A gauge write through the REAL store function.

    Going through `upsert_session_context_status` rather than hand-inserting a
    history row is the whole point: a test that builds its own input tests the
    callee, not the wiring, and the wiring is where GAU-01 lived.
    """
    upsert_session_context_status(
        cast("Any", state),
        agent_instance_id=LEDGER_ID,
        claude_session_id=claude_session_id,
        model="claude-opus-5",
        current_tokens=tokens,
        ceiling=1_000_000,
        measured_at=NOW.replace(tzinfo=None).isoformat(),
        reporter_surface="checkout",
        reporter_generation=3,
        agent_session_id=SESSION_ID,
    )


def _wall_now() -> datetime:
    """The clock the STORE stamps `recorded_at` with.

    The classification tests below pin a frozen NOW because they call the pure
    classifier directly. The VERB tests cannot: `upsert_session_context_status`
    stamps `recorded_at` from the real clock (that is the point -- the store's
    own clock is one of the three the series keeps), so a frozen NOW would read
    a just-written row as hours stale and every verb test would classify
    STOPPED. Freezing the store's clock instead would mean testing a seam that
    production does not have.
    """
    return datetime.now(UTC)


def _lifecycle_row(state: Any, *, last_alive: datetime | None, window_s: int = 5400) -> None:
    """Put a managed_session row in place whose derived last report_alive is
    `last_alive` — derived, never stored directly, exactly as production does
    it (`report_by = <call moment> + report_by_seconds`)."""
    record: dict[str, Any] = {
        "agent_instance_id": LEDGER_ID,
        "lifecycle_state": "live",
        "report_by_seconds": window_s,
        "is_deleted": 0,
    }
    if last_alive is not None:
        record["report_by"] = (last_alive + timedelta(seconds=window_s)).isoformat()
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "record": record,
            "conflict_columns": ["agent_instance_id"],
        },
    )


def _history_rows(state: Any) -> list[dict[str, Any]]:
    return state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_SESSION_CONTEXT_STATUS_HISTORY)


# ---------------------------------------------------------------------------
# The write path.
# ---------------------------------------------------------------------------


def test_the_write_path_appends() -> None:
    """CATCHES: the history append being dropped from the store.

    Without this the whole feature can be declared, migrated, and read — and
    never write anything, which is precisely how the cache came to be the only
    record in the first place.
    """
    state = _state()
    _write(state, tokens=100_000)
    rows = _history_rows(state)
    _check(len(rows) == 1, "one gauge write appends exactly one history row")
    if not rows:
        return
    row = rows[0]
    _check(row.get("current_tokens") == 100_000, "the reading itself is carried")
    _check(
        row.get("claude_session_id") == "c-1" and row.get("agent_session_id") == SESSION_ID,
        "the identity columns are carried, including the rotation marker",
    )
    _check(
        bool(row.get("recorded_at")) and bool(row.get("measured_at")),
        "BOTH clocks are stored — the reporter's and the store's",
    )
    _check(
        row.get("reporter_surface") == "checkout" and row.get("reporter_generation") == 3,
        "reporter attribution survives into the series, where an ALTERNATING "
        "value is the two-copies-racing signature a single row can only hint at",
    )


def test_the_cache_is_still_a_cache() -> None:
    """CATCHES: the history append accidentally turning the cache into a log.

    The cache's single-row contract is what the rotation decision reads; the
    series is additive and must not disturb it.
    """
    state = _state()
    _write(state, tokens=1)
    _write(state, tokens=2)
    _write(state, tokens=3)
    cached = read_session_context_status(cast("Any", state), LEDGER_ID)
    _check(cached is not None and cached["current_tokens"] == 3, "the cache holds the LATEST reading only")
    _check(len(_history_rows(state)) == 3, "...while the series holds all three")


def test_retention_is_a_hard_bound() -> None:
    """CATCHES: an unbounded series, and a soft-delete prune.

    A soft delete would leave every row in place behind an `is_deleted` flag
    that nothing in this platform ever reaps — the bound would read as enforced
    while the table grew exactly as fast as before.
    """
    state = _state()
    for i in range(GAUGE_HISTORY_RETENTION + 7):
        _write(state, tokens=i)
    rows = _history_rows(state)
    _check(
        len(rows) == GAUGE_HISTORY_RETENTION,
        f"the series is pruned to exactly {GAUGE_HISTORY_RETENTION} rows "
        f"(found {len(rows)})",
    )
    _check(
        all(int(r.get("is_deleted", 0)) == 0 for r in rows),
        "the pruned rows are GONE, not tombstoned — no is_deleted survivors",
    )
    kept = sorted(int(r["current_tokens"]) for r in rows)
    _check(
        kept and kept[-1] == GAUGE_HISTORY_RETENTION + 6,
        "the NEWEST readings are the ones kept",
    )


def test_a_split_write_is_named() -> None:
    """CATCHES: a swallowed append failure, AND a blanket raise that lies.

    Both writes are separate top-level state calls (deliberately not one
    transaction), so an append failure leaves a COMMITTED cache row. A generic
    'upsert failed' would report a write as failed that half-succeeded and
    invite a caller to re-send it. The error must name the split state.
    """
    state = _state()
    state.fail_next("write")
    raised: GaugeHistoryAppendError | None = None
    try:
        _write(state, tokens=42)
    except GaugeHistoryAppendError as exc:
        raised = exc
    _check(raised is not None, "a failed history append RAISES rather than degrading silently")
    if raised is None:
        return
    _check(
        raised.error_token == "cache_written_history_append_failed"
        and raised.error_token in str(raised),
        "the error names the SPLIT state, not a generic write failure",
    )
    cached = read_session_context_status(cast("Any", state), LEDGER_ID)
    _check(
        cached is not None and cached["current_tokens"] == 42,
        "...and the claim it makes is TRUE: the cache row really is committed",
    )
    _check(not _history_rows(state), "...while the history row really is absent")


# ---------------------------------------------------------------------------
# The classification — the part that separates three facts that looked alike.
# ---------------------------------------------------------------------------


def test_the_two_clocks_separate_idle_from_stopped() -> None:
    """★ THE DISCRIMINATOR, and the reason this verb is not just a list read.

    THE SAME stalled series classifies differently depending ONLY on the second
    clock. Collapsing them — the tempting simplification, since both look like
    "no recent gauge write" — is exactly the 2026-08-19 defect, where two idle
    lanes were reported identically to a broken one.
    """
    stalled = NOW - timedelta(seconds=GAUGE_SERIES_STALL_S + 60)
    working, why_working = classify_gauge_series(
        newest_recorded_at=stalled,
        last_alive=NOW - timedelta(seconds=30),
        lifecycle_readable=True,
        now=NOW,
    )
    idle, why_idle = classify_gauge_series(
        newest_recorded_at=stalled,
        last_alive=stalled,
        lifecycle_readable=True,
        now=NOW,
    )
    _check(working == SERIES_STOPPED, "series stalled + report_alive advancing = STOPPED (GAU-01)")
    _check(idle == SERIES_IDLE, "the SAME stalled series + a stalled report_alive = IDLE, not a fault")
    _check(
        "gauge is dark" in why_working and "not faulty" in why_idle,
        "each verdict states the evidence rather than the conclusion",
    )


def test_the_healthy_and_never_started_ends() -> None:
    """CATCHES: a bound that never reports healthy, and an absent series read
    as a fault."""
    healthy, _ = classify_gauge_series(
        newest_recorded_at=NOW - timedelta(seconds=60),
        last_alive=NOW,
        lifecycle_readable=True,
        now=NOW,
    )
    never, why_never = classify_gauge_series(
        newest_recorded_at=None,
        last_alive=None,
        lifecycle_readable=True,
        now=NOW,
    )
    _check(healthy == SERIES_HEALTHY, "a moving series reads HEALTHY")
    _check(
        never == SERIES_NEVER_STARTED and "no history row" in why_never,
        "no series at all reads NEVER_STARTED, and says so in terms of the "
        "evidence (no history row) rather than a bare verdict",
    )


def test_unreadable_evidence_abstains() -> None:
    """CATCHES: a classifier that guesses when its input is missing.

    A guess here is indistinguishable from a measurement, which is the whole
    failure mode this family of defects keeps producing. Two distinct causes,
    both must abstain rather than default to the confident branch.
    """
    stalled = NOW - timedelta(seconds=GAUGE_SERIES_STALL_S + 60)
    no_row, why_no_row = classify_gauge_series(
        newest_recorded_at=stalled, last_alive=None, lifecycle_readable=False, now=NOW,
    )
    no_window, why_no_window = classify_gauge_series(
        newest_recorded_at=stalled, last_alive=None, lifecycle_readable=True, now=NOW,
    )
    _check(no_row == SERIES_UNDETERMINED, "an unreadable lifecycle row abstains")
    _check(no_window == SERIES_UNDETERMINED, "a row with no report_by window abstains")
    _check(
        "managed_session" in why_no_row and "window" in why_no_window,
        "the two abstentions name DIFFERENT causes — collapsing them would send "
        "someone to the wrong surface",
    )


# ---------------------------------------------------------------------------
# The verb.
# ---------------------------------------------------------------------------


def test_the_verb_reads_the_series_and_classifies_it() -> None:
    """CATCHES: a verb that returns rows without the reading that makes them
    useful."""
    state = _state()
    clock = _wall_now()
    _write(state, tokens=10)
    _write(state, tokens=20)
    _lifecycle_row(state, last_alive=clock - timedelta(seconds=10))
    result = session_context_status_history(
        cast("Any", state), agent_instance_id=LEDGER_ID, now=clock,
    )
    _check(result["resolved"] is True, "a session with a series resolves")
    _check(result["returned"] == 2 and not result["truncated"], "both rows returned, page not truncated")
    _check(
        result["entries"][0]["current_tokens"] == 20,
        "entries are NEWEST FIRST — a reader asking 'what is the latest' must not get the oldest",
    )
    _check(result["series_state"] == SERIES_HEALTHY, "a fresh series with a live session reads healthy")
    _check(result["retention"] == GAUGE_HISTORY_RETENTION, "the verb publishes the bound it is subject to")


def test_the_verb_counts_rotation_boundaries() -> None:
    """CATCHES: a series read as one continuous measurement across a /clear.

    The instance id survives a rotation and the Claude session id does not, so
    without this count a reader would treat the 487,777 -> 253,247 step of the
    original incident as a measurement rather than a new context.
    """
    state = _state()
    clock = _wall_now()
    _write(state, tokens=400_000, claude_session_id="before")
    _write(state, tokens=10_000, claude_session_id="after")
    _lifecycle_row(state, last_alive=clock)
    result = session_context_status_history(
        cast("Any", state), agent_instance_id=LEDGER_ID, now=clock,
    )
    _check(result["rotation_boundaries"] == 1, "one /clear splice in the page is counted")


def test_truncation_is_published_not_implied() -> None:
    """CATCHES: a silently capped page, which answers 'when did this stop' with
    a boundary the caller picked by accident."""
    state = _state()
    clock = _wall_now()
    for i in range(5):
        _write(state, tokens=i)
    _lifecycle_row(state, last_alive=clock)
    result = session_context_status_history(
        cast("Any", state), agent_instance_id=LEDGER_ID, limit=2, now=clock,
    )
    _check(result["returned"] == 2, "the page honours the requested limit")
    _check(result["truncated"] is True, "...and SAYS the series continues past it")


def test_an_unknown_session_is_an_honest_gap() -> None:
    """CATCHES: a KeyError-shaped absence. The gap shape must carry the same
    keys as a resolved read, or a caller crashes on a legitimate gap."""
    state = _state()
    result = session_context_status_history(
        cast("Any", state), agent_instance_id="agi-never-seen", now=NOW,
    )
    _check(result["resolved"] is False, "an unknown session resolves False rather than raising")
    _check(result["series_state"] == SERIES_NEVER_STARTED, "...and is classified NEVER_STARTED")
    _check(
        {"entries", "returned", "truncated", "retention", "rotation_boundaries"} <= set(result),
        "the gap shape carries the SAME key set as a resolved read",
    )


def test_the_store_read_reports_its_own_truncation() -> None:
    """CATCHES: the store-level read hiding truncation from the verb."""
    state = _state()
    for i in range(4):
        _write(state, tokens=i)
    rows, truncated = read_session_context_status_history(cast("Any", state), LEDGER_ID, limit=2)
    _check(len(rows) == 2 and truncated, "the store read returns the page AND that more exist")
    rows_all, truncated_all = read_session_context_status_history(cast("Any", state), LEDGER_ID)
    _check(len(rows_all) == 4 and not truncated_all, "a full read reports no truncation")


def test_gau19_single_sample_never_escalates_the_race_window() -> None:
    """★ THE DISCRIMINATOR for GAU-19. CATCHES: a "fix" that just re-classifies
    the single read differently (e.g. widening the stall bound) instead of
    actually requiring a second read -- the discriminator here is the RACE,
    not the staleness. First read lands INSIDE the inter-hook window: gauge
    stale, report_alive just landed (the textbook single-sample STOPPED
    signature). Second read, taken past the confirm gap, shows the gauge
    caught up -- proving it was the race, not a freeze.
    """
    first_now = NOW
    race_last_alive = first_now - timedelta(seconds=20)
    stopped_state, _ = classify_gauge_series(
        newest_recorded_at=first_now - timedelta(seconds=GAUGE_SERIES_STALL_S + 381),
        last_alive=race_last_alive,
        lifecycle_readable=True,
        now=first_now,
    )
    _check(stopped_state == SERIES_STOPPED, "setup: the single sample DOES read STOPPED (the bug this fixes)")

    second_now = first_now + timedelta(seconds=GAUGE_STOPPED_CONFIRM_GAP_S + 5)
    confirmed, why = confirm_gauge_stopped(
        first_newest_recorded_at=first_now - timedelta(seconds=GAUGE_SERIES_STALL_S + 381),
        first_last_alive=race_last_alive,
        first_lifecycle_readable=True,
        first_now=first_now,
        second_newest_recorded_at=second_now - timedelta(seconds=5),
        second_last_alive=second_now - timedelta(seconds=2),
        second_lifecycle_readable=True,
        second_now=second_now,
    )
    _check(confirmed is False, "the race window does NOT escalate once re-read")
    _check("race window" in why, "the refusal names the race, not a bare 'not stopped'")


def test_gau19_a_genuine_freeze_still_escalates() -> None:
    """CATCHES: a fix that suppresses ALL single-sample STOPPED reads,
    including real ones -- the negative control the discriminator above
    needs. Both reads show the gauge stalled at the SAME old write while
    report_alive keeps landing (advancing between the two reads) -- this is
    GAU-01, not the GAU-19 race, and must still alarm.
    """
    first_now = NOW
    frozen_write = first_now - timedelta(seconds=GAUGE_SERIES_STALL_S + 500)
    second_now = first_now + timedelta(seconds=GAUGE_STOPPED_CONFIRM_GAP_S + 30)
    confirmed, why = confirm_gauge_stopped(
        first_newest_recorded_at=frozen_write,
        first_last_alive=first_now - timedelta(seconds=10),
        first_lifecycle_readable=True,
        first_now=first_now,
        second_newest_recorded_at=frozen_write,
        second_last_alive=second_now - timedelta(seconds=8),
        second_lifecycle_readable=True,
        second_now=second_now,
    )
    _check(confirmed is True, "a genuine freeze (gauge dark at both reads, report_alive advancing) STILL escalates")
    _check("genuinely dark" in why, "the confirmation states the evidence, not a bare verdict")


def test_gau19_report_alive_not_advancing_reads_as_idle_not_stopped() -> None:
    """CATCHES: confirming a STOPPED verdict off a session that went IDLE
    between the two reads (GAU-18 territory) -- report_alive must ADVANCE,
    not merely still exist, or this collapses back into the false alarm."""
    first_now = NOW
    frozen_write = first_now - timedelta(seconds=GAUGE_SERIES_STALL_S + 500)
    same_alive = first_now - timedelta(seconds=10)
    second_now = first_now + timedelta(seconds=GAUGE_STOPPED_CONFIRM_GAP_S + 30)
    confirmed, why = confirm_gauge_stopped(
        first_newest_recorded_at=frozen_write,
        first_last_alive=same_alive,
        first_lifecycle_readable=True,
        first_now=first_now,
        second_newest_recorded_at=frozen_write,
        second_last_alive=same_alive,
        second_lifecycle_readable=True,
        second_now=second_now,
    )
    _check(confirmed is False, "report_alive NOT advancing refuses to confirm STOPPED")
    _check("did not advance" in why, "the refusal names report_alive's own stall, not a generic no")


def test_gau19_too_short_a_gap_is_inconclusive_not_a_pass() -> None:
    """CATCHES: a confirm gap of zero (calling classify_gauge_series twice on
    the same instant, which proves nothing about the race resolving)."""
    first_now = NOW
    frozen_write = first_now - timedelta(seconds=GAUGE_SERIES_STALL_S + 500)
    second_now = first_now + timedelta(seconds=5)
    confirmed, why = confirm_gauge_stopped(
        first_newest_recorded_at=frozen_write,
        first_last_alive=first_now - timedelta(seconds=10),
        first_lifecycle_readable=True,
        first_now=first_now,
        second_newest_recorded_at=frozen_write,
        second_last_alive=second_now - timedelta(seconds=1),
        second_lifecycle_readable=True,
        second_now=second_now,
    )
    _check(confirmed is False, "a too-fast re-read refuses rather than confirms")
    _check("inconclusive" in why, "the refusal says inconclusive, not 'not stopped'")


def test_gau19_first_read_not_stopped_short_circuits() -> None:
    """CATCHES: confirming a STOPPED verdict when the first read was never
    STOPPED to begin with (e.g. HEALTHY or IDLE)."""
    first_now = NOW
    confirmed, why = confirm_gauge_stopped(
        first_newest_recorded_at=first_now - timedelta(seconds=30),
        first_last_alive=first_now,
        first_lifecycle_readable=True,
        first_now=first_now,
        second_newest_recorded_at=first_now - timedelta(seconds=30),
        second_last_alive=first_now,
        second_lifecycle_readable=True,
        second_now=first_now + timedelta(seconds=GAUGE_STOPPED_CONFIRM_GAP_S + 5),
    )
    _check(confirmed is False, "a first read that was never STOPPED never confirms")
    _check("nothing to confirm" in why, "the refusal says there was nothing to confirm")


def test_gau19_non_monotonic_reads_fail_loud() -> None:
    """CATCHES: silently accepting two reads out of order, which would let a
    caller launder a stale re-check as a fresh confirmation."""
    first_now = NOW
    try:
        confirm_gauge_stopped(
            first_newest_recorded_at=first_now - timedelta(seconds=GAUGE_SERIES_STALL_S + 500),
            first_last_alive=first_now,
            first_lifecycle_readable=True,
            first_now=first_now,
            second_newest_recorded_at=first_now - timedelta(seconds=GAUGE_SERIES_STALL_S + 500),
            second_last_alive=first_now,
            second_lifecycle_readable=True,
            second_now=first_now - timedelta(seconds=1),
        )
        _check(False, "a non-monotonic read pair raises rather than silently accepting")
    except VerbError as exc:
        _check(exc.code == "non_monotonic_reads", "raises the named non_monotonic_reads error")


def test_gau18_no_registry_preserves_pre_gau18_idle_behavior() -> None:
    """CATCHES: a change that alters IDLE's behaviour for callers who never
    opted into the watcher-presence join -- backward compatibility is the
    point of ``watcher_present`` defaulting to ``None``."""
    stalled = NOW - timedelta(seconds=GAUGE_SERIES_STALL_S + 60)
    state, why = classify_gauge_series(
        newest_recorded_at=stalled,
        last_alive=stalled,
        lifecycle_readable=True,
        now=NOW,
    )
    _check(state == SERIES_IDLE, "omitting watcher_present still reads IDLE")
    _check(
        "not faulty" in why and "STILL COVERS TWO SITUATIONS" not in why,
        "the ORIGINAL reason text is unchanged when the caller opted out",
    )


def test_gau18_presence_confirmed_reads_idle_with_the_mandatory_caveat() -> None:
    """★ THE HARD CONSTRAINT (coordinating-seat ruling, 2026-08-20): the idle reason
    line MUST plainly state it still covers two situations. CATCHES: a
    label that implies precision it does not have -- e.g. dropping the
    caveat once presence is confirmed, which is exactly the trap the
    ruling names."""
    stalled = NOW - timedelta(seconds=GAUGE_SERIES_STALL_S + 60)
    state, why = classify_gauge_series(
        newest_recorded_at=stalled,
        last_alive=stalled,
        lifecycle_readable=True,
        now=NOW,
        watcher_present=True,
    )
    _check(state == SERIES_IDLE, "a live watcher-presence row reads IDLE, not ABSENT")
    _check(
        "STILL COVERS TWO SITUATIONS" in why and "undriven" in why and "mid-call" in why,
        "the reason line names BOTH situations idle still conflates",
    )
    _check(
        "Nothing separates them" in why or "nothing separates them" in why.lower(),
        "the reason line states plainly that nothing separates them",
    )


def test_gau18_no_presence_reads_absent_not_idle() -> None:
    """CATCHES: collapsing ABSENT back into IDLE -- the whole point of the
    split is that a process that has vanished is a materially different
    fact from one that is alive but undriven."""
    stalled = NOW - timedelta(seconds=GAUGE_SERIES_STALL_S + 60)
    state, why = classify_gauge_series(
        newest_recorded_at=stalled,
        last_alive=stalled,
        lifecycle_readable=True,
        now=NOW,
        watcher_present=False,
    )
    _check(state == SERIES_ABSENT, "no live watcher-presence row reads ABSENT")
    _check("process itself appears gone" in why, "the reason states the process is gone, not merely quiet")


def test_gau18_watcher_presence_joins_on_agent_session_id_never_derived() -> None:
    """CATCHES: joining watcher presence on ``agent_instance_id`` (the
    ledger id) instead of ``agent_session_id`` -- GAU-26's own ruling is
    that a watcher registers under a DERIVED ``agi-watch-...`` id the
    ledger id will never match, so a ledger-keyed join always reads
    ``None``/absent even for a genuinely present watcher."""

    class _FakeBinding:
        updated_at = NOW.isoformat()

    class _FakeRegistry:
        def __init__(self) -> None:
            self.seen_session_id: str | None = None

        def resolve_by_agent_session_id(self, agent_session_id: str) -> _FakeBinding | None:
            self.seen_session_id = agent_session_id
            return _FakeBinding() if agent_session_id == SESSION_ID else None

    registry = _FakeRegistry()
    from agent_messaging_plugin.gauge_series import _watcher_presence  # noqa: PLC0415

    present = _watcher_presence(cast("Any", registry), SESSION_ID, now=NOW)
    _check(present is True, "a fresh binding resolved by agent_session_id reads present")
    _check(registry.seen_session_id == SESSION_ID, "the join was made on agent_session_id, not a derived id")

    stale_registry = _FakeRegistry()
    stale_result = _watcher_presence(cast("Any", stale_registry), "", now=NOW)
    _check(stale_result is None, "an empty agent_session_id opts out (None), never reads as absent")

    no_registry_result = _watcher_presence(None, SESSION_ID, now=NOW)
    _check(no_registry_result is None, "no registry supplied opts out (None), never reads as absent")


def main() -> int:
    tests = (
        test_the_write_path_appends,
        test_the_cache_is_still_a_cache,
        test_retention_is_a_hard_bound,
        test_a_split_write_is_named,
        test_the_two_clocks_separate_idle_from_stopped,
        test_the_healthy_and_never_started_ends,
        test_unreadable_evidence_abstains,
        test_the_verb_reads_the_series_and_classifies_it,
        test_the_verb_counts_rotation_boundaries,
        test_truncation_is_published_not_implied,
        test_an_unknown_session_is_an_honest_gap,
        test_the_store_read_reports_its_own_truncation,
        test_gau19_single_sample_never_escalates_the_race_window,
        test_gau19_a_genuine_freeze_still_escalates,
        test_gau19_report_alive_not_advancing_reads_as_idle_not_stopped,
        test_gau19_too_short_a_gap_is_inconclusive_not_a_pass,
        test_gau19_first_read_not_stopped_short_circuits,
        test_gau19_non_monotonic_reads_fail_loud,
        test_gau18_no_registry_preserves_pre_gau18_idle_behavior,
        test_gau18_presence_confirmed_reads_idle_with_the_mandatory_caveat,
        test_gau18_no_presence_reads_absent_not_idle,
        test_gau18_watcher_presence_joins_on_agent_session_id_never_derived,
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
