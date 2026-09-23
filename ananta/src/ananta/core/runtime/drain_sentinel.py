"""Drain sentinel — the canonical path + the core read side.

The blue-green drain sentinel ``~/.ananta/runtime/<name>.draining`` marks that
THIS color is being **intentionally** drained — a cutover handing off to the
next color (the swap finisher writes it for the SIGTERM+unregister window) or an
operator ``stop_self`` (which writes it to persist). the solet's SIGTERM handler reads
it to decide its exit code:

* sentinel present  → intentional drain → exit 0 → launchd ``KeepAlive`` does
  NOT respawn (no ghost color).
* sentinel absent   → a stray SIGTERM of a live color → exit non-zero → launchd
  respawns (correct crash-supervision of a launchd-managed color).

The **writer** is ``macos_self_deployment_plugin.drain_sentinel`` (the finisher's
``held()`` context manager + ``stop_self``'s ``write()``). The **reader** is core
(``ananta.cli`` / ``EventOrchestrator``), which must not import the plugin — so
the suffix + path resolution live HERE and the plugin consumes them
(plugin→core, the correct dependency direction). One definition means the read
path and the write path provably resolve the SAME file; a divergence would
silently defeat respawn-suppression (the handler would read a non-existent
sentinel and always exit non-zero → ghost respawn).
"""

from __future__ import annotations

import os
from pathlib import Path

from ananta.core.runtime.port_manager import get_runtime_dir

# The single source of truth for the sentinel filename suffix. Promoted from
# the plugin's ``constants.DRAINING_SENTINEL_SUFFIX`` so core carries no magic
# string and the plugin re-exports this value.
DRAINING_SENTINEL_SUFFIX = ".draining"


def _resolve_name(solet_name: str | None) -> str:
    """Resolve the solet name, falling back to ``SOLET_NAME``.

    Matches the writer's resolution (the plugin always passes the same
    ``self._solet_name`` it took from the environment), so the read and
    write paths agree. Raises loudly if neither a name nor the env var is
    available — a missing name must fail fast, never silently mis-resolve.
    """
    name = (solet_name or os.environ.get("SOLET_NAME", "")).strip()
    if not name:
        raise RuntimeError(
            "cannot resolve drain-sentinel path: no solet_name and "
            "SOLET_NAME is unset"
        )
    return name


def draining_sentinel_path(solet_name: str | None = None) -> Path:
    """Canonical drain-sentinel path for ``solet_name``.

    The file is flat in the runtime dir with the name in the FILENAME (not a
    per-name subdir): ``<runtime_dir>/<name>.draining``. ``solet_name=None``
    resolves it from ``SOLET_NAME``.
    """
    name = _resolve_name(solet_name)
    return get_runtime_dir(name) / f"{name}{DRAINING_SENTINEL_SUFFIX}"


def is_draining(solet_name: str | None = None) -> bool:
    """True iff the drain sentinel exists — safe to call from a signal handler.

    Never raises: any resolution/IO error resolves to ``False`` (treat an
    unreadable sentinel as 'not draining' → exit non-zero → launchd respawns,
    the safe-for-supervision default).
    """
    try:
        return draining_sentinel_path(solet_name).exists()
    except Exception:  # noqa: BLE001 — signal-handler read: never raise, fail safe
        return False


__all__ = [
    "DRAINING_SENTINEL_SUFFIX",
    "draining_sentinel_path",
    "is_draining",
]
