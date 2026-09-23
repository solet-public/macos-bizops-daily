import json
import logging
from collections.abc import Sized
from typing import Protocol

from ananta.constants import ProviderType
from ananta.core.plugins.plugin_contracts import ErrorCode
from ananta.core.process_registry.util import ProcessRegistryUtil
from ananta.error_handling import FrameworkError
from ananta.services.state_service import StateService

logger = logging.getLogger(__name__)


class TemplateValidator(Protocol):
    """Protocol for template validator interface."""

    def validate_process_template(
        self, template: dict[str, object], process_name: str
    ) -> tuple[bool, str | None, dict[str, object] | None]:
        """Validate a process template."""
        ...


class ProcessValidationManager:
    """
    Service for managing process validation and registry operations.

    ARCHITECTURAL ROLE: Supporting service that extracts process validation logic
    from ActionValidator while maintaining validation pipeline integrity.

    This service handles:
    - Process existence validation in registry
    - Process schema retrieval and validation
    - Process registry entry validation
    - Database error handling for process operations
    """

    def __init__(
        self,
        state_service: StateService | None = None,
        template_validator: TemplateValidator | None = None,
    ) -> None:
        """Initialize ProcessValidationManager."""
        self.state_service = state_service
        self.process_registry_util = ProcessRegistryUtil(state_service) if state_service else None
        self.template_validator = template_validator

    def validate_process_exists(self, action_def: dict[str, object]) -> tuple[bool, str | None]:
        """
        Validate that the process defined in action exists in the process registry.

        EXTRACTED FROM: ActionValidator._validate_process_exists() - B(9) complexity

        Args:
            action_def: Action definition containing process information

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not self.process_registry_util:
            return True, None  # Skip validation if no process registry util

        process_key = "<unknown>"  # Initialize before try block for except reference
        try:
            process = action_def.get("process", {})
            if not isinstance(process, dict):
                return False, "Action process must be a dictionary"

            provider_type = process.get("provider_type")
            provider = process.get("provider")
            function_name = process.get("function_name")

            if not all([provider_type, provider, function_name]):
                return (
                    False,
                    "Action process missing required fields: provider_type, provider, function_name",
                )

            process_key = f"{provider_type}::{provider}::{function_name}"

            # Use ProcessRegistryUtil for centralized process registry operations
            result = self.process_registry_util.query_by_process_key(process_key)

            if result and result.get("records"):
                return True, None
            else:
                error_msg = f"Process '{process_key}' not found in process registry"
                return False, error_msg

        except Exception as e:
            error_msg = str(e).lower()
            if self.process_registry_util.check_table_exists(error_msg):
                logger.error("CRITICAL: Core schemas not initialized before ActionValidator use")
                raise FrameworkError(
                    message="ActionValidator cannot function: Core database schemas not initialized. This indicates a startup sequence error - schemas must be created before ActionValidator is used.",
                    error_code=ErrorCode.SYSTEM_GENERIC,
                    details={
                        "missing_table": "process_registry",
                        "process_key": process_key,
                        "original_error": str(e),
                    },
                    original_error=e,
                ) from e
            else:
                logger.error(f"Database error checking process existence for '{process_key}': {e}")
                raise FrameworkError(
                    message=f"Database error while validating process '{process_key}'",
                    error_code=ErrorCode.SYSTEM_GENERIC,
                    details={"process_key": process_key, "error": str(e)},
                    original_error=e,
                ) from e

    def check_process_exists(self, process_key: str) -> bool:
        """
        Check if process exists in the process registry.

        EXTRACTED FROM: ActionValidator._check_process_exists() - B(6) complexity

        Args:
            process_key: Process key to check in format "provider_type::provider::function"

        Returns:
            True if process exists, False otherwise
        """
        if not self.process_registry_util:
            return True  # Skip validation if no process registry util

        try:
            # Use ProcessRegistryUtil for centralized process registry operations
            result = self.process_registry_util.query_by_process_key(process_key)

            if result:
                records = result.get("records", [])
                if isinstance(records, Sized):
                    return len(records) > 0

            return False

        except Exception as e:
            logger.error(f"Error checking process existence for '{process_key}': {e}")
            return False  # Assume process doesn't exist if we can't check

    def get_process_schema(self, process_key: str) -> dict[str, object] | None:
        """
        Retrieve process schema from the process registry.

        EXTRACTED FROM: ActionValidator.get_process_schema() - B(8) complexity

        Args:
            process_key: Process key to retrieve schema for

        Returns:
            Process schema dictionary or None if not found
        """
        if not self.state_service or not self.process_registry_util:
            return None

        try:
            result = self.state_service.read_state(
                namespace="core",
                query={
                    "table": "process_registry",
                    "filters": {"process_key": process_key},
                    "limit": 1,
                },
            )

            if result.get("action_status") == "completed":
                data = result.get("data")
                if not isinstance(data, dict):
                    return None

                result_obj = data.get("result")
                if not isinstance(result_obj, dict):
                    return None

                records = result_obj.get("records")
                if not isinstance(records, list) or len(records) == 0:
                    return None

                process_data = records[0]
                if not isinstance(process_data, dict):
                    return None

                # Extract schema information
                # Use invocation_schema (new) instead of parameter_schema (deprecated)
                invocation_schema = process_data.get("invocation_schema", {})
                result_schema = process_data.get("result_schema", "{}")

                return {
                    "process_key": process_key,
                    "invocation_schema": invocation_schema,
                    "result_schema": result_schema,
                    "description": process_data.get("description", ""),
                    "provider_type": process_data.get("provider_type"),
                    "provider": process_data.get("provider"),
                    "function_name": process_data.get("function_name"),
                }

            return None

        except Exception as e:
            logger.error(f"Error retrieving process schema for '{process_key}': {e}")
            return None

    def validate_process_registry_entry(
        self, process_data: dict[str, object], process_name: str = "unknown"
    ) -> tuple[bool, str | None]:
        """
        Validate process registry entry structure and content.

        COMPLEXITY REDUCED: C(20) → A(4) through focused validation method extraction

        Args:
            process_data: Process registry entry data to validate
            process_name: Name of the process for error reporting

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Step 1: Validate required fields
        valid, error = self._validate_required_fields(process_data, process_name)
        if not valid:
            return False, error

        # Step 2: Validate provider type and process key format
        valid, error = self._validate_process_structure(process_data, process_name)
        if not valid:
            return False, error

        # Step 3: Validate templates and schemas if present
        valid, error = self._validate_templates_and_schemas(process_data, process_name)
        if not valid:
            return False, error

        return True, None

    def _validate_required_fields(
        self, process_data: dict[str, object], process_name: str
    ) -> tuple[bool, str | None]:
        """Validate that all required fields are present and non-empty."""
        has_template = "process_template" in process_data

        if has_template:
            # Full process registry validation
            required_fields = [
                "provider_type",
                "provider",
                "function_name",
                "process_key",
                "name",
                "process_template",
            ]
        else:
            # Basic process data validation
            required_fields = ["process_key", "provider_type", "provider", "function_name"]

        for field in required_fields:
            if field not in process_data or not process_data[field]:
                return False, f"Process '{process_name}' missing required field: {field}"

        return True, None

    def _validate_process_structure(
        self, process_data: dict[str, object], process_name: str
    ) -> tuple[bool, str | None]:
        """Validate provider type and process key format."""
        # Validate provider_type
        provider_type = process_data.get("provider_type")
        if provider_type not in [ProviderType.PLUGIN.value, ProviderType.SERVICE_INTERFACE.value]:
            return (
                False,
                f"Process '{process_name}' has invalid provider_type '{provider_type}'. Must be '{ProviderType.PLUGIN.value}' or '{ProviderType.SERVICE_INTERFACE.value}'",
            )

        # Validate process_key format
        process_key = process_data.get("process_key", "")
        expected_key = f"{process_data.get('provider_type')}::{process_data.get('provider')}::{process_data.get('function_name')}"
        if process_key != expected_key:
            return (
                False,
                f"Process '{process_name}' has invalid process_key format. Expected '{expected_key}', got '{process_key}'",
            )

        return True, None

    def _validate_templates_and_schemas(
        self, process_data: dict[str, object], process_name: str
    ) -> tuple[bool, str | None]:
        """Validate process template, parameter schema, and result schema if present."""
        # Validate process template if present (OLD - deprecated)
        valid, error = self._validate_process_template(process_data, process_name)
        if not valid:
            return False, error

        # Validate parameter schema if present (OLD - deprecated)
        valid, error = self._validate_parameter_schema_optional(process_data, process_name)
        if not valid:
            return False, error

        # Validate result schema if present (OLD - deprecated)
        valid, error = self._validate_result_schema(process_data, process_name)
        if not valid:
            return False, error

        # Validate input_contract if present (NEW - Codex's design)
        valid, error = self._validate_input_contract(process_data, process_name)
        if not valid:
            return False, error

        # Validate action_blueprint if present (NEW - Codex's design)
        valid, error = self._validate_action_blueprint(process_data, process_name)
        if not valid:
            return False, error

        return True, None

    def _validate_process_template(
        self, process_data: dict[str, object], process_name: str
    ) -> tuple[bool, str | None]:
        """Validate process template if present in the data."""
        has_template = "process_template" in process_data
        if not has_template:
            return True, None

        try:
            raw_template = process_data["process_template"]
            process_template: dict[str, object]

            if isinstance(raw_template, str):
                parsed = json.loads(raw_template)
                if not isinstance(parsed, dict):
                    return False, f"Process '{process_name}' process_template must be a JSON object"
                process_template = parsed
            elif isinstance(raw_template, dict):
                process_template = raw_template
            else:
                return (
                    False,
                    f"Process '{process_name}' process_template must be a dict or JSON string",
                )

            # Use template validator if available
            if self.template_validator:
                name = process_data.get("name")
                if not isinstance(name, str):
                    return False, f"Process '{process_name}' name must be a string"

                template_valid, template_error, _ = (
                    self.template_validator.validate_process_template(process_template, name)
                )
                if not template_valid:
                    return False, template_error

        except json.JSONDecodeError as e:
            return False, f"Invalid JSON in process_template: {str(e)}"

        return True, None

    def _validate_parameter_schema_optional(
        self, process_data: dict[str, object], process_name: str
    ) -> tuple[bool, str | None]:
        """Validate parameter schema if present and non-empty."""
        parameter_schema = process_data.get("parameter_schema")
        if parameter_schema and parameter_schema != "{}":
            if isinstance(parameter_schema, str):
                schema_valid, schema_error = self.validate_parameter_schema(
                    parameter_schema, process_name
                )
                if not schema_valid:
                    return False, schema_error
            else:
                return False, f"Process '{process_name}' parameter_schema must be a string"
        return True, None

    def _validate_result_schema(
        self, process_data: dict[str, object], process_name: str
    ) -> tuple[bool, str | None]:
        """Validate result schema if present and non-empty."""
        result_schema = process_data.get("result_schema")
        if result_schema and result_schema != "{}":
            try:
                if isinstance(result_schema, str):
                    json.loads(result_schema)
                elif not isinstance(result_schema, dict):
                    return False, f"Process '{process_name}' has invalid result_schema format"
            except json.JSONDecodeError as e:
                return (
                    False,
                    f"Process '{process_name}' has invalid JSON in result_schema: {str(e)}",
                )
        return True, None

    def _validate_input_contract(
        self, process_data: dict[str, object], process_name: str
    ) -> tuple[bool, str | None]:
        """Validate input_contract if present (NEW - Codex's design)."""
        input_contract = process_data.get("input_contract")
        if not input_contract or input_contract == "{}":
            return True, None  # Optional field

        try:
            contract_obj = self._parse_json_field(input_contract, process_name, "input_contract")
            if contract_obj is None:
                return (
                    False,
                    f"Process '{process_name}' input_contract must be a dict or JSON string",
                )

            return self._validate_contract_structure(contract_obj, process_name)

        except json.JSONDecodeError as e:
            return False, f"Process '{process_name}' has invalid JSON in input_contract: {str(e)}"

    def _validate_contract_structure(
        self, contract_obj: dict[str, object], process_name: str
    ) -> tuple[bool, str | None]:
        """Validate the structure of an input contract object."""
        # Validate expected structure
        if "parameters" not in contract_obj:
            return (
                False,
                f"Process '{process_name}' input_contract missing required field: parameters",
            )

        # Validate parameters is a dict
        parameters = contract_obj.get("parameters")
        if not isinstance(parameters, dict):
            return False, f"Process '{process_name}' input_contract.parameters must be a dict"

        # Validate context_requirements if present
        context_reqs = contract_obj.get("context_requirements")
        if context_reqs is not None and not isinstance(context_reqs, list):
            return (
                False,
                f"Process '{process_name}' input_contract.context_requirements must be a list",
            )

        # Validate result_shape if present
        result_shape = contract_obj.get("result_shape")
        if result_shape is not None and not isinstance(result_shape, dict):
            return False, f"Process '{process_name}' input_contract.result_shape must be a dict"

        return True, None

    def _validate_action_blueprint(
        self, process_data: dict[str, object], process_name: str
    ) -> tuple[bool, str | None]:
        """Validate action_blueprint if present (NEW - Codex's design)."""
        action_blueprint = process_data.get("action_blueprint")
        if not action_blueprint or action_blueprint == "{}":
            return True, None  # Optional field

        try:
            blueprint_obj = self._parse_json_field(
                action_blueprint, process_name, "action_blueprint"
            )
            if blueprint_obj is None:
                return (
                    False,
                    f"Process '{process_name}' action_blueprint must be a dict or JSON string",
                )

            return self._validate_blueprint_structure(blueprint_obj, process_name)

        except json.JSONDecodeError as e:
            return False, f"Process '{process_name}' has invalid JSON in action_blueprint: {str(e)}"

    def _parse_json_field(
        self, field_value: object, process_name: str, field_name: str
    ) -> dict[str, object] | None:
        """Parse a JSON field that can be a string or dict."""
        if isinstance(field_value, str):
            parsed = json.loads(field_value)
            if not isinstance(parsed, dict):
                return None
            return parsed
        if isinstance(field_value, dict):
            return field_value
        return None

    def _validate_blueprint_structure(
        self, blueprint_obj: dict[str, object], process_name: str
    ) -> tuple[bool, str | None]:
        """Validate the structure of an action blueprint object."""
        # Validate required fields
        required_fields = ["process_key", "arguments"]
        for field in required_fields:
            if field not in blueprint_obj:
                return (
                    False,
                    f"Process '{process_name}' action_blueprint missing required field: {field}",
                )

        # Validate process_key is a string
        process_key = blueprint_obj.get("process_key")
        if not isinstance(process_key, str):
            return (
                False,
                f"Process '{process_name}' action_blueprint.process_key must be a string",
            )

        # Validate arguments is a dict
        arguments = blueprint_obj.get("arguments")
        if not isinstance(arguments, dict):
            return False, f"Process '{process_name}' action_blueprint.arguments must be a dict"

        return self._validate_blueprint_optional_fields(blueprint_obj, process_name)

    def _validate_blueprint_optional_fields(
        self, blueprint_obj: dict[str, object], process_name: str
    ) -> tuple[bool, str | None]:
        """Validate optional fields in an action blueprint."""
        context_overrides = blueprint_obj.get("context_overrides")
        if context_overrides is not None and not isinstance(context_overrides, dict):
            return (
                False,
                f"Process '{process_name}' action_blueprint.context_overrides must be a dict",
            )

        metadata = blueprint_obj.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            return False, f"Process '{process_name}' action_blueprint.metadata must be a dict"

        post_processing = blueprint_obj.get("post_processing")
        if post_processing is not None and not isinstance(post_processing, dict):
            return (
                False,
                f"Process '{process_name}' action_blueprint.post_processing must be a dict",
            )

        return True, None

    def validate_parameter_schema(
        self, parameter_schema: str, process_name: str
    ) -> tuple[bool, str | None]:
        """
        Validate parameter schema JSON format.

        EXTRACTED FROM: ActionValidator.validate_parameter_schema()

        Args:
            parameter_schema: Schema to validate
            process_name: Process name for error reporting

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            # Allow empty schemas for parameterless processes
            if not parameter_schema or parameter_schema == "{}":
                return True, None

            schema_obj = json.loads(parameter_schema)

            if not isinstance(schema_obj, dict):
                return False, f"Process '{process_name}' parameter_schema must be a JSON object"

            return True, None

        except json.JSONDecodeError as e:
            return False, f"Process '{process_name}' has invalid JSON in parameter_schema: {str(e)}"
