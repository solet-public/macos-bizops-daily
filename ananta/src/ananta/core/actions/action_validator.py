import logging
from dataclasses import dataclass
from enum import Enum

from ananta.core.actions.action_definition_manager import ActionDefinitionManager
from ananta.core.actions.action_suggestion_service import ActionSuggestionService
from ananta.core.actions.action_template_validator import ActionTemplateValidator
from ananta.core.plugins.plugin_contracts import ErrorCode
from ananta.core.plugins.plugin_validation import PluginValidationRegistry, ValidationPhase
from ananta.core.process_registry.validation_manager import ProcessValidationManager
from ananta.core.validation.validation import validate_action, validate_action_response
from ananta.error_handling import FrameworkError
from ananta.services.state_service import StateService

logger = logging.getLogger(__name__)


class ValidationDecision(Enum):
    PROCEED = "proceed"
    ROUTE_FOR_CORRECTION = "route_for_correction"
    REJECT = "reject"


@dataclass
class ValidationResult:
    decision: ValidationDecision
    success: bool
    error_message: str | None = None
    route_to_plugin: str | None = None
    suggested_actions: list[str] | None = None
    original_context: dict[str, object] | None = None
    correction_attempt: int = 1

    def __post_init__(self) -> None:
        if self.suggested_actions is None:
            self.suggested_actions = []


class ActionValidationError(FrameworkError):
    def __init__(self, message: str, validation_details: dict[str, object], **kwargs: object):
        super().__init__(
            message=message,
            error_code=ErrorCode.ACTION_INVALID_FORMAT,
            details=validation_details,
            **kwargs,  # type: ignore[arg-type]  # SAFE: kwargs forwarded to FrameworkError, runtime verified
        )
        self.validation_details = validation_details


