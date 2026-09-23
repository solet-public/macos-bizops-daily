"""Service for handling action validation logic."""

from ananta.core.domain.error_codes import ErrorCode
from ananta.error_handling import FrameworkError


class ActionValidationService:
    """Handles complex action validation logic."""

    def validate_action_structure(
        self, action: dict[str, object], definition: dict[str, object] | None
    ) -> None:
        """Validate action structure and parameters."""
        self._validate_action_keys(action)

        # For direct process template actions, skip action definition validation
        if self._is_direct_process_action(action):
            self._prepare_direct_process_action(action)
            return

        self._prepare_traditional_action(action)

        if definition:
            self._validate_action_parameters(action, definition)

    def _validate_action_keys(self, action: dict[str, object]) -> None:
        """Validate required action keys."""
        if "name" not in action and "process_key" not in action:
            raise FrameworkError(
                message="Action missing required field 'name' or 'process_key'",
                error_code=ErrorCode.ACTION_MISSING_NAME,
                details={"action": action},
            )

    def _is_direct_process_action(self, action: dict[str, object]) -> bool:
        """Check if action is a direct process template action."""
        return "name" not in action and "process_key" in action

    def _prepare_direct_process_action(self, action: dict[str, object]) -> None:
        """Prepare direct process template action."""
        if not isinstance(action.get("arguments"), dict):
            action["arguments"] = {}

    def _prepare_traditional_action(self, action: dict[str, object]) -> None:
        """Prepare traditional action with parameters."""
        if not isinstance(action.get("parameters"), dict):
            action["parameters"] = {}

    def _validate_action_parameters(
        self, action: dict[str, object], definition: dict[str, object]
    ) -> None:
        """Validate action parameters against definition."""
        if "parameters" not in definition:
            return

        def_params = definition["parameters"]
        if not isinstance(def_params, dict):
            return

        parameters = action["parameters"]
        if not isinstance(parameters, dict):
            return

        self._validate_required_parameters(action, parameters, def_params)
        self._validate_parameter_types(parameters, def_params)

    def _validate_required_parameters(
        self,
        action: dict[str, object],
        parameters: dict[str, object],
        def_params: dict[str, object],
    ) -> None:
        """Validate required parameters are present."""
        if "required" not in def_params or not isinstance(def_params["required"], list):
            return

        required_params = def_params["required"]
        missing_params = [param for param in required_params if param not in parameters]

        if missing_params:
            action_identifier = action.get("name", action.get("process_key", "unknown"))
            raise FrameworkError(
                message=f"Action '{action_identifier}' missing required parameters",
                error_code=ErrorCode.ACTION_INVALID_PARAMS,
                details={
                    "action": action_identifier,
                    "missing_parameters": missing_params,
                    "received_parameters": list(parameters.keys()),
                    "required_parameters": required_params,
                },
            )

    def _validate_parameter_types(
        self, parameters: dict[str, object], def_params: dict[str, object]
    ) -> None:
        """Validate parameter types against schema properties."""
        if "properties" not in def_params or not isinstance(def_params["properties"], dict):
            return

        properties = def_params["properties"]

        for param_name, param_value in parameters.items():
            if param_name in properties:
                param_schema = properties[param_name]
                if isinstance(param_schema, dict):
                    self._validate_single_parameter_type(param_name, param_value, param_schema)

    def _validate_single_parameter_type(
        self, param_name: str, param_value: object, param_schema: dict[str, object]
    ) -> None:
        """Validate a single parameter against its type schema."""
        param_type = param_schema.get("type")
        if not isinstance(param_type, str):
            return

        type_validators = {
            "string": lambda v: isinstance(v, str),
            "number": lambda v: isinstance(v, int | float),
            "integer": lambda v: isinstance(v, int),
            "boolean": lambda v: isinstance(v, bool),
            "array": lambda v: isinstance(v, list),
            "object": lambda v: isinstance(v, dict),
        }

        validator = type_validators.get(param_type)
        if validator and not validator(param_value):
            # SAFE: validator is a lambda from dict above, guaranteed to be callable
            raise FrameworkError(
                message=f"Parameter '{param_name}' must be of type {param_type}",
                error_code=ErrorCode.ACTION_INVALID_PARAMS,
                details={
                    "parameter": param_name,
                    "expected_type": param_type,
                    "actual_type": type(param_value).__name__,
                    "value": param_value,
                },
            )
