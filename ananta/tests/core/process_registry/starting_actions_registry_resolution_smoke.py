#!/usr/bin/env python3
"""Every shipped ``starting_actions`` process_key resolves in the REAL registry.

iss_49a3820a: batch #18 wired ``plugin::actr_memory_plugin::ensure_schedules``
as a boot ``starting_actions`` entry in five shipped profiles, but
``actr_memory_plugin`` is the bound ``memory_service`` provider, and
``PluginProcessScanner._should_skip_plugin`` drops a bound provider's whole
``plugin::<name>::*`` namespace at registry build. The key never resolved
live ("not found or malformed in registry"); the batch's tests called the
plugin method directly and never touched the registry.

This smoke drives the real registry collaborators, no fakes of the
registration path:

  1. The real ``ServiceInterfaceScanner`` registers
     ``service_interface::memory_service::ensure_schedules`` (the fix).
  2. The real ``PluginProcessScanner`` registers ZERO ``plugin::`` verbs for
     a real ``ACTRMemoryPlugin`` bound to ``memory_service`` — pins the class
     behaviour that made the old key dead.
  3. Guard: for every profile under ``initialization/profiles/`` and every
     shipped ``plugins/*/knowledge_base/profile_templates/`` profile, each
     ``starting_actions`` process_key resolves: a ``service_interface::`` key
     is in the real scanned registry; a ``plugin::<p>::<v>`` key names a
     plugin the profile loads, that the profile does NOT bind as a service
     provider (else it is skipped), and ``<v>`` is a real
     ``@platform_process`` in that plugin's source.

Project policy: no pytest. Offline. Exits 0 on success, 1 on any failure.
"""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))

from actr_memory_plugin.plugin import ACTRMemoryPlugin  # noqa: E402
from ananta.core.plugins.plugin_base import PluginBase  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import (  # noqa: E402
    InvocationSchemaGenerator,
)
from ananta.core.process_registry.plugin_process_scanner import (  # noqa: E402
    PluginProcessScanner,
)
from ananta.core.process_registry.plugin_registration_validator import (  # noqa: E402
    PluginRegistrationValidator,
)
from ananta.core.process_registry.service_interface_metadata_generator import (  # noqa: E402
    ServiceInterfaceMetadataGenerator,
)
from ananta.core.process_registry.service_interface_scanner import (  # noqa: E402
    ServiceInterfaceScanner,
)

