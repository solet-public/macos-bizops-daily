"""Synchronous Unix-socket client for the local-blue-green router.

Translates the six mgmt verbs from
``plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/router_mgmt.py`` into Python calls. Each
RPC opens a fresh blocking ``AF_UNIX`` connection, sends one
newline-delimited JSON request, reads one newline-delimited JSON
response, and closes. Connections are short-lived so a router restart
between calls is transparent: the next call opens a new connection
against the freshly-bound socket.

Heartbeat callers (the long-running keepalive loop) should still use
short-lived connections per beat — the cost is negligible (Unix
socket + 1 RTT) and the resilience to router restarts is the whole
point.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from macos_self_deployment_plugin.constants import (
    DEFAULT_ROUTER_REQUEST_TIMEOUT_SECONDS,
)


class RouterClientError(Exception):
    """Raised when an RPC cannot complete or returns a wire-level error.

    Carries the failing verb name and a human-readable detail. The
    plugin maps this to its envelope ``status='failed'`` shape.
    """

    def __init__(self, verb: str, detail: str) -> None:
        super().__init__(f"router rpc failed verb={verb}: {detail}")
        self.verb = verb
        self.detail = detail


class RouterClient:
    """Stateless RPC client for the router's mgmt-socket protocol.

    Stateless = no connection cached. Each call opens-sends-receives-closes.
    The cost is ~tens of microseconds per call; the resilience benefit
    is correct behaviour across router restarts.
    """

    def __init__(
        self,
        socket_path: Path,
        timeout_seconds: float = DEFAULT_ROUTER_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._socket_path = socket_path
        self._timeout_seconds = timeout_seconds

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def status(self) -> dict[str, Any]:
        return self._call("status", {})

    def register_color(
        self,
        port: int,
        color: str,
        instance_id: str,
        *,
        streamable_port: int | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"port": port, "color": color, "instance_id": instance_id}
        # BLG-04: omitted (not merely None) when the caller has nothing new
        # to report — the router's own `register()` treats an explicit
        # streamable_port as authoritative and a missing one as "preserve
        # whatever I already had", so a bare `{"streamable_port": None}` key
        # would be indistinguishable on the wire from "preserve" anyway, but
        # omitting it keeps the payload identical to pre-BLG-04 callers.
        if streamable_port is not None:
            args["streamable_port"] = streamable_port
        return self._call("register_color", args)

    def unregister_color(self, instance_id: str) -> dict[str, Any]:
        return self._call("unregister_color", {"instance_id": instance_id})

    def heartbeat(self, instance_id: str) -> dict[str, Any]:
        return self._call("heartbeat", {"instance_id": instance_id})

    def activate(self, color: str, instance_id: str) -> dict[str, Any]:
        return self._call(
            "activate", {"color": color, "instance_id": instance_id}
        )

    def rollback(self, color: str, instance_id: str) -> dict[str, Any]:
        return self._call("rollback", {"color": color, "instance_id": instance_id})

    def _call(self, verb: str, args: dict[str, Any]) -> dict[str, Any]:
        """Open one short-lived connection; send one line; read one line."""
        request = json.dumps({"verb": verb, "args": args}).encode() + b"\n"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout_seconds)
        try:
            try:
                sock.connect(str(self._socket_path))
            except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
                raise RouterClientError(
                    verb, f"connect to {self._socket_path}: {exc}"
                ) from exc
            try:
                sock.sendall(request)
            except (BrokenPipeError, OSError) as exc:
                raise RouterClientError(verb, f"sendall: {exc}") from exc
            response_bytes = self._read_line(sock, verb)
            try:
                payload = json.loads(response_bytes.decode())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RouterClientError(
                    verb, f"decode response: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise RouterClientError(
                    verb, f"response not a JSON object: {payload!r}"
                )
            return payload
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _read_line(self, sock: socket.socket, verb: str) -> bytes:
        """Read bytes until the trailing newline or the peer closes."""
        buf = bytearray()
        while True:
            try:
                chunk = sock.recv(4096)
            except TimeoutError as exc:
                raise RouterClientError(verb, f"recv timeout: {exc}") from exc
            except OSError as exc:
                raise RouterClientError(verb, f"recv: {exc}") from exc
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in chunk:
                break
        if not buf:
            raise RouterClientError(verb, "empty response")
        # Strip the trailing newline plus any incidental whitespace.
        newline_at = buf.find(b"\n")
        if newline_at == -1:
            return bytes(buf)
        return bytes(buf[:newline_at])
