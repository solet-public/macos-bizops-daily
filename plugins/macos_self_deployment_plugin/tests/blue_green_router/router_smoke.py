"""Slice C router smoke — standalone, no pytest, no solet needed.

Spawns the router as an asyncio task within this process plus two
mock-color HTTP servers (blue on 8101, green on 8150). Drives the
mgmt-plane via the Unix socket and the public surface via httpx.

Covers the §3.1 deliverable checklist from the L3-implementation-plan
design record (dev-checkout workbench — not part of the shipped tree):

  1. register_color + activate + curl through 8100 → blue.
  2. Second register + activate(green) → curl routes to green.
  3. Mcp-Session-Id affinity:
     - new session initialized vs active=blue captures sid.
     - activate(green); same sid still routes to blue (drain).
     - wait > drain_window; same sid now routes to green
       (or 404 on missing-session).
  4. status() + rollback() round-trips.
  5. heartbeat() returns unknown_instance for unknown ids.

Drain window is overridden to 3 seconds; public port is moved off
the canonical 8100 to a free high port to avoid colliding with any
running solet.

Run: .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/blue_green_router/router_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
from macos_self_deployment_plugin.blue_green_router.router import run_router  # noqa: E402

logger = logging.getLogger("router_smoke")

SMOKE_DRAIN_SECONDS = 3
SMOKE_HEARTBEAT_TIMEOUT_SECONDS = 600
BLUE_PORT = 8101
GREEN_PORT = 8150


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


class _MockColor:
    """Asyncio TCP server pretending to be a solet's bridge.

    Responds to GET /probe with a fixed JSON body and a fresh
    Mcp-Session-Id when the incoming request did NOT carry one. If
    the incoming request carries an Mcp-Session-Id, echoes it back.
    """

    def __init__(self, color: str, port: int) -> None:
        self.color = color
        self.port = port
        self._server: asyncio.base_events.Server | None = None
        self.request_log: list[dict[str, object]] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, host="127.0.0.1", port=self.port
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _read_request_head(
        self,
        reader: asyncio.StreamReader,
    ) -> bytes | None:
        """Buffer the request head (up to ``\\r\\n\\r\\n``); return None on close or oversize."""
        head_buf = bytearray()
        while b"\r\n\r\n" not in head_buf:
            chunk = await reader.read(4096)
            if not chunk:
                return None
            head_buf.extend(chunk)
            if len(head_buf) > 16384:
                return None
        return bytes(head_buf).split(b"\r\n\r\n", 1)[0]

    @staticmethod
    def _extract_session_id(head: bytes) -> str | None:
        """Find the ``Mcp-Session-Id`` header in the request head, if present."""
        for raw in head.split(b"\r\n"):
            if b":" not in raw:
                continue
            name, _, value = raw.partition(b":")
            if name.lower() == b"mcp-session-id":
                return value.strip().decode()
        return None

    def _build_response_bytes(
        self,
        effective_sid: str,
        issued_sid: str | None,
    ) -> bytes:
        """Serialize the canned mock JSON response, optionally setting ``Mcp-Session-Id``."""
        body = json.dumps(
            {
                "color": self.color,
                "session_id": effective_sid,
                "echo": "ok",
            }
        ).encode()
        headers = [
            b"HTTP/1.1 200 OK",
            b"Content-Type: application/json",
            b"Content-Length: " + str(len(body)).encode(),
            b"Connection: close",
        ]
        if issued_sid is not None:
            headers.append(b"Mcp-Session-Id: " + issued_sid.encode())
        return b"\r\n".join(headers) + b"\r\n\r\n" + body

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        head = await self._read_request_head(reader)
        if head is None:
            writer.close()
            return
        session_id = self._extract_session_id(head)
        issued_sid = (
            None if session_id is not None
            else f"sid-{self.color}-{uuid.uuid4().hex[:8]}"
        )
        effective_sid = session_id or issued_sid or "unknown"
        response = self._build_response_bytes(effective_sid, issued_sid)
        writer.write(response)
        await writer.drain()
        writer.close()
        self.request_log.append(
            {"sid": session_id, "issued": issued_sid, "ts": time.time()}
        )


@asynccontextmanager
async def _router_running(socket_path: Path, public_port: int):
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
        ),
        name="router_smoke",
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


async def _curl_public(
    port: int, sid: str | None = None
) -> tuple[int, dict[str, str], dict[str, object]]:
    """Send a GET /probe through the router; return (status, headers, body)."""

    headers: dict[str, str] = {}
    if sid is not None:
        headers["Mcp-Session-Id"] = sid
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"http://127.0.0.1:{port}/probe", headers=headers)
        return resp.status_code, dict(resp.headers), resp.json()


async def case_basic_routing(
    mgmt: _Mgmt,
    public_port: int,
) -> bool:
    print("\n[case_basic_routing] register blue + activate + curl-through")
    r1 = await mgmt.call("register_color", {"port": BLUE_PORT, "color": "blue", "instance_id": "i-blue"})
    if not _stamp("register blue", r1.get("accepted") is True, repr(r1)):
        return False
    r2 = await mgmt.call("activate", {"color": "blue", "instance_id": "i-blue"})
    if not _stamp("activate blue", r2.get("activated") is True, repr(r2)):
        return False
    status, _headers, body = await _curl_public(public_port)
    ok = status == 200 and body.get("color") == "blue"
    _stamp(f"curl→blue (status={status})", ok, repr(body))
    return ok


async def case_swap_to_green(
    mgmt: _Mgmt,
    public_port: int,
) -> bool:
    print("\n[case_swap_to_green] register green + activate(green) + curl")
    r1 = await mgmt.call("register_color", {"port": GREEN_PORT, "color": "green", "instance_id": "i-green"})
    if not _stamp("register green", r1.get("accepted") is True, repr(r1)):
        return False
    r2 = await mgmt.call("activate", {"color": "green", "instance_id": "i-green"})
    if not _stamp("activate green", r2.get("activated") is True and r2.get("previous_color") == "blue", repr(r2)):
        return False
    status, _headers, body = await _curl_public(public_port)
    ok = status == 200 and body.get("color") == "green"
    _stamp(f"curl→green (status={status})", ok, repr(body))
    return ok


async def _affinity_step_init_blue_session(
    public_port: int,
) -> tuple[bool, str | None]:
    """Step 1: initialize a session against active=blue; capture issued sid."""
    status, headers, body = await _curl_public(public_port)
    sid = headers.get("mcp-session-id") or body.get("session_id")
    ok_init = status == 200 and body.get("color") == "blue" and isinstance(sid, str)
    _stamp(f"init session vs active=blue (sid={sid!r})", ok_init)
    return ok_init, sid if isinstance(sid, str) else None


async def _affinity_step_stickiness_during_drain(
    public_port: int,
    sid: str,
) -> bool:
    """Step 2: same sid should still hit blue while blue is draining."""
    status, _, body = await _curl_public(public_port, sid=sid)
    ok_stick = status == 200 and body.get("color") == "blue"
    _stamp("same sid post-activate(green) still routes to blue (drain)", ok_stick, repr(body))
    return ok_stick


async def _affinity_step_after_drain(
    public_port: int,
    sid: str,
) -> bool:
    """Step 3: past drain window — same sid → 503 or color=green per §2.5 Option α."""
    status, _, body_or_none = await _curl_public(public_port, sid=sid)
    ok_drain_or_green = (status == 503) or (
        status == 200 and body_or_none.get("color") == "green"
    )
    _stamp(
        f"same sid after drain → 503 or green (status={status})",
        ok_drain_or_green,
        repr(body_or_none),
    )
    return ok_drain_or_green


async def _affinity_step_fresh_request_routes_green(
    public_port: int,
) -> bool:
    """Step 4: fresh request with no sid → routes to active=green."""
    status, _, body = await _curl_public(public_port)
    ok_fresh = status == 200 and body.get("color") == "green"
    _stamp(f"fresh (no sid) → green (status={status})", ok_fresh, repr(body))
    return ok_fresh


async def case_session_affinity(
    mgmt: _Mgmt,
    public_port: int,
) -> bool:
    print("\n[case_session_affinity] new sid bound to current active, survives swap during drain")
    # Reset to a known state: activate blue first.
    await mgmt.call("activate", {"color": "blue", "instance_id": "i-blue"})

    ok_init, sid = await _affinity_step_init_blue_session(public_port)
    if not ok_init or sid is None:
        return False

    # Give the router a beat to record the response-side session mapping.
    await asyncio.sleep(0.1)

    # Activate green; sid still tied to blue during drain.
    await mgmt.call("activate", {"color": "green", "instance_id": "i-green"})
    ok_stick = await _affinity_step_stickiness_during_drain(public_port, sid)

    # Wait past drain window.
    await asyncio.sleep(SMOKE_DRAIN_SECONDS + 1.0)
    ok_drain_or_green = await _affinity_step_after_drain(public_port, sid)

    ok_fresh = await _affinity_step_fresh_request_routes_green(public_port)

    return ok_init and ok_stick and ok_drain_or_green and ok_fresh


async def case_status_and_rollback(mgmt: _Mgmt) -> bool:
    print("\n[case_status_and_rollback] exact-instance rollback to drain entry")
    # Build TWO still-draining blue instances. A color-only rollback would pick
    # the first, SIGTERM'd blue; this request must restore the newest blue.
    await mgmt.call("activate", {"color": "blue", "instance_id": "i-blue"})
    await mgmt.call("activate", {"color": "green", "instance_id": "i-green"})
    newest_blue_id = "i-blue-newest"
    await mgmt.call("register_color", {"color": "blue", "port": 8102, "instance_id": newest_blue_id})
    await mgmt.call("activate", {"color": "blue", "instance_id": newest_blue_id})
    await mgmt.call("activate", {"color": "green", "instance_id": "i-green"})
    status_resp = await mgmt.call("status")
    has_active = status_resp.get("active_color") == "green"
    blue_drain_ids = [
        e.get("instance_id")
        for e in status_resp.get("drain_entries", [])
        if e.get("color") == "blue"
    ]
    has_two_blue_drains = {"i-blue", newest_blue_id}.issubset(blue_drain_ids)
    _stamp("status: active=green", has_active, repr(status_resp.get("active_color")))
    _stamp(
        "status: two blue instances in drain_entries",
        has_two_blue_drains,
        repr(status_resp.get("drain_entries")),
    )

    rb = await mgmt.call("rollback", {"color": "blue", "instance_id": newest_blue_id})
    ok_rb = rb.get("rolled_back") is True and rb.get("active_color") == "blue"
    post_rollback = await mgmt.call("status")
    restored_exact_instance = post_rollback.get("active_instance_id") == newest_blue_id
    _stamp("rollback to newest blue", ok_rb, repr(rb))
    _stamp(
        "rollback restored requested blue instance, not first same-color drain",
        restored_exact_instance,
        repr(post_rollback),
    )

    # After rollback to blue, GREEN is now in drain (it was the active
    # color at the rollback moment). Try to rollback to itself: blue is
    # now active, not draining, so this should be rejected.
    rb_self = await mgmt.call(
        "rollback", {"color": "blue", "instance_id": newest_blue_id}
    )
    ok_rb_self = rb_self.get("rolled_back") is False
    _stamp(
        "rollback to blue when already active → rejected",
        ok_rb_self,
        repr(rb_self),
    )

    return (
        has_active
        and has_two_blue_drains
        and ok_rb
        and restored_exact_instance
        and ok_rb_self
    )


async def case_heartbeat(mgmt: _Mgmt) -> bool:
    print("\n[case_heartbeat] known vs unknown instance")
    r1 = await mgmt.call("heartbeat", {"instance_id": "i-blue"})
    ok1 = r1.get("alive") is True
    _stamp("heartbeat i-blue alive", ok1, repr(r1))
    r2 = await mgmt.call("heartbeat", {"instance_id": "i-ghost"})
    ok2 = r2.get("alive") is False and r2.get("unknown_instance") is True
    _stamp("heartbeat i-ghost unknown_instance", ok2, repr(r2))
    return ok1 and ok2


async def case_no_band_enforcement(mgmt: _Mgmt) -> bool:
    print("\n[case_no_band_enforcement] Slice 3 removed port bands — any port accepted")
    # Slice 3 of the bridge-port-routing design eliminated the hardcoded
    # 8101-8198 port bands (child ports are dynamic bind(0) now), so the
    # router no longer rejects "out-of-band" ports. A formerly out-of-band
    # port like 8200 must now register cleanly; this case regresses if band
    # validation is ever reintroduced.
    r = await mgmt.call(
        "register_color",
        {"port": 8200, "color": "blue", "instance_id": "i-anyport"},
    )
    ok = r.get("accepted") is True
    _stamp("register blue:8200 accepted (no band enforcement)", ok, repr(r))
    return ok


async def main_async() -> int:
    logging.basicConfig(level=logging.WARNING)
    public_port = _pick_free_port()
    tmpdir = Path(tempfile.mkdtemp(prefix="bg-router-smoke-"))
    socket_path = tmpdir / "router.sock"
    blue = _MockColor("blue", BLUE_PORT)
    green = _MockColor("green", GREEN_PORT)
    await blue.start()
    await green.start()
    print(
        f"router_smoke: public_port={public_port} socket={socket_path} "
        f"drain={SMOKE_DRAIN_SECONDS}s"
    )
    try:
        async with _router_running(socket_path, public_port):
            mgmt = _Mgmt(socket_path)
            results = [
                ("basic_routing", await case_basic_routing(mgmt, public_port)),
                ("swap_to_green", await case_swap_to_green(mgmt, public_port)),
                ("session_affinity", await case_session_affinity(mgmt, public_port)),
                ("status_and_rollback", await case_status_and_rollback(mgmt)),
                ("heartbeat", await case_heartbeat(mgmt)),
                ("no_band_enforcement", await case_no_band_enforcement(mgmt)),
            ]
    finally:
        await blue.stop()
        await green.stop()

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
