"""
Validation Service

Responsibility: Handle all validation logic for ActionManager
Dependencies: ActionValidator, validation utilities, logging
Complexity: Medium - focused on validation and error checking

Extracted from ActionManager god class (18 methods)
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime

from ananta.constants import ProviderType
from ananta.core.actions.action_validator import ActionValidator, ValidationResult
from ananta.core.domain.protocols import PluginInterface
from ananta.core.plugins.plugin_contracts import ErrorCode
from ananta.core.validation.validation import validate_action, validate_action_response
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


class ValidationService:
    """
    Service for handling all validation logic in action execution.

    Design Principles:
        pass
    - Single Responsibility: Validation logic only
    - Comprehensive Validation: Action definitions, parameters, execution prerequisites
    - Clear Error Handling: Detailed validation errors with context
    - Template Validation: Complete template variable resolution checking
    """

    def __init__(self, validator: ActionValidator, state_service: object | None = None) -> None:
        """Initialize ValidationService with required dependencies."""
        self.validator = validator
        self.state_service = state_service

    def validate_registration_prerequisites(self) -> bool:
        """Validate that prerequisites for action registration are met."""
        if not self.state_service:
            logger.error("State service not available for action registration")
            return False
        return True

    def validate_action_definition_for_registration(
        self, action_def: dict[str, object]
    ) -> dict[str, object]:
        """Validate action definition and return validation result with action name."""
        # Validate action definition using unified validator
        valid, validation_error = self.validator.definition_manager.validate_action_definition(
            action_def
        )
        if not valid:
            logger.error(f"Action definition validation failed: {validation_error}")
            return {"valid": False}

        action_name = action_def.get("name")
        if not action_name:
            logger.error("Action definition missing required 'name' field")
            return {"valid": False}

        return {"valid": True, "action_name": action_name}

    def extract_and_validate_process_info(
        self,
        action_def: dict[str, object],
        action_name: str,
        process_external_id_getter: Callable[[str], str | None],
    ) -> dict[str, object]:
        """Extract and validate process information from action definition."""
        process_raw = action_def.get("process", {})
        if not isinstance(process_raw, dict):
            logger.error(
                f"Action definition for '{action_name}' has invalid process field (not a dict)"
            )
            raise FrameworkError(
                message=f"Action definition for '{action_name}' has invalid process field",
                error_code="action_manager.invalid_process_field",
                details={"action_name": action_name, "process": process_raw},
            )
        process: dict[str, object] = process_raw

        # Use canonical function_name field consistently
        provider_type = process.get("provider_type", "plugin")
        provider = process.get("provider") or process.get("plugin")
        function_name = process.get("function_name")

        if not provider:
            logger.error(
                f"Action definition for '{action_name}' missing required provider/plugin field"
            )
            raise FrameworkError(
                message=f"Action definition for '{action_name}' missing required provider/plugin field",
                error_code="action_manager.missing_provider_field",
                details={"action_name": action_name, "process": process},
            )

        if not function_name:
            logger.error(
                f"Action definition for '{action_name}' missing required function_name field"
            )
            raise FrameworkError(
                message=f"Action definition for '{action_name}' missing required function_name field",
                error_code="action_manager.missing_function_name_field",
                details={"action_name": action_name, "process": process},
            )

        # Build process key and lookup external ID
        process_key = f"{provider_type}::{provider}::{function_name}"
        process_external_id = process_external_id_getter(process_key)

        if not process_external_id:
            logger.error(
                f"Process '{process_key}' not found in process registry for action '{action_name}'"
            )
            return {"valid": False}

        return {"valid": True, "process_external_id": process_external_id}

    def validate_update_prerequisites(self, action_name: str) -> bool:
        """Validate prerequisites for action updates."""
        if not self.state_service:
            logger.error(f"State service not available for action '{action_name}' update")
            return False
        return True

    def validate_plugin_execution_prerequisites(
        self, plugin: PluginInterface, action_name: str
    ) -> None:
        """Validate plugin execution prerequisites."""
        if not plugin:
            raise FrameworkError(
                message=f"Plugin not available for action '{action_name}'",
                error_code=ErrorCode.PLUGIN_NOT_FOUND,
                details={"action_name": action_name},
            )

        if not hasattr(plugin, "execute_action"):
            plugin_class = plugin.__class__.__name__
            logger.error(
                f"Plugin '{plugin_class}' for action '{action_name}' does not have execute_action method"
            )
            raise FrameworkError(
                message=f"Plugin '{plugin_class}' for action '{action_name}' does not have execute_action method",
                error_code=ErrorCode.PLUGIN_NOT_FOUND,
                details={"plugin_class": plugin_class, "action_name": action_name},
            )

    def validate_and_get_plugin_function(
        self, plugin: PluginInterface, function_name: str, action_name: str
    ) -> object:
        """Validate plugin has required function and return it.

        Plugins implementing a typed ABC contract (e.g. ``MidwifeServiceInterface``)
        often split the verb across two methods: the typed implementation under
        the contract name (e.g. ``birth_solet`` taking typed kwargs) and a
        ``@platform_process``-decorated dispatch wrapper under the convention
        ``<verb>_action`` (taking the standard ``(params, state)`` shape). The
        process_key parses to the verb name; before the exclusive
        midwife/undertaker service-interface retirement on 2026-06-09 those
        plugins were always bound, so plugin-namespace dispatch was skipped for
        them and the typed/wrapper collision never surfaced. Now that
        capability-family plugins ship multiple sibling implementations and
        none is exclusively bound, plugin-namespace dispatch must resolve to
        the wrapper not the typed ABC method. Prefer ``<verb>_action`` when it
        carries ``_platform_process_metadata`` (the marker the
        ``@platform_process`` decorator stamps on the wrapper). The fallback
        path stays compatible with single-method plugins where the verb name
        IS the Python attribute name.
        """
        action_wrapper_name = f"{function_name}_action"
        wrapper_candidate = getattr(plugin, action_wrapper_name, None)
        if wrapper_candidate is not None and hasattr(
            wrapper_candidate, "_platform_process_metadata"
        ):
            function_name = action_wrapper_name

        if not hasattr(plugin, function_name):
            plugin_class = plugin.__class__.__name__
            available_methods = [method for method in dir(plugin) if not method.startswith("_")]
            logger.error(
                f"Plugin '{plugin_class}' for action '{action_name}' does not have function '{function_name}'"
            )
            logger.error(f"Available methods in '{plugin_class}': {available_methods}")
            raise FrameworkError(
                message=f"Plugin '{plugin_class}' for action '{action_name}' does not have function '{function_name}'",
                error_code=ErrorCode.PLUGIN_NOT_FOUND,
                details={
                    "plugin_class": plugin_class,
                    "function_name": function_name,
                    "available_methods": available_methods,
                    "action_name": action_name,
                },
            )

        plugin_function = getattr(plugin, function_name)
        if not callable(plugin_function):
            plugin_class = plugin.__class__.__name__
            logger.error(
                f"Plugin '{plugin_class}' function '{function_name}' for action '{action_name}' is not callable"
            )
            raise FrameworkError(
                message=f"Plugin '{plugin_class}' function '{function_name}' for action '{action_name}' is not callable",
                error_code=ErrorCode.PLUGIN_NOT_FOUND,
                details={
                    "plugin_class": plugin_class,
                    "function_name": function_name,
                    "action_name": action_name,
                },
            )

        return plugin_function

    def execute_and_validate_plugin_result(
        self, plugin_function: object, action_name: str, action_object: dict[str, object]
    ) -> dict[str, object]:
        """Execute plugin function and validate the result."""
        # Execute the plugin function

        # Validate that plugin_function is callable
        if not callable(plugin_function):
            raise FrameworkError(
                message=f"Plugin function for action '{action_name}' is not callable",
                error_code=ErrorCode.PLUGIN_NOT_FOUND,
                details={"action_name": action_name},
            )

        result = plugin_function(action_object)

        # Type narrow result to dict for validation
        if not isinstance(result, dict):
            logger.error(f"Plugin result for action '{action_name}' is not a dict")
            raise FrameworkError(
                message=f"Plugin result for action '{action_name}' must be a dict",
                error_code=ErrorCode.ACTION_INVALID_RESPONSE_FORMAT,
                details={"action_name": action_name, "result_type": type(result).__name__},
            )

        # Validate the result
        validation_error = validate_action_response(result)
        if validation_error:
            logger.error(
                f"Plugin result validation failed for action '{action_name}': {validation_error}"
            )
            raise FrameworkError(
                message=f"Plugin result validation failed for action '{action_name}': {validation_error}",
                error_code=ErrorCode.ACTION_INVALID_RESPONSE_FORMAT,
                details={
                    "action_name": action_name,
                    "validation_error": validation_error,
                    "result": result,
                },
            )

        return result

    def validate_and_prepare_process_record(
        self, process_key: str, process_data: dict[str, object]
    ) -> dict[str, object] | None:
        """Validate process data and prepare record for database storage.

        Args:
            process_key: The process key identifier
            process_data: Process data to validate and prepare

        Returns:
            Validated process record or None if validation fails
        """
        if not process_data:
            logger.error(f"Empty process data for process key '{process_key}'")
            return None

        # Extract and validate required fields
        external_id = process_data.get("external_id")
        if not external_id:
            logger.error(f"Process data for '{process_key}' missing external_id")
            return None

        # Prepare validated record
        validated_record = {
            "process_key": process_key,
            "external_id": external_id,
            "process_status": process_data.get("process_status", "active"),
            "action_name": process_data.get("action_name", ""),
            "provider": process_data.get("provider", ""),
            "function_name": process_data.get("function_name", ""),
            "metadata": json.dumps(process_data.get("metadata", {})),
        }

        return validated_record

    async def validate_action_completeness(
        self,
        action_def: dict[str, object],
        action_name: str,
        is_prepared_action: bool,
        is_runtime_generated: bool,
        execution_id: str,
        start_time: datetime,
    ) -> None:
        """Validate that action is complete (no template variables) before execution.

        Args:
            action_def: The action definition to validate
            action_name: Name of the action being validated
            is_prepared_action: Whether this is a prepared action
            is_runtime_generated: Whether this is a runtime-generated action
            execution_id: Unique execution identifier for tracking
            start_time: Execution start time for tracking

        Raises:
            FrameworkError: If action contains template variables at execution time
        """
        # Skip validation for prepared actions and runtime-generated actions since they're already complete
        if is_prepared_action or is_runtime_generated:
            return

        def _has_template_variables(action_def: dict[str, object]) -> bool:
            arguments = action_def.get("arguments", {})
            if not arguments:
                return True  # Empty arguments indicate incomplete action

            # Check if any argument values contain template patterns
            action_str = json.dumps(arguments)
            # Look for template patterns like <<<VARIABLE>>>, <<<@file>>>, <<<:function()>>>
            template_pattern = r"<<<[^>]+>>>"
            return bool(re.search(template_pattern, action_str))

        if _has_template_variables(action_def):
            logger.error(
                f"TEMPLATE_VALIDATION_ERROR: Action '{action_name}' contains unresolved template variables at execution time"
            )
            logger.error(f"Execution ID: {execution_id}, Start time: {start_time}")
            logger.error(f"Action definition: {json.dumps(action_def, indent=2)}")
            raise FrameworkError(
                message=f"Action '{action_name}' contains unresolved template variables at execution time",
                error_code="action_manager.unresolved_template_variables",
                details={
                    "action_name": action_name,
                    "execution_id": execution_id,
                    "action_definition": action_def,
                },
            )

    async def validate_prepared_action(
        self,
        action_name: str,
        prepared_action: dict[str, object],
        action_parameters: dict[str, object],
        validation_result: ValidationResult | None,
        request_id: str | None,
    ) -> None:
        """Validate prepared action before execution."""
        if request_id:
            pass

    def validate_action_parameters(
        self, action_name: str, action_parameters: dict[str, object], action_def: dict[str, object]
    ) -> None:
        # ARCHITECTURAL SEPARATION: Validation layer uses 'parameters' (processed data for validation)
        action: dict[str, object] = {"name": action_name, "parameters": action_parameters}
        validate_action(action, action_def)

    def validate_action_execution_parameters(
        self,
        action_name: str,
        provider_type: str,
        provider: str,
        function_name: str,
        process: dict[str, object],
    ) -> None:
        """Validate all required action execution parameters are present and valid."""
        # Validate required fields are present
        if not provider_type:
            logger.error(
                f"Missing provider_type for action '{action_name}'. Must specify 'plugin' or 'service'"
            )
            raise FrameworkError(
                message=f"Missing provider_type for action '{action_name}'. Must specify 'plugin' or 'service'",
                error_code=ErrorCode.ACTION_MISSING_PLUGIN,
                details={"action_name": action_name, "process": process},
            )

        # Validate provider_type is supported
        if provider_type not in [ProviderType.PLUGIN.value, ProviderType.SERVICE_INTERFACE.value]:
            logger.error(
                f"Invalid provider_type '{provider_type}' for action '{action_name}'. Must be '{ProviderType.PLUGIN.value}' or '{ProviderType.SERVICE_INTERFACE.value}'"
            )
            raise FrameworkError(
                message=f"Invalid provider_type '{provider_type}' for action '{action_name}'. Must be '{ProviderType.PLUGIN.value}' or '{ProviderType.SERVICE_INTERFACE.value}'",
                error_code=ErrorCode.ACTION_MISSING_PLUGIN,
                details={"action_name": action_name, "provider_type": provider_type},
            )

        if not provider:
            provider_desc = (
                "plugin name"
                if provider_type == ProviderType.PLUGIN.value
                else "service interface name"
            )
            logger.error(f"Missing provider ({provider_desc}) for action '{action_name}'")
            raise FrameworkError(
                message=f"Missing provider ({provider_desc}) for action '{action_name}'",
                error_code=ErrorCode.ACTION_MISSING_PLUGIN,
                details={"action_name": action_name, "provider_type": provider_type},
            )

        if not function_name:
            logger.error(f"Missing function name for action '{action_name}'")
            raise FrameworkError(
                message=f"Missing function name for action '{action_name}'",
                error_code=ErrorCode.ACTION_MISSING_PLUGIN,
                details={
                    "action_name": action_name,
                    "provider_type": provider_type,
                    "provider": provider,
                },
            )

    def validate_service_function(
        self, service: object, function_name: str, action_name: str
    ) -> object:
        """Validate service has required function and return it."""
        if not hasattr(service, function_name):
            service_class = service.__class__.__name__
            available_methods = [method for method in dir(service) if not method.startswith("_")]
            logger.error(
                f"Service '{service_class}' for action '{action_name}' does not have function '{function_name}'"
            )
            logger.error(f"Available methods in '{service_class}': {available_methods}")
            raise FrameworkError(
                message=f"Service '{service_class}' for action '{action_name}' does not have function '{function_name}'",
                error_code=ErrorCode.PLUGIN_NOT_FOUND,
                details={
                    "service_class": service_class,
                    "function_name": function_name,
                    "available_methods": available_methods,
                    "action_name": action_name,
                },
            )

        service_function = getattr(service, function_name)
        if not callable(service_function):
            service_class = service.__class__.__name__
            logger.error(
                f"Service '{service_class}' function '{function_name}' for action '{action_name}' is not callable"
            )
            raise FrameworkError(
                message=f"Service '{service_class}' function '{function_name}' for action '{action_name}' is not callable",
                error_code=ErrorCode.PLUGIN_NOT_FOUND,
                details={
                    "service_class": service_class,
                    "function_name": function_name,
                    "action_name": action_name,
                },
            )

        return service_function

    def validate_sql_query(self, sql_query: str) -> bool:
        """Validate SQL query for basic safety checks."""
        # Basic validation - could be enhanced with more sophisticated checks
        if not sql_query.strip():
            return False

        # Check for dangerous operations (basic whitelist approach)
        sql_lower = sql_query.lower().strip()
        dangerous_keywords = ["drop", "delete", "truncate", "alter", "create"]

        # Allow only SELECT and INSERT operations
        if not (sql_lower.startswith("select") or sql_lower.startswith("insert")):
            return False

        # Check for dangerous keywords
        for keyword in dangerous_keywords:
            if keyword in sql_lower:
                return False

        return True
