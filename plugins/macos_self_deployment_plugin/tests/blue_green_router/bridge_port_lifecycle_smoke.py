"""Slice 1 + 1.5 smoke — bridge port file ownership + stale-canonical cleanup.

Per Slice 1 and Slice 1.5 of the bridge-port-routing-and-session-
lifecycle design record (operator framing: "ironclad, absolutely
infallible"). Closes TLC counterexample #2 from the bridge-lifecycle
TLA narrative record §10.2 (both dev-checkout workbench records — not
part of the shipped tree).

What this smoke verifies:

* **Case 1 (Slice 1 — router writes both files):** after install_router.py
  runs end-to-end with a sandboxed HOME, both
  ``<runtime_dir>/<name>.router.port`` and ``<runtime_dir>/<name>.bridge.port``
  exist, both contain the same port number, both are mode 0o600. The
  per-color sibling convention is untouched (Slice 3's scope).
* **Case 2 (Slice 1.5 — launch.py scrubs stale canonical):** with a
  pre-staged stale ``<name>.bridge.port`` (simulating a crashed prior
  router that left the canonical pointing at a dead port), launch.py's
  ``_cleanup_stale_runtime_files`` removes it. ``<name>.sock`` and
  ``<name>.rest.port`` (the pre-existing scrub set) are removed in the
  same call. Files for unrelated solets are untouched.
* **Case 3 (Slice 1.5 repair — live router restores canonical):** when a
  live router management socket answers status() and ``<name>.router.port``
  is valid, launch.py re-materializes the router-owned
  ``<name>.bridge.port`` after the stale scrub.

Sandbox discipline (per ``[[sandbox_mutating_smokes]]``):

* Smoke solet name 'bgportlife' — distinct from any real install
  (the live solet's real name, 'bgsmoke', etc.).
* ``HOME`` env override on every subprocess redirects ``Path.home()`` to
  a tmp dir; never touches ``~/.ananta/runtime/``.
* launchd / systemd paths overridden via the install_router flags so no
  real LaunchAgent / systemd unit ever lands on the operator's machine.
* try/finally uninstall of the smoke router so a mid-flight failure
  cannot leave a KeepAlive-respawning phantom agent registered.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/blue_green_router/bridge_port_lifecycle_smoke.py
"""

from __future__ import annotations

import json
import os
import socket as socket_mod
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from macos_self_deployment_plugin.blue_green_router.service_install import (  # noqa: E402
    launchd_label,
    systemd_unit_name,
)

# Canonical installer/uninstaller location + plugin src root. F2 Choice Y
# deleted the old ``deployment/local_blue_green/`` tree; the scripts now live
# inside the plugin package, and ``launch.py``'s stale-runtime helpers moved
# to ``macos_self_deployment_plugin.stale_runtime_cleanup``. Computed from the
# plugin root (parents[2]) — independent of the (vestigial, plugins-dir-valued)
# REPO_ROOT above.
_PLUGIN_SRC = Path(__file__).resolve().parents[2] / "src"
_ROUTER_SCRIPTS_DIR = _PLUGIN_SRC / "macos_self_deployment_plugin" / "blue_green_router"
_INSTALL_ROUTER = _ROUTER_SCRIPTS_DIR / "install_router.py"
_UNINSTALL_ROUTER = _ROUTER_SCRIPTS_DIR / "uninstall_router.py"

SMOKE_SOLET_NAME = "bgportlife"
OTHER_SOLET_NAME = "bgportlife-other"


