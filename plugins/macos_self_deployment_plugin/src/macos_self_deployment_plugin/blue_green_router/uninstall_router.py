"""Uninstall the local blue-green router from launchd (macOS) or systemd (Linux).

Per L3 plan §3.6 (`workbench/2026-06-01_local_blue_green_L3_implementation_plan.md`).

Symmetric to install_router.py. Idempotent: re-running on an already-uninstalled
system is a no-op success. Fast-fail on any unexpected supervisor exit code.

Usage:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/uninstall_router.py <solet_name>

Path overrides (smoke harness only):
    --plist-path <PATH>
    --unit-path <PATH>
    --socket-path <PATH>
    --runtime-dir <PATH>     where install_router wrote the port-discovery
                             files (default ~/.ananta/runtime; iss_6e8c204c)
"""

from __future__ import annotations

import argparse
import os
import platform
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
    RUNTIME_DIR,
    InstallError,
    default_launchd_plist_path,
    default_socket_path,
    default_systemd_unit_path,
    launchd_label,
    systemd_unit_name,
    validate_solet_name,
)

SOCKET_CLEANUP_DEADLINE_SECONDS: float = 5.0
SOCKET_CLEANUP_POLL_INTERVAL_SECONDS: float = 0.1


def _remove_router_port_files(solet_name: str, runtime_dir: Path) -> None:
    """Remove both router-owned port-discovery files written by install_router.

    ``runtime_dir`` is the same dir install_router wrote into (``--runtime-dir``
    or the real one), so a sandboxed smoke's uninstall never reaches the
    operator's real files. Missing files are no-op successes.
    """
    for port_file in (
        runtime_dir / f"{solet_name}.router.port",
        runtime_dir / f"{solet_name}.bridge.port",
    ):
        port_file.unlink(missing_ok=True)

# launchctl exits 3 (ESRCH "No such process") when bootout-ing a label that
# isn't loaded. Treat it as the no-op success we want for idempotent uninstall.
LAUNCHCTL_BOOTOUT_NOT_LOADED_EXIT: int = 3
SYSTEMCTL_NOT_LOADED_EXIT: int = 5


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        validate_solet_name(args.solet_name)
        system_name = platform.system()
        if system_name == "Darwin":
            _uninstall_launchd(args)
        elif system_name == "Linux":
            _uninstall_systemd(args)
        else:
            raise InstallError(
                f"unsupported platform {system_name!r}; supported: Darwin, Linux",
            )
        socket_path = args.socket_path or default_socket_path(args.solet_name)
        _verify_socket_gone(socket_path)
        _remove_router_port_files(args.solet_name, args.runtime_dir or RUNTIME_DIR)
    except InstallError as exc:
        print(f"uninstall_router: {exc}", file=sys.stderr)
        return 1
    print(
        f"uninstall_router: OK — router for solet={args.solet_name!r} "
        "is stopped and removed",
    )
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="uninstall_router",
        description=(
            "Uninstall the local blue-green router from launchd/systemd. "
            "Idempotent: re-running on an uninstalled system is a no-op success."
        ),
    )
    parser.add_argument("solet_name", help="Solet name (e.g. 'iris').")
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
        "--runtime-dir", type=Path, default=None,
        help=(
            "Override default ~/.ananta/runtime, where install_router wrote "
            "the port-discovery files (smoke only)."
        ),
    )
    return parser.parse_args(argv)


def _uninstall_launchd(args: argparse.Namespace) -> None:
    plist_path = args.plist_path or default_launchd_plist_path(args.solet_name)
    label = launchd_label(args.solet_name)
    service_target = f"gui/{os.getuid()}/{label}"
    # Bootout first so KeepAlive can't respawn after we unlink the plist.
    _run_launchctl(
        ["bootout", service_target],
        allow_exit_codes={0, LAUNCHCTL_BOOTOUT_NOT_LOADED_EXIT},
    )
    plist_path.unlink(missing_ok=True)


def _uninstall_systemd(args: argparse.Namespace) -> None:
    unit_path = args.unit_path or default_systemd_unit_path(args.solet_name)
    unit_name = systemd_unit_name(args.solet_name)
    _run_systemctl(
        ["disable", "--now", unit_name],
        allow_exit_codes={0, SYSTEMCTL_NOT_LOADED_EXIT},
    )
    unit_path.unlink(missing_ok=True)
    _run_systemctl(["daemon-reload"], allow_exit_codes={0})


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


def _verify_socket_gone(socket_path: Path) -> None:
    deadline = time.monotonic() + SOCKET_CLEANUP_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if not socket_path.exists():
            return
        time.sleep(SOCKET_CLEANUP_POLL_INTERVAL_SECONDS)
    raise InstallError(
        f"socket {socket_path} still present after {SOCKET_CLEANUP_DEADLINE_SECONDS}s "
        "— supervisor reported stop but router did not unlink its socket",
    )


if __name__ == "__main__":
    raise SystemExit(main())
