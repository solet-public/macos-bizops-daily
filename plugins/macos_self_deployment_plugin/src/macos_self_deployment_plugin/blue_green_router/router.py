"""Local blue-green router — public HTTP+SSE proxy + mgmt-plane wire-up.

Implements §2 of `2026-06-01_local_blue_green_L3_implementation_plan.md`:

- Public surface: HTTP+SSE proxy on a stable port (canonical 8100)
  that forwards every byte to the resolved color upstream. Routing
  decisions are made per request: parse the request line + headers,
  read the optional `Mcp-Session-Id` header, consult RouterState for
  the bound color (or fall back to active_color). On the response
  side, capture any newly-issued `Mcp-Session-Id` header and record
  the (session → instance) binding for subsequent requests.

- Mgmt plane: see `router_mgmt.py`. Dispatch handlers below translate
  newline-JSON RPCs into RouterState mutations.

- Stateless across router restarts (D23' Option I): nothing on disk;
  the buffer-on-no-active-color path holds new connections for up to
  10s while children re-register via heartbeat.

- Session affinity per D21' Option α: an Mcp-Session-Id stays bound
  to the color that initialized it until that color's drain window
  expires.

Run foreground for development:

    python3 -m macos_self_deployment_plugin.blue_green_router.router \\
        --solet iris --public-port 8100

The launchd/systemd install (Slice H) wraps this exact invocation.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .router_mgmt import MgmtServer
from .router_state import (
    DEFAULT_DRAIN_WINDOW_SECONDS,
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    ColorBinding,
    RouterState,
)

logger = logging.getLogger("local_blue_green.router")

DEFAULT_PUBLIC_PORT: int = 8100
DEFAULT_BUFFER_TIMEOUT_SECONDS: float = 10.0
DEFAULT_DRAIN_SWEEP_INTERVAL_SECONDS: float = 5.0
# Cycle 7 §2.1 of
# ``workbench/2026-06-16_bridge_port_three_color_split_brain_followon.md``:
# the watchdog re-writes ``<name>.bridge.port`` + ``<name>.router.port``
# on every tick. The bind-time one-shot self-write closes the cold-start
# race; the watchdog closes the steady-state race where F2 Phase 0c's
# ``stale_runtime_cleanup.cleanup_and_restore`` scrubs the file then
# fails to restore via the live router's mgmt socket (transient probe
# failure leaves the file empty with no other convergent re-write path).
# Operator-locked at 5s on 2026-06-16.
DEFAULT_BRIDGE_PORT_WATCHDOG_INTERVAL_SECONDS: float = 5.0
MCP_SESSION_HEADER_LOWER: str = "mcp-session-id"
HEADER_TERMINATOR: bytes = b"\r\n\r\n"
MAX_HEADER_BYTES: int = 65536


@dataclass(frozen=True, slots=True)
class ParsedRequestHead:
    request_line: bytes
    headers: list[tuple[bytes, bytes]]
    mcp_session_id: str | None
    content_length: int | None
    transfer_encoding: str | None
    head_bytes: bytes


def _runtime_dir() -> Path:
    return Path.home() / ".ananta" / "runtime"


def mgmt_socket_path(solet: str, runtime_dir: Path | None = None) -> Path:
    return (runtime_dir or _runtime_dir()) / f"{solet}.router.sock"


def _write_port_discovery_files(
    solet: str, port: int, runtime_dir: Path | None = None,
) -> None:
    """Write ``<name>.router.port`` and ``<name>.bridge.port`` to runtime dir.

    ``runtime_dir`` is the installer's ``--runtime-dir`` (threaded through
    the service template); ``None`` is the real ``~/.ananta/runtime``. The
    daemon runs under launchd/systemd with the REAL ``HOME``, so without
    this parameter a sandboxed install smoke still left its port files in
    the operator's runtime dir (iss_6e8c204c).

    Called once at router bind-time so the discovery files exist immediately
    after the public surface starts listening, independent of when
    ``install_router.py`` last ran. Closes the cold-start race where
    ``launch.py`` scrubs the files at termination time and tries to restore
    them from the live router before the router has finished binding —
    ``_restore_router_owned_bridge_port_file_if_router_live`` in launch.py is
    a synchronous one-shot, so a still-binding router leaves the bridge
    pointer absent. Self-write here makes the router self-sufficient.
    """
    runtime = runtime_dir or _runtime_dir()
    runtime.mkdir(parents=True, mode=0o700, exist_ok=True)
    for name in (f"{solet}.router.port", f"{solet}.bridge.port"):
        path = runtime / name
        path.write_text(str(port), encoding="utf-8")
        path.chmod(0o600)


def _split_header_line(raw: bytes) -> tuple[bytes, bytes, str, str] | None:
    """Split one header line into ``(name_bytes, value_bytes, name_lower, value_str)``.

    Returns ``None`` if the line is empty, lacks ``:``, or fails ASCII decode.
    """
    if not raw or b":" not in raw:
        return None
    name_bytes, _, value_bytes = raw.partition(b":")
    value_bytes = value_bytes.strip()
    try:
        return name_bytes, value_bytes, name_bytes.lower().decode(), value_bytes.decode()
    except UnicodeDecodeError:
        return None


def _coerce_content_length(value_str: str) -> int | None:
    try:
        return int(value_str)
    except ValueError:
        return None


@dataclass
class _SpecialHeaderAccumulator:
    mcp_session_id: str | None = None
    content_length: int | None = None
    transfer_encoding: str | None = None

    def apply(self, name_lower: str, value_str: str) -> None:
        if name_lower == MCP_SESSION_HEADER_LOWER and value_str:
            self.mcp_session_id = value_str
        elif name_lower == "content-length":
            self.content_length = _coerce_content_length(value_str)
        elif name_lower == "transfer-encoding":
            self.transfer_encoding = value_str.lower()


def _parse_request_head(head_bytes: bytes) -> ParsedRequestHead:
    """Split bytes-up-to-CRLFCRLF into request line + headers.

    ``head_bytes`` must include the trailing CRLFCRLF terminator so the
    caller can forward ``head_bytes`` verbatim to upstream without
    having to re-append the terminator. Parsing operates on a
    stripped-terminator view internally.
    """

    stripped = head_bytes.rstrip(HEADER_TERMINATOR)
    lines = stripped.split(b"\r\n")
    if not lines or not lines[0]:
        msg = "empty request"
        raise ValueError(msg)

    request_line = lines[0]
    headers: list[tuple[bytes, bytes]] = []
    specials = _SpecialHeaderAccumulator()
    for raw in lines[1:]:
        parsed = _split_header_line(raw)
        if parsed is None:
            continue
        name_bytes, value_bytes, name_lower, value_str = parsed
        headers.append((name_bytes, value_bytes))
        specials.apply(name_lower, value_str)

    return ParsedRequestHead(
        request_line=request_line,
        headers=headers,
        mcp_session_id=specials.mcp_session_id,
        content_length=specials.content_length,
        transfer_encoding=specials.transfer_encoding,
        head_bytes=head_bytes,
    )


async def _read_request_head(
    reader: asyncio.StreamReader,
) -> tuple[bytes, bytes] | None:
    """Read until CRLFCRLF or limit; return (head, remainder).

    `head` ends in CRLFCRLF. `remainder` carries any over-read bytes
    that belong to the request body and must be forwarded verbatim
    to the upstream after the head.

    Returns None on clean close before any bytes arrived.
    """

    buf = bytearray()
    while HEADER_TERMINATOR not in buf:
        chunk = await reader.read(4096)
        if not chunk:
            if not buf:
                return None
            msg = "incomplete request head"
            raise ValueError(msg)
        buf.extend(chunk)
        if len(buf) > MAX_HEADER_BYTES:
            msg = "request head too large"
            raise ValueError(msg)
    terminator_at = buf.index(HEADER_TERMINATOR)
    head = bytes(buf[: terminator_at + len(HEADER_TERMINATOR)])
    remainder = bytes(buf[terminator_at + len(HEADER_TERMINATOR) :])
    return head, remainder


async def _open_upstream(
    binding: ColorBinding, *, port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port)


def _main_port(binding: ColorBinding) -> int | None:
    return binding.port


def _streamable_port(binding: ColorBinding) -> int | None:
    return binding.streamable_port


async def _pump(
    src: asyncio.StreamReader,
    dst: asyncio.StreamWriter,
    *,
    on_chunk: Callable[[bytes], None] | None = None,
    chunk_size: int = 65536,
) -> None:
    try:
        while True:
            chunk = await src.read(chunk_size)
            if not chunk:
                break
            if on_chunk is not None:
                on_chunk(chunk)
            dst.write(chunk)
            await dst.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            dst.write_eof()
        except (OSError, RuntimeError, NotImplementedError):
            pass


class _ResponseHeaderSniffer:
    """Best-effort scan of upstream-to-client byte stream for Mcp-Session-Id.

    The router doesn't parse the HTTP response — it just streams
    bytes — but we want to capture the Set-style Mcp-Session-Id
    header so future requests on that session route to the right
    color. The sniffer accumulates bytes until it has seen the first
    CRLFCRLF, scans the header block once, then disengages.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._done = False
        self.captured: str | None = None

    def consume(self, chunk: bytes) -> None:
        if self._done:
            return
        self._buf.extend(chunk)
        if HEADER_TERMINATOR in self._buf:
            head_end = self._buf.index(HEADER_TERMINATOR)
            head_bytes = bytes(self._buf[:head_end])
            self._scan(head_bytes)
            self._done = True
            self._buf.clear()
        elif len(self._buf) > MAX_HEADER_BYTES:
            self._done = True
            self._buf.clear()

    def _scan(self, head_bytes: bytes) -> None:
        for raw in head_bytes.split(b"\r\n"):
            if b":" not in raw:
                continue
            name_bytes, _, value_bytes = raw.partition(b":")
            try:
                name_lower = name_bytes.lower().decode()
                value_str = value_bytes.strip().decode()
            except UnicodeDecodeError:
                continue
            if name_lower == MCP_SESSION_HEADER_LOWER and value_str:
                self.captured = value_str
                return


