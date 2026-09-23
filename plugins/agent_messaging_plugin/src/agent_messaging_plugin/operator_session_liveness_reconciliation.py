"""R3-U1 presence reconciliation for ``managed_session`` rows.

This module deliberately does *not* own a sweep cadence.  It classifies a
caller-provided set of non-terminal ledger rows against the bridge's positive
liveness signal, and can either report the proposed transitions or apply them.
R3's future sweeper consumes this exact primitive for its operator-presence
branch; the public verb is the one-time, dry-run-first operator-row backfill.

The only destructive conclusion is ``dead``: an absent registry binding, a
closed/missing bridge, or a bridge whose valid poll timestamp has passed the
manager's liveness window.  A malformed timestamp or a registry/bridge lookup
fault is ``indeterminate`` and is held.  Thus a live-but-idle operator seat is
untouched by construction: inactivity is not an input to this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from .peer_dispatch import binding_is_live
from .schema import (
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
)
from .session_hosts import OPERATOR_HOST
from .session_lifecycle_store import list_managed_sessions, transition_lifecycle_state

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .bridge_sessions import BridgeSessionManager
    from .peer_registry import PeerRegistry


LivenessClassification = Literal["live", "dead", "indeterminate"]

_NONTERMINAL_STATES = frozenset(
    {
        LIFECYCLE_SPAWNING,
        LIFECYCLE_LIVE,
        LIFECYCLE_IDLE,
        LIFECYCLE_OVERDUE,
        LIFECYCLE_PARKED,
    },
)


@dataclass(frozen=True, slots=True)
class ManagedSessionLivenessClassification:
    """One row's presence conclusion and, when safe, proposed terminal edge."""

    agent_instance_id: str
    lifecycle_state: str
    classification: LivenessClassification
    detail: str
    proposed_to_state: str | None

    def to_payload(self) -> dict[str, str | None]:
        return {
            "agent_instance_id": self.agent_instance_id,
            "lifecycle_state": self.lifecycle_state,
            "classification": self.classification,
            "detail": self.detail,
            "proposed_to_state": self.proposed_to_state,
        }


def classify_managed_session_binding_liveness(
    *,
    row: dict[str, Any],
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    now: datetime | None = None,
) -> ManagedSessionLivenessClassification:
    """Classify one non-terminal ledger row without reading inactivity time.

    ``binding_is_live`` remains the positive truth source.  Its ``False``
    outcome is refined only enough to preserve the R3 hold contract: malformed
    timestamps and lookup faults cannot establish death and therefore hold;
    missing/closed/stale bridges are the established not-live evidence.
    """
    agent_instance_id = str(row.get("agent_instance_id") or "")
    lifecycle_state = str(row.get("lifecycle_state") or "")
    if not agent_instance_id:
        return ManagedSessionLivenessClassification(
            agent_instance_id="",
            lifecycle_state=lifecycle_state,
            classification="indeterminate",
            detail="managed_session row has no agent_instance_id",
            proposed_to_state=None,
        )
    if lifecycle_state not in _NONTERMINAL_STATES:
        return ManagedSessionLivenessClassification(
            agent_instance_id=agent_instance_id,
            lifecycle_state=lifecycle_state,
            classification="indeterminate",
            detail="terminal lifecycle state is outside reconciliation input",
            proposed_to_state=None,
        )
    try:
        binding = peer_registry.resolve_by_agent_instance_id(agent_instance_id)
    except Exception as exc:  # noqa: BLE001 -- query fault is HOLD, never death
        return _indeterminate(agent_instance_id, lifecycle_state, exc)
    if binding is None:
        return _dead(agent_instance_id, lifecycle_state, "no live peer binding")
    return _classify_bound_binding(
        agent_instance_id=agent_instance_id,
        lifecycle_state=lifecycle_state,
        bridge_manager=bridge_manager,
        binding=binding,
        now=now,
    )


def _classify_bound_binding(
    *,
    agent_instance_id: str,
    lifecycle_state: str,
    bridge_manager: BridgeSessionManager,
    binding: Any,
    now: datetime | None,
) -> ManagedSessionLivenessClassification:
    """Return LIVE from the canonical predicate or refine a not-live result."""
    try:
        if binding_is_live(
            bridge_manager=bridge_manager,
            binding=binding,
            window_seconds=bridge_manager.binding_liveness_window_s,
            now=now,
        ):
            return ManagedSessionLivenessClassification(
                agent_instance_id=agent_instance_id,
                lifecycle_state=lifecycle_state,
                classification="live",
                detail="binding_is_live returned true",
                proposed_to_state=None,
            )
        bridge = bridge_manager.get(binding.bridge_id)
    except Exception as exc:  # noqa: BLE001 -- probe fault is HOLD, never death
        return _indeterminate(agent_instance_id, lifecycle_state, exc)
    return _classify_not_live_binding(
        agent_instance_id=agent_instance_id,
        lifecycle_state=lifecycle_state,
        bridge=bridge,
        liveness_window_s=bridge_manager.binding_liveness_window_s,
        now=now,
    )


