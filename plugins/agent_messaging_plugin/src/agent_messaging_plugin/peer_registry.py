"""Peer registry backed by the platform :class:`Store` abstraction.

A peer entry maps ``(agent_id, agent_instance_id) -> BridgeBinding``.
The registry is the authoritative routing table for ``peer_send``:
recipients are resolved by ``agent_id`` (with optional
``agent_instance_id`` disambiguation) and either delivered through a
registered ``NativeWakeAdapter`` (e.g., Claude Code's chat-style wake
path) or — when no adapter is registered — surfaced as a long-poll
channel event by the caller.

Backed by an in-memory :class:`Store` over
:func:`get_peer_binding_schema`.  The store owns the lock and the
``created_at`` / ``updated_at`` timestamps; the registry contributes
the routing semantics (cross-bucket sweep on register, single-row
lookup on resolve, group-by-agent_id on list).  The wake-adapter map
is unchanged — it's a transport-adapter table, not a row store, so
it stays as a bespoke dict-with-lock.

Ported from the now-deleted ``agent_channel_plugin`` (2026-05-16) during
the bridge-consolidation work — see
``workbench/2026-05-16_codex_mcp_channel_and_inter_agent_outstanding_work.md``
Phase 2d for the pickup contract.  Migrated to :class:`Store` (Phase 2
of ``workbench/2026-05-19_unified_store_abstraction.md``,
2026-05-19) so ``last_active_at`` semantics fall out of the platform's
``updated_at`` convention.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from ananta.services.store import Store, open_store

from .role_binding_store import UNCLAIMED_SESSION_ID
from .schema import (
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .models import BridgeBinding, NativeWakeAdapter

logger = logging.getLogger(__name__)


def _is_known_agent_session_id(agent_session_id: str) -> bool:
    return bool(agent_session_id) and agent_session_id != UNCLAIMED_SESSION_ID


def _preserve_on_empty_text(
    *,
    incoming: str,
    existing: dict[str, object] | None,
    key: str,
) -> str:
    if incoming or existing is None:
        return incoming
    stored = str(existing.get(key) or "")
    return stored or incoming


def _preserve_on_empty_agent_session_id(
    *,
    incoming: str,
    existing: dict[str, object] | None,
) -> str:
    if _is_known_agent_session_id(incoming) or existing is None:
        return incoming
    stored = str(existing.get("agent_session_id") or "")
    return stored if _is_known_agent_session_id(stored) else incoming


class PeerAmbiguousError(LookupError):
    """Raised when multiple bindings match a peer_id without a hint.

    ``candidate_instance_ids`` lists every ``agent_instance_id`` that
    could have been targeted; ``candidate_session_labels`` is the
    matching parallel list of human-facing labels so the caller can
    render a disambiguation prompt without another registry walk.
    """

    def __init__(
        self,
        peer_agent_id: str,
        candidate_instance_ids: list[str],
        candidate_session_labels: list[str],
    ) -> None:
        self.peer_agent_id = peer_agent_id
        self.candidate_instance_ids = candidate_instance_ids
        self.candidate_session_labels = candidate_session_labels
        rendered = ", ".join(
            f"{inst} (label={label!r})"
            for inst, label in zip(
                candidate_instance_ids, candidate_session_labels, strict=True,
            )
        )
        super().__init__(
            f"peer_ambiguous: multiple {peer_agent_id!r} instances "
            f"registered; pass peer_agent_instance_id to disambiguate. "
            f"Candidates: {rendered}",
        )


class PeerUnreachableError(LookupError):
    """Raised when a peer_id (or specific instance) has no live binding."""

    def response_data(self) -> dict[str, object]:
        """Additional transport-neutral fields for a peer_unreachable result."""
        return {}


class PeerSessionAmbiguousError(PeerUnreachableError):
    """Raised when >1 live binding shares one ``agent_session_id`` (fail-loud dup).

    A stable ``agent_session_id`` identifies exactly one logical session, which
    holds at most one live bridge at a time — so a session-id resolving to
    multiple bindings is a registry-hygiene invariant violation, not a
    disambiguation prompt. The §5.4 handover-notify path catches this (its resolve
    is best-effort and never gates a claim), but the primitive itself fails loud
    so the invariant breach surfaces rather than a silent arbitrary pick.
    """

    def __init__(self, agent_session_id: str, candidate_instance_ids: list[str]) -> None:
        self.agent_session_id = agent_session_id
        self.candidate_instance_ids = candidate_instance_ids
        super().__init__(
            f"peer_session_ambiguous: {len(candidate_instance_ids)} live bindings "
            f"share agent_session_id {agent_session_id!r}: "
            f"{', '.join(candidate_instance_ids)}",
        )

    def response_data(self) -> dict[str, object]:
        """Expose the invariant-breach candidates without changing error code."""
        return {
            "peer_agent_session_id": self.agent_session_id,
            "candidate_instance_ids": self.candidate_instance_ids,
        }


class PeerRegistry:
    """Owns the per-agent_id peer registry + native wake adapters.

    Threading: the store owns the per-row mutation lock, so no
    registry-level lock is needed for bindings.  A separate mutex
    still guards the wake-adapter table (which is a transport-adapter
    map, not a row store) so a wake registration during dispatch
    can't deadlock against an in-flight resolve.
    """

    def __init__(
        self,
        *,
        bindings_store: Store | None = None,
        state_service: object | None = None,
    ) -> None:
        if bindings_store is None:
            if state_service is None:
                raise ValueError(
                    "PeerRegistry requires either bindings_store (tests) or "
                    "state_service (production) to open its persistent backend",
                )
            # Load-order dependency: the ``"postgres"`` factory is registered
            # by whichever vault plugin imports its ``postgres_backend``
            # module first. agent_messaging's ``start_interface`` runs after
            # vault's ``prepare_for_readiness``, so the registration is
            # already in place by the time we get here. A future profile
            # shipping without any vault plugin would surface this gap; see
            # ``workbench/2026-06-01_local_reconnect_ux_design.md`` §4 +
            # the pause-and-ping discussion 2026-06-01.
            bindings_store = open_store(
                get_peer_binding_schema(),
                namespace=PEER_BINDING_NAMESPACE,
                backend="postgres",
                state_service=state_service,
            )
        self._bindings: Store = bindings_store
        self._wake_adapters: dict[str, NativeWakeAdapter] = {}
        self._wake_adapters_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Binding lifecycle
    # ------------------------------------------------------------------

    def register(
        self,
        binding: BridgeBinding,
        *,
        is_live: Callable[[BridgeBinding], bool] | None = None,
    ) -> str:
        """Add or replace ``binding`` under its ``agent_id`` bucket.

        Returns the **effective** ``session_label`` that landed in the
        store, which may differ from ``binding.session_label`` when
        preserve-on-empty restores a previously-stored label (see
        below). Callers (the HTTP ``peer/register`` route, smokes) use
        the return value to echo the persisted state back to the
        client rather than the incoming value.

        Cross-bucket sweep: any existing row sharing this
        ``bridge_id`` OR this ``agent_instance_id`` OR this
        ``session_label`` (when non-empty) is hard-deleted first.
        This preserves the pre-Store semantics where a relabel or
        manual reassignment of a live bridge — a row that moved to
        a different ``agent_id`` bucket — gets cleaned up cleanly,
        AND ensures the **single-active-session-per-name** invariant
        (operator directive 2026-06-09): when a new session claims a
        name via ``/rename`` (which fires ``peer_register`` with the
        new session_label), the previous holder of that label is
        evicted from the registry. Without the session_label sweep
        the prior holder's row would persist across the rename
        because both ``bridge_id`` and ``agent_instance_id`` are
        new on the claiming session.  Then insert the fresh row.

        Preserve-on-empty (2026-06-01 §4.2, extended 2026-07-26):
        if the incoming ``binding.session_label`` or
        ``binding.agent_session_id`` is empty and a stored row for this
        ``agent_instance_id`` already carries a non-empty value, the
        stored value flows into the new row. The auto-reconnect path's
        ``_register_identity`` may send the subprocess's stale empty
        cache; the operator's last ``/rename`` and the bridge's known
        logical-session id are the authority. A non-empty incoming value
        overwrites (explicit beats implicit; matches address_book
        most-recent-claim semantics).

        The read + two deletes + one insert are not atomic at the
        store level. Two threads racing to register the same bridge
        can interleave; this matches the prior in-memory implementation
        under fast reconnect storms, and is acceptable at our scale.
        A future iteration can add a store-level ``replace_by``
        primitive if profiling shows it.
        """
        existing = self._bindings.read_one(
            {"agent_instance_id": binding.agent_instance_id},
        )
        effective_label = _preserve_on_empty_text(
            incoming=binding.session_label,
            existing=existing,
            key="session_label",
        )
        effective_agent_session_id = _preserve_on_empty_agent_session_id(
            incoming=binding.agent_session_id,
            existing=existing,
        )
        self._bindings.delete(
            {"bridge_id": binding.bridge_id}, soft_delete=False,
        )
        self._bindings.delete(
            {"agent_instance_id": binding.agent_instance_id},
            soft_delete=False,
        )
        # Single-active-session-per-name — WS-2e §4.3.2 moves the EVICTION to
        # claim-settle time. Here the label row is swept only when it is the
        # SAME session or DEAD by the liveness window; a LIVE different-session
        # row SURVIVES. The registry tolerates a duplicate label transiently
        # (peer_list shows both, so the operator can SEE it) and the ROLE layer
        # decides who holds the role.
        #
        # Measured rationale (Day's capture, 2026-08-01): two same-label
        # watchers heartbeat-registering hard-deleted each other's rows, so
        # role sends sampled an oscillating registry — 5 of 6 stranded in
        # queued_for_replay because resolve found NO ROW. Sparing the live row
        # closes the oscillator: both rows coexist, resolve always finds the
        # holder.
        #
        # `is_live` UNKNOWN (None) spares a different-session row rather than
        # sweeping it. That direction is deliberate: the cost of sparing is a
        # transient duplicate label the design already tolerates, while the
        # cost of sweeping is silently destroying a live session's receive
        # path — which is the defect this change exists to remove.
        if effective_label:
            self._sweep_label_row(
                effective_label,
                claimant_agent_session_id=effective_agent_session_id,
                is_live=is_live,
            )
        self._bindings.insert(
            {
                "agent_id": binding.agent_id,
                "agent_instance_id": binding.agent_instance_id,
                "bridge_id": binding.bridge_id,
                "session_label": effective_label,
                "parent_pid": binding.parent_pid,
                "agent_session_id": effective_agent_session_id,
                # codex-watch-migration wake_capable design (2026-08-06):
                # re-declared on EVERY register() call, no preserve-on-empty —
                # a bool has no "empty" state, so (unlike session_label above)
                # the incoming value always wins outright.
                "wake_capable": binding.wake_capable,
                "watcher_declared": binding.watcher_declared,
            },
        )
        logger.info(
            "peer registered: %s/%s (label=%r, parent_pid=%s) -> bridge %s",
            binding.agent_id,
            binding.agent_instance_id,
            effective_label,
            binding.parent_pid,
            binding.bridge_id,
        )
        return effective_label

    def unregister(self, bridge_id: str) -> int:
        """Remove every binding owned by ``bridge_id``; return count removed."""
        removed = self._bindings.delete(
            {"bridge_id": bridge_id}, soft_delete=False,
        )
        if removed:
            logger.info(
                "peer unregistered: bridge %s gone (%d binding(s))",
                bridge_id, removed,
            )
        return removed

    def touch_binding(self, agent_instance_id: str) -> int:
        """Bump ``updated_at`` on the binding for ``agent_instance_id``.

        Called from the dispatch path (``peer_send``,
        ``peer_inbox``, native-wake delivery) so ``updated_at``
        carries "last active" semantics that the canonical platform
        timestamp convention already wires through every query.
        Returns the count of rows touched (0 if no binding matches).
        """
        return self._bindings.touch({"agent_instance_id": agent_instance_id})

    def stamp_model_activity(self, agent_instance_id: str, stamp: str) -> int:
        """Mirror ``last_model_activity_at`` onto the binding for ``agent_instance_id``.

        REL-05: the durable half of the in-memory ``BridgeSessionState`` stamp,
        so the server-side sweep can read model-activity liveness without the
        live session. Distinct from ``touch_binding`` (which is ``updated_at``,
        bumped by every dispatch/inbox/register); this is the model-activity-only
        signal that the consumption reconciler and the deaf-wake census key on.
        Returns the count of rows updated (0 if no binding matches).
        """
        return self._bindings.update(
            {"agent_instance_id": agent_instance_id},
            {"last_model_activity_at": stamp},
        )

    def list_agent_ids(self) -> dict[str, list[BridgeBinding]]:
        """Snapshot the registry keyed by ``agent_id`` for peer_list responses."""
        rows = self._bindings.read()
        grouped: dict[str, list[BridgeBinding]] = {}
        for row in rows:
            agent_id = str(row["agent_id"])
            grouped.setdefault(agent_id, []).append(_binding_from_row(row))
        return {
            agent_id: sorted(
                bindings,
                key=lambda b: b.agent_instance_id,
            )
            for agent_id, bindings in grouped.items()
        }

    def list_by_bridge(self, bridge_id: str) -> list[BridgeBinding]:
        """Snapshot every binding owned by ``bridge_id``.

        Per the 2026-06-13 coding-agent-inference design v4 §4: callers
        that need the per-bridge binding set BEFORE the bridge closes
        (e.g. to clean up a per-binding sidecar inside the same
        ``close_bridge`` route handler) snapshot through this helper
        rather than scanning ``list_agent_ids`` linearly.
        """
        rows = self._bindings.read({"bridge_id": bridge_id})
        return [_binding_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(
        self,
        peer_id: str,
        peer_agent_instance_id: str | None = None,
    ) -> BridgeBinding:
        """Pick the recipient ``BridgeBinding`` for a ``peer_send`` call.

        Three rules:

        1. ``peer_agent_instance_id`` supplied → return that exact
           binding; raise ``PeerUnreachableError`` if no match.
        2. Exactly one binding under ``peer_id`` → return it.
        3. Multiple bindings → raise ``PeerAmbiguousError`` carrying
           the candidate ``agent_instance_id`` and ``session_label``
           lists so the caller can prompt for disambiguation.
        """
        if peer_agent_instance_id is not None:
            row = self._bindings.read_one(
                {
                    "agent_id": peer_id,
                    "agent_instance_id": peer_agent_instance_id,
                },
            )
            if row is None:
                raise PeerUnreachableError(
                    f"peer_unreachable: no binding for "
                    f"{peer_id!r}/{peer_agent_instance_id!r}",
                )
            return _binding_from_row(row)
        rows = self._bindings.read({"agent_id": peer_id})
        if not rows:
            raise PeerUnreachableError(
                f"peer agent_id {peer_id!r} is not currently "
                f"registered (no live bindings)",
            )
        if len(rows) == 1:
            return _binding_from_row(rows[0])
        candidates = sorted(
            (_binding_from_row(r) for r in rows),
            key=lambda b: b.agent_instance_id,
        )
        raise PeerAmbiguousError(
            peer_agent_id=peer_id,
            candidate_instance_ids=[c.agent_instance_id for c in candidates],
            candidate_session_labels=[c.session_label for c in candidates],
        )


    def _sweep_label_row(
        self,
        effective_label: str,
        *,
        claimant_agent_session_id: str,
        is_live: Callable[[BridgeBinding], bool] | None,
    ) -> None:
        """Evict a same-label row only when it is SELF or DEAD (§4.3.2)."""
        rows = self._bindings.read({"session_label": effective_label})
        for row in rows:
            existing = _binding_from_row(row)
            same_session = bool(
                claimant_agent_session_id
                and existing.agent_session_id == claimant_agent_session_id
            )
            # A row with no session id cannot be proven live and cannot be a
            # different SESSION in any meaningful sense — sweep it as before so
            # pre-S1 clients keep today's replace semantics.
            unattributed = not existing.agent_session_id
            if (
                not (same_session or unattributed)
                and (is_live is None or is_live(existing))
            ):
                continue  # LIVE (or unprovable) different session -> SPARE
            self._bindings.delete(
                {"agent_instance_id": existing.agent_instance_id},
                soft_delete=False,
            )

    def resolve_by_agent_instance_id(
        self, agent_instance_id: str,
    ) -> BridgeBinding | None:
        """Reverse-lookup the live binding for a durable ``agent_instance_id``
        alone, with no ``agent_id`` required up front.

        ``agent_instance_id`` is ``unique=True`` on this table (schema.py), so
        this is a direct single-row lookup, not a scan — unlike
        :meth:`resolve`, which requires the caller to already know the
        ``agent_id`` to disambiguate. Used by callers that only have a
        recorded instance id (e.g. a ``managed_session`` row's
        ``spawned_by_instance_id``) and no reliable ``agent_id`` source for
        it — a session spawned by an operator-launched seat with no
        ``managed_session`` row of its own has exactly this shape. Returns
        ``None`` for an empty id or no match (best-effort: nothing to notify).
        """
        if not agent_instance_id:
            return None
        row = self._bindings.read_one({"agent_instance_id": agent_instance_id})
        return _binding_from_row(row) if row is not None else None

    def resolve_by_agent_session_id(
        self, agent_session_id: str,
    ) -> BridgeBinding | None:
        """Reverse-lookup the live binding for a stable ``agent_session_id``.

        The §5.4 handover-notify path uses this to route a displaced-holder notice
        to the prior holder's CURRENT bridge (found via its stable session id)
        rather than the stale ``agent_instance_id`` recorded on the role binding
        — which has rotated by the time a reconnect displaces it. Returns ``None``
        for an empty session id or no match (best-effort: nothing to notify);
        raises :class:`PeerSessionAmbiguousError` on >1 match (fail-loud dup — a
        session holds at most one live bridge).
        """
        if not agent_session_id:
            return None
        rows = self._bindings.read({"agent_session_id": agent_session_id})
        if not rows:
            return None
        if len(rows) > 1:
            raise PeerSessionAmbiguousError(
                agent_session_id,
                [str(row.get("agent_instance_id") or "") for row in rows],
            )
        return _binding_from_row(rows[0])

    def agent_session_id_for_instance(self, agent_instance_id: str) -> str:
        """The stable ``agent_session_id`` bound to ``agent_instance_id`` (or "").

        The claim path sources the claimant's session id from its OWN live
        ``peer_binding`` row here (REL-07 finding 1) — NEVER from claim args, which
        never carry it — so the written role binding gets a non-empty
        ``agent_session_id`` and the reconnect CAS (keyed on it alone) can re-point
        the role. Empty when the instance is not registered (the caller falls back
        to leaving the column empty — no worse than the pre-fix state).
        """
        if not agent_instance_id:
            return ""
        row = self._bindings.read_one({"agent_instance_id": agent_instance_id})
        return str(row.get("agent_session_id") or "") if row is not None else ""

    # ------------------------------------------------------------------
    # Native wake adapters
    # ------------------------------------------------------------------

    def register_native_wake_adapter(
        self, agent_id: str, adapter: NativeWakeAdapter,
    ) -> None:
        """Store ``adapter`` keyed by ``agent_id``; re-register replaces silently.

        Adapters are typically singleton per agent kind (e.g., the
        plugin self-registers a ``claude_code`` adapter when it boots
        its IO surface), so last-write-wins is the right semantics.
        """
        with self._wake_adapters_lock:
            self._wake_adapters[agent_id] = adapter
        logger.info(
            "native wake adapter registered for agent_id=%r", agent_id,
        )

    def wake_adapter_for(self, agent_id: str) -> NativeWakeAdapter | None:
        """Return the registered ``NativeWakeAdapter`` for ``agent_id`` or None."""
        with self._wake_adapters_lock:
            return self._wake_adapters.get(agent_id)


def _binding_from_row(row: dict[str, object]) -> BridgeBinding:
    """Build a typed :class:`BridgeBinding` view over one store row."""
    from .models import BridgeBinding  # noqa: PLC0415 — break import cycle
    parent_pid_raw = row.get("parent_pid")
    parent_pid = (
        int(parent_pid_raw)
        if isinstance(parent_pid_raw, int)
        else None
    )
    return BridgeBinding(
        bridge_id=str(row["bridge_id"]),
        agent_id=str(row["agent_id"]),
        agent_instance_id=str(row["agent_instance_id"]),
        session_label=str(row.get("session_label") or ""),
        parent_pid=parent_pid,
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
        agent_session_id=str(row.get("agent_session_id") or ""),
        wake_capable=bool(row.get("wake_capable", True)),
        watcher_declared=bool(row.get("watcher_declared", False)),
    )


__all__ = [
    "PeerAmbiguousError",
    "PeerRegistry",
    "PeerSessionAmbiguousError",
    "PeerUnreachableError",
]
