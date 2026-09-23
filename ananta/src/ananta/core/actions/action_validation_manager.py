"""
Action Validation Manager Service

Responsibility: Handle all action validation operations for completeness and parameter validation
Dependencies: ExecutionTrackingManager, FrameworkError, ErrorCode, ErrorSeverity, logging, regex, JSON
Complexity: Medium-High - focused on complex validation logic with architectural violation detection

Extracted from ActionManager god class (B7 + B6 complexity validation methods)
"""

import json
import logging
import re
from datetime import datetime

from ananta.constants import ProviderType
from ananta.core.domain.enums import ErrorSeverity
from ananta.core.plugins.plugin_contracts import ErrorCode
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


class ActionValidationManager:
    """
    Service for managing action validation operations with architectural constraint enforcement.

    ARCHITECTURAL ROLE: Supporting service that extracts validation logic
    from ActionManager while maintaining action validation integrity.

    This service handles:
        pass
    - Action completeness validation with template variable detection
    - Execution parameter validation with provider type checking
    - Architectural violation detection and fail-fast error handling
    - Template pattern recognition using regex for various template formats
    - Provider type validation (plugin vs service_interface) with detailed error context
    - Required field presence validation with comprehensive error reporting
    """

    def __init__(self, execution_tracking_manager=None) -> None:  # type: ignore[no-untyped-def]
        """Initialize ActionValidationManager with required dependencies.

        SAFE: Optional dependency injected at runtime, explicit type would require import risking circular dependency.
        """
        self.execution_tracking_manager = execution_tracking_manager

    async def validate_action_completeness(
        self,
        action_def: dict[str, object],
        action_name: str,
        is_prepared_action: bool,
        is_runtime_generated: bool,
        execution_id: str,
        start_time: datetime,
    ) -> None:
        """
        Validate that action is complete (no template variables) before execution.

        EXTRACTED FROM: ActionManager._validate_action_completeness() - B(7) complexity

        This method handles comprehensive action completeness validation:
            pass
        1. Skip validation for prepared actions and runtime-generated actions (optimization)
        2. Detect template variables using regex pattern matching for multiple formats
        3. Check for result processor actions which bypass validation (exception case)
        4. Enforce architectural principle of fail-fast on incomplete actions
        5. Track execution failure for incomplete actions with detailed error context
        6. Provide comprehensive error reporting with solution guidance

        Args:
            action_def: The action definition to validate
            action_name: Name of the action being validated
            is_prepared_action: Whether this is a prepared action
            is_runtime_generated: Whether this is a runtime-generated action
            execution_id: Unique execution identifier for tracking
            start_time: Execution start time for tracking

        Raises:
            FrameworkError: If action contains template variables at execution time

        Returns:
            None: Validation passes or exception is raised
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
            template_patterns = re.findall(r"__[^_\s]+(?:_[^_\s]*)*__", action_str)
            return len(template_patterns) > 0

        # FAIL-FAST: Actions reaching execution MUST be complete
        # EXCEPTION: Result processor actions are framework-generated and pre-processed
        validation_context = action_def.get("_validation_context")
        is_result_processor = validation_context == "result_processor"

        if _has_template_variables(action_def) and not is_result_processor:
            logger.error(
                f"ARCHITECTURAL_VIOLATION: Action '{action_name}' contains template variables at execution time"
            )
            logger.error(
                f"Template variables found in: {json.dumps(action_def.get('arguments', {}), indent=2)}"
            )
            error = FrameworkError(
                message=f"Incomplete action '{action_name}' reached execution phase with template variables. Actions must be completed during preparation phase.",
                error_code=ErrorCode.ACTION_INVALID_FORMAT,
                details={
                    "action_name": action_name,
                    "violation": "template_variables_at_execution_time",
                    "architectural_principle": "fail_fast_on_incomplete_actions",
                    "solution": "Complete template resolution during action preparation, not execution",
                },
                severity=ErrorSeverity.ERROR,
            )
            if self.execution_tracking_manager:
                await self.execution_tracking_manager.track_action_execution_end(
                    execution_id, start_time, success=False, error=error.to_dict()
                )
            raise error
        elif is_result_processor:
            pass

    def validate_action_execution_parameters(
        self,
        action_name: str,
        provider_type: str,
        provider: str,
        function_name: str,
        process: dict[str, object],
    ) -> None:
        """
        Validate all required action execution parameters are present and valid.

        EXTRACTED FROM: ActionManager._validate_action_execution_parameters() - B(6) complexity

        This method handles comprehensive parameter validation:
            pass
        1. Validate provider_type is present with clear error messaging
        2. Validate provider_type is supported (plugin vs service_interface)
        3. Validate provider name is present with context-specific descriptions
        4. Validate function_name is present with comprehensive error details
        5. Provide detailed error context for troubleshooting and debugging
        6. Enforce strict validation to prevent execution failures downstream

        Args:
            action_name: Name of the action being validated
            provider_type: Type of provider ('plugin' or 'service_interface')
            provider: Name of the provider (plugin name or service interface name)
            function_name: Name of the function to execute
            process: Process configuration for error context

        Raises:
            FrameworkError: If any required parameter is missing or invalid

        Returns:
            None: Validation passes or exception is raised
        """
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
