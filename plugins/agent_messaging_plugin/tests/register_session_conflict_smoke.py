#!/usr/bin/env python3
"""R7 — peer_register refuses to bind under a LIVE session's id (WS-2e §4.3.3a).

Day's live find: `watch --role X` derives its identity from whatever
`AGENT_SESSION_ID` it INHERITS. Launched from a live fleet session's shell it
registers a SECOND binding under that session's id — a different instance id and
label, so none of `register`'s existing sweeps fire — and the register route's
self-refresh then re-points EVERY role the victim holds at the newcomer. Nothing
announces. The victim's `holds:true` stays true while naming someone else's
instance.

R7b is the half that keeps this safe. The gate is LIVENESS-gated, not
existence-gated: a subprocess restart always brings a new instance id under the
same session id, so an existence gate would refuse every legitimate succession.
A test suite that only proved "foreign id refused" would pass just as happily
against that far worse behaviour.

Project policy: stdlib-only, no pytest. Run with::

    python3 plugins/agent_messaging_plugin/tests/register_session_conflict_smoke.py
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.bridge_sessions import (  # noqa: E402
    DEFAULT_BINDING_LIVENESS_WINDOW_S,
    BridgeSessionManager,
)
from agent_messaging_plugin.http_routes import (  # noqa: E402
    SESSION_ID_BOUND_TO_LIVE_SESSION,
    _session_id_conflict,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_dispatch import binding_is_live  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

VICTIM_SESSION = "ases-victim-session-0001"
VICTIM_INSTANCE = "agi-mcp-victim"
THIEF_INSTANCE = "agi-watch-thief"

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str, detail: str = "") -> bool:
    global _passed
    if condition:
        _passed += 1
        return True
    _failed.append(f"{label}: {detail}" if detail else label)
    print(f"  FAIL  {_failed[-1]}")
    return False


def _registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _n: "ases-mgr",
        idle_timeout_s=3600,
        max_pending_events=200,
        long_poll_timeout_s=25,
    )


def _incumbent(
    registry: PeerRegistry,
    manager: BridgeSessionManager,
    *,
    idle_seconds: float,
) -> str:
    """Register a victim binding whose bridge last polled `idle_seconds` ago."""
    bridge = manager.open("testhome")
    bridge.last_seen_at = (
        datetime.now(UTC) - timedelta(seconds=idle_seconds)
    ).isoformat()
    registry.register(BridgeBinding(
        bridge_id=bridge.bridge_id,
        agent_id="claude_code",
        agent_instance_id=VICTIM_INSTANCE,
        session_label="Coordinator-Day",
        parent_pid=None,
        agent_session_id=VICTIM_SESSION,
    ))
    return bridge.bridge_id


def case_r7a_live_foreign_session_id_is_refused() -> None:
    """R7a — the theft case. RED before §4.3.3a: the register simply succeeded.

    The uncovered shape is LIVE SESSION + WATCHER, which is why the incumbent
    here is an `agi-mcp-*` instance and the newcomer an `agi-watch-*` one.
    Watcher-vs-watcher under one session id is ALREADY refused earlier, by W1's
    flock (measured) — an ordinary session holds no watch lock, so it is the
    session-plus-watcher pair that reaches this gate, and that is exactly how
    the original accident got through.
    """
    registry, manager = _registry(), _manager()
    _incumbent(registry, manager, idle_seconds=1.0)
    refusal = _session_id_conflict(
        peer_registry=registry,
        bridge_manager=manager,
        agent_session_id=VICTIM_SESSION,
        agent_instance_id=THIEF_INSTANCE,
    )
    if not _check(refusal is not None, "a live foreign session id is REFUSED"):
        return
    assert refusal is not None
    _check(refusal.status_code == 409, "refusal is 409 (state conflict)",
           f"got {refusal.status_code}")
    body = refusal.body.decode()
    _check(SESSION_ID_BOUND_TO_LIVE_SESSION in body, "refusal names the stable code")
    # The refusal has to be actionable: an operator seeing it must be able to
    # tell WHICH session already holds the id without going digging.
    _check("Coordinator-Day" in body, "refusal names the incumbent's LABEL")
    _check(VICTIM_INSTANCE in body, "refusal names the incumbent's INSTANCE")


def case_r7b_stale_incumbent_succeeds() -> None:
    """R7b — THE SAFETY HALF: subprocess succession must stay cheap.

    A restart brings a NEW instance id under the SAME session id with a DEAD
    predecessor. If this refused, every watcher restart and every bridge
    reconnect would fail permanently — catastrophically worse than the theft it
    prevents, and R7a alone would not notice.
    """
    registry, manager = _registry(), _manager()
    _incumbent(
        registry, manager,
        idle_seconds=float(DEFAULT_BINDING_LIVENESS_WINDOW_S + 60),
    )
    _check(
        _session_id_conflict(
            peer_registry=registry,
            bridge_manager=manager,
            agent_session_id=VICTIM_SESSION,
            agent_instance_id=THIEF_INSTANCE,
        ) is None,
        "a STALE incumbent allows succession (new instance, same session id)",
        "an existence-gate would refuse every legitimate restart",
    )


def case_same_instance_re_register_is_allowed() -> None:
    """A watcher re-arm / forwarder reconnect is SELF — replace, never refuse."""
    registry, manager = _registry(), _manager()
    _incumbent(registry, manager, idle_seconds=1.0)
    _check(
        _session_id_conflict(
            peer_registry=registry,
            bridge_manager=manager,
            agent_session_id=VICTIM_SESSION,
            agent_instance_id=VICTIM_INSTANCE,
        ) is None,
        "SAME instance re-registering is allowed (self-replace)",
    )


def case_r7c_fresh_session_id_unaffected() -> None:
    """R7c — the launcher-armed zero-command path must be untouched.

    A fresh launch exports a NEW session id: no incumbent, no refusal. This is
    why requiring an explicit --session-id was rejected as the fix — ambient
    inheritance is CORRECT here, and only wrong when the id is already live.
    """
    registry, manager = _registry(), _manager()
    _incumbent(registry, manager, idle_seconds=1.0)
    _check(
        _session_id_conflict(
            peer_registry=registry,
            bridge_manager=manager,
            agent_session_id="ases-freshly-launched-9999",
            agent_instance_id="agi-watch-fresh",
        ) is None,
        "a fresh session id registers normally (zero-command path intact)",
    )


def case_empty_session_id_is_allowed() -> None:
    """No session id, nothing to conflict with — streamable / older clients."""
    registry, manager = _registry(), _manager()
    _incumbent(registry, manager, idle_seconds=1.0)
    _check(
        _session_id_conflict(
            peer_registry=registry,
            bridge_manager=manager,
            agent_session_id="",
            agent_instance_id=THIEF_INSTANCE,
        ) is None,
        "an empty session id is not gated",
    )



# ---------------------------------------------------------------------------
# R4 — §4.3.2 label sweep moves to claim-settle: a LIVE different-session row
# with the same label SURVIVES register.
# ---------------------------------------------------------------------------

LABEL = "Probe-Dup"


def _labelled(
    registry: PeerRegistry,
    manager: BridgeSessionManager,
    *,
    instance: str,
    session_id: str,
    idle_seconds: float,
    is_live: object = None,
) -> None:
    """Register a binding carrying the shared LABEL, aged by `idle_seconds`."""
    bridge = manager.open("testhome")
    bridge.last_seen_at = (
        datetime.now(UTC) - timedelta(seconds=idle_seconds)
    ).isoformat()
    registry.register(
        BridgeBinding(
            bridge_id=bridge.bridge_id,
            agent_id="claude_code",
            agent_instance_id=instance,
            session_label=LABEL,
            parent_pid=None,
            agent_session_id=session_id,
        ),
        is_live=is_live,  # type: ignore[arg-type]
    )


def _live_predicate(manager: BridgeSessionManager):  # noqa: ANN202 - local closure
    return lambda existing: binding_is_live(
        bridge_manager=manager,
        binding=existing,
        window_seconds=manager.binding_liveness_window_s,
    )


def _rows_for_label(registry: PeerRegistry) -> list[str]:
    return sorted(
        b.agent_instance_id
        for bucket in registry.list_agent_ids().values()
        for b in bucket
        if b.session_label == LABEL
    )


def case_r4_live_same_label_row_survives() -> None:
    """R4 — the oscillator closes: BOTH live rows coexist.

    Day's capture measured two same-label watchers heartbeat-registering and
    hard-deleting each other's registry rows, so role sends sampled an
    oscillating registry — 5 of 6 stranded in queued_for_replay because
    `resolve` found NO ROW. Sparing the live row is what stops the oscillation;
    the ROLE layer, not the registry, decides who holds the role.
    """
    registry, manager = _registry(), _manager()
    live = _live_predicate(manager)
    _labelled(registry, manager, instance="agi-first", session_id="ases-one",
              idle_seconds=1.0, is_live=live)
    _labelled(registry, manager, instance="agi-second", session_id="ases-two",
              idle_seconds=1.0, is_live=live)
    _check(
        _rows_for_label(registry) == ["agi-first", "agi-second"],
        "a LIVE different-session same-label row SURVIVES register",
        f"got {_rows_for_label(registry)}",
    )


def case_r4_dead_same_label_row_is_swept() -> None:
    """THE SAFETY HALF: a DEAD same-label row is still evicted.

    Without this the registry would accumulate every dead predecessor forever
    and `peer_list` would become unreadable — and the 2026-06-09 directive's
    intent (a new session claiming a name displaces the prior holder) would be
    lost rather than merely relocated to claim-settle.
    """
    registry, manager = _registry(), _manager()
    live = _live_predicate(manager)
    _labelled(registry, manager, instance="agi-dead", session_id="ases-dead",
              idle_seconds=float(DEFAULT_BINDING_LIVENESS_WINDOW_S + 60), is_live=live)
    _labelled(registry, manager, instance="agi-fresh", session_id="ases-fresh",
              idle_seconds=1.0, is_live=live)
    _check(
        _rows_for_label(registry) == ["agi-fresh"],
        "a DEAD same-label row is swept (succession, directive preserved)",
        f"got {_rows_for_label(registry)}",
    )


def case_r4_same_session_relabel_still_replaces() -> None:
    """A session re-registering under its own id replaces itself, as before."""
    registry, manager = _registry(), _manager()
    live = _live_predicate(manager)
    _labelled(registry, manager, instance="agi-self-1", session_id="ases-self",
              idle_seconds=1.0, is_live=live)
    _labelled(registry, manager, instance="agi-self-2", session_id="ases-self",
              idle_seconds=1.0, is_live=live)
    _check(
        _rows_for_label(registry) == ["agi-self-2"],
        "same-session re-register replaces its own row",
        f"got {_rows_for_label(registry)}",
    )


def case_r4_unknown_liveness_spares() -> None:
    """Spare-on-unknown (ratified): no predicate -> do not destroy a route.

    A caller with no bridge manager cannot establish liveness. Sparing costs a
    transient duplicate label that peer_list shows and claim-settle resolves;
    sweeping costs a live session its receive path silently. Fail toward not
    destroying a route.
    """
    registry, manager = _registry(), _manager()
    _labelled(registry, manager, instance="agi-a", session_id="ases-a", idle_seconds=1.0)
    _labelled(registry, manager, instance="agi-b", session_id="ases-b", idle_seconds=1.0)
    _check(
        _rows_for_label(registry) == ["agi-a", "agi-b"],
        "unknown liveness SPARES the incumbent row",
        f"got {_rows_for_label(registry)}",
    )


def main() -> int:
    print("agent_messaging — register session-id conflict (WS-2e §4.3.3a, R7)")
    print("=" * 70)
    for case in (
        case_r7a_live_foreign_session_id_is_refused,
        case_r7b_stale_incumbent_succeeds,
        case_same_instance_re_register_is_allowed,
        case_r7c_fresh_session_id_unaffected,
        case_empty_session_id_is_allowed,
        case_r4_live_same_label_row_survives,
        case_r4_dead_same_label_row_is_swept,
        case_r4_same_session_relabel_still_replaces,
        case_r4_unknown_liveness_spares,
    ):
        case()
    print("-" * 70)
    if _failed:
        print(f"{_passed} passed, {len(_failed)} FAILED")
        return 1
    print(f"{_passed} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
