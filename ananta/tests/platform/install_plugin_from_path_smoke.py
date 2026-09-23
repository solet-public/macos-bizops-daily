#!/usr/bin/env python3
"""F3 ER-12 ``install_plugin_from_path`` smoke (no pytest).

Targets the two Bug-A / Bug-B root causes + the Codex Blocker 2 lifecycle
rewire identified in ``workbench/2026-06-16_phase_a_planning.md`` §5.
Each assertion is a POSITIVE outcome check that targets exactly one
empirical failure mode the design memo §5 (iv) enumerates.

Assertions (per design memo §5 (iv)):
  A1 - metadata freshness (Bug-B fix verification)
  A2 - allowlist augmentation (Bug-A fix verification)
  A3 - in-session reachability (Codex Blocker 2 lifecycle rewire fix)
  A4 - blue-green persistence: STRUCTURALLY DEFERRED. The
       ``apply_manifest`` -> ``restart_with_manifest`` -> green-spawn
       cycle is multi-process integration territory, matching the
       precedent at ``root_manifest_smoke.py:13`` ("§7.3 sub-asserts
       (b/c/d) ... are integration-test territory beyond the ~250 LOC
       scope and deferred to a follow-on smoke against a live solet"). The
       in-session A1/A2/A3 cover the bug fixes; A4 covers the platform
       contract that the in-memory state is also persisted via
       operator-driven apply_manifest. The live-solet follow-on owns it.

ER-6 intersection (per design memo §5 (v)):
  C1 - install of a contract-invalid plugin returns an error envelope
       AND the pip artifact is rolled back so the next launch path does
       not pick up a broken plugin.

Operator-cited repro:
  Conceptually covered by A1+A2+A3 against the synthetic fixture (which
  intentionally mirrors a ``dependencies = []`` plugin shape). The two operator-cited plugins are
  currently installed in the running solet roster (merged in commit
  ``8bb685e82`` 2026-06-15); re-running install_plugin_from_path
  against them in-process would not exercise the fresh-install code
  path. The synthetic fixture exists precisely so the smoke can
  exercise that path repeatably without depending on the operator-cited
  plugins' state.

C1a extension (2026-07-10, install_plugin_from_path failure-atomicity):
  Case 3 - install-atomicity red matrix (R1-R7): a failing install (a probe
           whose prepare_for_readiness raises = the g_suite WIRING-phase
           incident) leaves the previously-installed victim byte-identical.
  Case 6 - remove-atomicity: a post-commit registry-refresh failure unwinds
           the just-installed plugin atomically (stop -> de-register -> pop ->
           allowlist-remove -> pip rollback), victim untouched.
  Case 7 - single-writer invariant: an AST guard that the install path
           (install_plugin_from_path + its helpers) plus set_plugin_enabled's
           enable-if-absent branch (LIF-01/C1b, 2026-08-02) mutate the roster
           ONLY via plugin_manager.installer.{install,remove}, never a
           clear-and-rebuild. Still method-scoped, not whole-file: the
           disable path's `.pop()` (plugin_installer.py:196-199's
           discovery-vector caveat on `remove`) is its own tracked slice.

!!! NON-GATE / BUILD-VERIFY ONLY — do NOT rot, do NOT delete !!!
This smoke shells out to REAL `pip install -e` / `pip uninstall` several
times per run, so it is deliberately NOT registered in
`quality_gates/gate_smokes.txt` (Q3 ruling: matches the pre-existing
install-smoke precedent + GTE-09 "no dead live-DB smokes in the gate").
It MUST be run by hand in build-verify before any land that touches
install_plugin_from_path / the PluginInstaller primitive:
    SOLET_NAME=<name> .venv/bin/python3 \\
        ananta/tests/platform/install_plugin_from_path_smoke.py

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import ast
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
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.plugins.plugin_base import (  # noqa: E402
    EventBusProtocol,
    OrchestratorProtocol,
)
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
)

_FIXTURE_DIR = REPO_ROOT / "ananta" / "tests" / "fixtures" / "fresh_install_smoke_plugin"
_FIXTURE_NAME = "fresh_install_smoke_plugin"
# C1a red-first fixtures, materialised in tempdirs from the fixture-A tree.
# _PROBE_NAME: prepare_for_readiness() RAISES — the exact g_suite incident phase
#   (pip OK, discovery OK, contract OK, WIRING raises). _CLONE_NAME: a clean
#   fixture-A clone whose prepare succeeds (a second live plugin + a
#   registry-refresh-fail unwind target).
_PROBE_NAME = "install_atomicity_probe_plugin"
_CLONE_NAME = "install_atomicity_clone_plugin"
_ENTRY_POINT_GROUP = "ananta.plugins"
# Source file the single-writer AST guard (Case 7) scans.
_LIFECYCLE_SERVICE_SRC = (
    REPO_ROOT / "ananta" / "src" / "ananta" / "services"
    / "lifecycle_management_service" / "service.py"
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


def _pip_uninstall_silent(plugin_name: str) -> None:
    """Remove a plugin via pip without raising on non-zero exit.

    Used in setup + teardown to guarantee a clean pre/post-state. Any
    failure here is logged to stderr but does not abort the smoke; an
    over-zealous cleanup that aborts on a missing plugin would leave
    later cases unrunnable.
    """
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
    """Return the set of currently-visible ananta.plugins entry-point names."""
    return {
        ep.name
        for ep in importlib.metadata.entry_points().select(group=_ENTRY_POINT_GROUP)
    }


def _site_packages_distinfo_present(plugin_name: str) -> bool:
    """Check whether site-packages still carries a .dist-info for plugin_name.

    This is the rollback contract surface (what ``pip uninstall`` controls)
    rather than ``entry_points()``, which can legitimately reflect a
    source-tree ``.egg-info`` that survives an editable uninstall.
    """
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


@contextmanager
def _temp_fixture_copy(source: Path) -> Generator[Path]:
    """Copy a fixture source tree to a temp dir so pip's editable install
    can scribble ``.egg-info/`` and ``__pycache__/`` into a disposable
    location instead of polluting the committed fixture tree.
    """
    base = Path(tempfile.mkdtemp(prefix="er12_fixture_"))
    target = base / source.name
    shutil.copytree(source, target)
    try:
        yield target
    finally:
        shutil.rmtree(base, ignore_errors=True)


class _FakeEventBus:
    """Minimal EventBusProtocol stand-in so the smoke can wire the MANAGER.

    R-B: the removal/atomicity red-first needs the plugin manager itself
    wired (set_orchestrator_ref + set_event_bus_ref), not just the service,
    so a victim plugin's orchestrator_ref is a real master-vs-postfix
    discriminator. The bus is never published to here; it only has to
    satisfy set_event_bus_ref → plugin.set_event_bus.
    """

    def publish(self, event: object) -> bool:
        del event
        return True


class _FakeProcessRegistryManager:
    """Dict-backed stand-in for ProcessRegistryManager (de-register target).

    Mirrors ``unregister_dynamic_processes`` (process_registry_manager.py:174):
    deletes the given keys from the live process map. Records every
    de-register call so Case 6 can assert the removal primitive de-registered
    the departing plugin's process keys — in the stop→de-register→pop order —
    before dropping it from the roster.
    """

    def __init__(self) -> None:
        self.processes: dict[str, object] = {}
        self.unregistered: list[str] = []

    def unregister_dynamic_processes(self, process_keys: list[str]) -> None:
        self.unregistered.extend(process_keys)
        for key in process_keys:
            self.processes.pop(key, None)


class _RaisingKnowledgeService:
    """knowledge_service stand-in whose refresh raises for a targeted plugin.

    Case 6 uses it to force the post-commit registry-refresh failure that
    drives ``install_plugin_from_path``'s unwind path, while letting the
    victim plugin's own install refresh succeed.
    """

    def __init__(self, fail_for: str) -> None:
        self._fail_for = fail_for

    def refresh_plugin_processes(self, plugin_name: str) -> dict[str, object]:
        if plugin_name == self._fail_for:
            raise RuntimeError(
                "install-atomicity smoke: forced registry refresh failure "
                f"for {plugin_name!r}"
            )
        return {"plugin_name": plugin_name, "refreshed": True}


class _PartialRefreshKnowledgeService:
    """knowledge_service whose refresh RETURNS errors WITHOUT raising.

    Reproduces the real ``refresh_plugin_processes`` behavior for a NEW plugin
    (Codex B2): the merge cannot register keys not already in the registry, so
    it returns ``{"status": "partial", "errors": [...]}``. The pre-fix verb
    ignored that return and reported install success; the fix fails closed.
    """

    def refresh_plugin_processes(self, plugin_name: str) -> dict[str, object]:
        return {
            "status": "partial",
            "plugin_name": plugin_name,
            "updated_count": 0,
            "process_keys": [],
            "errors": [f"Process key not found in registry: plugin::{plugin_name}::test_verb"],
        }


class _SuccessKnowledgeService:
    """knowledge_service whose refresh reports success (no errors).

    Represents a HEALTHY platform for the install success-path cases. Strict
    refresh (the install path) now fails closed on a missing/unusable
    knowledge_service OR on returned errors (Codex B2 / Rev-B Blocker B), so the
    success-path cases must supply a real service rather than rely on the
    ``knowledge_service=None`` default (which previously MASKED that gap).
    """

    def refresh_plugin_processes(self, plugin_name: str) -> dict[str, object]:
        return {
            "status": "success",
            "plugin_name": plugin_name,
            "updated_count": 0,
            "process_keys": [],
            "errors": [],
        }


class _FailOnSecondRefreshKnowledgeService:
    """Success on the first refresh, errors on the second.

    Lets Case 11's SETUP install of the victim complete (so it becomes
    installed-but-not-loaded), while the guarded RE-install's refresh — reached
    only if the installed-guard is ABSENT and pip runs — fails, triggering the
    destructive ``_pip_uninstall_safe`` that Blocker A's guard prevents. This is
    exactly Rev-B's "on any later strict-refresh failure the rollback removes
    the pre-existing dist by name" shape.
    """

    def __init__(self) -> None:
        self._calls = 0

    def refresh_plugin_processes(self, plugin_name: str) -> dict[str, object]:
        self._calls += 1
        if self._calls >= 2:
            return {
                "status": "partial",
                "plugin_name": plugin_name,
                "errors": [f"Process key not found in registry: plugin::{plugin_name}::test_verb"],
            }
        return {"status": "success", "plugin_name": plugin_name, "errors": []}


class _RaisingUnregisterProcessRegistryManager:
    """ProcessRegistryManager whose de-register RAISES.

    Drives Codex B3: an unwind where ``remove``'s de-register step fails. The
    robust ``remove`` must still clean the roster + allowlist and the verb must
    report the rollback as PARTIAL rather than a definitive success.
    """

    def unregister_dynamic_processes(self, process_keys: list[str]) -> None:
        raise RuntimeError(
            "install-atomicity smoke: forced de-register failure for "
            f"{process_keys!r}"
        )


class _MinimalOrchestrator:
    """Fixture orchestrator wired with what install_plugin_from_path + remove need.

    The verb reads ``self.config`` for the config manager, calls
    ``self.get_service`` to find the knowledge service for registry refresh,
    and runs subprocess pip against ``sys.executable``. The removal primitive
    additionally reaches ``self._process_registry_manager`` (through the
    service's deregister callback). A minimal stand-in keeps the smoke
    library-level (matching the ``root_manifest_smoke.py`` precedent) without
    a full solet bring-up.
    """

    def __init__(
        self,
        plugin_manager: PluginManager,
        *,
        knowledge_service: object | None = None,
        process_registry_manager: object | None = None,
    ) -> None:
        self.plugin_manager = plugin_manager
        self.config = None
        self._knowledge_service = knowledge_service
        self._process_registry_manager = process_registry_manager

    def get_service(self, name: str) -> object | None:
        if name == "knowledge_service":
            return self._knowledge_service
        return None


@contextmanager
def _fresh_state(
    *,
    extra_uninstall: tuple[str, ...] = (),
    knowledge_service: object | None = None,
    process_registry_manager: object | None = None,
) -> Generator[tuple[LifecycleManagementService, PluginManager]]:
    """Build a fresh service + plugin manager, guarantee teardown cleanup.

    Pre-state: assert fixture plugin is NOT in entry_points (uninstall to
    enforce). Post-state: uninstall fixture(s), invalidate caches. A stuck
    fixture in the test runner's venv would be a tomorrow-problem for
    the next session that loads the solet, so the cleanup is wrapped in a
    try/finally that runs even on assertion failure. ``extra_uninstall``
    names additional fixtures (the probe/clone materialised in tempdirs)
    to pip-uninstall on both setup and teardown (R-B teardown hygiene).

    R-B: the manager itself is wired (set_orchestrator_ref +
    set_event_bus_ref), not just the service, so ``plugin_manager.orchestrator_ref``
    is populated and a victim plugin's ``orchestrator_ref`` becomes a real
    master-vs-postfix discriminator — on master a failed second install
    rediscovers-and-zombifies the victim to ``orchestrator_ref=None``;
    post-fix the victim's staged sibling never touches it.
    """
    all_fixtures = (_FIXTURE_NAME, *extra_uninstall)
    for name in all_fixtures:
        _pip_uninstall_silent(name)
    importlib.invalidate_caches()
    importlib.metadata.MetadataPathFinder.invalidate_caches()

    plugin_manager = PluginManager()
    plugin_manager.discover_plugins(allowed_plugins=set())
    orchestrator = _MinimalOrchestrator(
        plugin_manager,
        knowledge_service=knowledge_service,
        process_registry_manager=process_registry_manager,
    )
    plugin_manager.set_orchestrator_ref(cast(OrchestratorProtocol, orchestrator))
    plugin_manager.set_event_bus_ref(cast(EventBusProtocol, _FakeEventBus()))
    service = LifecycleManagementService(orchestrator_ref=orchestrator)

    try:
        yield service, plugin_manager
    finally:
        for name in all_fixtures:
            _pip_uninstall_silent(name)
        importlib.invalidate_caches()
        importlib.metadata.MetadataPathFinder.invalidate_caches()


def _install_via_verb(
    service: LifecycleManagementService, source: Path,
) -> dict[str, Any]:
    """Drive the canonical platform path: the live verb method."""
    return service.install_plugin_from_path(str(source))


def _case_a1_a2_a3_synthetic_fixture() -> None:
    """A1 + A2 + A3 covered by one install + four positive-outcome checks."""
    print("\nCase 1: A1+A2+A3 synthetic fixture install via the canonical verb")
    with _fresh_state(
        knowledge_service=_SuccessKnowledgeService(),
    ) as (service, plugin_manager), _temp_fixture_copy(
        _FIXTURE_DIR,
    ) as fixture_src:
        _check(
            not _site_packages_distinfo_present(_FIXTURE_NAME),
            "  precondition: fixture plugin not installed before the verb runs",
        )

        result = _install_via_verb(service, fixture_src)
        _check(
            result.get("action_status") == "completed",
            f"  verb returns action_status=completed (got {result!r})",
        )
        data = result.get("data") or {}
        _check(
            data.get("installed") is True,
            f"  data.installed is True (got {data!r})",
        )

        # ---- A1: metadata freshness (Bug-B fix) ----
        eps_after = _entry_point_names()
        _check(
            _FIXTURE_NAME in eps_after,
            "  A1 metadata freshness: entry_points sees fixture without restart",
        )

        # ---- A2: allowlist augmentation (Bug-A fix) ----
        # Direct protected access since smokes legitimately inspect internal
        # state; the property was reverted to keep PluginManager below the
        # god-class gate's public-method threshold.
        allowed = plugin_manager._allowed_plugins or set()  # noqa: SLF001
        _check(
            _FIXTURE_NAME in allowed,
            "  A2 allowlist augmentation: allowed_plugins now contains fixture",
        )
        _check(
            _FIXTURE_NAME in plugin_manager.plugins,
            "  A2 roster: rediscover produced a live instance in plugin_manager.plugins",
        )

        # ---- A3: in-session reachability (lifecycle rewire fix) ----
        plugin = plugin_manager.plugins[_FIXTURE_NAME]
        verb_result = plugin.test_verb({}, {})
        _check(
            verb_result.get("action_status") == "completed",
            "  A3 reachability: test_verb returns completed envelope",
        )
        _check(
            verb_result.get("data", {}).get("alive") is True,
            "  A3 reachability: test_verb data.alive is True (rewire fix verified)",
        )
        _check(
            plugin.is_ready(),
            "  A3 lifecycle: plugin reports is_ready() True after rewire",
        )


def _case_er6_intersection_broken_plugin() -> None:
    """Install + intentionally-broken plugin -> error envelope + pip uninstall ran."""
    print("\nCase 2: ER-6 intersection — broken plugin → error + rollback")
    with _fresh_state() as (service, plugin_manager), _broken_plugin_source() as source:
        broken_name = "fresh_install_smoke_plugin_broken"
        _check(
            not _site_packages_distinfo_present(broken_name),
            "  precondition: broken plugin not installed before the verb runs",
        )

        result = service.install_plugin_from_path(str(source))
        _check(
            result.get("action_status") == "error",
            f"  verb returns action_status=error (got {result.get('action_status')!r})",
        )
        error_message = str(result.get("error") or "")
        _check(
            "rolled back" in error_message,
            f"  error envelope names the rollback action (got {error_message!r})",
        )
        _check(
            "failed contract validation" in error_message,
            f"  error envelope names the validation failure (got {error_message!r})",
        )
        _check(
            broken_name not in plugin_manager.plugins,
            "  broken plugin is NOT in the live roster after rollback",
        )
        _check(
            not _site_packages_distinfo_present(broken_name),
            "  rollback removed the site-packages .dist-info artifact",
        )

        # Defensive cleanup beyond _fresh_state's cleanup, since the broken
        # plugin has a different name than the canonical fixture.
        _pip_uninstall_silent(broken_name)


@contextmanager
def _broken_plugin_source() -> Generator[Path]:
    """Build a temp plugin source whose discovery raises at instantiation."""
    base = Path(tempfile.mkdtemp(prefix="er6_broken_smoke_"))
    try:
        broken_name = "fresh_install_smoke_plugin_broken"
        pkg_dir = base / "src" / broken_name
        pkg_dir.mkdir(parents=True)
        (base / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = [\"setuptools>=68.0.0\", \"wheel\"]\n"
            "build-backend = \"setuptools.build_meta\"\n"
            "\n"
            "[project]\n"
            f"name = \"{broken_name}\"\n"
            "version = \"1.0.0\"\n"
            "requires-python = \"==3.13.*\"\n"
            "dependencies = []\n"
            "\n"
            "[project.entry-points.\"ananta.plugins\"]\n"
            f"{broken_name} = \"{broken_name}.plugin:Broken\"\n"
            "\n"
            "[tool.setuptools.packages.find]\n"
            "where = [\"src\"]\n",
            encoding="utf-8",
        )
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        # plugin.py raises on import — discover_plugins logs + skips, so the
        # plugin never enters plugin_manager.plugins. install_plugin_from_path
        # then hits the post-pip validation-failure branch and rolls back.
        (pkg_dir / "plugin.py").write_text(
            "raise RuntimeError("
            "'er6 smoke: deliberately broken plugin module'"
            ")\n",
            encoding="utf-8",
        )
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@contextmanager
def _materialize_variant(
    new_name: str, *, prepare_raises: bool = False,
) -> Generator[Path]:
    """Materialise a fixture-A clone under a fresh package name in a tempdir.

    Copies the committed fixture-A tree (minus ``__pycache__``), renames the
    ``src/<pkg>`` package dir, substitutes the plugin name across
    pyproject.toml / plugin.yaml / plugin.py, and (when ``prepare_raises``)
    rewrites ``prepare_for_readiness`` to raise. The raising variant
    reproduces the g_suite incident's WIRING-phase failure (pip OK, discovery
    OK, contract OK, wiring raises) — which fixture A (import-clean) and the
    Case-2 broken fixture (import-raise) both miss.
    """
    base = Path(tempfile.mkdtemp(prefix="c1a_variant_"))
    target = base / new_name
    shutil.copytree(
        _FIXTURE_DIR, target, ignore=shutil.ignore_patterns("__pycache__"),
    )
    try:
        (target / "src" / _FIXTURE_NAME).rename(target / "src" / new_name)
        for rel in ("pyproject.toml", "plugin.yaml", f"src/{new_name}/plugin.py"):
            path = target / rel
            text = path.read_text(encoding="utf-8").replace(_FIXTURE_NAME, new_name)
            if prepare_raises and rel.endswith("plugin.py"):
                text = text.replace(
                    "        self.set_ready()",
                    "        raise RuntimeError(\n"
                    "            \"install-atomicity smoke: deliberate "
                    "prepare_for_readiness failure\"\n"
                    "        )",
                )
            path.write_text(text, encoding="utf-8")
        yield target
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _is_plugins_subscript(target: ast.expr) -> bool:
    """True if ``target`` is a ``<expr>.plugins[<key>]`` subscript node."""
    return (
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "plugins"
    )


# Roster mutations the scanned methods must never contain — clear-and-rebuild
# calls and direct dict mutations on a `.plugins` attribute. Covers the
# install path (C1a) and set_plugin_enabled's enable-if-absent branch
# (LIF-01/C1b); the disable path's `.pop()` is not scanned (own tracked slice).
# `_rediscover_plugins` no longer exists in service.py (deleted with C1b,
# zero remaining callers) — kept here as a defensive name in case a future
# edit reintroduces a same-named helper.
_FORBIDDEN_ROSTER_CALLS = frozenset({
    "discover_plugins",
    "_rediscover_plugins",
    "_rediscover_plugins_with_allowlist",
})
_MUTATING_DICT_METHODS = frozenset({"pop", "clear", "update", "setdefault", "popitem"})


def _forbidden_call_hit(node: ast.AST) -> str | None:
    """Describe a clear-and-rebuild call or a ``.plugins`` dict-mutation, or None."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    attr = node.func.attr
    if attr in _FORBIDDEN_ROSTER_CALLS:
        return f"calls {attr}()"
    value = node.func.value
    if (
        attr in _MUTATING_DICT_METHODS
        and isinstance(value, ast.Attribute)
        and value.attr == "plugins"
    ):
        return f"calls .plugins.{attr}()"
    return None


