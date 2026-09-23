import json
import logging
import re

from ananta.core.plugins.plugin_validation import PluginValidationRegistry

logger = logging.getLogger(__name__)


class ActionTemplateValidator:
    """
    Service for validating action templates and template substitution.

    ARCHITECTURAL ROLE: Supporting service that extracts template validation logic
    from ActionValidator while maintaining validation pipeline integrity.

    This service handles:
    - Template pattern validation and substitution
    - Static and runtime template variable detection
    - Process template structure validation
    - Template error reporting and logging
    """

    def __init__(self, plugin_validation_registry: PluginValidationRegistry | None = None):
        """Initialize ActionTemplateValidator."""
        self.plugin_validation_registry = plugin_validation_registry or PluginValidationRegistry()

    def validate_template_substitution(
        self, action_request: dict[str, object], action_name: str
    ) -> tuple[bool, str | None]:
        """
        Validate template variable substitution in action requests.

        EXTRACTED FROM: ActionValidator._validate_template_substitution() - A(5) complexity

        Args:
            action_request: Action request dictionary to validate
            action_name: Name of the action for logging

        Returns:
            Tuple of (is_valid, error_message)
        """
        action_str = json.dumps(action_request, sort_keys=True)

        # Extract template variable matches
        matches = self._extract_template_matches(action_str, action_name)
        if not matches:
            return True, None

        # Categorize template variables
        static_unresolved, runtime_unresolved = self._categorize_template_variables(matches)

        # Validate static templates (should be resolved by now)
        static_valid, static_error = self._validate_static_templates(static_unresolved)
        if not static_valid:
            return False, static_error

        # Log runtime templates for monitoring
        self._log_runtime_templates(runtime_unresolved, action_name)

        return True, None

    def _extract_template_matches(self, action_str: str, action_name: str) -> list[str]:
        """
        Extract template variable matches from action string.

        EXTRACTED FROM: ActionValidator._extract_template_matches() - A(1) complexity
        """
        # Find all template variables: <<variable_name>>, <<<variable_name>>>
        # STRICT FORMAT: Only <<VAR>> (local) and <<<VAR>>> (global) are valid
        # Rejected formats: {{VAR}}, {VAR}, $VAR, ${VAR}
        template_patterns = [
            r"<<([A-Za-z_][A-Za-z0-9_]*)>>",  # <<variable>> - local placeholder
            r"<<<([^>]+)>>>",  # <<<variable>>> - global/function call
        ]

        matches = []
        for pattern in template_patterns:
            found_matches = re.findall(pattern, action_str)
            matches.extend(found_matches)

        return matches

    def _categorize_template_variables(self, matches: list[str]) -> tuple[list[str], list[str]]:
        """
        Categorize template variables into static and runtime types.

        EXTRACTED FROM: ActionValidator._categorize_template_variables() - A(3) complexity
        """
        static_unresolved = []
        runtime_unresolved = []

        for match in matches:
            if self._is_runtime_variable(match):
                runtime_unresolved.append(match)
            else:
                static_unresolved.append(match)

        return static_unresolved, runtime_unresolved

    def _is_runtime_variable(self, variable_name: str) -> bool:
        """
        Check if a template variable should be resolved at runtime.

        EXTRACTED FROM: ActionValidator._is_runtime_variable() - A(5) complexity
        """
        # Runtime variables are those that can only be resolved during execution
        runtime_prefixes = [
            "RUNTIME_",
            "SESSION_",
            "EXECUTION_",
            "DYNAMIC_",
            "CONTEXT_",
        ]

        # Check for runtime prefixes
        for prefix in runtime_prefixes:
            if variable_name.upper().startswith(prefix):
                return True

        # Variables with specific patterns that indicate runtime resolution
        runtime_patterns = [
            r".*_ID$",  # Variables ending with _ID are often runtime-generated
            r".*_TOKEN$",  # Security tokens should be runtime
            r".*_TIMESTAMP$",  # Timestamps are runtime
        ]

        for pattern in runtime_patterns:
            if re.match(pattern, variable_name.upper()):
                return True

        return False

    def _validate_static_templates(self, static_unresolved: list[str]) -> tuple[bool, str | None]:
        """
        Validate that static templates have been properly resolved.

        EXTRACTED FROM: ActionValidator._validate_static_templates() - A(3) complexity
        """
        if not static_unresolved:
            return True, None

        # Static templates should have been resolved by this point
        unresolved_vars = ", ".join(static_unresolved)
        error_message = f"Unresolved static template variables: {unresolved_vars}"
        logger.error(f"Template validation failed: {error_message}")

        return False, error_message

    def _log_runtime_templates(self, runtime_unresolved: list[str], action_name: str) -> None:
        """
        Log runtime templates for monitoring and debugging.

        EXTRACTED FROM: ActionValidator._log_runtime_templates() - A(2) complexity
        """
        if runtime_unresolved:
            logger.debug(
                f"Action '{action_name}' has runtime templates: {', '.join(runtime_unresolved)}"
            )

    def validate_process_template(
        self, template: dict[str, object], process_name: str
    ) -> tuple[bool, str | None, dict[str, object] | None]:
        """
        Validate process template structure and content.

        EXTRACTED FROM: ActionValidator.validate_process_template() - B(8) complexity

        Args:
            template: Process template dictionary to validate
            process_name: Name of the process for error reporting

        Returns:
            Tuple of (is_valid, error_message, process_definition)
        """

        # Extract process definition
        process_def = template.get("process")
        if not isinstance(process_def, dict):
            return False, f"Process template {process_name} missing 'process' section", None

        # Validate process definition structure
        validation_result = self._validate_template_patterns(process_def, process_name)
        if not validation_result[0]:
            return validation_result

        # Run plugin-specific template validation if available
        plugin_result = self._run_plugin_template_validation(template, process_name)
        if not plugin_result[0]:
            return plugin_result

        # Ensure we return the correct type by extracting process from template again
        final_process_def = template.get("process")
        if not isinstance(final_process_def, dict):
            return False, f"Process template {process_name} process section is invalid", None
        return True, None, final_process_def

    def _validate_template_patterns(
        self, process_def: dict[str, object], template_name: str
    ) -> tuple[bool, str | None, dict[str, object] | None]:
        """
        Validate template patterns within process definition.

        EXTRACTED FROM: ActionValidator._validate_template_patterns() - A(2) complexity
        """
        # Check for required fields in process definition
        required_fields = ["name", "type"]
        for field in required_fields:
            if field not in process_def:
                error_msg = f"Process template {template_name} missing required field: {field}"
                return False, error_msg, None

        # Validate template variable usage
        process_str = json.dumps(process_def, sort_keys=True)
        matches = self._extract_template_matches(process_str, template_name)

        if matches:
            static_unresolved, _runtime_unresolved = self._categorize_template_variables(matches)
            static_valid, static_error = self._validate_static_templates(static_unresolved)
            if not static_valid:
                return False, f"Template {template_name}: {static_error}", None

        return True, None, process_def

    def _run_plugin_template_validation(
        self, process_template: dict[str, object], template_name: str
    ) -> tuple[bool, str | None, dict[str, object] | None]:
        """Run plugin-specific template validation if available."""
        try:
            # Note: Plugin validation for templates would use PRE_STRUCTURE phase
            # since there's no TEMPLATE_VALIDATION phase in ValidationPhase enum
            # For now, we skip plugin validation for templates as it's not fully implemented

            # Extract process definition with type narrowing
            process_def = process_template.get("process")
            if not isinstance(process_def, dict):
                return False, f"Template {template_name} missing valid process section", None

            return True, None, process_def

        except Exception as e:
            error_msg = f"Plugin template validation failed for {template_name}: {str(e)}"
            logger.error(error_msg)
            return False, error_msg, None