async def _wait_for_route(
    state: RouterState,
    mcp_session_id: str | None,
    *,
    buffer_timeout: float,
    poll_interval: float = 0.05,
) -> ColorBinding | None:
    """Wait up to `buffer_timeout` seconds for a routable color.

    Implements §2.6 of the L3 plan: during the post-restart 0–20s
    window the router has no active color; instead of returning 503
    immediately, hold the connection for up to 10s so the children's
    re-registration heartbeats can land.
    """

    deadline = time.monotonic() + buffer_timeout
    while True:
        binding = state.resolve_route(mcp_session_id)
        if binding is not None:
            return binding
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(poll_interval)


async def _write_503(writer: asyncio.StreamWriter, reason: str) -> None:
    body = (
        f"local_blue_green_router: no active color available "
        f"({reason}). The router is up but no solet child is currently "
        "active. The platform itself may be healthy on its own ephemeral "
        "port (probe /api/v1/bridge/health there); if so, the platform's "
        "steady-state heartbeat re-asserts activation within ~10s on "
        "current builds — on older builds, restart the platform "
        "LaunchAgent once more to re-trigger cold-start activation. Leave "
        "the router itself running.\r\n"
    ).encode()
    response = (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    ) + body
    try:
        writer.write(response)
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass


@dataclass
class _PreparedProxyRoute:
    parsed: ParsedRequestHead
    remainder: bytes
    up_reader: asyncio.StreamReader
    up_writer: asyncio.StreamWriter
    binding: ColorBinding


