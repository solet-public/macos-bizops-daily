"""Typed result envelopes for ``CodingAgentSessionServiceInterface`` verbs.

Per workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md
§3.6 + §4.4 + §6 Slice 5: the macOS-scope ``macos_coding_agent_session_plugin``
owns the MCP bridge subprocess lifecycle (spawn / terminate / restart /
list) for every Claude / Codex tab the iTerm2 plugin opens. These DTOs
are this service interface's typed return envelopes, parallel to
:mod:`ananta.interfaces.lifecycle_result_types` for the lifecycle matrix
but scoped to per-session bridge management (not lifecycle).

Frozen dataclasses with slots; StrEnum statuses. Match
``AutostartResult`` shape verbatim per Architect's 2026-06-07 review
(``workbench/2026-06-07_slice5_implementation_readiness.md`` §2.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BridgeSpawnStatus(StrEnum):
    """Terminal status returned by ``spawn_bridge``.

    ``success`` — a new bridge subprocess was started and tracked under
    the supplied ``agent_instance_id``. ``already_running`` — the
    tracker already had a live bridge subprocess for this instance id;
    idempotent return, the existing pid is surfaced unchanged.
    ``failed`` — the spawn could not complete (subprocess.Popen raised,
    env validation failed, runtime dir absent, etc.).
    """

    SUCCESS = "success"
    ALREADY_RUNNING = "already_running"
    FAILED = "failed"


class BridgeTerminateStatus(StrEnum):
    """Terminal status returned by ``terminate_bridge``.

    ``success`` — the tracked bridge subprocess received SIGTERM and
    exited within the grace window (or SIGKILL after). ``not_running``
    — no bridge subprocess was tracked under the supplied
    ``agent_instance_id``; idempotent return. ``failed`` — termination
    surfaced an error the tracker could not recover from.
    """

    SUCCESS = "success"
    NOT_RUNNING = "not_running"
    FAILED = "failed"


class BridgeRestartStatus(StrEnum):
    """Terminal status returned by ``restart_bridge``.

    ``success`` — the prior bridge subprocess was terminated and a
    fresh subprocess spawned in its place under the same
    ``agent_instance_id``. ``not_running`` — no prior bridge was
    tracked; behaves like a fresh ``spawn_bridge`` and returns success
    semantics with ``prior_pid=0``. ``failed`` — either the terminate
    or the re-spawn step surfaced an unrecoverable error.
    """

    SUCCESS = "success"
    NOT_RUNNING = "not_running"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BridgeSpawnResult:
    """Typed envelope returned by ``spawn_bridge``.

    Attributes:
        status: Terminal status of the spawn attempt.
        agent_instance_id: Tracking key the caller supplied; echoed for
            audit correlation.
        solet_name: Target solet the bridge connects to.
        pid: OS pid of the spawned (or already-running) bridge
            subprocess. Zero on ``FAILED`` before any subprocess was
            launched.
        started_at: ISO-8601 UTC timestamp the bridge subprocess was
            spawned at. Empty on ``FAILED``.
        message: Human-readable detail surfaced to the operator and
            captured in audit trails.
    """

    status: BridgeSpawnStatus
    agent_instance_id: str
    solet_name: str
    pid: int
    started_at: str
    message: str


@dataclass(frozen=True, slots=True)
class BridgeTerminateResult:
    """Typed envelope returned by ``terminate_bridge``.

    Attributes:
        status: Terminal status of the terminate attempt.
        agent_instance_id: Tracking key the caller supplied; echoed.
        pid: OS pid of the bridge subprocess that was terminated. Zero
            when ``NOT_RUNNING``.
        terminated_at: ISO-8601 UTC timestamp of the terminate event.
            Empty on ``NOT_RUNNING`` / ``FAILED`` before mutation.
        message: Human-readable detail.
    """

    status: BridgeTerminateStatus
    agent_instance_id: str
    pid: int
    terminated_at: str
    message: str


@dataclass(frozen=True, slots=True)
class BridgeRestartResult:
    """Typed envelope returned by ``restart_bridge``.

    Attributes:
        status: Terminal status of the restart attempt.
        agent_instance_id: Tracking key the caller supplied; echoed.
        prior_pid: OS pid of the bridge subprocess that was terminated.
            Zero when ``NOT_RUNNING`` (no prior bridge was tracked).
        new_pid: OS pid of the freshly-spawned replacement bridge
            subprocess. Zero on ``FAILED`` before spawn fired.
        restarted_at: ISO-8601 UTC timestamp of the restart event.
            Empty on ``FAILED`` before mutation.
        message: Human-readable detail.
    """

    status: BridgeRestartStatus
    agent_instance_id: str
    prior_pid: int
    new_pid: int
    restarted_at: str
    message: str


@dataclass(frozen=True, slots=True)
class BridgeStatus:
    """Per-bridge row returned by ``list_bridges``.

    Attributes:
        agent_instance_id: Tracking key the bridge is registered under.
        solet_name: Target solet the bridge connects to.
        pid: OS pid of the bridge subprocess.
        alive: ``True`` iff the subprocess is currently alive (kill -0
            probe at list time).
        started_at: ISO-8601 UTC timestamp the bridge subprocess was
            originally spawned at.
    """

    agent_instance_id: str
    solet_name: str
    pid: int
    alive: bool
    started_at: str


@dataclass(frozen=True, slots=True)
class BridgeListResult:
    """Typed envelope returned by ``list_bridges``.

    Attributes:
        bridges: Per-bridge rows, one entry per tracked
            ``agent_instance_id``. Empty tuple when no bridges are
            tracked.
        message: Human-readable detail (typically a short count
            summary).
    """

    bridges: tuple[BridgeStatus, ...] = field(default_factory=tuple)
    message: str = ""


__all__ = [
    "BridgeListResult",
    "BridgeRestartResult",
    "BridgeRestartStatus",
    "BridgeSpawnResult",
    "BridgeSpawnStatus",
    "BridgeStatus",
    "BridgeTerminateResult",
    "BridgeTerminateStatus",
]
