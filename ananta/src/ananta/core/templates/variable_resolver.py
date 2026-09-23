"""
Variable Resolver Service

Responsibility: Handle variable resolution, file references, and template substitution
Dependencies: StateService, ValidationService
Complexity: Medium - manages file loading and variable resolution operations

Extracted from ActionManager god class during refactoring phases
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class VariableResolver:
    """
    Service for resolving variables, file references, and template substitutions.

    ARCHITECTURAL ROLE: Supporting service that extracts variable resolution logic
    from ActionManager while maintaining template and file reference functionality.

    This service handles:
    - Loading file references (e.g., __@path/to/file.json__ patterns)
    - Resolving template variables and substitutions
    - Managing APP_HOME-relative file operations
    - Variable validation and processing
    """

    def __init__(
        self,
        app_home: Path,
        state_service: object | None = None,
        validation_service: object | None = None,
        async_job_manager: object | None = None,
    ) -> None:
        """Initialize VariableResolver with required dependencies."""
        self.app_home: Path | None = Path(app_home) if app_home else None
        self.state_service = state_service
        self.validation_service = validation_service
        self.async_job_manager = async_job_manager

    def load_file_reference(self, filename: str) -> dict[str, object] | list[object] | str | None:
        """
        Load content from a file reference.

        This method handles file references in the format __@path/to/file.json__ used
        throughout the action system for loading external configuration files.

        Args:
            filename: Path to the file (relative to APP_HOME)

        Returns:
            Loaded file content as dict, list, string, or None if file not found/invalid
        """
        if not self._validate_file_reference_inputs(filename):
            return None

        resolved_path = self._resolve_and_validate_file_path(filename)
        if not resolved_path:
            return None

        return self._load_file_content_by_extension(resolved_path, filename)

    def _validate_file_reference_inputs(self, filename: str) -> bool:
        """Validate inputs for file reference loading."""
        if not filename:
            logger.error("VariableResolver: Empty filename provided for file reference")
            return False

        if not self.app_home:
            logger.error("VariableResolver: APP_HOME not configured")
            return False

        # Security: Reject path traversal attempts
        if ".." in filename:
            logger.error(f"VariableResolver: Path traversal not allowed: {filename}")
            return False

        return True

    def _resolve_and_validate_file_path(self, filename: str) -> Path | None:
        """Resolve and validate file path with security checks."""
        # Type narrowing: app_home must be Path at this point (validated in caller)
        if self.app_home is None:
            return None

        file_path = self.app_home / filename

        try:
            resolved_path = file_path.resolve()
            app_home_resolved = self.app_home.resolve()

            # Security: Ensure file is within APP_HOME
            if not str(resolved_path).startswith(str(app_home_resolved)):
                logger.error(
                    f"VariableResolver: File reference '{filename}' is outside APP_HOME"
                )
                return None

            if not resolved_path.exists():
                logger.error(
                    f"VariableResolver: Referenced file '{filename}' does not exist at {resolved_path}"
                )
                return None

            if not resolved_path.is_file():
                logger.error(f"VariableResolver: Referenced path '{filename}' is not a file")
                return None

            return resolved_path

        except Exception as e:
            logger.error(f"VariableResolver: Error resolving file path for '{filename}': {e}")
            return None

    def _load_file_content_by_extension(
        self, resolved_path: Path, filename: str
    ) -> dict[str, object] | list[object] | str | None:
        """Load file content based on file extension with appropriate error handling."""
        try:
            file_extension = resolved_path.suffix.lower()

            if file_extension == ".json":
                with open(resolved_path, encoding="utf-8") as f:
                    content: dict[str, object] | list[object] = json.load(f)
                return content

            elif file_extension in [".txt", ".md", ".yml", ".yaml"]:
                with open(resolved_path, encoding="utf-8") as f:
                    content_str: str = f.read()
                return content_str

            else:
                # For unknown extensions, try to load as text
                try:
                    with open(resolved_path, encoding="utf-8") as f:
                        content_str = f.read()
                    return content_str
                except UnicodeDecodeError:
                    logger.error(
                        f"VariableResolver: File '{filename}' cannot be loaded as text (binary file?)"
                    )
                    return None

        except json.JSONDecodeError as e:
            logger.error(f"VariableResolver: Invalid JSON in file '{filename}': {e}")
            return None
        except Exception as e:
            logger.error(f"VariableResolver: Error loading file '{filename}': {e}")
            return None

    def resolve_variables(self, template_string: str, context: dict[str, object]) -> str:
        """
        Resolve variables in a template string using the provided context.

        Args:
            template_string: String containing variable placeholders
            context: Dictionary of variable values

        Returns:
            String with variables resolved
        """
        if not template_string:
            return template_string

        # Simple variable substitution for now
        # This can be extended to support more complex template engines
        try:
            resolved = template_string
            for key, value in context.items():
                placeholder = f"{{{key}}}"
                if placeholder in resolved:
                    resolved = resolved.replace(placeholder, str(value))

            return resolved
        except Exception as e:
            logger.error(f"VariableResolver: Error resolving variables in template: {e}")
            return template_string

    def validate_file_references(self, data: object) -> bool:
        """
        Validate that all file references in the given data can be resolved.

        Args:
            data: Data structure to validate for file references

        Returns:
            True if all file references are valid, False otherwise
        """

        def _check_value(value: object) -> bool:
            if isinstance(value, str) and value.startswith("__@") and value.endswith("__"):
                filename = value[3:-2]  # Remove __@ and __
                content = self.load_file_reference(filename)
                return content is not None
            elif isinstance(value, dict):
                return all(_check_value(v) for v in value.values())
            elif isinstance(value, list):
                return all(_check_value(item) for item in value)
            return True

        try:
            return _check_value(data)
        except Exception as e:
            logger.error(f"VariableResolver: Error validating file references: {e}")
            return False

    def expand_file_references(self, data: object) -> object:
        """
        Recursively expand file references in data structures.

        Args:
            data: Data structure that may contain file references

        Returns:
            Data structure with file references expanded
        """

        def _expand_value(value: object) -> object:
            if isinstance(value, str) and value.startswith("__@") and value.endswith("__"):
                filename = value[3:-2]  # Remove __@ and __
                content = self.load_file_reference(filename)
                return content if content is not None else value
            elif isinstance(value, dict):
                return {k: _expand_value(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [_expand_value(item) for item in value]
            return value

        try:
            return _expand_value(data)
        except Exception as e:
            logger.error(f"VariableResolver: Error expanding file references: {e}")
            return data
