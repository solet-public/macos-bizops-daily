"""Plugin lookup + action dispatch + service-provider access enforcement.

Extracted from `PluginManager` during the Step 9.C decomposition
(design record, Step 9.C, dev-checkout workbench — not part of the shipped tree).

Responsibility: serve `get_plugin(name)` lookups and `execute_action(...)`
calls, enforcing that service-provider plugins are accessed only through
their service interfaces (not directly). Frame inspection identifies the
calling code's path against an allowlist of `services/*` paths.

Takes the shared `plugins: dict[str, PluginBase]` registry by reference
plus a callable that returns the current `orchestrator_ref` (late-bound
because the orchestrator can be re-attached via PluginManager's setter
after dispatcher construction).

`PluginManager`'s public delegates capture their own caller frame BEFORE
delegating and pass it through as `caller_frame` so the frame inspection
sees the original external caller and not the manager delegate.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from types import FrameType
from typing import TYPE_CHECKING

from ananta.constants import (
    DEFAULT_BLOB_STORAGE_PLUGIN,
    DEFAULT_EMBEDDING_PLUGIN,
    DEFAULT_INFERENCE_PLUGIN,
    DEFAULT_KNOWLEDGE_PLUGIN,
    DEFAULT_MEMORY_PLUGIN,
    DEFAULT_STATE_MANAGEMENT_PLUGIN,
    DEFAULT_THINKING_PLUGIN,
    DEFAULT_VECTOR_PLUGIN,
)
from ananta.core.domain.enums import ErrorSeverity
from ananta.core.domain.error_codes import ErrorCode
from ananta.core.plugins.plugin_base import OrchestratorProtocol, PluginBase
from ananta.error_handling import FrameworkError, PluginError

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class PluginDispatcher:
    """Look up plugins by name and dispatch actions while enforcing access."""

    def __init__(
        self,
        plugins: dict[str, PluginBase],
        get_orchestrator_ref: Callable[[], OrchestratorProtocol | None],
    ) -> None:
        self._plugins = plugins
        self._get_orchestrator_ref = get_orchestrator_ref

    # ------------------------------------------------------------------
    # Plugin lookup with service-provider access enforcement
    # ------------------------------------------------------------------

    def get_plugin(
        self, plugin_name: str, caller_frame: FrameType | None = None,
    ) -> PluginBase:
        """Get a specific plugin by name.

        Raises:
            PluginError: If plugin not found
            FrameworkError: If attempting direct access to service provider plugin

        Args:
            plugin_name: Name of the plugin to fetch.
            caller_frame: When `PluginManager.get_plugin` delegates here it
                captures its own caller's frame and passes it through so the
                frame-inspection enforcement sees the ORIGINAL external
                caller (the service wrapper / event_orchestrator / etc.).
                Internal callers from within the dispatcher (e.g.
                `execute_action`) leave this `None` and the dispatcher's own
                `inspect.currentframe().f_back` is used instead.
        """
        if plugin_name not in self._plugins:
            raise PluginError(
                message=f"Plugin '{plugin_name}' not found",
                error_code=ErrorCode.PLUGIN_NOT_FOUND,
                details={
                    "plugin_name": plugin_name,
                    "available_plugins": list(self._plugins.keys()),
                },
                severity=ErrorSeverity.ERROR,
            )

        # PHASE 0.5b ENFORCEMENT: Block direct access to service provider plugins
        # Exception: Service wrappers themselves are allowed to call get_plugin()
        service_name = self._get_service_name_for_plugin(plugin_name)
        if service_name:
            if caller_frame is None:
                current = inspect.currentframe()
                caller_frame = current.f_back if current else None

            if caller_frame is not None:
                caller_filename = caller_frame.f_code.co_filename
                caller_function = caller_frame.f_code.co_name

                # Skip enforcement if called from execute_action() in the same file
                # (execute_action has its own enforcement that checks the ultimate caller)
                if caller_function == "execute_action" and "plugin_dispatcher.py" in caller_filename:
                    pass  # Skip enforcement, let execute_action() handle it
                # Allow calls from service wrapper directories (services/<service_name>/__init__.py)
                # Also allow event_orchestrator which resolves service bindings via _get_bound_plugin_service()
                elif not any(
                    allowed in caller_filename
                    for allowed in [
                        "services/inference_service/",
                        "services/embedding_service/",
                        "services/vector_service/",
                        "services/state_service/",
                        "services/blob_storage_service/",
                        "services/discovery_service/",
                        "services/memory_service/",
                        "services/knowledge_service/",
                        "services/thinking_service/",
                        "event_orchestrator.py",  # Service binding resolution
                        "orchestration/service_manager.py",  # Lifecycle service binding
                    ]
                ):
                    raise FrameworkError(
                        f"Direct access to service provider plugin '{plugin_name}' is not allowed. "
                        f"This plugin provides the '{service_name}' service interface. "
                        f"Use service_interface::{service_name}::method_name instead. "
                        f"This enforcement ensures plugin swappability and architectural integrity."
                    )

        plugin = self._plugins[plugin_name]

        return plugin

    def _get_service_name_for_plugin(self, plugin_name: str) -> str | None:
        """Get the service interface name for a service provider plugin.

        Uses service bindings for dynamic resolution (supports non-default plugins).
        Falls back to static mapping during early startup before bindings are loaded.

        Args:
            plugin_name: Name of the plugin to check

        Returns:
            Service interface name if plugin is a service provider, None otherwise
        """
        orchestrator_ref = self._get_orchestrator_ref()
        # Dynamic resolution via service bindings (handles non-default plugins)
        if orchestrator_ref and hasattr(orchestrator_ref, "service_bindings"):
            bindings = orchestrator_ref.service_bindings
            if hasattr(bindings, "get_services_for_plugin"):
                services = bindings.get_services_for_plugin(plugin_name)
                if services:
                    return str(services[0])

        # Static fallback for early startup (before service bindings loaded)
        static_mappings = {
            DEFAULT_STATE_MANAGEMENT_PLUGIN: "state_service",
            DEFAULT_BLOB_STORAGE_PLUGIN: "blob_storage_service",
            DEFAULT_INFERENCE_PLUGIN: "inference_service",
            DEFAULT_EMBEDDING_PLUGIN: "embedding_service",
            DEFAULT_VECTOR_PLUGIN: "vector_service",
            DEFAULT_MEMORY_PLUGIN: "memory_service",
            DEFAULT_KNOWLEDGE_PLUGIN: "knowledge_service",
            DEFAULT_THINKING_PLUGIN: "thinking_service",
        }
        return static_mappings.get(plugin_name)

    def _get_allowed_service_paths(self) -> list[str]:
        """Return list of allowed caller paths for service provider access."""
        return [
            "/inference_service/",
            "/embedding_service/",
            "/vector_service/",
            "/state_service/",
            "/blob_storage_service/",
            "/discovery_service/",
            "/memory_service/",
            "/knowledge_service/",
            "/thinking_service/",
        ]

    def _is_caller_service_wrapper(self) -> bool:
        """Check if the immediate caller is from a service wrapper."""
        frame = inspect.currentframe()
        if not frame or not frame.f_back or not frame.f_back.f_back:
            return False
        caller_filename = frame.f_back.f_back.f_code.co_filename
        return any(svc in caller_filename for svc in self._get_allowed_service_paths())

    def _enforce_service_provider_access(self, plugin: PluginBase, plugin_name: str) -> None:
        """Block direct access to service provider plugins unless from service wrapper."""
        from ananta.core.plugins.capabilities import is_service_provider

        if not is_service_provider(plugin):
            return

        if self._is_caller_service_wrapper():
            return

        service_name = self._get_service_name_for_plugin(plugin_name)
        service_hint = (
            f" Use 'service_interface::{service_name}::method_name' instead."
            if service_name
            else ""
        )
        raise FrameworkError(
            f"Direct plugin access blocked: '{plugin_name}' is a service provider plugin "
            f"and must be accessed through its service interface.{service_hint} "
            f"Service provider plugins cannot be called directly via execute_action() "
            f"to ensure architectural integrity and swappability."
        )

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    def execute_action(
        self,
        plugin_name: str,
        params: dict[str, object],
        state: dict[str, object],
        APP_HOME: str,  # noqa: N803
        plugin_config: dict[str, object],
    ) -> dict[str, object]:
        """Execute an action on a specific plugin."""
        plugin = self.get_plugin(plugin_name)
        self._enforce_service_provider_access(plugin, plugin_name)
        self._extract_action_name(plugin_name, params)
        method = self._get_execute_action_method(plugin, plugin_name)
        result = method(params, state, APP_HOME, plugin_config)
        return self._validate_action_result(plugin_name, result)

    def _extract_action_name(self, plugin_name: str, params: dict[str, object]) -> str:
        """Extract and validate action name from params."""
        action_obj = params.get("action")
        if not isinstance(action_obj, dict):
            raise PluginError(
                message=f"Invalid params structure for plugin '{plugin_name}': 'action' must be a dict",
                error_code=ErrorCode.PLUGIN_EXECUTION_ERROR,
                details={"plugin_name": plugin_name, "params": params},
                severity=ErrorSeverity.ERROR,
            )

        action_name = action_obj.get("name")
        if not action_name:
            raise PluginError(
                message=f"No action name found in params for plugin '{plugin_name}'",
                error_code=ErrorCode.PLUGIN_EXECUTION_ERROR,
                details={"plugin_name": plugin_name, "params": params},
                severity=ErrorSeverity.ERROR,
            )
        return str(action_name)

    def _get_execute_action_method(
        self, plugin: PluginBase, plugin_name: str,
    ) -> Callable[..., dict[str, object]]:
        """Get and validate the _execute_action method from plugin."""
        if not hasattr(plugin, "_execute_action"):
            raise PluginError(
                message=f"Plugin '{plugin_name}' does not have _execute_action method",
                error_code=ErrorCode.PLUGIN_EXECUTION_ERROR,
                details={"plugin_name": plugin_name, "available_methods": dir(plugin)},
                severity=ErrorSeverity.ERROR,
            )

        method: object = plugin._execute_action
        if not callable(method):
            raise PluginError(
                message=f"Plugin '{plugin_name}' _execute_action is not callable",
                error_code=ErrorCode.PLUGIN_EXECUTION_ERROR,
                details={"plugin_name": plugin_name},
                severity=ErrorSeverity.ERROR,
            )
        # Cast is safe here because we've verified it's callable
        typed_method: Callable[..., dict[str, object]] = method
        return typed_method

    def _validate_action_result(
        self, plugin_name: str, result: object,
    ) -> dict[str, object]:
        """Validate and return action result as dict."""
        if not isinstance(result, dict):
            raise PluginError(
                message=f"Plugin '{plugin_name}' _execute_action returned non-dict result",
                error_code=ErrorCode.PLUGIN_EXECUTION_ERROR,
                details={"plugin_name": plugin_name, "result_type": type(result).__name__},
                severity=ErrorSeverity.ERROR,
            )
        return result
