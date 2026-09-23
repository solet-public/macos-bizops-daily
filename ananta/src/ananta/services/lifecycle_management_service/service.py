"""Lifecycle Management Service Implementation.

Provides runtime service lifecycle operations - starting and stopping services dynamically.
"""

import importlib
import importlib.metadata
import logging
import site
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml

from ananta.core.config.config_manager import ConfigManager
from ananta.core.domain.enums import ActionStatus
from ananta.core.plugins.capabilities import is_lifecycle_managed
from ananta.core.plugins.plugin_base import PluginBase
from ananta.core.plugins.plugin_installer import (
    PluginInstallError,
    collect_process_keys,
)
from ananta.services.lifecycle_management_service.binding_validator import (
    ValidationResult,
    validate_bindings_satisfied,
)
from ananta.services.lifecycle_management_service.interfaces.public import (
    LIST_PLUGINS_FILTER_VALUES,
    PLATFORM_CONFIG_TAKES_EFFECT_NEXT_RESTART,
    LifecycleManagementAPI,
)
from ananta.services.lifecycle_management_service.manifest_preflight import (
    ENTRY_POINT_MISSING_ERROR_CLASS,
    PreflightResult,
    format_failure_reasons,
    run_manifest_preflight,
)
from ananta.services.lifecycle_management_service.manifest_writer import (
    CurrentManifestState,
    ManifestDiff,
    ManifestPartialWriteError,
    ManifestPreconditionFailedError,
    ManifestWriteOutcome,
    diff_manifest,
    read_current_manifest_state,
    restore_previous_manifest,
    write_new_manifest,
)
from ananta.services.lifecycle_management_service.plugin_config_io import (
    PLATFORM_CONFIG_ALLOWLIST,
    diff_config_keys,
    is_scope_key_allowlisted,
    merge_platform_scope,
    read_platform_config_file,
    read_plugin_config_file,
    write_platform_config_file,
    write_plugin_config_file,
)
from ananta.services.lifecycle_management_service.plugin_inventory import (
    as_response_rows,
    enumerate_available_plugins,
)

_RELOAD_SAFE_ATTR = "RELOAD_SAFE"

_FILTER_ENABLED = "enabled"
_FILTER_LOADED = "loaded"
_FILTER_LIFECYCLE_MANAGED = "lifecycle_managed"

_LIST_FILTER_PREDICATES: dict[str, Callable[[dict[str, Any]], bool]] = {
    _FILTER_ENABLED: lambda row: bool(row["enabled"]),
    _FILTER_LOADED: lambda row: row["status"] != "uninitialized",
    _FILTER_LIFECYCLE_MANAGED: lambda row: bool(row["lifecycle_managed"]),
}

# Platform-config (scope, key) pairs the service can apply without a restart.
# Pairs that are allowlisted but absent here are persisted with
# restart_required=True so the caller knows when to reboot.
_PLATFORM_CONFIG_IMMEDIATE_APPLIERS: dict[tuple[str, str], str] = {
    ("logging", "log_level"): "_apply_log_level",
}

_PLUGIN_INSTALL_MARKERS: tuple[str, ...] = ("plugin.yaml", "pyproject.toml")

logger = logging.getLogger(__name__)


# Deployment-plugin restart statuses that confirm the new manifest is in
# effect (or will be once the async cutover completes). Anything outside
# this set means the restart did NOT take — apply_manifest must surface
# the failure instead of reporting ``status="applied"``. See Codex review
# #2 Finding 1 (workbench/2026-05-30_self_deployment_plugin_codex_review.md).
# The reachable values are the members of ``RestartStatus`` (queued /
# completed / failed / needs_intervention); ``"cutover_complete"`` was
# removed 2026-06-12 per workbench/2026-06-12_bug4_apply_manifest_cross_host_
# swap_engagement.md §3.1 + §5 AC #3 — the AWS wrapper now maps the
# deploy_self happy-path token onto ``RestartStatus.QUEUED`` before the
# value reaches this frozenset. ``needs_intervention`` (added for the local
# durable-rollback verb; a forward cutover can also reach it when a failed
# swap's router rollback does not take) is intentionally OUTSIDE this set:
# this is a deny-list (anything not present = "did NOT apply the manifest"),
# NOT an exhaustive match, so a new member surfaces as a non-applied failure
# automatically — no branch here needs to enumerate it.
_RESTART_STATUSES_THAT_APPLIED_THE_MANIFEST: frozenset[str] = frozenset({
    "queued",       # cloud: blue-green cutover scheduled, finisher action enqueued
    "completed",    # synchronous restart finished — rare, only future in-process impls
})

# Sentinel returned by the bound ``self_deployment_service`` plugin
# (currently ``macos_self_deployment_plugin``) when its L2 probe
# rejects the manifest. ``_commit_apply_manifest`` rolls the on-disk
# manifest back to the prior bytes (Architect's local blue/green
# design Coordinator review Finding 3) and surfaces the probe detail
# in the rejection envelope. Distinct from generic ``"failed"`` so
# the operator can tell pre-flight rejection from post-commit
# infrastructure failure.
_RESTART_STATUS_PROBE_FAILED = "probe_failed"

# Keys forbidden in v1 ``new_manifest`` payloads per Architect §15.1: full
# bundle externalization (bindings + per-plugin configs) is a v2 concern.
# Passing ``plugin_config_overrides`` is still rejected before any disk
# write so the cloud restart path's manifest-only S3 upload never desyncs
# the per-plugin configs (Codex review #2 Finding 2).
#
# v1.1: ``service_bindings`` IS now accepted as a caller-supplied rebind
# directive — required for plugin renames where the new manifest no
# longer lists the old provider. Caller-supplied bindings are atomically
# written alongside the manifest and validated against the new plugin
# list by the existing binding-validator pre-flight.
_V1_FORBIDDEN_MANIFEST_KEYS: tuple[str, ...] = (
    "plugin_config_overrides",
)


def _diff_to_dict(diff: ManifestDiff) -> dict[str, list[str]]:
    """Render a :class:`ManifestDiff` into the response-shape dict."""
    return {
        "added_plugins": list(diff.added_plugins),
        "removed_plugins": list(diff.removed_plugins),
        "rebound_services": list(diff.rebound_services),
    }


def _probe_rejection_reasons(restart: dict[str, Any]) -> list[str]:
    """Render the ``rejection_reasons`` list for a probe-failed envelope."""
    reasons = [
        f"probe_failed: deployment plugin's L2 probe rejected the "
        f"manifest before any restart fired: {restart['message']}"
    ]
    probe_payload = restart.get("probe")
    if isinstance(probe_payload, dict):
        reasons.append(
            f"probe.failing_step={probe_payload.get('failing_step')!r}; "
            f"probe.error_class={probe_payload.get('error_class')!r}; "
            f"probe.detail={probe_payload.get('detail')!r}"
        )
    return reasons


def _format_binding_reasons(result: ValidationResult) -> list[str]:
    """Render a binding-validator result into a list of operator-readable reasons."""
    reasons: list[str] = []
    for miss in result.missing_bindings:
        if miss.replacement is None:
            reasons.append(
                f"required_service_unbound: '{miss.service}' is not present "
                f"in new_manifest['service_bindings'] "
                f"(current provider: {miss.current_provider or 'none'})"
            )
        else:
            reasons.append(
                f"binding_provider_missing: '{miss.service}' is bound to "
                f"'{miss.replacement}' but that plugin is not in "
                f"new_manifest['plugins']"
            )
    return reasons


