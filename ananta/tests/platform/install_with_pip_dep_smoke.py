#!/usr/bin/env python3
"""C4 new-pip-deps smoke (no pytest).

Closes the OPERATOR-CITED bug ("blue-green migrations always failing when
new Python packages were required by a plugin being installed") via the
complementary surface to F3's ``install_plugin_from_path_smoke.py``. F3's
fixture intentionally used ``dependencies = []`` to isolate the
entry-point-cache + allowlist-freeze + lifecycle-rewire root causes; C4's
fixture declares a real pip dependency (``tabulate``) so the smoke
positively asserts the install-with-new-pip-dep code path that F3's
PEP 660 ``site.addsitedir`` catch was designed to make work.

Assertions (per Coordinator-Day's C4 dispatch brief Step 2):
  N1 - entry_points sees the fixture post-install (Bug-B carry).
  N2 - the fixture's pip-dep is importable in-process post-install
       (F3's _invalidate_importlib_caches's site.addsitedir step
       refreshes sys.path so import tabulate finds the freshly-installed
       package).
  N3 - allowlist augmentation + library-level PluginManager rediscovery
       + verb dispatchable in-process; the verb body imports tabulate
       and renders a small table; non-empty rendered output proves
       the dep is importable AND callable AND returns expected payload
       (Bug-A + Codex Blocker 2 carries).
  N4 - apply_manifest -> restart_with_manifest -> green spawn -> verb
       call against green returns expected. THIS IS THE OPERATOR-CITED
       CRITERION. Structurally requires live multi-process MCP dispatch
       against the active solet (cutover machinery is the system under test);
       not coverable by this library-level smoke harness. Coordinator
       drives N4 manually via ``solet call <process_key>`` as part of the C4
       cycle; the result is reported alongside this smoke in the Step 5
       IMPORTANT-back. See dispatch brief + advisor consultation
       2026-06-16 PT for the rationale.

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import shutil
import site
import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
)

_FIXTURE_DIR = (
    REPO_ROOT
    / "ananta"
    / "tests"
    / "fixtures"
    / "fresh_install_with_pip_dep_smoke_plugin"
)
_FIXTURE_NAME = "fresh_install_with_pip_dep_smoke_plugin"
_FIXTURE_PIP_DEP = "tabulate"
_ENTRY_POINT_GROUP = "ananta.plugins"

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


def _pip_uninstall_silent(plugin_name: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "--yes", plugin_name],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode not in (0, 1):
        print(
            f"  WARN  pip uninstall {plugin_name} returned "
            f"{completed.returncode}: {completed.stderr.strip()}",
            file=sys.stderr,
        )


def _entry_point_names() -> set[str]:
    return {
        ep.name
        for ep in importlib.metadata.entry_points().select(group=_ENTRY_POINT_GROUP)
    }


def _site_packages_distinfo_present(plugin_name: str) -> bool:
    for path in site.getsitepackages():
        sitepkgs = Path(path)
        if not sitepkgs.is_dir():
            continue
        for entry in sitepkgs.iterdir():
            if (
                entry.is_dir()
                and entry.name.startswith(f"{plugin_name}-")
                and entry.name.endswith(".dist-info")
            ):
                return True
    return False


def _is_module_importable(module_name: str) -> bool:
    """Probe a module by spec lookup without raising on absence."""
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError):
        return False
    return spec is not None


@contextmanager
def _temp_fixture_copy(source: Path) -> Generator[Path]:
    """Copy fixture to a temp dir so pip's editable install doesn't
    scribble ``.egg-info/`` / ``__pycache__/`` into the committed
    fixture tree.
    """
    base = Path(tempfile.mkdtemp(prefix="c4_fixture_"))
    target = base / source.name
    shutil.copytree(source, target)
    try:
        yield target
    finally:
        shutil.rmtree(base, ignore_errors=True)


class _SuccessKnowledgeService:
    """Healthy knowledge_service so the strict install-path refresh succeeds.

    ``install_plugin_from_path``'s registry refresh now fails closed (Rev-B
    Blocker B) when knowledge_service is absent or reports errors; this
    success-path smoke supplies a real one so the pip-dependency assertions
    still exercise a COMPLETED install.
    """

    def refresh_plugin_processes(self, plugin_name: str) -> dict[str, object]:
        return {"status": "success", "plugin_name": plugin_name, "errors": []}


class _MinimalOrchestrator:
    """Minimal stand-in for the live orchestrator needed by
    LifecycleManagementService.install_plugin_from_path. Matches the
    F3 ``_MinimalOrchestrator`` pattern.
    """

    def __init__(self, plugin_manager: PluginManager) -> None:
        self.plugin_manager = plugin_manager
        self.config = None
        self._knowledge_service = _SuccessKnowledgeService()

    def get_service(self, name: str) -> object | None:
        if name == "knowledge_service":
            return self._knowledge_service
        return None


@contextmanager
def _fresh_state() -> Generator[tuple[LifecycleManagementService, PluginManager]]:
    """Try/finally guarded fresh state. Pre-state: uninstall fixture +
    invalidate caches (the fixture's pip-dep ``tabulate`` is NOT
    uninstalled in pre-state because the smoke's N2 assertion needs to
    PROVE tabulate became importable BECAUSE of the install, so it
    must be absent at pre-state). Post-state: uninstall fixture + pip
    uninstall tabulate + invalidate.
    """
    _pip_uninstall_silent(_FIXTURE_NAME)
    _pip_uninstall_silent(_FIXTURE_PIP_DEP)
    importlib.invalidate_caches()
    importlib.metadata.MetadataPathFinder.invalidate_caches()

    plugin_manager = PluginManager()
    plugin_manager.discover_plugins(allowed_plugins=set())
    orchestrator = _MinimalOrchestrator(plugin_manager)
    service = LifecycleManagementService(orchestrator_ref=orchestrator)

    try:
        yield service, plugin_manager
    finally:
        _pip_uninstall_silent(_FIXTURE_NAME)
        _pip_uninstall_silent(_FIXTURE_PIP_DEP)
        importlib.invalidate_caches()
        importlib.metadata.MetadataPathFinder.invalidate_caches()


def _case_n1_n2_n3() -> None:
    """N1 + N2 + N3 covered by one install + the verb call against the
    library-level orchestrator.
    """
    print(
        "\nCase N1+N2+N3: install fixture with pip-dep via canonical verb"
    )
    with _fresh_state() as (service, plugin_manager), _temp_fixture_copy(
        _FIXTURE_DIR,
    ) as fixture_src:
        _check(
            not _site_packages_distinfo_present(_FIXTURE_NAME),
            "  precondition: fixture plugin not installed before the verb runs",
        )
        _check(
            not _is_module_importable(_FIXTURE_PIP_DEP),
            f"  precondition: pip-dep {_FIXTURE_PIP_DEP!r} not importable pre-install",
        )

        result = service.install_plugin_from_path(str(fixture_src))
        _check(
            result.get("action_status") == "completed",
            f"  verb returns action_status=completed (got {result!r})",
        )
        data = result.get("data") or {}
        _check(
            data.get("installed") is True,
            f"  data.installed is True (got {data!r})",
        )

        # ---- N1: metadata freshness (Bug-B carry) ----
        eps_after = _entry_point_names()
        _check(
            _FIXTURE_NAME in eps_after,
            "  N1 metadata freshness: entry_points sees fixture without restart",
        )

        # ---- N2: pip-dep resolved + importable in-process (the C4 surface) ----
        _check(
            _site_packages_distinfo_present(_FIXTURE_PIP_DEP),
            f"  N2 pip-dep resolution: {_FIXTURE_PIP_DEP!r} .dist-info present in site-packages",
        )
        _check(
            _is_module_importable(_FIXTURE_PIP_DEP),
            f"  N2 in-process importability: import {_FIXTURE_PIP_DEP} succeeds post-install",
        )

        # ---- N3: allowlist augmentation + rewire + verb dispatch returns rendered table ----
        allowed = plugin_manager._allowed_plugins or set()  # noqa: SLF001
        _check(
            _FIXTURE_NAME in allowed,
            "  N3 allowlist augmentation: _allowed_plugins contains fixture",
        )
        _check(
            _FIXTURE_NAME in plugin_manager.plugins,
            "  N3 roster: rediscover produced a live instance in plugin_manager.plugins",
        )
        plugin = plugin_manager.plugins[_FIXTURE_NAME]
        verb_result = plugin.tabulate_proof({}, {})
        _check(
            verb_result.get("action_status") == "completed",
            "  N3 reachability: tabulate_proof returns completed envelope",
        )
        verb_data = verb_result.get("data") or {}
        _check(
            verb_data.get("alive") is True,
            "  N3 reachability: verb data.alive is True (rewire fix carries)",
        )
        rendered = verb_data.get("tabulate_rendered") or ""
        _check(
            isinstance(rendered, str) and len(rendered) > 0,
            "  N3 pip-dep callable: tabulate_rendered is a non-empty string",
        )
        _check(
            "alive" in rendered and "True" in rendered,
            "  N3 pip-dep correctness: rendered output contains the expected table cells",
        )
        _check(
            plugin.is_ready(),
            "  N3 lifecycle: plugin reports is_ready() True after rewire",
        )


def _summary_and_exit() -> None:
    total = _passed + len(_failed)
    print(f"\n--- Summary: {_passed}/{total} passed ---")
    if _failed:
        print("Failures:")
        for label in _failed:
            print(f"  - {label}")
        sys.exit(1)
    sys.exit(0)


def main() -> None:
    print(
        f"Running C4 new-pip-deps smoke against {REPO_ROOT} "
        f"(pip-dep candidate: {_FIXTURE_PIP_DEP!r})"
    )
    _case_n1_n2_n3()
    _summary_and_exit()


if __name__ == "__main__":
    main()
