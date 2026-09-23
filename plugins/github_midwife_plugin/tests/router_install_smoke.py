"""SEED-06 unit smoke — `router_install.install_router_at_birth` branches.

Red-first coverage of the genesis router-install phase in isolation (the
end-to-end wiring is exercised by genesis_integration_smoke):

  * SKIP  — a free-tier newborn (no macos_self_deployment_plugin in the
            allowlist) skips cleanly and NEVER invokes the installer.
  * INSTALL — a blue-green-capable newborn invokes the shipped installer as
            a subprocess run by its OWN venv python, with argv
            [<venv python>, <install_router.py>, <name>].
  * FAIL-LOUD — a capable newborn whose installer is missing, whose venv is
            missing, whose install exits non-zero, or whose install cannot be
            executed at all raises RouterInstallError (birth must not proceed
            believing in a router that is not there).

Fully sandboxed: a fake Runner records the argv it would run and returns a
canned CompletedProcess; no real launchctl, port bind, or subprocess.

Run directly: ``.venv/bin/python3
plugins/github_midwife_plugin/tests/router_install_smoke.py``.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "github_midwife_plugin" / "src"))

from github_midwife_plugin.router_install import (  # noqa: E402
    SELF_DEPLOYMENT_PLUGIN,
    RouterInstallError,
    install_router_at_birth,
)

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


class _FakeRunner:
    """Records argv + kwargs; returns a canned CompletedProcess (or raises)."""

    def __init__(
        self, returncode: int = 0, stderr: str = "", raises: Exception | None = None
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(
            args=cmd, returncode=self.returncode, stdout="", stderr=self.stderr
        )


def _make_clone(root: Path, *, with_installer: bool = True, with_venv: bool = True) -> Path:
    """Minimal clone tree; toggle the router installer + venv presence."""
    clone = root / "clone"
    if with_venv:
        (clone / ".venv" / "bin").mkdir(parents=True)
        (clone / ".venv" / "bin" / "python3").write_text("#!/bin/sh\n")
    else:
        clone.mkdir(parents=True, exist_ok=True)
    if with_installer:
        installer_dir = (
            clone / "plugins" / SELF_DEPLOYMENT_PLUGIN / "src" / SELF_DEPLOYMENT_PLUGIN
            / "blue_green_router"
        )
        installer_dir.mkdir(parents=True)
        (installer_dir / "install_router.py").write_text("# stub installer\n")
    return clone


def _installer_path(clone: Path) -> Path:
    return (
        clone / "plugins" / SELF_DEPLOYMENT_PLUGIN / "src" / SELF_DEPLOYMENT_PLUGIN
        / "blue_green_router" / "install_router.py"
    )


def _check_skips_for_free_tier(root: Path) -> None:
    clone = _make_clone(root, with_installer=False)
    runner = _FakeRunner()
    result = install_router_at_birth(
        name="freehum", clone_root=clone,
        plugin_allowlist=["github_midwife_plugin"], run=runner,
    )
    _check("free-tier newborn skips the router", result.status == "skipped", result.reason)
    _check("free-tier skip never invokes the installer", runner.calls == [], str(runner.calls))


def _check_installs_for_capable_newborn(root: Path) -> None:
    clone = _make_clone(root)
    runner = _FakeRunner(returncode=0)
    result = install_router_at_birth(
        name="bizhum", clone_root=clone,
        plugin_allowlist=["github_midwife_plugin", SELF_DEPLOYMENT_PLUGIN], run=runner,
    )
    _check("capable newborn installs the router", result.status == "installed", result.reason)
    _check("installer invoked exactly once", len(runner.calls) == 1, str(runner.calls))
    expected = [
        str(clone / ".venv" / "bin" / "python3"),
        str(_installer_path(clone)),
        "bizhum",
    ]
    _check(
        "installer invoked with the newborn's venv python + installer + name",
        runner.calls[0] == expected,
        f"got {runner.calls[0]} expected {expected}",
    )


def _check_fails_loud_when_installer_missing(root: Path) -> None:
    clone = _make_clone(root, with_installer=False)
    runner = _FakeRunner()
    try:
        install_router_at_birth(
            name="bizhum", clone_root=clone,
            plugin_allowlist=[SELF_DEPLOYMENT_PLUGIN], run=runner,
        )
    except RouterInstallError as exc:
        _check(
            "missing installer raises RouterInstallError naming the missing path",
            "install_router.py" in str(exc) and "did not ship" in str(exc),
            str(exc),
        )
        _check("missing installer never invokes the runner", runner.calls == [], str(runner.calls))
    else:
        raise SmokeFailureError("missing-installer: install_router_at_birth did not raise")


def _check_fails_loud_when_venv_missing(root: Path) -> None:
    clone = _make_clone(root, with_venv=False)
    runner = _FakeRunner()
    try:
        install_router_at_birth(
            name="bizhum", clone_root=clone,
            plugin_allowlist=[SELF_DEPLOYMENT_PLUGIN], run=runner,
        )
    except RouterInstallError as exc:
        _check(
            "missing venv raises RouterInstallError naming the venv python",
            "venv python missing" in str(exc),
            str(exc),
        )
    else:
        raise SmokeFailureError("missing-venv: install_router_at_birth did not raise")


def _check_fails_loud_when_install_nonzero(root: Path) -> None:
    clone = _make_clone(root)
    runner = _FakeRunner(returncode=1, stderr="install_router: no available router port")
    try:
        install_router_at_birth(
            name="bizhum", clone_root=clone,
            plugin_allowlist=[SELF_DEPLOYMENT_PLUGIN], run=runner,
        )
    except RouterInstallError as exc:
        _check(
            "non-zero install exit raises RouterInstallError carrying the exit + stderr",
            "exited 1" in str(exc) and "no available router port" in str(exc),
            str(exc),
        )
    else:
        raise SmokeFailureError("nonzero-exit: install_router_at_birth did not raise")


def _check_fails_loud_when_install_cannot_run(root: Path) -> None:
    clone = _make_clone(root)
    runner = _FakeRunner(raises=OSError("launchctl not found"))
    try:
        install_router_at_birth(
            name="bizhum", clone_root=clone,
            plugin_allowlist=[SELF_DEPLOYMENT_PLUGIN], run=runner,
        )
    except RouterInstallError as exc:
        _check(
            "an unrunnable installer raises RouterInstallError",
            "could not be executed" in str(exc),
            str(exc),
        )
    else:
        raise SmokeFailureError("unrunnable: install_router_at_birth did not raise")


def main() -> int:
    cases = (
        _check_skips_for_free_tier,
        _check_installs_for_capable_newborn,
        _check_fails_loud_when_installer_missing,
        _check_fails_loud_when_venv_missing,
        _check_fails_loud_when_install_nonzero,
        _check_fails_loud_when_install_cannot_run,
    )
    try:
        for case in cases:
            with tempfile.TemporaryDirectory() as tmp:
                case(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"router_install_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