def _subscript_mutation_hit(node: ast.AST) -> str | None:
    """Describe a ``.plugins[...]`` assignment or ``del``, or None."""
    if isinstance(node, ast.Assign) and any(
        _is_plugins_subscript(t) for t in node.targets
    ):
        return "assigns .plugins[...]"
    if isinstance(node, ast.Delete) and any(
        _is_plugins_subscript(t) for t in node.targets
    ):
        return "deletes .plugins[...]"
    return None


def _roster_mutation_hit(node: ast.AST) -> str | None:
    """Describe a direct roster mutation at ``node`` (call or subscript), or None."""
    return _forbidden_call_hit(node) or _subscript_mutation_hit(node)


def _case3_install_atomicity_red_matrix() -> None:
    """R1-R7: a wiring-phase install failure leaves the victim byte-identical."""
    print("\nCase 3: install-atomicity red matrix (R1-R7) — victim survives a failed install")
    with _fresh_state(
        extra_uninstall=(_PROBE_NAME,),
        knowledge_service=_SuccessKnowledgeService(),
    ) as (service, plugin_manager), _temp_fixture_copy(_FIXTURE_DIR) as fixture_a:
        setup = _install_via_verb(service, fixture_a)
        _check(
            setup.get("action_status") == "completed",
            f"  setup: victim (fixture A) installs (got {setup.get('action_status')!r})",
        )
        victim = plugin_manager.plugins.get(_FIXTURE_NAME)
        orchestrator = plugin_manager.orchestrator_ref
        _check(
            victim is not None and victim.orchestrator_ref is orchestrator,
            "  setup: victim wired (orchestrator_ref is the manager's orchestrator)",
        )
        _check(
            victim is not None and victim.is_ready(),
            "  setup: victim is_ready() before the failing install",
        )

        with _materialize_variant(_PROBE_NAME, prepare_raises=True) as probe_src:
            result = service.install_plugin_from_path(str(probe_src))
        _check(
            result.get("action_status") == "error",
            f"  probe install returns error (got {result.get('action_status')!r})",
        )

        # RED on master, MUST pass post-fix.
        _check(
            plugin_manager.plugins.get(_FIXTURE_NAME) is victim,
            "  R1 victim instance identity preserved (master: rediscovery replaced it)",
        )
        surviving = plugin_manager.plugins.get(_FIXTURE_NAME)
        _check(
            surviving is not None and surviving.orchestrator_ref is orchestrator,
            "  R2 victim orchestrator_ref intact (master: zombie orchestrator_ref=None)",
        )
        _check(
            surviving is not None and surviving.is_ready(),
            "  R3 victim is_ready() (master: zombie never prepared)",
        )
        _check(
            _PROBE_NAME not in plugin_manager.plugins,
            "  R4 failed probe not left in roster (master: half-wired probe retained)",
        )
        allowed = plugin_manager._allowed_plugins or set()  # noqa: SLF001
        _check(
            _PROBE_NAME not in allowed,
            "  R5 failed probe not left in allowlist (master: allowlist leak)",
        )
        _check(
            not _site_packages_distinfo_present(_PROBE_NAME),
            "  R6 probe pip artifact rolled back (master: no rollback on wiring branch)",
        )
        error_message = str(result.get("error") or "")
        _check(
            "at phase" in error_message and "rolled back" in error_message,
            f"  R7 error names the failing phase + rollback (got {error_message!r})",
        )

        # Post-fix guard against over-rotation: the survivor still dispatches.
        if surviving is not None:
            verb_result = surviving.test_verb({}, {})
            _check(
                verb_result.get("data", {}).get("alive") is True,
                "  victim still dispatches test_verb after the failed install",
            )


