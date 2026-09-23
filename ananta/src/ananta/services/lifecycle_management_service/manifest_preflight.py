"""Pre-flight checks for ``apply_manifest`` — L1 of the local blue/green design.

Three checks layered on top of :mod:`binding_validator`:

* §2.1 **Import-time check** — load each new manifest plugin's entry-point
  and import every ``ananta.services.*.interfaces.public`` module. Catches
  stale enum references and other module-body errors that would have
  caused the boot to crash post-restart (e.g. ``ParameterType.ARRAY``
  surfaced by the post-Phase-A bundle restart cascade).
* §2.2 **Instantiate-and-decorator check** — instantiate each plugin
  class bare (no orchestrator wiring) and run two validators against the
  instance:

  - :class:`PluginRegistrationValidator` — verifies EdgeProcessProvider
    contract: ``get_edge_process_definitions`` matches decorated methods
    (customizations optional since the 2026-07-15 relax).
  - :class:`KnowledgeBaseOverlayLoader` — exercises the same JSON-file load
    path the runtime uses; catches missing ``processes/<name>.json`` stubs.

  Per the Architect's review (Finding 1), these are TWO separate
  validators — the kb_overlay_loader hard-fails on missing JSON, which
  is the failure class that the post-Phase-A trigger_poll.json miss
  and the M5.C three-stub miss landed in.

* §2.3 **Cross-plugin merge check** — the Architect's design framed this
  as a separate dry-run merge pass. Coordinator's review Finding 2
  observed that the actually-distinct work is post-merge embedding-
  description validation + the customizations contract; plugin-namespace
  collisions are impossible by construction (the plugin name IS the
  namespace) and service_interface_process collisions are caught at
  decorator scan. We therefore re-use ``KnowledgeBaseOverlayLoader.apply``
  for §2.3 (which runs the post-merge customization check) and add a
  call to ``validate_all_embedding_descriptions``.

**Cache-poisoning limit (design gap surfaced by L1 implementation):**
``importlib.import_module`` returns the cached module if the module is
already in ``sys.modules``. The pre-flight runs in the live platform, so
plugins or service-interface modules that were already imported with the
OLD good code will pass even if the on-disk source now has a NEW bad
version. The check therefore catches NET-NEW plugins (never imported in
this process) reliably; edit-existing-plugin-then-apply-manifest is
the dominant case but only catches errors that surface at INSTANTIATE or
DECORATOR-SCAN time, not at module-import time. L2's probe mechanic
(design §3) is the canonical fix.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import pkgutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ananta.core.actions.action_metadata import ActionMetadata
from ananta.core.process_registry.invocation_schema_generator import (
    InvocationSchemaGenerator,
)
from ananta.core.process_registry.kb_overlay_loader import (
    KnowledgeBaseOverlayLoader,
)
from ananta.core.process_registry.plugin_registration_validator import (
    PluginRegistrationValidator,
)
from ananta.core.services.service_interface_decorator import (
    ServiceInterfaceActionMetadata,
    is_service_interface_process_enabled,
)

if TYPE_CHECKING:
    from ananta.core.plugins.plugin_base import PluginBase

_ENTRY_POINT_GROUP = "ananta.plugins"
_SERVICES_PACKAGE = "ananta.services"
_INTERFACES_PUBLIC_SUFFIX = ".interfaces.public"

# The one L1 failure class with a demonstrated FALSE-REJECT mode: the live
# process's importlib.metadata scan can miss a dist-info installed after
# boot (the 2026-07-06 "L1.1 boot-stale" specimen). The L1 CALL SITE
# (``service._preflight_apply_manifest``) defers this class to the L2
# probe (GTE-06 A2); this function itself stays context-free and fully
# rejecting so the probe context still rejects a genuinely-missing entry
# point.
ENTRY_POINT_MISSING_ERROR_CLASS = "EntryPointMissingError"


@dataclass(frozen=True, slots=True)
class PreflightFailure:
    """A single failure surfaced by one of the L1 pre-flight checks."""

    check: str
    plugin: str | None
    message: str
    error_class: str


@dataclass(slots=True)
class PreflightResult:
    """Outcome of the three L1 checks. ``ok`` iff ``failures`` is empty."""

    failures: list[PreflightFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


# ─── §2.1 Import-time check ─────────────────────────────────────────────────


def check_imports(
    plugin_names: Iterable[str],
) -> tuple[dict[str, type], list[PreflightFailure]]:
    """Load each plugin entry-point and import every service-interface module.

    Returns the successfully-loaded plugin classes alongside any failures.
    A failure for one plugin does NOT short-circuit the remaining imports —
    L1 surfaces every issue in one pass so the operator sees the full set.
    """
    failures: list[PreflightFailure] = []
    plugin_classes: dict[str, type] = {}

    wanted = {name for name in plugin_names if name}
    if wanted:
        entry_points_by_name = _entry_points_by_name()
        for plugin_name in sorted(wanted):
            ep = entry_points_by_name.get(plugin_name)
            if ep is None:
                failures.append(PreflightFailure(
                    check="L1.1_import",
                    plugin=plugin_name,
                    message=(
                        f"no installed entry point for {plugin_name!r} "
                        f"in group {_ENTRY_POINT_GROUP!r}"
                    ),
                    error_class=ENTRY_POINT_MISSING_ERROR_CLASS,
                ))
                continue
            try:
                plugin_class = ep.load()
            except Exception as exc:  # noqa: BLE001 — surface ANY load failure
                failures.append(PreflightFailure(
                    check="L1.1_import",
                    plugin=plugin_name,
                    message=str(exc),
                    error_class=type(exc).__name__,
                ))
                continue
            if not isinstance(plugin_class, type):
                failures.append(PreflightFailure(
                    check="L1.1_import",
                    plugin=plugin_name,
                    message=(
                        f"entry point did not resolve to a class; got "
                        f"{type(plugin_class).__name__}"
                    ),
                    error_class="EntryPointResolutionError",
                ))
                continue
            plugin_classes[plugin_name] = plugin_class

    # Static scan: import every ananta.services.*.interfaces.public so stale
    # references in service-interface decoration land here, not at boot.
    failures.extend(_check_service_interface_modules())

    return plugin_classes, failures


def _entry_points_by_name() -> dict[str, importlib.metadata.EntryPoint]:
    entry_points = importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP)
    return {ep.name: ep for ep in entry_points}


def _check_service_interface_modules() -> list[PreflightFailure]:
    failures: list[PreflightFailure] = []
    try:
        services_pkg = importlib.import_module(_SERVICES_PACKAGE)
    except Exception as exc:  # noqa: BLE001
        failures.append(PreflightFailure(
            check="L1.1_import",
            plugin=None,
            message=f"could not import {_SERVICES_PACKAGE}: {exc}",
            error_class=type(exc).__name__,
        ))
        return failures
    for _, mod_name, ispkg in pkgutil.iter_modules(
        services_pkg.__path__, prefix=f"{_SERVICES_PACKAGE}.",
    ):
        if not ispkg:
            continue
        public_name = f"{mod_name}{_INTERFACES_PUBLIC_SUFFIX}"
        try:
            importlib.import_module(public_name)
        except ModuleNotFoundError:
            # No interfaces/public.py for this service; legitimate.
            continue
        except Exception as exc:  # noqa: BLE001 — surface any other failure
            failures.append(PreflightFailure(
                check="L1.1_import",
                plugin=None,
                message=f"{public_name}: {exc}",
                error_class=type(exc).__name__,
            ))
    return failures


# ─── §2.2 Instantiate + decorator + kb_overlay checks ───────────────────────


def check_instantiation_and_decorators(
    plugin_classes: Mapping[str, type],
) -> tuple[dict[str, PluginBase], list[PreflightFailure]]:
    """Instantiate each class bare and run the registration validator.

    Returns the instantiated plugins (keyed by name) so §2.3 can run kb_overlay
    against them without re-instantiating.
    """
    failures: list[PreflightFailure] = []
    plugin_instances: dict[str, PluginBase] = {}
    validator = PluginRegistrationValidator()

    for plugin_name, plugin_class in plugin_classes.items():
        try:
            instance = plugin_class()
        except Exception as exc:  # noqa: BLE001
            failures.append(PreflightFailure(
                check="L1.2_instantiate",
                plugin=plugin_name,
                message=f"plugin __init__ raised: {exc}",
                error_class=type(exc).__name__,
            ))
            continue

        try:
            actions = instance.get_available_actions()
        except Exception as exc:  # noqa: BLE001
            failures.append(PreflightFailure(
                check="L1.2_decorator_scan",
                plugin=plugin_name,
                message=f"get_available_actions raised: {exc}",
                error_class=type(exc).__name__,
            ))
            continue

        try:
            validator.validate_edge_process_provider(
                plugin_name, instance, actions,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(PreflightFailure(
                check="L1.2_registration_validator",
                plugin=plugin_name,
                message=str(exc),
                error_class=type(exc).__name__,
            ))
            continue

        plugin_instances[plugin_name] = instance

    return plugin_instances, failures


def check_kb_overlay_and_collisions(
    plugin_instances: Mapping[str, PluginBase],
) -> list[PreflightFailure]:
    """Build a partial registry from the new plugin set + service interfaces.

    Then run ``KnowledgeBaseOverlayLoader.apply`` on it to verify every
    process has its companion knowledge-base JSON (catches missing JSON
    stubs — bug class #6/#7) and the post-merge customizations contract
    holds across the merged set (§2.3 territory per Coordinator review
    Finding 2).
    """
    failures: list[PreflightFailure] = []

    registry = _build_partial_registry(plugin_instances)

    overlay = KnowledgeBaseOverlayLoader(
        plugin_manager=_StubPluginManager(plugin_instances),  # type: ignore[arg-type]
        schema_generator=InvocationSchemaGenerator(),
    )
    try:
        overlay.apply(registry)
    except Exception as exc:  # noqa: BLE001
        failures.append(PreflightFailure(
            check="L1.2_kb_overlay",
            plugin=None,
            message=str(exc),
            error_class=type(exc).__name__,
        ))

    try:
        PluginRegistrationValidator().validate_all_embedding_descriptions(registry)
    except Exception as exc:  # noqa: BLE001
        failures.append(PreflightFailure(
            check="L1.3_merge_validation",
            plugin=None,
            message=str(exc),
            error_class=type(exc).__name__,
        ))

    return failures


class _StubPluginManager:
    """Minimal plugin_manager surface for KnowledgeBaseOverlayLoader path resolution."""

    def __init__(self, plugins: Mapping[str, PluginBase]) -> None:
        self.plugins: dict[str, PluginBase] = dict(plugins)


def _build_partial_registry(
    plugin_instances: Mapping[str, PluginBase],
) -> dict[str, object]:
    """Build a ``{processes: {...}}`` registry shape covering every process the
    new manifest's plugins would contribute, plus every static
    ``@service_interface_process`` registration the loaded service-interface
    modules carry. The registry is tiny relative to the live one but
    structurally compatible with the kb_overlay loader.
    """
    processes: dict[str, dict[str, Any]] = {}
    _merge_plugin_processes(plugin_instances, processes)
    _merge_service_interface_processes(processes)
    return {"processes": processes}


def _merge_plugin_processes(
    plugin_instances: Mapping[str, PluginBase],
    processes: dict[str, dict[str, Any]],
) -> None:
    for plugin_name, instance in plugin_instances.items():
        try:
            actions = instance.get_available_actions()
        except Exception:  # noqa: BLE001
            continue
        for action in actions:
            key = f"plugin::{plugin_name}::{action.name}"
            processes[key] = _entry_from_action(
                provider_type="plugin",
                provider=plugin_name,
                action=action,
            )


def _merge_service_interface_processes(
    processes: dict[str, dict[str, Any]],
) -> None:
    """Scan every ``ananta.services.*.interfaces.public`` module that's loaded
    and emit a registry entry for each ``@service_interface_process``-decorated
    method. The decorator attaches metadata to the method object via a
    ``_service_interface_metadata`` attribute (see
    ``core/services/service_interface_decorator.py``).
    """
    try:
        services_pkg = importlib.import_module(_SERVICES_PACKAGE)
    except Exception:  # noqa: BLE001
        return
    for _, mod_name, ispkg in pkgutil.iter_modules(
        services_pkg.__path__, prefix=f"{_SERVICES_PACKAGE}.",
    ):
        if not ispkg:
            continue
        try:
            module = importlib.import_module(
                f"{mod_name}{_INTERFACES_PUBLIC_SUFFIX}",
            )
        except ModuleNotFoundError:
            continue
        except Exception:  # noqa: BLE001
            continue
        for _attr_name, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue
            _emit_service_interface_entries(obj, processes)


def _emit_service_interface_entries(
    cls: type, processes: dict[str, dict[str, Any]],
) -> None:
    for _name, member in inspect.getmembers(cls):
        metadata = getattr(member, "_service_interface_metadata", None)
        if not isinstance(metadata, ServiceInterfaceActionMetadata):
            continue
        if not is_service_interface_process_enabled(metadata):
            continue
        key = f"service_interface::{metadata.provider}::{metadata.name}"
        processes[key] = {
            "provider_type": "service_interface",
            "provider": metadata.provider,
            "function_name": metadata.name,
            "name": metadata.name,
            "is_discoverable": metadata.is_discoverable,
            "processor_policy_category": (
                metadata.processor_policy_category.value
                if metadata.processor_policy_category is not None
                else None
            ),
            "result_processor_customizations": (
                metadata.result_processor_customizations
            ),
            "error_processor_customizations": (
                metadata.error_processor_customizations
            ),
        }


def _entry_from_action(
    *, provider_type: str, provider: str, action: ActionMetadata,
) -> dict[str, Any]:
    policy = getattr(action, "processor_policy_category", None)
    return {
        "provider_type": provider_type,
        "provider": provider,
        "function_name": action.name,
        "name": action.name,
        "is_discoverable": getattr(action, "is_discoverable", True),
        "processor_policy_category": policy.value if policy is not None else None,
        "result_processor_customizations": getattr(
            action, "result_processor_customizations", None,
        ),
        "error_processor_customizations": getattr(
            action, "error_processor_customizations", None,
        ),
    }


# ─── Top-level orchestrator ────────────────────────────────────────────────


def run_manifest_preflight(
    new_manifest: Mapping[str, Any],
) -> PreflightResult:
    """Run §2.1 + §2.2 + §2.3 against the new manifest. Aggregate failures.

    Each check runs to completion; one plugin's failure does NOT short-
    circuit the remaining checks on other plugins. The result carries the
    union of failures so the operator sees every issue in one pass.
    """
    result = PreflightResult()
    plugins = _extract_plugin_names(new_manifest)
    plugin_classes, import_failures = check_imports(plugins)
    result.failures.extend(import_failures)

    plugin_instances, instantiation_failures = check_instantiation_and_decorators(
        plugin_classes,
    )
    result.failures.extend(instantiation_failures)

    overlay_failures = check_kb_overlay_and_collisions(plugin_instances)
    result.failures.extend(overlay_failures)

    return result


def _extract_plugin_names(new_manifest: Mapping[str, Any]) -> tuple[str, ...]:
    plugins = new_manifest.get("plugins")
    if not isinstance(plugins, list):
        return ()
    return tuple(p for p in plugins if isinstance(p, str) and p)


def format_failure_reasons(result: PreflightResult) -> list[str]:
    """Render a flat ``rejection_reasons`` list for the apply_manifest envelope."""
    return [
        f"{failure.check}: "
        f"{failure.plugin or 'platform'}: "
        f"{failure.error_class}: {failure.message}"
        for failure in result.failures
    ]


__all__ = [
    "ENTRY_POINT_MISSING_ERROR_CLASS",
    "PreflightFailure",
    "PreflightResult",
    "check_imports",
    "check_instantiation_and_decorators",
    "check_kb_overlay_and_collisions",
    "format_failure_reasons",
    "run_manifest_preflight",
]
