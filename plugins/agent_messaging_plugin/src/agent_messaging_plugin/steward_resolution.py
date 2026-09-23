"""Steward-binding resolution for the platform sweep's notify legs (GAU-26).

Every steward-notify leg in :mod:`session_sweep` keys on the LEDGER
``spawned_by_instance_id`` recorded on the worker's row, but this fleet runs
TWO session-id minting schemes at once and a live bridge binding is not
guaranteed to be registered under that id. Resolution therefore has three
legs, in order; they are complementary rather than redundant, which is what
makes the set complete:

1. the peer registry by instance id — a bridge-bound steward, registered under
   its plain ledger id, and the only leg that works for one with no
   ``managed_session`` row of its own;
2. the stable ``agent_session_id`` READ from the steward's ``managed_session``
   row — a watcher-held steward, whose binding keys on a derived watch id;
3. the original ``(agent_id, instance_id)`` detour, kept for a direct-lookup
   miss.

Extracted from ``session_sweep.py`` rather than added to it: that module sits
0.115 MI above the maintainability gate's C boundary at ``d03bfdd98``, so it
had no room left for any addition at all. This module is where the next
steward-resolution change goes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_SESSION_ID,
)
from ananta.llm.agent_messaging.state_results import require_records

from .peer_registry import PeerAmbiguousError, PeerSessionAmbiguousError, PeerUnreachableError
from .schema import TABLE_MANAGED_SESSION

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .models import BridgeBinding
    from .peer_registry import PeerRegistry

logger = logging.getLogger(__name__)


def _managed_session_row(
    state: StateManagementInterface, agent_instance_id: str,
) -> dict[str, Any] | None:
    """The spawner's own ``managed_session`` row, or ``None`` when it has
    none (an operator-launched steward is the normal case here, not a
    fault). ONE read serving both ledger-backed resolution legs below —
    they used to issue the same query twice."""
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": agent_instance_id}},
    )
    rows = require_records(result)
    return rows[0] if rows else None


def managed_session_agent_id(
    state: StateManagementInterface, agent_instance_id: str,
) -> str:
    row = _managed_session_row(state, agent_instance_id)
    return str(row.get("agent_id") or "") if row is not None else ""


def _resolve_steward_via_agent_session_id(
    *,
    peer_registry: PeerRegistry,
    managed_row: dict[str, Any],
) -> BridgeBinding | None:
    """GAU-26: resolve the steward by the STABLE ``agent_session_id`` its own
    ``managed_session`` row records, for the population the instance-id
    lookup cannot see.

    Two session-id minting schemes run at once. A watcher-held session
    registers its live bridge binding under a WATCH id
    (``agi-watch-<sha256(agent_session_id)[:24]>``, ``_resolve_watch_identity``
    in ``local_cli/cli.py``), while every ``spawned_by_instance_id`` recorded
    on a worker row is the steward's LEDGER id. So
    ``resolve_by_agent_instance_id`` keys a ledger id against a table holding
    watch ids and misses by construction — measured live 2026-08-19 19:27:36Z:
    *"spawner agi-73ba7ce5… not resolvable to a live binding (checked the peer
    registry directly and via its managed_session row) — marked overdue,
    steward not notified"*, on a fleet where 7 of 9 live registrations were
    watcher-held. The ``agent_session_id`` is the join that spans the two
    schemes: the ledger row carries it (``backfill_registration`` writes it at
    registration) and ``peer_binding`` carries it too.

    **The key is READ, never COMPUTED.** ``"ases-" + <ledger id>`` is the SPAWN
    path's env injection (``tmux_adapter``/``headless_adapter``), not a join —
    first-party counter-example on this fleet: the operator seat pairs ledger
    ``agi-6be1383613fbd0ec10874571e89956e1`` with session
    ``ases-1786663089-37639-3748``. An implementation that rebuilds the key
    resolves spawned lanes and silently misroutes every steward minted by any
    other launcher: the same defect with its polarity flipped. Recomputing the
    derived watch id on a miss is rejected for the same reason (it hard-codes
    the derivation in a second place). ``test_overdue_join_reads_the_session_id
    _and_never_derives_it`` registers a decoy binding under exactly the value a
    reconstructor would build, so a deriving build misroutes by name.

    Ambiguity (>1 live binding for one session id) is presence, not absence,
    but it is not a delivery target either — this is a best-effort notify path
    that must never raise back into the sweep loop, so it degrades to ``None``
    and the caller's warning fires.
    """
    agent_session_id = str(managed_row.get(COL_AGENT_SESSION_ID) or "")
    if not agent_session_id:
        return None
    try:
        return peer_registry.resolve_by_agent_session_id(agent_session_id)
    except PeerSessionAmbiguousError:
        logger.warning(
            "steward resolution: agent_session_id %s has more than one live "
            "binding; not guessing a delivery target",
            agent_session_id, exc_info=True,
        )
        return None


def _resolve_steward_via_managed_session(
    *,
    peer_registry: PeerRegistry,
    spawner_instance_id: str,
    managed_row: dict[str, Any],
) -> BridgeBinding | None:
    """Fallback path for :func:`_notify_steward_of_overdue` — the ORIGINAL
    (pre-fix) resolution route: look up the spawner's ``agent_id`` via its
    own ``managed_session`` row, then resolve ``(agent_id, instance_id)``.
    Kept for a direct registry-by-instance-id miss; no longer the primary
    path since it silently fails for any spawner with no managed_session row
    of its own (the operator-launched-seat case this fix addresses)."""
    spawner_agent_id = str(managed_row.get("agent_id") or "")
    if not spawner_agent_id:
        return None
    try:
        return peer_registry.resolve(spawner_agent_id, spawner_instance_id)
    except (PeerUnreachableError, PeerAmbiguousError):
        return None


def resolve_steward_binding(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    spawner_instance_id: str,
) -> BridgeBinding | None:
    """Shared resolution step for every steward-notify path below: resolve
    straight from the peer registry by instance id, then — on a miss — through
    the spawner's own ``managed_session`` row, first by the stable
    ``agent_session_id`` it records (GAU-26, see
    :func:`_resolve_steward_via_agent_session_id`) and finally by the original
    ``(agent_id, instance_id)`` detour. See :func:`_notify_steward_of_overdue`
    for why the direct lookup is primary.

    The two ledger-backed legs are complementary rather than redundant, which
    is what makes the pair complete: a bridge-bound steward that has no
    ``managed_session`` row at all is already reachable by the direct lookup;
    a watcher-held steward has the row and is reachable only by the session-id
    join."""
    binding = peer_registry.resolve_by_agent_instance_id(spawner_instance_id)
    if binding is not None:
        return binding
    managed_row = _managed_session_row(state, spawner_instance_id)
    if managed_row is None:
        return None
    binding = _resolve_steward_via_agent_session_id(
        peer_registry=peer_registry, managed_row=managed_row,
    )
    if binding is not None:
        return binding
    return _resolve_steward_via_managed_session(
        peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
        managed_row=managed_row,
    )
