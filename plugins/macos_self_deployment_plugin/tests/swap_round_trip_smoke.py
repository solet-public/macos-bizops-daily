"""End-to-end swap smoke for macos_self_deployment_plugin.

Stands up an in-process router (plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/router.py)
on a tmpfile mgmt socket; manually registers + activates a fake blue
binding; wires a ``MacosSelfDeploymentPlugin`` with a fake spawn
helper, a mock ActionFactory, and a mock orchestrator carrying one
plugin with a ``set_active`` spy; then exercises all four contract
verbs end-to-end:

1. ``restart_with_manifest`` →
   - spawns "green" (fake spawn registers a real binding via the
     router client),
   - polls until ready (handled by the orchestrator),
   - activates green (verified by reading router state),
   - calls ``set_active(False)`` on the mock plugin (verified via spy),
   - enqueues ``complete_swap`` (verified by reading the mock action
     factory's recorded submissions).

2. ``complete_swap`` →
   - SIGTERMs a real ``sleep`` child (the test's "prior blue" stand-in),
   - waits for it to exit cleanly,
   - unregisters the prior binding from the router.

3. ``swap_status`` → returns the router snapshot + the plugin's local
   ``swap_in_progress`` flag.

4. ``swap_rollback`` → with green active + blue in drain, re-points
   the router back to blue. Verified by reading router state.

No ``pytest``; the smoke runs directly via
``.venv/bin/python3 plugins/macos_self_deployment_plugin/tests/swap_round_trip_smoke.py``
and exits with code 0 on success, 1 on any failure with stderr detail.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Allow the smoke to import the deployment/ tree whether invoked from
# project root or directly via the plugin's tests/ directory. The
# plugin package itself is installed editable, so its imports resolve
# from site-packages; deployment/ is a non-packaged source tree
# requiring the path hint. The trailing noqa keeps ruff happy with
# imports following module-level code.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from macos_self_deployment_plugin import process_identity  # noqa: E402
from macos_self_deployment_plugin.blue_green_router.router import run_router  # noqa: E402
from macos_self_deployment_plugin.constants import (  # noqa: E402
    COLOR_BLUE,
    COLOR_GREEN,
    STATUS_COMPLETED,
    STATUS_QUEUED,
    STATUS_ROLLBACK_NOT_APPLICABLE,
    STATUS_ROLLED_BACK,
)
from macos_self_deployment_plugin.pending_finisher import (  # noqa: E402
    PendingFinisher,
    pending_finisher_path,
    write_pending_finisher,
)
from macos_self_deployment_plugin.plugin import (  # noqa: E402
    MacosSelfDeploymentPlugin,
    _runtime_dir,
)
from macos_self_deployment_plugin.preflight_probe_runner import ProbeOutcome  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CandidatePaths,
    GcResult,
    SwapResult,
)
from macos_self_deployment_plugin.router_client import (  # noqa: E402
    RouterClient,
)
from macos_self_deployment_plugin.schema_preflight import (  # noqa: E402
    PreflightVerdict,
)
from macos_self_deployment_plugin.swap_orchestrator import (  # noqa: E402
    SwapOrchestrator,
)

# Slice 3 of the bridge-port-routing design eliminated the hardcoded
# 8101-8198 port bands; ``register_color`` accepts arbitrary high-range ports.
# Each smoke run allocates its own ports so concurrently-running smoke processes
# cannot collide on a fleet-global fixture literal.
_MIN_TEST_PORT = 50000


class _FailureError(RuntimeError):
    """Raised by ``expect`` when an assertion fails."""


@dataclass
class _SmokeResources:
    """Private resources for one smoke run, released in one finalizer."""

    scratch_root: Path
    tmpdir: Path
    harness: _RouterHarness
    green_listener: socket.socket | None = None
    green_health_stop: threading.Event | None = None
    green_health_thread: threading.Thread | None = None
    prior_proc: Any | None = None


def _remove_private_scratch(resources: _SmokeResources, *, retain_artifacts: bool) -> None:
    """Remove only this run's tempfile-owned directory unless explicitly retained."""

    if retain_artifacts:
        print(f"[smoke] retaining private scratch: {resources.tmpdir}")
        return
    if resources.tmpdir.parent != resources.scratch_root or not resources.tmpdir.name.startswith("lbg_smoke_"):
        raise _FailureError(f"refusing to remove non-private scratch path: {resources.tmpdir}")
    shutil.rmtree(resources.tmpdir)


