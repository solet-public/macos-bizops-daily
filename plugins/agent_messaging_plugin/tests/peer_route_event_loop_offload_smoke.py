#!/usr/bin/env python3
"""Peer routes must not block the bridge server's event loop (iss_87aa81c4).

``peer/send`` ran ``_peer_send_impl`` inline in its ``async def`` handler.
That reaches ``dispatch_peer_send`` → ``drive_on_delivery`` → the tmux
driver's ``_wait_for_paste_stable`` (``time.sleep`` + ``subprocess.run``, up to
10 s per Claude wake), so every send froze the loop that serves every other
bridge's ``/events`` long-poll and ``/health``. The same shape existed on
``peer/send_by_name``, ``peer/claim_role`` (handover notice → dispatch),
``peer/register`` (tmux/ps host probes + an autonomic claim's handover) and
the streamable ``POST`` (``tools/call`` → dispatch).

Each case replaces the blocking step with a fake that sleeps ``SLOW_S`` and
measures, on a REAL asyncio loop, what that sleep does to concurrent work:

  1-3. ``peer/send`` / ``peer/send_by_name`` / ``peer/claim_role`` — a
       ``/health`` request issued ``PROBE_AT_S`` into the slow call answers
       within ``MAX_LAG_S``, and the slow route's response comes back
       unchanged (status + body).
  4.   ``peer/register`` — same, with the sleep inside the register body.
  5.   ``peer/register`` — two concurrent registrations never overlap inside
       the body (the check-then-act the loop used to serialize for free).
  6.   streamable ``POST`` — the loop keeps servicing timers while the
       dispatch runs.
  7.   thread hop — a real ``BridgeSessionManager`` long-poll awaiting on the
       loop wakes promptly when ``append_event`` runs on a worker thread, which
       is where the offloaded dispatch now appends from.

On the pre-fix tree cases 1-4 and 6 fail: ``/health`` (or the loop timer) is
delayed by roughly ``SLOW_S``.

Run:

    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/peer_route_event_loop_offload_smoke.py

Exits 0 on success, 1 on first failure with a labeled message.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"),
)

from agent_messaging_plugin import http_routes  # noqa: E402
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.mcp_streamable import router as streamable_router  # noqa: E402

SLOW_S = 2.0
PROBE_AT_S = 0.2
MAX_LAG_S = 0.5
REGISTER_HOLD_S = 0.3
BASE_URL = "http://bridge.test"


def _fail(label: str, detail: str) -> None:
    print(f"FAIL: {label}: {detail}", file=sys.stderr)
    sys.exit(1)


def _ok(label: str) -> None:
    print(f"  OK: {label}")


@contextmanager
def _patched(module: Any, name: str, replacement: object) -> Generator[None]:
    original = getattr(module, name)
    setattr(module, name, replacement)
    try:
        yield
    finally:
        setattr(module, name, original)


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _solet: "sess-smoke",
        idle_timeout_s=600,
        max_pending_events=100,
        long_poll_timeout_s=5,
    )


def _build_app(bridge_manager: BridgeSessionManager) -> FastAPI:
    app = FastAPI()
    stub = object()
    http_routes.register_routes(
        app,
        bridge_manager=bridge_manager,
        peer_registry=stub,  # type: ignore[arg-type]
        platform_surface=stub,  # type: ignore[arg-type]
        agent_messaging_service=stub,
        config={"long_poll_timeout_seconds": 5},
    )
    return app


def _slow_route_response(route: str) -> Callable[..., JSONResponse]:
    def _slow(**_kwargs: object) -> JSONResponse:
        time.sleep(SLOW_S)
        return JSONResponse(content={"fake": route}, status_code=207)

    return _slow


async def _health_lag_during(
    client: httpx.AsyncClient,
    slow_request: Awaitable[httpx.Response],
) -> tuple[float, httpx.Response]:
    """Issue ``/health`` PROBE_AT_S after ``slow_request`` starts; return its lag.

    Lag is measured from the INTENDED issue time, so a blocked loop that
    delays the probe's own timer is counted, not hidden.
    """
    started = time.perf_counter()
    slow_task = asyncio.ensure_future(slow_request)

    async def _probe() -> float:
        await asyncio.sleep(PROBE_AT_S)
        response = await client.get("/api/v1/bridge/health")
        if response.status_code != 200:
            raise AssertionError(f"/health answered {response.status_code}")
        return time.perf_counter() - (started + PROBE_AT_S)

    lag = await _probe()
    return lag, await slow_task


async def _case_route_does_not_block(label: str, impl_name: str, path: str) -> None:
    bridge_manager = _bridge_manager()
    app = _build_app(bridge_manager)
    transport = httpx.ASGITransport(app=app)
    with _patched(http_routes, impl_name, _slow_route_response(impl_name)):
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            lag, slow = await _health_lag_during(
                client,
                client.post(path, json=_route_body(path)),
            )
    if lag > MAX_LAG_S:
        _fail(label, f"/health was delayed {lag:.2f}s by a {SLOW_S}s {impl_name}")
    if slow.status_code != 207 or slow.json() != {"fake": impl_name}:
        _fail(label, f"route response changed: {slow.status_code} {slow.text}")
    _ok(f"{label} — /health lag {lag:.3f}s during a {SLOW_S}s call, response unchanged")


def _route_body(path: str) -> dict[str, object]:
    if path.endswith("/peer/send"):
        return {"peer_id": "claude_code", "content": [{"type": "text", "text": "x"}]}
    if path.endswith("/peer/send_by_name"):
        return {"name": "Some-Role", "content": "x"}
    if path.endswith("/peer/claim_role"):
        return {"name": "Some-Role"}
    return {
        "agent_id": "claude_code",
        "agent_instance_id": "agi-smoke",
        "agent_session_id": "ases-smoke",
    }


def _conflict_refusal() -> JSONResponse:
    return JSONResponse(content={"code": "fake_conflict"}, status_code=409)


async def case_4_register_does_not_block() -> None:
    bridge_manager = _bridge_manager()
    bridge = bridge_manager.open(solet_name="")
    app = _build_app(bridge_manager)
    transport = httpx.ASGITransport(app=app)

    def _slow_conflict(**_kwargs: object) -> JSONResponse:
        time.sleep(SLOW_S)
        return _conflict_refusal()

    path = f"/api/v1/bridge/{bridge.bridge_id}/peer/register"
    with _patched(http_routes, "_session_id_conflict", _slow_conflict):
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            lag, slow = await _health_lag_during(
                client,
                client.post(path, json=_route_body(path)),
            )
    label = "case 4 (peer/register)"
    if lag > MAX_LAG_S:
        _fail(label, f"/health was delayed {lag:.2f}s by a {SLOW_S}s register body")
    if slow.status_code != 409 or slow.json() != {"code": "fake_conflict"}:
        _fail(label, f"route response changed: {slow.status_code} {slow.text}")
    _ok(f"{label} — /health lag {lag:.3f}s during a {SLOW_S}s register body")


async def case_5_register_bodies_never_overlap() -> None:
    bridge_manager = _bridge_manager()
    bridges = [bridge_manager.open(solet_name="") for _ in range(2)]
    app = _build_app(bridge_manager)
    transport = httpx.ASGITransport(app=app)
    counter_lock = threading.Lock()
    inside = 0
    peak = 0

    def _tracking_conflict(**_kwargs: object) -> JSONResponse:
        nonlocal inside, peak
        with counter_lock:
            inside += 1
            peak = max(peak, inside)
        time.sleep(REGISTER_HOLD_S)
        with counter_lock:
            inside -= 1
        return _conflict_refusal()

    with _patched(http_routes, "_session_id_conflict", _tracking_conflict):
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            responses = await asyncio.gather(*(
                client.post(
                    f"/api/v1/bridge/{bridge.bridge_id}/peer/register",
                    json=_route_body("/peer/register"),
                )
                for bridge in bridges
            ))
    label = "case 5 (peer/register serialization)"
    if peak != 1:
        _fail(label, f"{peak} register bodies ran concurrently; expected 1")
    if any(response.status_code != 409 for response in responses):
        _fail(label, f"unexpected statuses {[r.status_code for r in responses]}")
    _ok(f"{label} — concurrent registrations ran one at a time")


async def _loop_lag_during(slow: Awaitable[object]) -> tuple[float, object]:
    started = time.perf_counter()
    slow_task = asyncio.ensure_future(slow)
    await asyncio.sleep(PROBE_AT_S)
    lag = time.perf_counter() - (started + PROBE_AT_S)
    return lag, await slow_task


async def case_6_streamable_post_does_not_block() -> None:
    envelope = object()
    session = object()
    seen: list[tuple[object, object]] = []

    async def _preconditions(_request: object, **_kwargs: object) -> tuple[None, object]:
        return None, envelope

    def _resolve(_request: object, _envelope: object, **_kwargs: object) -> object:
        return session

    def _slow_dispatch(got_envelope: object, got_session: object, _ctx: object) -> Response:
        seen.append((got_envelope, got_session))
        time.sleep(SLOW_S)
        return Response(status_code=202)

    with (
        _patched(streamable_router, "_validate_post_preconditions", _preconditions),
        _patched(streamable_router, "_resolve_session", _resolve),
        _patched(streamable_router, "_dispatch_and_build_response", _slow_dispatch),
    ):
        lag, response = await _loop_lag_during(
            streamable_router._handle_post(  # noqa: SLF001 — the route body under test
                None,  # type: ignore[arg-type]
                session_manager=None,  # type: ignore[arg-type]
                bearer_verifier=None,  # type: ignore[arg-type]
                dispatch_ctx=None,  # type: ignore[arg-type]
                allowed_origins=(),
            ),
        )
    label = "case 6 (streamable POST)"
    if lag > MAX_LAG_S:
        _fail(label, f"loop timer was delayed {lag:.2f}s by a {SLOW_S}s dispatch")
    if not isinstance(response, Response) or response.status_code != 202:
        _fail(label, f"dispatch response changed: {response!r}")
    if seen != [(envelope, session)]:
        _fail(label, f"dispatch got the wrong arguments: {seen!r}")
    _ok(f"{label} — loop timer lag {lag:.3f}s during a {SLOW_S}s dispatch")


async def case_7_worker_thread_append_wakes_long_poll() -> None:
    bridge_manager = _bridge_manager()
    bridge = bridge_manager.open(solet_name="")
    waiter = asyncio.ensure_future(
        bridge_manager.events_after(bridge.bridge_id, -1, timeout_s=5),
    )
    await asyncio.sleep(0.1)
    started = time.perf_counter()
    await asyncio.to_thread(
        bridge_manager.append_event,
        bridge.bridge_id,
        "post_message",
        "hello from a worker thread",
    )
    _acked, events = await waiter
    elapsed = time.perf_counter() - started
    label = "case 7 (worker-thread append → loop long-poll)"
    if [event.content for event in events] != ["hello from a worker thread"]:
        _fail(label, f"long-poll returned {events!r}")
    if elapsed > MAX_LAG_S:
        _fail(label, f"long-poll woke {elapsed:.2f}s after the append")
    _ok(f"{label} — woke {elapsed:.3f}s after the append")


async def _main() -> None:
    bid = "agc-unused"
    await _case_route_does_not_block(
        "case 1 (peer/send)", "_peer_send_impl", f"/api/v1/bridge/{bid}/peer/send",
    )
    await _case_route_does_not_block(
        "case 2 (peer/send_by_name)",
        "_peer_send_by_name_impl",
        f"/api/v1/bridge/{bid}/peer/send_by_name",
    )
    await _case_route_does_not_block(
        "case 3 (peer/claim_role)",
        "_peer_claim_role_impl",
        f"/api/v1/bridge/{bid}/peer/claim_role",
    )
    await case_4_register_does_not_block()
    await case_5_register_bodies_never_overlap()
    await case_6_streamable_post_does_not_block()
    await case_7_worker_thread_append_wakes_long_poll()


def main() -> int:
    print("peer_route_event_loop_offload_smoke (iss_87aa81c4)")
    asyncio.run(_main())
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