def _stamp(label: str, ok: bool, detail: str = "") -> bool:
    sym = "PASS" if ok else "FAIL"
    print(f"  [{sym}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def _pick_free_port() -> int:
    with socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _belt_and_suspenders_bootout() -> None:
    """Best-effort bootout in case a previous smoke crashed mid-flight."""

    import platform

    if platform.system() != "Darwin":
        return
    service_target = f"gui/{os.getuid()}/{launchd_label(SMOKE_SOLET_NAME)}"
    subprocess.run(
        ["launchctl", "bootout", service_target],
        capture_output=True,
        text=True,
        check=False,
    )


def _run_install(
    *,
    home_dir: Path,
    plist_path: Path,
    unit_path: Path,
    socket_path: Path,
    log_dir: Path,
    public_port: int,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    return subprocess.run(
        [
            sys.executable,
            str(_INSTALL_ROUTER),
            SMOKE_SOLET_NAME,
            "--public-port", str(public_port),
            "--plist-path", str(plist_path),
            "--unit-path", str(unit_path),
            "--socket-path", str(socket_path),
            "--log-dir", str(log_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _run_uninstall(
    *,
    home_dir: Path,
    plist_path: Path,
    unit_path: Path,
    socket_path: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    return subprocess.run(
        [
            sys.executable,
            str(_UNINSTALL_ROUTER),
            SMOKE_SOLET_NAME,
            "--plist-path", str(plist_path),
            "--unit-path", str(unit_path),
            "--socket-path", str(socket_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _read_port_file(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _file_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _uninstall_removes_port_files(
    *,
    home_dir: Path,
    plist_path: Path,
    unit_path: Path,
    socket_path: Path,
    router_port_file: Path,
    bridge_port_file: Path,
) -> bool:
    """Uninstall the fixture router and prove both router-owned files are gone."""

    uninstall_proc = _run_uninstall(
        home_dir=home_dir,
        plist_path=plist_path,
        unit_path=unit_path,
        socket_path=socket_path,
    )
    uninstall_ok = uninstall_proc.returncode == 0
    router_removed = not router_port_file.exists()
    bridge_removed = not bridge_port_file.exists()
    uninstall_detail = "" if uninstall_ok else f"stderr={uninstall_proc.stderr!r}"
    _stamp("uninstall exit 0", uninstall_ok, uninstall_detail)
    _stamp("uninstall removes router.port", router_removed, str(router_port_file))
    _stamp("uninstall removes bridge.port", bridge_removed, str(bridge_port_file))
    return all((uninstall_ok, router_removed, bridge_removed))


def _serve_one_router_status(socket_path: Path) -> threading.Thread:
    """Serve one router status reply on a sandboxed Unix socket.

    Answers ONLY the router's real verb-dispatch protocol
    (``{"verb": "status", ...}``). A request in any other shape (e.g. a
    protocol regression sending ``{"action": "status"}``) gets no reply, so
    the liveness probe reads empty and ``restore_...`` returns False — making
    this case the regression guard for the protocol axis, not just the socket
    name + port-source axes. The reply deliberately omits ``router_port`` so a
    future restore that reads the port from the status payload (rather than the
    ``.router.port`` file) also reds this case.
    """

    def _server() -> None:
        if socket_path.exists():
            socket_path.unlink()
        with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as sock:
            sock.bind(str(socket_path))
            sock.listen(1)
            conn, _ = sock.accept()
            with conn:
                buf = bytearray()
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                line = bytes(buf).split(b"\n", 1)[0]
                try:
                    request = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    request = {}
                if isinstance(request, dict) and request.get("verb") == "status":
                    conn.sendall(b'{"router_started_at": 123.0}\n')
                # else: send nothing — wrong protocol fails the liveness probe.

    thread = threading.Thread(target=_server, name="fake-router-status", daemon=True)
    thread.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if socket_path.exists():
            return thread
        time.sleep(0.01)
    raise RuntimeError(f"fake router socket did not appear: {socket_path}")


def _case_slice1_router_writes_both_files(
    home_dir: Path, tmpdir: Path, socket_dir: Path
) -> bool:
    """Slice 1 — install_router writes both router.port and bridge.port."""

    print("\n[case_slice1] install_router writes both <name>.router.port and <name>.bridge.port")
    plist_dir = tmpdir / "LaunchAgents"
    plist_dir.mkdir()
    unit_dir = tmpdir / "systemd"
    unit_dir.mkdir()
    log_dir = tmpdir / "logs"
    log_dir.mkdir()
    plist_path = plist_dir / f"{launchd_label(SMOKE_SOLET_NAME)}.plist"
    unit_path = unit_dir / systemd_unit_name(SMOKE_SOLET_NAME)
    runtime_dir = home_dir / ".ananta" / "runtime"
    runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Socket path goes under a short tmp dir, not inside HOME, because
    # macOS AF_UNIX paths are capped at ~104 chars and the HOME-sandboxed
    # path is well above that limit.
    socket_path = socket_dir / f"{SMOKE_SOLET_NAME}.router.sock"
    public_port = _pick_free_port()

    proc = _run_install(
        home_dir=home_dir,
        plist_path=plist_path,
        unit_path=unit_path,
        socket_path=socket_path,
        log_dir=log_dir,
        public_port=public_port,
    )

    install_ok = proc.returncode == 0
    _stamp("install exit 0", install_ok, f"stderr={proc.stderr!r}" if not install_ok else "")
    if not install_ok:
        return False

    router_port_file = runtime_dir / f"{SMOKE_SOLET_NAME}.router.port"
    bridge_port_file = runtime_dir / f"{SMOKE_SOLET_NAME}.bridge.port"

    router_exists = router_port_file.exists()
    bridge_exists = bridge_port_file.exists()
    _stamp("router.port written", router_exists, str(router_port_file))
    _stamp("bridge.port written", bridge_exists, str(bridge_port_file))
    if not (router_exists and bridge_exists):
        return False

    router_port = _read_port_file(router_port_file)
    bridge_port = _read_port_file(bridge_port_file)
    matches_router = router_port == public_port
    matches_bridge = bridge_port == public_port
    _stamp(
        "router.port content matches public_port",
        matches_router,
        f"file={router_port} expected={public_port}",
    )
    _stamp(
        "bridge.port content matches public_port",
        matches_bridge,
        f"file={bridge_port} expected={public_port}",
    )
    _stamp(
        "router.port and bridge.port hold the same value",
        router_port is not None and router_port == bridge_port,
        f"router={router_port} bridge={bridge_port}",
    )

    router_mode = _file_mode(router_port_file)
    bridge_mode = _file_mode(bridge_port_file)
    _stamp("router.port mode 0o600", router_mode == 0o600, f"mode={router_mode:#o}")
    _stamp("bridge.port mode 0o600", bridge_mode == 0o600, f"mode={bridge_mode:#o}")

    # Uninstall must remove both router-owned discovery files. The operator's
    # real home is untouched; this only inspects the smoke sandbox.
    uninstall_removes_files = _uninstall_removes_port_files(
        home_dir=home_dir,
        plist_path=plist_path,
        unit_path=unit_path,
        socket_path=socket_path,
        router_port_file=router_port_file,
        bridge_port_file=bridge_port_file,
    )

    return all(
        (
            matches_router,
            matches_bridge,
            router_port == bridge_port,
            router_mode == 0o600,
            bridge_mode == 0o600,
            uninstall_removes_files,
        )
    )


def _case_slice15_cleanup_removes_stale_bridge_port(home_dir: Path) -> bool:
    """Slice 1.5 — launch.py's cleanup removes stale <name>.bridge.port."""

    print(
        "\n[case_slice15] launch._cleanup_stale_runtime_files scrubs "
        "<name>.bridge.port + <name>.sock + <name>.rest.port; leaves "
        "other solets untouched"
    )
    runtime_dir = home_dir / ".ananta" / "runtime"
    runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    stale_bridge = runtime_dir / f"{SMOKE_SOLET_NAME}.bridge.port"
    stale_sock = runtime_dir / f"{SMOKE_SOLET_NAME}.sock"
    stale_rest = runtime_dir / f"{SMOKE_SOLET_NAME}.rest.port"
    other_bridge = runtime_dir / f"{OTHER_SOLET_NAME}.bridge.port"
    stale_bridge.write_text("8101")
    stale_sock.write_text("")
    stale_rest.write_text("8001")
    other_bridge.write_text("8800")

    # Subprocess-invoke the cleanup helper. launch.py was deleted (F2
    # Choice Y); its stale-runtime helpers live in
    # ``macos_self_deployment_plugin.stale_runtime_cleanup`` now. Sandbox via
    # HOME so ``runtime_dir()`` resolves under the smoke's tmp home; the name
    # is passed explicitly (the new helper takes it as an argument).
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(_PLUGIN_SRC)!r})\n"
        "from macos_self_deployment_plugin import stale_runtime_cleanup\n"
        f"stale_runtime_cleanup.cleanup_stale_runtime_files({SMOKE_SOLET_NAME!r})\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    invoke_ok = proc.returncode == 0
    _stamp(
        "_cleanup_stale_runtime_files invoked cleanly",
        invoke_ok,
        f"stderr={proc.stderr!r}" if not invoke_ok else "",
    )
    if not invoke_ok:
        return False

    bridge_gone = not stale_bridge.exists()
    sock_gone = not stale_sock.exists()
    rest_gone = not stale_rest.exists()
    other_preserved = other_bridge.exists() and other_bridge.read_text().strip() == "8800"
    _stamp("stale bridge.port removed", bridge_gone, str(stale_bridge))
    _stamp("stale sock removed", sock_gone, str(stale_sock))
    _stamp("stale rest.port removed", rest_gone, str(stale_rest))
    _stamp(
        "other solet's bridge.port preserved",
        other_preserved,
        str(other_bridge),
    )

    return bridge_gone and sock_gone and rest_gone and other_preserved


def _case_slice15_repair_restores_bridge_port_from_live_router() -> bool:
    """Slice 1.5 repair — live router restores canonical bridge.port.

    Drives ``stale_runtime_cleanup.restore_...`` end-to-end against a fake
    live router: a ``<name>.router.sock`` that answers the verb-status probe
    + a ``<name>.router.port`` file holding 7777. The helper must read the
    port from the file, confirm liveness on the socket, and re-materialize
    ``<name>.bridge.port`` (7777, mode 0o600). This case is the regression
    guard that keeps the restore path from silently rotting.
    """

    print(
        "\n[case_slice15_repair] stale-cleanup restores <name>.bridge.port "
        "from the live router when its mgmt socket answers status()"
    )
    # Short sandbox HOME under ~/.ananta (never /tmp, per the operator rule).
    # The router mgmt socket lands at <home>/.ananta/runtime/<name>.router.sock;
    # ~/.ananta keeps the AF_UNIX path well under the macOS 104-byte limit.
    ananta_root = Path.home() / ".ananta"
    ananta_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bgpl-", dir=str(ananta_root)) as short_home:
        home_dir = Path(short_home)
        runtime_dir = home_dir / ".ananta" / "runtime"
        runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        bridge_port = runtime_dir / f"{SMOKE_SOLET_NAME}.bridge.port"
        router_port = runtime_dir / f"{SMOKE_SOLET_NAME}.router.port"
        router_socket = runtime_dir / f"{SMOKE_SOLET_NAME}.router.sock"
        bridge_port.write_text("8101")
        router_port.write_text("7777")
        server = _serve_one_router_status(router_socket)

        env = os.environ.copy()
        env["HOME"] = str(home_dir)
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(_PLUGIN_SRC)!r})\n"
            "from macos_self_deployment_plugin import stale_runtime_cleanup\n"
            f"stale_runtime_cleanup.cleanup_stale_runtime_files({SMOKE_SOLET_NAME!r})\n"
            "restored = stale_runtime_cleanup."
            f"restore_router_owned_bridge_port_file_if_router_live({SMOKE_SOLET_NAME!r})\n"
            "raise SystemExit(0 if restored else 1)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        server.join(timeout=2.0)

        invoke_ok = proc.returncode == 0
        _stamp(
            "cleanup + router-owned repair invoked cleanly",
            invoke_ok,
            f"stderr={proc.stderr!r}" if not invoke_ok else "",
        )
        if not invoke_ok:
            return False

        restored_content = _read_port_file(bridge_port)
        restored_mode = _file_mode(bridge_port)
        content_ok = restored_content == 7777
        mode_ok = restored_mode == 0o600
        router_preserved = _read_port_file(router_port) == 7777
        _stamp("bridge.port restored from router.port", content_ok, f"value={restored_content}")
        _stamp("bridge.port mode 0o600", mode_ok, f"mode={restored_mode:#o}")
        _stamp("router.port preserved", router_preserved, str(router_port))

        return content_ok and mode_ok and router_preserved


def main() -> int:
    import platform

    if platform.system() not in ("Darwin", "Linux"):
        print(f"smoke skip: unsupported platform {platform.system()}")
        return 0

    _belt_and_suspenders_bootout()
    tmpdir = Path(tempfile.mkdtemp(prefix="bg-bridge-port-lifecycle-smoke-"))
    home_dir = tmpdir / "home"
    home_dir.mkdir()
    # Short socket dir under tempfile root keeps AF_UNIX path below the
    # macOS 104-char ceiling.
    socket_dir = Path(tempfile.mkdtemp(prefix="bg-sock-"))
    print(
        f"bridge_port_lifecycle_smoke: tmp={tmpdir} home={home_dir} "
        f"socket_dir={socket_dir} name={SMOKE_SOLET_NAME!r}"
    )

    results: list[tuple[str, bool]] = []
    try:
        results.append((
            "slice1_router_writes_both_files",
            _case_slice1_router_writes_both_files(home_dir, tmpdir, socket_dir),
        ))
        results.append((
            "slice15_cleanup_removes_stale_bridge_port",
            _case_slice15_cleanup_removes_stale_bridge_port(home_dir),
        ))
        results.append((
            "slice15_repair_restores_bridge_port_from_live_router",
            _case_slice15_repair_restores_bridge_port_from_live_router(),
        ))
    finally:
        plist_path = tmpdir / "LaunchAgents" / f"{launchd_label(SMOKE_SOLET_NAME)}.plist"
        unit_path = tmpdir / "systemd" / systemd_unit_name(SMOKE_SOLET_NAME)
        socket_path = socket_dir / f"{SMOKE_SOLET_NAME}.router.sock"
        _run_uninstall(
            home_dir=home_dir, plist_path=plist_path,
            unit_path=unit_path, socket_path=socket_path,
        )
        _belt_and_suspenders_bootout()

    print("\nsummary")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        _stamp(name, ok)
    print(f"\n{passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