async def _prepare_proxy_route(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    state: RouterState,
    buffer_timeout: float,
    port_selector: Callable[[ColorBinding], int | None] = _main_port,
    no_route_reason: str = "no_active_color",
) -> _PreparedProxyRoute | None:
    """Parse the request head, resolve the bound color, open upstream.

    ``port_selector`` picks which port on the resolved binding to connect
    to — the main bridge port (default) or, for the BLG-04 streamable
    listener, ``binding.streamable_port``. A resolved color whose selected
    port is ``None`` (a color that hasn't reported a streamable port, e.g.
    it runs with streamable disabled) is treated the same as no route.

    On any recoverable failure (bad request, no route, upstream refused)
    writes a 503 to ``writer`` and returns ``None``.
    """
    peer = writer.get_extra_info("peername")
    try:
        head_pair = await _read_request_head(reader)
        if head_pair is None:
            return None
        head, remainder = head_pair
        parsed = _parse_request_head(head)
    except ValueError as exc:
        logger.warning("public: parse failed for %s: %s", peer, exc)
        await _write_503(writer, "bad_request")
        return None

    binding = await _wait_for_route(
        state, parsed.mcp_session_id, buffer_timeout=buffer_timeout,
    )
    if binding is None:
        await _write_503(writer, no_route_reason)
        return None
    upstream_port = port_selector(binding)
    if upstream_port is None:
        await _write_503(writer, no_route_reason)
        return None

    try:
        up_reader, up_writer = await _open_upstream(binding, port=upstream_port)
    except (ConnectionRefusedError, OSError) as exc:
        logger.warning(
            "public: upstream %s:%d connect failed: %s",
            binding.color, upstream_port, exc,
        )
        await _write_503(writer, "upstream_connect_failed")
        return None

    return _PreparedProxyRoute(
        parsed=parsed,
        remainder=remainder,
        up_reader=up_reader,
        up_writer=up_writer,
        binding=binding,
    )