class LifecycleManagementService(LifecycleManagementAPI):
    """Service for managing lifecycle of plugins and services at runtime.

    Implements dynamic service start/stop operations for managing
    service plugins during orchestrator runtime.
    """

    def __init__(
        self,
        orchestrator_ref: Any,
        plugin_config: dict[str, Any] | None = None,
    ) -> None:
        """Initialize lifecycle management service.

        Args:
            orchestrator_ref: Reference to the event orchestrator
            plugin_config: Optional plugin configuration
        """
        self._orchestrator = orchestrator_ref
        self._config = plugin_config or {}
        logger.debug("LifecycleManagementService initialized")

    def start_service_via_interface(
        self,
        service: dict[str, Any] | str,
        start: str | None = None,  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a service plugin with specified configuration."""
        try:
            config = config or {}
            extracted_name = self._extract_service_name(service)

            if not extracted_name:
                return self._error_result(
                    "service name is required (provide string or dict with 'name' key)"
                )

            plugin = self._get_service_plugin(extracted_name)
            if isinstance(plugin, dict):
                return plugin  # Error result

            if self._is_already_running(plugin, extracted_name):
                return self._already_running_result(extracted_name)

            self._start_plugin(plugin, extracted_name)

            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {
                    "success": True,
                    "service_name": extracted_name,
                    "message": f"Service '{extracted_name}' started successfully",
                    "config": config,
                },
                "actions": [],
            }

        except Exception as e:
            logger.error(f"Error starting service: {e}", exc_info=True)
            return self._error_result(f"Failed to start service: {str(e)}")

    def _extract_service_name(self, service: dict[str, Any] | str) -> str | None:
        """Extract service name from various input formats."""
        if isinstance(service, dict):
            return service.get("name")
        return service

    def _get_service_plugin(self, name: str) -> Any | dict[str, Any]:
        """Get and validate service plugin. Returns plugin or error dict."""
        if not hasattr(self._orchestrator, "plugin_manager"):
            return self._error_result("Plugin manager not available")

        plugin = self._orchestrator.plugin_manager.plugins.get(name)
        if not plugin:
            return self._error_result(f"Plugin '{name}' not found")

        if not (hasattr(plugin, "is_service_plugin") and plugin.is_service_plugin):
            return self._error_result(f"Plugin '{name}' is not a service plugin")

        return plugin

    def _is_already_running(self, plugin: Any, name: str) -> bool:
        """Check if plugin is already running."""
        if hasattr(plugin, "is_ready") and plugin.is_ready():
            logger.debug(f"Service '{name}' is already running")
            return True
        return False

    def _already_running_result(self, name: str) -> dict[str, Any]:
        """Build result for already-running service."""
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "success": True,
                "service_name": name,
                "message": f"Service '{name}' is already running",
                "already_running": True,
            },
            "actions": [],
        }

    def _start_plugin(self, plugin: Any, name: str) -> None:
        """Start the plugin through its lifecycle methods."""
        logger.debug(f"Starting service: {name}")

        if hasattr(plugin, "prepare_for_readiness"):
            plugin.prepare_for_readiness()

        if hasattr(plugin, "start_services"):
            plugin.start_services()

        if hasattr(plugin, "is_ready") and not plugin.is_ready():
            if hasattr(plugin, "set_ready"):
                plugin.set_ready()

        logger.debug(f"✅ Service '{name}' started successfully")

    def _error_result(self, error_msg: str) -> dict[str, Any]:
        """Build standard error result."""
        return {
            "action_status": ActionStatus.ERROR.value,
            "error": error_msg,
            "data": None,
            "actions": [],
        }

    def stop_service(self, service: str) -> dict[str, Any]:
        """Stop a running service."""
        try:
            if not service:
                return self._error_result("service name is required")

            plugin = self._get_service_plugin(service)
            if isinstance(plugin, dict):
                return plugin  # Error result

            if self._is_already_stopped(plugin, service):
                return self._already_stopped_result(service)

            self._stop_plugin(plugin, service)

            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {
                    "success": True,
                    "service_name": service,
                    "message": f"Service '{service}' stopped successfully",
                },
                "actions": [],
            }

        except Exception as e:
            logger.error(f"Error stopping service: {e}", exc_info=True)
            return self._error_result(f"Failed to stop service: {str(e)}")

    def _is_already_stopped(self, plugin: Any, name: str) -> bool:
        """Check if plugin is already stopped."""
        if hasattr(plugin, "is_ready") and not plugin.is_ready():
            logger.debug(f"Service '{name}' is already stopped")
            return True
        return False

    def _already_stopped_result(self, name: str) -> dict[str, Any]:
        """Build result for already-stopped service."""
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "success": True,
                "service_name": name,
                "message": f"Service '{name}' is already stopped",
                "already_stopped": True,
            },
            "actions": [],
        }

    def _stop_plugin(self, plugin: Any, name: str) -> None:
        """Stop the plugin through its lifecycle methods."""
        logger.debug(f"Stopping service: {name}")

        if hasattr(plugin, "stop_services"):
            plugin.stop_services()

        logger.debug(f"✅ Service '{name}' stopped successfully")

    def reload_python_module(self, module_name: str) -> dict[str, Any]:
        """Reload a Python module marked RELOAD_SAFE = True; refuse unmarked ones."""
        try:
            if not module_name:
                return self._error_result("module_name is required")

            module = sys.modules.get(module_name)
            if module is None:
                return self._error_result(
                    f"Module '{module_name}' is not currently loaded; "
                    "only modules already imported into sys.modules can be reloaded"
                )

            if not getattr(module, _RELOAD_SAFE_ATTR, False):
                return self._error_result(
                    f"Module '{module_name}' is not marked RELOAD_SAFE; "
                    "add `RELOAD_SAFE = True` at module top level before reloading. "
                    "Stateful modules MUST NOT be marked safe."
                )

            importlib.reload(module)
            logger.info(f"Reloaded module: {module_name}")

            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {
                    "success": True,
                    "module_name": module_name,
                    "message": f"Module '{module_name}' reloaded successfully",
                },
                "actions": [],
            }

        except Exception as e:
            logger.error(
                f"Error reloading module {module_name}: {e}", exc_info=True
            )
            return self._error_result(
                f"Failed to reload module '{module_name}': {e}"
            )

    # ------------------------------------------------------------------
    # Plugin lifecycle introspection / reregistration (D1, D2)
    # ------------------------------------------------------------------

    def list_plugins(self, filter: str | None = None) -> dict[str, Any]:  # noqa: A002 - public contract
        """Return the live plugin roster with status / config metadata."""
        try:
            if filter is not None and filter not in LIST_PLUGINS_FILTER_VALUES:
                return self._error_result(
                    f"filter must be one of {list(LIST_PLUGINS_FILTER_VALUES)} or omitted; "
                    f"got {filter!r}"
                )

            plugin_manager = self._require_plugin_manager()
            if isinstance(plugin_manager, dict):
                return plugin_manager  # Error result

            rows: list[dict[str, Any]] = [
                self._build_plugin_row(name, plugin)
                for name, plugin in plugin_manager.plugins.items()
            ]
            filtered_rows = self._apply_list_filter(rows, filter)

            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"plugins": filtered_rows},
                "actions": [],
            }

        except Exception as e:
            logger.error(f"Error listing plugins: {e}", exc_info=True)
            return self._error_result(f"Failed to list plugins: {e}")

    def list_available_plugins(self) -> dict[str, Any]:
        """Enumerate plugins that could be loaded into the next manifest.

        Walks ``importlib.metadata.entry_points(group="ananta.plugins")``
        for installed plugins and the repo's ``plugins/`` directory for
        source-tree candidates not yet installed. See
        :mod:`ananta.services.lifecycle_management_service.plugin_inventory`
        for the enumeration semantics.
        """
        try:
            rows = as_response_rows(enumerate_available_plugins())
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"plugins": rows},
                "actions": [],
            }
        except Exception as e:
            logger.error(f"Error listing available plugins: {e}", exc_info=True)
            return self._error_result(f"Failed to list available plugins: {e}")

    # ------------------------------------------------------------------
    # Internal helpers for D1 / D2
    # ------------------------------------------------------------------

    def _require_plugin_manager(self) -> Any:
        """Return the orchestrator's plugin manager, or an error dict."""
        if not hasattr(self._orchestrator, "plugin_manager"):
            return self._error_result("Plugin manager not available on orchestrator")
        plugin_manager = self._orchestrator.plugin_manager
        if plugin_manager is None:
            return self._error_result("Plugin manager not initialised on orchestrator")
        return plugin_manager

    @staticmethod
    def _is_lifecycle_managed(plugin: PluginBase) -> bool:
        """Boolean wrapper around the lifecycle TypeGuard.

        Strips the ``TypeGuard[LifecycleManaged]`` return so calling this does
        not widen the caller's ``plugin`` reference to the union of PluginBase
        and LifecycleManaged.
        """
        return is_lifecycle_managed(plugin)

    def _build_plugin_row(self, name: str, plugin: PluginBase) -> dict[str, Any]:
        """Materialise a single roster row for ``list_plugins``."""
        config = self._get_plugin_config(name)
        enabled = bool(config.get("enabled", True))
        priority = self._resolve_priority(name, config)
        lifecycle_managed = self._is_lifecycle_managed(plugin)
        is_running = lifecycle_managed and bool(plugin.is_ready())
        readiness = plugin.readiness_state.value
        last_error = plugin.readiness_error
        process_count = len(plugin.get_available_actions())
        version = self._resolve_version(plugin, config)

        row: dict[str, Any] = {
            "name": name,
            "version": version,
            "status": readiness,
            "enabled": enabled,
            "priority": priority,
            "process_count": process_count,
            "lifecycle_managed": lifecycle_managed,
            "is_running": is_running,
        }
        if last_error:
            row["last_error"] = last_error
        return row

    def _resolve_version(self, plugin: PluginBase, config: dict[str, Any]) -> str:
        """Pick the most specific version known for a plugin."""
        config_version = config.get("version")
        if isinstance(config_version, str) and config_version:
            return config_version
        instance_version = getattr(plugin, "version", None)
        if isinstance(instance_version, str) and instance_version:
            return instance_version
        defaults = plugin.get_default_config()
        default_version = defaults.get("version")
        if isinstance(default_version, str) and default_version:
            return default_version
        return "unknown"

    def _resolve_priority(self, plugin_name: str, config: dict[str, Any]) -> int:
        """Resolve a plugin's load priority from config, falling back to defaults."""
        config_priority = config.get("priority")
        if isinstance(config_priority, int):
            return config_priority
        plugin_manager = self._orchestrator.plugin_manager
        config_manager = getattr(self._orchestrator, "config", None)
        return int(plugin_manager._discovery._get_plugin_priority(plugin_name, config_manager))

    def _get_plugin_config(self, plugin_name: str) -> dict[str, Any]:
        """Read a plugin's resolved config dict, returning an empty dict on absence."""
        config_manager = getattr(self._orchestrator, "config", None)
        if config_manager is None or not hasattr(config_manager, "get_plugin_config"):
            return {}
        return dict(config_manager.get_plugin_config(plugin_name))

    def _apply_list_filter(
        self, rows: list[dict[str, Any]], filter_value: str | None
    ) -> list[dict[str, Any]]:
        """Filter the materialised roster after every row exists."""
        if filter_value is None:
            return rows
        predicate = _LIST_FILTER_PREDICATES.get(filter_value)
        if predicate is None:
            return rows
        return [row for row in rows if predicate(row)]

    def _stop_lifecycle_plugin_if_running(
        self, plugin: PluginBase, plugin_name: str
    ) -> None:
        """Stop services on a LifecycleManaged plugin that is currently ready."""
        if not is_lifecycle_managed(plugin):
            return
        # is_ready() lives on PluginBase; reach for it through a separate cast
        # so mypy does not collapse the variable type to LifecycleManaged.
        if not cast(PluginBase, plugin).is_ready():
            return
        logger.debug(f"Stopping services for '{plugin_name}' before reregistration")
        plugin.stop_services()

    def _start_lifecycle_plugin_if_managed(
        self, plugin: PluginBase, plugin_name: str
    ) -> None:
        """Run the lifecycle start sequence on a freshly re-instantiated plugin."""
        plugin.prepare_for_readiness()
        if is_lifecycle_managed(plugin):
            logger.debug(f"Starting services for reregistered plugin '{plugin_name}'")
            plugin.start_services()
        if not plugin.is_ready():
            plugin.set_ready()

    def _refresh_registry_for_plugin(
        self, plugin_name: str, *, strict: bool = False,
    ) -> None:
        """Delegate to ``knowledge_service::refresh_plugin_processes`` if available.

        ``strict`` (install path only): fail-closed when the knowledge_service
        is missing/unusable OR when the refresh REPORTS errors in its return
        value — either way the process keys were NOT registered, so a success
        envelope would be a false claim. ``refresh_plugin_processes`` merges the
        plugin's process-JSON text fields into EXISTING registry entries; a
        NEWLY installed plugin's keys are not yet in the registry, so the merge
        returns ``{"status": "partial", "errors": [...]}`` WITHOUT raising. The
        pre-fix code ignored that return and reported install success while the
        registry actually rejected the new keys. In strict mode we raise so the
        caller unwinds the committed install rather than claiming a
        registration that did not happen. (Truly registering new keys needs the
        full registry-entry build — decorator schema + JSON — that boot
        performs; runtime add is a blue-green ``apply_manifest`` operation. See
        the C1a build report; deeper runtime-register support is a follow-up.)

        The default (``strict=False``) preserves the exact prior behavior for
        the enable/disable callers, which refresh EXISTING entries.
        """
        knowledge_service = self._orchestrator.get_service("knowledge_service")
        if knowledge_service is None or not hasattr(
            knowledge_service, "refresh_plugin_processes"
        ):
            if strict:
                # A degraded / miswired orchestrator with no usable
                # knowledge_service cannot register the new plugin's process
                # keys. Fail closed rather than fall through to a success
                # envelope that claims a registration that never happened.
                raise RuntimeError(
                    f"knowledge_service is unavailable; cannot register the "
                    f"process keys for {plugin_name!r} — runtime install fails "
                    f"closed rather than report an unregistered success"
                )
            logger.warning(
                "knowledge_service unavailable; plugin '%s' registry was not refreshed",
                plugin_name,
            )
            return
        result = knowledge_service.refresh_plugin_processes(plugin_name=plugin_name)
        if strict and isinstance(result, dict) and result.get("errors"):
            raise RuntimeError(
                f"process registry refresh for {plugin_name!r} reported "
                f"error(s): {result['errors']}"
            )

    # ------------------------------------------------------------------
    # Plugin enable/disable, priority, config (D3, D4, D5)
    # ------------------------------------------------------------------

    def set_plugin_enabled(
        self, plugin_name: str, enabled: bool
    ) -> dict[str, Any]:
        """Toggle a plugin's enabled state, persisting and applying live."""
        try:
            if not plugin_name:
                return self._error_result("plugin_name is required")

            plugin_manager = self._require_plugin_manager()
            if isinstance(plugin_manager, dict):
                return plugin_manager
            config_manager = self._require_config_manager()
            if isinstance(config_manager, dict):
                return config_manager

            self._persist_plugin_config_field(
                config_manager, plugin_name, "enabled", enabled,
            )

            if enabled:
                return self._apply_plugin_enable(plugin_manager, plugin_name)
            return self._apply_plugin_disable(plugin_manager, plugin_name)

        except Exception as e:
            logger.error(
                f"Error toggling plugin '{plugin_name}' enabled={enabled}: {e}",
                exc_info=True,
            )
            return self._error_result(
                f"Failed to set plugin '{plugin_name}' enabled={enabled}: {e}"
            )

    def set_plugin_priority(
        self, plugin_name: str, priority: int
    ) -> dict[str, Any]:
        """Persist a plugin's load priority; takes effect on the next start."""
        try:
            if not plugin_name:
                return self._error_result("plugin_name is required")

            config_manager = self._require_config_manager()
            if isinstance(config_manager, dict):
                return config_manager

            self._persist_plugin_config_field(
                config_manager, plugin_name, "priority", priority,
            )

            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {
                    "applied": True,
                    "takes_effect": PLATFORM_CONFIG_TAKES_EFFECT_NEXT_RESTART,
                    "plugin_name": plugin_name,
                    "message": (
                        f"Plugin '{plugin_name}' priority set to {priority}; "
                        "takes effect on the next solet restart"
                    ),
                },
                "actions": [],
            }

        except Exception as e:
            logger.error(
                f"Error setting plugin '{plugin_name}' priority={priority}: {e}",
                exc_info=True,
            )
            return self._error_result(
                f"Failed to set plugin '{plugin_name}' priority={priority}: {e}"
            )

    def reload_plugin_config(self, plugin_name: str) -> dict[str, Any]:
        """Re-read a plugin's config and push the new view into the plugin."""
        try:
            if not plugin_name:
                return self._error_result("plugin_name is required")

            plugin_manager = self._require_plugin_manager()
            if isinstance(plugin_manager, dict):
                return plugin_manager
            config_manager = self._require_config_manager()
            if isinstance(config_manager, dict):
                return config_manager

            plugin = plugin_manager.plugins.get(plugin_name)
            if plugin is None:
                return self._error_result(
                    f"Plugin '{plugin_name}' is not loaded; nothing to reload"
                )

            previous_config = dict(
                config_manager._plugin_configs.get(plugin_name, {})
            )
            new_config = read_plugin_config_file(config_manager, plugin_name)
            config_manager._plugin_configs[plugin_name] = dict(new_config)
            dirty_keys = diff_config_keys(previous_config, new_config)

            if hasattr(plugin, "initialize"):
                plugin.initialize(cast(dict[str, object], dict(new_config)))

            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {
                    "success": True,
                    "plugin_name": plugin_name,
                    "dirty_keys": dirty_keys,
                    "message": (
                        f"Plugin '{plugin_name}' config reloaded "
                        f"({len(dirty_keys)} key(s) changed)"
                    ),
                },
                "actions": [],
            }

        except Exception as e:
            logger.error(
                f"Error reloading config for plugin '{plugin_name}': {e}",
                exc_info=True,
            )
            return self._error_result(
                f"Failed to reload plugin '{plugin_name}' config: {e}"
            )

    # ------------------------------------------------------------------
    # Platform config + plugin installation (D6, D7)
    # ------------------------------------------------------------------

    def update_platform_config(
        self, scope: str, key: str, value: Any
    ) -> dict[str, Any]:
        """Persist a single platform-level config entry and apply it live where possible."""
        try:
            if not scope:
                return self._error_result("scope is required")
            if not key:
                return self._error_result("key is required")
            if not is_scope_key_allowlisted(scope, key):
                return self._error_result(
                    f"(scope={scope!r}, key={key!r}) is not in the platform-config "
                    f"allowlist; permitted entries: {PLATFORM_CONFIG_ALLOWLIST}"
                )

            config_manager = self._require_config_manager()
            if isinstance(config_manager, dict):
                return config_manager

            document = read_platform_config_file(config_manager)
            previous_value = merge_platform_scope(document, scope, key, value)
            write_platform_config_file(config_manager, document)

            applier_name = _PLATFORM_CONFIG_IMMEDIATE_APPLIERS.get((scope, key))
            if applier_name is None:
                return self._platform_update_result(
                    scope=scope, key=key, previous_value=previous_value,
                    restart_required=True,
                    detail="persisted; takes effect on next solet restart",
                )

            applier = cast(
                Callable[[Any], None], getattr(self, applier_name),
            )
            applier(value)
            return self._platform_update_result(
                scope=scope, key=key, previous_value=previous_value,
                restart_required=False,
                detail="persisted and applied live",
            )

        except Exception as e:
            logger.error(
                f"Error updating platform config {scope}.{key}={value!r}: {e}",
                exc_info=True,
            )
            return self._error_result(
                f"Failed to update platform config {scope}.{key}: {e}"
            )

    def install_plugin_from_path(self, path: str) -> dict[str, Any]:
        """Install a plugin from a local source directory at runtime.

        Stage-then-atomic-commit via ``plugin_manager.installer``: pip-install
        the artifact, then discover → instantiate → contract-validate → wire
        the new plugin OFF to the side and commit it with a single atomic
        roster insert. Any failure after a successful pip install rolls the
        pip artifact back and leaves the live roster, allowlist, and every
        pre-existing plugin instance byte-identical (the C1 atomicity
        contract). A failed install can no longer clear-and-rebuild the roster
        and zombify every live plugin.

        Caller-serialization assumption (unchanged, but shrunk): the platform
        action processor sequences verb calls; the only remaining read-modify-
        write on shared state is the installer's allowlist add, and the commit
        itself is a single dict store safe against concurrent roster readers.
        """
        try:
            resolved = self._validate_install_source(path)
            if isinstance(resolved, dict):
                return resolved
            source, new_plugin_name = resolved

            plugin_manager = self._require_plugin_manager()
            if isinstance(plugin_manager, dict):
                return plugin_manager

            # Pre-pip fail-closed guard: pip must NOT run for a name that
            # already exists in this environment. Otherwise a later staged /
            # wiring / strict-refresh failure hits the rollback
            # `pip uninstall <name>`, which removes the PRE-EXISTING
            # distribution by name — and there is no way to restore it, so the
            # failed install is not byte-identical (the g_suite / connector
            # incident shape). Guard on BOTH the live roster (loaded) AND the
            # installed entry-point set (an installed-but-NOT-loaded plugin —
            # e.g. an inert connector awaiting a blue-green boot — is absent
            # from the roster but present in importlib.metadata). Route to
            # apply_manifest without touching pip. (Full Q1 deprecation —
            # dead-branch deletion + KB rewrite — is C1b.)
            if new_plugin_name in plugin_manager.plugins:
                return self._error_result(
                    f"plugin {new_plugin_name!r} is already loaded; runtime "
                    f"re-install cannot replace a live instance — use "
                    f"apply_manifest (blue-green) for code pickup"
                )
            if new_plugin_name in self._installed_plugin_entry_point_names():
                return self._error_result(
                    f"plugin {new_plugin_name!r} is already installed in this "
                    f"environment (entry-point present) but not loaded; runtime "
                    f"install cannot safely replace an existing distribution — "
                    f"use apply_manifest (blue-green) to activate it"
                )

            try:
                self._run_pip_install_editable(source)
            except Exception as exc:
                # pip failed → there is no artifact to roll back. (Hardening
                # the pip boundary into a dedicated import_plugin verb is
                # tracked separately; this keeps the failure honest today.)
                logger.error(
                    f"pip install for plugin from path {path!r} failed: {exc}",
                    exc_info=True,
                )
                return self._error_result(
                    f"Failed to install plugin from path {path!r}: "
                    f"pip install failed: {exc}"
                )

            return self._stage_commit_or_rollback(
                plugin_manager, source, new_plugin_name, path,
            )

        except Exception as e:
            logger.error(
                f"Error installing plugin from path {path!r}: {e}", exc_info=True,
            )
            return self._error_result(
                f"Failed to install plugin from path {path!r}: {e}"
            )

    def _stage_commit_or_rollback(
        self, plugin_manager: Any, source: Path, new_plugin_name: str, path: str,
    ) -> dict[str, Any]:
        """Post-pip: cache-invalidate → staged atomic install → registry refresh.

        Any failure after the successful pip install rolls the pip artifact
        back and returns an error envelope, so the live roster is never left
        mutated by a failed install. A ``PluginInstallError`` from the staged
        install names the failing phase; a post-commit registry-refresh
        failure unwinds the committed install atomically.
        """
        try:
            self._invalidate_importlib_caches()
            plugin = plugin_manager.installer.install(
                new_plugin_name,
                wire=lambda p: self._wire_plugin_instance(plugin_manager, p),
            )
        except PluginInstallError as exc:
            self._pip_uninstall_safe(new_plugin_name)
            return self._error_result(
                f"Failed to install plugin from path {path!r} at phase "
                f"{exc.phase}: {exc}; pip artifact rolled back"
            )
        except Exception as exc:
            # cache-invalidation (or any other post-pip failure): roll the pip
            # artifact back so a later launch does not pick up a half-install.
            self._pip_uninstall_safe(new_plugin_name)
            return self._error_result(
                f"Failed to install plugin from path {path!r}: {exc}; "
                f"pip artifact rolled back"
            )

        try:
            # Collect keys INSIDE the try (RIDER-2): a get_available_actions
            # raise on the committed instance must also unwind, not escape to
            # the outer catch-all leaving the install committed-but-unwound.
            new_process_keys = self._collect_plugin_process_keys(plugin)
            self._refresh_registry_for_plugin(new_plugin_name, strict=True)
        except Exception as exc:
            teardown_detail = self._unwind_committed_install(
                plugin_manager, new_plugin_name,
            )
            if teardown_detail is None:
                return self._error_result(
                    f"install of {new_plugin_name!r} rolled back: "
                    f"registry refresh failed: {exc}"
                )
            # Roster + allowlist ARE clean (robust remove), but stop/de-register
            # was partial — report honestly instead of a definitive rollback.
            return self._error_result(
                f"install of {new_plugin_name!r} FAILED (registry refresh failed: "
                f"{exc}) and rollback was PARTIAL: {teardown_detail}; a restart is "
                f"recommended to clear residual state"
            )
        return self._install_success_envelope(
            new_plugin_name, source, new_process_keys,
        )

    def _installed_plugin_entry_point_names(self) -> set[str]:
        """Names of ``ananta.plugins`` entry points currently installed in this venv.

        Invalidates the metadata cache first so the pre-pip guard sees the true
        on-disk install state (a plugin installed by a prior operation or
        another session into the shared venv), not a stale snapshot — a
        false-negative here would let pip run and a later rollback uninstall the
        pre-existing distribution.
        """
        importlib.metadata.MetadataPathFinder.invalidate_caches()
        return {
            ep.name
            for ep in importlib.metadata.entry_points().select(group="ananta.plugins")
        }

    def _validate_install_source(
        self, path: str,
    ) -> tuple[Path, str] | dict[str, Any]:
        """Validate the install path + resolve the new plugin's name.

        Returns the resolved ``(source, plugin_name)`` pair on success, or
        an error envelope on any precondition violation (empty path,
        non-directory, missing install marker, unresolvable plugin name).
        """
        if not path:
            return self._error_result("path is required")
        source = Path(path).expanduser().resolve()
        if not source.exists() or not source.is_dir():
            return self._error_result(
                f"path {source} does not exist or is not a directory"
            )
        if not any((source / marker).exists() for marker in _PLUGIN_INSTALL_MARKERS):
            return self._error_result(
                f"path {source} does not contain any of {_PLUGIN_INSTALL_MARKERS}; "
                "refusing to install"
            )
        new_plugin_name = self._resolve_plugin_name_from_source(source)
        if not new_plugin_name:
            return self._error_result(
                f"could not resolve plugin entry-point name from "
                f"pyproject.toml [project.entry-points.\"ananta.plugins\"] "
                f"or plugin.yaml at {source}"
            )
        return source, new_plugin_name

    def _install_success_envelope(
        self, plugin_name: str, source: Path, process_keys: list[str],
    ) -> dict[str, Any]:
        """Build the canonical ``install_plugin_from_path`` success envelope."""
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "installed": True,
                "plugin_name": plugin_name,
                "new_process_keys": process_keys,
                "message": (
                    f"Installed plugin '{plugin_name}' from {source}; "
                    f"{len(process_keys)} process key(s) registered"
                ),
            },
            "actions": [],
        }

    # ------------------------------------------------------------------
    # apply_manifest (Coordinator dispatch 2026-05-30, Architect §4.2)
    # ------------------------------------------------------------------

    def apply_manifest(
        self,
        new_manifest: dict[str, Any],
        reason: str,
        expected_etag: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Validate, atomically write, and delegate restart with a new manifest."""
        try:
            if not reason:
                return self._error_result("reason is required")

            app_home = self._resolve_app_home()
            if isinstance(app_home, dict):
                return app_home

            current = read_current_manifest_state(app_home)
            synthesized = self._v1_synthesize_manifest(new_manifest, current)
            if "rejection_envelope" in synthesized:
                return synthesized["rejection_envelope"]
            effective_manifest = synthesized["effective_manifest"]
            rejection, deferred_findings = self._preflight_apply_manifest(
                effective_manifest, current,
            )
            if rejection is not None:
                return rejection

            try:
                diff = diff_manifest(current, effective_manifest)
            except ValueError as exc:
                return self._reject_apply_manifest(
                    current_etag=current.etag,
                    reasons=[str(exc)],
                    message=f"new_manifest shape invalid: {exc}",
                )

            if dry_run:
                return self._dry_run_apply_manifest(
                    current=current, diff=diff, deferred_findings=deferred_findings,
                )

            return self._commit_apply_manifest(
                app_home=app_home,
                current=current,
                new_manifest=effective_manifest,
                diff=diff,
                expected_etag=expected_etag,
                reason=reason,
                deferred_findings=deferred_findings,
            )
        except Exception as exc:
            logger.error(
                f"apply_manifest crashed (reason={reason!r}): {exc}",
                exc_info=True,
            )
            return self._error_result(f"apply_manifest failed: {exc}")

    # ------------------------------------------------------------------
    # apply_manifest internal helpers
    # ------------------------------------------------------------------

    def _resolve_app_home(self) -> Path | dict[str, Any]:
        """Return ``Path(orchestrator.APP_HOME)`` or an error dict."""
        app_home_str = getattr(self._orchestrator, "APP_HOME", None)
        if app_home_str is None:
            return self._error_result(
                "orchestrator.APP_HOME is not set; "
                "apply_manifest cannot locate the runtime config directory"
            )
        return Path(app_home_str)

    def _v1_synthesize_manifest(
        self,
        new_manifest: dict[str, Any],
        current: CurrentManifestState,
    ) -> dict[str, Any]:
        """Narrow ``new_manifest`` to v1 scope per Architect §15.1.

        v1 ``apply_manifest`` accepts ``{plugins: list[str], profile_name?:
        str, service_bindings?: dict[str, str]}``. ``plugin_config_overrides``
        stays deferred to v2 (full-bundle externalization). Caller-supplied
        ``service_bindings`` are required for plugin renames where the new
        manifest no longer lists the old provider — the binding-validator
        pre-flight rejects plugin-list-only renames as ``binding_provider_missing``.

        The function returns one of two shapes:

        - ``{"effective_manifest": <dict>}`` — caller-supplied ``plugins`` +
          ``service_bindings`` (caller's if provided, else current snapshot)
          + optional ``profile_name``. Downstream validation / diff / write
          functions consume the effective manifest unchanged.
        - ``{"rejection_envelope": <dict>}`` — the canonical reject envelope
          when ``plugin_config_overrides`` is present (still v2 territory).
        """
        forbidden = tuple(
            key for key in _V1_FORBIDDEN_MANIFEST_KEYS if key in new_manifest
        )
        if forbidden:
            return {
                "rejection_envelope": self._reject_apply_manifest(
                    current_etag=current.etag,
                    reasons=[
                        f"forbidden_v1_keys_present: "
                        f"new_manifest contains {sorted(forbidden)!r}; v1 "
                        "apply_manifest accepts {plugins, profile_name?, "
                        "service_bindings?} only. plugin_config_overrides "
                        "is deferred to v2."
                    ],
                    message=(
                        "Pre-flight rejection: v1.1 apply_manifest accepts "
                        "{plugins, profile_name?, service_bindings?}; "
                        "plugin_config_overrides remains operator-tooling "
                        "territory until v2. See workbench/2026-05-30_"
                        "plugin_lifecycle_architect_pass.md §15.1 + "
                        "workbench/2026-06-07_tier_3_w_vault_local_keychain_brief.md §1.1."
                    ),
                ),
            }
        caller_bindings = new_manifest.get("service_bindings")
        effective_bindings: dict[str, str]
        if caller_bindings is not None:
            if not isinstance(caller_bindings, dict):
                return {
                    "rejection_envelope": self._reject_apply_manifest(
                        current_etag=current.etag,
                        reasons=[
                            "service_bindings_shape_invalid: "
                            f"new_manifest['service_bindings'] must be a dict; "
                            f"got {type(caller_bindings).__name__}"
                        ],
                        message=(
                            "Pre-flight rejection: service_bindings must be a "
                            "mapping of service_name -> provider_plugin_name."
                        ),
                    ),
                }
            effective_bindings = {**current.service_bindings, **caller_bindings}
        else:
            effective_bindings = dict(current.service_bindings)
        effective_manifest: dict[str, Any] = {
            "plugins": new_manifest.get("plugins"),
            "service_bindings": effective_bindings,
        }
        profile_name = new_manifest.get("profile_name")
        if profile_name is not None:
            effective_manifest["profile_name"] = profile_name
        return {"effective_manifest": effective_manifest}

    def _preflight_apply_manifest(
        self,
        new_manifest: dict[str, Any],
        current: CurrentManifestState,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Run pre-flight gates; return ``(rejection_envelope | None, deferred)``.

        Order:

        1. **Binding satisfaction** (existing) — every bound provider in the new
           manifest must be in the new plugin list.
        2. **L1.1 import + L1.2 instantiate/decorator + L1.3 kb_overlay** — the
           three new checks landed by the local blue/green design §2. They are
           grouped in :func:`manifest_preflight.run_manifest_preflight`; a
           single rejection envelope carries every failure so the operator
           sees the full set in one pass.

        GTE-06 A2 — deferral is decided HERE, at the L1 call site only:
        ``EntryPointMissingError`` findings do NOT reject (the live
        process's ``importlib.metadata`` scan can be stale for post-boot
        installs — the L1.1 boot-stale false-reject). They are returned as
        ``deferred`` reasons, surfaced on the dry-run/applied envelopes,
        and decided authoritatively by the L2 fresh-source probe.
        ``run_manifest_preflight`` itself stays context-free and fully
        rejecting, so the probe context still rejects a genuinely-missing
        entry point.
        """
        try:
            result = validate_bindings_satisfied(
                new_manifest=new_manifest,
                current_bindings=current.service_bindings,
            )
        except ValueError as exc:
            return self._reject_apply_manifest(
                current_etag=current.etag,
                reasons=[str(exc)],
                message=f"new_manifest shape invalid: {exc}",
            ), []
        if not result.satisfied:
            return self._reject_apply_manifest(
                current_etag=current.etag,
                reasons=_format_binding_reasons(result),
                message=(
                    "Pre-flight validation rejected the manifest; see "
                    "rejection_reasons for details."
                ),
            ), []

        preflight = run_manifest_preflight(new_manifest)
        deferred = [
            failure for failure in preflight.failures
            if failure.error_class == ENTRY_POINT_MISSING_ERROR_CLASS
        ]
        rejecting = [
            failure for failure in preflight.failures
            if failure.error_class != ENTRY_POINT_MISSING_ERROR_CLASS
        ]
        deferred_reasons = format_failure_reasons(
            PreflightResult(failures=deferred),
        )
        if deferred_reasons:
            logger.warning(
                "L1 preflight deferred %d EntryPointMissingError finding(s) "
                "to the L2 fresh-source probe (live-process importlib.metadata "
                "can be stale for post-boot installs): %s",
                len(deferred_reasons),
                "; ".join(deferred_reasons),
            )
        if not rejecting:
            return None, deferred_reasons
        return self._reject_apply_manifest(
            current_etag=current.etag,
            reasons=format_failure_reasons(PreflightResult(failures=rejecting)),
            message=(
                "Pre-flight import/decorator/kb-overlay checks rejected the "
                "manifest; see rejection_reasons for the per-plugin failures."
            ),
        ), deferred_reasons

    def _dry_run_apply_manifest(
        self,
        *,
        current: CurrentManifestState,
        diff: ManifestDiff,
        deferred_findings: list[str],
    ) -> dict[str, Any]:
        """Return the dry-run envelope without writing or restarting."""
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "status": "dry_run",
                "diff": _diff_to_dict(diff),
                "current_etag": current.etag,
                "preflight_deferred": list(deferred_findings),
                "message": (
                    "Dry run: manifest validated and diff computed; no "
                    "writes performed. Pass current_etag back as "
                    "expected_etag on a follow-up call to commit."
                ),
            },
            "actions": [],
        }

    def _commit_apply_manifest(
        self,
        *,
        app_home: Path,
        current: CurrentManifestState,
        new_manifest: dict[str, Any],
        diff: ManifestDiff,
        expected_etag: str | None,
        reason: str,
        deferred_findings: list[str],
    ) -> dict[str, Any]:
        """Atomic write + delegate restart; return the applied envelope.

        Tier 3 §1.1 rollback contract: ``write_new_manifest`` writes
        ``service_bindings.json`` first then ``manifest.yaml``. If the
        second write throws (OSError / disk full / interrupted) after
        the first succeeded, the on-disk state has new bindings + old
        manifest — exactly the kind of divergence the Tier 2 sub-2
        complete-swap incident framing warned about. The
        :class:`ManifestPartialWriteError` catch below restores both
        files from the CAS-time ``pre_write_state`` snapshot the writer
        actually validated (GTE-06 T4 — NOT the entry-time ``current``
        read, which can be stale under concurrency) and returns a
        structured error envelope. CAS protection on the next
        apply_manifest call still holds because the etag computation
        depends on both files' bytes.
        """
        effective_etag = expected_etag if expected_etag is not None else current.etag
        try:
            outcome = write_new_manifest(
                app_home=app_home,
                new_manifest=new_manifest,
                expected_etag=effective_etag,
                profile_name=str(new_manifest.get("profile_name") or "local"),
            )
        except ManifestPreconditionFailedError as cas_fail:
            return self._reject_apply_manifest(
                current_etag=cas_fail.current_etag,
                reasons=[
                    "precondition_failed: "
                    f"expected etag {cas_fail.expected_etag!r} but on-disk "
                    f"etag is {cas_fail.current_etag!r}"
                ],
                message=(
                    "CAS check failed; re-read current state via "
                    "dry_run=true and re-submit with the fresh etag."
                ),
            )
        except ValueError as exc:
            return self._reject_apply_manifest(
                current_etag=current.etag,
                reasons=[str(exc)],
                message=f"new_manifest shape invalid: {exc}",
            )
        except ManifestPartialWriteError as exc:
            return self._partial_write_rollback_envelope(
                app_home=app_home, current=current, exc=exc,
            )

        restart = self._delegate_restart(new_manifest=new_manifest, reason=reason)
        restart_status = restart["status"]
        if restart_status == _RESTART_STATUS_PROBE_FAILED:
            return self._probe_failed_rollback_envelope(
                app_home=app_home,
                current=current,
                diff=diff,
                restart=restart,
                outcome=outcome,
            )
        if restart_status not in _RESTART_STATUSES_THAT_APPLIED_THE_MANIFEST:
            return self._restart_failed_after_manifest_commit_envelope(
                app_home=app_home,
                diff=diff,
                current=current,
                restart=restart,
                outcome=outcome,
            )
        data: dict[str, Any] = {
            "status": "applied",
            "diff": _diff_to_dict(diff),
            "current_etag": current.etag,
            "new_etag": outcome.new_etag,
            "manifest_written_to": str(outcome.manifest_path),
            "service_bindings_written_to": str(outcome.bindings_path),
            "restart_action_id": restart["restart_action_id"],
            "restart_status": restart_status,
            "preflight_deferred": list(deferred_findings),
            "message": restart["message"],
        }
        probe_evidence = restart.get("probe")
        if isinstance(probe_evidence, dict):
            # Q5: probe success evidence rides the applied envelope — the
            # live verify sequence reads it as the positive proof the L2
            # probe executed on the green path.
            data["probe"] = probe_evidence
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": data,
            "actions": [],
        }

    def _partial_write_rollback_envelope(
        self,
        *,
        app_home: Path,
        current: CurrentManifestState,
        exc: ManifestPartialWriteError,
    ) -> dict[str, Any]:
        """Restore the CAS-time snapshot after a mid-flight write failure (T4)."""
        try:
            restore_previous_manifest(
                app_home,
                manifest_bytes=exc.pre_write_state.manifest_bytes,
                bindings_bytes=exc.pre_write_state.bindings_bytes,
            )
        except OSError as restore_exc:
            logger.error(
                "apply_manifest partial-write rollback FAILED: %s; "
                "manifest + bindings may be divergent on disk",
                restore_exc,
                exc_info=True,
            )
            return self._reject_apply_manifest(
                current_etag=current.etag,
                reasons=[
                    "partial_write_rollback_failed: original write failed with "
                    f"{exc.original!r}; restore-previous-manifest also failed with "
                    f"{restore_exc!r}. On-disk manifest + bindings may be divergent; "
                    "operator must inspect both files and restore by hand from a "
                    "known-good source (e.g. profile/config/ in the source repo)."
                ],
                message=(
                    "apply_manifest partial-write rollback FAILED; on-disk "
                    "state may be inconsistent. See rejection_reasons."
                ),
            )
        return self._reject_apply_manifest(
            current_etag=current.etag,
            reasons=[
                "partial_write_failed: "
                f"write_new_manifest raised {exc.original!r}; previous manifest + "
                "bindings have been restored from the CAS-time pre-write snapshot."
            ],
            message=(
                "apply_manifest write failed mid-flight; on-disk state "
                "was restored to the CAS-time pre-write snapshot."
            ),
        )

    def _restart_failed_after_manifest_commit_envelope(
        self,
        *,
        app_home: Path,
        diff: ManifestDiff,
        current: CurrentManifestState,
        restart: dict[str, Any],
        outcome: ManifestWriteOutcome,
    ) -> dict[str, Any]:
        """Restore a failed restart's manifest commit and build its envelope.

        Codex review #2 Finding 1: when the manifest write succeeds but
        ``self_deployment_service::restart_with_manifest`` cannot schedule the
        cutover, the running color still has the old in-memory config but
        the on-disk manifest would otherwise point future boots at the new
        shape. Restore the CAS-time snapshot just as the probe-failure path
        does. The etag guard avoids overwriting another caller's acknowledged
        manifest commit during the restart window.
        """
        on_disk = read_current_manifest_state(app_home)
        if on_disk.etag != outcome.new_etag:
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {
                    "status": "restart_failed_after_manifest_commit",
                    "diff": _diff_to_dict(diff),
                    "current_etag": current.etag,
                    "new_etag": outcome.new_etag,
                    "on_disk_etag": on_disk.etag,
                    "manifest_written_to": str(outcome.manifest_path),
                    "service_bindings_written_to": str(outcome.bindings_path),
                    "restart_action_id": restart["restart_action_id"],
                    "restart_status": restart["status"],
                    "rejection_reasons": [
                        "restart_failed_after_manifest_commit: deployment plugin "
                        f"returned status={restart['status']!r}: {restart['message']}",
                        "manifest_rollback_skipped: on-disk manifest changed after "
                        "this flow wrote it; refusing to overwrite a concurrent commit",
                    ],
                    "message": (
                        "Deployment restart failed after this flow wrote the manifest, "
                        "but the on-disk manifest changed before rollback. It was not "
                        "overwritten; inspect the current commit before retrying."
                    ),
                },
                "actions": [],
            }
        try:
            bindings_path, manifest_path = restore_previous_manifest(
                app_home,
                manifest_bytes=outcome.pre_write_state.manifest_bytes,
                bindings_bytes=outcome.pre_write_state.bindings_bytes,
            )
        except OSError as restore_exc:
            logger.exception("Restart-failed rollback could not restore prior manifest")
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {
                    "status": "restart_failed_after_manifest_commit",
                    "diff": _diff_to_dict(diff),
                    "current_etag": current.etag,
                    "new_etag": outcome.new_etag,
                    "manifest_written_to": str(outcome.manifest_path),
                    "service_bindings_written_to": str(outcome.bindings_path),
                    "restart_action_id": restart["restart_action_id"],
                    "restart_status": restart["status"],
                    "rejection_reasons": [
                        "restart_failed_after_manifest_commit: deployment plugin "
                        f"returned status={restart['status']!r}: {restart['message']}",
                        "manifest_rollback_failed: "
                        f"{type(restore_exc).__name__}: {restore_exc}",
                    ],
                    "message": (
                        "Deployment restart failed and restoration of the pre-swap "
                        "manifest also failed. The running color remains unchanged; "
                        "operator intervention is required before a future boot."
                    ),
                },
                "actions": [],
            }
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "status": "restart_failed_after_manifest_commit",
                "diff": _diff_to_dict(diff),
                "current_etag": current.etag,
                "new_etag": outcome.new_etag,
                "manifest_written_to": str(outcome.manifest_path),
                "service_bindings_written_to": str(outcome.bindings_path),
                "manifest_restored_to": str(manifest_path),
                "service_bindings_restored_to": str(bindings_path),
                "restart_action_id": restart["restart_action_id"],
                "restart_status": restart["status"],
                "rejection_reasons": [
                    f"restart_failed_after_manifest_commit: deployment plugin "
                    f"returned status={restart['status']!r}: {restart['message']}",
                    "manifest_rolled_back: restored the CAS-time pre-write snapshot",
                ],
                "message": (
                    "Deployment restart failed after the manifest write. The running "
                    "color remains on the old config and the on-disk profile manifest "
                    "was restored to its CAS-time pre-write snapshot. Resolve the "
                    "underlying deploy failure and re-invoke apply_manifest."
                ),
            },
            "actions": [],
        }

    def _probe_failed_rollback_envelope(
        self,
        *,
        app_home: Path,
        current: CurrentManifestState,
        diff: ManifestDiff,
        restart: dict[str, Any],
        outcome: ManifestWriteOutcome,
    ) -> dict[str, Any]:
        """Restore the prior on-disk manifest and surface the probe failure.

        Per Architect's local blue/green design + Coordinator review
        Finding 3: when the L2 probe rejects the just-committed
        manifest, the new bytes on disk must NOT persist — otherwise a
        later boot would blindly adopt the un-validated manifest (the
        startup path runs no probe). The rollback restores
        ``manifest.yaml`` + ``service_bindings.json`` from the CAS-time
        ``pre_write_state`` snapshot (T4) via
        :func:`restore_previous_manifest`; both writes are temp+rename
        so the on-disk state never observes a half-restored shape.

        GTE-06 A1 — the restore is CAS-GUARDED: nothing mutexes the
        apply path, so during the build+probe window a second caller can
        legitimately commit over the bytes THIS flow wrote. The restore
        therefore re-reads the on-disk etag and proceeds ONLY when it
        still equals ``outcome.new_etag`` (the bytes this flow wrote);
        on mismatch it does NOT restore — the on-disk manifest is
        someone else's acknowledged commit — and returns the loud
        ``probe_failed_manifest_changed_during_probe`` envelope instead.
        """
        on_disk = read_current_manifest_state(app_home)
        if on_disk.etag != outcome.new_etag:
            return self._probe_failed_but_manifest_changed_envelope(
                current=current,
                diff=diff,
                restart=restart,
                outcome=outcome,
                on_disk_etag=on_disk.etag,
            )
        try:
            bindings_path, manifest_path = restore_previous_manifest(
                app_home,
                manifest_bytes=outcome.pre_write_state.manifest_bytes,
                bindings_bytes=outcome.pre_write_state.bindings_bytes,
            )
            rollback_succeeded = True
            rollback_detail = "on-disk manifest restored to prior state."
        except OSError as exc:
            logger.exception("Probe-failed rollback could not restore prior manifest")
            bindings_path = app_home / Path("config") / "service_bindings.json"
            manifest_path = app_home / Path("config") / "manifest.yaml"
            rollback_succeeded = False
            rollback_detail = (
                f"PROBE-FAILED ROLLBACK ALSO FAILED ({type(exc).__name__}: "
                f"{exc}); the on-disk manifest at {manifest_path} now reflects "
                "the unvalidated proposal. Operator MUST manually restore."
            )
        probe_payload = restart.get("probe")
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "status": (
                    "probe_failed_manifest_rolled_back"
                    if rollback_succeeded
                    else "probe_failed_rollback_failed"
                ),
                "diff": _diff_to_dict(diff),
                "current_etag": current.etag,
                "manifest_path": str(manifest_path),
                "service_bindings_path": str(bindings_path),
                "restart_action_id": str(restart.get("restart_action_id") or ""),
                "restart_status": str(restart.get("status") or ""),
                "probe": probe_payload if isinstance(probe_payload, dict) else {},
                "rejection_reasons": _probe_rejection_reasons(restart),
                "message": (
                    f"L2 probe rejected the manifest BEFORE any restart fired; "
                    f"{rollback_detail} The live the solet is untouched. See "
                    "rejection_reasons + probe for the failing-step detail."
                ),
            },
            "actions": [],
        }

    def _probe_failed_but_manifest_changed_envelope(
        self,
        *,
        current: CurrentManifestState,
        diff: ManifestDiff,
        restart: dict[str, Any],
        outcome: ManifestWriteOutcome,
        on_disk_etag: str,
    ) -> dict[str, Any]:
        """A1: probe rejected, but the on-disk manifest is no longer ours.

        A concurrent ``apply_manifest`` committed during this flow's
        build+probe window. Restoring OUR pre-write snapshot would
        silently STOMP that acknowledged commit, so nothing is restored;
        the operator gets a loud envelope naming both etags.
        """
        probe_payload = restart.get("probe")
        logger.error(
            "L2 probe rejected the manifest, but the on-disk manifest changed "
            "during the probe window (wrote etag %s, on-disk etag %s) — NOT "
            "rolling back over a concurrent commit.",
            outcome.new_etag,
            on_disk_etag,
        )
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "status": "probe_failed_manifest_changed_during_probe",
                "diff": _diff_to_dict(diff),
                "current_etag": current.etag,
                "written_etag": outcome.new_etag,
                "on_disk_etag": on_disk_etag,
                "restart_action_id": str(restart.get("restart_action_id") or ""),
                "restart_status": str(restart.get("status") or ""),
                "probe": probe_payload if isinstance(probe_payload, dict) else {},
                "rejection_reasons": _probe_rejection_reasons(restart),
                "message": (
                    "L2 probe rejected this flow's manifest, but the on-disk "
                    "manifest changed during the probe window (a concurrent "
                    "apply_manifest committed). NOT rolling back — the on-disk "
                    "bytes are someone else's acknowledged commit. This flow's "
                    "proposal was NOT deployed; the live the solet is untouched."
                ),
            },
            "actions": [],
        }

    def _delegate_restart(
        self,
        *,
        new_manifest: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """Invoke the bound ``self_deployment_service`` plugin's restart_with_manifest.

        Returns a dict with ``status``, ``restart_action_id``, and
        ``message`` keys (plus optional ``probe`` payload on
        ``probe_failed`` from the bound plugin's L2 probe).
        ``status`` is the plugin's own status — one of
        ``"queued"`` (async cutover scheduled — happy path), ``"completed"``
        (synchronous restart finished), or ``"failed"`` (restart could not
        be scheduled). Per Codex review #2 Finding 1, the caller MUST
        propagate ``status="failed"`` into the ``apply_manifest`` envelope;
        treating a failed restart as ``"applied"`` leaves a partial remote
        commit (manifest on disk / in S3, running color unchanged) with
        no durable recovery handle.

        Three additional internal sentinel statuses cover the
        delegation-layer failures the deployment plugin never sees:

        - ``"unbound"`` — no plugin is bound to ``self_deployment_service`` on
          this solet. The manifest is on disk but the caller will
          need a manual restart.
        - ``"plugin_protocol_error"`` — bound plugin returned a non-dict
          from ``restart_with_manifest``. Contract violation; treat as
          failed.
        - ``"plugin_raised"`` — bound plugin raised an unhandled
          exception. Logged via ``logger.exception``; treat as failed.
        """
        try:
            plugin = self._orchestrator.get_service("self_deployment_service")
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "get_service('self_deployment_service') raised %s; treating as unbound",
                exc,
            )
            plugin = None
        if plugin is None or not hasattr(plugin, "restart_with_manifest"):
            return {
                "status": "unbound",
                "restart_action_id": "",
                "message": (
                    "Manifest written but no plugin is bound to "
                    "self_deployment_service on this solet; restart "
                    "manually via ./launch.py to pick up the new "
                    f"manifest. Reason: {reason}"
                ),
            }
        try:
            result = plugin.restart_with_manifest(
                new_manifest=new_manifest,
                expected_etag="",
                reason=reason,
                dry_run=False,
            )
        except Exception as exc:
            logger.exception(
                "Bound self-deployment plugin raised during restart_with_manifest",
            )
            return {
                "status": "plugin_raised",
                "restart_action_id": "",
                "message": (
                    f"self-deployment plugin raised during restart_with_manifest: "
                    f"{type(exc).__name__}: {exc}. Manifest written locally; "
                    "the running color is unchanged. Resolve the underlying "
                    "plugin error and re-invoke apply_manifest."
                ),
            }
        # The plugin contract returns a RestartResult dataclass. We
        # destructure into the legacy dict envelope the caller already
        # consumes; the dict carries the same six fields the new
        # RestartResult guarantees populated. Plugin protocol violations
        # (non-RestartResult returns) raise AttributeError below and the
        # delegate envelope surfaces a typed protocol_error.
        try:
            delegated: dict[str, Any] = {
                "status": result.status.value,
                "restart_action_id": result.restart_action_id,
                "message": (
                    result.message
                    or f"self-deployment plugin scheduled restart (reason: {reason})"
                ),
            }
            if result.probe is not None:
                # GTE-06: carry the L2 probe payload — the PROBE_FAILED
                # rejection detail, or the Q5 success evidence on QUEUED.
                delegated["probe"] = result.probe
        except AttributeError:
            return {
                "status": "plugin_protocol_error",
                "restart_action_id": "",
                "message": (
                    "self-deployment plugin returned a non-RestartResult from "
                    f"restart_with_manifest: {type(result).__name__}"
                ),
            }
        return delegated

    def _reject_apply_manifest(
        self,
        *,
        current_etag: str,
        reasons: list[str],
        message: str,
    ) -> dict[str, Any]:
        """Return the canonical rejection envelope for apply_manifest."""
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "status": "rejected",
                "current_etag": current_etag,
                "rejection_reasons": list(reasons),
                "message": message,
            },
            "actions": [],
        }

    # ------------------------------------------------------------------
    # Internal helpers for D3 / D4 / D5 / D6 / D7
    # ------------------------------------------------------------------

    def _require_config_manager(self) -> ConfigManager | dict[str, Any]:
        """Return the orchestrator's ConfigManager, or an error dict."""
        config_manager = getattr(self._orchestrator, "config", None)
        if config_manager is None:
            return self._error_result("config manager not available on orchestrator")
        if not isinstance(config_manager, ConfigManager):
            return self._error_result(
                f"orchestrator.config is not a ConfigManager "
                f"(got {type(config_manager).__name__})"
            )
        return config_manager

    def _persist_plugin_config_field(
        self,
        config_manager: ConfigManager,
        plugin_name: str,
        field: str,
        value: Any,
    ) -> None:
        """Merge ``field=value`` into the per-plugin config and write to disk."""
        document = read_plugin_config_file(config_manager, plugin_name)
        document[field] = value
        write_plugin_config_file(config_manager, plugin_name, document)

    def _apply_plugin_enable(
        self, plugin_manager: Any, plugin_name: str
    ) -> dict[str, Any]:
        """Enable a plugin live: load if absent; start if LifecycleManaged."""
        plugin = plugin_manager.plugins.get(plugin_name)
        if plugin is None:
            return self._install_plugin_for_enable(plugin_manager, plugin_name)

        self._start_lifecycle_plugin_if_managed(plugin, plugin_name)
        self._refresh_registry_for_plugin(plugin_name)
        return self._enable_result(
            plugin_name, applied=True, restart_required=False,
            detail="enabled and active in the live roster",
        )

    def _install_plugin_for_enable(
        self, plugin_manager: Any, plugin_name: str
    ) -> dict[str, Any]:
        """Load a not-yet-loaded plugin via the atomic installer (C1b).

        Stages, wires (``_wire_plugin_instance`` — the same boot-order replay
        used by ``install_plugin_from_path``), and commits with a single
        atomic dict store. Unlike the legacy ``_rediscover_plugins`` path,
        this never touches any other roster entry: a failure here leaves the
        live roster, allowlist, and every pre-existing plugin instance
        byte-identical.

        ``phase="staging_discovery"`` (entry-point genuinely not installed,
        OR excluded by the solet's profile manifest allowlist) is the
        ONLY ``restart_required=True`` case — same wording as before, so the
        card's contract does not change shape. Every other phase
        (``contract_validation``, ``wiring``) reports an error with the
        platform unchanged, instead of the pre-fix silent roster-wide brick.

        The manifest check runs BEFORE the installer call: ``installer.install``
        stages discovery scoped to ``{plugin_name}`` alone (it has no notion
        of the manifest), so an entry-point that exists on disk but is
        deliberately excluded from this solet's profile would otherwise
        load anyway and get unioned into the live allowlist — silently
        overriding the manifest's exclusion. Pre-fix, ``_rediscover_plugins``
        re-ran full discovery scoped to the real ``_allowed_plugins``, so a
        manifest-excluded plugin never appeared and this case fell through to
        the same "not found" envelope; this check preserves that.
        """
        allowed_plugins = plugin_manager._allowed_plugins  # noqa: SLF001
        if allowed_plugins is not None and plugin_name not in allowed_plugins:
            return self._entry_point_not_found_result(plugin_name)

        try:
            plugin_manager.installer.install(
                plugin_name,
                wire=lambda p: self._wire_plugin_instance(plugin_manager, p),
            )
        except PluginInstallError as exc:
            if exc.phase == "staging_discovery":
                return self._entry_point_not_found_result(plugin_name)
            return self._error_result(
                f"Failed to enable plugin {plugin_name!r} at phase {exc.phase}: "
                f"{exc}; live roster unchanged"
            )

        self._refresh_registry_for_plugin(plugin_name)
        return self._enable_result(
            plugin_name, applied=True, restart_required=False,
            detail="enabled and active in the live roster",
        )

    def _entry_point_not_found_result(self, plugin_name: str) -> dict[str, Any]:
        """The one ``restart_required=True`` envelope for a not-yet-loadable enable."""
        return self._enable_result(
            plugin_name, applied=False, restart_required=True,
            detail=(
                "enabled=true persisted but the plugin's entry-point was not "
                "found via discovery; install or restart to load it"
            ),
        )

    def _apply_plugin_disable(
        self, plugin_manager: Any, plugin_name: str
    ) -> dict[str, Any]:
        """Disable a plugin live: stop services, drop from roster, refresh registry."""
        plugin = plugin_manager.plugins.get(plugin_name)
        if plugin is None:
            return self._enable_result(
                plugin_name, applied=True, restart_required=False,
                detail="enabled=false persisted; plugin was not in the live roster",
            )

        self._stop_lifecycle_plugin_if_running(plugin, plugin_name)
        plugin_manager.plugins.pop(plugin_name, None)
        self._refresh_registry_for_plugin(plugin_name)
        return self._enable_result(
            plugin_name, applied=True, restart_required=False,
            detail="disabled and removed from the live roster",
        )

    def _enable_result(
        self,
        plugin_name: str,
        *,
        applied: bool,
        restart_required: bool,
        detail: str,
    ) -> dict[str, Any]:
        """Build the canonical ``set_plugin_enabled`` envelope."""
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "applied": applied,
                "restart_required": restart_required,
                "plugin_name": plugin_name,
                "message": f"Plugin '{plugin_name}': {detail}",
            },
            "actions": [],
        }

    def _platform_update_result(
        self,
        *,
        scope: str,
        key: str,
        previous_value: Any,
        restart_required: bool,
        detail: str,
    ) -> dict[str, Any]:
        """Build the canonical ``update_platform_config`` envelope."""
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "applied": True,
                "prev_value": previous_value,
                "restart_required": restart_required,
                "scope": scope,
                "key": key,
                "message": f"platform.{scope}.{key} {detail}",
            },
            "actions": [],
        }

    def _apply_log_level(self, value: Any) -> None:
        """Apply ``logging.log_level`` to the root logger in-process."""
        if not isinstance(value, str):
            raise TypeError(
                f"logging.log_level must be a string; got {type(value).__name__}"
            )
        level_name = value.upper()
        level_map = logging.getLevelNamesMapping()
        if level_name not in level_map:
            raise ValueError(
                f"logging.log_level={value!r} does not resolve to a numeric level"
            )
        logging.getLogger().setLevel(level_map[level_name])
        logger.info("Root logger level set to %s via update_platform_config", level_name)

    def _run_pip_install_editable(self, source: Path) -> None:
        """Run ``pip install -e <source>`` against the active interpreter."""
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(source)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"pip install -e {source} failed (exit {completed.returncode}): "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        logger.info("Installed plugin source %s via pip install -e", source)

    def _invalidate_importlib_caches(self) -> None:
        """Reset import + metadata caches AND refresh sys.path after pip install.

        Three things have to happen for the new plugin to be reachable
        in-session after ``pip install -e <source>``:

        1. ``importlib.invalidate_caches()`` — drop the finder-side cache
           so newly-written ``.dist-info/`` directories become visible.
        2. ``importlib.metadata.MetadataPathFinder.invalidate_caches()`` —
           drop the metadata-side ``EntryPoints`` snapshot so
           ``entry_points(group="ananta.plugins")`` re-reads the
           filesystem (ER-12 Bug-B).
        3. ``site.addsitedir`` for each site-packages directory — process
           the newly-written editable ``.pth`` files so the source
           directory lands on ``sys.path``. Without this step the entry
           point's metadata is visible but ``import_module`` raises
           ``ModuleNotFoundError`` because the running interpreter's
           ``sys.path`` still reflects startup state. The design memo
           §5 (i) missed this layer; the F3 smoke surfaced it
           empirically (PEP 660 editable installs ship a startup-time
           ``.pth`` that ``invalidate_caches`` does not touch).

        ``addsitedir`` is idempotent against ``sys.path`` (CPython's
        internal ``known_paths`` set prevents duplicate appends) but does
        re-process every ``.pth`` file under each site-packages directory
        on each call — O(N·M) work bounded by typical solet size
        (~3 site-packages dirs × ~50 .pth files). ``sys.path`` mutation
        is not thread-safe; callers must serialize ``install_plugin_from_path``
        invocations (the platform action processor does so today).
        """
        importlib.invalidate_caches()
        importlib.metadata.MetadataPathFinder.invalidate_caches()
        for path in site.getsitepackages():
            site.addsitedir(path)

    def _resolve_plugin_name_from_source(self, source: Path) -> str:
        """Parse the plugin's entry-point name from its source directory.

        Prefers ``pyproject.toml``'s
        ``[project.entry-points."ananta.plugins"]`` block — the lone key
        there is what :class:`PluginDiscovery` matches against. Falls
        back to ``plugin.yaml``'s ``name`` field when pyproject does not
        carry an entry-points block. Returns the empty string when
        neither is resolvable; the caller treats that as a fail-closed
        precondition violation.

        For pyproject sources with multiple ``ananta.plugins`` entries,
        returns the FIRST entry by insertion order. the solet-tree plugins are
        single-entry by convention today (grep across
        ``plugins/*/pyproject.toml``); this resolver does not support
        multi-entry pyprojects.
        """
        pyproject = source / "pyproject.toml"
        if pyproject.is_file():
            with pyproject.open("rb") as fh:
                data = tomllib.load(fh)
            entry_points = (
                (data.get("project") or {})
                .get("entry-points", {})
                .get("ananta.plugins", {})
            )
            if isinstance(entry_points, dict) and entry_points:
                return next(iter(entry_points))

        plugin_yaml = source / "plugin.yaml"
        if plugin_yaml.is_file():
            doc = yaml.safe_load(plugin_yaml.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                name = doc.get("name", "")
                if isinstance(name, str) and name:
                    return name
        return ""

    def _wire_plugin_instance(
        self, plugin_manager: Any, plugin: PluginBase,
    ) -> None:
        """Wire a freshly-staged plugin instance: refs + lifecycle init.

        Supplied as the ``wire`` callback to ``installer.install``; it replays
        the canonical boot order on the STAGED instance (not via the roster):
        ``set_orchestrator_ref`` → ``set_event_bus`` → ``prepare_for_readiness``
        → ``initialize(plugin_config)`` → ``start_services`` (if
        lifecycle-managed) → ``set_ready``. The prepare-before-initialize order
        matters because real plugins request service dependencies in
        ``prepare_for_readiness`` that ``initialize`` then configures —
        initialize-before-prepare would surface as a stateful plugin observing
        missing service handles. A raise here aborts the staged install with
        the live roster byte-identical (this is the g_suite incident's phase).

        Reaches into ``plugin_manager._event_bus_ref`` and
        ``plugin_manager._config_manager`` because exposing them as public
        properties would push PluginManager past the god-class gate's
        non-process-public-methods threshold; the protected access stays
        single-sited here.
        """
        if plugin_manager.orchestrator_ref is not None:
            plugin.set_orchestrator_ref(plugin_manager.orchestrator_ref)
        event_bus = plugin_manager._event_bus_ref  # noqa: SLF001
        if event_bus is not None:
            plugin.set_event_bus(event_bus)

        plugin.prepare_for_readiness()

        config_manager = plugin_manager._config_manager  # noqa: SLF001
        if config_manager is not None and hasattr(plugin, "initialize"):
            plugin_config = config_manager.get_plugin_config(plugin.name, {})
            plugin.initialize(plugin_config)

        if is_lifecycle_managed(plugin):
            plugin.start_services()
        if not plugin.is_ready():
            plugin.set_ready()

    def _unwind_committed_install(
        self, plugin_manager: Any, plugin_name: str,
    ) -> str | None:
        """Roll back a committed install; return None if fully clean, else a detail.

        Used when a POST-commit step (registry refresh) fails: remove the
        plugin from the roster + allowlist via the symmetric
        ``installer.remove`` and roll the pip artifact back — preserving the C1
        contract (error envelope ⇒ platform as-before). ``installer.remove``
        ALWAYS clears the roster + allowlist (best-effort stop + de-register),
        so the pip rollback below can never strand a roster entry on an
        uninstalled distribution. A raise from ``remove`` means the roster IS
        clean but stop/de-register was partial — returned as a detail string so
        the caller reports the partial teardown honestly rather than claiming a
        definitive rollback. The registry is NOT re-refreshed (nothing was
        registered — the refresh is what failed); the de-register prunes any
        partial registry-dict entry idempotently (registry dict only; see
        ``_deregister_process_keys`` — discovery vectors are not pruned, and in
        the unwind path nothing was ever registered anyway).
        """
        teardown_detail: str | None = None
        try:
            plugin_manager.installer.remove(
                plugin_name,
                stop=lambda p: self._stop_lifecycle_plugin_if_running(
                    p, plugin_name,
                ),
                deregister=self._deregister_process_keys,
            )
        except Exception as exc:
            teardown_detail = str(exc)
            logger.warning(
                "partial unwind of committed install %r: %s", plugin_name, exc,
            )
        self._pip_uninstall_safe(plugin_name)
        return teardown_detail

    def _deregister_process_keys(self, process_keys: list[str]) -> None:
        """De-register process keys from the live process-registry dict.

        Reaches the orchestrator's process registry manager:
        ``unregister_dynamic_processes`` deletes the keys from the runtime
        registry dict. It ALSO attempts a discovery-vector purge via
        ``getattr(discovery_service, "remove_process", None)`` — but
        ``DiscoveryService`` currently exposes no ``remove_process`` (only
        ``store_process`` / ``clear_process_vectors`` / ``rebuild_index``), so
        that call is a guarded no-op and the discovery vectors are NOT pruned.
        For C1a this is harmless: the only caller is the install unwind, where
        registration never completed, so nothing is in discovery to strand.
        (The C1b disable path — removing a FULLY-registered plugin — will need a
        real discovery removal: add ``DiscoveryService.remove_process`` or
        rebuild the index. Flagged for rev-3.) No public verb exists for
        de-registration, so the protected access stays single-sited here — it is
        the ``deregister`` mechanism supplied to ``installer.remove``.
        """
        registry_manager = self._orchestrator._process_registry_manager  # noqa: SLF001
        registry_manager.unregister_dynamic_processes(process_keys)

    def _pip_uninstall_safe(self, plugin_name: str) -> None:
        """Roll back a pip install on post-pip validation failure (ER-6).

        Used by ``install_plugin_from_path`` when pip succeeded but the
        plugin failed contract validation during discovery. Subprocess
        failures here are logged but do NOT mask the caller's original
        validation error envelope.

        Pairs the uninstall with a cache invalidation so subsequent
        ``importlib.metadata.entry_points`` calls do not see the
        rolled-back plugin. Without this, the rollback is observable on
        disk but invisible at the metadata layer until the next
        unrelated install or process restart.
        """
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "--yes", plugin_name],
                capture_output=True, text=True, check=False,
            )
        except OSError as exc:
            logger.warning(
                "pip uninstall %s rollback failed to launch: %s",
                plugin_name, exc,
            )
            return
        if completed.returncode != 0:
            logger.warning(
                "pip uninstall %s rollback exited %d: %s",
                plugin_name, completed.returncode,
                completed.stderr.strip() or completed.stdout.strip(),
            )
        self._invalidate_importlib_caches()

    def _collect_plugin_process_keys(self, plugin: PluginBase) -> list[str]:
        """Return the registered process keys exposed by a plugin instance.

        Sorted for a deterministic install success envelope; the shared
        ``collect_process_keys`` (also used by the removal primitive's
        de-register step) owns the ``plugin::<plugin>::<function>`` /
        ``<function>`` formatting so both derive keys identically.
        """
        return sorted(collect_process_keys(plugin))