def _close_private_resources(resources: _SmokeResources, *, retain_artifacts: bool) -> None:
    """Close only objects created by this smoke before removing its private scratch."""

    try:
        if resources.green_health_stop is not None:
            resources.green_health_stop.set()
        if resources.green_listener is not None:
            resources.green_listener.close()
        if resources.green_health_thread is not None:
            resources.green_health_thread.join(timeout=1.0)
        if resources.prior_proc is not None and resources.prior_proc.poll() is None:
            resources.prior_proc.terminate()
            resources.prior_proc.wait(timeout=5)
    finally:
        try:
            resources.harness.stop()
        finally:
            _remove_private_scratch(resources, retain_artifacts=retain_artifacts)



def _smoke_green_probe(*, candidate: CandidatePaths, app_home: Path) -> ProbeOutcome:
    """GTE-06 seam: a GREEN probe so this smoke's pre-existing flow is unchanged."""
    del app_home
    return ProbeOutcome(
        ok=True,
        payload={"ok": True, "duration_ms": 0, "release_id": candidate.release_id},
    )


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise _FailureError(message)


class _MockActionFactory:
    """Records each submit_action_definition call without persisting it."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self._counter = 0

    def submit_action_definition(
        self,
        action_definition: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> str:
        del context
        self.submissions.append(dict(action_definition))
        self._counter += 1
        return f"ae-mock-{self._counter}"


class _SetActiveSpy:
    """Mock plugin exposing set_active; records each invocation."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set_active(self, active: bool) -> None:
        self.calls.append(active)


class _RouterHarness:
    """Spin run_router in a background thread + own its lifecycle."""

    def __init__(self, socket_path: Path, public_port: int) -> None:
        self._socket_path = socket_path
        self._public_port = public_port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_future: asyncio.Future[None] | None = None

    def start(self) -> None:
        def _run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self._stop_future = loop.create_future()
            ready_event = asyncio.Event()
            run_task = loop.create_task(
                run_router(
                    solet="smoke",
                    public_port=self._public_port,
                    socket_path=self._socket_path,
                    drain_window_seconds=2,
                    heartbeat_timeout_seconds=300,
                    buffer_timeout=0.5,
                    ready_event=ready_event,
                ),
                name="router_under_smoke",
            )

            async def _watch_ready() -> None:
                await ready_event.wait()
                self._ready.set()

            loop.create_task(_watch_ready(), name="watch_ready")

            try:
                loop.run_until_complete(self._stop_future)
            except asyncio.CancelledError:
                pass
            finally:
                run_task.cancel()
                try:
                    loop.run_until_complete(run_task)
                except (asyncio.CancelledError, BaseException):  # noqa: BLE001
                    pass
                loop.close()

        thread = threading.Thread(target=_run, name="router_harness", daemon=True)
        thread.start()
        if not self._ready.wait(timeout=5.0):
            msg = "router did not become ready within 5s"
            raise _FailureError(msg)
        self._thread = thread

    def stop(self) -> None:
        if self._loop is None or self._stop_future is None or self._thread is None:
            return
        loop = self._loop
        stop_future = self._stop_future

        def _set_stop() -> None:
            if not stop_future.done():
                stop_future.set_result(None)

        loop.call_soon_threadsafe(_set_stop)
        self._thread.join(timeout=5.0)