async def _drain_pending(pending: set[asyncio.Task[None]]) -> None:
    """Cancel + await pending pump tasks; swallow expected close-time errors."""
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(
            asyncio.CancelledError, ConnectionResetError, BrokenPipeError,
        ):
            await task


def _log_pump_exceptions(done: set[asyncio.Task[None]]) -> None:
    benign = (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError)
    for task in done:
        exc = task.exception()
        if exc is not None and not isinstance(exc, benign):
            logger.warning("public: pump exception: %s", exc)


async def _proxy_streams(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    route: _PreparedProxyRoute,
) -> str | None:
    """Pump request bytes upstream and response bytes back; race directions.

    The downstream pump completes when the upstream closes (response
    finished). The upstream pump typically blocks indefinitely on a GET
    (client doesn't half-close after sending headers). When either
    completes, cancel the other so the connection can close.
    """
    up_writer = route.up_writer
    up_writer.write(route.parsed.head_bytes)
    if route.remainder:
        up_writer.write(route.remainder)
    await up_writer.drain()

    sniffer = _ResponseHeaderSniffer()

    def _on_response_chunk(chunk: bytes) -> None:
        sniffer.consume(chunk)

    downstream_task = asyncio.create_task(
        _pump(route.up_reader, writer, on_chunk=_on_response_chunk),
        name="public_downstream",
    )
    upstream_task = asyncio.create_task(
        _pump(reader, up_writer), name="public_upstream",
    )
    done, pending = await asyncio.wait(
        {downstream_task, upstream_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    await _drain_pending(pending)
    _log_pump_exceptions(done)
    return sniffer.captured


async def _handle_public_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    state: RouterState,
    buffer_timeout: float,
) -> None:
    route = await _prepare_proxy_route(
        reader, writer, state=state, buffer_timeout=buffer_timeout,
    )
    if route is None:
        return
    try:
        captured_session = await _proxy_streams(reader, writer, route=route)
        if captured_session is not None:
            state.record_session(captured_session, route.binding.instance_id)
    finally:
        with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
            route.up_writer.close()
            await route.up_writer.wait_closed()


async def _handle_streamable_public_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    state: RouterState,
    buffer_timeout: float,
) -> None:
    """BLG-04: same proxy as :func:`_handle_public_request`, streamable port.

    Resolves the SAME active-color/session-affinity binding as the main
    proxy — only the upstream port selection differs (``binding.
    streamable_port`` instead of ``binding.port``). A color that hasn't
    reported a streamable port (streamable disabled, or not registered yet)
    503s here exactly like "no active color" does on the main proxy.
    """
    route = await _prepare_proxy_route(
        reader, writer, state=state, buffer_timeout=buffer_timeout,
        port_selector=_streamable_port, no_route_reason="no_streamable_route",
    )
    if route is None:
        return
    try:
        captured_session = await _proxy_streams(reader, writer, route=route)
        if captured_session is not None:
            state.record_session(captured_session, route.binding.instance_id)
    finally:
        with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
            route.up_writer.close()
            await route.up_writer.wait_closed()


async def _drain_sweeper(
    state: RouterState,
    *,
    interval: float = DEFAULT_DRAIN_SWEEP_INTERVAL_SECONDS,
) -> None:
    while True:
        await asyncio.sleep(interval)
        expired = state.sweep_expired_drain()
        for entry in expired:
            logger.info(
                "drain: expired color=%s instance=%s",
                entry.binding.color,
                entry.binding.instance_id,
            )


