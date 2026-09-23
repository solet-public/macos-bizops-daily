"""Service for handling different schema validation strategies."""

from collections.abc import Callable


class ValidationResult:
    """Result of a schema validation operation."""

    def __init__(
        self, valid: bool, errors: list[str] | None = None, warnings: list[str] | None = None
    ):
        self.valid = valid
        self.errors = errors or []
        self.warnings = warnings or []

    def __bool__(self) -> bool:
        return self.valid

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class SchemaValidationService:
    """Handles different types of schema validation strategies."""

    def validate_by_type(self, data: object, schema: dict[str, object]) -> ValidationResult:
        """Validate data against schema based on schema type."""
        errors = []
        warnings = ["Using basic validation - install jsonschema for full validation"]

        schema_type = schema.get("type")

        if schema_type == "object":
            errors.extend(self._validate_object(data, schema))
        elif schema_type == "array":
            errors.extend(self._validate_array(data, schema))
        elif schema_type == "string":
            errors.extend(self._validate_string(data, schema))
        elif schema_type == "number":
            errors.extend(self._validate_number(data, schema))
        elif schema_type == "boolean":
            errors.extend(self._validate_boolean(data, schema))

        return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

    def _validate_object(self, data: object, schema: dict[str, object]) -> list[str]:
        """Validate object type data."""
        errors = []

        if not isinstance(data, dict):
            errors.append("Data must be an object")
            return errors

        # Validate required fields
        required = schema.get("required", [])
        if isinstance(required, list):
            for req_field in required:
                if req_field not in data:
                    errors.append(f"Required field '{req_field}' is missing")

        # Validate field types
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for field, value in data.items():
                if field in properties:
                    field_prop = properties[field]
                    if isinstance(field_prop, dict):
                        field_errors = self._validate_field(field, value, field_prop)
                        errors.extend(field_errors)

        return errors

    def _validate_field(
        self, field_name: str, value: object, field_schema: dict[str, object]
    ) -> list[str]:
        """Validate a single field against its schema."""
        errors = []
        field_type = field_schema.get("type")

        type_validators: dict[str, Callable[[object], bool]] = {
            "string": lambda v: isinstance(v, str),
            "number": lambda v: isinstance(v, int | float),
            "boolean": lambda v: isinstance(v, bool),
            "array": lambda v: isinstance(v, list),
            "object": lambda v: isinstance(v, dict),
        }

        if isinstance(field_type, str):
            validator = type_validators.get(field_type)
            if validator and not validator(value):
                errors.append(f"Field '{field_name}' must be a {field_type}")

        return errors

    def _validate_array(
        self, data: object, _schema: dict[str, object]
    ) -> list[str]:  # Reserved for interface compatibility
        """Validate array type data."""
        if not isinstance(data, list):
            return ["Data must be an array"]
        return []

    def _validate_string(
        self, data: object, _schema: dict[str, object]
    ) -> list[str]:  # Reserved for interface compatibility
        """Validate string type data."""
        if not isinstance(data, str):
            return ["Data must be a string"]
        return []

    def _validate_number(
        self, data: object, _schema: dict[str, object]
    ) -> list[str]:  # Reserved for interface compatibility
        """Validate number type data."""
        if not isinstance(data, int | float):
            return ["Data must be a number"]
        return []

    def _validate_boolean(
        self, data: object, _schema: dict[str, object]
    ) -> list[str]:  # Reserved for interface compatibility
        """Validate boolean type data."""
        if not isinstance(data, bool):
            return ["Data must be a boolean"]
        return []
