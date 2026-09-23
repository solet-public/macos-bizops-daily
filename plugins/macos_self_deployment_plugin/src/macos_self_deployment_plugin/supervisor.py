"""Colour-agnostic launchd crash-supervisor for blue-green solet (Option B).

Under the prior model the LaunchAgent ran ``ananta.cli`` *directly*, so the
launchd-managed process WAS a solet colour. That had two defects: (1) a
blue-green cutover drains+SIGTERMs the launchd-managed colour, and launchd's
``KeepAlive`` ghost-respawned it (the F2 Choice Y respawn-suppression relied
on a graceful SIGTERM→exit-0 handler that did not exist until Q1); (2) after
a clean cutover the active colour is an unsupervised non-launchd sidecar and
the launchd job is dormant — nothing restarts a dead active colour between
cutovers.

Option B replaces the direct-launch with THIS module. The LaunchAgent runs
``<current>/venv/bin/python3 -m macos_self_deployment_plugin.supervisor
--app-home <profile>``: a thin, colour-agnostic supervisor that

* spawns the active solet from ``current`` on cold-start,
* re-spawns it after a crash,
* and survives blue-green cutovers untouched.

Two structural consequences:

* **The ghost class is impossible.** No solet colour is ever launchd-managed,
  so a drained/SIGTERM'd colour is never respawned by launchd. Q1's graceful
  SIGTERM→teardown is kept for clean-shutdown hygiene (pool close), but its
  *respawn-suppression* role is subsumed here — the supervisor decides spawns
  by the router's active-colour state, not by any exit code.
* **Liveness is poll-based, never ``waitpid`` on the active colour.** The
  swap spawns green as a sidecar (not the supervisor's child), so the
  supervisor could never ``waitpid`` it. Instead it polls the router and
  spawns a replacement **only when the router reports no active colour**
  (``active_instance_id is None``). The router's own ``_heartbeat_gc`` clears
  a dead active binding within ~heartbeat-timeout; a supervisor-spawned
  replacement then self-activates (``heartbeat_lifecycle._ensure_active_color``
  claims active iff the router has none). During a cutover the router always
  shows a fresh active colour, so the supervisor correctly no-ops — it is
  purely additive and never kills.

The persistent drain sentinel (``stop_self``) still gates respawn: while
``is_draining`` is true the supervisor suppresses spawning, so "stop and stay
stopped" holds. (Clearing the sentinel lets the next poll respawn — a cleaner
resume than the old cold-restart requirement.)

Deliberately lightweight: imports only stdlib + ``constants`` + ``router_client``
+ ``child_spawn`` + the core runtime-dir / drain-sentinel seams. It must NOT
drag in the plugin-class graph (the package ``__init__`` is lazy for exactly
this reason) — this is the process keeping the solet alive.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Any, Final

from ananta.core.runtime import get_runtime_dir, is_draining

from macos_self_deployment_plugin.child_spawn import spawn_solet_child
from macos_self_deployment_plugin.constants import (
    DEFAULT_SUPERVISOR_POLL_INTERVAL_SECONDS,
    DEFAULT_SUPERVISOR_SPAWN_BACKOFF_BASE_SECONDS,
    DEFAULT_SUPERVISOR_SPAWN_BACKOFF_CAP_SECONDS,
    ENV_SOLET_NAME,
    ENV_SOLET_RELEASE_ID,
    PLUGIN_NAME,
    ROUTER_SOCKET_SUFFIX,
)
from macos_self_deployment_plugin.release_manager import (
    CURRENT_LINK_NAME,
    RELEASES_ROOT_DEFAULT,
    VENV_DIRNAME,
)
from macos_self_deployment_plugin.router_client import RouterClient, RouterClientError

_LOG_TAG: Final[str] = f"{PLUGIN_NAME}.supervisor"
_PYTHON3_BASENAME: Final[str] = "python3"
_VENV_BIN_DIRNAME: Final[str] = "bin"
# The router's ``status`` wire key the supervisor keys liveness on. It is the
# PROVEN key — ``heartbeat_lifecycle._ensure_active_color`` reads the same one
# (the platform demonstrably boots reading it) — and it is binding-derived:
# ``RouterState.status`` sets ``active_color`` non-None iff ``active_instance_id``
# is set AND that binding still exists, so it is exactly "is there a live active
# colour" with no extra check (verified against the live router wire shape).
_ACTIVE_COLOR_KEY: Final[str] = "active_color"


class TickOutcome(StrEnum):
    """The action a single supervisor poll took — observable for tests/logs."""

    HEALTHY = "healthy"          # router reports a live active colour; no-op
    SPAWNED = "spawned"          # spawned a replacement solet from ``current``
    PENDING = "pending"          # a prior spawn is still booting toward active
    ROUTER_DOWN = "router_down"  # router unreachable; wait (don't blind-spawn)
    DRAINING = "draining"        # operator stop_self in effect; respawn suppressed
    BACKOFF = "backoff"          # crash-loop backoff after a failed boot; waiting
    SPAWN_FAILED = "spawn_failed"  # spawn seam RAISED (missing interpreter/Popen); backing off


@dataclass(frozen=True, slots=True)
class SupervisorSeams:
    """Injectable I/O + clock seams (production defaults; smoke fakes).

    Bundled into one object so the :class:`Supervisor` instance-attribute
    count stays well under the god-class threshold and so a test can drive
    the decision logic with zero real sockets, processes, or sleeps.
    """

    router_status: Callable[[], dict[str, Any] | None]
    spawn: Callable[[], subprocess.Popen[bytes]]
    is_draining: Callable[[], bool]
    sleep: Callable[[float], None]
    monotonic: Callable[[], float]


class Supervisor:
    """Poll-based, never-kills crash-supervisor for the active solet colour."""

    def __init__(
        self,
        *,
        solet_name: str,
        app_home: Path,
        releases_root: Path,
        seams: SupervisorSeams,
        poll_interval_seconds: float = DEFAULT_SUPERVISOR_POLL_INTERVAL_SECONDS,
        backoff_base_seconds: float = DEFAULT_SUPERVISOR_SPAWN_BACKOFF_BASE_SECONDS,
        backoff_cap_seconds: float = DEFAULT_SUPERVISOR_SPAWN_BACKOFF_CAP_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._solet_name = solet_name
        self._app_home = app_home
        self._releases_root = releases_root
        self._seams = seams
        self._poll_interval = poll_interval_seconds
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._logger = logger or logging.getLogger(PLUGIN_NAME)
        self._pending: subprocess.Popen[bytes] | None = None
        self._children: list[subprocess.Popen[bytes]] = []
        self._boot_failures = 0
        self._last_spawn_at = float("-inf")
        self._stop = False

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        """Poll → decide → sleep, until SIGTERM/SIGINT. Never dies on a tick."""
        self._install_signal_handlers()
        self._logger.info(
            "%s: starting (solet=%s releases_root=%s poll=%.1fs)",
            _LOG_TAG, self._solet_name, self._releases_root, self._poll_interval,
        )
        while not self._stop:
            try:
                outcome = self.tick()
                self._logger.debug("%s: tick → %s", _LOG_TAG, outcome.value)
            except Exception:  # noqa: BLE001 — never let one bad tick kill supervision
                self._logger.exception("%s: tick error (continuing)", _LOG_TAG)
            self._seams.sleep(self._poll_interval)
        self._logger.info("%s: stopping (signal received)", _LOG_TAG)

    def tick(self) -> TickOutcome:
        """One poll cycle. Reaps zombies, then spawns iff no active colour."""
        self._reap_children()
        snapshot = self._seams.router_status()
        if snapshot is None:
            return TickOutcome.ROUTER_DOWN
        if self._active_present(snapshot):
            self._on_healthy()
            return TickOutcome.HEALTHY
        return self._maybe_spawn()

    # ------------------------------------------------------------------
    # Decision helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _active_present(snapshot: dict[str, Any]) -> bool:
        """A live active colour exists iff the router names one (``active_color``).

        The router's ``_heartbeat_gc`` clears a dead active binding (and
        ``active_color`` is recomputed from the surviving bindings), so a
        present ``active_color`` IS a live active colour — no cross-process
        heartbeat math (the router's clock is authoritative).
        """
        return bool(snapshot.get(_ACTIVE_COLOR_KEY))

    def _on_healthy(self) -> None:
        if self._pending is not None:
            self._logger.info("%s: active colour healthy; clearing pending spawn", _LOG_TAG)
        self._pending = None
        self._boot_failures = 0

    def _maybe_spawn(self) -> TickOutcome:
        # A spawn already in flight toward active — let it boot, never double.
        if self._pending is not None and self._pending.poll() is None:
            return TickOutcome.PENDING
        # Operator stop_self: respawn stays suppressed while the sentinel persists.
        if self._seams.is_draining():
            return TickOutcome.DRAINING
        # Crash-loop guard: a broken ``current`` degrades to slow-retry.
        if not self._backoff_elapsed():
            return TickOutcome.BACKOFF
        return self._spawn()

    def _spawn(self) -> TickOutcome:
        # Stamp the attempt time + count it BEFORE invoking the seam so a seam
        # that RAISES (missing ``current`` interpreter, ``Popen`` failure) still
        # arms the exponential backoff. Otherwise the failure escaped to
        # ``run_forever`` and the next poll re-attempted immediately — a broken
        # ``current`` retried every poll interval instead of backing off
        # (Codex MINOR-1). The success path is unchanged (both fields were set
        # within this same synchronous call before).
        self._last_spawn_at = self._seams.monotonic()
        self._boot_failures += 1
        try:
            proc = self._seams.spawn()
        except Exception:  # noqa: BLE001 — a failed spawn must back off, not crash-loop the poll
            self._pending = None
            self._logger.exception(
                "%s: spawn from current FAILED (consecutive_boot_attempts=%d); "
                "backing off", _LOG_TAG, self._boot_failures,
            )
            return TickOutcome.SPAWN_FAILED
        self._pending = proc
        self._children.append(proc)
        self._logger.info(
            "%s: spawned solet from current pid=%s (consecutive_boot_attempts=%d)",
            _LOG_TAG, proc.pid, self._boot_failures,
        )
        return TickOutcome.SPAWNED

    def _backoff_seconds(self) -> float:
        if self._boot_failures == 0:
            return 0.0
        return min(self._backoff_base * (2 ** (self._boot_failures - 1)), self._backoff_cap)

    def _backoff_elapsed(self) -> bool:
        return (self._seams.monotonic() - self._last_spawn_at) >= self._backoff_seconds()

    def _reap_children(self) -> None:
        """Poll every tracked child so exited ones are reaped (no zombies)."""
        self._children = [proc for proc in self._children if proc.poll() is None]

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                # Not the main thread (e.g. a test harness) — skip; the run
                # loop's ``_stop`` is still settable by other means.
                pass

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        del frame
        self._logger.info("%s: signal %d → stopping run loop", _LOG_TAG, signum)
        self._stop = True


# ----------------------------------------------------------------------
# Production wiring (the seams the smoke replaces with fakes)
# ----------------------------------------------------------------------


def _resolve_current_interpreter(releases_root: Path) -> str:
    """The literal ``<releases_root>/current/venv/bin/python3`` symlink path.

    Emitted literally (not resolved): the OS resolves ``current`` at exec
    time, so a cutover/rollback flip is picked up by the next spawn with no
    bookkeeping. A dangling ``current`` fails loud at exec — never a silent
    wrong-tree boot.
    """
    return str(
        releases_root / CURRENT_LINK_NAME / VENV_DIRNAME / _VENV_BIN_DIRNAME / _PYTHON3_BASENAME
    )


def _resolve_current_release_id(releases_root: Path) -> str:
    """Best-effort ``rel-<id>`` for the audit env field; ``""`` if unreadable."""
    try:
        target = os.readlink(releases_root / CURRENT_LINK_NAME)
    except OSError:
        return ""
    return Path(target).name


def _make_spawn(
    *, solet_name: str, app_home: Path, releases_root: Path
) -> Callable[[], subprocess.Popen[bytes]]:
    """Production spawn seam: launch the ``current`` release; the child self-defaults.

    Sets neither ``SOLET_COLOR`` nor ``SOLET_INSTANCE_ID`` — the solet
    defaults colour=blue and mints its own instance-id (mirroring the
    historical direct-launch cold-start), so the child self-activates whenever
    the router has no active colour. Colour ⊥ release: a blue replacement
    running the ``current`` release's code is correct even when the crashed
    colour was green.
    """

    def _spawn() -> subprocess.Popen[bytes]:
        log_path = (
            app_home / "data" / "logs" / f"supervisor_spawn_{uuid.uuid4().hex[:8]}.log"
        )
        return spawn_solet_child(
            interpreter=_resolve_current_interpreter(releases_root),
            app_home=app_home,
            solet_name=solet_name,
            log_path=log_path,
            extra_env={ENV_SOLET_RELEASE_ID: _resolve_current_release_id(releases_root)},
        )

    return _spawn


def _make_router_status(solet_name: str) -> Callable[[], dict[str, Any] | None]:
    """Production liveness seam: router ``status`` over the mgmt unix socket."""
    socket_path = get_runtime_dir(solet_name) / f"{solet_name}{ROUTER_SOCKET_SUFFIX}"
    client = RouterClient(socket_path)

    def _status() -> dict[str, Any] | None:
        try:
            return client.status()
        except RouterClientError:
            return None

    return _status


def build_supervisor(
    *,
    solet_name: str,
    app_home: Path,
    releases_root: Path | None = None,
    logger: logging.Logger | None = None,
) -> Supervisor:
    """Wire a :class:`Supervisor` with production seams."""
    root = (
        releases_root
        if releases_root is not None
        else Path(RELEASES_ROOT_DEFAULT).expanduser() / solet_name
    )
    seams = SupervisorSeams(
        router_status=_make_router_status(solet_name),
        spawn=_make_spawn(solet_name=solet_name, app_home=app_home, releases_root=root),
        is_draining=lambda: is_draining(solet_name),
        sleep=time.sleep,
        monotonic=time.monotonic,
    )
    return Supervisor(
        solet_name=solet_name,
        app_home=app_home,
        releases_root=root,
        seams=seams,
        logger=logger,
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"{PLUGIN_NAME}.supervisor",
        description="Colour-agnostic launchd crash-supervisor for blue-green solet.",
    )
    parser.add_argument("--app-home", required=True, type=Path)
    parser.add_argument("--solet-name", default=os.environ.get(ENV_SOLET_NAME, ""))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    name = str(args.solet_name).strip()
    if not name:
        print(
            f"{_LOG_TAG}: {ENV_SOLET_NAME} unset and --solet-name not given",
            file=sys.stderr,
        )
        return 2
    supervisor = build_supervisor(solet_name=name, app_home=args.app_home)
    supervisor.run_forever()
    return 0


__all__ = ["Supervisor", "SupervisorSeams", "TickOutcome", "build_supervisor", "main"]


if __name__ == "__main__":
    sys.exit(main())
