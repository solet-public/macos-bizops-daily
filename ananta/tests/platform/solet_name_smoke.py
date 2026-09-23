"""Hard-error verification for EnvironmentConfig.solet_name().

Covers Task #16: the silent ``'ananta'`` default was deleted; an unset
SOLET_NAME now hard-errors with a discoverable hint. The smoke
verifies both the env-var path and the port_manager.py callers route
through the canonical accessor.

Run: .venv/bin/python3 ananta/tests/platform/solet_name_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _print(label: str, ok: bool, detail: str = "") -> None:
    sym = "PASS" if ok else "FAIL"
    print(f"  [{sym}] {label}" + (f" — {detail}" if detail else ""))


def test_hard_error_when_unset() -> bool:
    """SOLET_NAME unset → RuntimeError with discoverability hint."""
    from ananta.core.config.environment_config import EnvironmentConfig

    saved = os.environ.pop("SOLET_NAME", None)
    try:
        try:
            EnvironmentConfig.solet_name()
        except RuntimeError as exc:
            msg = str(exc)
            checks = [
                ("names env var", "SOLET_NAME" in msg),
                ("names APP_HOME", "APP_HOME" in msg),
                # The example name is deliberately neutral ('example'), not the
                # origin solet's name — seed surfaces carry no origin name.
                ("names example", "example" in msg.lower()),
                ("points at conventions doc", "cli_conventions" in msg),
            ]
            all_ok = all(ok for _, ok in checks)
            for label, ok in checks:
                _print(f"  message check: {label}", ok)
            return all_ok
        else:
            _print("expected RuntimeError, got none", False)
            return False
    finally:
        if saved is not None:
            os.environ["SOLET_NAME"] = saved


def test_returns_env_var_when_set() -> bool:
    """SOLET_NAME set → returns the value."""
    from ananta.core.config.environment_config import EnvironmentConfig

    saved = os.environ.get("SOLET_NAME")
    os.environ["SOLET_NAME"] = "smoke_test_solet"
    try:
        actual = EnvironmentConfig.solet_name()
        return actual == "smoke_test_solet"
    finally:
        if saved is None:
            os.environ.pop("SOLET_NAME", None)
        else:
            os.environ["SOLET_NAME"] = saved


def test_port_manager_routes_through_canonical_accessor() -> bool:
    """port_manager.py callers hard-error when SOLET_NAME is unset."""
    from ananta.core.runtime import port_manager

    saved = os.environ.pop("SOLET_NAME", None)
    try:
        callsites = [
            ("get_runtime_dir", lambda: port_manager.get_runtime_dir()),
            ("write_port_file", lambda: port_manager.write_port_file(9999)),
            ("read_port_file", lambda: port_manager.read_port_file()),
            ("remove_port_file", lambda: port_manager.remove_port_file()),
            ("PortManager.__init__", lambda: port_manager.PortManager()),
        ]
        all_ok = True
        for label, call in callsites:
            try:
                call()
            except RuntimeError as exc:
                ok = "SOLET_NAME" in str(exc)
                _print(f"  {label} hard-errors with discoverable msg", ok)
                if not ok:
                    all_ok = False
            else:
                _print(f"  {label} expected RuntimeError, got none", False)
                all_ok = False
        return all_ok
    finally:
        if saved is not None:
            os.environ["SOLET_NAME"] = saved


def test_default_solet_name_constant_gone() -> bool:
    """DEFAULT_SOLET_NAME constant deleted from constants.py."""
    import ananta.constants as constants

    has_attr = hasattr(constants, "DEFAULT_SOLET_NAME")
    _print("DEFAULT_SOLET_NAME removed", not has_attr)
    return not has_attr


def main() -> int:
    repo_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo_root / "ananta" / "src"))

    print("solet_name_smoke")
    results = [
        ("hard_error_when_unset", test_hard_error_when_unset()),
        ("returns_env_var_when_set", test_returns_env_var_when_set()),
        ("port_manager_routes_through_canonical_accessor",
         test_port_manager_routes_through_canonical_accessor()),
        ("default_constant_gone", test_default_solet_name_constant_gone()),
    ]
    print()
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        _print(name, ok)
    print(f"\n{passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
