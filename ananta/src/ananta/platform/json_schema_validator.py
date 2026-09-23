import json
from pathlib import Path
from typing import Any, cast

from ananta.core.services.schema_validation_service import SchemaValidationService
from ananta.error_handling import FrameworkError


class ValidationResult:
    def __init__(
        self, valid: bool, errors: list[str] | None = None, warnings: list[str] | None = None
    ) -> None:
        self.valid = valid
        self.errors = errors or []
        self.warnings = warnings or []

    def __bool__(self) -> bool:
        return self.valid

    def to_dict(self) -> dict[str, object]:
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}


class JSONSchemaValidator:
    def __init__(self, schema_directory: str | None = None) -> None:
        self.schema_directory = Path(schema_directory) if schema_directory else None
        self._schemas: dict[str, dict[str, object]] = {}
        self._loaded = False

        # Initialize complexity reduction services
        self.validation_service = SchemaValidationService()

    def load_schemas(self) -> None:
        if self._loaded or not self.schema_directory:
            return

        if not self.schema_directory.exists():
            return

        for schema_file in self.schema_directory.glob("*.json"):
            try:
                with open(schema_file, encoding="utf-8") as f:
                    schema = json.load(f)
                    schema_name = schema_file.stem
                    self._schemas[schema_name] = schema
            except Exception as e:
                raise FrameworkError(f"Failed to load schema {schema_file}: {e}") from e

        self._loaded = True

    def register_schema(self, name: str, schema: dict[str, object]) -> None:
        self._schemas[name] = schema

    def validate(self, data: object, schema_name: str) -> ValidationResult:
        if not self._loaded and self.schema_directory:
            self.load_schemas()

        if schema_name not in self._schemas:
            return ValidationResult(valid=False, errors=[f"Schema '{schema_name}' not found"])

        return self._validate_against_schema(data, self._schemas[schema_name])

    def validate_against_schema(self, data: object, schema: dict[str, object]) -> ValidationResult:
        return self._validate_against_schema(data, schema)

    def _validate_against_schema(self, data: object, schema: dict[str, object]) -> ValidationResult:
        try:
            from jsonschema import Draft7Validator

            validator = Draft7Validator(schema)
            # jsonschema handles type validation internally; cast to Any for type checker
            errors = list(validator.iter_errors(cast(Any, data)))

            if errors:
                error_messages = []
                for error in errors:
                    path = (
                        " -> ".join(str(p) for p in error.absolute_path)
                        if error.absolute_path
                        else "root"
                    )
                    error_messages.append(f"Path '{path}': {error.message}")

                return ValidationResult(valid=False, errors=error_messages)

            return ValidationResult(valid=True)

        except ImportError:
            return self._basic_validation(data, schema)
        except Exception as e:
            return ValidationResult(valid=False, errors=[f"Validation error: {str(e)}"])

    def _basic_validation(self, data: object, schema: dict[str, object]) -> ValidationResult:
        """Basic schema validation using dedicated service."""
        return self.validation_service.validate_by_type(data, schema)  # type: ignore[return-value]
        # SAFE: Both ValidationResult types have identical structure, architectural consolidation deferred

    def validate_ai_app(self, app_definition: dict[str, object]) -> ValidationResult:
        return self.validate(app_definition, "ai_app")

    def validate_action(self, action_definition: dict[str, object]) -> ValidationResult:
        return self.validate(action_definition, "action")

    def validate_custom_object(self, object_definition: dict[str, object]) -> ValidationResult:
        return self.validate(object_definition, "custom_object")

    def validate_plugin_metadata(self, plugin_metadata: dict[str, object]) -> ValidationResult:
        return self.validate(plugin_metadata, "plugin")

    def get_available_schemas(self) -> list[str]:
        if not self._loaded and self.schema_directory:
            self.load_schemas()
        return list(self._schemas.keys())

    def has_schema(self, schema_name: str) -> bool:
        if not self._loaded and self.schema_directory:
            self.load_schemas()
        return schema_name in self._schemas

    def get_schema(self, schema_name: str) -> dict[str, object] | None:
        if not self._loaded and self.schema_directory:
            self.load_schemas()
        return self._schemas.get(schema_name)
