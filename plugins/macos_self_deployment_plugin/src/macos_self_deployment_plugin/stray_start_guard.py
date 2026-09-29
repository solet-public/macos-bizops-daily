"""Cold-start guard: a colourless process must not become a second live colour.

``launchctl load`` of the primary LaunchAgent while the blue-green router already
has a healthy active colour starts a second ``ananta.cli``.  The plist sets no
``SOLET_COLOR``, so the plugin used to default that process to blue: it
registered as an inactive colour that never drained, and -- before it ever looked
at the router -- its cold-start scrub deleted the live active instance's socket
and port files.

This guard runs first in ``prepare_for_readiness``, before any scrub, and decides
from the router's own status whether the active colour is really live:

* an explicit ``SOLET_COLOR`` is a swap candidate the orchestrator spawned on
  purpose -- it always proceeds;
* no router answering is a genuine cold boot -- it proceeds (the readiness wait
  that follows fails loudly if the router never comes up);
* no active colour is a cold boot or a crash relaunch -- it proceeds;
* an active colour whose heartbeat keeps advancing is live -- the caller exits 0,
  so ``KeepAlive.SuccessfulExit=false`` does not relaunch it.

A crash relaunch can see the dead instance's binding for up to the router's
heartbeat-GC timeout, so "an active colour exists" alone is not evidence of a live
one.  The heartbeat is: a live instance beats every ``DEFAULT_HEARTBEAT_INTERVAL_SECONDS``
and a dead one's ``last_heartbeat`` freezes until GC clears the binding.  Both
values come from the router's own clock, so they are only ever compared with each
other, never with this process's clock.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, NoReturn

from macos_self_deployment_plugin import stale_runtime_cleanup
from macos_self_deployment_plugin.blue_green_router.router_state import (
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
)
from macos_self_deployment_plugin.constants import PLUGIN_NAME

_LOG_TAG: Final[str] = f"{PLUGIN_NAME}.stray_start_guard"

# Margin past the router's heartbeat-GC timeout, so a dead active binding has been
# cleared by the time the window ends.  The window is an upper bound only: a live
# active colour is recognised on its first observed heartbeat advance.
_GC_MARGIN_SECONDS: Final[float] = 5.0
STRAY_GUARD_WINDOW_SECONDS: float = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS + _GC_MARGIN_SECONDS
STRAY_GUARD_POLL_SECONDS: float = 1.0


class StartVerdict(StrEnum):
    """Why a cold start may, or may not, go on to claim a colour."""

    PROCEED_EXPLICIT_COLOR = "proceed_explicit_color"
    PROCEED_NO_ROUTER = "proceed_no_router"
    PROCEED_NO_ACTIVE_COLOR = "proceed_no_active_color"
    DECLINE_LIVE_ACTIVE_COLOR = "decline_live_active_color"


class StrayGuardIndeterminateError(RuntimeError):
    """The router's active binding neither beat nor cleared inside the window."""


def _active_heartbeat(snapshot: Mapping[str, Any], instance_id: str) -> float:
    """The router-clock ``last_heartbeat`` of the active instance's colour entry."""
    for entry in snapshot["colors"]:
        if entry["instance_id"] == instance_id:
            return float(entry["last_heartbeat"])
    msg = (
        f"{_LOG_TAG}: router status names active_instance_id={instance_id!r} "
        f"but lists no colour entry for it: {sorted(snapshot)}"
    )
    raise StrayGuardIndeterminateError(msg)


def _active_instance_id(snapshot: Mapping[str, Any]) -> str | None:
    if not snapshot.get("active_color"):
        return None
    instance_id = snapshot.get("active_instance_id")
    return str(instance_id) if instance_id else None


def _observe(
    snapshot: Mapping[str, Any], baseline: tuple[str, float] | None,
) -> tuple[StartVerdict | None, tuple[str, float] | None]:
    """One router observation: a verdict once it is decisive, and the next baseline.

    The first sighting of an active instance only records its heartbeat; a later
    sighting of the SAME instance with a larger heartbeat proves it is alive.  A new
    active instance (a swap landed mid-wait) restarts the comparison.
    """
    instance_id = _active_instance_id(snapshot)
    if instance_id is None:
        return StartVerdict.PROCEED_NO_ACTIVE_COLOR, None
    beat = _active_heartbeat(snapshot, instance_id)
    if baseline is None or baseline[0] != instance_id:
        return None, (instance_id, beat)
    if beat > baseline[1]:
        return StartVerdict.DECLINE_LIVE_ACTIVE_COLOR, baseline
    return None, baseline


