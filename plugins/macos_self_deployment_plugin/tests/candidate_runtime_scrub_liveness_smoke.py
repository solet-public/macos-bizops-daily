"""Regression smoke: a swap candidate's runtime scrub must not remove the live router's bridge pointer.

``MacosSelfDeploymentPlugin.prepare_for_readiness`` runs
``stale_runtime_cleanup.cleanup_stale_runtime_files`` in every process, including
a blue-green swap candidate started with an explicit ``SOLET_COLOR`` while the old
colour and the router are live.  The scrub used to unlink the router-owned
``<name>.bridge.port`` with no check, so until the restore (or the router's 5s
bridge-port watchdog) rewrote it a fresh ``solet-bridge call`` found no file and
raised ``SoletNotRunningError`` although the router and the live colour were up.

The file's owner is the router, so its liveness is the router's identity: it is
kept only if the router's management socket answers AND the file names the port
the router published in ``<name>.router.port``.  A bare "something listens on
that port" is not enough, because a crashed router's stale pointer can name a
port a foreign process has since bound; a cold start with no router must end
with the file ABSENT so ``solet-bridge`` gets a clean ``SoletNotRunningError``.

Nothing is faked at the probe: ``HOME`` points at a scratch directory so the real
``runtime_dir()`` resolves there, the router is the real ``MgmtServer`` with the
router's real dispatch answering on ``<name>.router.sock``, the foreign listener
is a real loopback socket, and the real ``cleanup_stale_runtime_files`` and
``router_mgmt_status`` decide.

Cases:

* live router, file names its port -> kept, through the scrub and through the real
  ``prepare_for_readiness`` of an explicit-colour candidate up to its wait on the
  router (the window the CLI could hit);
* live router, file names another port -> removed (router-mismatch);
* live router, no ``<name>.router.port`` -> removed (identity cannot be shown),
  including when the bridge.port is unparseable too (no ``None == None`` match);
* no router, file names a live FOREIGN listener -> removed, including through a
  real cold-start ``prepare_for_readiness`` that fails to find a router;
* no router, ``bridge.port`` AND ``router.port`` both name a live foreign listener
  (a crashed router's pointers after the port was recycled), with and without a
  stale listener-less ``<name>.router.sock`` -> removed, so the router's
  management socket, not the port files alone, is what proves identity;
* no router, dead port / unparseable file -> removed (crash recovery);
* ``.sock``, ``.rest.port`` and ``.draining`` are scrubbed as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))

from macos_self_deployment_plugin import plugin as plugin_module  # noqa: E402
from macos_self_deployment_plugin import stale_runtime_cleanup  # noqa: E402
from macos_self_deployment_plugin.blue_green_router import router as router_module  # noqa: E402
from macos_self_deployment_plugin.blue_green_router.router_mgmt import MgmtServer  # noqa: E402
from macos_self_deployment_plugin.blue_green_router.router_state import RouterState  # noqa: E402

_NAME = "scrubprobe"
# A unix socket path is limited to ~104 bytes on macOS, so the scratch home lives
# under /tmp rather than the (long) per-user temp directory.
_SCRATCH_ROOT = "/tmp"
_COLD_START_ROUTER_WAIT_SECONDS = 0.5


@contextlib.contextmanager
def _scratch_home() -> Iterator[Path]:
    """A scratch ``HOME`` so the real ``runtime_dir()`` resolves under it."""
    previous = os.environ.get("HOME")
    with tempfile.TemporaryDirectory(prefix="scrub-", dir=_SCRATCH_ROOT) as temp_dir:
        os.environ["HOME"] = temp_dir
        try:
            runtime = stale_runtime_cleanup.runtime_dir()
            runtime.mkdir(parents=True, mode=0o700)
            yield runtime
        finally:
            _restore_env("HOME", previous)


@contextlib.contextmanager
def _live_router(runtime: Path, *, publish_port: int | None) -> Iterator[None]:
    """The real router management server answering on ``<name>.router.sock``.

    ``publish_port`` is written to ``<name>.router.port`` as ``install_router``
    does; ``None`` leaves that file absent.
    """
    if publish_port is not None:
        (runtime / f"{_NAME}.router.port").write_text(str(publish_port), encoding="utf-8")
    loop = asyncio.new_event_loop()
    server = MgmtServer(
        runtime / f"{_NAME}.router.sock", router_module._make_dispatch(RouterState()),
    )
    started = threading.Event()

    def serve() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start())
        started.set()
        loop.run_forever()

    thread = threading.Thread(target=serve, name="mgmt-server", daemon=True)
    thread.start()
    if not started.wait(timeout=10.0):
        raise AssertionError("router management server did not start")
    try:
        yield
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10.0)
        loop.run_until_complete(server.stop())
        loop.close()


def _listening_tcp() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener


def _refused_port() -> int:
    """A port that was just free and has no listener (connect is refused)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _stale_socket_file(path: Path) -> None:
    """A unix socket file whose listener is gone (left by a crashed process)."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.close()
    if not path.exists():
        raise AssertionError(f"closing the listener unlinked {path}; cannot build a stale socket")


def _restore_env(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


def _check(label: str, condition: bool, failures: list[str]) -> None:
    print(f"{label}: {'PASS' if condition else 'FAIL'}")
    if not condition:
        failures.append(label)


def _run_prepare_for_readiness(*, color: str | None, wait: object | None = None) -> str | None:
    """Real ``prepare_for_readiness``; returns the RuntimeError text if it raised."""
    original_wait = plugin_module._wait_for_router_socket
    original_wait_seconds = plugin_module.DEFAULT_ROUTER_SOCKET_WAIT_SECONDS
    old_name = os.environ.get("SOLET_NAME")
    old_color = os.environ.get("SOLET_COLOR")
    try:
        if wait is not None:
            plugin_module._wait_for_router_socket = wait  # type: ignore[assignment]
        plugin_module.DEFAULT_ROUTER_SOCKET_WAIT_SECONDS = _COLD_START_ROUTER_WAIT_SECONDS
        os.environ["SOLET_NAME"] = _NAME
        if color is None:
            os.environ.pop("SOLET_COLOR", None)
        else:
            os.environ["SOLET_COLOR"] = color
        instance = plugin_module.MacosSelfDeploymentPlugin()
        instance._reconcile_releases = lambda: None  # type: ignore[method-assign]
        instance.set_ready = lambda: None  # type: ignore[method-assign]
        try:
            instance.prepare_for_readiness()
        except RuntimeError as exc:
            return str(exc)
        return None
    finally:
        plugin_module._wait_for_router_socket = original_wait
        plugin_module.DEFAULT_ROUTER_SOCKET_WAIT_SECONDS = original_wait_seconds
        _restore_env("SOLET_NAME", old_name)
        _restore_env("SOLET_COLOR", old_color)


def _case_live_router_keeps_its_pointer(failures: list[str]) -> None:
    with _scratch_home() as runtime:
        public = _listening_tcp()
        port = str(public.getsockname()[1])
        bridge_file = runtime / f"{_NAME}.bridge.port"
        bridge_file.write_text(port, encoding="utf-8")
        try:
            with _live_router(runtime, publish_port=int(port)):
                stale_runtime_cleanup.cleanup_stale_runtime_files(_NAME)
                _check(
                    "live router: bridge.port survives the scrub with its content",
                    bridge_file.exists() and bridge_file.read_text(encoding="utf-8") == port,
                    failures,
                )
                seen_during_wait: list[str | None] = []

                def wait_for_router(_path: Path) -> None:
                    seen_during_wait.append(
                        bridge_file.read_text(encoding="utf-8") if bridge_file.exists() else None,
                    )

                _run_prepare_for_readiness(color="green", wait=wait_for_router)
                _check(
                    "live router: bridge.port is present while a candidate waits on the router",
                    seen_during_wait == [port],
                    failures,
                )
        finally:
            public.close()


def _case_router_mismatch_is_removed(failures: list[str]) -> None:
    with _scratch_home() as runtime:
        public = _listening_tcp()
        other = _listening_tcp()
        bridge_file = runtime / f"{_NAME}.bridge.port"
        bridge_file.write_text(str(other.getsockname()[1]), encoding="utf-8")
        try:
            with _live_router(runtime, publish_port=int(public.getsockname()[1])):
                stale_runtime_cleanup.cleanup_stale_runtime_files(_NAME)
                _check(
                    "router mismatch: a bridge.port naming a port the router did not publish is removed",
                    not bridge_file.exists(),
                    failures,
                )
        finally:
            public.close()
            other.close()


def _case_router_without_published_port_is_removed(failures: list[str]) -> None:
    with _scratch_home() as runtime:
        public = _listening_tcp()
        bridge_file = runtime / f"{_NAME}.bridge.port"
        bridge_file.write_text(str(public.getsockname()[1]), encoding="utf-8")
        try:
            with _live_router(runtime, publish_port=None):
                stale_runtime_cleanup.cleanup_stale_runtime_files(_NAME)
                _check(
                    "router without a published port: bridge.port is removed",
                    not bridge_file.exists(),
                    failures,
                )
                bridge_file.write_text("not-a-port", encoding="utf-8")
                stale_runtime_cleanup.cleanup_stale_runtime_files(_NAME)
                _check(
                    "router without a published port: an unparseable bridge.port is removed",
                    not bridge_file.exists(),
                    failures,
                )
        finally:
            public.close()


def _case_foreign_listener_without_router_is_removed(failures: list[str]) -> None:
    with _scratch_home() as runtime:
        foreign = _listening_tcp()
        bridge_file = runtime / f"{_NAME}.bridge.port"
        bridge_file.write_text(str(foreign.getsockname()[1]), encoding="utf-8")
        try:
            stale_runtime_cleanup.cleanup_stale_runtime_files(_NAME)
            _check(
                "no router: a stale bridge.port naming a live foreign listener is removed",
                not bridge_file.exists(),
                failures,
            )
            bridge_file.write_text(str(foreign.getsockname()[1]), encoding="utf-8")
            failure = _run_prepare_for_readiness(color=None)
            _check(
                "no router: a real cold start finds no router and fails loudly",
                failure is not None,
                failures,
            )
            _check(
                "no router: the cold start leaves bridge.port ABSENT (clean SoletNotRunningError)",
                not bridge_file.exists(),
                failures,
            )
        finally:
            foreign.close()


def _case_recycled_port_pointers_without_router_are_removed(failures: list[str]) -> None:
    """A crashed router leaves both port files; a foreign process now owns the port."""
    for label, stale_router_socket in (
        ("no router socket", False),
        ("a stale listener-less router socket", True),
    ):
        with _scratch_home() as runtime:
            foreign = _listening_tcp()
            port = str(foreign.getsockname()[1])
            bridge_file = runtime / f"{_NAME}.bridge.port"
            bridge_file.write_text(port, encoding="utf-8")
            (runtime / f"{_NAME}.router.port").write_text(port, encoding="utf-8")
            if stale_router_socket:
                _stale_socket_file(runtime / f"{_NAME}.router.sock")
            try:
                stale_runtime_cleanup.cleanup_stale_runtime_files(_NAME)
                _check(
                    f"no router ({label}): bridge.port and router.port both naming a "
                    "live foreign listener -> bridge.port is removed",
                    not bridge_file.exists(),
                    failures,
                )
            finally:
                foreign.close()


def _case_dead_owner_is_removed(failures: list[str]) -> None:
    with _scratch_home() as runtime:
        bridge_file = runtime / f"{_NAME}.bridge.port"
        bridge_file.write_text(str(_refused_port()), encoding="utf-8")
        stale_runtime_cleanup.cleanup_stale_runtime_files(_NAME)
        _check("no router: a bridge.port naming a refused port is removed", not bridge_file.exists(), failures)
        bridge_file.write_text("not-a-port", encoding="utf-8")
        stale_runtime_cleanup.cleanup_stale_runtime_files(_NAME)
        _check("no router: an unparseable bridge.port is removed", not bridge_file.exists(), failures)


def _case_other_files_are_scrubbed_as_before(failures: list[str]) -> None:
    with _scratch_home() as runtime:
        sock = runtime / f"{_NAME}.sock"
        _stale_socket_file(sock)
        rest_file = runtime / f"{_NAME}.rest.port"
        rest_file.write_text(str(_refused_port()), encoding="utf-8")
        draining = runtime / f"{_NAME}.draining"
        draining.touch()
        stale_runtime_cleanup.cleanup_stale_runtime_files(_NAME)
        _check("stale socket file is removed", not sock.exists(), failures)
        _check("rest.port is removed", not rest_file.exists(), failures)
        _check("the .draining marker is removed", not draining.exists(), failures)


def main() -> int:
    failures: list[str] = []
    _case_live_router_keeps_its_pointer(failures)
    _case_router_mismatch_is_removed(failures)
    _case_router_without_published_port_is_removed(failures)
    _case_foreign_listener_without_router_is_removed(failures)
    _case_recycled_port_pointers_without_router_are_removed(failures)
    _case_dead_owner_is_removed(failures)
    _case_other_files_are_scrubbed_as_before(failures)
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed")
        return 1
    print("PASS: the live router's bridge pointer survives the scrub; every other stale file is removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
