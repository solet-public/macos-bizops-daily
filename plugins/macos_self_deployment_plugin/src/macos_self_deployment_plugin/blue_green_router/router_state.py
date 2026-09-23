"""In-memory routing state for the local blue-green router.

Implements the §2 contract from the L3-implementation-plan design
record (dev-checkout workbench — not part of the shipped tree):

- Session-id affinity per D21' Option α (operator-confirmed
  2026-06-02): an Mcp-Session-Id stays bound to the color that
  initialized it until that color's drain window expires.
- Stateless across router restarts per D23' Option I: nothing on
  disk; passive reconciliation via heartbeats (router.py).
- Drain-window semantics on activate: new sessions hit the
  newly-activated color; existing session-bound traffic continues
  against the prior color until drain expiry.

Slice 3 of the bridge-port-routing-and-session-lifecycle design record
(dev-checkout workbench — not part of the shipped tree) eliminated the
hardcoded blue/green port bands per invariant I1.A.
``register()`` no longer validates the incoming port against a range —
the spawn-path guarantee from invariant I2 (Slices 2 + 2.5) prevents
rogue registrations because every spawn path either registers
correctly within budget or self-SIGTERMs.

All mutation is synchronous + single-threaded; the router runs one
asyncio loop and serializes mutations through `RouterState`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

DEFAULT_DRAIN_WINDOW_SECONDS: Final[int] = 30
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS: Final[int] = 30


@dataclass
class ColorBinding:
    """A registered solet child currently routable by the router."""

    color: str
    port: int
    instance_id: str
    registered_at: float
    last_heartbeat: float
    # BLG-04: the color's OWN ephemeral streamable-HTTP listener port.
    # Delivered via an optional `register()` arg — sticky-preserved across
    # re-registers that omit it (see `RouterState.register`), since the
    # child binds its main port first and typically learns its streamable
    # port slightly later. `None` until delivered, or if this color never
    # runs with streamable enabled; the streamable proxy 503s a color with
    # no reported port rather than guessing.
    streamable_port: int | None = None


@dataclass
class DrainEntry:
    """A formerly-active binding still inside its drain window.

    Bound sessions continue routing here until `drain_ends_at`; after
    that, the entry is swept and bound sessions get a 404 on next
    request.
    """

    binding: ColorBinding
    drain_ends_at: float


@dataclass
class RegisterResult:
    accepted: bool
    reason: str | None = None


@dataclass
class ActivateResult:
    activated: bool
    previous_color: str | None = None
    drain_window_seconds: int = 0
    reason: str | None = None


@dataclass
class RollbackResult:
    rolled_back: bool
    active_color: str | None = None
    reason: str | None = None


@dataclass
class HeartbeatResult:
    alive: bool
    unknown_instance: bool = False


@dataclass
class StatusEntry:
    color: str
    port: int
    instance_id: str
    status: str
    last_heartbeat: float
    streamable_port: int | None = None


@dataclass
class StatusSnapshot:
    colors: list[StatusEntry]
    active_color: str | None
    active_instance_id: str | None
    router_started_at: float
    drain_entries: list[StatusEntry] = field(default_factory=list)


class RouterState:
    """Authoritative in-memory routing table.

    The class is intentionally a plain object — no asyncio
    primitives; the caller (router.py) holds the event loop and
    serializes all calls into a single coroutine.
    """

    def __init__(
        self,
        drain_window_seconds: int = DEFAULT_DRAIN_WINDOW_SECONDS,
        heartbeat_timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.drain_window_seconds = drain_window_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._clock = clock or time.time
        self.bindings: dict[str, ColorBinding] = {}
        self.active_instance_id: str | None = None
        self.drain_entries: list[DrainEntry] = []
        self.session_to_instance: dict[str, str] = {}
        self.started_at = self._clock()

    def now(self) -> float:
        return self._clock()

    def register(
        self,
        port: int,
        color: str,
        instance_id: str,
        streamable_port: int | None = None,
    ) -> RegisterResult:
        if color not in ("blue", "green"):
            return RegisterResult(False, reason="unknown_color")
        existing = self.bindings.get(instance_id)
        if existing is not None and (
            existing.port != port or existing.color != color
        ):
            return RegisterResult(False, reason="instance_id_collision")
        ts = self.now()
        self.bindings[instance_id] = ColorBinding(
            color=color,
            port=port,
            instance_id=instance_id,
            registered_at=existing.registered_at if existing else ts,
            last_heartbeat=ts,
            # Sticky: a re-register that doesn't carry a streamable_port
            # (the common case — only the first delivery or a fresh instance
            # passes one) preserves whatever was already known instead of
            # wiping it back to None.
            streamable_port=(
                streamable_port if streamable_port is not None
                else (existing.streamable_port if existing else None)
            ),
        )
        return RegisterResult(True)

    def unregister(self, instance_id: str) -> bool:
        binding = self.bindings.pop(instance_id, None)
        if binding is None:
            return False
        if self.active_instance_id == instance_id:
            self.active_instance_id = None
        self.drain_entries = [
            d for d in self.drain_entries if d.binding.instance_id != instance_id
        ]
        return True

    def heartbeat(self, instance_id: str) -> HeartbeatResult:
        binding = self.bindings.get(instance_id)
        if binding is None:
            return HeartbeatResult(alive=False, unknown_instance=True)
        binding.last_heartbeat = self.now()
        return HeartbeatResult(alive=True)

    def activate(self, color: str, instance_id: str) -> ActivateResult:
        binding = self.bindings.get(instance_id)
        if binding is None:
            return ActivateResult(False, reason="instance_not_registered")
        if binding.color != color:
            return ActivateResult(False, reason="color_mismatch")

        prev_instance_id = self.active_instance_id
        prev_color: str | None = None
        if prev_instance_id is not None and prev_instance_id != instance_id:
            prev_binding = self.bindings.get(prev_instance_id)
            if prev_binding is not None:
                prev_color = prev_binding.color
                self.drain_entries.append(
                    DrainEntry(
                        binding=prev_binding,
                        drain_ends_at=self.now() + self.drain_window_seconds,
                    )
                )

        self.active_instance_id = instance_id
        return ActivateResult(
            activated=True,
            previous_color=prev_color,
            drain_window_seconds=self.drain_window_seconds,
        )

    def rollback(self, color: str, instance_id: str) -> RollbackResult:
        for entry in self.drain_entries:
            if (
                entry.binding.color == color
                and entry.binding.instance_id == instance_id
                and entry.drain_ends_at > self.now()
            ):
                self.active_instance_id = entry.binding.instance_id
                self.drain_entries = [
                    d for d in self.drain_entries if d is not entry
                ]
                return RollbackResult(
                    rolled_back=True, active_color=color
                )
        return RollbackResult(
            rolled_back=False, reason="no_drain_window_active"
        )

    def resolve_route(
        self, mcp_session_id: str | None
    ) -> ColorBinding | None:
        if mcp_session_id is not None:
            bound_instance = self.session_to_instance.get(mcp_session_id)
            if bound_instance is not None:
                binding = self._lookup_routable_binding(bound_instance)
                if binding is not None:
                    return binding
        if self.active_instance_id is not None:
            return self.bindings.get(self.active_instance_id)
        return None

    def _lookup_routable_binding(self, instance_id: str) -> ColorBinding | None:
        binding = self.bindings.get(instance_id)
        if binding is None:
            return None
        if self.active_instance_id == instance_id:
            return binding
        for entry in self.drain_entries:
            if entry.binding.instance_id == instance_id and entry.drain_ends_at > self.now():
                return binding
        return None

    def record_session(self, mcp_session_id: str, instance_id: str) -> None:
        if instance_id in self.bindings:
            self.session_to_instance[mcp_session_id] = instance_id

    def sweep_expired_drain(self) -> list[DrainEntry]:
        now = self.now()
        expired = [e for e in self.drain_entries if e.drain_ends_at <= now]
        if not expired:
            return []
        self.drain_entries = [
            e for e in self.drain_entries if e.drain_ends_at > now
        ]
        expired_instance_ids = {e.binding.instance_id for e in expired}
        self.session_to_instance = {
            sid: iid
            for sid, iid in self.session_to_instance.items()
            if iid not in expired_instance_ids
        }
        return expired

    def status(self) -> StatusSnapshot:
        active_color: str | None = None
        if self.active_instance_id is not None:
            active = self.bindings.get(self.active_instance_id)
            if active is not None:
                active_color = active.color
        entries = [
            StatusEntry(
                color=b.color,
                port=b.port,
                instance_id=b.instance_id,
                status="active" if b.instance_id == self.active_instance_id else "inactive",
                last_heartbeat=b.last_heartbeat,
                streamable_port=b.streamable_port,
            )
            for b in self.bindings.values()
        ]
        drain = [
            StatusEntry(
                color=d.binding.color,
                port=d.binding.port,
                instance_id=d.binding.instance_id,
                status="draining",
                last_heartbeat=d.binding.last_heartbeat,
                streamable_port=d.binding.streamable_port,
            )
            for d in self.drain_entries
            if d.drain_ends_at > self.now()
        ]
        return StatusSnapshot(
            colors=entries,
            active_color=active_color,
            active_instance_id=self.active_instance_id,
            router_started_at=self.started_at,
            drain_entries=drain,
        )