def _raise_indeterminate(snapshot: Mapping[str, Any], window: float) -> NoReturn:
    msg = (
        f"{_LOG_TAG}: active instance {_active_instance_id(snapshot)!r} kept its binding "
        f"for {window:.0f}s without a heartbeat advance or a router GC clear; "
        "cannot tell a live colour from a dead one"
    )
    raise StrayGuardIndeterminateError(msg)


def evaluate_cold_start(
    *,
    explicit_color: bool,
    status_probe: Callable[[], dict[str, object] | None],
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    window_seconds: float | None = None,
    poll_seconds: float | None = None,
) -> StartVerdict:
    """Decide whether a cold start may proceed, watching the active heartbeat.

    ``status_probe`` returns the router's ``status`` payload, or ``None`` when no
    router answers.  Raises :class:`StrayGuardIndeterminateError` when the active
    binding is still present, with a frozen heartbeat, at the end of the window --
    an unclassifiable router state is an error, not a guess in either direction.
    """
    if explicit_color:
        return StartVerdict.PROCEED_EXPLICIT_COLOR
    window = STRAY_GUARD_WINDOW_SECONDS if window_seconds is None else window_seconds
    poll = STRAY_GUARD_POLL_SECONDS if poll_seconds is None else poll_seconds
    deadline = monotonic() + window
    baseline: tuple[str, float] | None = None
    while True:
        snapshot = status_probe()
        if snapshot is None:
            return StartVerdict.PROCEED_NO_ROUTER
        verdict, baseline = _observe(snapshot, baseline)
        if verdict is not None:
            return verdict
        if monotonic() >= deadline:
            _raise_indeterminate(snapshot, window)
        sleep(poll)


def _roster_is_serving(orchestrator_ref: object | None) -> bool:
    """True once the platform is dispatching actions -- i.e. this is not a boot.

    ``prepare_for_readiness`` also runs for a staged plugin re-install inside a
    live platform, where the active colour IS this process.  The guard is a boot
    check and must never fire there.
    """
    plugin_manager = getattr(orchestrator_ref, "plugin_manager", None)
    return bool(getattr(plugin_manager, "_is_serving", False))


def decline_if_stray_start(
    *,
    solet_name: str,
    socket_path: Path,
    explicit_color: bool,
    orchestrator_ref: object | None,
    logger: logging.Logger,
) -> None:
    """Exit 0 when this boot would be a second process beside a live active colour.

    ``SystemExit`` derives from ``BaseException``, so the startup sequence's
    ``except Exception`` does not convert it into a failed boot: it reaches
    ``sys.exit`` in the CLI with code 0 and launchd's ``SuccessfulExit=false``
    leaves it stopped.  No lifecycle service has started yet -- every plugin's
    ``prepare_for_readiness`` runs before any ``start_services`` -- so there is
    no worker thread to strand.
    """
    if _roster_is_serving(orchestrator_ref):
        return
    verdict = evaluate_cold_start(
        explicit_color=explicit_color,
        status_probe=lambda: stale_runtime_cleanup.router_mgmt_status(socket_path),
    )
    if verdict is not StartVerdict.DECLINE_LIVE_ACTIVE_COLOR:
        logger.debug("%s: solet=%s verdict=%s", _LOG_TAG, solet_name, verdict.value)
        return
    logger.warning(
        "%s: refusing to start a second instance of solet %r: the router already has "
        "a live active colour (its heartbeat is advancing) and SOLET_COLOR is unset, "
        "so this process would register as an inactive colour that never drains. "
        "Exiting 0 so KeepAlive.SuccessfulExit=false does not relaunch it. "
        "To replace the running colour use a blue-green swap (apply_manifest), not "
        "launchctl load.",
        _LOG_TAG,
        solet_name,
    )
    raise SystemExit(0)