def _case6_remove_atomicity_unwind() -> None:
    """Remove atomicity: a post-commit registry-refresh failure unwinds atomically."""
    print("\nCase 6: remove-atomicity — post-commit registry-refresh failure unwinds atomically")
    prm = _FakeProcessRegistryManager()
    ks = _RaisingKnowledgeService(fail_for=_CLONE_NAME)
    with _fresh_state(
        extra_uninstall=(_CLONE_NAME,),
        knowledge_service=ks,
        process_registry_manager=prm,
    ) as (service, plugin_manager), _temp_fixture_copy(_FIXTURE_DIR) as fixture_a:
        setup = _install_via_verb(service, fixture_a)
        _check(
            setup.get("action_status") == "completed",
            f"  setup: victim installs (refresh succeeds for it) (got {setup.get('action_status')!r})",
        )
        victim = plugin_manager.plugins.get(_FIXTURE_NAME)
        orchestrator = plugin_manager.orchestrator_ref
        _check(
            victim is not None
            and victim.orchestrator_ref is orchestrator
            and victim.is_ready(),
            "  setup: victim wired + ready",
        )

        with _materialize_variant(_CLONE_NAME, prepare_raises=False) as clone_src:
            result = service.install_plugin_from_path(str(clone_src))
        _check(
            result.get("action_status") == "error",
            f"  clone install errors after forced refresh failure (got {result.get('action_status')!r})",
        )

        clone_key = f"plugin::{_CLONE_NAME}::test_verb"
        # RED on master (clone left half-in), GREEN post-fix (atomic unwind).
        _check(
            _CLONE_NAME not in plugin_manager.plugins,
            "  clone removed from roster by the unwind (master: committed, left in roster)",
        )
        _check(
            clone_key in prm.unregistered,
            "  clone process keys DE-REGISTERED before pop (master: unregister never called)",
        )
        allowed = plugin_manager._allowed_plugins or set()  # noqa: SLF001
        _check(
            _CLONE_NAME not in allowed,
            "  clone removed from allowlist by the unwind (master: allowlist leak)",
        )
        _check(
            not _site_packages_distinfo_present(_CLONE_NAME),
            "  clone pip artifact rolled back (master: not rolled back)",
        )
        _check(
            plugin_manager.plugins.get(_FIXTURE_NAME) is victim,
            "  victim instance preserved through the clone unwind (master: zombified)",
        )
        surviving = plugin_manager.plugins.get(_FIXTURE_NAME)
        _check(
            surviving is not None
            and surviving.orchestrator_ref is orchestrator
            and surviving.is_ready(),
            "  victim still wired + ready (master: zombie)",
        )
        error_message = str(result.get("error") or "")
        _check(
            "rolled back" in error_message and "registry refresh" in error_message,
            f"  error names the registry-refresh rollback (got {error_message!r})",
        )


