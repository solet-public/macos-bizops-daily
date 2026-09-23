"""
Validation Coordination Service

Responsibility: Handle plugin and service validation operations for ActionManager
Dependencies: ValidationService, Core error handling
Complexity: Low-Medium - focused validation coordination

Extracted from ActionManager god class during Phase 4 decomposition
"""

import logging
from collections.abc import Callable
from typing import Protocol, cast

from ananta.core.domain.protocols import PluginInterface
from ananta.core.plugins.plugin_contracts import ErrorCode
from ananta.core.validation.validation import validate_action_response
from ananta.error_handling import FrameworkError, PluginError

logger = logging.getLogger(__name__)

# Type alias for plugin function signature
PluginFunction = Callable[[dict[str, object], dict[str, object], str, dict[str, object]], object]

# Type alias for service function signature
ServiceFunction = Callable[[dict[str, object]], object]


class ValidationServiceProtocol(Protocol):
    """Protocol for validation service dependency."""

    def validate_plugin_execution_prerequisites(
        self, plugin: PluginInterface, action_name: str
    ) -> None:
        """Validate plugin execution prerequisites."""
        ...

    def validate_and_get_plugin_function(
        self, plugin: PluginInterface, function_name: str, action_name: str
    ) -> object:
        """Validate plugin has required function and return it."""
        ...

    def validate_service_function(
        self, service: object, function_name: str, action_name: str
    ) -> object:
        """Validate service has required function and return it."""
        ...


class ValidationCoordinator:
    """
    Service for coordinating plugin and service validation operations.

    ARCHITECTURAL ROLE: Supporting service that extracts validation coordination logic
    from ActionManager to create cleaner separation of concerns.

    This service handles:
    - Plugin execution prerequisite validation
    - Plugin function validation and retrieval
    - Plugin result validation
    - Service function validation
    """

    def __init__(self, validation_service: ValidationServiceProtocol):
        """Initialize ValidationCoordinator with validation service dependency."""
        self.validation_service = validation_service

    def validate_plugin_execution_prerequisites(
        self, plugin: PluginInterface, action_name: str
    ) -> None:
        """Validate that plugin execution prerequisites are met.

        Args:
            plugin: Plugin instance to validate
            action_name: Name of action for error context

        Raises:
            PluginError: If plugin is invalid or prerequisites not met
        """
        if not plugin:
            logger.error(f"Plugin object is null or undefined for action '{action_name}'")
            raise PluginError(
                message="Plugin object is null or undefined",
                error_code=ErrorCode.PLUGIN_INVALID_INSTANCE,
                details={"action": action_name},
            )

    def validate_and_get_plugin_function(
        self, plugin: PluginInterface, function_name: str, action_name: str
    ) -> PluginFunction:
        """Validate plugin function exists and is callable.

        Args:
            plugin: Plugin instance to validate
            function_name: Name of function to validate
            action_name: Action name for error context

        Returns:
            Validated callable function

        Raises:
            PluginError: If function is missing or not callable
        """
        # Set current action context on plugin if supported
        if hasattr(plugin, "_current_action_name"):
            plugin._current_action_name = action_name

        # Validate function exists
        if not hasattr(plugin, function_name):
            logger.error(
                f"Plugin '{plugin.__class__.__name__}' missing required function '{function_name}' for action '{action_name}'"
            )
            raise PluginError(
                message=f"Plugin missing required function: {function_name}",
                error_code=ErrorCode.PLUGIN_MISSING_METHOD,
                details={
                    "plugin_name": plugin.__class__.__name__,
                    "function_name": function_name,
                    "action": action_name,
                },
            )

        # Validate function is callable
        function = getattr(plugin, function_name)
        if not callable(function):
            logger.error(
                f"Plugin '{plugin.__class__.__name__}' function '{function_name}' is not callable for action '{action_name}'"
            )
            raise PluginError(
                message=f"Plugin function is not callable: {function_name}",
                error_code=ErrorCode.PLUGIN_MISSING_METHOD,
                details={
                    "plugin_name": plugin.__class__.__name__,
                    "function_name": function_name,
                    "action": action_name,
                },
            )

        # Safe cast after validation - we've confirmed it's callable
        return cast(PluginFunction, function)

    def execute_and_validate_plugin_result(
        self,
        plugin_function: PluginFunction,
        action_object: dict[str, object],
        state: dict[str, object],
        app_home: str,
        plugin_config: dict[str, object],
        action_name: str,
        timestamp: str,
    ) -> dict[str, object]:
        """Execute plugin function and validate the result.

        Args:
            plugin_function: Validated plugin function to execute
            action_object: Action parameters
            state: Execution state
            app_home: Application home directory
            plugin_config: Plugin configuration
            action_name: Action name for error context
            timestamp: Execution timestamp

        Returns:
            Validated plugin result with timestamp

        Raises:
            FrameworkError: If result validation fails
        """
        logger.debug(
            f"ACTION_MANAGER_DEBUG: Calling plugin function with action_object: {action_object}"
        )
        result = plugin_function(action_object, state, app_home, plugin_config)

        # Type narrow result to dict before validation
        if not isinstance(result, dict):
            logger.error(
                f"Plugin returned non-dict result for action '{action_name}': {type(result).__name__}"
            )
            raise FrameworkError(
                message=f"Plugin returned non-dict result: {type(result).__name__}",
                error_code=ErrorCode.ACTION_INVALID_RESPONSE_FORMAT,
                details={"action_name": action_name, "result_type": type(result).__name__},
            )

        # Validate result format
        validation_error = validate_action_response(result)
        if validation_error:
            logger.error(
                f"Plugin returned invalid response format for action '{action_name}': {validation_error}"
            )
            raise FrameworkError(
                message="Plugin returned invalid response format",
                error_code=ErrorCode.ACTION_INVALID_RESPONSE_FORMAT,
                details=validation_error,
            )

        # Ensure timestamp is present
        if "timestamp" not in result:
            result["timestamp"] = timestamp

        return result

    def validate_service_function(
        self, service: object, function_name: str, action_name: str
    ) -> ServiceFunction:
        """Validate that service has the required function and it's callable.

        Args:
            service: Service instance to validate
            function_name: Name of function to validate
            action_name: Action name for error context

        Returns:
            Validated callable function

        Raises:
            FrameworkError: If function is missing or not callable
        """
        # Validate function exists
        if not hasattr(service, function_name):
            logger.error(
                f"Service '{service.__class__.__name__}' missing required function '{function_name}' for action '{action_name}'"
            )
            raise FrameworkError(
                message=f"Service missing required function: {function_name}",
                error_code=ErrorCode.PLUGIN_MISSING_METHOD,
                details={
                    "service_name": service.__class__.__name__,
                    "function_name": function_name,
                    "action": action_name,
                },
            )

        # Validate function is callable
        function = getattr(service, function_name)
        if not callable(function):
            logger.error(
                f"Service '{service.__class__.__name__}' function '{function_name}' is not callable for action '{action_name}'"
            )
            raise FrameworkError(
                message=f"Service function is not callable: {function_name}",
                error_code=ErrorCode.PLUGIN_MISSING_METHOD,
                details={
                    "service_name": service.__class__.__name__,
                    "function_name": function_name,
                    "action": action_name,
                },
            )

        # Safe cast after validation - we've confirmed it's callable
        return cast(ServiceFunction, function)
