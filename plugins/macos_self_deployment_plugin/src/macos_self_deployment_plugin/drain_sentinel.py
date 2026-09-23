"""Crash-supervisor coordination sentinel for macos_self_deployment_plugin.

Slice 4 of
``workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md``
(Layer 2 second half — drain sentinel for crash-supervisor coordination)
introduces a single on-disk sentinel file ``~/.ananta/runtime/<name>.draining``
that the LaunchAgent's ``PathState`` predicate gates on. While the
sentinel exists, the ``KeepAlive`` predicate evaluates false; launchd
suppresses respawn. The sentinel is held only for the SIGTERM + unregister
window of an intentional drain — so a solet child that genuinely crashes
independently still triggers a launchd respawn.

Architect's 2026-06-06 verdict: ONE cross-color sentinel, not per-color.
Sidesteps launchd's OR-combined KeepAlive dict-key semantics entirely
(one PathState entry; no per-color compose question). Apple's
``man launchd.plist`` flags PathState as race-prone, but at our per-swap
timescale (minutes) the race window is irrelevant.

This module lives outside :class:`MacosSelfDeploymentPlugin` so the
class body stays under the god-class threshold and the helper is
trivially reusable by other code paths that need the same sentinel
(notably the planned Slice 4.5 ``stop_self`` verb on the
self-deployment service interface).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Generator
from pathlib import Path

from ananta.core.runtime import draining_sentinel_path

from macos_self_deployment_plugin.constants import PLUGIN_NAME

_logger = logging.getLogger(PLUGIN_NAME)


def sentinel_path(solet_name: str) -> Path:
    """Resolve the drain-sentinel file path for ``solet_name``.

    Delegates to the core single-source resolver
    (:func:`ananta.core.runtime.draining_sentinel_path`) so this WRITE side
    (``write`` / ``held``) and the core READ side (``is_draining`` in this solet's
    SIGTERM handler) provably resolve the SAME file — a divergence would
    silently defeat respawn-suppression. Cross-color (the name is in the
    filename, no ``-blue`` / ``-green`` suffix).
    """
    return draining_sentinel_path(solet_name)


def write(solet_name: str) -> Path:
    """Write the sentinel and leave it on disk for the caller's lifetime.

    Used by ``stop_self`` (Slice 4.5) where the sentinel must persist
    beyond the verb's return so the LaunchAgent's PathState predicate
    keeps respawn suppressed until the operator re-launches the solet
    via ``python -m ananta.cli``. The cleanup path is
    :func:`stale_runtime_cleanup.cleanup_and_restore` on the next
    cold restart, invoked from the plugin's ``prepare_for_readiness``
    hook.

    Distinct from :func:`held`: the context manager removes the
    sentinel in finally (correct for blue-green drain where the
    solet continues serving). ``write`` is the explicit
    no-auto-cleanup form for the "stop and stay stopped" case.
    """
    path = sentinel_path(solet_name)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.touch()
    _logger.info(
        "%s: drain sentinel written at %s (persists until "
        "stale_runtime_cleanup.cleanup_and_restore runs)",
        PLUGIN_NAME, path,
    )
    return path


@contextlib.contextmanager
def held(solet_name: str) -> Generator[Path]:
    """Context manager that writes the sentinel on entry, removes on exit.

    Use to wrap the drain-kill SIGTERM + unregister window in
    :meth:`MacosSelfDeploymentPlugin.complete_swap`. The ``finally``
    branch is guaranteed to run even when the wrapped code raises, so
    a crash mid-drain does not leave a stale sentinel that suppresses
    future LaunchAgent respawns. The cold-restart safety net in
    ``stale_runtime_cleanup.cleanup_and_restore``'s ``_cleanup_stale_runtime_files`` covers the harder
    case where the whole solet process dies mid-drain before reaching
    the ``finally``.
    """
    path = sentinel_path(solet_name)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.touch()
    _logger.info(
        "%s: drain sentinel written at %s (LaunchAgent respawn suppressed)",
        PLUGIN_NAME, path,
    )
    try:
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
            _logger.info(
                "%s: drain sentinel removed at %s "
                "(LaunchAgent respawn re-enabled)",
                PLUGIN_NAME, path,
            )
        except OSError as exc:
            # Cleanup-failure is non-fatal: the next cold restart
            # scrubs the file via stale_runtime_cleanup.cleanup_and_restore's _cleanup_stale_runtime_files.
            # Log loud so the operator notices.
            _logger.error(
                "%s: failed to remove drain sentinel at %s: %s",
                PLUGIN_NAME, path, exc,
            )
