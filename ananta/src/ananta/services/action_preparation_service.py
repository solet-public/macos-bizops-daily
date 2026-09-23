import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from ananta.constants import (
    CONTEXT_KEY_APP_HOME,
    CONTEXT_KEY_PROCESS_KEY,
    CONTEXT_KEY_RUNTIME_ARGS,
    CONTEXT_KEY_STATE,
)
from ananta.core.actions.action_validator import ActionValidator
from ananta.core.domain.enums import ErrorSeverity
from ananta.core.orchestration.feature_flags import OrchestrationFeatureFlags
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.core.templates.template_exceptions import (
    TemplateResolutionError,
    UnresolvedTemplateVariablesError,
)
from ananta.core.templates.template_functions import (
    MemoryServiceProtocol,
    PluginManagerProtocol,
    StateServiceProtocol,
)
from ananta.error_handling import FrameworkError

# TemplateEngine imported conditionally in __init__ based on feature flags
# Import Protocol classes from template_functions to avoid incompatible Protocol definitions

logger = logging.getLogger(__name__)


# Protocol definitions for type safety (local to action_preparation_service)
@runtime_checkable
class ActionManagerProtocol(Protocol):
    """Protocol for ActionManager interface."""

    pass


@runtime_checkable
class DiscoveryServiceProtocol(Protocol):
    """Protocol for DiscoveryService interface."""

    def get_process_by_name(self, name: str) -> dict[str, object] | None: ...


@runtime_checkable
class UnifiedMetadataRegistryProtocol(Protocol):
    """Protocol for UnifiedMetadataRegistry interface."""

    pass


@runtime_checkable
class TemplateEngineProtocol(Protocol):
    """Protocol for template engines (both old and new)."""

    def initialize(self) -> bool: ...

    def resolve_templates(
        self, action_def: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]: ...


