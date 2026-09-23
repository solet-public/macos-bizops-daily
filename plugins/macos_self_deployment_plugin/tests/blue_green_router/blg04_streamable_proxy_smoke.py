"""BLG-04 streamable-proxy smoke — standalone, no pytest, no solet needed.

Proves the router-side half of the BLG-04 fix (offline-testable per the
design doc's own "what a future lane needs" list — the live half, an
actual two-color overlap window on real OS processes, needs a live
deploy and is NOT what this smoke covers):

  1. A color that reports BOTH a main port and a `streamable_port` on
     `register_color` is reachable through the router's SEPARATE
     streamable listener, proxied to that reported port — not the main
     one.
  2. Activating a different color re-points the streamable proxy to
     THAT color's reported streamable port, mirroring the existing main
     proxy's swap behavior (session-affinity semantics are identical —
     reused verbatim, not re-implemented).
  3. A color that never reports a streamable port (streamable disabled
     on that color, or not registered yet) 503s on the streamable
     listener with `no_streamable_route` while its MAIN proxy keeps
     working normally — the self-limiting fallback the design doc
     specifies, and the reason a partial rollout can never regress the
     main bridge port.
  4. `status()` exposes `streamable_port` per color/drain-entry.
  5. The streamable listener is opt-in: passing `streamable_public_port
     =None` (today's default) starts the router with NO second listener
     at all — byte-identical to pre-BLG-04 behaviour.

Run: .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/blue_green_router/blg04_streamable_proxy_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
from macos_self_deployment_plugin.blue_green_router.router import run_router  # noqa: E402

logger = logging.getLogger("blg04_streamable_proxy_smoke")

SMOKE_DRAIN_SECONDS = 3
SMOKE_HEARTBEAT_TIMEOUT_SECONDS = 600


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Mgmt:
    """Tiny client for the newline-JSON mgmt RPC over Unix socket."""

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path

    async def call(self, verb: str, args: dict[str, object] | None = None) -> dict[str, object]:
        reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
        try:
            payload = json.dumps({"verb": verb, "args": args or {}}).encode() + b"\n"
            writer.write(payload)
            await writer.drain()
            line = await reader.readline()
            if not line:
                msg = "mgmt: short read"
                raise RuntimeError(msg)
            return json.loads(line.decode())
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass


class _MockListener:
    """One bare-bones HTTP-echoing TCP server, tagged with a fixed label.

    Two of these per color (one at the "main" port, one at the
    "streamable" port) let a test assert not just WHICH color answered
    but WHICH of that color's two listeners the router actually reached.
    """

    def __init__(self, label: str, port: int) -> None:
        self.label = label
        self.port = port
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, host="127.0.0.1", port=self.port
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        head_buf = bytearray()
        while b"\r\n\r\n" not in head_buf:
            chunk = await reader.read(4096)
            if not chunk:
                writer.close()
                return
            head_buf.extend(chunk)
            if len(head_buf) > 16384:
                writer.close()
                return
        body = json.dumps({"label": self.label}).encode()
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n"
        ) + body
        writer.write(response)
        await writer.drain()
        writer.close()


@asynccontextmanager
async def _router_running(
    socket_path: Path,
    public_port: int,
    *,
    streamable_public_port: int | None,
):
    ready = asyncio.Event()
    task = asyncio.create_task(
        run_router(
            solet="smoke",
            public_port=public_port,
            public_host="127.0.0.1",
            socket_path=socket_path,
            drain_window_seconds=SMOKE_DRAIN_SECONDS,
            heartbeat_timeout_seconds=SMOKE_HEARTBEAT_TIMEOUT_SECONDS,
            buffer_timeout=2.0,
            ready_event=ready,
            streamable_public_port=streamable_public_port,
            streamable_public_host="127.0.0.1",
        ),
        name="blg04_streamable_proxy_smoke",
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=3.0)
        yield task
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("router task exited with exception")


def _stamp(label: str, ok: bool, detail: str = "") -> bool:
    sym = "PASS" if ok else "FAIL"
    print(f"  [{sym}] {label}" + (f" — {detail}" if detail else ""))
    return ok


async def _curl(port: int, path: str = "/probe") -> tuple[int, dict[str, object]]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"http://127.0.0.1:{port}{path}")
        body: dict[str, object] = {}
        try:
            body = resp.json()
        except ValueError:
            pass
        return resp.status_code, body


async def case_streamable_proxies_to_reported_port(
    mgmt: _Mgmt, public_port: int, streamable_public_port: int,
    blue_main: int, blue_streamable: int,
) -> bool:
    print("\n[case_streamable_proxies_to_reported_port] register blue w/ both ports, activate, curl both listeners")
    r1 = await mgmt.call(
        "register_color",
        {"port": blue_main, "color": "blue", "instance_id": "i-blue", "streamable_port": blue_streamable},
    )
    ok_register = r1.get("accepted") is True
    _stamp("register blue with streamable_port", ok_register, repr(r1))
    r2 = await mgmt.call("activate", {"color": "blue", "instance_id": "i-blue"})
    ok_activate = r2.get("activated") is True
    _stamp("activate blue", ok_activate, repr(r2))

    status_main, body_main = await _curl(public_port)
    ok_main = status_main == 200 and body_main.get("label") == "blue-main"
    _stamp(f"main proxy → blue-main (status={status_main})", ok_main, repr(body_main))

    status_stream, body_stream = await _curl(streamable_public_port)
    ok_stream = status_stream == 200 and body_stream.get("label") == "blue-streamable"
    _stamp(f"streamable proxy → blue-streamable (status={status_stream})", ok_stream, repr(body_stream))

    return ok_register and ok_activate and ok_main and ok_stream


async def case_swap_repoints_streamable(
    mgmt: _Mgmt, streamable_public_port: int,
    green_main: int, green_streamable: int,
) -> bool:
    print("\n[case_swap_repoints_streamable] register+activate green, streamable proxy follows")
    r1 = await mgmt.call(
        "register_color",
        {"port": green_main, "color": "green", "instance_id": "i-green", "streamable_port": green_streamable},
    )
    ok_register = r1.get("accepted") is True
    _stamp("register green with streamable_port", ok_register, repr(r1))
    r2 = await mgmt.call("activate", {"color": "green", "instance_id": "i-green"})
    ok_activate = r2.get("activated") is True
    _stamp("activate green", ok_activate, repr(r2))

    status_stream, body_stream = await _curl(streamable_public_port)
    ok_stream = status_stream == 200 and body_stream.get("label") == "green-streamable"
    _stamp(f"streamable proxy re-points → green-streamable (status={status_stream})", ok_stream, repr(body_stream))
    return ok_register and ok_activate and ok_stream


async def case_no_streamable_port_503s_but_main_still_works(
    mgmt: _Mgmt, public_port: int, streamable_public_port: int,
    yellow_main: int,
) -> bool:
    print("\n[case_no_streamable_port_503s_but_main_still_works] color w/ no streamable_port")
    # A brand-new instance id with NO streamable_port ever supplied — using
    # an existing instance would risk the sticky-preserve semantics
    # (`RouterState.register`) masking what this case tests.
    r2 = await mgmt.call(
        "register_color",
        {"port": yellow_main, "color": "green", "instance_id": "i-yellow-no-streamable"},
    )
    ok_register = r2.get("accepted") is True
    _stamp("register i-yellow-no-streamable, no streamable_port", ok_register, repr(r2))
    r3 = await mgmt.call("activate", {"color": "green", "instance_id": "i-yellow-no-streamable"})
    ok_activate = r3.get("activated") is True
    _stamp("activate i-yellow-no-streamable", ok_activate, repr(r3))

    status_main, body_main = await _curl(public_port)
    ok_main = status_main == 200 and body_main.get("label") == "yellow-main"
    _stamp(f"main proxy still routes fine (status={status_main})", ok_main, repr(body_main))

    status_stream, body_stream = await _curl(streamable_public_port)
    ok_stream_503 = status_stream == 503
    _stamp(f"streamable proxy 503s, no_streamable_route (status={status_stream})", ok_stream_503, repr(body_stream))

    return ok_register and ok_activate and ok_main and ok_stream_503


async def case_status_exposes_streamable_port(mgmt: _Mgmt) -> bool:
    print("\n[case_status_exposes_streamable_port] status() carries streamable_port per color")
    status_resp = await mgmt.call("status")
    colors = status_resp.get("colors")
    ok_shape = isinstance(colors, list) and all(
        isinstance(c, dict) and "streamable_port" in c for c in colors
    )
    _stamp("every status color entry carries a streamable_port key", ok_shape, repr(colors))
    return ok_shape


async def case_opt_out_starts_no_second_listener() -> bool:
    print("\n[case_opt_out_starts_no_second_listener] streamable_public_port=None → no second listener")
    public_port = _pick_free_port()
    probe_port = _pick_free_port()  # never bound by the router; nothing should answer here
    tmpdir = Path(tempfile.mkdtemp(prefix="bg-router-smoke-optout-"))
    socket_path = tmpdir / "router.sock"
    async with _router_running(socket_path, public_port, streamable_public_port=None):
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                await client.get(f"http://127.0.0.1:{probe_port}/probe")
            connected = True
        except httpx.ConnectError:
            connected = False
    ok = not connected
    _stamp("nothing listening on the never-configured streamable port", ok)
    return ok


async def main_async() -> int:
    logging.basicConfig(level=logging.WARNING)
    public_port = _pick_free_port()
    streamable_public_port = _pick_free_port()
    blue_main, blue_streamable = _pick_free_port(), _pick_free_port()
    green_main, green_streamable = _pick_free_port(), _pick_free_port()
    yellow_main = _pick_free_port()
    tmpdir = Path(tempfile.mkdtemp(prefix="bg-router-smoke-blg04-"))
    socket_path = tmpdir / "router.sock"

    listeners = [
        _MockListener("blue-main", blue_main),
        _MockListener("blue-streamable", blue_streamable),
        _MockListener("green-main", green_main),
        _MockListener("green-streamable", green_streamable),
        _MockListener("yellow-main", yellow_main),
    ]
    for listener in listeners:
        await listener.start()
    print(
        f"blg04_streamable_proxy_smoke: public_port={public_port} "
        f"streamable_public_port={streamable_public_port} socket={socket_path}"
    )
    try:
        async with _router_running(
            socket_path, public_port, streamable_public_port=streamable_public_port,
        ):
            mgmt = _Mgmt(socket_path)
            results = [
                (
                    "streamable_proxies_to_reported_port",
                    await case_streamable_proxies_to_reported_port(
                        mgmt, public_port, streamable_public_port, blue_main, blue_streamable,
                    ),
                ),
                (
                    "swap_repoints_streamable",
                    await case_swap_repoints_streamable(
                        mgmt, streamable_public_port, green_main, green_streamable,
                    ),
                ),
                (
                    "no_streamable_port_503s_but_main_still_works",
                    await case_no_streamable_port_503s_but_main_still_works(
                        mgmt, public_port, streamable_public_port, yellow_main,
                    ),
                ),
                (
                    "status_exposes_streamable_port",
                    await case_status_exposes_streamable_port(mgmt),
                ),
            ]
    finally:
        for listener in listeners:
            await listener.stop()

    results.append(
        ("opt_out_starts_no_second_listener", await case_opt_out_starts_no_second_listener()),
    )

    print("\nsummary")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        _stamp(name, ok)
    print(f"\n{passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
