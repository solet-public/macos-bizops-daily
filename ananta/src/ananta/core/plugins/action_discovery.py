"""Action discovery for plugins.

Extracted from `PluginBase` during the Step 9.B decomposition
(design record, Step 9.2, dev-checkout workbench — not part of the shipped tree).

Walks a plugin instance's class surface, identifies `@platform_process`-
decorated action methods, validates each method's signature against the
fixed (`params`, `state`) action-method contract, and returns the list
of resolved `ActionMetadata` objects.

Module-level functions take the plugin instance as their first argument
so they can access `plugin.name` / `plugin.action_factory` / etc. without
inheriting from PluginBase. Two public entry points:

  - `discover_actions(plugin)` -> list[ActionMetadata]
  - `validate_action_signature(plugin, method_name, method)` -> None
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ananta.core.actions.action_metadata import ActionMetadata
from ananta.error_handling import PluginError

if TYPE_CHECKING:
    from ananta.core.plugins.plugin_base import PluginBase

logger = logging.getLogger(__name__)


# Methods that exist on PluginBase or are commonly added by plugins but
# are NOT @platform_process action methods. Discovered methods with these
# names are skipped during action enumeration. Names that don't exist on
# the class are harmlessly absent from `dir(plugin)` anyway, so this set
# can stay permissive without harm.
_SKIP_METHODS: frozenset[str] = frozenset({
    "get_default_config",
    "set_orchestrator_ref",
    "get_available_actions",
    "execute",
    "set_config_provider",
    "set_event_bus",
    "_resolve_io_process_key",
    "set_validation_registry",
    "get_parameter_validators",
    "get_action_validators",
    "is_service_provider",
    "prepare_for_readiness",
    "supports_template_functions",
    "execute_template_function",
    "get_template_functions",
    "cleanup",
    "cleanup_event_handlers",
    "initialize_event_handlers",
    "execute_async",
    "get_custom_validators",
    "is_ready",
    "set_ready",
    "set_error",
    "set_action_factory",
    "set_state_service",
    "get_schema_definitions",
    "query_model",
    "query_model_async",
    "get_plugin_param",
    "set_blob_storage_service",
    "get_available_providers",
    "get_default_params",
    "get_service_metadata",
    "get_template_engine",
    "get_variable_resolver",
    "register_provider",
    "start_services",
    "stop_services",
    "clear_scheduled_action_async",
    "clear_scheduled_actions_by_tag_async",
    "create_cron_schedule_async",
    "execute_in_seconds_async",
    "process_telegram_action",
    "initialize",
})


def discover_actions(plugin: PluginBase) -> list[ActionMetadata]:
    """Walk a plugin's class surface and return all `@platform_process` actions.

    For each candidate method:
      1. Skip private names, dunders, descriptors, and known non-action methods.
      2. Require the `_platform_process_metadata` attribute (set by the
         `@platform_process` decorator).
      3. Validate the method's signature against the (`params`, `state`)
         contract; raise `PluginError` on mismatch (FATAL — propagated up).
      4. Return a copy of the decorator's `ActionMetadata` with the plugin
         name attached.

    Recovery: if step 3 raises a non-`PluginError` exception (defensive
    against decorator-construction bugs), fall back to default metadata
    so the rest of the discovery pass can complete.
    """
    actions: list[ActionMetadata] = []
    decorator_count = 0

    for name in dir(plugin):
        if not _is_candidate_action_method(plugin, name):
            continue

        method = getattr(plugin, name)
        if not hasattr(method, "_platform_process_metadata"):
            continue

        decorator_count += 1
        try:
            action_metadata = _process_decorated_method(plugin, name, method)
            actions.append(action_metadata)
        except PluginError:
            raise
        except Exception as e:
            logger.error(
                f"Failed to process decorator metadata for '{plugin.name}.{name}': {e}, "
                f"falling back to default metadata"
            )
            action_metadata = _create_default_action_metadata(plugin, name)
            actions.append(action_metadata)

    return actions


def _is_candidate_action_method(plugin: PluginBase, name: str) -> bool:
    """Check if method name is a candidate for action discovery."""
    if name.startswith("_"):
        return False
    if name == "__init__":
        return False
    if name in _SKIP_METHODS:
        return False

    # Check for property/descriptor on the class BEFORE calling getattr
    # on the instance — getattr triggers the property getter, which may
    # fail during startup (e.g., assertions on uninitialized services).
    for cls in type(plugin).__mro__:
        if name in cls.__dict__ and isinstance(cls.__dict__[name], property):
            return False

    attr = getattr(plugin, name)
    if not callable(attr):
        return False
    return True


def _process_decorated_method(
    plugin: PluginBase, name: str, method: Callable[..., Any]
) -> ActionMetadata:
    """Process a decorated method and return its ActionMetadata.

    Validates the action signature. Result/error customizations are
    OPTIONAL on EDGE processes (the post-merge both-blocks FATAL was
    relaxed 2026-07-15, frontier-first consolidation); a decorator may
    omit them entirely or let the companion JSON file supply them.
    """
    from dataclasses import replace

    decorator_metadata: ActionMetadata = getattr(  # noqa: B009
        method, "_platform_process_metadata"
    )
    validate_action_signature(plugin, name, method)
    return replace(decorator_metadata, plugin=plugin.name)


def _generate_display_name(name: str) -> str:
    """Generate human-readable display name from action name."""
    return name.replace("_", " ").title()


def _build_action_metadata(plugin: PluginBase, name: str) -> ActionMetadata:
    """Build ActionMetadata object with standard defaults."""
    return ActionMetadata(
        name=name,
        display_name=_generate_display_name(name),
        description=f"Execute {name} action",
        plugin=plugin.name,
        function=name,
        parameters={},  # Could be enhanced to parse docstrings
        output_type="object",
        output_description="Action execution result",
    )


def _create_default_action_metadata(
    plugin: PluginBase, name: str
) -> ActionMetadata:
    """Create default action metadata as a recovery fallback."""
    return _build_action_metadata(plugin, name)


def _get_expected_action_params() -> list[tuple[str, inspect._ParameterKind, Any]]:
    """Return expected action method parameters (excluding self)."""
    return [
        ("params", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
        ("state", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
    ]


def _extract_method_params(method: Callable[..., Any]) -> list[inspect.Parameter]:
    """Extract method parameters, excluding 'self'."""
    sig = inspect.signature(method)
    params = list(sig.parameters.values())
    if params and params[0].name == "self":
        params = params[1:]
    return params


def validate_action_signature(
    plugin: PluginBase, method_name: str, method: Callable[..., Any]
) -> None:
    """Validate that an action method has the correct signature.

    All plugin action methods MUST have this signature:
        def action_method(
            self,
            params: Dict[str, Any],
            state: Dict[str, Any],
        ) -> Dict[str, Any]:

    Plugins get APP_HOME from `self.orchestrator_ref.APP_HOME`, not as a parameter.

    Raises:
        PluginError: If signature doesn't match the required contract
    """
    expected_params = _get_expected_action_params()
    actual_params = _extract_method_params(method)

    if len(actual_params) != len(expected_params):
        _raise_param_count_error(plugin, method_name, method, expected_params, actual_params)

    for i, (param, (exp_name, _exp_kind, exp_default)) in enumerate(
        zip(actual_params, expected_params, strict=True)
    ):
        _validate_single_param(plugin, method_name, param, exp_name, exp_default, i)


def _raise_param_count_error(
    plugin: PluginBase,
    method_name: str,
    method: Callable[..., Any],
    expected_params: list[tuple[str, inspect._ParameterKind, Any]],
    actual_params: list[inspect.Parameter],
) -> None:
    """Raise PluginError for parameter count mismatch."""
    sig = inspect.signature(method)
    expected_sig = f"def {method_name}(self, params: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]"
    actual_sig = f"def {method_name}{sig}"
    raise PluginError(
        f"Invalid signature for action method '{plugin.name}.{method_name}' - "
        f"Plugin action methods must match the fixed contract defined by ActionProcessor.\n\n"
        f"Expected signature:\n  {expected_sig}\n\n"
        f"Actual signature:\n  {actual_sig}\n\n"
        f"All plugin action methods decorated with @platform_process MUST accept (params, state). "
        f"Plugins get APP_HOME from self.orchestrator_ref.APP_HOME internally.",
        error_code=f"{plugin.name}.invalid_action_signature",
        details={
            "method": method_name,
            "expected_params": [p[0] for p in expected_params],
            "actual_params": [p.name for p in actual_params],
        },
    )


def _validate_single_param(
    plugin: PluginBase,
    method_name: str,
    param: inspect.Parameter,
    exp_name: str,
    exp_default: Any,
    index: int,
) -> None:
    """Validate a single parameter matches expected name and default."""
    if param.name != exp_name:
        raise PluginError(
            f"Invalid signature for '{plugin.name}.{method_name}' - "
            f"Parameter {index + 1} should be named '{exp_name}', got '{param.name}'",
            error_code=f"{plugin.name}.invalid_param_name",
            details={"method": method_name, "expected": exp_name, "actual": param.name},
        )
    if exp_default is inspect.Parameter.empty:
        if param.default != inspect.Parameter.empty:
            raise PluginError(
                f"Invalid signature for '{plugin.name}.{method_name}' - "
                f"Required parameter '{exp_name}' should NOT have a default value",
                error_code=f"{plugin.name}.unexpected_default_value",
                details={"method": method_name, "parameter": exp_name},
            )
    elif param.default == inspect.Parameter.empty:
        raise PluginError(
            f"Invalid signature for '{plugin.name}.{method_name}' - "
            f"Optional parameter '{exp_name}' must have a default value (= None)",
            error_code=f"{plugin.name}.missing_default_value",
            details={"method": method_name, "parameter": exp_name},
        )