def _allocate_free_port() -> int:
    """Bind ephemeral port to discover a free one; close + return."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _allocate_high_range_port() -> int:
    """Allocate a free port while preserving the high-range routing regression."""
    for _ in range(64):
        port = _MIN_TEST_PORT + (int.from_bytes(os.urandom(2)) % (65536 - _MIN_TEST_PORT))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise _FailureError("could not allocate a high-range smoke port")


def _high_range_listening_socket() -> tuple[socket.socket, threading.Event, threading.Thread]:
    """Create a high-range listener that answers the candidate bridge health probe."""
    for _ in range(64):
        port = _MIN_TEST_PORT + (int.from_bytes(os.urandom(2)) % (65536 - _MIN_TEST_PORT))
        try:
            listener = _listening_socket(port)
        except OSError:
            continue
        else:
            stop_event = threading.Event()
            thread = threading.Thread(
                target=_serve_bridge_health,
                args=(listener, stop_event),
                name="swap-smoke-bridge-health",
                daemon=True,
            )
            thread.start()
            return listener, stop_event, thread
    raise _FailureError("could not allocate a high-range green listener")


def _listening_socket(port: int) -> socket.socket:
    """Bind + listen on ``port`` for the bridge-health stand-in.

    ``GreenCandidate.wait_until_registered`` requires a bounded
    ``GET /api/v1/bridge/health`` response from the green's registered port.
    The smoke's ``fake_spawn_green`` registers a router binding but launches
    no real solet, so this listener plus its responder models that bridge
    surface. The caller closes it at teardown.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(8)
    sock.settimeout(0.1)
    return sock


def _serve_bridge_health(listener: socket.socket, stop_event: threading.Event) -> None:
    """Answer bridge health requests until the smoke finalizer stops the listener."""
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: 0\r\n"
        b"Connection: close\r\n\r\n"
    )
    while not stop_event.is_set():
        try:
            connection, _address = listener.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        with connection:
            try:
                connection.recv(4096)
                connection.sendall(response)
            except OSError:
                continue