def _method_roster_violations(method: ast.FunctionDef) -> list[str]:
    """Every direct roster mutation / clear-and-rebuild inside one method."""
    hits: list[str] = []
    for inner in ast.walk(method):
        hit = _roster_mutation_hit(inner)
        if hit is not None:
            hits.append(f"{method.name} {hit}")
    return hits


def _case7_single_writer_guard() -> None:
    """AST guard: the install path mutates the roster ONLY via the installer."""
    print("\nCase 7: single-writer invariant — install path mutates roster only via installer")
    tree = ast.parse(_LIFECYCLE_SERVICE_SRC.read_text(encoding="utf-8"))
    scanned_methods = {
        "install_plugin_from_path",
        "_stage_commit_or_rollback",
        "_wire_plugin_instance",
        "_unwind_committed_install",
        # LIF-01/C1b: set_plugin_enabled's enable-if-absent branch now routes
        # through the same installer; disable's `.pop()` is still its own
        # tracked slice (plugin_installer.py:196-199's discovery-vector
        # caveat on `remove`), not scanned here.
        "_apply_plugin_enable",
        "_install_plugin_for_enable",
        "_entry_point_not_found_result",
    }
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    violations: list[str] = []
    for func in functions:
        if func.name in scanned_methods:
            violations.extend(_method_roster_violations(func))

    _check(
        not violations,
        f"  install path never clear-and-rebuilds nor directly mutates the roster "
        f"(violations: {violations})",
    )
    defined = {func.name for func in functions}
    _check(
        scanned_methods <= defined,
        f"  all scanned install-path methods exist (missing: {sorted(scanned_methods - defined)})",
    )


