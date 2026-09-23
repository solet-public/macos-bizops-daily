"""Bridge subprocess registry keyed by ``agent_instance_id``.

Owns the four primitives this plugin exposes through its service
interface: ``spawn``, ``terminate``, ``restart``, ``list``. Holds the
``agent_instance_id → TrackedBridge`` map in process memory; the
``macos_coding_agent_session_plugin`` plugin class is a thin facade
that converts platform-side arguments into these calls.

No persistence across plugin restarts — per D-W-SLICE-5-2 the iTerm2
plugin re-registers bridges on solet restart, so the tracker is
ephemeral by design.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from macos_coding_agent_session_plugin.constants import (
    DEFAULT_AGENT_IDENTITY,
    DEFAULT_TERMINATE_GRACE_SECONDS,
    DEFAULT_TERMINATE_POLL_INTERVAL_SECONDS,
    ENV_AGENT_IDENTITY,
    ENV_AGENT_INSTANCE_ID,
    ENV_SOLET_NAME,
)


@dataclass(frozen=True, slots=True)
class TrackedBridge:
    """One row in the tracker's registry."""

    agent_instance_id: str
    solet_name: str
    pid: int
    started_at: str


SpawnFn = Callable[[str, str], subprocess.Popen[Any]]


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _pid_alive(pid: int) -> bool:
    """Return ``True`` iff ``pid`` is alive (kill -0 probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def default_spawn(agent_instance_id: str, solet_name: str) -> subprocess.Popen[Any]:
    """Production spawn: launch ``python -m agent_messaging_plugin.mcp_bridge``.

    The child inherits the parent's PATH + PYTHONPATH so the same
    ananta editable install reaches it. ``SOLET_NAME`` +
    ``AGENT_IDENTITY`` are the env keys the bridge subprocess
    requires per the bridge overview KB article. agent_instance_id is
    passed through ``AGENT_INSTANCE_ID`` for stable registry
    identity across bridge reconnects.
    """
    env = os.environ.copy()
    env[ENV_SOLET_NAME] = solet_name
    env.setdefault(ENV_AGENT_IDENTITY, DEFAULT_AGENT_IDENTITY)
    env[ENV_AGENT_INSTANCE_ID] = agent_instance_id
    return subprocess.Popen(
        [sys.executable, "-m", "agent_messaging_plugin.mcp_bridge"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


class BridgeTracker:
    """Thread-safe registry of MCP bridge subprocesses."""

    def __init__(
        self,
        logger: logging.Logger,
        spawn_fn: SpawnFn = default_spawn,
        grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
        poll_interval_seconds: float = DEFAULT_TERMINATE_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._logger = logger
        self._spawn_fn = spawn_fn
        self._grace_seconds = grace_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._lock = RLock()
        self._registry: dict[str, TrackedBridge] = {}
        # Keep Popen handles separate from the (frozen) registry rows so
        # terminate can ``wait()`` after kill and reap the zombie. Without
        # this, the OS process table accumulates defunct entries until the
        # plugin shuts down.
        self._handles: dict[str, subprocess.Popen[Any]] = {}

    # ------------------------------------------------------------------
    # Primitive operations (one per service-interface verb)
    # ------------------------------------------------------------------

    def spawn(
        self,
        *,
        agent_instance_id: str,
        solet_name: str,
    ) -> tuple[str, TrackedBridge | None, str]:
        """Spawn a new bridge subprocess and register it.

        Returns ``(status, tracked, message)`` where ``status`` is one
        of ``"success" / "already_running" / "failed"``. ``tracked`` is
        the registry row when status is success or already_running.
        """
        if not agent_instance_id or not solet_name:
            return ("failed", None, "spawn requires agent_instance_id + solet_name")
        with self._lock:
            existing = self._registry.get(agent_instance_id)
            if existing is not None and _pid_alive(existing.pid):
                return (
                    "already_running",
                    existing,
                    f"bridge already tracked under {agent_instance_id} (pid={existing.pid})",
                )
            try:
                proc = self._spawn_fn(agent_instance_id, solet_name)
            except OSError as exc:
                self._logger.exception(
                    "spawn failed for agent_instance_id=%s", agent_instance_id,
                )
                return ("failed", None, f"subprocess.Popen raised: {exc}")
            row = TrackedBridge(
                agent_instance_id=agent_instance_id,
                solet_name=solet_name,
                pid=proc.pid,
                started_at=_utc_now_iso(),
            )
            self._registry[agent_instance_id] = row
            self._handles[agent_instance_id] = proc
            return ("success", row, f"bridge spawned (pid={row.pid})")

    def terminate(
        self,
        *,
        agent_instance_id: str,
    ) -> tuple[str, int, str]:
        """Terminate the tracked bridge.

        Returns ``(status, pid, message)``.
        """
        with self._lock:
            row = self._registry.pop(agent_instance_id, None)
            handle = self._handles.pop(agent_instance_id, None)
        if row is None:
            return ("not_running", 0, f"no bridge tracked under {agent_instance_id}")
        return self._sigterm_then_kill(row.pid, handle)

    def restart(
        self,
        *,
        agent_instance_id: str,
    ) -> tuple[str, int, TrackedBridge | None, str]:
        """Terminate + re-spawn under the same id.

        Returns ``(status, prior_pid, fresh_row, message)``. When no
        prior bridge is tracked the status is ``"not_running"`` and the
        caller surfaces it without raising — the FSEvents watcher
        relies on this idempotent semantic. The re-spawn step uses the
        prior row's ``solet_name``; without a prior row there is
        no solet to preserve and the verb returns
        ``(not_running, 0, None, ...)``.
        """
        with self._lock:
            row = self._registry.pop(agent_instance_id, None)
            handle = self._handles.pop(agent_instance_id, None)
        if row is None:
            return ("not_running", 0, None, f"no bridge tracked under {agent_instance_id}")
        terminate_status, _, terminate_msg = self._sigterm_then_kill(row.pid, handle)
        if terminate_status == "failed":
            with self._lock:
                self._registry[agent_instance_id] = row
                if handle is not None:
                    self._handles[agent_instance_id] = handle
            return ("failed", row.pid, None, f"terminate step failed: {terminate_msg}")
        spawn_status, fresh_row, spawn_msg = self.spawn(
            agent_instance_id=agent_instance_id, solet_name=row.solet_name,
        )
        if spawn_status == "failed":
            return ("failed", row.pid, None, f"spawn step failed: {spawn_msg}")
        return ("success", row.pid, fresh_row, "bridge restarted")

    def list_bridges(self) -> tuple[TrackedBridge, ...]:
        """Snapshot the current registry."""
        with self._lock:
            return tuple(self._registry.values())

    def tracked_ids(self) -> tuple[str, ...]:
        """Snapshot the current registry's keys (for watcher iteration)."""
        with self._lock:
            return tuple(self._registry.keys())

    def is_alive(self, pid: int) -> bool:
        """Public wrapper for unit-test introspection."""
        return _pid_alive(pid)

    def shutdown(self) -> None:
        """Terminate every tracked bridge (plugin shutdown path)."""
        with self._lock:
            rows = list(self._registry.values())
            handles = dict(self._handles)
            self._registry.clear()
            self._handles.clear()
        for row in rows:
            self._sigterm_then_kill(row.pid, handles.get(row.agent_instance_id))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sigterm_then_kill(
        self, pid: int, handle: subprocess.Popen[Any] | None = None,
    ) -> tuple[str, int, str]:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return ("success", pid, f"pid={pid} already gone")
        except PermissionError as exc:
            self._logger.error("SIGTERM denied on pid=%d: %s", pid, exc)
            return ("failed", pid, f"sigterm denied: {exc}")
        deadline = time.monotonic() + self._grace_seconds
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                self._reap(handle)
                return ("success", pid, f"pid={pid} terminated cleanly")
            time.sleep(self._poll_interval_seconds)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            self._reap(handle)
            return ("success", pid, f"pid={pid} terminated at grace boundary")
        except PermissionError as exc:
            return ("failed", pid, f"sigkill denied: {exc}")
        self._reap(handle)
        return ("success", pid, f"pid={pid} sigkilled after grace")

    @staticmethod
    def _reap(handle: subprocess.Popen[Any] | None) -> None:
        """Call ``Popen.wait()`` so the OS reaps the zombie process entry."""
        if handle is None:
            return
        try:
            handle.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            return
        except OSError:
            return


def restart_each(
    tracker: BridgeTracker,
    ids: Iterable[str],
    logger: logging.Logger,
) -> None:
    """Helper: restart each tracked bridge; logs but does not raise.

    Used by the FSEvents watcher when the canonical bridge port file
    changes content. Best-effort — one failed restart does not stop
    the other restarts.
    """
    for agent_instance_id in ids:
        try:
            status, _, _, message = tracker.restart(agent_instance_id=agent_instance_id)
        except Exception:  # noqa: BLE001 — watcher path must keep firing
            logger.exception(
                "restart_each: restart raised for id=%s", agent_instance_id,
            )
            continue
        logger.info(
            "restart_each: id=%s status=%s message=%s",
            agent_instance_id, status, message,
        )
