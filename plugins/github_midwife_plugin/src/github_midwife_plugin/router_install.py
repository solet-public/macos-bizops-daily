"""SEED-06 — install the per-solet blue-green router at birth.

A genesis spine phase that runs right after the main autostart LaunchAgent
(`autostart.SimpleAutostartRenderer`). Design: `workbench/2026-07-18_seed06_
router_in_seed_design.md` §4 D1 (Q1 RULED — genesis auto-step, zero operator
action) and §5.

**Conditional (SEED-06 Q3).** Only a newborn whose profile allowlist includes
`macos_self_deployment_plugin` ships the router code (`macos-bizops`, NOT
`macos_free_minimal`). A free-tier newborn is single-color by design and this
phase SKIPS cleanly. When the plugin IS in the allowlist the phase is
FAIL-LOUD — a solet that boots believing it can blue-green but silently
cannot is worse than a loud birth failure (design §5).

The heavy lifting (dynamic port pick in 8800-8999, plist/systemd-unit render,
`launchctl bootstrap`, readiness verify) lives in the shipped installer
`macos_self_deployment_plugin/blue_green_router/install_router.py`; this module
only INVOKES it, as a subprocess run by the NEWBORN's own venv python. That is
deliberate: the installer renders the plist's `ProgramArguments` from
`sys.executable`, so running it under the newborn's venv is what makes the
router daemon launch from — and import — the newborn's own plugin code (the
same newborn-venv-subprocess seam `credential_seed` uses via `venv_provision`).
`install_router` is idempotent, so a re-run at a later birth attempt is a
no-op success.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess[str]]

SELF_DEPLOYMENT_PLUGIN = "macos_self_deployment_plugin"

# Path to the shipped installer, relative to the clone root. The router is a
# subpackage of macos_self_deployment_plugin, so an assembled macos-bizops seed that
# carries the plugin carries this file (assemble ships the plugin subtree
# verbatim); a free seed omits the plugin dir entirely.
_INSTALL_ROUTER_RELPATH = (
    Path("plugins") / SELF_DEPLOYMENT_PLUGIN / "src" / SELF_DEPLOYMENT_PLUGIN
    / "blue_green_router" / "install_router.py"
)

# Generous ceiling over install_router's own internal budget (a ~5s readiness
# verify + a launchctl bootstrap + a port scan). Set well above the real worst
# case so a slow-but-succeeding install is never killed by this wrapper; a
# genuine hang still fails loud rather than blocking the birth forever.
_INSTALL_TIMEOUT_S = 60.0


class RouterInstallError(RuntimeError):
    """The blue-green router could not be installed for a capable newborn."""


@dataclass(frozen=True, slots=True)
class RouterInstallResult:
    status: str  # "installed" | "skipped"
    reason: str


def install_router_at_birth(
    *,
    name: str,
    clone_root: Path,
    plugin_allowlist: Sequence[str],
    run: Runner,
) -> RouterInstallResult:
    """Install the blue-green router for `name` iff the newborn is blue-green-capable.

    Returns a ``skipped`` result for a free-tier newborn (no self-deployment
    plugin in the allowlist). Raises :class:`RouterInstallError` for a
    blue-green-capable newborn whose router installer is missing, whose venv is
    absent, or whose install fails — birth must not proceed believing in a
    router that is not there.
    """
    if SELF_DEPLOYMENT_PLUGIN not in plugin_allowlist:
        return RouterInstallResult(
            status="skipped",
            reason=f"single-color: {SELF_DEPLOYMENT_PLUGIN} not in profile allowlist",
        )

    installer = clone_root / _INSTALL_ROUTER_RELPATH
    if not installer.is_file():
        raise RouterInstallError(
            f"{SELF_DEPLOYMENT_PLUGIN} is in the profile allowlist (newborn is "
            f"blue-green-capable) but its router installer is missing at "
            f"{installer} — the seed did not ship the blue_green_router assets."
        )

    venv_python = clone_root / ".venv" / "bin" / "python3"
    if not venv_python.is_file():
        raise RouterInstallError(
            f"newborn venv python missing at {venv_python} — the venv must be "
            "provisioned before genesis installs the router."
        )

    try:
        result = run(
            [str(venv_python), str(installer), name],
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RouterInstallError(
            f"install_router for {name!r} could not be executed: {exc}"
        ) from exc
    if result.returncode != 0:
        raise RouterInstallError(
            f"install_router for {name!r} exited {result.returncode}: "
            f"{(result.stderr or '').strip()}"
        )
    return RouterInstallResult(
        status="installed", reason="blue-green router installed at birth"
    )


__all__ = [
    "SELF_DEPLOYMENT_PLUGIN",
    "RouterInstallError",
    "RouterInstallResult",
    "Runner",
    "install_router_at_birth",
]