def _case8_already_loaded_no_destructive_pip() -> None:
    """Codex B1: an already-loaded re-install must NOT run pip (no destructive uninstall)."""
    print("\nCase 8: already-loaded re-install must NOT touch pip (no destructive uninstall)")
    with _fresh_state(
        knowledge_service=_SuccessKnowledgeService(),
    ) as (service, plugin_manager), _temp_fixture_copy(
        _FIXTURE_DIR,
    ) as fixture_a:
        setup = _install_via_verb(service, fixture_a)
        _check(
            setup.get("action_status") == "completed",
            f"  setup: victim installs (got {setup.get('action_status')!r})",
        )
        victim = plugin_manager.plugins.get(_FIXTURE_NAME)
        _check(
            victim is not None and _site_packages_distinfo_present(_FIXTURE_NAME),
            "  setup: victim in roster + dist-info present",
        )

        # Re-install the SAME already-loaded plugin from the same source.
        result = service.install_plugin_from_path(str(fixture_a))
        _check(
            result.get("action_status") == "error",
            f"  already-loaded re-install returns error (got {result.get('action_status')!r})",
        )
        _check(
            _site_packages_distinfo_present(_FIXTURE_NAME),
            "  live plugin's dist-info UNTOUCHED (pre-fix: destructive pip uninstall of the running plugin)",
        )
        _check(
            plugin_manager.plugins.get(_FIXTURE_NAME) is victim,
            "  live plugin instance preserved in roster",
        )
        error_message = str(result.get("error") or "")
        _check(
            "already loaded" in error_message and "rolled back" not in error_message,
            f"  clean already-loaded guidance, no false 'rolled back' claim (got {error_message!r})",
        )