async def _heartbeat_gc(
    state: RouterState,
    *,
    interval: float = 5.0,
) -> None:
    while True:
        await asyncio.sleep(interval)
        now = state.now()
        timeout = state.heartbeat_timeout_seconds
        stale = [
            iid
            for iid, b in state.bindings.items()
            if now - b.last_heartbeat > timeout
        ]
        for iid in stale:
            is_active = iid == state.active_instance_id
            logger.info(
                "hb-gc: dropping stale binding instance=%s active=%s",
                iid, is_active,
            )
            state.unregister(iid)


async def _bridge_port_watchdog(
    solet: str,
    public_port: int,
    *,
    interval: float = DEFAULT_BRIDGE_PORT_WATCHDOG_INTERVAL_SECONDS,
    runtime_dir: Path | None = None,
) -> None:
    """Re-write ``<name>.router.port`` + ``<name>.bridge.port`` every tick.

    Closes Cycle 7 §2.1 of
    ``workbench/2026-06-16_bridge_port_three_color_split_brain_followon.md``:
    ``_write_port_discovery_files`` runs ONCE at router bind-time so a
    transient mgmt-probe failure during F2 Phase 0c's
    ``stale_runtime_cleanup.cleanup_and_restore`` (Failure Mode B) leaves
    the canonical
    ``<name>.bridge.port`` empty with no convergent re-write. The
    watchdog ticks every 5s and re-materializes both files via
    ``Path.write_text`` — idempotent on the happy path, convergent
    against an empty/missing file inside one tick window.

    Writes directly bypass ``port_manager.write_port_file`` because the
    latter raises ``ValueError`` on ``service_name='bridge'`` by design
    (the regression guard at ``port_manager.py:164-171``); the router
    owns the bridge port file, so the watchdog is the legitimate caller
    that bypasses the guard via the same direct-write path
    ``_write_port_discovery_files`` already uses.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            _write_port_discovery_files(solet, public_port, runtime_dir)
        except OSError as exc:
            logger.warning(
                "bridge-port-watchdog: re-write failed (will retry next tick): %s",
                exc,
            )


def _make_dispatch(state: RouterState):
    async def dispatch(verb: str, args: dict[str, object]) -> dict[str, object]:
        if verb == "register_color":
            return _dispatch_register(state, args)
        if verb == "unregister_color":
            return _dispatch_unregister(state, args)
        if verb == "heartbeat":
            return _dispatch_heartbeat(state, args)
        if verb == "activate":
            return _dispatch_activate(state, args)
        if verb == "rollback":
            return _dispatch_rollback(state, args)
        if verb == "status":
            return _dispatch_status(state)
        return {"error": "unknown_verb", "verb": verb}

    return dispatch


def _dispatch_register(state: RouterState, args: dict[str, object]) -> dict[str, object]:
    port = args.get("port")
    color = args.get("color")
    instance_id = args.get("instance_id")
    if not isinstance(port, int) or not isinstance(color, str) or not isinstance(instance_id, str):
        return {"accepted": False, "reason": "bad_args"}
    streamable_port = args.get("streamable_port")
    if streamable_port is not None and not isinstance(streamable_port, int):
        return {"accepted": False, "reason": "bad_args"}
    result = state.register(port, color, instance_id, streamable_port=streamable_port)
    out: dict[str, object] = {"accepted": result.accepted}
    if result.reason:
        out["reason"] = result.reason
    return out


def _dispatch_unregister(state: RouterState, args: dict[str, object]) -> dict[str, object]:
    instance_id = args.get("instance_id")
    if not isinstance(instance_id, str):
        return {"unregistered": False, "reason": "bad_args"}
    return {"unregistered": state.unregister(instance_id)}


def _dispatch_heartbeat(state: RouterState, args: dict[str, object]) -> dict[str, object]:
    instance_id = args.get("instance_id")
    if not isinstance(instance_id, str):
        return {"alive": False, "reason": "bad_args"}
    result = state.heartbeat(instance_id)
    out: dict[str, object] = {"alive": result.alive}
    if result.unknown_instance:
        out["unknown_instance"] = True
    return out


def _dispatch_activate(state: RouterState, args: dict[str, object]) -> dict[str, object]:
    color = args.get("color")
    instance_id = args.get("instance_id")
    if not isinstance(color, str) or not isinstance(instance_id, str):
        return {"activated": False, "reason": "bad_args"}
    result = state.activate(color, instance_id)
    out: dict[str, object] = {
        "activated": result.activated,
        "drain_window_seconds": result.drain_window_seconds,
    }
    if result.previous_color is not None:
        out["previous_color"] = result.previous_color
    if result.reason:
        out["reason"] = result.reason
    return out


def _dispatch_rollback(state: RouterState, args: dict[str, object]) -> dict[str, object]:
    color = args.get("color")
    instance_id = args.get("instance_id")
    if not isinstance(color, str) or not isinstance(instance_id, str):
        return {"rolled_back": False, "reason": "bad_args"}
    result = state.rollback(color, instance_id)
    out: dict[str, object] = {"rolled_back": result.rolled_back}
    if result.active_color is not None:
        out["active_color"] = result.active_color
    if result.reason:
        out["reason"] = result.reason
    return out


def _dispatch_status(state: RouterState) -> dict[str, object]:
    snap = state.status()
    return {
        "router_started_at": snap.router_started_at,
        "active_color": snap.active_color,
        "active_instance_id": snap.active_instance_id,
        "colors": [
            {
                "color": e.color,
                "port": e.port,
                "instance_id": e.instance_id,
                "status": e.status,
                "last_heartbeat": e.last_heartbeat,
                "streamable_port": e.streamable_port,
            }
            for e in snap.colors
        ],
        "drain_entries": [
            {
                "color": e.color,
                "port": e.port,
                "instance_id": e.instance_id,
                "status": e.status,
                "last_heartbeat": e.last_heartbeat,
                "streamable_port": e.streamable_port,
            }
            for e in snap.drain_entries
        ],
    }


async def run_router(
    *,
    solet: str,
    public_port: int = DEFAULT_PUBLIC_PORT,
    public_host: str = "127.0.0.1",
    socket_path: Path | None = None,
    runtime_dir: Path | None = None,
    drain_window_seconds: int = DEFAULT_DRAIN_WINDOW_SECONDS,
    heartbeat_timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    buffer_timeout: float = DEFAULT_BUFFER_TIMEOUT_SECONDS,
    bridge_port_watchdog_interval: float = DEFAULT_BRIDGE_PORT_WATCHDOG_INTERVAL_SECONDS,
    ready_event: asyncio.Event | None = None,
    streamable_public_port: int | None = None,
    streamable_public_host: str = "0.0.0.0",  # noqa: S104
) -> None:
    """Run the router until cancelled.

    The `ready_event` is set after both surfaces are bound — useful
    for smoke harnesses that spawn the router in-process and need
    to wait for liveness before driving it.

    ``runtime_dir`` overrides where the port-discovery files (and the
    default mgmt socket) live; ``None`` is the real ``~/.ananta/runtime``.

    BLG-04: ``streamable_public_port`` is opt-in (``None`` = today's
    behaviour, no second listener). When set, the router — a single
    always-up process, never itself color-duplicated — binds it and
    proxies to whichever color has reported a ``streamable_port``,
    replacing each color's own now-removed fixed-port bind as the thing
    that holds this port. Default host is ``0.0.0.0`` (not the main
    proxy's loopback-only default) because this port's whole purpose is
    external reachability (phone/Caddy), matching what the removed
    per-color listener bound.
    """

    state = RouterState(
        drain_window_seconds=drain_window_seconds,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
    )
    sock = socket_path or mgmt_socket_path(solet, runtime_dir)

    async def _public_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await _handle_public_request(
                reader, writer, state=state, buffer_timeout=buffer_timeout
            )
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass

    async def _streamable_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await _handle_streamable_public_request(
                reader, writer, state=state, buffer_timeout=buffer_timeout
            )
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass

    public_server = await asyncio.start_server(
        _public_client, host=public_host, port=public_port
    )
    streamable_server: asyncio.Server | None = None
    if streamable_public_port is not None:
        streamable_server = await asyncio.start_server(
            _streamable_client, host=streamable_public_host, port=streamable_public_port,
        )
        logger.info(
            "streamable: listening on %s:%d (solet=%s)",
            streamable_public_host,
            streamable_public_port,
            solet,
        )
    _write_port_discovery_files(solet, public_port, runtime_dir)
    logger.info(
        "public: listening on %s:%d (solet=%s)",
        public_host,
        public_port,
        solet,
    )
    mgmt = MgmtServer(sock, _make_dispatch(state))
    await mgmt.start()

    if ready_event is not None:
        ready_event.set()

    drain_task = asyncio.create_task(_drain_sweeper(state), name="drain_sweeper")
    hb_task = asyncio.create_task(_heartbeat_gc(state), name="heartbeat_gc")
    bridge_port_task = asyncio.create_task(
        _bridge_port_watchdog(
            solet, public_port, interval=bridge_port_watchdog_interval,
            runtime_dir=runtime_dir,
        ),
        name="bridge_port_watchdog",
    )

    try:
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(public_server)
            if streamable_server is not None:
                await stack.enter_async_context(streamable_server)
            serve_tasks = [
                asyncio.create_task(mgmt.serve_forever(), name="mgmt_serve"),
                asyncio.create_task(public_server.serve_forever(), name="public_serve"),
            ]
            if streamable_server is not None:
                serve_tasks.append(
                    asyncio.create_task(
                        streamable_server.serve_forever(), name="streamable_serve",
                    ),
                )
            try:
                await asyncio.gather(*serve_tasks)
            except asyncio.CancelledError:
                raise
    finally:
        drain_task.cancel()
        hb_task.cancel()
        bridge_port_task.cancel()
        with _SuppressCancel():
            await asyncio.gather(
                drain_task, hb_task, bridge_port_task, return_exceptions=True,
            )
        await mgmt.stop()


class _SuppressCancel:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, _exc: BaseException | None, _tb: object) -> bool:
        return exc_type is asyncio.CancelledError


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="local_blue_green.router",
        description="Local blue-green router for the solet (Slice C).",
    )
    parser.add_argument("--solet", required=True, help="Solet name (e.g. 'iris').")
    parser.add_argument(
        "--public-port", type=int, default=DEFAULT_PUBLIC_PORT,
        help=f"Public bridge port (default {DEFAULT_PUBLIC_PORT}).",
    )
    parser.add_argument(
        "--public-host", default="127.0.0.1",
        help="Bind host (default 127.0.0.1).",
    )
    parser.add_argument(
        "--socket-path", default=None,
        help=(
            "Override mgmt socket path "
            "(default ~/.ananta/runtime/<solet>.router.sock)."
        ),
    )
    parser.add_argument(
        "--runtime-dir", default=None,
        help=(
            "Override the runtime dir for the port-discovery files "
            "(default ~/.ananta/runtime; install_router passes its own "
            "--runtime-dir here so a sandboxed smoke never writes the real one)."
        ),
    )
    parser.add_argument(
        "--drain-window-seconds", type=int, default=DEFAULT_DRAIN_WINDOW_SECONDS,
        help="Drain window for old colors after activate.",
    )
    parser.add_argument(
        "--buffer-timeout-seconds", type=float, default=DEFAULT_BUFFER_TIMEOUT_SECONDS,
        help="Hold incoming requests for this long when no active color exists.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    parser.add_argument(
        "--streamable-public-port", type=int, default=None,
        help=(
            "BLG-04: external stable port for the streamable HTTP MCP "
            "transport, proxied to whichever color has reported a "
            "streamable port. Omitted (default) = no streamable listener, "
            "today's behaviour."
        ),
    )
    parser.add_argument(
        "--streamable-public-host", default="0.0.0.0",  # noqa: S104
        help="Streamable bind host (default 0.0.0.0 — external reachability).",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    socket_path = Path(args.socket_path) if args.socket_path else None
    runtime_dir = Path(args.runtime_dir) if args.runtime_dir else None

    async def _runner() -> None:
        ready = asyncio.Event()
        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, shutdown.set)
            except NotImplementedError:
                pass
        run_task = asyncio.create_task(
            run_router(
                solet=args.solet,
                public_port=args.public_port,
                public_host=args.public_host,
                socket_path=socket_path,
                runtime_dir=runtime_dir,
                drain_window_seconds=args.drain_window_seconds,
                buffer_timeout=args.buffer_timeout_seconds,
                ready_event=ready,
                streamable_public_port=args.streamable_public_port,
                streamable_public_host=args.streamable_public_host,
            ),
            name="run_router",
        )
        try:
            done = await asyncio.wait(
                {run_task, asyncio.create_task(shutdown.wait())},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done[1]:
                task.cancel()
            if run_task in done[0]:
                run_task.result()
            else:
                run_task.cancel()
                with _SuppressCancel():
                    await run_task
        finally:
            if not run_task.done():
                run_task.cancel()
                with _SuppressCancel():
                    await run_task

    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
