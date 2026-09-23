#!/usr/bin/env python3
"""§5 CWD-hygiene acceptance smoke (no pytest).

Design ``2026-06-27_true_local_blue_green_materialized_artifacts_design.md``
§5 invariant: **no managed process may have its CWD set to a code tree.**
A stray relative-path write under a code tree pollutes the git working
tree today (``.gitignore:49 /error.log`` proves it happened once) and,
once releases are materialized, would mutate a rollback target.

This smoke proves three of the four §5 sites resolve their managed
process CWD to the out-of-tree runtime dir (site 1, the autostart plist
``WorkingDirectory``, is covered by ``autostart_plist_render_smoke.py``):

* Site 2 — ``default_spawn`` green-child ``cwd`` (monkeypatched Popen).
* Site 3 — router launchd plist ``WorkingDirectory`` (template render).
* Site 4 — router systemd unit ``WorkingDirectory`` (template render).

These render/spawn-arg level checks intentionally do NOT depend on the
full ``swap_round_trip`` end-to-end (whose fake spawn never exercises
the real ``cwd``).

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/cwd_out_of_tree_smoke.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.runtime import get_runtime_dir  # noqa: E402
from macos_self_deployment_plugin import swap_orchestrator  # noqa: E402
from macos_self_deployment_plugin.blue_green_router import (  # noqa: E402
    install_router,
    service_install,
)
from macos_self_deployment_plugin.release_manager import CandidatePaths  # noqa: E402

_SOLET = "example"
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


class _FakePopen:
    """Captures the spawn ``cmd`` + ``cwd`` without launching a real process."""

    last_cwd: str | None = None
    last_cmd: list[str] | None = None

    def __init__(self, cmd: list[str], **kwargs: Any) -> None:
        _FakePopen.last_cmd = cmd
        _FakePopen.last_cwd = kwargs.get("cwd")
        self.pid = 4242


def test_default_spawn_cwd_is_runtime_dir() -> None:
    """Site 2: the green child's CWD is the out-of-tree runtime dir."""
    # ~/.ananta exists (the solet uses it); scratch app_home lives under it, NOT /tmp.
    ananta_root = Path.home() / ".ananta"
    ananta_root.mkdir(exist_ok=True)
    original_popen = subprocess.Popen
    subprocess.Popen = _FakePopen  # type: ignore[assignment,misc]
    try:
        with tempfile.TemporaryDirectory(
            prefix="cwd-smoke-", dir=str(ananta_root)
        ) as scratch:
            app_home = Path(scratch) / "profile"
            app_home.mkdir(parents=True, exist_ok=True)
            # §4.5: default_spawn now takes the candidate release it spawns
            # from. The CWD assertion below is unchanged — cwd resolves to the
            # runtime dir regardless of which interpreter is launched.
            candidate = CandidatePaths(
                release_id="rel-cwd-smoke",
                release_dir=Path(scratch) / "rel-cwd-smoke",
                code_root=Path(scratch) / "rel-cwd-smoke" / "code",
                venv_python=Path(scratch) / "rel-cwd-smoke" / "venv" / "bin" / "python3",
                version_file=Path(scratch) / "rel-cwd-smoke" / "VERSION",
                missing_pth_targets=(),
                schema_snapshot=None,
            )
            pid = swap_orchestrator.default_spawn(
                app_home, "green", "example-green-test", _SOLET, candidate,
            )
    finally:
        subprocess.Popen = original_popen  # type: ignore[misc]

    expected = str(get_runtime_dir(_SOLET))
    _check(pid == 4242, "default_spawn returned the (faked) child pid")
    _check(
        _FakePopen.last_cwd == expected,
        f"default_spawn cwd == get_runtime_dir('{_SOLET}') "
        f"(expected {expected!r}, got {_FakePopen.last_cwd!r})",
    )
    _check(
        isinstance(_FakePopen.last_cwd, str)
        and str(REPO_ROOT) not in _FakePopen.last_cwd,
        f"default_spawn cwd is outside the repo tree (got {_FakePopen.last_cwd!r})",
    )


def _router_context() -> dict[str, str]:
    runtime_dir = service_install.RUNTIME_DIR
    return {
        "LAUNCHD_LABEL": service_install.launchd_label(_SOLET),
        "PYTHON_BIN": "/usr/bin/python3",
        "WORKING_DIR": str(runtime_dir),
        "RUNTIME_DIR": str(runtime_dir),
        "SOLET_NAME": _SOLET,
        "PUBLIC_PORT": "8800",
        "SOCKET_PATH": str(service_install.default_socket_path(_SOLET)),
        "LOG_DIR": str(service_install.LOG_DIR),
    }


def test_router_launchd_working_directory_out_of_tree() -> None:
    """Site 3: the router launchd plist WorkingDirectory is out-of-tree."""
    rendered = service_install.render_template(
        service_install.LAUNCHD_TEMPLATE_NAME, _router_context()
    )
    expected = str(service_install.RUNTIME_DIR)
    needle = f"<key>WorkingDirectory</key>\n    <string>{expected}</string>"
    _check(
        needle in rendered,
        f"launchd WorkingDirectory == RUNTIME_DIR ({expected!r})",
    )
    _check(
        str(REPO_ROOT) not in rendered,
        "rendered launchd plist contains no repo-root path",
    )


def test_router_systemd_working_directory_out_of_tree() -> None:
    """Site 4: the router systemd unit WorkingDirectory is out-of-tree."""
    rendered = service_install.render_template(
        service_install.SYSTEMD_TEMPLATE_NAME, _router_context()
    )
    expected = str(service_install.RUNTIME_DIR)
    _check(
        f"WorkingDirectory={expected}" in rendered,
        f"systemd WorkingDirectory == RUNTIME_DIR ({expected!r})",
    )
    _check(
        str(REPO_ROOT) not in rendered,
        "rendered systemd unit contains no repo-root path",
    )


def test_install_router_build_context_wires_out_of_tree() -> None:
    """Sites 3/4 wiring: install_router supplies the out-of-tree WORKING_DIR.

    The template-render tests above self-supply WORKING_DIR; this regression-
    locks the actual ``_build_context`` so a future edit that reintroduces a
    repo-root WorkingDirectory (the old ``REPO_ROOT`` key) is caught.
    """
    args = argparse.Namespace(
        solet_name=_SOLET,
        public_port=8800,
        socket_path=None,
        log_dir=None,
        runtime_dir=None,
    )
    context = install_router._build_context(args)  # noqa: SLF001
    _check(
        context.get("WORKING_DIR") == str(service_install.RUNTIME_DIR),
        f"_build_context WORKING_DIR == RUNTIME_DIR "
        f"(got {context.get('WORKING_DIR')!r})",
    )
    _check(
        "REPO_ROOT" not in context,
        "_build_context no longer emits a REPO_ROOT key",
    )


def main() -> int:
    print("=== cwd_out_of_tree_smoke (§5: managed-process CWD out-of-tree) ===")
    test_default_spawn_cwd_is_runtime_dir()
    test_router_launchd_working_directory_out_of_tree()
    test_router_systemd_working_directory_out_of_tree()
    test_install_router_build_context_wires_out_of_tree()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