def _spawn_sleep_child() -> subprocess.Popen[bytes]:
    """Spawn a long-running sleep subprocess; return the Popen handle.

    Returning the Popen instead of the bare pid is load-bearing for
    the SIGTERM verification step: when ``complete_swap`` SIGTERMs the
    child, it would become a zombie under this Python parent without
    a ``wait()`` to reap it, and ``os.kill(pid, 0)`` would keep
    returning truthy. Calling ``proc.wait()`` reaps the zombie.
    """
    return subprocess.Popen(
        ["sleep", "60"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_child_dead(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> bool:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


class _FakeReleaseManager:
    """Records build/cutover without CoW-cloning the real 1.8 GB tree.

    Phase-2 wired ``ReleaseManager`` into the swap. This fake returns a
    synthetic candidate (the fake spawn never launches a real interpreter,
    so its paths are inert) and records the ``cutover`` call so the smoke
    can assert the candidate-threading → cutover wiring fired.
    """

    def __init__(self) -> None:
        self.build_count = 0
        self.cutover_release_ids: list[str] = []
        self.gc_count = 0

    def gc(self, *, keep: int | None = None) -> GcResult:
        del keep
        self.gc_count += 1
        return GcResult(deleted=(), retained=())

    def build_candidate(
        self, *, manifest_etag: str = "",
        manifest_plugins: tuple[str, ...] | None = None,
        schema_snapshot_fn: object = None,
    ) -> CandidatePaths:
        del manifest_etag, manifest_plugins, schema_snapshot_fn
        self.build_count += 1
        rel = "rel-smoke-green"
        base = Path("/nonexistent") / rel
        return CandidatePaths(
            release_id=rel,
            release_dir=base,
            code_root=base / "code",
            venv_python=base / "venv" / "bin" / "python3",
            version_file=base / "VERSION",
            missing_pth_targets=(),
            schema_snapshot=None,
        )

    def cutover(self, candidate: CandidatePaths) -> SwapResult:
        self.cutover_release_ids.append(candidate.release_id)
        return SwapResult(current=candidate.release_id, previous="rel-smoke-blue")

    @property
    def current_release(self) -> str | None:
        return None

    @property
    def previous_release(self) -> str | None:
        return None

    def current_schema_snapshot(self) -> dict[str, object] | None:
        return None

    def candidate_for(self, release_id: str) -> CandidatePaths:
        del release_id
        raise NotImplementedError("forward-path smoke: candidate_for unused")

    def rollback(self) -> SwapResult:
        raise NotImplementedError("forward-path smoke: rollback unused")


def _additive_preflight(
    candidate: CandidatePaths, *,
    current_snapshot: dict[str, object] | None,
    current_release_exists: bool,
) -> PreflightVerdict:
    """Smoke schema preflight: always additive (the swap path is what's tested)."""
    del candidate, current_snapshot, current_release_exists
    return PreflightVerdict(is_additive=True, breaking_changes=())


def _make_plugin(
    router_client: RouterClient,
    *,
    self_color: str,
    self_instance_id: str,
    set_active_spy: _SetActiveSpy,
    action_factory: _MockActionFactory,
    spawn_fn: Any,
    release_manager: _FakeReleaseManager,
    runtime_dir: Path,
) -> MacosSelfDeploymentPlugin:
    """Build a plugin instance wired by hand (skipping prepare_for_readiness)."""
    plugin = MacosSelfDeploymentPlugin()
    plugin.action_factory = action_factory  # type: ignore[assignment]
    plugin._solet_name = "smoke"  # noqa: SLF001 — smoke wiring
    plugin._self_color = self_color  # noqa: SLF001
    plugin._self_instance_id = self_instance_id  # noqa: SLF001
    plugin._router_client = router_client  # noqa: SLF001
    plugin._orchestrator = SwapOrchestrator(  # noqa: SLF001
        router_client=router_client,
        action_factory=action_factory,
        session_factory=lambda: "sess-smoke-round-trip",
        solet_name="smoke",
        release_manager=release_manager,
        schema_preflight=_additive_preflight,
        preflight_probe=_smoke_green_probe,
        set_color_active=lambda _active: None,
        spawn_fn=spawn_fn,
        ready_timeout_seconds=5,
        ready_poll_interval_seconds=0.05,
        runtime_dir=runtime_dir,
    )
    plugin.orchestrator_ref = SimpleNamespace(  # type: ignore[assignment]
        plugin_manager=SimpleNamespace(plugins={"spy_plugin": set_active_spy}),
    )
    return plugin


def _run_smoke_body(
    resources: _SmokeResources,
    *,
    fail_after_router: bool,
) -> None:  # noqa: C901, PLR0915 — long-form smoke
    print("[smoke] starting local_blue_green swap round-trip smoke")
    tmpdir = resources.tmpdir
    socket_path = tmpdir / "smoke.router.sock"
    public_port = resources.harness._public_port  # noqa: SLF001 -- smoke-owned harness
    print(f"[smoke] router up on port={public_port} socket={socket_path}")

    client = RouterClient(socket_path)
    if fail_after_router:
        raise _FailureError("injected post-router failure")

    # ---- Pre-state: register + activate blue ----
    blue_port = _allocate_high_range_port()
    green_listener, green_health_stop, green_health_thread = _high_range_listening_socket()
    green_port = int(green_listener.getsockname()[1])
    expect(blue_port != green_port, "blue and green smoke ports must be distinct")
    resources.green_listener = green_listener
    resources.green_health_stop = green_health_stop
    resources.green_health_thread = green_health_thread
    blue_instance_id = f"example-blue-{uuid.uuid4().hex[:8]}"
    reg = client.register_color(blue_port, COLOR_BLUE, blue_instance_id)
    expect(bool(reg.get("accepted")), f"register blue: {reg}")
    act = client.activate(COLOR_BLUE, blue_instance_id)
    expect(bool(act.get("activated")), f"activate blue: {act}")

    # Stand-in for the prior-blue OS process (for SIGTERM verification).
    prior_proc = _spawn_sleep_child()
    resources.prior_proc = prior_proc
    prior_pid = prior_proc.pid
    print(f"[smoke] prior-blue sleep child pid={prior_pid}")

    action_factory = _MockActionFactory()
    spy = _SetActiveSpy()

    spawn_calls: list[dict[str, Any]] = []
    _SYNTHETIC_GREEN_PID = 999999  # never signaled; closure return-value sentinel

    def fake_spawn_green(
        app_home: Path,
        next_color: str,
        next_instance_id: str,
        solet_name: str,
        candidate: CandidatePaths,
    ) -> int:
        """Smoke-only spawn helper.

        Records the call (incl. the candidate release it was handed, §4.5)
        and immediately registers a green binding via the smoke's
        RouterClient so the orchestrator's wait-for-register loop succeeds.
        Returns a fixed synthetic pid because the smoke does NOT actually
        launch another solet; the pid is never signaled or otherwise
        dereferenced by the smoke afterwards.
        """
        spawn_calls.append(
            {
                "app_home": str(app_home),
                "next_color": next_color,
                "next_instance_id": next_instance_id,
                "solet_name": solet_name,
                "candidate_release_id": candidate.release_id,
            },
        )
        result = client.register_color(
            green_port, next_color, next_instance_id,
        )
        expect(bool(result.get("accepted")), f"fake_spawn register: {result}")
        return _SYNTHETIC_GREEN_PID

    release_mgr = _FakeReleaseManager()
    plugin = _make_plugin(
        client,
        self_color=COLOR_BLUE,
        self_instance_id=blue_instance_id,
        set_active_spy=spy,
        action_factory=action_factory,
        spawn_fn=fake_spawn_green,
        release_manager=release_mgr,
        runtime_dir=tmpdir,
    )

    # The candidate readiness gate sends its bridge-health request to the
    # green's registered port; the per-run responder remains live through the
    # swap and is stopped at teardown below.

    # ----------------------------------------------------------------
    # Verb 1 — restart_with_manifest
    # ----------------------------------------------------------------
    result = plugin.restart_with_manifest(
        new_manifest={},
        expected_etag="etag-smoke-1",
        reason="smoke-verb-1",
        dry_run=False,
    )
    expect(
        result.status.value == STATUS_QUEUED,
        f"restart status: {result.status} {result.message}",
    )
    # GTE-06 Q5: the GREEN probe's success evidence rides the QUEUED result
    # (swap_orchestrator attaches it post-executor), so the applied envelope
    # carries positive proof the L2 probe executed on the green path.
    expect(
        result.probe is not None and result.probe.get("ok") is True,
        f"QUEUED result carries the probe success evidence: {result.probe}",
    )
    expect(bool(spawn_calls), "spawn_fn was not called")
    expect(
        spawn_calls[0]["next_color"] == COLOR_GREEN,
        f"next color: {spawn_calls[0]}",
    )
    # §4.5/§4.7 Phase-2 wiring: the candidate was built once, threaded into the
    # spawn, and cut over after a successful activate (before complete_swap).
    expect(
        release_mgr.build_count == 1,
        f"build_candidate called exactly once: {release_mgr.build_count}",
    )
    expect(
        spawn_calls[0]["candidate_release_id"] == "rel-smoke-green",
        f"candidate threaded into spawn: {spawn_calls[0]}",
    )
    expect(
        release_mgr.cutover_release_ids == ["rel-smoke-green"],
        f"cutover fired on the candidate post-activate: {release_mgr.cutover_release_ids}",
    )
    expect(
        release_mgr.gc_count >= 1,
        f"§4.7 lifecycle GC ran during the swap (build→cutover→gc): {release_mgr.gc_count}",
    )
    snap = client.status()
    expect(
        snap.get("active_color") == COLOR_GREEN,
        f"router active_color after activate: {snap.get('active_color')}",
    )
    drain_entries = snap.get("drain_entries") or []
    expect(
        any(
            isinstance(e, dict) and e.get("color") == COLOR_BLUE for e in drain_entries
        ),
        f"blue should be in drain after activate: {drain_entries}",
    )
    expect(
        spy.calls == [False],
        f"set_active(False) should fire exactly once: {spy.calls}",
    )
    expect(
        len(action_factory.submissions) == 1,
        f"complete_swap should be enqueued once: {len(action_factory.submissions)}",
    )
    enqueued = action_factory.submissions[0]
    expect(
        enqueued["name"] == "complete_swap",
        f"enqueued name: {enqueued.get('name')}",
    )
    # Task #21 regression guard (NS.C migration per the plugin-
    # namespace-callsite-sweep design record, dev-checkout workbench
    # -- not part of the shipped tree): the
    # enqueued action MUST carry the canonical service-interface
    # process_key, NOT the legacy `plugin::macos_self_deployment_plugin::*`
    # form (which is skipped at scan time per `_should_skip_plugin()`).
    expect(
        enqueued["process_key"] == "service_interface::local_self_deployment_service::complete_swap",
        f"canonical process_key (got {enqueued.get('process_key')!r})",
    )
    # rpk contract guard (same fix class as session_ledger trigger_poll
    # 2026-06-06): every plan-derived EDGE action must declare
    # result_processor_kind, or success-path validation raises
    # RESULT_CONTRACT_VIOLATION (result_processor_kind_missing).
    expect(
        enqueued.get("result_processor_kind") == "inference",
        f"result_processor_kind=inference (got {enqueued.get('result_processor_kind')!r})",
    )
    expect(
        enqueued["arguments"]["prior_pid"] == os.getpid(),
        f"prior_pid in args: {enqueued['arguments'].get('prior_pid')}",
    )
    expect(
        enqueued["arguments"]["prior_instance_id"] == blue_instance_id,
        f"prior_instance_id: {enqueued['arguments'].get('prior_instance_id')}",
    )
    print(
        "[smoke] verb-1 restart_with_manifest: PASS "
        f"(action_id={result.restart_action_id})",
    )

    # ----------------------------------------------------------------
    # Verb 2 — complete_swap (against the real sleep child PID)
    # ----------------------------------------------------------------
    # B2 round-2: complete_swap is now record-driven + identity-guarded — it
    # SIGTERMs the prior named in the durable pending-finisher record only when
    # the live start-time token still matches (no bare-pid kill). Seed a record
    # naming the stand-in prior (the real sleep child) with its real token, at
    # the path complete_swap reads, so the verb reaps exactly that process.
    finisher_path = pending_finisher_path(_runtime_dir(), plugin._solet_name)  # noqa: SLF001
    write_pending_finisher(
        finisher_path,
        PendingFinisher(
            prior_pid=prior_pid,
            prior_instance_id=blue_instance_id,
            prior_color=COLOR_BLUE,
            candidate_release_id="rel-smoke-green",
            prior_start_token=process_identity.start_token(prior_pid),
        ),
    )
    swap_result = plugin.complete_swap(
        prior_pid=prior_pid,
        prior_instance_id=blue_instance_id,
        prior_color=COLOR_BLUE,
    )
    expect(
        swap_result["status"] == STATUS_COMPLETED,
        f"complete_swap status: {swap_result}",
    )
    expect(
        _wait_child_dead(prior_proc, timeout=15.0),
        f"prior-blue pid {prior_pid} should be reaped after SIGTERM",
    )
    snap = client.status()
    blue_still_registered = any(
        isinstance(b, dict) and b.get("instance_id") == blue_instance_id
        for b in snap.get("colors") or []
    )
    expect(
        not blue_still_registered,
        f"blue should be unregistered from router: {snap}",
    )
    print("[smoke] verb-2 complete_swap: PASS")

    # ----------------------------------------------------------------
    # Verb 3 — swap_status
    # ----------------------------------------------------------------
    status_payload = plugin.swap_status()
    expect("router_status" in status_payload, "swap_status missing router_status")
    expect(
        status_payload["self_color"] == COLOR_BLUE,
        f"swap_status self_color: {status_payload}",
    )
    expect(
        status_payload["self_instance_id"] == blue_instance_id,
        f"swap_status self_instance_id: {status_payload}",
    )
    expect(
        status_payload["swap_in_progress"] is False,
        f"swap_in_progress should be False after restart returns: {status_payload}",
    )
    print("[smoke] verb-3 swap_status: PASS")

    # ----------------------------------------------------------------
    # Verb 4 — swap_rollback
    # ----------------------------------------------------------------
    # First case: with no drain entry, rollback is not applicable.
    # We pop blue out of drain by directly forcing a quick fresh state:
    # re-register a NEW blue (a fresh "prior") + activate it briefly,
    # then swap to a NEW green so the new blue lands in drain.
    new_blue_id = f"example-blue-{uuid.uuid4().hex[:8]}"
    reg = client.register_color(blue_port, COLOR_BLUE, new_blue_id)
    expect(bool(reg.get("accepted")), f"re-register blue: {reg}")
    act = client.activate(COLOR_BLUE, new_blue_id)
    expect(bool(act.get("activated")), f"re-activate blue: {act}")
    # Register a brand-new green and activate so blue drops into drain.
    new_green_id = f"example-green-{uuid.uuid4().hex[:8]}"
    reg = client.register_color(green_port, COLOR_GREEN, new_green_id)
    expect(bool(reg.get("accepted")), f"re-register green: {reg}")
    act = client.activate(COLOR_GREEN, new_green_id)
    expect(bool(act.get("activated")), f"re-activate green: {act}")
    rollback_payload = plugin.swap_rollback(reason="smoke-rollback")
    expect(
        rollback_payload["status"] == STATUS_ROLLED_BACK,
        f"swap_rollback expected rolled_back: {rollback_payload}",
    )
    expect(
        rollback_payload["rolled_back_to"] == COLOR_BLUE,
        f"rolled_back_to color: {rollback_payload}",
    )
    snap = client.status()
    expect(
        snap.get("active_color") == COLOR_BLUE,
        f"after rollback active_color should be blue: {snap}",
    )
    print("[smoke] verb-4 swap_rollback (in-window): PASS")

    # Second rollback call after the drain window expires — not applicable.
    # The harness above set drain_window_seconds=2; pause long enough
    # for the sweeper to clear green out of drain.
    time.sleep(4.0)
    rollback_payload_2 = plugin.swap_rollback(reason="smoke-rollback-2")
    expect(
        rollback_payload_2["status"] == STATUS_ROLLBACK_NOT_APPLICABLE,
        f"second swap_rollback expected not_applicable: {rollback_payload_2}",
    )
    print("[smoke] verb-4 swap_rollback (drain-expired): PASS")

    # No spawned-green process to clean up: ``fake_spawn_green`` only
    # registered a router binding; the returned pid is a fixed sentinel
    # never tied to a real OS process. The prior-blue sleep child was
    # already reaped above by ``complete_swap``.
    print("[smoke] ALL VERBS PASS")


def run_smoke(*, retain_artifacts: bool = False, fail_after_router: bool = False) -> None:
    """Run the smoke with a private scratch lifetime closed on every exit path."""

    scratch_root = Path.home() / ".ananta"
    scratch_root.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="lbg_smoke_", dir=str(scratch_root)))
    harness = _RouterHarness(tmpdir / "smoke.router.sock", _allocate_free_port())
    resources = _SmokeResources(scratch_root, tmpdir, harness)
    harness.start()
    try:
        _run_smoke_body(resources, fail_after_router=fail_after_router)
    finally:
        _close_private_resources(resources, retain_artifacts=retain_artifacts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--retain-artifacts", action="store_true")
    parser.add_argument("--fail-after-router", action="store_true")
    args = parser.parse_args()
    try:
        run_smoke(
            retain_artifacts=args.retain_artifacts,
            fail_after_router=args.fail_after_router,
        )
    except _FailureError as exc:
        print(f"[smoke] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception:  # noqa: BLE001 — top-level last-resort surface
        import traceback

        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
