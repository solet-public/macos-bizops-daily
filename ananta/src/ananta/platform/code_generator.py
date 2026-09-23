from pathlib import Path
from typing import TypedDict

from ananta.error_handling import FrameworkError


class FieldDefinition(TypedDict, total=False):
    """Type definition for field metadata."""

    type: str
    required: bool
    description: str
    validation: str


class ActionDefinition(TypedDict, total=False):
    """Type definition for action metadata."""

    description: str
    parameters: list[str | dict[str, str | bool]]


class ObjectDefinition(TypedDict, total=False):
    """Type definition for object metadata."""

    name: str
    description: str
    fields: dict[str, FieldDefinition]
    actions: dict[str, ActionDefinition]


class AppMetadata(TypedDict, total=False):
    """Type definition for application metadata."""

    app_id: str
    custom_objects: dict[str, ObjectDefinition]


class CodeGenerator:
    def __init__(self, output_directory: str | None = None):
        self.output_directory = Path(output_directory) if output_directory else None
        self._generated_files: list[str] = []

    def generate_model(
        self, object_definition: ObjectDefinition, model_type: str = "pydantic"
    ) -> str:
        """
        Generate Python model from object definition.

        Args:
            object_definition: Custom object metadata
            model_type: Type of model to generate ("pydantic" or "sqlalchemy")

        Returns:
            Generated Python model code
        """
        object_name = object_definition.get("name", "UnknownObject")
        fields = object_definition.get("fields", {})

        if model_type == "pydantic":
            return self._generate_pydantic_model(object_name, fields, object_definition)
        elif model_type == "sqlalchemy":
            return self._generate_sqlalchemy_model(object_name, fields, object_definition)
        else:
            raise FrameworkError(f"Unsupported model type: {model_type}")

    def _generate_pydantic_model(
        self, object_name: str, fields: dict[str, FieldDefinition], definition: ObjectDefinition
    ) -> str:
        """Generate Pydantic model code."""
        class_name = self._to_class_name(object_name)

        imports = [
            "from typing import Optional, List, Dict, Any",
            "from pydantic import BaseModel, Field, validator",
        ]

        field_lines = []
        validators = []

        for field_name, field_def in fields.items():
            field_type = self._map_field_type(field_def.get("type", "string"))
            required = field_def.get("required", True)
            description = field_def.get("description", "")
            validation = field_def.get("validation")

            if not required:
                field_type = f"Optional[{field_type}]"
                default = " = None"
            else:
                default = ""

            field_annotation = f"    {field_name}: {field_type}{default}"
            if description:
                field_annotation += f' = Field(description="{description}")'

            field_lines.append(field_annotation)

            if validation:
                validator_code = self._generate_validator(field_name, validation)
                if validator_code:
                    validators.append(validator_code)

        model_code = "\n".join(imports) + "\n\n\n"
        model_code += f"class {class_name}(BaseModel):\n"

        description_val = definition.get("description")
        if description_val:
            model_code += f'    """{description_val}"""\n\n'

        if field_lines:
            model_code += "\n".join(field_lines) + "\n"
        else:
            model_code += "    pass\n"

        if validators:
            model_code += "\n" + "\n".join(validators)

        return model_code

    def _generate_sqlalchemy_model(
        self, object_name: str, fields: dict[str, FieldDefinition], definition: ObjectDefinition
    ) -> str:
        """Generate SQLAlchemy model code."""
        class_name = self._to_class_name(object_name)
        table_name = self._to_table_name(object_name)

        imports = [
            "from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text, Float",
            "from sqlalchemy.ext.declarative import declarative_base",
            "from datetime import datetime",
        ]

        field_lines = [
            f'    __tablename__ = "{table_name}"',
            "",
            "    id = Column(String, primary_key=True)",
        ]

        for field_name, field_def in fields.items():
            sql_type = self._map_sql_type(field_def.get("type", "string"))
            nullable = not field_def.get("required", True)

            field_line = f"    {field_name} = Column({sql_type}, nullable={nullable})"
            field_lines.append(field_line)

        model_code = "\n".join(imports) + "\n\n"
        model_code += "Base = declarative_base()\n\n\n"
        model_code += f"class {class_name}(Base):\n"

        description_val = definition.get("description")
        if description_val:
            model_code += f'    """{description_val}"""\n\n'

        model_code += "\n".join(field_lines) + "\n"

        return model_code

    # Migration emission removed (2026-05-08).
    #
    # The four `generate_migration` / `_generate_create_migration` /
    # `generate_alter_table_migration` / `_generate_drop_migration` methods
    # that lived here emitted Alembic-shaped Python files. They were never
    # callable end-to-end: the platform has no Alembic dependency, so the
    # `from alembic import op` lines in their output would have raised at
    # runtime. The CREATE stub also silently dropped its `fields` argument
    # (calling `definition.get("fields", {})` and discarding the result),
    # producing a CREATE TABLE with only an `id` column.
    #
    # Plugin schema lifecycle (CREATE/ALTER/DROP applied to live PostgreSQL)
    # is owned by `plugins/postgres_state_management_plugin/.../ddl_renderer.py`
    # via the `service_interface::plugin_schema_service::*` surface.
    # See `knowledge_bases/ananta_platform/15_metadata_driven_ddl/`.

    def generate_llm_specs(
        self, metadata: AppMetadata
    ) -> list[dict[str, str | dict[str, str | list[str] | dict[str, dict[str, str]]]]]:
        specs: list[dict[str, str | dict[str, str | list[str] | dict[str, dict[str, str]]]]] = []
        app_id = metadata.get("app_id", "unknown_app")
        custom_objects = metadata.get("custom_objects", {})

        for obj_name, obj_def in custom_objects.items():
            actions = obj_def.get("actions", {})
            for action_name, action_def in actions.items():
                spec = self._generate_function_spec(app_id, obj_name, action_name, action_def)
                specs.append(spec)

        return specs

    def _generate_function_spec(
        self, app_id: str, obj_name: str, action_name: str, action_def: ActionDefinition
    ) -> dict[str, str | dict[str, str | list[str] | dict[str, dict[str, str]]]]:
        """Generate OpenAI function specification."""
        parameters = action_def.get("parameters", [])

        properties: dict[str, dict[str, str]] = {}
        required: list[str] = []

        for param in parameters:
            if isinstance(param, str):
                properties[param] = {"type": "string", "description": f"{param} parameter"}
                required.append(param)
            else:
                param_name_val = param.get("name")
                if not isinstance(param_name_val, str):
                    param_name_val = "unknown"

                param_type_val = param.get("type")
                if not isinstance(param_type_val, str):
                    param_type_val = "string"

                param_desc_val = param.get("description")
                if not isinstance(param_desc_val, str):
                    param_desc_val = f"{param_name_val} parameter"

                properties[param_name_val] = {
                    "type": self._map_openai_type(param_type_val),
                    "description": param_desc_val,
                }

                param_required_val = param.get("required")
                if param_required_val is None or param_required_val is True:
                    required.append(param_name_val)

        return {
            "name": f"{app_id}_{obj_name}_{action_name}",
            "description": action_def.get(
                "description", f"Execute {action_name} action on {obj_name} object"
            ),
            "parameters": {"type": "object", "properties": properties, "required": required},
        }

    def _to_class_name(self, name: str) -> str:
        return "".join(word.capitalize() for word in name.replace("-", "_").split("_"))

    def _to_table_name(self, name: str) -> str:
        return name.lower().replace("-", "_")

    def _map_field_type(self, field_type: str) -> str:
        type_mapping = {
            "string": "str",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
            "array": "List[Any]",
            "object": "Dict[str, Any]",
            "geolocation": "Dict[str, float]",
        }
        return type_mapping.get(field_type, "str")

    def _map_sql_type(self, field_type: str) -> str:
        type_mapping = {
            "string": "String",
            "integer": "Integer",
            "number": "Float",
            "boolean": "Boolean",
            "array": "Text",  # JSON array as text
            "object": "Text",  # JSON object as text
            "geolocation": "String",
        }
        return type_mapping.get(field_type, "String")

    def _map_openai_type(self, field_type: str) -> str:
        type_mapping = {
            "string": "string",
            "integer": "number",
            "number": "number",
            "boolean": "boolean",
            "array": "array",
            "object": "object",
            "geolocation": "object",
        }
        return type_mapping.get(field_type, "string")

    def _generate_validator(self, field_name: str, validation: str) -> str | None:
        if validation == "species_exists":
            return f"""    @validator('{field_name}')
    def validate_{field_name}(cls, v):
        # Add species validation logic here
        return v"""

        return None

    def save_generated_code(self, code: str, filename: str) -> str:
        if not self.output_directory:
            raise FrameworkError("Output directory not configured")

        self.output_directory.mkdir(parents=True, exist_ok=True)
        file_path = self.output_directory / filename

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        self._generated_files.append(str(file_path))
        return str(file_path)

    def get_generated_files(self) -> list[str]:
        return self._generated_files.copy()

    def clear_generated_files(self) -> None:
        self._generated_files.clear()
