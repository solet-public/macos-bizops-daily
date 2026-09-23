#!/usr/bin/env python3
"""Offline gate for the candidate bridge-port/router-registration path.

The gate starts the real blue-green router on a private Unix socket, drives the
real heartbeat lifecycle, and invokes ``ActionQueuePoller._poll_once`` directly
so the batch-claim and serial-dispatch semantics are production semantics. It
never starts a Solet or touches a live runtime file.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ananta.core.actions.action_queue_poller import ActionQueuePoller  # noqa: E402
from macos_self_deployment_plugin import heartbeat_lifecycle  # noqa: E402
from macos_self_deployment_plugin.blue_green_router import router as router_module  # noqa: E402
from macos_self_deployment_plugin.constants import (  # noqa: E402
    DEFAULT_TRANSIENT_STATE_BUDGET_SECONDS,
    FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED,
)
from macos_self_deployment_plugin.router_client import RouterClient  # noqa: E402

_SMOKE_BUDGET_SECONDS = 1.25
_SMOKE_SLACK_SECONDS = 2.0
_START_INTERFACE = "plugin::agent_messaging_plugin::start_interface"


class _FailureError(RuntimeError):
    """Assertion failure that preserves cleanup in the caller's finalizer."""


def _expect(condition: object, label: str) -> None:
    if not condition:
        raise _FailureError(label)
    print(f"  PASS  {label}")


def _private_scratch() -> Path:
    root = Path.home() / ".ananta" / "offline-boot-gate"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=root))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


class _RouterHarness:
    """Run the production router with all router-created files private."""

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.socket_path = runtime_dir / "candidate.router.sock"
        self._public_port = _free_port()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Future[None] | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._stop = loop.create_future()
            ready = asyncio.Event()
            with patch.object(router_module, "_runtime_dir", return_value=self.runtime_dir):
                task = loop.create_task(
                    router_module.run_router(
                        solet="candidate",
                        public_port=self._public_port,
                        socket_path=self.socket_path,
                        ready_event=ready,
                        heartbeat_timeout_seconds=300,
                    )
                )

                async def _watch_ready() -> None:
                    await ready.wait()
                    self._ready.set()

                loop.create_task(_watch_ready())
                try:
                    loop.run_until_complete(self._stop)
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, BaseException):
                        loop.run_until_complete(task)
                    loop.close()

        self._thread = threading.Thread(target=_run, name="offline-router", daemon=True)
        self._thread.start()
        _expect(self._ready.wait(timeout=5.0), "private router became ready")

    def stop(self) -> None:
        if self._loop is None or self._stop is None or self._thread is None:
            return

        def _finish() -> None:
            if not self._stop.done():
                self._stop.set_result(None)

        self._loop.call_soon_threadsafe(_finish)
        self._thread.join(timeout=5.0)
        _expect(not self._thread.is_alive(), "private router stopped")


class _PollerHarness:
    """Minimal collaborators around the real poller's batch/serial method."""

    def __init__(
        self,
        actions: list[SimpleNamespace],
        handler: Callable[[SimpleNamespace], Awaitable[None]],
    ) -> None:
        self._actions = actions
        self._handler = handler
        self.claimed: list[str] = []
        self.dispatched: list[str] = []
        self.total_actions_processed = 0
        self._last_observed_queue_depth = 0

    async def _get_queued_actions(self) -> list[SimpleNamespace]:
        self._last_observed_queue_depth = len(self._actions)
        return self._actions

    def _mark_action_processing(self, action_id: str) -> bool:
        self.claimed.append(action_id)
        return True

    async def _process_action(self, action: SimpleNamespace) -> None:
        self.dispatched.append(action.id)
        await self._handler(action)

    async def run_once(self) -> None:
        await ActionQueuePoller._poll_once(self)  # type: ignore[arg-type]


def _start_heartbeat(
    client: RouterClient,
    port_lookup: Callable[[], int | None],
    instance_id: str,
) -> tuple[threading.Event, list[str], threading.Thread]:
    stop = threading.Event()
    tokens: list[str] = []

    def _record_token(token: str) -> None:
        tokens.append(token)

    thread = threading.Thread(
        target=heartbeat_lifecycle.run,
        kwargs={
            "client": client,
            "self_color": "green",
            "self_instance_id": instance_id,
            "port_lookup": port_lookup,
            "stop_event": stop,
            "sigterm_callback": _record_token,
            "logger": logging.getLogger("isolated_candidate_boot_gate"),
            "set_color_active": lambda _: None,
            "budget_seconds": _SMOKE_BUDGET_SECONDS,
        },
        name="offline-candidate-heartbeat",
        daemon=True,
    )
    thread.start()
    return stop, tokens, thread


def _registered(client: RouterClient, instance_id: str, port: int) -> bool:
    colors = client.status().get("colors")
    return isinstance(colors, list) and any(
        isinstance(entry, dict)
        and entry.get("instance_id") == instance_id
        and entry.get("port") == port
        for entry in colors
    )


