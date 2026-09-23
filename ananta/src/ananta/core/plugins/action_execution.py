"""Action execution for plugins.

Extracted from `PluginBase` during the Step 9.B decomposition
(design record, Step 9.2, dev-checkout workbench — not part of the shipped tree).

Owns the runtime execute path: pre-action parameter validation, dispatch
to the action method (sync or async), response formatting, post-action
response validation, and standardized error-response construction.

Module-level functions take the plugin instance as their first argument
so they can access `plugin.name` / `plugin.validation_registry` /
`plugin._current_action_name` without inheriting from PluginBase. Public
entry point: `execute(plugin, action_name, parameters)`.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from ananta.core.domain.enums import ErrorSeverity
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.error_handling import AnantaError, PluginError

if TYPE_CHECKING:
    from ananta.core.plugins.plugin_base import PluginBase


async def execute(
    plugin: PluginBase,
    action_name: str,
    parameters: dict[str, object],
) -> dict[str, object]:
    """Run an action through the standard pre-validate / dispatch / format /
    post-validate sequence; convert any raised exception into a standardized
    error response dict.
    """
    plugin._current_action_name = action_name

    try:
        _validate_pre_action(plugin, action_name, parameters)
        response = await _execute_action_method(plugin, action_name, parameters)
        formatted_response = _format_action_response(response)
        _validate_post_action(plugin, action_name, formatted_response)
        return formatted_response

    except Exception as e:
        return _create_error_response(e)


def _validate_pre_action(
    plugin: PluginBase,
    action_name: str,
    parameters: dict[str, object],
) -> None:
    """Perform pre-action validation if validation registry is available."""
    if not plugin.validation_registry:
        return

    from ananta.core.plugins.plugin_validation import ValidationPhase

    action_request: dict[str, object] = {"name": action_name, "arguments": parameters}
    source_context: dict[str, object] = {"plugin": plugin.name, "action": action_name}
    validation_result = plugin.validation_registry.validate_with_plugins(
        action_request, ValidationPhase.PARAMETER, source_context
    )
    if not validation_result.success:
        raise PluginError(
            message=validation_result.error_message
            or f"Validation failed for action {action_name}",
            error_code="plugin.validation_failed",
            details={"plugin": plugin.name, "action": action_name},
            severity=ErrorSeverity.ERROR,
        )


async def _execute_action_method(
    plugin: PluginBase,
    action_name: str,
    parameters: dict[str, object],
) -> object:
    """Execute the action method (sync or async) and return its response."""
    method = getattr(plugin, action_name, None)
    if not method:
        raise PluginError(
            message=f"Unknown action: {action_name}",
            error_code="plugin.unknown_action",
            details={"plugin": plugin.name, "action": action_name},
            severity=ErrorSeverity.ERROR,
        )

    if inspect.iscoroutinefunction(method):
        return await method(parameters)
    return method(parameters)


def _format_action_response(response: object) -> dict[str, object]:
    """Format action response into standard dict structure."""
    if not isinstance(response, dict):
        response = {"result": response}

    response["action_status"] = ActionStatus.COMPLETED.value
    return response


def _validate_post_action(
    plugin: PluginBase,
    action_name: str,
    response: dict[str, object],
) -> None:
    """Perform post-action validation if validation registry is available."""
    if not plugin.validation_registry:
        return

    from ananta.core.plugins.plugin_validation import ValidationPhase

    action_request: dict[str, object] = {
        "name": action_name,
        "arguments": response,  # Response is validated as arguments
    }
    source_context: dict[str, object] = {
        "plugin": plugin.name,
        "action": action_name,
        "is_response": "true",
    }
    validation_result = plugin.validation_registry.validate_with_plugins(
        action_request, ValidationPhase.FINAL, source_context
    )
    if not validation_result.success:
        raise PluginError(
            message=validation_result.error_message
            or f"Response validation failed for action {action_name}",
            error_code="plugin.response_validation_failed",
            details={"plugin": plugin.name, "action": action_name},
            severity=ErrorSeverity.ERROR,
        )


def _create_error_response(e: Exception) -> dict[str, object]:
    """Create standardized error response from exception."""
    error_response: dict[str, object] = {
        "error": str(e),
        "action_status": ActionStatus.ERROR.value,
    }

    if isinstance(e, AnantaError):
        error_response["error_code"] = e.error_code
        error_response["error_type"] = e.error_type

    return error_response
