"""Single-source solet-child spawn mechanics for the two spawn paths.

Two callers spawn a solet child process and they MUST agree on the launch
contract (interpreter invocation, working directory, stdio, base env) or
the spawned child self-activates inconsistently:

- :func:`swap_orchestrator.default_spawn` — the blue-green *swap* path.
  Spawns the **candidate** ``rel-<id>`` interpreter with an explicit
  pre-minted colour + instance-id + release-id so the swap can drive
  ``register`` → ``activate`` for that exact child.
- :func:`supervisor.Supervisor` — the Option-B *cold-start / crash*
  path. Spawns the ``current`` interpreter and lets the child self-default its
  colour/instance-id (mirroring the historical direct-launch cold-start),
  so the child self-activates whenever the router has no active colour.

The shared mechanics live here so neither path can drift from the other:
the cmd shape (``-m ananta.cli --app-home``), the out-of-tree CWD (§5
hygiene — never a code tree, or a stray relative-path write mutates it),
``start_new_session=True`` (the child is its own POSIX session leader so a
SIGTERM to its spawner never cascades), and the per-spawn append-only log.
Per-caller differences (which interpreter, which extra env keys) are
parameters, not forks of the spawn body.

Deliberately lightweight — imports only stdlib + the ``ENV_*`` constants +
the core runtime-dir resolver. The supervisor (the process keeping the solet
alive) imports this without dragging in the plugin-class graph.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from ananta.core.runtime import get_runtime_dir

from macos_self_deployment_plugin.constants import ENV_SOLET_NAME

# The entrypoint every solet child is launched as. Module form (``-m``) so
# resolution rides the interpreter's own venv ``.pth``, independent of CWD.
_ANANTA_CLI_MODULE: Final[str] = "ananta.cli"
_APP_HOME_FLAG: Final[str] = "--app-home"


def build_child_command(interpreter: str, app_home: Path) -> list[str]:
    """The ``ProgramArguments``-equivalent for a spawned solet child."""
    return [interpreter, "-m", _ANANTA_CLI_MODULE, _APP_HOME_FLAG, str(app_home)]


def spawn_solet_child(
    *,
    interpreter: str,
    app_home: Path,
    solet_name: str,
    log_path: Path,
    extra_env: Mapping[str, str],
) -> subprocess.Popen[bytes]:
    """Spawn one solet child and return its live :class:`subprocess.Popen`.

    The ``swap`` path discards everything but ``.pid``; the supervisor
    keeps the handle so it can ``poll()`` (liveness + zombie reaping).

    Args:
        interpreter: The python3 to exec — the candidate release's venv
            (swap) or the literal ``current/venv/bin/python3`` symlink
            (supervisor; the OS resolves it at exec, so a cutover/rollback
            flip is picked up with no re-render).
        app_home: ``--app-home`` for the child (the shared profile).
        solet_name: Propagated as ``SOLET_NAME`` and used to
            resolve the out-of-tree CWD.
        log_path: Per-spawn append-only log capturing the child's
            stdout+stderr (a green/cold-start that fails to register
            otherwise leaves no diagnostic).
        extra_env: Caller-specific env on top of the inherited environment
            + ``SOLET_NAME`` (e.g. the swap's explicit
            colour/instance/release; the supervisor's release-id audit
            field).
    """
    cmd = build_child_command(interpreter, app_home)
    env = os.environ.copy()
    env[ENV_SOLET_NAME] = solet_name
    env.update(extra_env)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_dir = get_runtime_dir(solet_name)
    # The parent's log handle is closed right after fork; the child keeps
    # its inherited dup of the fd, so the redirect survives.
    with log_path.open("ab") as log_fh:
        return subprocess.Popen(
            cmd,
            cwd=str(runtime_dir),
            env=env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )


__all__ = ["build_child_command", "spawn_solet_child"]