ENSURE_SCHEDULES_KEY = "service_interface::memory_service::ensure_schedules"
PROFILE_GLOBS = (
    "initialization/profiles/*.yaml",
    "plugins/*/knowledge_base/profile_templates/*.yaml",
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


class _Bindings:
    def __init__(self, bound_plugins: set[str]) -> None:
        self._bound = bound_plugins

    def is_plugin_bound_to_service(self, plugin_name: str) -> bool:
        return plugin_name in self._bound


class _Orchestrator:
    def __init__(self, bound_plugins: set[str]) -> None:
        self.service_bindings = _Bindings(bound_plugins)


class _PluginManager:
    def __init__(self, plugins: dict[str, PluginBase], bound_plugins: set[str]) -> None:
        self.plugins = plugins
        self.orchestrator_ref = _Orchestrator(bound_plugins)


def _real_service_interface_registry() -> dict[str, object]:
    registry: dict[str, object] = {"processes": {}}
    ServiceInterfaceScanner(schema_generator=InvocationSchemaGenerator()).scan(registry)
    processes = registry["processes"]
    assert isinstance(processes, dict)
    return processes


def _bare_actr_plugin() -> ACTRMemoryPlugin:
    instance = ACTRMemoryPlugin.__new__(ACTRMemoryPlugin)
    instance.name = "actr_memory_plugin"  # type: ignore[assignment]
    instance.logger = logging.getLogger("starting_actions_registry_resolution_smoke")
    instance.logger.disabled = True
    return instance


def _case_service_interface_registers_ensure_schedules(processes: dict[str, object]) -> None:
    print("\nCase 1: real ServiceInterfaceScanner registers ensure_schedules")
    _check(ENSURE_SCHEDULES_KEY in processes, f"{ENSURE_SCHEDULES_KEY} is in the real registry")


def _case_bound_provider_plugin_namespace_is_skipped() -> None:
    print("\nCase 2: real PluginProcessScanner skips a bound provider's plugin:: namespace")
    plugin = _bare_actr_plugin()
    schema_generator = InvocationSchemaGenerator()
    scanner = PluginProcessScanner(
        plugin_manager=_PluginManager({"actr_memory_plugin": plugin}, {"actr_memory_plugin"}),  # type: ignore[arg-type]
        validator=PluginRegistrationValidator(),
        metadata_generator=ServiceInterfaceMetadataGenerator(),
        schema_generator=schema_generator,
    )
    registry: dict[str, object] = {"processes": {}}
    registered = scanner.scan_and_register(registry)
    processes = registry["processes"]
    assert isinstance(processes, dict)
    actr_keys = [key for key in processes if key.startswith("plugin::actr_memory_plugin::")]
    _check(
        registered == 0 and not actr_keys,
        f"bound actr_memory_plugin registers no plugin:: verbs (got {registered}: {actr_keys})",
    )
    _check(
        not hasattr(ACTRMemoryPlugin.ensure_schedules, "_platform_process_metadata"),
        "ACTRMemoryPlugin.ensure_schedules is an interface method, not a dead @platform_process",
    )


def _decorator_registered_name(decorator: ast.expr, func_name: str) -> str | None:
    """Registered verb name of a ``@platform_process(...)`` decorator, else None."""
    if not isinstance(decorator, ast.Call):
        return None
    target = decorator.func
    called = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
    if called != "platform_process":
        return None
    for keyword in decorator.keywords:
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    return func_name


def _platform_process_verbs(plugin_name: str) -> set[str]:
    """AST-read the plugin's ``@platform_process`` verbs.

    AST, not import: importing a plugin module can need a live environment
    (``agent_messaging_plugin`` requires ``SOLET_NAME`` at import), and this
    smoke must stay offline.
    """
    verbs: set[str] = set()
    for source in sorted((REPO_ROOT / "plugins" / plugin_name / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text(), filename=str(source))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                registered = _decorator_registered_name(decorator, node.name)
                if registered is not None:
                    verbs.add(registered)
    return verbs


def _check_plugin_key(profile: str, process_key: str, plugins: set[str], bound: set[str]) -> None:
    _, plugin_name, verb = process_key.split("::")
    _check(plugin_name in plugins, f"{profile}: {process_key} names a plugin the profile loads")
    _check(
        plugin_name not in bound,
        f"{profile}: {process_key} names a plugin the profile does not bind as a service "
        "provider (a bound provider's plugin:: namespace is skipped)",
    )
    _check(
        verb in _platform_process_verbs(plugin_name),
        f"{profile}: {process_key} is a real @platform_process in the plugin's source",
    )


def _check_profile_starting_actions(
    rel: str, profile: dict[str, Any], processes: dict[str, object]
) -> int:
    plugins = set(profile.get("plugins") or [])
    bound = set((profile.get("service_bindings") or {}).values())
    for action in profile["starting_actions"]:
        process_key = action["process_key"]
        if process_key.startswith("service_interface::"):
            _check(process_key in processes, f"{rel}: {process_key} is in the real registry")
        elif process_key.startswith("plugin::"):
            _check_plugin_key(rel, process_key, plugins, bound)
        else:
            _check(False, f"{rel}: {process_key} has an unknown namespace")
    return len(profile["starting_actions"])


def _case_every_starting_action_resolves(processes: dict[str, object]) -> None:
    print("\nCase 3: every shipped starting_actions process_key resolves")
    profiles = sorted(p for pattern in PROFILE_GLOBS for p in REPO_ROOT.glob(pattern))
    checked = 0
    for path in profiles:
        profile = yaml.safe_load(path.read_text())
        if not isinstance(profile, dict) or not profile.get("starting_actions"):
            continue
        checked += _check_profile_starting_actions(
            str(path.relative_to(REPO_ROOT)), profile, processes
        )
    _check(checked > 0, f"guard actually checked starting_actions entries (checked {checked})")


def main() -> int:
    print("starting_actions registry resolution smoke (iss_49a3820a)")
    print("==========================================================")
    processes = _real_service_interface_registry()
    _case_service_interface_registers_ensure_schedules(processes)
    _case_bound_provider_plugin_namespace_is_skipped()
    _case_every_starting_action_resolves(processes)

    print("\n----------------------------------------------------------")
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    if _failed:
        print("\nFailures:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