def _classify_not_live_binding(
    *,
    agent_instance_id: str,
    lifecycle_state: str,
    bridge: Any,
    liveness_window_s: int,
    now: datetime | None,
) -> ManagedSessionLivenessClassification:
    """Differentiate proven absence from malformed, therefore held, evidence."""
    if bridge is None or bridge.closed:
        return _dead(agent_instance_id, lifecycle_state, "binding bridge is missing or closed")
    try:
        last_seen = datetime.fromisoformat(bridge.last_seen_at)
    except (TypeError, ValueError) as exc:
        return _indeterminate(agent_instance_id, lifecycle_state, exc)
    observed_at = last_seen if last_seen.tzinfo is not None else last_seen.replace(tzinfo=UTC)
    clock = now or datetime.now(UTC)
    if (clock - observed_at).total_seconds() > liveness_window_s:
        return _dead(agent_instance_id, lifecycle_state, "binding poll is outside liveness window")
    return _indeterminate(
        agent_instance_id,
        lifecycle_state,
        RuntimeError("binding_is_live returned false without a classifiable cause"),
    )


def reconcile_managed_sessions_against_binding_liveness(
    state: StateManagementInterface,
    *,
    rows: Iterable[dict[str, Any]],
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    dry_run: bool,
    now: datetime | None = None,
) -> dict[str, object]:
    """Classify rows and optionally apply only provably-dead terminal edges.

    The reusable seam accepts rows rather than owning a host or cadence.  Its
    caller decides which row class is eligible; R3s reuses it for the operator
    branch without duplicating reconciliation logic.
    """
    classifications: list[ManagedSessionLivenessClassification] = []
    applied: list[str] = []
    for row in rows:
        result = classify_managed_session_binding_liveness(
            row=row,
            peer_registry=peer_registry,
            bridge_manager=bridge_manager,
            now=now,
        )
        classifications.append(result)
        if dry_run or result.classification != "dead" or result.proposed_to_state is None:
            continue
        transition_lifecycle_state(
            state,
            agent_instance_id=result.agent_instance_id,
            from_state=result.lifecycle_state,
            to_state=result.proposed_to_state,
            directed_by="reconcile:binding_is_live",
            reason=result.detail,
        )
        applied.append(result.agent_instance_id)
    return {
        "dry_run": dry_run,
        "classifications": [result.to_payload() for result in classifications],
        "applied": applied,
    }


def reconcile_operator_session_liveness(
    state: StateManagementInterface,
    *,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    dry_run: bool,
    now: datetime | None = None,
) -> dict[str, object]:
    """R3-U1's one-time operator-row consumer of the shared primitive."""
    rows = (
        row
        for row in list_managed_sessions(state, {"host": OPERATOR_HOST})
        if str(row.get("lifecycle_state") or "") in _NONTERMINAL_STATES
    )
    return reconcile_managed_sessions_against_binding_liveness(
        state,
        rows=rows,
        peer_registry=peer_registry,
        bridge_manager=bridge_manager,
        dry_run=dry_run,
        now=now,
    )


def _dead(
    agent_instance_id: str,
    lifecycle_state: str,
    detail: str,
) -> ManagedSessionLivenessClassification:
    return ManagedSessionLivenessClassification(
        agent_instance_id=agent_instance_id,
        lifecycle_state=lifecycle_state,
        classification="dead",
        detail=detail,
        proposed_to_state=LIFECYCLE_TERMINATED,
    )


def _indeterminate(
    agent_instance_id: str,
    lifecycle_state: str,
    exc: Exception,
) -> ManagedSessionLivenessClassification:
    return ManagedSessionLivenessClassification(
        agent_instance_id=agent_instance_id,
        lifecycle_state=lifecycle_state,
        classification="indeterminate",
        detail=f"{type(exc).__name__}: {exc}",
        proposed_to_state=None,
    )


__all__ = [
    "ManagedSessionLivenessClassification",
    "classify_managed_session_binding_liveness",
    "reconcile_managed_sessions_against_binding_liveness",
    "reconcile_operator_session_liveness",
]
