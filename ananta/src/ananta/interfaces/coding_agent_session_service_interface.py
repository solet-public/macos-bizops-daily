"""Coding Agent Session Service Interface — MCP bridge subprocess lifecycle.

Standalone (non-lifecycle) service interface that owns per-coding-agent-tab
MCP bridge subprocess management. Per
``workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md``
§3.6 + §4.4 + §6 Slice 5, the macOS-scope plugin bound to this interface
(``macos_coding_agent_session_plugin``) spawns ``python -m
agent_messaging_plugin.mcp_bridge`` as a child process when the iTerm2
plugin opens a new claude-code / codex tab, terminates the subprocess
when the tab closes, and restarts the subprocess transparently when
``<solet>.bridge.port`` content changes (router blue-green swap).

Eliminates the operator's ``/mcp reconnect`` ceremony by making the
bridge subprocess silently rediscover the router port on every change.

The matching service-interface public-API surface (with
``@service_interface_process`` decorators) lives at
``ananta/src/ananta/services/coding_agent_session_service/interfaces/public.py``
per the D1 two-layer mandate (ABC + decorator surface + KB JSONs).

Verb semantics: all four verbs are **sync**. Spawning and terminating
are sub-second; restart is bounded by the terminate grace window
(5 seconds default). Backgrounding is the caller's concern via
``scheduling_service::execute_in_seconds``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ananta.interfaces.coding_agent_session_result_types import (
    BridgeListResult,
    BridgeRestartResult,
    BridgeSpawnResult,
    BridgeTerminateResult,
)


class CodingAgentSessionServiceInterface(ABC):
    """Per-coding-agent-tab MCP bridge subprocess lifecycle verbs."""

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def spawn_bridge(
        self,
        *,
        agent_instance_id: str,
        solet_name: str,
    ) -> BridgeSpawnResult:
        """Spawn an MCP bridge subprocess for a coding-agent tab.

        Contract:

        - The bridge subprocess is ``python -m
          agent_messaging_plugin.mcp_bridge`` with
          ``SOLET_NAME=<solet_name>`` and
          ``AGENT_IDENTITY=claude_code`` in its environment. The
          plugin records the spawned pid keyed by
          ``agent_instance_id`` so subsequent ``terminate_bridge`` /
          ``restart_bridge`` calls find the right process.
        - Idempotent: if a bridge subprocess is already tracked under
          ``agent_instance_id`` AND that pid is still alive, the verb
          returns ``BridgeSpawnStatus.ALREADY_RUNNING`` with the
          existing pid unchanged.

        Args:
            agent_instance_id: Stable identifier for this coding-agent
                tab; the iTerm2 plugin obtains it from the agent's MCP
                peer-registration handshake (``agent_messaging_plugin``
                peer registry).
            solet_name: Target solet the bridge connects to
                (e.g. ``"example"`` for a locally-run
                solet).

        Returns:
            :class:`BridgeSpawnResult` carrying status, pid, and audit
            metadata.
        """

    @abstractmethod
    def terminate_bridge(self, *, agent_instance_id: str) -> BridgeTerminateResult:
        """Terminate a tracked MCP bridge subprocess.

        Contract:

        - SIGTERM the tracked subprocess; wait up to the configured
          grace window (5 seconds default); SIGKILL if the process is
          still alive at deadline.
        - Idempotent: if no subprocess is tracked under the supplied
          id, returns ``BridgeTerminateStatus.NOT_RUNNING`` without
          error.

        Args:
            agent_instance_id: Tracking key supplied at spawn time.

        Returns:
            :class:`BridgeTerminateResult` carrying status, terminated
            pid, and audit metadata.
        """

    @abstractmethod
    def restart_bridge(self, *, agent_instance_id: str) -> BridgeRestartResult:
        """Terminate + re-spawn a tracked MCP bridge subprocess.

        Contract:

        - Composes ``terminate_bridge`` + ``spawn_bridge`` against the
          same ``agent_instance_id`` and ``solet_name`` the prior
          spawn recorded. The new subprocess preserves the
          ``agent_instance_id`` binding; only the OS pid changes.
        - When no prior bridge is tracked, the verb behaves like a
          fresh spawn (``BridgeRestartStatus.NOT_RUNNING`` with
          ``prior_pid=0``). The FSEvents watcher relies on this
          behaviour to handle the "watcher fires before any tab opened"
          edge case without surfacing an error.

        Args:
            agent_instance_id: Tracking key supplied at spawn time.

        Returns:
            :class:`BridgeRestartResult` carrying prior + new pids and
            audit metadata.
        """

    @abstractmethod
    def list_bridges(self) -> BridgeListResult:
        """Enumerate every tracked MCP bridge subprocess.

        Read-only. Returns one row per tracked ``agent_instance_id``
        with the recorded pid, the live ``alive`` probe (kill -0), and
        the original spawn timestamp. The iTerm2 plugin's
        ``terminate_session`` consults this list to discover which
        ``agent_instance_id`` corresponds to a tab it is closing, and
        the FSEvents watcher iterates this list when ``bridge.port``
        content changes.

        Returns:
            :class:`BridgeListResult` carrying the per-bridge tuple.
        """
