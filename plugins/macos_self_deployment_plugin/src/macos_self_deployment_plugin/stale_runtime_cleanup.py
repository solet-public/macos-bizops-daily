"""F2-A5 stale runtime cleanup + bridge-port restore.

Ports ``launch.py``'s ``_cleanup_stale_runtime_files`` +
``_restore_router_owned_bridge_port_file_if_router_live`` into a plugin
lifecycle hook. Invoked from the plugin's ``prepare_for_readiness``
hook — runs BEFORE any port-binding per the platform's two-phase
startup contract (F2-IMPL-PEER-VERIFY-9).

Closes TLC counterexample #2 from Slice 1.5 of
``workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md``:
a stale ``<name>.bridge.port`` value left by a crashed router lets the
next cold-start solet bind a port matching the file's content; cleanup
must run BEFORE port-binding so the file points at the router (post-
install) and not at solet child.
"""

from __future__ import annotations

import json
import logging
import socket
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

_STALE_FILENAME_TEMPLATES: Final[tuple[str, ...]] = (
    "{name}.sock",
    "{name}.rest.port",
    "{name}.bridge.port",
    # Slice 4 cross-color drain sentinel — see launch.py:391-404 docstring.
    "{name}.draining",
)

# Exclusive upper bound for a valid TCP port (ports are 1..65535).
_PORT_CEILING: Final[int] = 65536


def runtime_dir() -> Path:
    """``~/.ananta/runtime`` — runtime sockets + port files live here."""
    return Path.home() / ".ananta" / "runtime"


def cleanup_stale_runtime_files(solet_name: str) -> None:
    """Remove runtime files left by a non-graceful prior the solet or router exit."""
    base = runtime_dir()
    for template in _STALE_FILENAME_TEMPLATES:
        stale = base / template.format(name=solet_name)
        if stale.exists() or stale.is_symlink():
            try:
                stale.unlink()
            except OSError as exc:
                logger.warning("Could not remove stale %s: %s", stale, exc)
                continue
            logger.info("Removed stale runtime file: %s", stale)


def restore_router_owned_bridge_port_file_if_router_live(solet_name: str) -> bool:
    """Re-materialize ``<name>.bridge.port`` from the live router.

    Cold start scrubs the canonical bridge-port file before solet spawn so
    stale router state cannot be mistaken for the current ingress. When the
    local blue-green router is already alive at scrub time, its
    ``<name>.router.sock`` management socket plus the ``<name>.router.port``
    file ``install_router`` wrote are enough to re-materialize the
    router-owned pointer immediately — otherwise the ``install_router``
    re-write is the only path and the file stays absent until then.

    Returns ``True`` if the bridge-port file was restored, ``False``
    otherwise. The router's public port is read from the
    ``<name>.router.port`` file (the management socket is probed only for
    liveness — the ``status`` reply does not carry the public port).
    """
    base = runtime_dir()
    router_port_file = base / f"{solet_name}.router.port"
    router_socket = base / f"{solet_name}.router.sock"
    bridge_port_file = base / f"{solet_name}.bridge.port"

    router_port = _read_port_file(router_port_file)
    if router_port is None:
        return False
    # The mgmt probe is the SINGLE authority for router liveness here; it
    # replaced a preceding `router_socket.exists()` stat that could not change
    # the outcome in either direction (the probe connects BY PATH, so it fails
    # for an absent path exactly as the stat did). This is a diagnosis change,
    # NOT a fix: the two states below were already handled correctly and
    # indistinguishably, and now they are logged apart. Contrast
    # `plugin._wait_for_router_socket`, where the equivalent stat WAS
    # load-bearing and wrong — see the mgmt socket ownership contract article.
    if router_mgmt_status(router_socket) is None:
        if router_socket.exists():
            logger.debug(
                "Router mgmt socket present at %s but nothing answered; "
                "treating the router as down (stale socket file).",
                router_socket,
            )
        else:
            logger.debug(
                "No router mgmt socket at %s; treating the router as down. "
                "NOTE: a live router holding an UNLINKED socket is "
                "indistinguishable from this by any path-based check.",
                router_socket,
            )
        return False
    bridge_port_file.write_text(str(router_port), encoding="utf-8")
    bridge_port_file.chmod(0o600)
    logger.info(
        "Restored %s from live router (port %d)", bridge_port_file, router_port,
    )
    return True


def cleanup_and_restore(solet_name: str) -> None:
    """Scrub stale runtime files then restore bridge-port if router is live.

    Single entrypoint for the plugin's ``prepare_for_readiness`` hook.
    """
    cleanup_stale_runtime_files(solet_name)
    restore_router_owned_bridge_port_file_if_router_live(solet_name)


def _read_port_file(path: Path) -> int | None:
    """Read + range-validate a port integer from a ``*.port`` file."""
    try:
        port = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return port if 0 < port < _PORT_CEILING else None


def router_mgmt_status(socket_path: Path) -> dict[str, object] | None:
    """Probe the router's mgmt socket for liveness; return its status payload.

    Speaks the router's verb-dispatch protocol (``{"verb": "status",
    "args": {}}``) and reads a single newline-terminated JSON reply, matching
    ``install_router._mgmt_status`` and the pre-deletion ``launch.py`` helper.
    Used only to confirm the router is alive; the public port is read from the
    ``<name>.router.port`` file by the caller.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(str(socket_path))
            sock.sendall(json.dumps({"verb": "status", "args": {}}).encode() + b"\n")
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
    except (TimeoutError, OSError) as exc:
        logger.debug("Router mgmt probe failed at %s: %s", socket_path, exc)
        return None
    line = bytes(buf).split(b"\n", 1)[0].strip()
    if not line:
        return None
    try:
        payload = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        logger.debug("Router mgmt reply not JSON at %s: %s", socket_path, exc)
        return None
    return payload if isinstance(payload, dict) else None
