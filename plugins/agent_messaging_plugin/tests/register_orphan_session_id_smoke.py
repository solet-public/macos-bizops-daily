#!/usr/bin/env python3
"""iss_0cc6f884 — an INHERITED session id must not inherit that session's roles.

MEASURED 2026-09-05 00:04Z, re-confirmed 02:11:46Z. A Claude Code pre-warmed
spare (`claude bg-spare`, no pane, no operator, no model turn) started the MCP
bridge, which read `AGENT_SESSION_ID` from its inherited environment — an id
minted for a session that had died on 2026-08-29. It registered, and was handed
that session's durable coordination role. It never called
`peer_claim_role`. The real
operator seat's claim was then refused with `role_held_live`, and 40
role-addressed messages sat unread in a process with no model turn.

THE THREE-STEP CHAIN, none of whose steps is individually wrong:

1. `mcp_bridge._resolve_agent_session_id` reads `AGENT_SESSION_ID`
   unconditionally, so any long-lived parent leaks it to every child that does
   not override (the daemon here; the tmux server carrying Git-Controller's id
   is the same class — iss_c2a66577).
2. `_session_id_conflict` waves a DEAD incumbent through. That is CORRECT: it is
   the subprocess-succession path, and an existence gate would refuse every
   legitimate restart.
3. `refresh_role_binding_cas` re-points EVERY role binding filtered on
   `agent_session_id` ALONE — no claim, no liveness proof, no error.

So the capture is step 3 trusting the key step 1 corrupted, through the door
step 2 must keep open. The fix cannot be at step 2's liveness check, and
`binding_is_live` is not the lever either: the spare's bridge genuinely WAS
polling for its 60-second life (Claude Code's own orphan watchdog reaps an
unclaimed spare after `no client for 60000ms`), so it was live by any honest
definition.

WHAT THIS SUITE PINS. The discriminator is neither liveness nor the host, but
whether the dead incumbent was THIS host process: a bridge resuming its own
session keeps its `parent_pid`; a process that merely inherited the variable
brings its own. `case_killing_*` is RED on master. Every other case is the
safety half — each one passes on master too, and each would go RED against an
over-broad fix. A suite proving only "orphan downgraded" would pass just as
happily against a rule that broke every restart, every operator seat, and every
watcher-held role.

Project policy: stdlib-only, no pytest. Run with::

    python3 plugins/agent_messaging_plugin/tests/register_orphan_session_id_smoke.py
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
    ORPHAN_SESSION_ID_DOWNGRADE,
    _orphan_session_id_downgrade,
    _session_id_conflict,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.role_binding_store import UNCLAIMED_SESSION_ID  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

# The dead 2026-08-29 session whose id the spare inherited.
DEAD_SESSION = "ases-1788003732-64723-13649"
DEAD_INSTANCE = "agi-dead-session-of-2026-08-29"
DEAD_PARENT_PID = 64723

# The spare: its own host process, someone else's session id.
SPARE_INSTANCE = "agi-a7a304b3d127aa4ad463fdbbe4e64949"
SPARE_PARENT_PID = 32470

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
    parent_pid: int | None,
    live: bool = False,
    session_id: str = DEAD_SESSION,
    instance_id: str = DEAD_INSTANCE,
    label: str = "Primary-Seat",
) -> None:
    """Seed the binding the newcomer's inherited session id resolves to.

    Defaults to a DEAD incumbent, because that is the case this gate judges: a
    live one belongs to ``_session_id_conflict``. The bridge is opened on the
    CALLER's manager so liveness is really evaluated rather than accidentally
    unknown.
    """
    bridge = manager.open("testhome")
    idle = 1.0 if live else float(DEFAULT_BINDING_LIVENESS_WINDOW_S + 60)
    bridge.last_seen_at = (
        datetime.now(UTC) - timedelta(seconds=idle)
    ).isoformat()
    registry.register(BridgeBinding(
        bridge_id=bridge.bridge_id,
        agent_id="claude_code",
        agent_instance_id=instance_id,
        session_label=label,
        parent_pid=parent_pid,
        agent_session_id=session_id,
    ))


def case_killing_inherited_id_from_a_foreign_parent_is_downgraded() -> None:
    """THE KILLING TEST — RED on master, where this returns nothing at all.

    The spare's exact measured shape: a different host process registering under
    a dead session's id. Downgraded, so the CAS (which fails closed on an empty
    session id) cannot hand it the role that id owns.
    """
    registry, manager = _registry(), _manager()
    _incumbent(registry, manager, parent_pid=DEAD_PARENT_PID)
    _check(
        _orphan_session_id_downgrade(
            peer_registry=registry,
            bridge_manager=manager,
            agent_session_id=DEAD_SESSION,
            agent_instance_id=SPARE_INSTANCE,
            parent_pid=SPARE_PARENT_PID,
        ),
        "an inherited id from a FOREIGN parent is downgraded",
        "this is the measured capture; unfixed it silently inherits the "
        "coordination role that session id owns",
    )


def case_safety_same_parent_succession_is_untouched() -> None:
    """THE SAFETY HALF THAT MATTERS MOST: a bridge resuming its own session.

    The MCP bridge subprocess restarts under the SAME host process, minting a
    fresh instance id under the same session id — indistinguishable from the
    spare on every axis EXCEPT parent_pid. If this downgraded, every bridge
    reconnect would silently lose its roles, which is strictly worse than the
    defect being fixed and is exactly what a naive orphan rule would do.
    """
    registry, manager = _registry(), _manager()
    _incumbent(registry, manager, parent_pid=DEAD_PARENT_PID)
    _check(
        not _orphan_session_id_downgrade(
            peer_registry=registry,
            bridge_manager=manager,
            agent_session_id=DEAD_SESSION,
            agent_instance_id="agi-restarted-bridge-same-host",
            parent_pid=DEAD_PARENT_PID,
        ),
        "SAME-parent succession is NOT downgraded",
        "a bridge restart must keep its session id and its roles",
    )


def case_safety_same_instance_re_register_is_untouched() -> None:
    """A re-arm / idempotent re-register is SELF, never an adoption."""
    registry, manager = _registry(), _manager()
    _incumbent(registry, manager, parent_pid=DEAD_PARENT_PID)
    _check(
        not _orphan_session_id_downgrade(
            peer_registry=registry,
            bridge_manager=manager,
            agent_session_id=DEAD_SESSION,
            agent_instance_id=DEAD_INSTANCE,
            parent_pid=SPARE_PARENT_PID,
        ),
        "the SAME instance id is never downgraded (re-arm, even if re-parented)",
    )


def case_safety_fresh_session_id_is_untouched() -> None:
    """A first registration has no incumbent, so there is nothing to adopt."""
    registry, manager = _registry(), _manager()
    _check(
        not _orphan_session_id_downgrade(
            peer_registry=registry,
            bridge_manager=manager,
            agent_session_id="ases-brand-new-session",
            agent_instance_id=SPARE_INSTANCE,
            parent_pid=SPARE_PARENT_PID,
        ),
        "an unseen session id is NOT downgraded",
    )


def case_safety_absent_evidence_allows() -> None:
    """Fail OPEN with no parent_pid on either side — and say so out loud.

    A client that sends no `parent_pid` (Streamable HTTP) keeps the old hole.
    That residual is deliberate: this gate can only subtract trust from an
    otherwise-successful registration, so convicting without evidence would
    break real callers to close a hole that needs a contract change instead.
    """
    registry, manager = _registry(), _manager()
    _incumbent(registry, manager, parent_pid=DEAD_PARENT_PID)
    _check(
        not _orphan_session_id_downgrade(
            peer_registry=registry,
            bridge_manager=manager,
            agent_session_id=DEAD_SESSION,
            agent_instance_id=SPARE_INSTANCE,
            parent_pid=None,
        ),
        "a newcomer with NO parent_pid is not downgraded (no evidence)",
    )
    registry_no_incumbent_pid = _registry()
    manager_b = _manager()
    _incumbent(registry_no_incumbent_pid, manager_b, parent_pid=None)
    _check(
        not _orphan_session_id_downgrade(
            peer_registry=registry_no_incumbent_pid,
            bridge_manager=manager_b,
            agent_session_id=DEAD_SESSION,
            agent_instance_id=SPARE_INSTANCE,
            parent_pid=SPARE_PARENT_PID,
        ),
        "an incumbent with NO parent_pid does not convict the newcomer",
    )


def case_safety_empty_and_sentinel_session_ids_allow() -> None:
    """Neither an absent id nor the unclaimed sentinel is an identity."""
    registry, manager = _registry(), _manager()
    for session_id, label in (
        ("", "an empty"),
        (UNCLAIMED_SESSION_ID, "the unclaimed sentinel"),
    ):
        _check(
            not _orphan_session_id_downgrade(
                peer_registry=registry,
                bridge_manager=manager,
                agent_session_id=session_id,
                agent_instance_id=SPARE_INSTANCE,
                parent_pid=SPARE_PARENT_PID,
            ),
            f"{label} session id is not downgraded",
        )


def case_reachability_a_live_session_id_never_reaches_the_cas() -> None:
    """The LIVE-inheritance hypothesis, MEASURED: that path is already closed.

    Asked whether a child spawned TODAY from a LIVE seat's environment inherits
    a LIVE session id, slips past the orphan predicate (which only convicts a
    DEAD incumbent), and still gets every role re-pointed to it by the CAS.

    It cannot, and this pins why. ``refresh_role_binding_cas`` has exactly ONE
    production caller — ``_state_table_self_refresh``, reached only from
    ``peer_register_route`` — and on that one path ``_session_id_conflict`` runs
    FIRST and refuses a live incumbent with 409 before any re-point happens. So
    the live case is refused outright rather than downgraded, and the dead case
    is what the orphan rule is for. The two gates partition the space.

    This case is GREEN on master, deliberately: it asserts a property master
    already has. It is here so that a later change which relaxes the live gate —
    or adds a second CAS caller that skips it — turns this red instead of
    silently re-opening the capture path on the one axis the orphan rule cannot
    see.
    """
    registry, manager = _registry(), _manager()
    _incumbent(
        registry, manager,
        parent_pid=57455,
        live=True,
        session_id="ases-live-seat-session",
        instance_id="agi-live-seat",
    )
    refusal = _session_id_conflict(
        peer_registry=registry,
        bridge_manager=manager,
        agent_session_id="ases-live-seat-session",
        agent_instance_id="agi-child-of-the-live-seat",
    )
    if not _check(
        refusal is not None,
        "a LIVE session id is REFUSED before the CAS can re-point anything",
        "if this goes red, the live-inheritance path is open and the orphan "
        "rule cannot see it",
    ):
        return
    assert refusal is not None
    _check(refusal.status_code == 409, "the live-id refusal is 409",
           f"got {refusal.status_code}")
    # And the orphan rule correctly declines to double-judge the live case: it
    # is not this gate's job, and convicting here would duplicate the 409 with a
    # silent downgrade that hides it.
    _check(
        not _orphan_session_id_downgrade(
            peer_registry=registry,
            bridge_manager=manager,
            agent_session_id="ases-live-seat-session",
            agent_instance_id="agi-child-of-the-live-seat",
            parent_pid=32470,
        ),
        "the orphan rule does not also convict the LIVE case (no double-judge)",
    )


def case_downgrade_token_is_stable() -> None:
    """The response field is a stable token a caller can branch on."""
    _check(
        ORPHAN_SESSION_ID_DOWNGRADE == "orphan_session_id",
        "the downgrade names a stable code",
        f"got {ORPHAN_SESSION_ID_DOWNGRADE!r}",
    )


def main() -> int:
    print("agent_messaging — orphan session id must not inherit roles (iss_0cc6f884)")
    print("=" * 74)
    for case in (
        case_killing_inherited_id_from_a_foreign_parent_is_downgraded,
        case_safety_same_parent_succession_is_untouched,
        case_safety_same_instance_re_register_is_untouched,
        case_safety_fresh_session_id_is_untouched,
        case_safety_absent_evidence_allows,
        case_safety_empty_and_sentinel_session_ids_allow,
        case_reachability_a_live_session_id_never_reaches_the_cas,
        case_downgrade_token_is_stable,
    ):
        case()
    print("-" * 74)
    if _failed:
        print(f"{_passed} passed, {len(_failed)} FAILED")
        return 1
    print(f"{_passed} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