class ActionValidator:
    def __init__(
        self,
        state_service: StateService | None = None,
        plugin_validation_registry: PluginValidationRegistry | None = None,
    ):
        self.state_service = state_service
        self.plugin_validation_registry = plugin_validation_registry or PluginValidationRegistry()
        self.template_validator = ActionTemplateValidator(self.plugin_validation_registry)
        self.definition_manager = ActionDefinitionManager(state_service)
        self.process_validation_manager = ProcessValidationManager(
            state_service, self.template_validator
        )  # EXTRACTED: Process validation service
        self.suggestion_service = ActionSuggestionService(
            state_service
        )  # EXTRACTED: Action suggestion service

    def validate_with_routing(
        self,
        action_request: dict[str, object],
        action_definition: dict[str, object],
        source_context: dict[str, object],
    ) -> ValidationResult:
        """
        Validate action request with comprehensive routing and plugin validation phases.

        REFACTORED: Extracted helper methods to reduce complexity from C(12).

        This method provides centralized action validation with multiple validation phases,
        plugin integration, parameter checking, and comprehensive error handling.
        """
        try:
            # Phase 1: Initialize validation context and logging
            source_str, originating_plugin, action_name = self._initialize_validation_context(
                action_request, source_context
            )

            # Phase 2: Execute pre-structure validation phase
            result = self._execute_pre_structure_validation(
                action_request, action_definition, source_context, source_str
            )
            if not result.success:
                return result

            # Phase 3: Execute parameter and post-parameter validation
            result = self._execute_parameter_validation_phase(
                action_request, action_definition, source_context, action_name, originating_plugin
            )
            if not result.success:
                return result

            # Phase 4: Execute final validation and return success
            return self._execute_final_validation_phase(action_request, source_context, action_name)

        except Exception as e:
            return self._handle_validation_exception(e, locals())

    def validate_action_request(
        self, action_request: dict[str, object], source_context: dict[str, object]
    ) -> tuple[bool, str | None]:
        source_str = "unknown"  # Initialize before try block for exception handling
        try:
            source_str = str(source_context.get("action_level", "unknown"))
            originating_plugin = str(source_context.get("plugin_level", "unknown"))

            action_request["_source_context"] = source_context
            structure_valid, structure_error = self._validate_action_structure(
                action_request, source_str
            )
            if not structure_valid:
                return False, structure_error

            action_name_obj = action_request["name"]
            if not isinstance(action_name_obj, str):
                return False, "Action name must be a string"
            action_name = action_name_obj

            action_def = self.definition_manager.get_action_definition(action_name)
            if not action_def:
                error_msg = f"Action '{action_name}' not found in action definitions"
                logger.error(f"{error_msg} - originating plugin: {originating_plugin}")
                return False, error_msg

            process_valid, process_error = self.process_validation_manager.validate_process_exists(
                action_def
            )
            if not process_valid:
                return False, process_error

            params_valid, params_error = self._validate_parameters(
                action_request, action_def, action_name
            )
            if not params_valid:
                return False, params_error

            return True, None

        except Exception as e:
            error_msg = f"Validation error for action from {source_str}: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    def validate_batch_actions(
        self, actions: list[dict[str, object]], source: str = "batch"
    ) -> tuple[bool, list[str]]:
        errors = []

        for i, action in enumerate(actions):
            valid, error = self.validate_action_request(
                action, {"action_level": f"{source}[{i}]", "plugin_level": "unknown"}
            )
            if not valid:
                errors.append(f"Action {i}: {error}")

        return len(errors) == 0, errors

    def _validate_action_structure(
        self, action: dict[str, object], source: str
    ) -> tuple[bool, str | None]:
        try:
            # Use existing validation logic but with enhanced error messages
            validate_action(action, None)
            return True, None
        except FrameworkError as e:
            return False, f"[{source}] {e.message}"
        except Exception as e:
            return False, f"[{source}] Structure validation error: {str(e)}"

    def _validate_parameters(
        self, action_request: dict[str, object], action_def: dict[str, object], action_name: str
    ) -> tuple[bool, str | None]:
        try:
            # Check for unresolved template variables before validation
            template_valid, template_error = self.template_validator.validate_template_substitution(
                action_request, action_name
            )
            if not template_valid:
                return False, template_error

            # Use existing parameter validation logic
            validate_action(action_request, action_def)
            return True, None
        except FrameworkError as e:
            return False, e.message
        except Exception as e:
            return False, f"Parameter validation error: {str(e)}"

    def get_process_schema(self, process_key: str) -> dict[str, object] | None:
        """Get process schema - delegated to ProcessValidationManager."""
        return self.process_validation_manager.get_process_schema(process_key)

    def validate_response(
        self, response: dict[str, object], action_name: str = "unknown"
    ) -> tuple[bool, str | None]:
        try:
            validation_error = validate_action_response(response)
            if validation_error:
                error_msg = f"Response validation failed for action '{action_name}': {validation_error.get('message', 'Unknown error')}"
                return False, error_msg
            return True, None
        except Exception as e:
            return False, f"Response validation error for action '{action_name}': {str(e)}"

    def create_validation_summary(
        self, results: list[tuple[str, bool, str | None]]
    ) -> dict[str, object]:
        total = len(results)
        passed = sum(1 for _, valid, _ in results if valid)
        failed = total - passed

        errors_list: list[dict[str, str | None]] = []
        summary: dict[str, object] = {
            "total_validations": total,
            "passed": passed,
            "failed": failed,
            "success_rate": (passed / total * 100) if total > 0 else 0,
            "errors": errors_list,
        }

        for action_name, valid, error in results:
            if not valid:
                errors_list.append({"action": action_name, "error": error})

        return summary

    def _handle_missing_action(
        self, action_name: str, originating_plugin: str, source_context: dict[str, object]
    ) -> ValidationResult:
        """Handle missing action scenarios with suggestion generation - delegated to ActionSuggestionService."""
        logger.error(f"Action '{action_name}' not found - originating plugin: {originating_plugin}")

        # Check if this looks like a correctable action (common patterns)
        is_potentially_correctable = self.suggestion_service.is_potentially_correctable_action(
            action_name
        )

        # Generate suggested alternatives by querying process registry
        suggested_actions = self.suggestion_service.find_similar_actions(action_name)

        # Route back to originating plugin if it's an inference provider and action seems correctable
        if (
            originating_plugin != "unknown"
            and originating_plugin != "external"
            and (is_potentially_correctable or suggested_actions)
        ):
            return ValidationResult(
                decision=ValidationDecision.ROUTE_FOR_CORRECTION,
                success=False,
                error_message=f"Action '{action_name}' not found in action definitions",
                route_to_plugin=originating_plugin,
                suggested_actions=suggested_actions,
                original_context=source_context,
            )

        # Cannot find action - reject (do not route to inference provider)
        else:
            return ValidationResult(
                decision=ValidationDecision.REJECT,
                success=False,
                error_message=f"Action '{action_name}' not found in action definitions",
                route_to_plugin=None,
                suggested_actions=suggested_actions,
                original_context=source_context,
            )

    def _handle_parameter_validation_failure(
        self,
        action_name: str,
        error_message: str,
        originating_plugin: str,
        source_context: dict[str, object],
    ) -> ValidationResult:
        logger.error(f"Parameter validation failed for '{action_name}': {error_message}")

        # Parameter errors are usually correctable by the originating inference provider
        if originating_plugin != "unknown' and originating_plugin != 'external":
            return ValidationResult(
                decision=ValidationDecision.ROUTE_FOR_CORRECTION,
                success=False,
                error_message=f"Parameter validation failed for '{action_name}': {error_message}",
                route_to_plugin=originating_plugin,
                original_context=source_context,
            )

        # Parameter validation failed - reject (do not route to inference provider)
        return ValidationResult(
            decision=ValidationDecision.REJECT,
            success=False,
            error_message=f"Parameter validation failed for '{action_name}': {error_message}",
            route_to_plugin=None,
            original_context=source_context,
        )

    def validate_parameter_schema(
        self, parameter_schema: str, process_name: str
    ) -> tuple[bool, str | None]:
        """Validate parameter schema - delegated to ProcessValidationManager."""
        return self.process_validation_manager.validate_parameter_schema(
            parameter_schema, process_name
        )

    def _initialize_validation_context(
        self, action_request: dict[str, object], source_context: dict[str, object]
    ) -> tuple[str, str, str]:
        """
        Initialize validation context and extract key identifiers.

        EXTRACTED: Helper method for validate_with_routing complexity reduction.
        """
        source_str = str(source_context.get("action_level", "unknown"))
        originating_plugin = str(source_context.get("plugin_level", "unknown"))
        action_name_obj = action_request.get("name", "unknown")
        action_name = str(action_name_obj)

        logger.debug(
            f"VALIDATE-ROUTING-001: Starting validation for action '{action_name}' "
            f"from source: {source_str}, originating plugin: {originating_plugin}"
        )

        return source_str, originating_plugin, action_name

    def _execute_pre_structure_validation(
        self,
        action_request: dict[str, object],
        _action_definition: dict[str, object],  # Reserved for interface compatibility
        source_context: dict[str, object],
        source_str: str,
    ) -> ValidationResult:
        """
        Execute pre-structure validation phase including action structure validation.

        EXTRACTED: Helper method for validate_with_routing complexity reduction.
        """
        # Add source context for traceability
        action_request["_source_context"] = source_context

        # Validate action structure
        structure_valid, structure_error = self._validate_action_structure(
            action_request, source_str
        )
        if not structure_valid:
            return ValidationResult(
                decision=ValidationDecision.REJECT,
                success=False,
                error_message=structure_error,
                original_context=source_context,
            )

        return ValidationResult(
            decision=ValidationDecision.PROCEED,
            success=True,
            original_context=source_context,
        )

    def _execute_parameter_validation_phase(
        self,
        action_request: dict[str, object],
        action_definition: dict[str, object],
        source_context: dict[str, object],
        action_name: str,
        originating_plugin: str,
    ) -> ValidationResult:
        """
        Execute parameter validation and post-parameter validation phases.

        EXTRACTED: Helper method for validate_with_routing complexity reduction.
        """
        # Check if action definition exists
        if not action_definition:
            return self._handle_missing_action(action_name, originating_plugin, source_context)

        # Validate that the process exists
        process_valid, process_error = self.process_validation_manager.validate_process_exists(
            action_definition
        )
        if not process_valid:
            return ValidationResult(
                decision=ValidationDecision.REJECT,
                success=False,
                error_message=process_error,
                original_context=source_context,
            )

        # Validate action parameters
        action_request.get("parameters", {})

        try:
            validate_action(action_request, action_definition)
        except Exception as param_error:
            return self._handle_parameter_validation_failure(
                action_name, str(param_error), originating_plugin, source_context
            )

        # Execute plugin validation hooks if available
        if self.plugin_validation_registry:
            plugin_result = self.plugin_validation_registry.validate_with_plugins(
                action_request, ValidationPhase.POST_PARAMETER, source_context
            )
            if not plugin_result.success:
                return ValidationResult(
                    decision=ValidationDecision.REJECT,
                    success=False,
                    error_message=f"Plugin validation failed: {plugin_result.error_message}",
                    original_context=source_context,
                )

        return ValidationResult(
            decision=ValidationDecision.PROCEED,
            success=True,
            original_context=source_context,
        )

    def _execute_final_validation_phase(
        self,
        _action_request: dict[str, object],
        source_context: dict[str, object],
        action_name: str,  # Reserved for interface compatibility
    ) -> ValidationResult:
        """
        Execute final validation phase and return success result.

        EXTRACTED: Helper method for validate_with_routing complexity reduction.
        """

        return ValidationResult(
            decision=ValidationDecision.PROCEED,
            success=True,
            original_context=source_context,
        )

    def _handle_validation_exception(
        self, exception: Exception, local_vars: dict[str, object]
    ) -> ValidationResult:
        """
        Handle exceptions during validation with comprehensive error logging.

        EXTRACTED: Helper method for validate_with_routing complexity reduction.
        """
        action_request_obj = local_vars.get("action_request", {})
        source_context_obj = local_vars.get("source_context", {})

        if isinstance(action_request_obj, dict):
            action_request = action_request_obj
            action_name = str(action_request.get("name", "unknown"))
        else:
            action_name = "unknown"

        if isinstance(source_context_obj, dict):
            source_context = source_context_obj
        else:
            source_context = {}

        logger.error(
            f"VALIDATE-ROUTING-ERROR: Exception during validation for action '{action_name}': {exception}",
            exc_info=True,
        )

        return ValidationResult(
            decision=ValidationDecision.REJECT,
            success=False,
            error_message=f"Validation failed due to internal error: {str(exception)}",
            original_context=source_context,
        )

    def validate_process_registry_entry(
        self, process_data: dict[str, object], process_name: str = "unknown"
    ) -> tuple[bool, str | None]:
        """Validate process registry entry - delegated to ProcessValidationManager."""
        return self.process_validation_manager.validate_process_registry_entry(
            process_data, process_name
        )