def _case9_refresh_errors_fail_closed() -> None:
    """Codex B2: registry-refresh RETURNING errors (not raising) must fail-closed + unwind."""
    print("\nCase 9: registry-refresh returning errors must fail-closed + unwind (not false success)")
    prm = _FakeProcessRegistryManager()
    ks = _PartialRefreshKnowledgeService()
    with _fresh_state(
        extra_uninstall=(_CLONE_NAME,),
        knowledge_service=ks,
        process_registry_manager=prm,
    ) as (service, plugin_manager):
        with _materialize_variant(_CLONE_NAME, prepare_raises=False) as clone_src:
            result = service.install_plugin_from_path(str(clone_src))
        _check(
            result.get("action_status") == "error",
            f"  install fails closed on refresh errors (got {result.get('action_status')!r}) "
            f"[pre-fix: false success]",
        )
        _check(
            _CLONE_NAME not in plugin_manager.plugins,
            "  plugin unwound from roster after refresh-error fail-close [pre-fix: left committed]",
        )
        allowed = plugin_manager._allowed_plugins or set()  # noqa: SLF001
        _check(
            _CLONE_NAME not in allowed,
            "  plugin removed from allowlist [pre-fix: allowlist leak]",
        )
        _check(
            not _site_packages_distinfo_present(_CLONE_NAME),
            "  pip artifact rolled back [pre-fix: not rolled back]",
        )
        error_message = str(result.get("error") or "")
        _check(
            "registry refresh" in error_message,
            f"  error names the registry-refresh failure (got {error_message!r})",
        )


