import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

from ananta.constants import (
    CONTEXT_KEY_ACTION_ID,
    CONTEXT_KEY_APP_HOME,
    CONTEXT_KEY_ENVIRONMENT,
    CONTEXT_KEY_FLOW_ID,
    CONTEXT_KEY_GLOBAL_VARS,
    CONTEXT_KEY_PROCESS_KEY,
    CONTEXT_KEY_RUNTIME_ARGS,
    CONTEXT_KEY_SESSION_ID,
    CONTEXT_KEY_STATE,
    CONTEXT_KEY_USER_STATE,
)
from ananta.core.contexts.action_contexts import (
    TemplateFunctionContext,
    TemplateResolutionContext,
)
from ananta.core.contexts.normalization import normalize_flow_id, normalize_session_id
from ananta.core.services.variable_resolution_service import VariableResolutionService
from ananta.core.templates.template_exceptions import (
    TemplateFileNotFoundError,
    TemplateResolutionError,
    UnresolvedTemplateVariablesError,
)
from ananta.core.templates.template_functions import (
    MemoryServiceProtocol,
    PluginManagerProtocol,
    StateServiceProtocol,
    TemplateFunctionRegistry,
)
from ananta.error_handling import FrameworkError

from .unified_metadata_registry import UnifiedMetadataRegistry

logger = logging.getLogger(__name__)