class ActionPreparationService:
    app_home: Path
    state_service: StateServiceProtocol
    action_manager: ActionManagerProtocol | None
    discovery_service: DiscoveryServiceProtocol | None
    plugin_manager: PluginManagerProtocol | None
    template_engine: TemplateEngineProtocol
    validator: ActionValidator
    _using_new_template_engine: bool

    def __init__(
        self,
        APP_HOME: str,
        state_service: StateServiceProtocol,
        action_manager: ActionManagerProtocol | None = None,
        discovery_service: DiscoveryServiceProtocol | None = None,
        plugin_manager: PluginManagerProtocol | None = None,
        unified_metadata_registry: UnifiedMetadataRegistryProtocol | None = None,
        memory_service: object | None = None,
        knowledge_service: object | None = None,
    ):
        """Initialize ActionPreparationService.

        Args:
            APP_HOME: Application home directory path
            state_service: State management service
            action_manager: Action manager for template function execution
            discovery_service: Service for process discovery
            plugin_manager: Plugin manager for template function execution
            memory_service: Memory service for template function execution
            knowledge_service: Knowledge service for template function execution
        """

        self.app_home = Path(APP_HOME)
        self.state_service = state_service
        self.action_manager = action_manager
        self.discovery_service = discovery_service
        self.plugin_manager = plugin_manager
        self.memory_service = memory_service
        self.knowledge_service = knowledge_service

        # Initialize template engine (conditionally use NewTemplateEngine)
        feature_flag_result = OrchestrationFeatureFlags.use_new_template_engine()

        if feature_flag_result and unified_metadata_registry is not None:
            # FAIL-FAST: No try/catch - let initialization failures propagate
            from ananta.platform.new_template_engine import NewTemplateEngine
            from ananta.platform.unified_metadata_registry import UnifiedMetadataRegistry

            # Type narrowing: at this point unified_metadata_registry is not None
            # We need to verify it's actually a UnifiedMetadataRegistry
            if not isinstance(unified_metadata_registry, UnifiedMetadataRegistry):
                raise FrameworkError(
                    message="unified_metadata_registry must be a UnifiedMetadataRegistry instance",
                    error_code="ananta.action_preparation.invalid_metadata_registry",
                )

            # Cast memory_service to the protocol type expected by NewTemplateEngine
            memory_service_arg: MemoryServiceProtocol | None = None
            if memory_service is not None:
                # Type narrowing for protocol compatibility
                memory_service_arg = memory_service  # type: ignore[assignment]

            self.template_engine = NewTemplateEngine(
                unified_metadata_registry,
                state_service=state_service,
                action_manager=action_manager,
                plugin_manager=plugin_manager,
                discovery_service=discovery_service,
                memory_service=memory_service_arg,
                knowledge_service=knowledge_service,
            )
            self.template_engine.initialize()
            self._using_new_template_engine = True
        else:
            raise FrameworkError(
                message="Legacy TemplateEngine is no longer supported. NewTemplateEngine must be used.",
                error_code="ananta.action_preparation.legacy_engine_removed",
            )

        # Initialize validator
        # ActionValidator expects StateService | None, but we have StateServiceProtocol
        # We need to import and check if it's actually a StateService
        from ananta.services.state_service import StateService

        if isinstance(state_service, StateService):
            self.validator = ActionValidator(state_service)
        else:
            # If it's not a StateService, pass None to validator
            # The validator will work with limited functionality
            self.validator = ActionValidator(None)

    def _resolve_templates_universal(
        self,
        action_def: dict[str, object],
        runtime_args: dict[str, object],
        state: dict[str, object],
        hierarchical_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Universal template resolution that works with NewTemplateEngine."""
        # Extract process_key from action_def for template resolution context
        process_key = action_def.get("process_key", "")
        if not isinstance(process_key, str):
            process_key = str(process_key) if process_key else ""

        # NewTemplateEngine API - single consolidated method
        context: dict[str, object] = {
            CONTEXT_KEY_RUNTIME_ARGS: runtime_args,
            CONTEXT_KEY_STATE: state,
            CONTEXT_KEY_APP_HOME: str(self.app_home),
            CONTEXT_KEY_PROCESS_KEY: process_key,
        }
        if hierarchical_context is not None:
            context["hierarchical_context"] = hierarchical_context

        return self.template_engine.resolve_templates(action_def, context)

    def _validate_static_templates_universal(
        self,
        action_def: dict[str, object],
        action_name: str,
        runtime_args: dict[str, object],
        state: dict[str, object],
    ) -> dict[str, object]:
        """Universal static template validation that works with NewTemplateEngine."""
        # TODO: Use action_name in error messages or logging for better debugging context
        _ = action_name  # Acknowledge parameter is part of public API

        # Extract process_key from action_def for template resolution context
        process_key = action_def.get("process_key", "")
        if not isinstance(process_key, str):
            process_key = str(process_key) if process_key else ""

        # NewTemplateEngine resolves all placeholders in one step
        context: dict[str, object] = {
            CONTEXT_KEY_RUNTIME_ARGS: runtime_args,
            CONTEXT_KEY_STATE: state,
            CONTEXT_KEY_APP_HOME: str(self.app_home),
            CONTEXT_KEY_PROCESS_KEY: process_key,
        }
        return self.template_engine.resolve_templates(action_def, context)

    def prepare_action(
        self,
        action_def: dict[str, object],
        runtime_args: dict[str, object],
        state: dict[str, object],
    ) -> dict[str, object]:
        action_name_obj = action_def.get("name", "unknown")
        # Type narrowing: ensure action_name is a string
        action_name = str(action_name_obj) if action_name_obj is not None else "unknown"
        logger.debug(f"PREPARATION_START: Preparing action '{action_name}'")

        try:
            prepared_action = self._ensure_process_definition(action_def)

            # Step 2: Resolve templates based on validation context
            if self._has_template_patterns(prepared_action):
                logger.debug(f"PREPARATION_TEMPLATES: Resolving templates for {action_name}")
                logger.debug(f"TEMPLATE_PATH_DEBUG: Template patterns detected in {action_name}")
                try:
                    is_runtime_boundary = self._is_runtime_boundary_action(prepared_action)
                    logger.debug(
                        f"TEMPLATE_PATH_DEBUG: Is runtime boundary action: {is_runtime_boundary}"
                    )
                    if is_runtime_boundary:
                        # CRITICAL FIX: Result processor actions need hierarchical template resolution
                        # even though they're runtime-generated
                        hierarchical_context_obj = runtime_args.get("hierarchical_context", {})
                        # Type narrowing: ensure hierarchical_context is a dict
                        if isinstance(hierarchical_context_obj, dict):
                            hierarchical_context: dict[str, object] = hierarchical_context_obj
                        else:
                            hierarchical_context = {}

                        parent_id = hierarchical_context.get("parent_id")
                        has_parent = bool(parent_id)

                        if has_parent:
                            logger.debug(
                                f"PREPARATION_RUNTIME_WITH_PARENT: Action '{action_name}' has parent_id, using full hierarchical resolution"
                            )
                            # For result processors with parents, we need hierarchical resolution
                            state_with_context = {
                                **state,
                                "hierarchical_context": hierarchical_context,
                            }
                            prepared_action = self._validate_static_templates_universal(
                                prepared_action, action_name, runtime_args, state_with_context
                            )
                        else:
                            logger.debug(
                                f"PREPARATION_RUNTIME_BOUNDARY: Using static-only template resolution for '{action_name}'"
                            )
                            prepared_action = self._validate_static_templates_universal(
                                prepared_action, action_name, runtime_args, state
                            )
                    else:
                        logger.debug(
                            f"PREPARATION_STATIC_CONTEXT: Using full template resolution for '{action_name}'"
                        )
                        logger.debug(
                            f"TEMPLATE_PATH_DEBUG: Calling universal template resolution for '{action_name}'"
                        )
                        prepared_action = self._resolve_templates_universal(
                            prepared_action, runtime_args, state
                        )
                        logger.debug(
                            f"TEMPLATE_PATH_DEBUG: Template resolution completed for '{action_name}'"
                        )
                    logger.debug(
                        f"PREPARATION_TEMPLATES_SUCCESS: Templates resolved for '{action_name}'"
                    )
                except Exception as template_error:
                    logger.error(f"PREPARATION_TEMPLATE_ERROR: {template_error}")
                    raise
            else:
                logger.debug(f"PREPARATION_NO_TEMPLATES: No templates found in '{action_name}'")

            # Step 3: Add execution metadata
            prepared_action = self._add_execution_metadata(prepared_action)

            # Step 4: Validate complete action
            self._validate_complete_action(prepared_action, action_name)

            logger.debug(f"PREPARATION_SUCCESS: Action '{action_name}' prepared successfully")
            return prepared_action

        except (TemplateResolutionError, UnresolvedTemplateVariablesError) as e:
            logger.error(f"PREPARATION_TEMPLATE_ERROR: {action_name} - {str(e)}")
            raise FrameworkError(
                message=f"Template resolution failed for action '{action_name}': {str(e)}",
                error_code="ananta.action_preparation.template_error",
                details={"action_name": action_name, "template_error": str(e)},
                severity=ErrorSeverity.ERROR,
                original_error=e,
            ) from e
        except Exception as e:
            logger.error(f"PREPARATION_ERROR: {action_name} - {str(e)}")
            raise FrameworkError(
                message=f"Action preparation failed for '{action_name}': {str(e)}",
                error_code="ananta.action_preparation.general_error",
                details={"action_name": action_name, "error": str(e)},
                severity=ErrorSeverity.ERROR,
                original_error=e,
            ) from e

    def _ensure_process_definition(self, action_def: dict[str, object]) -> dict[str, object]:
        action_name_obj = action_def.get("name")

        # If action already has process info, return as-is
        if "process" in action_def or "process_key" in action_def:
            return action_def.copy()

        # Look up process definition
        if not action_name_obj:
            raise FrameworkError(
                message="Action missing both name and process fields",
                error_code="ananta.action_preparation.missing_name_and_process",
            )

        # Type narrowing: ensure action_name is a string
        if not isinstance(action_name_obj, str):
            raise FrameworkError(
                message=f"Action name must be a string, got {type(action_name_obj).__name__}",
                error_code="ananta.action_preparation.invalid_action_name_type",
            )

        action_name = action_name_obj

        # Use discovery service if available
        if self.discovery_service is not None:
            process_data = self.discovery_service.get_process_by_name(action_name)
            if process_data is not None:
                result = action_def.copy()
                result["process_key"] = process_data.get("composite_key")
                logger.debug(
                    f"PREPARATION_PROCESS_LOOKUP: Found process for '{action_name}': {result['process_key']}"
                )
                return result

        # FAIL-FAST: No fallback code allowed
        raise FrameworkError(
            message=f"No process definition found for action '{action_name}' and no fallback allowed",
            error_code="ananta.action_preparation.no_process_definition",
            details={"action_name": action_name},
            severity=ErrorSeverity.ERROR,
        )

    def _has_template_patterns(self, action_def: dict[str, object]) -> bool:
        import re

        action_str = json.dumps(action_def)

        # Check for various template patterns
        patterns = [
            r"<<<[A-Z_][A-Z0-9_]*>>>",  # Variables: <<<USER_INPUT>>>
            r"<<<@[^>]*>>>",  # Files: <<<@filename.json>>>
            r"<<<:[^>]*>>>",  # Functions: <<<:service_interface::state_service::execute_sql(...)>>>
            r"<<<ACTION_RESULT_FROM:[A-Z0-9_:]+>>>",  # Action result patterns: <<<ACTION_RESULT_FROM:PARENT>>>
        ]

        for pattern in patterns:
            if re.search(pattern, action_str):
                return True
        return False

    def _is_runtime_boundary_action(self, action_def: dict[str, object]) -> bool:
        # Check for runtime-generated flag
        if action_def.get("_runtime_generated", False):
            return True

        # Check if action has result_processor (inherently runtime-dependent)
        if "result_processor" in action_def:
            return True

        # Check if action comes from inference plugin response
        if action_def.get("_validation_context") == "runtime":
            return True

        # Check if action contains runtime template patterns anywhere in structure
        # This catches actions that have runtime templates in nested example_responses
        import json

        action_str = json.dumps(action_def)
        runtime_patterns = [
            "<<<ACTION_RESULT>>>",
            "<<<ACTION_RESULT_FROM:",
            "<<<SESSION_METADATA>>>",
            "<<<FLOW_TRIGGER_DATA>>>",
            "<<<PARENT_ACTION_RESULT>>>",
        ]

        for pattern in runtime_patterns:
            if pattern in action_str:
                return True

        return False

    def _add_execution_metadata(self, action_def: dict[str, object]) -> dict[str, object]:
        result = action_def.copy()

        if "action_status" not in result:
            result["action_status"] = ActionStatus.QUEUED.value

        if "timestamp" not in result:
            result["timestamp"] = datetime.now(UTC).isoformat()

        # Ensure process definition is properly formatted for validation
        # If we have process_key but no process object, reconstruct the process object
        if "process_key" in result and "process" not in result:
            process_key_obj = result["process_key"]
            # Type narrowing: ensure process_key is a string
            if isinstance(process_key_obj, str) and "::" in process_key_obj:
                parts = process_key_obj.split("::")
                if len(parts) == 3:
                    result["process"] = {
                        "provider_type": parts[0],
                        "provider": parts[1],
                        "function_name": parts[2],
                    }

        return result

    def _validate_complete_action(self, action_def: dict[str, object], action_name: str) -> None:
        self._check_unresolved_templates(action_def, action_name)
        self._run_validator_checks(action_def, action_name)

    def _check_unresolved_templates(self, action_def: dict[str, object], action_name: str) -> None:
        """Check for unresolved template variables with context awareness."""
        import re

        action_str = json.dumps(action_def)
        unresolved_vars = re.findall(r"<<<([^>]+(?:>[^>]+)*)>>>", action_str)

        if not unresolved_vars:
            return

        static_unresolved, runtime_unresolved = self._categorize_unresolved_vars(unresolved_vars)

        if static_unresolved:
            logger.error(
                f"PREPARATION_VALIDATION_FAILED: {action_name} has unresolved static templates: {static_unresolved}"
            )
            raise UnresolvedTemplateVariablesError(static_unresolved, action_name)

        if runtime_unresolved:
            logger.debug(
                f"PREPARATION_VALIDATION_RUNTIME_OK: {action_name} has expected unresolved runtime templates: {runtime_unresolved}"
            )

    def _categorize_unresolved_vars(
        self, unresolved_vars: list[str]
    ) -> tuple[list[str], list[str]]:
        """Categorize unresolved variables into static and runtime."""
        runtime_variables = {
            "ACTION_RESULT",
            "PREVIOUS_RESULT",
            "SESSION_METADATA",
            "FLOW_TRIGGER_DATA",
            "PARENT_ACTION_RESULT",
        }
        runtime_patterns = [
            "ACTION_RESULT_FROM:",
            "SESSION_METADATA",
            "FLOW_TRIGGER_DATA",
            "PARENT_ACTION_RESULT",
        ]

        static_unresolved = []
        runtime_unresolved = []

        for var in unresolved_vars:
            if self._is_runtime_variable(var, runtime_variables, runtime_patterns):
                runtime_unresolved.append(var)
            else:
                static_unresolved.append(var)

        return static_unresolved, runtime_unresolved

    def _is_runtime_variable(
        self, var: str, runtime_variables: set[str], runtime_patterns: list[str]
    ) -> bool:
        """Check if a variable is a runtime variable."""
        return (
            var in runtime_variables
            or var.startswith("RESULT_FROM_")
            or var.startswith(":")
            or any(pattern in var for pattern in runtime_patterns)
        )

    def _run_validator_checks(self, action_def: dict[str, object], action_name: str) -> None:
        """Run validator checks on the prepared action."""
        if not self.validator:
            return

        if self._is_runtime_boundary_action(action_def):
            logger.debug(
                f"PREPARATION_VALIDATION_RUNTIME_BOUNDARY: Skipping template validation for runtime boundary action '{action_name}'"
            )
            logger.debug(
                f"PREPARATION_VALIDATION_SUCCESS: {action_name} - runtime boundary action is ready for execution"
            )
            return

        source_context: dict[str, object] = {
            "plugin_level": "action_preparation_service",
            "request_level": "action_preparation",
            "action_level": "validate_prepared_action",
            "trigger_type": "action_preparation_validation",
        }

        validation_result = self.validator.validate_with_routing(
            action_def, action_def, source_context
        )
        if not validation_result.success:
            logger.error(
                f"PREPARATION_VALIDATION_FAILED: {action_name} - {validation_result.error_message}"
            )
            raise FrameworkError(
                message=f"Prepared action validation failed for '{action_name}': {validation_result.error_message}",
                error_code="ananta.action_preparation.validation_failed",
                details={
                    "action_name": action_name,
                    "validation_error": validation_result.error_message,
                },
            )

    def get_preparation_info(self) -> dict[str, object]:
        return {
            "service": "ActionPreparationService",
            "capabilities": [
                "Template resolution (variables, files, functions)",
                "Process lookup and completion",
                "Action validation",
                "Execution metadata addition",
            ],
            "template_engine_info": (
                getattr(self.template_engine, "get_template_info", lambda: None)()
                if self.template_engine
                else None
            ),
            "app_home": str(self.app_home),
        }