def _case10_unwind_partial_honest_report() -> None:
    """Codex B3: unwind where de-register raises — roster still cleaned, honest PARTIAL report."""
    print("\nCase 10: unwind where de-register raises — roster still cleaned, honest partial report")
    prm = _RaisingUnregisterProcessRegistryManager()
    ks = _RaisingKnowledgeService(fail_for=_CLONE_NAME)
    with _fresh_state(
        extra_uninstall=(_CLONE_NAME,),
        knowledge_service=ks,
        process_registry_manager=prm,
    ) as (service, plugin_manager):
        with _materialize_variant(_CLONE_NAME, prepare_raises=False) as clone_src:
            result = service.install_plugin_from_path(str(clone_src))
        _check(
            result.get("action_status") == "error",
            f"  install errors (got {result.get('action_status')!r})",
        )
        _check(
            _CLONE_NAME not in plugin_manager.plugins,
            "  roster still cleaned despite de-register failure [pre-fix: half-state, plugin stuck]",
        )
        allowed = plugin_manager._allowed_plugins or set()  # noqa: SLF001
        _check(
            _CLONE_NAME not in allowed,
            "  allowlist still cleaned despite de-register failure [pre-fix: allowlist leak]",
        )
        error_message = str(result.get("error") or "")
        _check(
            "PARTIAL" in error_message,
            f"  caller told rollback was PARTIAL, not a definitive success "
            f"[pre-fix: unconditional 'rolled back'] (got {error_message!r})",
        )


