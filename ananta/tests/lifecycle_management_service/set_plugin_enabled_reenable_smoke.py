#!/usr/bin/env python3
"""LIF-01 ``set_plugin_enabled`` disable->enable smoke (C1b, no pytest).

Design: ``workbench/2026-07-31_architect_four_item_dispatch_designs.md`` S1
("LIF-01 -- set_plugin_enabled re-enable path").

Pre-fix on master, ``_apply_plugin_enable``'s absent-from-roster branch
routed through ``_rediscover_plugins`` -> ``PluginManager.discover_plugins``,
whose first statement is ``self.plugins.clear()``: re-enabling ONE plugin
wiped the ENTIRE live roster and replaced every loaded plugin with a fresh,
un-injected, never-prepared instance. The re-enabled plugin then failed
with "orchestrator_ref not injected" (caught and reported as a one-plugin
error), while every OTHER loaded plugin was silently replaced underneath
callers holding the old instances.

The fix (C1b) routes the absent branch through
``plugin_manager.installer.install(plugin_name, wire=_wire_plugin_instance)``
-- the same atomic stage/wire/commit primitive ``install_plugin_from_path``
already uses. A failure leaves the roster, allowlist, and every
pre-existing instance byte-identical; only a genuinely-uninstalled
entry-point still reports ``restart_required=True`` (contract unchanged).

Assertions (per the design's "Proof" section):
  Case 1 - re-enable success: the atomic install path succeeds where the
           legacy rediscovery path raised "orchestrator_ref not injected",
           the re-enabled plugin is ready, AND its process key still
           dispatches (the Proof section's third leg: process keys
           re-registered, not just "no exception").
  Case 2 - bystander identity preserved: TWO unrelated, already-loaded
           plugins' ``id()`` are UNCHANGED across the victim's
           disable -> enable cycle. This is the assertion that catches the
           roster-wide clear; no per-plugin assertion can see it.
  Case 3 - surviving contract: enabling a genuinely-uninstalled entry-point
           name still returns ``restart_required=True`` (mutated input: a
           name absent from ``ananta.plugins``), unchanged card semantics.
  Case 4 - manifest-exclusion guard: ``PluginInstaller.install`` scopes
           discovery to ``{plugin_name}`` alone and has no notion of the
           solet's profile-manifest allowlist, so without an explicit
           pre-check a manifest-excluded-but-pip-installed plugin would
           load anyway on re-enable and get silently unioned into the live
           allowlist. A plugin removed from ``_allowed_plugins`` before
           re-enable must still report ``restart_required=True`` and stay
           excluded (mutated input: the manifest's own allowlist).
  Case 5 - wiring-phase failure isolation: a plugin whose ``wire`` callback
           fails specifically on the RE-enable (not the initial install)
           returns an error envelope with the roster, bystanders, and
           allowlist untouched -- the ``PluginInstallError(phase="wiring")``
           branch documented in the KB card, exercised for real.

!!! NON-GATE / BUILD-VERIFY ONLY -- do NOT rot, do NOT delete !!!
This smoke shells out to REAL `pip install -e` / `pip uninstall` three
times per run (one per fixture plugin), matching the pre-existing
``install_plugin_from_path_smoke.py`` precedent (Q3 ruling: real-pip
smokes stay out of `quality_gates/gate_smokes.txt`; GTE-09 "no dead
live-DB smokes in the gate"). Run by hand in build-verify before any land
that touches ``_apply_plugin_enable`` / the ``PluginInstaller`` primitive:
    SOLET_NAME=<name> .venv/bin/python3 \\
        ananta/tests/lifecycle_management_service/set_plugin_enabled_reenable_smoke.py

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.config.config_manager import ConfigManager  # noqa: E402
from ananta.core.plugins.plugin_base import (  # noqa: E402
    EventBusProtocol,
    OrchestratorProtocol,
    PluginBase,
)
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
)

_BYSTANDER1_DIR = REPO_ROOT / "ananta" / "tests" / "fixtures" / "fresh_install_smoke_plugin"
_BYSTANDER1_NAME = "fresh_install_smoke_plugin"
_BYSTANDER2_NAME = "lif01_bystander2_smoke_plugin"
_VICTIM_DIR = REPO_ROOT / "ananta" / "tests" / "fixtures" / "lif01_reenable_smoke_plugin"
_VICTIM_NAME = "lif01_reenable_smoke_plugin"
_UNINSTALLED_NAME = "lif01_definitely_not_installed_plugin_xyz"
# Must stay in sync with lif01_reenable_smoke_plugin.plugin.WIRING_FAILURE_ENV --
# duplicated rather than imported since the fixture isn't on sys.path until
# after its own pip install runs.
_WIRING_FAILURE_ENV = "LIF01_REENABLE_SMOKE_PLANT_WIRING_FAILURE"

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
    """Remove a plugin via pip without raising on non-zero exit."""
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


@contextmanager
def _temp_fixture_copy(source: Path) -> Generator[Path]:
    """Copy a fixture source tree to a temp dir so pip's editable install
    scribbles ``.egg-info/`` / ``__pycache__/`` into a disposable location.
    """
    base = Path(tempfile.mkdtemp(prefix="lif01_fixture_"))
    target = base / source.name
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    try:
        yield target
    finally:
        shutil.rmtree(base, ignore_errors=True)


@contextmanager
def _materialize_bystander2() -> Generator[Path]:
    """Clone fixture-A (bystander 1) under a second name for bystander 2.

    A second, independently-named already-loaded plugin so Case 2 has TWO
    bystanders to check, per the design's proof section.
    """
    base = Path(tempfile.mkdtemp(prefix="lif01_bystander2_"))
    target = base / _BYSTANDER2_NAME
    shutil.copytree(
        _BYSTANDER1_DIR, target, ignore=shutil.ignore_patterns("__pycache__"),
    )
    try:
        (target / "src" / _BYSTANDER1_NAME).rename(target / "src" / _BYSTANDER2_NAME)
        for rel in (
            "pyproject.toml", "plugin.yaml", f"src/{_BYSTANDER2_NAME}/plugin.py",
        ):
            path = target / rel
            text = path.read_text(encoding="utf-8").replace(
                _BYSTANDER1_NAME, _BYSTANDER2_NAME,
            )
            path.write_text(text, encoding="utf-8")
        yield target
    finally:
        shutil.rmtree(base, ignore_errors=True)


class _FakeEventBus:
    """Minimal EventBusProtocol stand-in so the smoke can wire the manager."""

    def publish(self, event: object) -> bool:
        del event
        return True


class _SuccessKnowledgeService:
    """knowledge_service stand-in whose refresh always reports success."""

    def refresh_plugin_processes(self, plugin_name: str) -> dict[str, object]:
        return {
            "status": "success",
            "plugin_name": plugin_name,
            "updated_count": 0,
            "process_keys": [],
            "errors": [],
        }


class _SmokeOrchestrator:
    """Fixture orchestrator carrying a REAL ``ConfigManager``.

    ``set_plugin_enabled`` requires ``self._orchestrator.config`` to be an
    actual ``ConfigManager`` instance (``_require_config_manager`` does an
    ``isinstance`` check) and persists the ``enabled`` flag through it
    before applying the change live -- a duck-typed stand-in cannot satisfy
    this, unlike the plugin-manager-only fixtures used by the
    ``install_plugin_from_path`` smoke.
    """

    def __init__(self, plugin_manager: PluginManager, config: ConfigManager) -> None:
        self.plugin_manager = plugin_manager
        self.config = config
        self._knowledge_service = _SuccessKnowledgeService()

    def get_service(self, name: str) -> object | None:
        if name == "knowledge_service":
            return self._knowledge_service
        return None


@contextmanager
def _fresh_state() -> Generator[tuple[LifecycleManagementService, PluginManager]]:
    """Build a fresh service + plugin manager wired for ``set_plugin_enabled``."""
    for name in (_BYSTANDER1_NAME, _BYSTANDER2_NAME, _VICTIM_NAME):
        _pip_uninstall_silent(name)

    config_dir = Path(tempfile.mkdtemp(prefix="lif01_config_"))
    try:
        config_manager = ConfigManager(APP_HOME=str(config_dir))

        plugin_manager = PluginManager()
        plugin_manager.discover_plugins(config_manager, allowed_plugins=set())
        orchestrator = _SmokeOrchestrator(plugin_manager, config_manager)
        plugin_manager.set_orchestrator_ref(cast(OrchestratorProtocol, orchestrator))
        plugin_manager.set_event_bus_ref(cast(EventBusProtocol, _FakeEventBus()))
        service = LifecycleManagementService(orchestrator_ref=orchestrator)

        try:
            yield service, plugin_manager
        finally:
            for name in (_BYSTANDER1_NAME, _BYSTANDER2_NAME, _VICTIM_NAME):
                _pip_uninstall_silent(name)
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


def _install_roster(
    service: LifecycleManagementService, plugin_manager: PluginManager,
) -> tuple[PluginBase, PluginBase, PluginBase]:
    """Install victim + two bystanders; return their live-roster instances."""
    with _temp_fixture_copy(_BYSTANDER1_DIR) as bystander1_src:
        setup1 = service.install_plugin_from_path(str(bystander1_src))
    _check(
        setup1.get("action_status") == "completed",
        f"  setup: bystander 1 installs (got {setup1.get('action_status')!r}: "
        f"{setup1.get('error')})",
    )

    with _materialize_bystander2() as bystander2_src:
        setup2 = service.install_plugin_from_path(str(bystander2_src))
    _check(
        setup2.get("action_status") == "completed",
        f"  setup: bystander 2 installs (got {setup2.get('action_status')!r}: "
        f"{setup2.get('error')})",
    )

    with _temp_fixture_copy(_VICTIM_DIR) as victim_src:
        setup_victim = service.install_plugin_from_path(str(victim_src))
    _check(
        setup_victim.get("action_status") == "completed",
        f"  setup: victim installs (got {setup_victim.get('action_status')!r}: "
        f"{setup_victim.get('error')})",
    )
    victim = plugin_manager.plugins.get(_VICTIM_NAME)
    _check(
        victim is not None and victim.is_ready(),
        "  setup: victim wired + ready at install time (orchestrator_ref injected)",
    )

    bystander1 = plugin_manager.plugins.get(_BYSTANDER1_NAME)
    bystander2 = plugin_manager.plugins.get(_BYSTANDER2_NAME)
    _check(
        bystander1 is not None and bystander2 is not None,
        "  setup: both bystanders present in the roster before the disable/enable cycle",
    )
    assert victim is not None
    assert bystander1 is not None
    assert bystander2 is not None
    return victim, bystander1, bystander2


def _case1_2_reenable_and_bystanders(
    service: LifecycleManagementService,
    plugin_manager: PluginManager,
    bystander1: PluginBase,
    bystander2: PluginBase,
) -> None:
    """Cases 1-2: disable -> enable succeeds; bystander identity preserved."""
    disable_result = service.set_plugin_enabled(_VICTIM_NAME, False)
    _check(
        disable_result.get("data", {}).get("applied") is True,
        f"  disable applies cleanly (got {disable_result!r})",
    )
    _check(
        _VICTIM_NAME not in plugin_manager.plugins,
        "  victim removed from the live roster after disable",
    )
    _check(
        plugin_manager.plugins.get(_BYSTANDER1_NAME) is bystander1
        and plugin_manager.plugins.get(_BYSTANDER2_NAME) is bystander2,
        "  bystanders untouched by disable (not the defect's phase)",
    )

    enable_result = service.set_plugin_enabled(_VICTIM_NAME, True)

    # ---- Case 1: re-enable succeeds (RED on master: "orchestrator_ref
    # not injected" from a freshly-rediscovered, unwired instance) ----
    _check(
        enable_result.get("action_status") == "completed",
        f"  Case 1: re-enable returns action_status=completed "
        f"(got {enable_result.get('action_status')!r}: {enable_result.get('error')!r}) "
        f"[pre-fix: error 'orchestrator_ref not injected']",
    )
    enable_data = enable_result.get("data") or {}
    _check(
        enable_data.get("applied") is True and enable_data.get("restart_required") is False,
        f"  Case 1: re-enable envelope reports applied=True, restart_required=False "
        f"(got {enable_data!r})",
    )
    reenabled_victim = plugin_manager.plugins.get(_VICTIM_NAME)
    _check(
        reenabled_victim is not None and reenabled_victim.is_ready(),
        "  Case 1: re-enabled victim is wired + ready",
    )
    verb_result = (
        cast(Any, reenabled_victim).test_verb({}, {}) if reenabled_victim is not None else {}
    )
    _check(
        verb_result.get("data", {}).get("alive") is True,
        f"  Case 1: re-enabled victim's process key still dispatches "
        f"(Proof section's third leg: process keys re-registered) (got {verb_result!r})",
    )

    # ---- Case 2: bystander identity preserved (RED on master: the
    # roster-wide discover_plugins().clear() replaces every instance) ----
    _check(
        plugin_manager.plugins.get(_BYSTANDER1_NAME) is bystander1,
        "  Case 2: bystander 1 id() unchanged across victim's disable -> enable "
        "[pre-fix: roster-wide clear replaces it]",
    )
    _check(
        plugin_manager.plugins.get(_BYSTANDER2_NAME) is bystander2,
        "  Case 2: bystander 2 id() unchanged across victim's disable -> enable "
        "[pre-fix: roster-wide clear replaces it]",
    )


def _case3_missing_plugin_guard(
    service: LifecycleManagementService,
    plugin_manager: PluginManager,
    bystander1: PluginBase,
    bystander2: PluginBase,
) -> None:
    """Case 3: surviving contract -- a genuinely-uninstalled name still restart_required=True."""
    missing_result = service.set_plugin_enabled(_UNINSTALLED_NAME, True)
    missing_data = missing_result.get("data") or {}
    _check(
        missing_result.get("action_status") == "completed"
        and missing_data.get("applied") is False
        and missing_data.get("restart_required") is True,
        f"  Case 3: enabling a genuinely-uninstalled name still returns "
        f"restart_required=True (got {missing_result!r})",
    )
    _check(
        plugin_manager.plugins.get(_BYSTANDER1_NAME) is bystander1
        and plugin_manager.plugins.get(_BYSTANDER2_NAME) is bystander2,
        "  Case 3: bystanders untouched by the missing-plugin guard call",
    )


def _case4_manifest_exclusion_guard(
    service: LifecycleManagementService,
    plugin_manager: PluginManager,
    bystander1: PluginBase,
    bystander2: PluginBase,
) -> None:
    """Case 4: a plugin excluded from the profile manifest stays excluded.

    ``PluginInstaller.install`` scopes discovery to ``{plugin_name}`` alone
    -- it has no notion of the real ``_allowed_plugins`` manifest -- and
    unions the name into the live allowlist on commit. Without an explicit
    pre-check in ``_install_plugin_for_enable``, a plugin the operator's
    profile manifest deliberately excludes would load anyway on re-enable
    and get silently added to the allowlist.
    """
    disable_result = service.set_plugin_enabled(_VICTIM_NAME, False)
    _check(
        disable_result.get("data", {}).get("applied") is True,
        f"  Case 4 setup: victim disables cleanly (got {disable_result!r})",
    )

    allowed = plugin_manager._allowed_plugins  # noqa: SLF001
    assert allowed is not None
    allowed.discard(_VICTIM_NAME)

    enable_result = service.set_plugin_enabled(_VICTIM_NAME, True)
    enable_data = enable_result.get("data") or {}
    _check(
        enable_data.get("applied") is False and enable_data.get("restart_required") is True,
        f"  Case 4: manifest-excluded plugin still returns restart_required=True "
        f"(got {enable_result!r}) [without the guard: loads anyway despite exclusion]",
    )
    _check(
        _VICTIM_NAME not in plugin_manager.plugins,
        "  Case 4: manifest-excluded plugin stays OUT of the live roster",
    )
    _check(
        _VICTIM_NAME not in (plugin_manager._allowed_plugins or set()),  # noqa: SLF001
        "  Case 4: manifest-excluded plugin's name is NOT silently unioned into the allowlist",
    )
    _check(
        plugin_manager.plugins.get(_BYSTANDER1_NAME) is bystander1
        and plugin_manager.plugins.get(_BYSTANDER2_NAME) is bystander2,
        "  Case 4: bystanders untouched by the manifest-exclusion guard call",
    )

    # Restore manifest membership so Case 5 can re-enable the victim normally.
    allowed.add(_VICTIM_NAME)


def _case5_wiring_failure_isolated(
    service: LifecycleManagementService,
    plugin_manager: PluginManager,
    bystander1: PluginBase,
    bystander2: PluginBase,
) -> None:
    """Case 5: a wiring-phase failure specifically on RE-enable is isolated.

    The victim installed cleanly once (Case 1's setup); this plants a
    failure that only fires on the second ``prepare_for_readiness`` call
    (the re-enable's wire callback), driving
    ``PluginInstallError(phase="wiring")`` through ``_install_plugin_for_enable``
    for real, rather than only documenting the branch in the KB card.

    This disable call is a no-op by the time it runs: Case 4 already left
    the victim out of the roster (re-enabling it there would have collided
    with the manifest-exclusion guard), so the envelope here reads
    "plugin was not in the live roster", not a live stop-and-remove. Kept
    for readability/symmetry with the other cases, not for its own effect.
    """
    disable_result = service.set_plugin_enabled(_VICTIM_NAME, False)
    _check(
        disable_result.get("data", {}).get("applied") is True,
        f"  Case 5 setup: victim disable call is a no-op, already out of the roster "
        f"since Case 4 (got {disable_result!r})",
    )

    os.environ[_WIRING_FAILURE_ENV] = "1"
    try:
        enable_result = service.set_plugin_enabled(_VICTIM_NAME, True)
    finally:
        os.environ.pop(_WIRING_FAILURE_ENV, None)

    _check(
        enable_result.get("action_status") == "error",
        f"  Case 5: a planted wiring-phase failure returns an error envelope "
        f"(got {enable_result!r})",
    )
    error_message = str(enable_result.get("error") or "")
    _check(
        "at phase wiring" in error_message,
        f"  Case 5: error names the wiring phase (got {error_message!r})",
    )
    _check(
        _VICTIM_NAME not in plugin_manager.plugins,
        "  Case 5: victim NOT left in the roster after a failed wiring-phase enable",
    )
    _check(
        plugin_manager.plugins.get(_BYSTANDER1_NAME) is bystander1
        and plugin_manager.plugins.get(_BYSTANDER2_NAME) is bystander2,
        "  Case 5: bystanders untouched by the failed wiring-phase enable",
    )


def _case1_2_3_4_5_reenable_and_bystanders() -> None:
    """Cases 1-5: re-enable, bystander identity, and every guard the design's Proof names."""
    print(
        "\nCase 1-5: LIF-01 disable -> enable, bystander identity, "
        "surviving contract, manifest exclusion, wiring-phase isolation"
    )
    with _fresh_state() as (service, plugin_manager):
        _, bystander1, bystander2 = _install_roster(service, plugin_manager)
        _case1_2_reenable_and_bystanders(service, plugin_manager, bystander1, bystander2)
        _case3_missing_plugin_guard(service, plugin_manager, bystander1, bystander2)
        _case4_manifest_exclusion_guard(service, plugin_manager, bystander1, bystander2)
        _case5_wiring_failure_isolated(service, plugin_manager, bystander1, bystander2)


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
    print(f"Running set_plugin_enabled re-enable smoke (LIF-01 / C1b) against {REPO_ROOT}")
    _case1_2_3_4_5_reenable_and_bystanders()
    _summary_and_exit()


if __name__ == "__main__":
    main()