def _scenario_candidate_registers() -> None:
    print("Scenario 1: isolated candidate start_interface acquires and registers")
    scratch = _private_scratch()
    router = _RouterHarness(scratch)
    bridge_listener: socket.socket | None = None
    stop: threading.Event | None = None
    heartbeat: threading.Thread | None = None
    try:
        router.start()
        _expect(router.socket_path.is_socket(), "router socket is inside private runtime")
        _expect(
            (scratch / "candidate.bridge.port").is_file()
            and (scratch / "candidate.router.port").is_file(),
            "router discovery files are inside private runtime",
        )
        _expect(
            router.socket_path.parent == scratch,
            "private router socket remains beneath this test's owned runtime",
        )
        port_cell: dict[str, int | None] = {"value": None}

        async def _start_interface(action: SimpleNamespace) -> None:
            nonlocal bridge_listener
            _expect(action.process_key == _START_INTERFACE, "candidate dispatched start_interface")
            bridge_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            bridge_listener.bind(("127.0.0.1", 0))
            bridge_listener.listen()
            port_cell["value"] = int(bridge_listener.getsockname()[1])

        poller = _PollerHarness(
            [SimpleNamespace(id="start", process_key=_START_INTERFACE)], _start_interface
        )
        client = RouterClient(router.socket_path)
        instance_id = "offline-candidate-green"
        stop, tokens, heartbeat = _start_heartbeat(
            client, lambda: port_cell["value"], instance_id
        )
        asyncio.run(poller.run_once())
        port = port_cell["value"]
        _expect(isinstance(port, int), "candidate start_interface bound a bridge port")
        deadline = time.monotonic() + _SMOKE_BUDGET_SECONDS + _SMOKE_SLACK_SECONDS
        while time.monotonic() < deadline and not _registered(client, instance_id, port):
            time.sleep(0.05)
        _expect(_registered(client, instance_id, port), "candidate registered through private router")
        _expect(tokens == [], "successful candidate registration did not fail closed")
        _expect(poller.claimed == ["start"], "real poller claimed candidate start action")
        _expect(poller.dispatched == ["start"], "real poller executed candidate start action")
    finally:
        if stop is not None:
            stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=5.0)
        if bridge_listener is not None:
            bridge_listener.close()
        router.stop()
        shutil.rmtree(scratch)


def _wait_for_tokens(tokens: list[str], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tokens:
            return True
        time.sleep(0.02)
    return bool(tokens)


async def _run_stalled_action_scenario() -> tuple[list[str], list[str], list[str], float]:
    scratch = _private_scratch()
    router = _RouterHarness(scratch)
    stop: threading.Event | None = None
    heartbeat: threading.Thread | None = None
    task: asyncio.Task[None] | None = None
    try:
        router.start()
        stalled_entered = asyncio.Event()
        never_complete = asyncio.Event()

        async def _dispatch(action: SimpleNamespace) -> None:
            if action.id == "earlier-stalled":
                stalled_entered.set()
                await never_complete.wait()
                return
            raise _FailureError("blocked poller must not dispatch start_interface")

        poller = _PollerHarness(
            [
                SimpleNamespace(
                    id="earlier-stalled", process_key="plugin::fixture::stall"  # wint:negative-fixture
                ),
                SimpleNamespace(id="start", process_key=_START_INTERFACE),
            ],
            _dispatch,
        )
        client = RouterClient(router.socket_path)
        stop, tokens, heartbeat = _start_heartbeat(
            client, lambda: None, "offline-stranded-green"
        )
        started_at = time.monotonic()
        task = asyncio.create_task(poller.run_once())
        await asyncio.wait_for(stalled_entered.wait(), timeout=1.0)
        _expect(
            poller.claimed == ["earlier-stalled"],
            "real poller claims only work it begins before serial dispatch",
        )
        _expect(
            poller.dispatched == ["earlier-stalled"],
            "earlier stall leaves start_interface unclaimed for a healthy poller",
        )
        fired = await asyncio.to_thread(
            _wait_for_tokens, tokens, _SMOKE_BUDGET_SECONDS + _SMOKE_SLACK_SECONDS
        )
        elapsed = time.monotonic() - started_at
        _expect(fired, "stranded candidate fails closed inside bounded window")
        _expect(
            tokens == [FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED],
            "stranded start_interface reports the missing-bridge structured token",
        )
        _expect(client.status().get("colors") == [], "stranded candidate never registers")
        return poller.claimed, poller.dispatched, tokens, elapsed
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if stop is not None:
            stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=5.0)
        router.stop()
        shutil.rmtree(scratch)


def _scenario_stalled_action_fails_closed() -> None:
    print("Scenario 2: earlier stalled action leaves start_interface unclaimed")
    claimed, dispatched, tokens, elapsed = asyncio.run(_run_stalled_action_scenario())
    _expect(claimed == ["earlier-stalled"], "one-at-a-time claim evidence retained")
    _expect(dispatched == ["earlier-stalled"], "blocked serial-dispatch evidence retained")
    _expect(tokens == [FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED], "failure token retained")
    _expect(
        elapsed <= _SMOKE_BUDGET_SECONDS + _SMOKE_SLACK_SECONDS,
        "failure did not wait for the live orphan-reaper interval",
    )


def main() -> int:
    print("=== isolated_candidate_boot_offline_gate_smoke ===")
    defaults = heartbeat_lifecycle.run.__kwdefaults__
    _expect(defaults is not None, "heartbeat exposes keyword defaults")
    _expect(
        defaults["budget_seconds"] == DEFAULT_TRANSIENT_STATE_BUDGET_SECONDS,
        "heartbeat default remains the live swap transient-state budget",
    )
    try:
        _scenario_candidate_registers()
        _scenario_stalled_action_fails_closed()
    except _FailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("isolated_candidate_boot_offline_gate: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