def _case11_installed_not_loaded_no_destructive_pip() -> None:
    """Rev-B Blocker A: an INSTALLED-but-not-loaded same-name install must NOT run pip."""
    print("\nCase 11: installed-but-not-loaded same-name install must NOT touch pip (connector shape)")
    # ks succeeds on the setup install (call 1) but errors on any re-install
    # refresh (call 2) — so if the installed-guard is ABSENT, the re-install
    # runs pip then fails at strict refresh, and the rollback destroys the
    # pre-existing dist by name (the exact harm the guard prevents).
    with _fresh_state(
        knowledge_service=_FailOnSecondRefreshKnowledgeService(),
    ) as (service, plugin_manager), _temp_fixture_copy(_FIXTURE_DIR) as fixture_a:
        setup = _install_via_verb(service, fixture_a)
        _check(
            setup.get("action_status") == "completed",
            f"  setup: victim installs (got {setup.get('action_status')!r})",
        )
        _check(
            _site_packages_distinfo_present(_FIXTURE_NAME),
            "  setup: victim dist-info present",
        )
        # Simulate installed-but-not-loaded (an inert connector awaiting a
        # blue-green boot): drop it from the roster, leaving the pip
        # distribution + entry-point in place.
        plugin_manager.plugins.pop(_FIXTURE_NAME, None)
        _check(
            _FIXTURE_NAME not in plugin_manager.plugins,
            "  precondition: not in the live roster (installed but not loaded)",
        )
        _check(
            _FIXTURE_NAME in _entry_point_names(),
            "  precondition: still INSTALLED (ananta.plugins entry-point present)",
        )

        result = service.install_plugin_from_path(str(fixture_a))
        _check(
            result.get("action_status") == "error",
            f"  installed-not-loaded re-install returns error (got {result.get('action_status')!r})",
        )
        _check(
            _site_packages_distinfo_present(_FIXTURE_NAME),
            "  pre-existing dist-info UNTOUCHED (pre-fix: roster-only guard let pip run → rollback destroyed it)",
        )
        error_message = str(result.get("error") or "")
        _check(
            "already installed" in error_message and "rolled back" not in error_message,
            f"  clean already-installed guidance, no false 'rolled back' claim (got {error_message!r})",
        )


def _case12_missing_knowledge_service_fail_closed() -> None:
    """Rev-B Blocker B: strict refresh with NO usable knowledge_service must fail-closed + unwind."""
    print("\nCase 12: strict refresh with NO knowledge_service must fail-closed + unwind (not false success)")
    prm = _FakeProcessRegistryManager()
    # knowledge_service=None — the degraded / miswired orchestrator the
    # `_fresh_state` default previously MASKED (Rev-B test-gap).
    with _fresh_state(
        extra_uninstall=(_CLONE_NAME,),
        knowledge_service=None,
        process_registry_manager=prm,
    ) as (service, plugin_manager):
        with _materialize_variant(_CLONE_NAME, prepare_raises=False) as clone_src:
            result = service.install_plugin_from_path(str(clone_src))
        _check(
            result.get("action_status") == "error",
            f"  install fails closed when knowledge_service is absent (got "
            f"{result.get('action_status')!r}) [pre-fix: false success]",
        )
        _check(
            _CLONE_NAME not in plugin_manager.plugins,
            "  plugin unwound from roster [pre-fix: committed]",
        )
        allowed = plugin_manager._allowed_plugins or set()  # noqa: SLF001
        _check(
            _CLONE_NAME not in allowed,
            "  plugin removed from allowlist [pre-fix: allowlist leak]",
        )
        _check(
            not _site_packages_distinfo_present(_CLONE_NAME),
            "  pip artifact rolled back [pre-fix: not rolled back]",
        )
        error_message = str(result.get("error") or "")
        _check(
            "knowledge_service" in error_message,
            f"  error names the missing knowledge_service (got {error_message!r})",
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
    print(f"Running install_plugin_from_path atomicity smoke (C1a) against {REPO_ROOT}")
    _case_a1_a2_a3_synthetic_fixture()
    _case_er6_intersection_broken_plugin()
    _case3_install_atomicity_red_matrix()
    _case6_remove_atomicity_unwind()
    _case7_single_writer_guard()
    _case8_already_loaded_no_destructive_pip()
    _case9_refresh_errors_fail_closed()
    _case10_unwind_partial_honest_report()
    _case11_installed_not_loaded_no_destructive_pip()
    _case12_missing_knowledge_service_fail_closed()
    _summary_and_exit()


if __name__ == "__main__":
    main()
