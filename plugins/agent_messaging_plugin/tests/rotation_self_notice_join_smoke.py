#!/usr/bin/env python3
"""Smoke for the L4c ROUTING JOIN (2026-08-18): the `agent_session_id` column
that makes a watcher-held worker reachable from its own gauge row.

★ WHY THIS FILE EXISTS SEPARATELY FROM `rotation_self_notice_smoke.py`, and
why that separation is itself the finding. The existing 44-assertion L4c suite
passes GREEN AND UNCHANGED against this landing -- and would pass equally
against a build in which the join is dead code. That is not a criticism of it;
it tests the leg's DECISIONS (band, latch, staleness) with a fake registry that
resolves by instance id alone. The join is a WIRING fact, and a fake that
answers the question you ask it cannot tell you whether the wiring exists. So
everything here runs the REAL `PeerRegistry` over a REAL in-memory
`peer_binding` store and a REAL `BridgeSessionManager`, and asserts on an event
that really landed on the bridge -- construct, mutate, observe, rather than
construct and inspect.

★ THE CONTROL IS THE TEST. `test_the_join_is_what_does_the_work` runs the
IDENTICAL scenario with the join column NULL and asserts it is unroutable. Any
of these deliveries could otherwise be explained by "the instance lookup
happened to work", which is precisely the pre-change build. One passing
delivery proves nothing without the paired failure.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/rotation_self_notice_join_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
)
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin import rotation_self_notice as rsn  # noqa: E402
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.context_status_verbs import (  # noqa: E402
    report_context_status,
    session_context_status,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import (  # noqa: E402
    PeerRegistry,
    PeerSessionAmbiguousError,
)
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    TABLE_SESSION_CONTEXT_STATUS,
    get_peer_binding_schema,
    get_session_context_status_schema,
)
from agent_messaging_plugin.session_context_status_store import (  # noqa: E402
    upsert_session_context_status,
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


# ---------------------------------------------------------------------------
# THE TWO IDS, held apart on purpose.
#
# A watcher-held worker's gauge row keys on its LEDGER id while its live bridge
# binding keys on its WATCH id. The whole defect is that those are different
# strings for one session. They are named here so that any test which
# accidentally uses one where it means the other reads wrong on sight.
#
# ★ THE SESSION ID IS DELIBERATELY NOT `"ases-" + LEDGER`. The live launcher
# does derive it that way today, and a join built by reconstructing the prefix
# would pass every test that used a realistic-looking value -- including a live
# one -- while never resolving anything through the registry. Choosing an
# unrelated string is what makes this suite able to fail that implementation.
# ---------------------------------------------------------------------------

LEDGER_ID = "agi-ledger0000000000000000000000"
WATCH_ID = "agi-watch-0f0f0f0f0f0f0f0f0f0f"
SESSION_ID = "ases-deliberately-unrelated-to-the-ledger-id"

SEAT_ID = "agi-seat00000000000000000000000"
SEAT_SESSION_ID = "ases-seat-session"


def _now() -> datetime:
    return datetime(2026, 8, 18, 3, 0, 0, tzinfo=UTC)


def _fresh_stamp() -> str:
    """A `measured_at` comfortably inside SELF_NOTICE_STALENESS_S.

    Written NAIVE, like the live column: the DATETIME type drops the offset,
    and the leg's own age arithmetic has to re-attach UTC. Using an aware
    stamp here would quietly test an easier shape than production has.
    """
    return (_now() - timedelta(seconds=60)).replace(tzinfo=None).isoformat()


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _registry_with(
    bindings: list[tuple[str, str]],
) -> tuple[PeerRegistry, BridgeSessionManager, dict[str, str]]:
    """A REAL registry + REAL bridge manager, with one open bridge per binding.

    ``bindings`` is ``[(agent_instance_id, agent_session_id), ...]``. Returns
    the registry, the manager, and ``{agent_instance_id: bridge_id}`` so a test
    can assert WHICH bridge received an event rather than merely that one did.
    """
    store: Store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    registry = PeerRegistry(bindings_store=store)
    manager = BridgeSessionManager(
        session_id_factory=lambda _name: "ags-join-smoke",
        idle_timeout_s=3600,
        max_pending_events=20,
        long_poll_timeout_s=1,
    )
    bridges: dict[str, str] = {}
    for index, (instance_id, session_id) in enumerate(bindings):
        bridge = manager.open(solet_name="", parent_pid=None)
        bridges[instance_id] = bridge.bridge_id
        registry.register(
            BridgeBinding(
                bridge_id=bridge.bridge_id,
                agent_id="claude_code",
                agent_instance_id=instance_id,
                # Distinct per binding: `register` hard-deletes any existing row
                # sharing a non-empty session_label, so reusing one label would
                # silently evict the previous binding and quietly reduce a
                # two-session test to a one-session test.
                session_label=f"lane-under-test-{index}",
                parent_pid=None,
                agent_session_id=session_id,
            ),
        )
    return registry, manager, bridges


def _write_gauge_row(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    agent_session_id: str | None,
    current_tokens: int = 336_000,
) -> None:
    """Write the row through the REAL store function, not a hand-built dict.

    Going through `upsert_session_context_status` is deliberate: a test that
    hand-writes the row into the fake would still pass if the store forgot to
    persist the new column, which is one of the two places this landing could
    silently do nothing.
    """
    upsert_session_context_status(
        state,
        agent_instance_id=agent_instance_id,
        claude_session_id="c-session",
        model="claude-opus-5",
        current_tokens=current_tokens,
        ceiling=1_000_000,
        measured_at=_fresh_stamp(),
        agent_session_id=agent_session_id,
    )


class _RecordingMessagingService:
    """The durable half, recorded rather than exercised (GAU-06 G2).

    This file's subject is ROUTING -- which key resolves a session's binding --
    so the durable write is stood in for rather than run against a real service
    and repository. What it must NOT be is absent: the leg refuses to notify at
    all without a messaging service, so omitting it here would make every
    routing assertion in this file pass or fail for a reason that has nothing to
    do with the join. The request SHAPE is asserted in
    ``rotation_self_notice_smoke.py``, which is where it belongs.
    """

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def peer_send(self, request: Any) -> object:
        self.sent.append(request)
        # Carries a thread_id because the writer PRUNES the thread it just
        # wrote to (GAU-06 retention). A bare object() here is not a smaller
        # stand-in, it is one the notify path cannot get past -- which is how
        # this file went red the second time on the same change.
        return SimpleNamespace(
            thread_id=f"agt-rotation-{request.peer_agent_instance_id}",
            message_id="agm-join-fake",
            cursor=1,
        )


def _sweep(
    state: StateManagementInterface,
    registry: PeerRegistry,
    manager: BridgeSessionManager,
) -> rsn.SelfNoticeCounts:
    return rsn.sweep_rotation_self_notice(
        state,
        now=_now(),
        peer_registry=registry,
        bridge_manager=manager,
        agent_messaging_service=_RecordingMessagingService(),  # type: ignore[arg-type]
        latch=rsn.BandEdgeLatch(),
    )


def _events_on(manager: BridgeSessionManager, bridge_id: str) -> list[Any]:
    """Events really queued on that bridge.

    Read as a plain list rather than through `events_after`, which DRAINS what
    a cursor acknowledges -- a draining read would let one assertion change
    what the next one sees, and these tests assert on the same bridge twice.
    """
    bridge = manager.get(bridge_id)
    return list(bridge.pending_events) if bridge is not None else []


# ---------------------------------------------------------------------------
# The headline: the thing that was impossible before this landing.
# ---------------------------------------------------------------------------

def test_a_watcher_held_worker_is_reached_through_the_join() -> None:
    """★ THE LANDING, end to end, through real wiring.

    CATCHES: the join not being wired at any of its four joints -- the schema
    column, the store write, the read back into the leg, or the registry
    lookup. Any one of them missing and this row is unroutable exactly as it
    was before.

    The gauge row is keyed on the LEDGER id. The only live binding is keyed on
    the WATCH id. `resolve_by_agent_instance_id(LEDGER_ID)` MUST miss -- that
    is the defect -- so a delivery here can only have come through
    `resolve_by_agent_session_id`.
    """
    state = _state()
    registry, manager, bridges = _registry_with([(WATCH_ID, SESSION_ID)])
    _write_gauge_row(state, agent_instance_id=LEDGER_ID, agent_session_id=SESSION_ID)

    _check(
        registry.resolve_by_agent_instance_id(LEDGER_ID) is None,
        "PREMISE: the ledger id resolves to NOTHING -- if this ever passes, the "
        "test below stops being about the join and silently becomes trivial",
    )

    counts = _sweep(state, registry, manager)
    _check(counts.appended == 1, "the watcher-held worker IS notified (was impossible)")
    _check(
        counts.unroutable == 0 and counts.undeliverable == 0,
        "...and is not counted as a gap -- all three numbers pinned, since "
        "appended==1 alone cannot rule out a second row being miscounted",
    )

    events = _events_on(manager, bridges[WATCH_ID])
    _check(len(events) == 1, "exactly one event really landed on the WATCH binding's bridge")
    _check(
        bool(events) and events[0].event_type == rsn.EVENT_ROTATION_SELF_NOTICE,
        "and it is a rotation_self_notice, not some other event",
    )
    _check(
        bool(events) and events[0].content == "context is 336,000 — make sure everything is durable.",
        "and its prose is the one-line durability notice",
    )


def test_the_join_is_what_does_the_work() -> None:
    """★ THE CONTROL. Identical scenario, join column NULL.

    Without this, the test above is consistent with "resolution succeeded for
    some other reason" -- which is precisely what the pre-change build did on
    bridge-held sessions. This is the pre-change build, reproduced on purpose:
    it must still be unroutable, and it must still deliver nothing.

    It also pins the SEMANTICS of NULL: a pre-upgrade reporter is a counted,
    benign coverage gap, never a crash and never a silent skip.
    """
    state = _state()
    registry, manager, bridges = _registry_with([(WATCH_ID, SESSION_ID)])
    _write_gauge_row(state, agent_instance_id=LEDGER_ID, agent_session_id=None)

    counts = _sweep(state, registry, manager)
    _check(counts.appended == 0, "a row with NO join is NOT notified -- the pre-change behaviour")
    _check(counts.unroutable == 1, "...and is counted as unroutable, not silently skipped")
    _check(
        not _events_on(manager, bridges[WATCH_ID]),
        "...and nothing was delivered to the bridge that a working join would have found",
    )


def test_the_join_is_resolved_not_reconstructed() -> None:
    """★ THE RULING'S CENTRAL BOUND, made falsifiable.

    CATCHES: `"ases-" + agent_instance_id`, `startswith("ases-")`, prefix
    slicing -- any implementation that derives the session id from the ledger
    id instead of resolving the stored one.

    Such an implementation passes the headline test whenever the fixture uses a
    realistic-looking id, which is why LEDGER_ID and SESSION_ID here share no
    substring. Here the STORED join points at a session id that a reconstructor
    could never produce, and the reconstructable value belongs to a DIFFERENT
    live session. So a reconstructing build does not merely fail to deliver --
    it delivers to the wrong session, and this test says which.
    """
    state = _state()
    decoy_session_id = f"ases-{LEDGER_ID}"  # exactly what a reconstructor would build
    registry, manager, bridges = _registry_with(
        [(WATCH_ID, SESSION_ID), (SEAT_ID, decoy_session_id)],
    )
    _write_gauge_row(state, agent_instance_id=LEDGER_ID, agent_session_id=SESSION_ID)

    counts = _sweep(state, registry, manager)
    _check(counts.appended == 1, "the stored join resolves")
    _check(
        len(_events_on(manager, bridges[WATCH_ID])) == 1,
        "the notice went to the session the STORED join names",
    )
    _check(
        not _events_on(manager, bridges[SEAT_ID]),
        "★ and NOT to the session a reconstructed 'ases-'+ledger id would have "
        "named -- a prefix-building implementation misroutes here instead of "
        "merely failing, which is the harm the ruling names",
    )


def test_the_bridge_held_case_still_resolves_on_its_own_key() -> None:
    """CATCHES: making the join MANDATORY -- i.e. a build that only consults
    `agent_session_id` and so breaks every operator seat, the exact population
    the leg was written for.

    A seat's gauge row and binding share one id. It must resolve with the join
    column NULL, because a seat reported by a pre-upgrade hook is still the
    most important session on the fleet.
    """
    state = _state()
    registry, manager, bridges = _registry_with([(SEAT_ID, SEAT_SESSION_ID)])
    _write_gauge_row(state, agent_instance_id=SEAT_ID, agent_session_id=None)

    counts = _sweep(state, registry, manager)
    _check(counts.appended == 1, "a bridge-held seat resolves on its own key with NO join stored")
    _check(
        len(_events_on(manager, bridges[SEAT_ID])) == 1,
        "...and really receives the notice",
    )


def test_an_empty_join_is_not_a_lookup() -> None:
    """CATCHES: `str(row.get(...) or "")` at the read boundary, which maps NULL
    to `""`.

    An empty session id is not a session id. `resolve_by_agent_session_id`
    returns None for it, so the outcome LOOKS right -- unroutable either way --
    which is exactly why this is worth pinning: the bug would be invisible in
    the counts and would only surface as a wasted lookup per row per tick,
    forever.
    """
    state = _state()
    registry, manager, _ = _registry_with([(WATCH_ID, SESSION_ID)])
    _write_gauge_row(state, agent_instance_id=LEDGER_ID, agent_session_id="")

    counts = _sweep(state, registry, manager)
    _check(counts.appended == 0, "an empty join does not resolve")
    _check(counts.unroutable == 1, "...and is counted as the coverage gap it is")


def test_two_bindings_for_one_session_id_fails_loud() -> None:
    """CATCHES: swallowing `PeerSessionAmbiguousError` into `unroutable`.

    A session holds at most one live bridge, so two bindings for one session id
    is a corrupt registry. The leg's job is delivering a context measurement to
    the RIGHT session; guessing between two candidates could deliver it to the
    wrong one. Loud beats quiet here, and the rider's per-leg try/except is what
    keeps loud from costing the sibling legs their tick.
    """
    state = _state()
    registry, manager, _ = _registry_with([(WATCH_ID, SESSION_ID), (SEAT_ID, SESSION_ID)])
    _write_gauge_row(state, agent_instance_id=LEDGER_ID, agent_session_id=SESSION_ID)

    raised = False
    try:
        _sweep(state, registry, manager)
    except PeerSessionAmbiguousError:
        raised = True
    _check(raised, "a duplicate session-id binding raises rather than being counted as a gap")


# ---------------------------------------------------------------------------
# The column itself, and the write/read round trip.
# ---------------------------------------------------------------------------

def test_the_column_is_declared_and_nullable() -> None:
    """CATCHES: the store writing a column the schema never declared.

    `session_context_status` is now IN `RealShapeState._schema_enforcement`
    (GAU-03, 2026-08-19), so the fake rejects an undeclared column on this
    table exactly as live postgres does, and the store write below is itself
    the round-trip check -- it would fail loud, here, on a store that named a
    column the schema does not declare. The landing that added the join column
    could not rely on that: the table was outside the allowlist, an undeclared
    write round-tripped GREEN through the fake, and the check had to read the
    declared schema structurally instead. That workaround is retired; the
    structural assertions that remain are the ones about the DECLARATION
    (presence and nullability), which no write can make for us.

    NULLABLE is load-bearing twice: it is the NOT-REPORTED tri-state, and it is
    what makes the migration safe -- the state layer reconciles this as ALTER
    TABLE ADD COLUMN, which is instant for a nullable column on a populated
    table and fails outright for a NOT NULL one with no default.
    """
    columns = get_session_context_status_schema().columns
    _check("agent_session_id" in columns, "the join column is declared on the table")
    _check(
        "agent_session_id" in columns and not columns["agent_session_id"].not_null,
        "...and is NULLABLE -- a NOT NULL add would fail the migration on the "
        "populated live table",
    )

    state = _state()
    _write_gauge_row(state, agent_instance_id=LEDGER_ID, agent_session_id=SESSION_ID)
    rows = cast("Any", state)._rows  # noqa: SLF001
    written = next(iter(rows.values()))[0]
    _check(
        written.get("agent_session_id") == SESSION_ID,
        "the store's write is accepted by the now-enforcing fake and persists "
        "the join column",
    )


def test_the_fake_enforces_this_tables_declared_schema() -> None:
    """THE NEGATIVE CONTROL for the check above, and the reason GAU-03 was a
    defect rather than a preference.

    Without this, `test_the_column_is_declared_and_nullable`'s round trip is
    indistinguishable from the pre-GAU-03 world: a fake that enforces NOTHING
    for this table accepts every write, so the write passing proves only that
    the store ran. This asserts the discriminator directly -- an undeclared
    column MUST come back as a provider-ERROR envelope (`action_status` other
    than 'completed'), which is how postgres rejects 'column does not exist'.
    Delete the table from `_schema_enforcement` and this test is the one that
    goes red.

    The write goes through `state.upsert_state` rather than the store, on
    purpose: the store cannot name an undeclared column (that is the property
    under test upstream), so the drift has to be injected at the state call
    the store makes.
    """
    state = _state()
    result = cast("Any", state).upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_CONTEXT_STATUS,
            "record": {
                "agent_instance_id": LEDGER_ID,
                "claude_session_id": "c-session",
                "model": "claude-opus-5",
                "current_tokens": 1,
                "ceiling": 1_000_000,
                "measured_at": _fresh_stamp(),
                "not_a_declared_column": "drift",
            },
            "conflict_columns": ["agent_instance_id"],
        },
    )
    _check(
        result.get("action_status") != "completed",
        "an UNDECLARED column on session_context_status is rejected by the fake "
        "(postgres would reject it live)",
    )
    _check(
        "not_a_declared_column" in str(result.get("error", {})),
        "...and the rejection names the offending column",
    )


def test_the_verb_round_trips_the_join_and_preserves_null() -> None:
    """CATCHES: the verb accepting the field and dropping it, and the read verb
    coercing NULL to "".

    Both directions are asserted because they fail differently: a dropped write
    makes every session unroutable, while a NULL coerced to "" makes a
    pre-upgrade reporter indistinguishable from a session that could not be
    routed -- the exact discriminator failure the counts exist to avoid.
    """
    state = _state()
    report_context_status(
        state,
        agent_instance_id=LEDGER_ID,
        runtime_session_id="c-session",
        provider="anthropic",
        runtime="claude_code",
        model="claude-opus-5",
        effort="high",
        current_tokens=281_507,
        ceiling=1_000_000,
        measured_at=_fresh_stamp(),
        agent_session_id=SESSION_ID,
    )
    view = session_context_status(state, agent_instance_id=LEDGER_ID)
    _check(view["agent_session_id"] == SESSION_ID, "the verb round-trips the join value")

    other = _state()
    report_context_status(
        other,
        agent_instance_id=SEAT_ID,
        runtime_session_id="c-session",
        provider="anthropic",
        runtime="claude_code",
        model="claude-opus-5",
        effort="high",
        current_tokens=281_507,
        ceiling=1_000_000,
        measured_at=_fresh_stamp(),
    )
    unreported = session_context_status(other, agent_instance_id=SEAT_ID)
    _check(
        unreported["agent_session_id"] is None,
        "an unreported join reads back as None, NOT '' -- NOT REPORTED must "
        "stay distinct from 'could not route'",
    )


# ---------------------------------------------------------------------------
# The REPORTER half. Everything above tests the read side; if the hook never
# sends the value, all of it is inert and every test above still passes.
#
# ★ SHAPE-AWARE, because this half is the only part of the file that reads the
# TREE rather than the code under test, and the two trees this file runs in do
# not have the same shape. A DEV CHECKOUT carries `.claude/hooks/` (the copy
# Claude Code actually executes here); a BORN CLONE -- the seed's own
# born-clone gate target, and every adopter's tree -- ships NO `.claude/`
# directory at all, correctly and by design, and runs the VENDORED copy under
# this plugin family instead. The first version of these two legs read the
# checkout copy UNGUARDED and so hard-failed with FileNotFoundError in every
# adopter tree (26 passed / 2 failed against bundle content vs. 32 / 0 here),
# refusing the born-clone publication gate. That is the same defect
# `worker_hook_shipping_smoke.py:21-31` fixed on 2026-08-10 in this same
# directory, and the fix here follows it: key on the MEASURED tree shape --
# never an env var, which can disagree with what is on disk -- assert against
# the copy that ACTUALLY EXISTS in the measured shape rather than retiring the
# assertion, and make a skip state its reason.
#
# ★★ EVERY line these two legs print NAMES THE COPY IT EXERCISED, on passes as
# well as skips. Without that, a green is ambiguous by construction: the two
# copies are exactly what `reporter_surface` exists to distinguish, so
# "the hook reports the join -- PASS" with no subject cannot tell a reader
# which tree it ran in. The specific failure this closes: the vendored copy
# drifts, the checkout copy is fine, a dev-checkout run stays green, and
# nothing in the output says which copy was ever read.
# ---------------------------------------------------------------------------

_CHECKOUT_HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"
_CHECKOUT_HOOK = _CHECKOUT_HOOKS_DIR / "rotation_due_watch.py"
_VENDORED_HOOK = (
    REPO_ROOT / "plugins" / "github_midwife_plugin" / "claude_plugin"
    / "coordination-hooks" / "hooks" / "rotation_due_watch.py"
)


def _dev_checkout_shape() -> bool:
    """True when this tree carries a `.claude/hooks/` at all.

    ★ Keyed on the DIRECTORY, not on `_CHECKOUT_HOOK` itself, and the
    difference is load-bearing in the direction that matters: if the shape gate
    were the file, then deleting the checkout hook in a dev checkout would make
    every checkout-copy assertion politely classify itself N/A -- the drift
    would silence its own detector. Keyed on the directory, a dev checkout that
    has lost the file is still measured as a dev checkout and the file's
    absence becomes a FAILED assertion, which is what it is.
    """
    return _CHECKOUT_HOOKS_DIR.is_dir()


def _reporter_generation_of(source: str) -> int | None:
    """The `_REPORTER_GENERATION` literal declared in a hook copy, or None.

    Parsed rather than substring-matched so the lockstep check can COMPARE two
    copies instead of asserting one hardcoded value -- see the call site for
    why that distinction is what keeps the guard alive across future bumps.
    """
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("_REPORTER_GENERATION"):
            _, _, value = stripped.partition("=")
            try:
                return int(value.split("#", 1)[0].strip())
            except ValueError:
                return None
    return None


def test_the_hook_actually_sends_the_join() -> None:
    """★ CATCHES: the whole landing being INERT.

    "A config knob that is never passed" is a defect class this repo has paid
    for, and it is the one this landing is most exposed to: every assertion
    above passes against a build where the reporting hook never sends
    `agent_session_id`, because they all supply it themselves. The column
    would exist, the store would persist it, the leg would resolve it, and the
    live table would be entirely NULL.

    So this asserts on the payload the hook BUILDS, with the environment as the
    only input -- and pins both states, because a helper that returns the value
    unconditionally would pass a presence-only check while writing `""` into
    every pre-upgrade row.

    ★ SHAPE-AWARE SUBJECT, not a shape-aware skip. This leg exercises the hook
    copy that ACTUALLY EXISTS in the measured tree: the checkout copy in a dev
    checkout (the one Claude Code runs here, and the one whose payload this
    lane changed), the vendored copy in a born clone (the one that ships, and
    the only one an adopter's tree can run). Going N/A in a born clone would
    have been the easy fix and the wrong one -- it would retire the inert-knob
    guard in precisely the trees where nobody is watching. Prefer moving an
    assertion to where it can still run over retiring it.
    """
    import os
    from types import SimpleNamespace

    surface = "checkout" if _dev_checkout_shape() else "vendored"
    hook_path = _CHECKOUT_HOOK if _dev_checkout_shape() else _VENDORED_HOOK
    # Named on the PASS line, not only on failure: which copy answered is the
    # single fact this leg's green is otherwise silent about.
    _check(
        hook_path.is_file(),
        f"[{surface} copy] the reporting hook exists in this tree shape "
        f"and is the copy exercised below: {hook_path}",
    )
    if not hook_path.is_file():
        return
    source = hook_path.read_text()

    # ★ exec THE SOURCE, never importlib. This exact assertion produced a FALSE
    # RED during the mutation battery: `_REPORTER_GENERATION = 3` -> `= 2` is a
    # SAME-SIZE edit, and CPython validates a cached .pyc on (mtime, size), so
    # restoring the file left a stale bytecode cache that still held the
    # mutated constant. The source was byte-identical to its backup and the
    # test still failed. A mutation battery whose verdicts can be poisoned by
    # its own previous run is worse than no battery: it produces a confident
    # wrong table. Exec'ing the source cannot cache and cannot go stale.
    #
    # Safe to exec: the module's top level is constants and defs, and its
    # `if __name__ == "__main__"` guard does not fire under this namespace.
    # `__file__` is REQUIRED, not decoration: `_reporter_arguments()` resolves
    # the hook's own surface class from `Path(__file__)`, so an exec namespace
    # without it raises NameError the moment anyone asserts on that function.
    # Found by a live probe that omitted it and produced a red which looked
    # like a hook defect and was purely an invocation artifact. Set here so the
    # next assertion added to this test cannot rediscover it the same way.
    namespace: dict[str, Any] = {
        "__name__": "_rotation_due_watch_under_test",
        "__file__": str(hook_path),
    }
    exec(compile(source, str(hook_path), "exec"), namespace)  # noqa: S102
    hook = SimpleNamespace(**namespace)

    previous = os.environ.get("AGENT_SESSION_ID")
    try:
        os.environ["AGENT_SESSION_ID"] = SESSION_ID
        present = hook._session_id_argument()  # noqa: SLF001
        # ★ env -u, not "": the negative control must UNSET the variable it
        # tests. A test process that inherits a real AGENT_SESSION_ID would
        # otherwise pass this branch for the wrong reason.
        os.environ.pop("AGENT_SESSION_ID", None)
        absent = hook._session_id_argument()  # noqa: SLF001
    finally:
        if previous is None:
            os.environ.pop("AGENT_SESSION_ID", None)
        else:
            os.environ["AGENT_SESSION_ID"] = previous

    _check(
        present == {"agent_session_id": SESSION_ID},
        f"[{surface} copy] the hook reports $AGENT_SESSION_ID VERBATIM when it is set",
    )
    _check(
        absent == {},
        f"[{surface} copy] ...and OMITS the key entirely when it is unset -- omission "
        "records NOT REPORTED, whereas '' would assert an empty session id",
    )
    _check(
        hook._REPORTER_GENERATION >= 3,  # noqa: SLF001 - >= so an honest future bump does not red
        f"[{surface} copy] the reporter generation was bumped, so a NULL join can be "
        "attributed to a stale reporter rather than a routing failure",
    )

    source = hook_path.read_text()
    _check(
        "_session_id_argument()" in source.split("def _session_id_argument")[0]
        or "**_session_id_argument()" in source,
        f"[{surface} copy] ...and the helper is actually SPLICED INTO the report payload "
        "-- a helper nothing calls is the inert-knob defect wearing a test",
    )


def test_both_hook_copies_report_the_join() -> None:
    """★ CATCHES: upgrading one copy of the reporting hook and not the other.

    The hook's own comment requires the generation bump "in BOTH repo copies,
    in the same landing". Two copies can be registered on the same event and
    they serialize on a shared throttle marker, so exactly one serves each
    tick and NOTHING in the resulting row says which. Upgrading one copy
    therefore produces a row whose join is present or NULL depending on which
    copy won the tick -- intermittent, unattributable, and far harder to
    diagnose than a clean absence.

    ★ SHAPE-AWARE, and split by what each assertion actually needs. The
    VENDORED copy's own assertions need only the copy that ships, so they run
    UNCONDITIONALLY in every tree shape -- that copy is the one an adopter
    executes, and it is the copy this file previously guarded while leaving the
    other bare. The LOCKSTEP COMPARISON needs two copies to compare, so it is a
    DEV-CHECKOUT-ONLY truth: a born clone has no second subject, and asserting
    agreement there would be asserting the wrong invariant against the very
    tree this smoke is shipped to protect. It goes N/A there, with its reason
    stated -- a skip that names what it skipped and why the tree shape made it
    inapplicable is auditable; a bare N/A is indistinguishable from a skip that
    fired for the wrong reason. It is NOT weakened where it can run: in a dev
    checkout it still binds, still compares extracted values rather than a
    hardcoded literal, and the checkout copy's absence there is now a FAILURE
    rather than a crash.
    """
    vendored = _VENDORED_HOOK
    _check(
        vendored.is_file(),
        f"[vendored copy] the vendored copy of the reporting hook exists -- "
        f"asserted in EVERY tree shape, it is what a born clone runs: {vendored}",
    )
    if not vendored.is_file():
        return
    vendored_source = vendored.read_text()
    _check(
        "_SESSION_ID_ENV" in vendored_source
        and "**_session_id_argument()" in vendored_source,
        f"[vendored copy] the VENDORED copy reports the join too: {vendored}",
    )
    # ★ COMPARE the two, never hardcode the value. An assertion written as
    # `"_REPORTER_GENERATION = 3" in both` enforces lockstep only at 3: the
    # next honest bump to 4 -- correctly applied to BOTH copies -- turns this
    # test red for no reason, and a test that cries wolf on correct work is one
    # a future lane deletes rather than fixes. Comparing the extracted values
    # enforces the invariant that actually matters (they AGREE) at every
    # generation, forever, and needs no maintenance when the value moves.
    vendored_generation = _reporter_generation_of(vendored_source)
    _check(
        vendored_generation is not None and vendored_generation >= 3,
        f"[vendored copy] the SHIPPED copy's generation is at least 3, the generation "
        f"that reports the join (vendored={vendored_generation}) -- asserted in EVERY "
        "tree shape, because in a born clone this is the only copy there is",
    )

    if not _dev_checkout_shape():
        print(
            "  N/A   [born-clone shape] the checkout-vs-vendored LOCKSTEP comparison: "
            f"no .claude/hooks/ in this tree ({_CHECKOUT_HOOKS_DIR}), which is the "
            "CORRECT shape for a born clone and every adopter clone -- there is no "
            "second copy here, so 'both copies agree' has no second subject and the "
            "throttle-marker race it guards cannot occur. The vendored copy's own "
            "assertions above DID run in this tree and still bind.",
        )
        return

    _check(
        _CHECKOUT_HOOK.is_file(),
        f"[dev-checkout shape] the checkout copy exists in a tree that has "
        f"a .claude/hooks/ -- its absence HERE is drift, not a shape: {_CHECKOUT_HOOK}",
    )
    if not _CHECKOUT_HOOK.is_file():
        return
    checkout_generation = _reporter_generation_of(_CHECKOUT_HOOK.read_text())
    _check(
        checkout_generation is not None and checkout_generation == vendored_generation,
        f"[dev-checkout shape] both copies carry the SAME generation "
        f"(checkout={checkout_generation}, vendored={vendored_generation}) -- a split "
        "generation makes a row unattributable to a reporter version, and only one "
        "copy serves each tick",
    )
    _check(
        checkout_generation is not None and checkout_generation >= 3,
        f"[dev-checkout shape] ...and the CHECKOUT copy is at least 3 too "
        f"(checkout={checkout_generation}), the generation that reports the join",
    )


def test_the_process_verb_mapping_does_not_drop_the_field() -> None:
    """★ CATCHES: the field being declared and mapped nowhere -- the last
    wiring joint, and the one a test of the lifecycle function cannot see.

    `AgentMessagingPlugin.report_context_status` does NOT forward its params
    dict. It maps a fixed set of `raw.get(...)` keys into explicit kwargs, so a
    key that is declared in the process metadata but missing from that mapping
    is accepted, ignored, and dropped in silence -- which is exactly how the
    live pre-change verb behaved when this lane sent it an undeclared
    `agent_session_id` and got back `recorded`.

    Every other test in this file supplies the value BELOW that mapping and so
    cannot distinguish a wired mapping from an absent one. This drives the real
    process method with a raw params dict, the shape the platform hands it.
    """
    state = RealShapeState()
    plugin = cast("Any", SimpleNamespace(
        _get_state_service=lambda: cast("StateManagementInterface", state),
    ))
    result = AgentMessagingPlugin.report_context_status(
        plugin,
        {
            "parameters": {
                "agent_instance_id": LEDGER_ID,
                "runtime_session_id": "c-session",
                "provider": "anthropic",
                "runtime": "claude_code",
                "model": "claude-opus-5",
                "effort": "high",
                "current_tokens": 281_507,
                "ceiling": 1_000_000,
                "measured_at": _fresh_stamp(),
                "agent_session_id": SESSION_ID,
            },
        },
        {},
    )
    # `action_status`, not `success`: this plugin's success envelope carries
    # the former and no `success` key at all. Asserting on the wrong key here
    # produced a red beside a green on the SAME call, which is its own useful
    # reminder that an envelope's shape is a fact to check, not to assume.
    _check(
        result.get("action_status") == "completed",
        f"the process verb accepted the report ({result.get('action_status')})",
    )

    view = session_context_status(
        cast("StateManagementInterface", state), agent_instance_id=LEDGER_ID,
    )
    _check(
        view["agent_session_id"] == SESSION_ID,
        "★ the value survives the process verb's raw.get mapping -- without "
        "this assertion the whole landing can be declared, stored, resolved "
        "and still write NULL for every live session",
    )


def _run() -> int:
    # ★ The measured tree shape, stated ONCE at the top of the run, because two
    # of the legs below assert against a different hook copy depending on it.
    # A reader who sees only the per-line "[checkout copy]" / "[vendored copy]"
    # tags can still reconstruct this; printing it here means they do not have
    # to, and a run pasted into a report carries its own tree shape with it.
    shape = (
        f"dev-checkout (.claude/hooks/ present: {_CHECKOUT_HOOKS_DIR})"
        if _dev_checkout_shape()
        else f"born-clone (no .claude/ in this tree: {REPO_ROOT})"
    )
    print(f"Measured tree shape: {shape}")
    tests = [
        test_a_watcher_held_worker_is_reached_through_the_join,
        test_the_join_is_what_does_the_work,
        test_the_join_is_resolved_not_reconstructed,
        test_the_bridge_held_case_still_resolves_on_its_own_key,
        test_an_empty_join_is_not_a_lookup,
        test_two_bindings_for_one_session_id_fails_loud,
        test_the_column_is_declared_and_nullable,
        test_the_fake_enforces_this_tables_declared_schema,
        test_the_verb_round_trips_the_join_and_preserves_null,
        test_the_hook_actually_sends_the_join,
        test_both_hook_copies_report_the_join,
        test_the_process_verb_mapping_does_not_drop_the_field,
    ]
    for test in tests:
        print(f"\n{test.__name__}")
        # ★ A CRASH IS NOT A FAILURE REPORT. An unhandled exception here would
        # abort the run before the summary and hide every later verdict,
        # silently narrowing this battery's own coverage -- which is exactly
        # how a mutation run can report a confident, wrong table. Caught and
        # reported as a named failure instead.
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, never abort the battery
            _check(False, f"{test.__name__} raised {type(exc).__name__}: {exc}")
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(_run())
