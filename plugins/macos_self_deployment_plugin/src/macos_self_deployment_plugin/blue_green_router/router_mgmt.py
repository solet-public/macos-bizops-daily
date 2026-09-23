"""Newline-delimited JSON RPC server over a Unix-domain socket.

The mgmt plane is the control-plane surface the deployment plugin
and the spawned solet children talk to. Localhost-only by virtue of
being a Unix socket; the authn boundary is filesystem permissions
(0600, owner-only).

Protocol per §2.4 of `2026-06-01_local_blue_green_L3_implementation_plan.md`:
each line is a JSON object `{"verb": "<name>", "args": {...}}`; the
server replies with one JSON object per line. Unknown verbs return
`{"error": "unknown_verb"}`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..constants import (
    DEFAULT_MGMT_SOCKET_PROBE_TIMEOUT_SECONDS,
    DEFAULT_MGMT_SOCKET_RECLAIM_POLL_INTERVAL_SECONDS,
    DEFAULT_MGMT_SOCKET_RECLAIM_WAIT_SECONDS,
)

logger = logging.getLogger(__name__)


DispatchFn = Callable[[str, dict[str, object]], Awaitable[dict[str, object]]]


class RouterSocketBusyError(RuntimeError):
    """Raised when the mgmt socket path is held by a router that still answers.

    Distinct from a generic bind failure on purpose: this one means another
    router is ALIVE at the path and refused to go away inside the reclaim
    window, so the correct action is to leave its socket alone and investigate
    the duplicate, never to unlink and take the path.
    """


class MgmtServer:
    """Asyncio Unix-socket server for the router's mgmt plane."""

    def __init__(
        self,
        socket_path: Path,
        dispatch: DispatchFn,
        reclaim_wait_seconds: float = DEFAULT_MGMT_SOCKET_RECLAIM_WAIT_SECONDS,
        reclaim_poll_interval_seconds: float = (
            DEFAULT_MGMT_SOCKET_RECLAIM_POLL_INTERVAL_SECONDS
        ),
    ) -> None:
        self._socket_path = socket_path
        self._dispatch = dispatch
        self._reclaim_wait_seconds = reclaim_wait_seconds
        self._reclaim_poll_interval_seconds = reclaim_poll_interval_seconds
        self._server: asyncio.base_events.Server | None = None
        # (st_dev, st_ino) of the socket file THIS server bound, captured after
        # bind. The unlink authority in stop(): a path whose identity no longer
        # matches belongs to a different server and must not be removed.
        self._bound_identity: tuple[int, int] | None = None

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        await self._reclaim_socket_path()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        self._socket_path.chmod(0o600)
        self._bound_identity = self._path_identity()
        logger.info(
            "mgmt: listening on %s (dev/ino %s)",
            self._socket_path,
            self._bound_identity,
        )

    async def _reclaim_socket_path(self) -> None:
        """Take the socket path only if no live router is answering on it.

        The blind ``unlink()`` this replaces was the forward-facing half of the
        overlapping-restart bug: a second router deleted a live one's socket on
        its way in, leaving the first serving an fd whose path no longer
        existed. Probe before taking: a path that answers ``status`` belongs to
        a router that is still up.

        An overlapping restart is a NORMAL operator path (`kickstart -k` is
        overlapping by design), so an answering incumbent is waited out, not
        refused on sight — the outgoing router's graceful shutdown is the window
        being tolerated. Only a path still answering past the bounded wait is a
        genuine two-router condition, and that refuses loudly. See the coupling
        note on DEFAULT_MGMT_SOCKET_RECLAIM_WAIT_SECONDS: this wait is bounded
        well under the platform child's own socket deadline on purpose.

        MEASURED 2026-08-14, and the reason this probe is load-bearing rather
        than decorative: ``asyncio.start_unix_server`` REMOVES an existing
        socket file at bind time all by itself, and it does NOT check whether
        anything is listening on it first (CPython's create_unix_server unlinks
        any path where ``S_ISSOCK`` holds). The event loop will therefore steal
        a LIVE router's socket without complaint. This probe runs BEFORE that
        call and raises first, so it is the only thing standing between an
        incoming router and a live incumbent's socket. Corollary: the explicit
        unlink below is belt-and-braces — asyncio would remove the stale file
        anyway — kept because a reclaim that is intended should be stated and
        logged, not left to another layer's implementation detail.
        """
        deadline = time.monotonic() + self._reclaim_wait_seconds
        waited_for_incumbent = False
        while True:
            if not self._socket_path.exists():
                return
            if await self._probe_status() is None:
                break
            if time.monotonic() >= deadline:
                raise RouterSocketBusyError(
                    f"mgmt socket {self._socket_path} is held by a router that "
                    f"still answers `status` after "
                    f"{self._reclaim_wait_seconds:.1f}s. Refusing to unlink a "
                    "LIVE router's socket — a second router is running against "
                    "this path. Stop the incumbent before starting this one."
                )
            waited_for_incumbent = True
            await asyncio.sleep(self._reclaim_poll_interval_seconds)
        if waited_for_incumbent:
            logger.info(
                "mgmt: incumbent at %s stopped answering; reclaiming the path",
                self._socket_path,
            )
        else:
            logger.info(
                "mgmt: reclaiming stale socket file at %s (nothing answering)",
                self._socket_path,
            )
        try:
            self._socket_path.unlink()
        except FileNotFoundError:
            pass

    async def _probe_status(self) -> dict[str, object] | None:
        """Speak the ``status`` verb at the socket path; None if nothing answers.

        Liveness by CONVERSATION, not by stat: a socket file on disk proves
        only that a path exists, and a router holding an unlinked socket proves
        the converse — the file is the wrong evidence in both directions.
        """
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(DEFAULT_MGMT_SOCKET_PROBE_TIMEOUT_SECONDS):
                reader, writer = await asyncio.open_unix_connection(
                    str(self._socket_path)
                )
                writer.write(json.dumps({"verb": "status", "args": {}}).encode() + b"\n")
                await writer.drain()
                line = await reader.readline()
        except (TimeoutError, OSError) as exc:
            logger.debug("mgmt: probe at %s did not answer: %s", self._socket_path, exc)
            return None
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionResetError, BrokenPipeError, OSError):
                    pass
        if not line.strip():
            return None
        try:
            payload = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            logger.debug("mgmt: probe reply at %s not JSON: %s", self._socket_path, exc)
            return None
        return payload if isinstance(payload, dict) else None

    def _path_identity(self) -> tuple[int, int] | None:
        """``(st_dev, st_ino)`` of the socket path, or None if it is absent."""
        try:
            stat_result = self._socket_path.stat()
        except OSError:
            return None
        return (stat_result.st_dev, stat_result.st_ino)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._release_socket_path()

    def _release_socket_path(self) -> None:
        """Unlink the socket file ONLY if it is still the one we bound.

        The unconditional unlink this replaces is how an overlapping restart
        killed the platform: the outgoing router's shutdown ``finally`` ran
        AFTER the incoming router had already rebound the path, so it deleted
        the NEW router's socket file. The new router kept serving its fd — alive
        to ``lsof``, invisible on disk — and the platform child's path-based
        readiness check then failed at its deadline.

        ``(st_dev, st_ino)`` is the identity that survives that race: rebinding
        creates a NEW inode at the same path, so a mismatch is exactly the
        "someone else owns this now" condition, and the correct action is to
        leave it alone. This is the half that PREVENTS the failure; the reclaim
        probe in start() is the half that stops us causing it for someone else.
        """
        bound = self._bound_identity
        self._bound_identity = None
        if bound is None:
            return
        current = self._path_identity()
        if current is None:
            logger.info(
                "mgmt: socket %s already absent at stop; nothing to unlink",
                self._socket_path,
            )
            return
        if current != bound:
            logger.warning(
                "mgmt: socket %s now belongs to another server "
                "(we bound dev/ino %s, on-disk is %s) — leaving it in place",
                self._socket_path,
                bound,
                current,
            )
            return
        try:
            self._socket_path.unlink()
        except FileNotFoundError:
            pass

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername") or "<unix>"
        logger.debug("mgmt: client connected %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                response = await self._process_line(line)
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            logger.debug("mgmt: client disconnected %s", peer)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    async def _process_line(self, line: bytes) -> dict[str, object]:
        try:
            payload = json.loads(line.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("mgmt: bad request: %s", exc)
            return {"error": "bad_json", "detail": str(exc)}
        if not isinstance(payload, dict):
            return {"error": "bad_request", "detail": "top-level must be object"}
        verb = payload.get("verb")
        if not isinstance(verb, str) or not verb:
            return {"error": "bad_request", "detail": "verb missing or not string"}
        args = payload.get("args", {})
        if not isinstance(args, dict):
            return {"error": "bad_request", "detail": "args must be object"}
        try:
            return await self._dispatch(verb, args)
        except Exception as exc:
            logger.exception("mgmt: dispatch failed for verb=%s", verb)
            return {"error": "dispatch_failed", "detail": str(exc)}
