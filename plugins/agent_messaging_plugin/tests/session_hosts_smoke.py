#!/usr/bin/env python3
"""Unit smoke for the D1 ``HostDriver`` Protocol + the ``operator``/
``headless`` drivers (§5). ``tmux`` lands in a later D-step. Uses
resolution + config-remedy checks only, never a real subprocess spawn (that
lives in ``headless_adapter_smoke.py``, which injects a fake ``popen_fn``).

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/session_hosts_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import session_hosts  # noqa: E402
from agent_messaging_plugin.session_hosts import (  # noqa: E402
    OPERATOR_HOST,
    HostCannotSpawnError,
    HostMechanismMissingError,
    HostNotDeclaredError,
    resolve_host_driver,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def main() -> int:
    os.environ.pop("FLEET_SESSION_HOST", None)

    driver, host = resolve_host_driver(OPERATOR_HOST)
    _check(
        host == OPERATOR_HOST,
        "an explicit 'operator' override resolves to the operator driver",
    )

    cannot_spawn = False
    try:
        driver.spawn({})
    except HostCannotSpawnError as exc:
        cannot_spawn = True
        _check("launch manually" in str(exc), "the refusal names the manual launch remedy")
    _check(
        cannot_spawn,
        "operator.spawn() refuses with HostCannotSpawnError (degenerate, never spawns)",
    )

    tmux_driver, tmux_host = resolve_host_driver(None)  # falls to DEFAULT_HOST='tmux'
    _check(
        tmux_host == "tmux",
        "no override + no env -> safe default 'tmux' -> a REGISTERED driver",
    )
    headless_driver, headless_host = resolve_host_driver("headless")
    os.environ.pop("FLEET_HEADLESS_PERMISSION_MODE", None)
    headless_cannot_spawn = False
    try:
        headless_driver.spawn({"agent_instance_id": "agi-test"})
    except HostCannotSpawnError as exc:
        headless_cannot_spawn = True
        _check(
            "permission mode" in str(exc),
            "the unconfigured-permission-mode remedy is in the refusal text",
        )
    _check(
        headless_cannot_spawn,
        "an unconfigured headless driver fails closed with host_cannot_spawn "
        "(config remedies), never a silent bypass-permissions default",
    )

    # "screen" is explicitly documented unsupported (skeleton §2) and never
    # gets a driver — "tmux" no longer works for this case now that D2
    # registered it (session_hosts_smoke.py must track the registry, not
    # assume any one name stays forever unregistered).
    os.environ["FLEET_SESSION_HOST"] = "screen"
    env_missing = False
    try:
        resolve_host_driver(None)
    except HostMechanismMissingError as exc:
        env_missing = True
        _check(
            exc.host == "screen",
            "the FLEET_SESSION_HOST env default is honored over the built-in default",
        )
    _check(env_missing, "an env-declared host with no driver also fails loud, not silently")
    os.environ.pop("FLEET_SESSION_HOST", None)

    tmux_driver, tmux_host = resolve_host_driver("tmux")
    _check(
        tmux_host == "tmux" and tmux_driver is not None,
        "an explicit 'tmux' override resolves to the now-registered tmux driver (D2)",
    )

    blank_declared = False
    try:
        resolve_host_driver("")
    except HostNotDeclaredError:
        blank_declared = True
    _check(
        blank_declared,
        "an explicit blank host override raises HostNotDeclaredError, distinct "
        "from HostMechanismMissingError",
    )

    _check(
        driver.alive("anything") is True,
        "operator driver reports alive=True (observation only)",
    )
    _check(driver.driver_channel("anything") is None, "operator driver has no driver channel")
    _check(driver.verify_config() == [], "operator driver's config is always ready (no remedies)")

    original_which = session_hosts.shutil.which
    original_popen = session_hosts.subprocess.Popen
    session_hosts.shutil.which = lambda _name: None

    def _unexpected_popen(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("missing bridge guard invoked subprocess.Popen")

    session_hosts.subprocess.Popen = _unexpected_popen  # type: ignore[assignment]
    missing_bridge_refused = False
    try:
        session_hosts.QualificationHostDriver().spawn(
            {"agent_instance_id": "agi-qualification", "local_name": "qualification-probe"},
        )
    except HostCannotSpawnError as exc:
        missing_bridge_refused = "solet-bridge is required for qualification host" in str(exc)
    except AssertionError:
        missing_bridge_refused = False
    finally:
        session_hosts.shutil.which = original_which
        session_hosts.subprocess.Popen = original_popen  # type: ignore[assignment]
    _check(
        missing_bridge_refused,
        "qualification host refuses a missing solet-bridge before subprocess spawn",
    )

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