class NewTemplateEngine:
    def __init__(
        self,
        metadata_registry: UnifiedMetadataRegistry,
        state_service: StateServiceProtocol | None = None,
        action_manager: object | None = None,
        plugin_manager: PluginManagerProtocol | None = None,
        discovery_service: object | None = None,
        memory_service: MemoryServiceProtocol | None = None,
        knowledge_service: object | None = None,
    ):
        """Initialize NewTemplateEngine with metadata registry and optional dependencies."""
        self.metadata_registry = metadata_registry
        self.template_patterns: dict[str, object] | None = None
        self._initialized = False

        # Store dependencies for TemplateFunctionRegistry initialization
        self.state_service = state_service
        self.action_manager = action_manager
        self.plugin_manager = plugin_manager
        self.discovery_service = discovery_service
        self.memory_service = memory_service
        self.knowledge_service = knowledge_service
        self.function_registry: TemplateFunctionRegistry | None = None

        # Initialize complexity reduction services
        self.variable_resolver = VariableResolutionService()

    def initialize(self) -> bool:
        if self._initialized:
            return True

        self.template_patterns = self.metadata_registry.get_template_patterns()

        if not self.template_patterns:
            raise FrameworkError(
                "Template patterns not found in metadata registry. "
                "Ensure template_patterns.json is present in platform metadata schemas."
            )

        # Type narrow self.template_patterns for logging
        # Note: At this point, template_patterns is guaranteed to be a non-empty dict
        # because we raised if it was falsy above
        assert isinstance(self.template_patterns, dict), (
            "Template patterns must be a dict after validation"
        )
        template_types_raw = self.template_patterns.get("template_types", {})
        if isinstance(template_types_raw, dict):
            pass
        else:
            pass

        # Initialize TemplateFunctionRegistry if dependencies are available
        if self.state_service is not None:
            # Type narrow services for protocol compatibility
            discovery_arg: object | None = (
                self.discovery_service if self.discovery_service else None
            )
            memory_arg: object | None = self.memory_service if self.memory_service else None
            knowledge_arg: object | None = (
                self.knowledge_service if self.knowledge_service else None
            )
            self.function_registry = TemplateFunctionRegistry(
                state_service=self.state_service,
                action_manager=self.action_manager,
                plugin_manager=self.plugin_manager,
                discovery_service=discovery_arg,  # type: ignore[arg-type]
                memory_service=memory_arg,  # type: ignore[arg-type]
                knowledge_service=knowledge_arg,
            )
        else:
            pass

        self._initialized = True
        return True

    def resolve_templates(
        self, action_def: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        """
        Single consolidated template resolution method using metadata.

        Replaces scattered template resolution across 8+ methods with unified approach:
        - Uses metadata-defined resolution order
        - Single pass through data structure
        - Configurable template patterns from metadata
        - Fail-fast validation with metadata rules

        Args:
            action_def: Action definition containing templates
            context: Resolution context (runtime_args, state, hierarchical_context)

        Returns:
            Fully resolved action definition
        """
        if not self._initialized:
            self.initialize()

        action_name = self._extract_action_name(action_def)

        try:
            resolution_order = self._get_resolution_order()
            resolved_data = self._apply_resolution_order(
                action_def, resolution_order, context, action_name
            )
            self._validate_resolution_complete(resolved_data, action_name)
            return resolved_data

        except TemplateResolutionError:
            raise
        except Exception as e:
            raise TemplateResolutionError(
                f"Metadata-driven template resolution failed for {action_name}: {str(e)}",
                template_context={"action": action_name, "context": context},
            ) from e

    def _extract_action_name(self, action_def: dict[str, object]) -> str:
        """Extract action name from definition."""
        action_name_raw = action_def.get("name", "unknown")
        return action_name_raw if isinstance(action_name_raw, str) else "unknown"

    def _get_resolution_order(self) -> list[str]:
        """Get template resolution order from metadata."""
        assert self.template_patterns is not None, "Template engine not initialized"
        assert isinstance(self.template_patterns, dict), "Template patterns must be a dict"
        resolution_order_raw = self.template_patterns.get("resolution_order", [])
        if not isinstance(resolution_order_raw, list):
            raise FrameworkError("resolution_order must be a list")
        return resolution_order_raw

    def _apply_resolution_order(
        self,
        action_def: dict[str, object],
        resolution_order: list[str],
        context: dict[str, object],
        action_name: str,
    ) -> dict[str, object]:
        """Apply template resolution in metadata-defined order."""
        resolved_data: dict[str, object] = action_def

        for template_type in resolution_order:
            resolved_result = self._resolve_template_type(resolved_data, template_type, context)

            if not isinstance(resolved_result, dict):
                raise FrameworkError(
                    f"Template resolution for type '{template_type}' returned "
                    f"{type(resolved_result).__name__} instead of dict for action '{action_name}'"
                )
            resolved_data = resolved_result

        return resolved_data

    def _resolve_template_type(
        self, data: object, template_type: str, context: dict[str, object]
    ) -> object:
        assert self.template_patterns is not None, "Template engine not initialized"
        assert isinstance(self.template_patterns, dict), "Template patterns must be a dict"

        template_types_raw = self.template_patterns.get("template_types")
        if not isinstance(template_types_raw, dict):
            return data

        template_config = template_types_raw.get(template_type)
        if not isinstance(template_config, dict):
            return data

        pattern_raw = template_config.get("pattern")
        resolver_name_raw = template_config.get("resolver")

        if not isinstance(pattern_raw, str) or not isinstance(resolver_name_raw, str):
            return data

        # Recursive resolution through data structure
        return self._resolve_in_data_structure(data, pattern_raw, resolver_name_raw, context)

    def _resolve_in_data_structure(
        self,
        data: object,
        pattern: str,
        resolver_name: str,
        context: dict[str, object],
        _depth: int = 0,
    ) -> object:
        """Recursively resolve templates in data structure."""
        if isinstance(data, dict):
            return {
                key: self._resolve_in_data_structure(
                    value, pattern, resolver_name, context, _depth + 1
                )
                for key, value in data.items()
            }
        elif isinstance(data, list):
            return [
                self._resolve_in_data_structure(item, pattern, resolver_name, context, _depth + 1)
                for item in data
            ]
        elif isinstance(data, str):
            return self._resolve_in_string(data, pattern, resolver_name, context)
        else:
            return data

    def _resolve_in_string(
        self, text: str, pattern: str, resolver_name: str, context: dict[str, object]
    ) -> object:
        """Resolve templates within a string using metadata-defined pattern and resolver."""
        matches = re.findall(pattern, text)
        if not matches:
            return text

        # Get resolver function through metadata configuration
        resolver = self._get_resolver(resolver_name, context)

        # Check if this is full string replacement (entire text matches single template)
        full_pattern_matches = re.findall(f"^{pattern}$", text.strip())
        if len(matches) == 1 and full_pattern_matches:
            # Full string replacement - preserve object types
            resolved_value = resolver(matches[0], context)
            return resolved_value
        else:
            # Partial replacement - use re.sub to respect actual pattern structure

            def replace_match(match_obj: re.Match[str]) -> str:
                match_content = match_obj.group(1)  # Get capture group content
                resolved_value = resolver(match_content, context)
                if resolved_value is None:
                    return match_obj.group(0)
                # Use json.dumps for structured data to preserve proper escaping
                if isinstance(resolved_value, dict | list):
                    return json.dumps(resolved_value)
                return str(resolved_value)

            resolved_text = re.sub(pattern, replace_match, text)
            return resolved_text

    def _get_resolver(
        self, resolver_name: str, _context: dict[str, object]
    ) -> Callable[[str, dict[str, object]], object]:  # Reserved for interface compatibility
        assert self.template_patterns is not None, "Template engine not initialized"
        assert isinstance(self.template_patterns, dict), "Template patterns must be a dict"

        resolver_configurations_raw = self.template_patterns.get("resolver_configurations", {})
        if not isinstance(resolver_configurations_raw, dict):
            resolver_config: dict[str, object] = {}
        else:
            resolver_config_raw = resolver_configurations_raw.get(resolver_name, {})
            resolver_config = resolver_config_raw if isinstance(resolver_config_raw, dict) else {}

        # Map resolver names to methods (could be made more dynamic)
        resolver_methods = {
            "file_inclusion_resolver": self._resolve_file_inclusion,
            "function_execution_resolver": self._resolve_function_execution,
            "variable_substitution_resolver": self._resolve_variable_substitution,
            "hierarchical_context_resolver": self._resolve_hierarchical_context,
            "post_execution_resolver": self._resolve_post_execution,
        }

        resolver_method = resolver_methods.get(resolver_name)
        if not resolver_method:
            raise FrameworkError(f"Unknown resolver: {resolver_name}")

        # Return resolver with configuration
        return lambda match, ctx: resolver_method(match, ctx, resolver_config)

    def _resolve_file_inclusion(
        self, match: str, context: dict[str, object], config: dict[str, object]
    ) -> object:
        """Resolve file inclusion templates using metadata configuration."""
        filename = match

        # Get base path from config (metadata-driven)
        base_path_raw = config.get("base_path", context.get("APP_HOME", ""))
        base_path_str = base_path_raw if isinstance(base_path_raw, str) else ""
        base_path = Path(base_path_str)
        if "actions" not in str(base_path):
            base_path = base_path / "actions"

        file_path = base_path / filename

        # Try with and without .json extension
        if not file_path.exists() and not filename.endswith(".json"):
            file_path = base_path / f"{filename}.json"

        if not file_path.exists():
            raise TemplateFileNotFoundError(filename, [str(file_path)])

        try:
            encoding_raw = config.get("encoding", "utf-8")
            encoding = encoding_raw if isinstance(encoding_raw, str) else "utf-8"
            with open(file_path, encoding=encoding) as f:
                if filename.endswith(".json") or f"{filename}.json" == file_path.name:
                    return json.load(f)
                else:
                    return f.read()
        except Exception as e:
            raise TemplateFileNotFoundError(filename, [str(file_path)], original_error=e) from e

    def _resolve_function_execution(
        self, match: str, context: dict[str, object], _config: dict[str, object]
    ) -> object:
        """Resolve function execution templates using TemplateFunctionRegistry.

        Returns object (not str) to preserve dict/list types for proper JSON embedding.
        Builds typed TemplateFunctionContext - no dict-based context interfaces.
        """
        func_call = match

        if not self.function_registry:
            raise TemplateResolutionError(
                f"Function execution not available: {func_call}. "
                "TemplateFunctionRegistry requires state_service dependency."
            )

        try:
            typed_context = self._build_typed_context(context)
            result = self.function_registry.execute_function(func_call, typed_context)
            return self._parse_function_result(result)

        except FrameworkError:
            # Re-raise FrameworkErrors (like missing flow_id) without wrapping
            raise
        except Exception as e:
            logger.error(f"Function execution failed for '{func_call}': {e}", exc_info=True)
            raise TemplateResolutionError(f"Function execution failed: {func_call} - {e}") from e

    def _build_typed_context(self, context: dict[str, object]) -> TemplateFunctionContext:
        """Build typed TemplateFunctionContext from raw dict.

        Required fields (fail fast if missing):
        - flow_id: Flow context identifier
        - process_key: Process key for the action
        - APP_HOME: Application home directory

        Optional fields:
        - action_id: Optional for pre-persistence contexts
        - session_id: Optional session context

        Args:
            context: Raw context dict containing flow_id, session_id, process_key, APP_HOME.

        Returns:
            Typed TemplateFunctionContext for template function execution.

        Raises:
            FrameworkError: If flow_id, process_key, or APP_HOME is missing.
        """
        # Extract action_id (optional for pre-persistence)
        action_id_raw = context.get(CONTEXT_KEY_ACTION_ID)
        action_id: str | None = str(action_id_raw) if action_id_raw else None

        # Extract process_key (required - fail fast if missing)
        process_key_raw = context.get(CONTEXT_KEY_PROCESS_KEY)
        if not process_key_raw:
            raise FrameworkError(
                message="Template function execution requires process_key in context",
                error_code="template.process_key_required",
                details={CONTEXT_KEY_ACTION_ID: action_id},
            )
        process_key = str(process_key_raw)

        # Extract app_home (required - fail fast if missing)
        app_home_raw = context.get(CONTEXT_KEY_APP_HOME)
        if not app_home_raw:
            raise FrameworkError(
                message="Template function execution requires APP_HOME in context",
                error_code="template.app_home_required",
                details={CONTEXT_KEY_ACTION_ID: action_id, CONTEXT_KEY_PROCESS_KEY: process_key},
            )
        app_home = str(app_home_raw)

        # Get runtime_args for session_id and flow_id extraction
        runtime_args_raw = context.get(CONTEXT_KEY_RUNTIME_ARGS, {})
        runtime_args = runtime_args_raw if isinstance(runtime_args_raw, dict) else {}

        # Extract and normalize session_id (optional)
        session_id_raw = runtime_args.get(CONTEXT_KEY_SESSION_ID) or context.get(
            CONTEXT_KEY_SESSION_ID
        )
        session_id = normalize_session_id(session_id_raw)

        # Extract and normalize flow_id (required - will fail in TemplateFunctionContext if missing)
        flow_id_raw = runtime_args.get(CONTEXT_KEY_FLOW_ID) or context.get(CONTEXT_KEY_FLOW_ID)
        flow_id = normalize_flow_id(flow_id_raw)

        # Fail fast if flow_id is missing
        if not flow_id:
            raise FrameworkError(
                message="Template function execution requires flow_id in context",
                error_code="template.flow_id_required",
                details={
                    CONTEXT_KEY_ACTION_ID: action_id,
                    CONTEXT_KEY_PROCESS_KEY: process_key,
                },
            )

        # Extract local_variables (optional, for result data in templates)
        local_vars_raw = context.get("local_variables", {})
        local_variables = local_vars_raw if isinstance(local_vars_raw, dict) else {}

        # Extract global_variables (optional)
        global_vars_raw = context.get("global_variables", {})
        global_variables = global_vars_raw if isinstance(global_vars_raw, dict) else {}

        # TemplateFunctionContext.__post_init__ will also validate flow_id
        return TemplateFunctionContext(
            action_id=action_id,
            process_key=process_key,
            session_id=session_id,
            flow_id=flow_id,
            app_home=app_home,
            local_variables=local_variables,
            global_variables=global_variables,
        )

    def _parse_function_result(self, result: object) -> object:
        """Parse JSON string results back to dicts for proper embedding."""
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return result
        return result

    def _resolve_variable_substitution(
        self, match: str, context: dict[str, object], config: dict[str, object]
    ) -> str | None:
        """Resolve variable substitution using metadata-configured precedence."""
        var_name = match

        # Build typed resolution context from dict
        resolution_context = self._build_resolution_context(context)

        # Try context sources first using variable resolution service
        result = self.variable_resolver.resolve_variable(var_name, resolution_context, config)
        if result is not None:
            return result

        # NEW: Query key-value store for template variables if not found in immediate context
        return self._query_key_value_store(var_name, context)

    def _build_resolution_context(self, context: dict[str, object]) -> TemplateResolutionContext:
        """Build typed TemplateResolutionContext from raw dict.

        Extracts named sources from context dict with proper type narrowing.
        """
        # Extract runtime_args
        runtime_args_raw = context.get(CONTEXT_KEY_RUNTIME_ARGS, {})
        runtime_args = runtime_args_raw if isinstance(runtime_args_raw, dict) else {}

        # Extract state
        state_raw = context.get(CONTEXT_KEY_STATE, {})
        state = state_raw if isinstance(state_raw, dict) else {}

        # Extract global_vars
        global_vars_raw = context.get(CONTEXT_KEY_GLOBAL_VARS, {})
        global_vars = global_vars_raw if isinstance(global_vars_raw, dict) else {}

        # Extract user_state
        user_state_raw = context.get(CONTEXT_KEY_USER_STATE, {})
        user_state = user_state_raw if isinstance(user_state_raw, dict) else {}

        # Extract environment
        environment_raw = context.get(CONTEXT_KEY_ENVIRONMENT, {})
        environment = environment_raw if isinstance(environment_raw, dict) else {}

        return TemplateResolutionContext(
            runtime_args=runtime_args,
            state=state,
            global_vars=global_vars,
            user_state=user_state,
            environment=environment,
        )

    def _query_key_value_store(
        self, var_name: str, _context: dict[str, object]
    ) -> str | None:  # Reserved for interface compatibility
        """Query key-value store for template variables."""

        if not self.state_service:
            return None

        # TEMPORARY WORKAROUND: Query GLOBAL scope first since SESSION scope is broken
        # TODO: Restore SESSION scope priority when key-value store schema supports session_id
        try:
            # Use hasattr to check if the method exists before calling it
            if not hasattr(self.state_service, "get_key_value"):
                return None

            result = self.state_service.get_key_value(
                namespace="template.variables", key=var_name, scope="GLOBAL"
            )

            # Extract value from ActionResult format
            if result:
                if result.get("action_status") == "completed" and "data" in result:
                    data_raw = result.get("data")
                    if isinstance(data_raw, dict):
                        kv_value_raw = data_raw.get("value")
                        # Type narrow: explicitly check and assign to a typed variable
                        if isinstance(kv_value_raw, str):
                            kv_value: str = kv_value_raw
                            return kv_value

        except Exception:
            pass

        return None

    def _resolve_hierarchical_context(
        self, match: str, _context: dict[str, object], _config: dict[str, object]
    ) -> str:  # Reserved for interface compatibility
        """Resolve hierarchical context templates using metadata configuration."""
        context_pattern = match

        # This would integrate with existing hierarchical context resolution
        # For now, return a placeholder to demonstrate metadata-driven approach
        return f"[CONTEXT_RESULT:{context_pattern}]"

    def _resolve_post_execution(
        self, match: str, _context: dict[str, object], _config: dict[str, object]
    ) -> str:  # Reserved for interface compatibility
        """Resolve post-execution templates using metadata configuration."""
        result_pattern = match

        # This would integrate with existing post-execution resolution
        # For now, return a placeholder to demonstrate metadata-driven approach
        return f"[POST_EXEC_RESULT:{result_pattern}]"

    def resolve_post_execution_templates(
        self, action_def: dict[str, object], execution_result: object
    ) -> dict[str, object]:
        """Resolve <<VARIABLE>> post-execution templates with execution results.

        This method maintains API compatibility with legacy TemplateEngine for ActionManager.
        """
        action_name_raw = action_def.get("name", "unknown")
        action_name_raw if isinstance(action_name_raw, str) else "unknown"

        def substitute_post_execution_variables(obj: object) -> object:
            if isinstance(obj, dict):
                return {
                    key: substitute_post_execution_variables(value) for key, value in obj.items()
                }
            elif isinstance(obj, list):
                return [substitute_post_execution_variables(item) for item in obj]
            elif isinstance(obj, str):
                # Replace <<RESULT>> with execution result
                if obj == "<<RESULT>>":
                    return execution_result
                # Handle partial replacements within strings
                if "<<RESULT>>" in obj:
                    import json

                    result_str = (
                        json.dumps(execution_result)
                        if not isinstance(execution_result, str)
                        else execution_result
                    )
                    substituted = obj.replace("<<RESULT>>", result_str)
                    return substituted
                # Future: Add other <<VARIABLE>> patterns here as needed
                return obj
            else:
                return obj

        resolved_action_raw = substitute_post_execution_variables(action_def)

        # Type narrow the result
        if not isinstance(resolved_action_raw, dict):
            raise FrameworkError(
                f"Expected resolved_action to be dict, got {type(resolved_action_raw)}"
            )
        return resolved_action_raw

    def _validate_resolution_complete(self, resolved_data: object, action_name: str) -> None:
        """
        Validate complete resolution using metadata-defined rules.

        Replaces scattered validation with metadata-driven approach:
        - Validation rules from metadata configuration
        - Configurable fail-fast behavior
        - Unresolved pattern detection from metadata
        """
        if not self._should_validate_resolution():
            return

        unresolved = self._find_unresolved_templates(resolved_data)
        if unresolved:
            raise UnresolvedTemplateVariablesError(unresolved, action_name)

    def _should_validate_resolution(self) -> bool:
        """Check if resolution validation is enabled."""
        assert self.template_patterns is not None, "Template engine not initialized"
        assert isinstance(self.template_patterns, dict), "Template patterns must be a dict"

        validation_rules_raw = self.template_patterns.get("validation_rules", {})
        if not isinstance(validation_rules_raw, dict):
            return False

        if not validation_rules_raw.get("fail_fast", True):
            return False

        if not validation_rules_raw.get("unresolved_patterns_error", True):
            return False

        return True

    def _find_unresolved_templates(self, resolved_data: object) -> list[str]:
        """Find unresolved templates in resolved data."""
        assert self.template_patterns is not None, "Template engine not initialized"
        assert isinstance(self.template_patterns, dict), "Template patterns must be a dict"

        template_types_raw = self.template_patterns.get("template_types")
        if not isinstance(template_types_raw, dict):
            return []

        data_str = json.dumps(resolved_data)
        unresolved: list[str] = []

        for _template_type, config in template_types_raw.items():
            matches = self._find_unresolved_for_type(config, data_str)
            unresolved.extend(matches)

        return unresolved

    def _find_unresolved_for_type(self, config: object, data_str: str) -> list[str]:
        """Find unresolved templates for a specific type."""
        if not isinstance(config, dict):
            return []

        processing_stage = config.get("processing_stage", "static")
        if processing_stage == "post_execution":
            return []

        pattern_raw = config.get("pattern")
        if not isinstance(pattern_raw, str):
            return []

        matches = re.findall(pattern_raw, data_str)
        return [f"<<<{match}>>>" for match in matches]

    def get_engine_info(self) -> dict[str, object]:
        if not self._initialized:
            self.initialize()

        return {
            "engine_type": "metadata_driven",
            "initialized": self._initialized,
            "template_patterns": self.template_patterns,
            "consolidates_methods": [
                "resolve_action_template",
                "_resolve_file_includes",
                "_execute_template_functions",
                "_substitute_variables",
                "_resolve_hierarchical_templates",
                "_resolve_action_result_templates",
                "_resolve_static_templates",
                "resolve_post_execution_templates",
            ],
            "performance_improvements": [
                "Single-pass resolution",
                "Metadata-driven patterns",
                "Configurable validation",
                "Dynamic resolver lookup",
            ],
        }
