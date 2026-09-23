"""Install the local blue-green router under launchd (macOS) or systemd (Linux).

Per L3 plan §3.6 and Slice 1 of the bridge-port-routing-and-session-
lifecycle design (both dev-checkout workbench records — not part of the
shipped tree).

Idempotent: re-running on an already-installed system is a no-op success.
Fast-fail: any subprocess error surfaces verbatim — no fallbacks, no retries.

Usage:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/install_router.py <solet_name> \\
        [--public-port <PORT>]

When --public-port is omitted (the canonical path per operator directive
D-BRIDGE-1 — all port assignment dynamic), install_router scans the dynamic
router-port range (DYNAMIC_ROUTER_PORT_START..DYNAMIC_ROUTER_PORT_END,
currently 8800-8999) for a free port. The --public-port flag is retained
only as an operator-debug override. This lets multiple solets on the
same host each install their own router without colliding on a fixed port.

The chosen port is written to TWO discovery files under
``~/.ananta/runtime/``:

* ``<name>.router.port`` — read by port_manager.py to skip the router's
  port from solet allocation bands.
* ``<name>.bridge.port`` — read by the MCP bridge subprocess to discover
  the router as the single ingress for ``<name>`` (Slice 1: router becomes
  the canonical writer of this file; previously solet children wrote it
  per-color and the canonical drifted).

Path overrides (smoke harness only):
    --plist-path <PATH>      Override default ~/Library/LaunchAgents/local.solet.<name>.router.plist
    --unit-path <PATH>       Override default ~/.config/systemd/user/local.solet.<name>.router.service
    --socket-path <PATH>     Override default ~/.ananta/runtime/<name>.router.sock
    --log-dir <PATH>         Override default ~/.ananta/logs
    --runtime-dir <PATH>     Override default ~/.ananta/runtime (port-discovery
                             files + the router daemon's WorkingDirectory)

Every host path the installer writes is reachable through one of these
overrides; a smoke that sets all of them touches nothing outside its own
tmp root (iss_6e8c204c closed the port-discovery files, which previously
bypassed the overrides and landed in the REAL runtime dir).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket as socket_mod
import subprocess
import sys
import time
from pathlib import Path

# Self-bootstrap so the script runs from any CWD; the deployment/ namespace
# package only resolves when the repo root is on sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from macos_self_deployment_plugin.blue_green_router.service_install import (  # noqa: E402
    LAUNCHD_TEMPLATE_NAME,
    LOG_DIR,
    RUNTIME_DIR,
    SYSTEMD_TEMPLATE_NAME,
    InstallError,
    default_launchd_plist_path,
    default_socket_path,
    default_systemd_unit_path,
    launchd_label,
    python_bin,
    render_template,
    systemd_unit_name,
    validate_solet_name,
)

VERIFY_DEADLINE_SECONDS: float = 5.0
VERIFY_POLL_INTERVAL_SECONDS: float = 0.1
LAUNCHD_TEARDOWN_DEADLINE_SECONDS: float = 5.0
LAUNCHD_TEARDOWN_POLL_INTERVAL_SECONDS: float = 0.1
# launchctl exits 3 (ESRCH "No such process") when bootout-ing a label that
# isn't loaded. Treat it as the no-op success we want for idempotent reload.
LAUNCHCTL_BOOTOUT_NOT_LOADED_EXIT: int = 3

# Dynamic router port range. Lives ABOVE solet allocation bands
# (port_manager.py: blue 8101-8149, green 8150-8198, probe 8200-8299) so
# the router never collides with the solet — and so multiple solets on the
# same host can each pick a different free router port without contention.
DYNAMIC_ROUTER_PORT_START: int = 8800
DYNAMIC_ROUTER_PORT_END: int = 8999


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        validate_solet_name(args.solet_name)
        if args.public_port is None:
            args.public_port = _find_free_router_port()
        system_name = platform.system()
        if system_name == "Darwin":
            _install_launchd(args)
        elif system_name == "Linux":
            _install_systemd(args)
        else:
            raise InstallError(
                f"unsupported platform {system_name!r}; supported: Darwin, Linux",
            )
        socket_path = args.socket_path or default_socket_path(args.solet_name)
        _verify_router_up(socket_path)
        _write_router_port_files(args.solet_name, args.public_port, _runtime_dir(args))
    except InstallError as exc:
        print(f"install_router: {exc}", file=sys.stderr)
        return 1
    print(
        f"install_router: OK — router for solet={args.solet_name!r} "
        f"is up on port {args.public_port}",
    )
    return 0


def _find_free_router_port() -> int:
    """Scan the dynamic router-port range for a free port.

    Used when --public-port is omitted. Skips ports already bound on
    127.0.0.1 so multi-solet installs each pick distinct ports.
    """
    for port in range(DYNAMIC_ROUTER_PORT_START, DYNAMIC_ROUTER_PORT_END + 1):
        try:
            with socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM) as sock:
                sock.setsockopt(socket_mod.SOL_SOCKET, socket_mod.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    raise InstallError(
        f"no available router port in "
        f"{DYNAMIC_ROUTER_PORT_START}-{DYNAMIC_ROUTER_PORT_END}",
    )


def _runtime_dir(args: argparse.Namespace) -> Path:
    """The runtime dir this install targets: ``--runtime-dir`` or the real one.

    Resolved in exactly one place so every runtime-dir write in this
    module (port-discovery files, the daemon's WorkingDirectory) honours
    the smoke override; a second ``RUNTIME_DIR`` reference here is the
    sandbox escape iss_6e8c204c was.
    """
    return args.runtime_dir or RUNTIME_DIR


def _write_router_port_files(
    solet_name: str, port: int, runtime_dir: Path,
) -> tuple[Path, Path]:
    """Write the router's chosen public port to both discovery files.

    Two files in ``runtime_dir`` (``~/.ananta/runtime/`` for a real
    install) receive the same port value:

    * ``<name>.router.port`` — read by ``port_manager.py`` so this solet's
      allocation bands skip the router's port. Kept as a distinct file
      for backward compatibility with port_manager's existing reader.
    * ``<name>.bridge.port`` — read by the MCP bridge subprocess via
      ``read_port_file(service_name='bridge', solet_name=<name>)``
      to discover the router as the single ingress for the solet.

    Slice 1 of the bridge-port-routing-and-session-lifecycle design makes
    the router the canonical writer of ``<name>.bridge.port``; solet children
    are decoupled from canonical-file ownership (per-color sibling files
    continue to be written by solet children until Slice 3 retires them).
    """
    runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    router_port_file = runtime_dir / f"{solet_name}.router.port"
    router_port_file.write_text(str(port))
    router_port_file.chmod(0o600)
    bridge_port_file = runtime_dir / f"{solet_name}.bridge.port"
    bridge_port_file.write_text(str(port))
    bridge_port_file.chmod(0o600)
    return router_port_file, bridge_port_file


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="install_router",
        description=(
            "Install the local blue-green router under launchd/systemd. "
            "Idempotent: re-running on an installed system is a no-op success."
        ),
    )
    parser.add_argument("solet_name", help="Solet name (e.g. 'iris').")
    parser.add_argument(
        "--public-port", type=int, default=None,
        help=(
            f"Public bridge port. Operator-debug override; the canonical "
            f"path leaves this unset so install_router scans the dynamic "
            f"range {DYNAMIC_ROUTER_PORT_START}-{DYNAMIC_ROUTER_PORT_END} "
            f"for a free port (per operator directive D-BRIDGE-1: all port "
            f"assignment dynamic)."
        ),
    )
    parser.add_argument(
        "--plist-path", type=Path, default=None,
        help="Override default ~/Library/LaunchAgents/<label>.plist (smoke only).",
    )
    parser.add_argument(
        "--unit-path", type=Path, default=None,
        help="Override default ~/.config/systemd/user/<unit>.service (smoke only).",
    )
    parser.add_argument(
        "--socket-path", type=Path, default=None,
        help="Override default ~/.ananta/runtime/<name>.router.sock (smoke only).",
    )
    parser.add_argument(
        "--log-dir", type=Path, default=None,
        help="Override default ~/.ananta/logs (smoke only).",
    )
    parser.add_argument(
        "--runtime-dir", type=Path, default=None,
        help=(
            "Override default ~/.ananta/runtime for the port-discovery files "
            "and the router daemon's WorkingDirectory (smoke only)."
        ),
    )
    return parser.parse_args(argv)


def _build_context(args: argparse.Namespace) -> dict[str, str]:
    socket_path = args.socket_path or default_socket_path(args.solet_name)
    log_dir = args.log_dir or LOG_DIR
    return {
        "LAUNCHD_LABEL": launchd_label(args.solet_name),
        "PYTHON_BIN": python_bin(),
        # §5 CWD hygiene (design 2026-06-27): the router daemon's
        # WorkingDirectory must be out-of-tree, NOT the repo root — a stray
        # relative-path write under a code tree would pollute it. The runtime
        # dir is the canonical out-of-tree home (also where the router's
        # socket + port files live), created in ``_ensure_dirs``.
        "WORKING_DIR": str(_runtime_dir(args)),
        # Also handed to the daemon as ``--runtime-dir``: it self-writes the
        # port-discovery files at bind time and on every watchdog tick, under
        # the REAL ``HOME`` of launchd/systemd, so the installer's own
        # override has to travel into the service definition (iss_6e8c204c).
        "RUNTIME_DIR": str(_runtime_dir(args)),
        "SOLET_NAME": args.solet_name,
        "PUBLIC_PORT": str(args.public_port),
        "SOCKET_PATH": str(socket_path),
        "LOG_DIR": str(log_dir),
    }


def _ensure_dirs(
    plist_or_unit_path: Path, socket_path: Path, log_dir: Path, runtime_dir: Path,
) -> None:
    plist_or_unit_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    # The router daemon's WorkingDirectory (§5 CWD hygiene). Canonically the
    # same dir as socket_path.parent, but created explicitly so a smoke that
    # overrides --socket-path elsewhere still has a valid out-of-tree CWD.
    runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)


def _install_launchd(args: argparse.Namespace) -> None:
    plist_path = args.plist_path or default_launchd_plist_path(args.solet_name)
    socket_path = args.socket_path or default_socket_path(args.solet_name)
    log_dir = args.log_dir or LOG_DIR
    _ensure_dirs(plist_path, socket_path, log_dir, _runtime_dir(args))
    context = _build_context(args)
    plist_path.write_text(render_template(LAUNCHD_TEMPLATE_NAME, context), encoding="utf-8")
    label = launchd_label(args.solet_name)
    domain_target = f"gui/{os.getuid()}"
    service_target = f"{domain_target}/{label}"
    # Idempotent reload: bootout swallows "not loaded" (exit 113), then
    # bootstrap installs cleanly. kickstart guarantees the process is
    # actually running even if launchd didn't auto-start it.
    _run_launchctl(
        ["bootout", service_target],
        allow_exit_codes={0, LAUNCHCTL_BOOTOUT_NOT_LOADED_EXIT},
    )
    # launchctl bootout returns before launchd has fully torn down the prior
    # registration; bootstrap on the same label then fails with EIO (exit 5).
    # Poll launchctl print until the label is gone before re-bootstrapping.
    _wait_for_label_gone(service_target)
    _run_launchctl(["bootstrap", domain_target, str(plist_path)], allow_exit_codes={0})
    _run_launchctl(["kickstart", "-k", service_target], allow_exit_codes={0})


def _wait_for_label_gone(service_target: str) -> None:
    deadline = time.monotonic() + LAUNCHD_TEARDOWN_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["launchctl", "print", service_target],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return
        time.sleep(LAUNCHD_TEARDOWN_POLL_INTERVAL_SECONDS)
    # Don't raise — bootstrap will surface the actual error if still loaded.


def _install_systemd(args: argparse.Namespace) -> None:
    unit_path = args.unit_path or default_systemd_unit_path(args.solet_name)
    socket_path = args.socket_path or default_socket_path(args.solet_name)
    log_dir = args.log_dir or LOG_DIR
    _ensure_dirs(unit_path, socket_path, log_dir, _runtime_dir(args))
    context = _build_context(args)
    unit_path.write_text(render_template(SYSTEMD_TEMPLATE_NAME, context), encoding="utf-8")
    unit_name = systemd_unit_name(args.solet_name)
    _run_systemctl(["daemon-reload"], allow_exit_codes={0})
    _run_systemctl(["enable", "--now", unit_name], allow_exit_codes={0})


def _run_launchctl(args: list[str], *, allow_exit_codes: set[int]) -> subprocess.CompletedProcess[str]:
    return _run_supervisor("launchctl", args, allow_exit_codes=allow_exit_codes)


def _run_systemctl(args: list[str], *, allow_exit_codes: set[int]) -> subprocess.CompletedProcess[str]:
    return _run_supervisor("systemctl", ["--user", *args], allow_exit_codes=allow_exit_codes)


def _run_supervisor(
    program: str,
    args: list[str],
    *,
    allow_exit_codes: set[int],
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [program, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in allow_exit_codes:
        raise InstallError(
            f"{program} {' '.join(args)} exited {result.returncode}\n"
            f"stdout: {result.stdout.rstrip()}\n"
            f"stderr: {result.stderr.rstrip()}",
        )
    return result


def _verify_router_up(socket_path: Path) -> None:
    deadline = time.monotonic() + VERIFY_DEADLINE_SECONDS
    last_error: str = "socket never appeared"
    while time.monotonic() < deadline:
        if socket_path.exists():
            try:
                status = _mgmt_status(socket_path)
            except (OSError, ValueError) as exc:
                last_error = f"mgmt status() failed: {exc}"
            else:
                if "router_started_at" in status:
                    return
                last_error = f"mgmt status() returned unexpected payload: {status!r}"
        time.sleep(VERIFY_POLL_INTERVAL_SECONDS)
    raise InstallError(
        f"router did not become ready within {VERIFY_DEADLINE_SECONDS}s at "
        f"{socket_path}: {last_error}",
    )


def _mgmt_status(socket_path: Path) -> dict[str, object]:
    with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        sock.connect(str(socket_path))
        sock.sendall(json.dumps({"verb": "status", "args": {}}).encode() + b"\n")
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
    line = bytes(buf).split(b"\n", 1)[0]
    payload = json.loads(line.decode())
    if not isinstance(payload, dict):
        raise ValueError(f"non-object mgmt reply: {payload!r}")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
