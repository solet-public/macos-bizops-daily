import json
from pathlib import Path

from ananta.error_handling import FrameworkError

# Type aliases for JSON data structures
# We need to define JSONValue recursively to handle nested structures
# Using a placeholder that mypy understands
JSONPrimitive = str | int | float | bool | None
JSONValue = JSONPrimitive | dict[str, "JSONValue"] | list["JSONValue"]
JSONDict = dict[str, JSONValue]


class MetadataRegistry:
    def __init__(self, metadata_folder: str):
        self.metadata_folder = Path(metadata_folder)
        self._registry: dict[str, JSONValue] = {}
        self._validators: dict[str, JSONValue] = {}
        self._ai_apps: dict[str, JSONDict] = {}
        self._schemas: dict[str, JSONDict] = {}
        self._initialized = False

    def initialize(self) -> bool:
        import logging

        logging.getLogger(__name__)

        if self._initialized:
            return True

        self._load_schemas()

        self._load_ai_apps()

        self._initialized = True
        return True

    def _load_schemas(self) -> None:
        import logging

        logging.getLogger(__name__)

        schemas_path = self.metadata_folder / "schemas"

        if not schemas_path.exists():
            return

        schema_files = list(schemas_path.glob("*.json"))

        for schema_file in schema_files:
            try:
                with open(schema_file, encoding="utf-8") as f:
                    schema = json.load(f)
                    schema_name = schema_file.stem
                    self._schemas[schema_name] = schema
            except Exception as e:
                raise FrameworkError(f"Failed to load schema {schema_file}: {e}") from e

    def _load_ai_apps(self) -> None:
        import logging

        logging.getLogger(__name__)

        apps_path = self.metadata_folder / "apps"

        if not apps_path.exists():
            return

        app_files = list(apps_path.glob("*.json"))

        for app_file in app_files:
            try:
                with open(app_file, encoding="utf-8") as f:
                    app_definition = json.load(f)
                    app_id = app_definition.get("app_id", app_file.stem)
                    self._ai_apps[app_id] = app_definition
            except Exception as e:
                raise FrameworkError(f"Failed to load AI App {app_file}: {e}") from e

    def get_ai_app_metadata(self, ai_app_id: str) -> JSONDict | None:
        if not self._initialized:
            self.initialize()

        return self._ai_apps.get(ai_app_id)

    def get_action_schema(self, ai_app_id: str, action_name: str) -> JSONDict | None:
        app_metadata = self.get_ai_app_metadata(ai_app_id)
        if not app_metadata:
            return None

        custom_objects = app_metadata.get("custom_objects", {})
        if not isinstance(custom_objects, dict):
            return None

        for _obj_name, obj_def in custom_objects.items():
            if not isinstance(obj_def, dict):
                continue
            actions = obj_def.get("actions", {})
            if not isinstance(actions, dict):
                continue
            action = actions.get(action_name)
            if action is not None and isinstance(action, dict):
                # Ensure all values in the dict are JSONValue compatible
                result: JSONDict = dict(action.items())
                return result

        return None

    def get_object_schema(self, ai_app_id: str, object_name: str) -> JSONDict | None:
        app_metadata = self.get_ai_app_metadata(ai_app_id)
        if not app_metadata:
            return None

        custom_objects = app_metadata.get("custom_objects", {})
        if not isinstance(custom_objects, dict):
            return None

        obj_schema = custom_objects.get(object_name)
        if obj_schema is not None and isinstance(obj_schema, dict):
            result: JSONDict = dict(obj_schema.items())
            return result

        return None

    def llm_function_specs(self, ai_app_id: str | None = None) -> list[JSONDict]:
        if not self._initialized:
            self.initialize()

        apps_to_process = self._get_apps_to_process(ai_app_id)
        return self._collect_function_specs(apps_to_process)

    def _get_apps_to_process(self, ai_app_id: str | None) -> dict[str, JSONDict]:
        """Get apps to process based on optional filter."""
        if ai_app_id:
            app_metadata = self.get_ai_app_metadata(ai_app_id)
            if app_metadata:
                return {ai_app_id: app_metadata}
            return {}
        return self._ai_apps

    def _collect_function_specs(self, apps: dict[str, JSONDict]) -> list[JSONDict]:
        """Collect function specs from all apps."""
        specs: list[JSONDict] = []
        for app_id, app_metadata in apps.items():
            specs.extend(self._extract_specs_from_app(app_id, app_metadata))
        return specs

    def _extract_specs_from_app(self, app_id: str, app_metadata: JSONDict) -> list[JSONDict]:
        """Extract function specs from a single app."""
        specs: list[JSONDict] = []
        custom_objects = app_metadata.get("custom_objects", {})
        if not isinstance(custom_objects, dict):
            return specs

        for obj_name, obj_def in custom_objects.items():
            specs.extend(self._extract_specs_from_object(app_id, obj_name, obj_def))
        return specs

    def _extract_specs_from_object(
        self, app_id: str, obj_name: str, obj_def: JSONValue
    ) -> list[JSONDict]:
        """Extract function specs from a single object definition."""
        if not isinstance(obj_def, dict):
            return []

        actions = obj_def.get("actions", {})
        if not isinstance(actions, dict):
            return []

        specs: list[JSONDict] = []
        for action_name, action_def in actions.items():
            if isinstance(action_def, dict):
                spec = self._generate_function_spec(app_id, obj_name, action_name, action_def)
                specs.append(spec)
        return specs

    def _generate_function_spec(
        self, app_id: str, obj_name: str, action_name: str, action_def: JSONDict
    ) -> JSONDict:
        """Generate OpenAI function spec for an action."""
        properties, required = self._parse_parameters(action_def.get("parameters", []))
        description = self._get_description(action_def, action_name, obj_name)

        return self._build_function_spec(
            app_id, obj_name, action_name, description, properties, required
        )

    def _parse_parameters(self, parameters: JSONValue) -> tuple[dict[str, JSONDict], list[str]]:
        """Parse parameters into properties and required lists."""
        properties: dict[str, JSONDict] = {}
        required: list[str] = []

        if not isinstance(parameters, list):
            return properties, required

        for param in parameters:
            self._process_parameter(param, properties, required)

        return properties, required

    def _process_parameter(
        self, param: JSONValue, properties: dict[str, JSONDict], required: list[str]
    ) -> None:
        """Process a single parameter definition."""
        if isinstance(param, str):
            properties[param] = {"type": "string"}
            required.append(param)
        elif isinstance(param, dict):
            param_name = param.get("name", "unknown")
            if not isinstance(param_name, str):
                param_name = "unknown"
            param_type = param.get("type", "string")
            if not isinstance(param_type, str):
                param_type = "string"
            properties[param_name] = {"type": param_type}
            if param.get("required", True) is True:
                required.append(param_name)

    def _get_description(self, action_def: JSONDict, action_name: str, obj_name: str) -> str:
        """Get description from action definition or generate default."""
        description = action_def.get("description", f"Execute {action_name} on {obj_name}")
        if isinstance(description, str):
            return description
        return f"Execute {action_name} on {obj_name}"

    def _build_function_spec(
        self,
        app_id: str,
        obj_name: str,
        action_name: str,
        description: str,
        properties: dict[str, JSONDict],
        required: list[str],
    ) -> JSONDict:
        """Build the final function spec dict."""
        properties_value: dict[str, JSONValue] = dict(properties.items())
        required_value: list[JSONValue] = list(required)

        parameters_value: dict[str, JSONValue] = {
            "type": "object",
            "properties": properties_value,
            "required": required_value,
        }

        return {
            "name": f"{app_id}_{obj_name}_{action_name}",
            "description": description,
            "parameters": parameters_value,
        }

    def validate_metadata(self, metadata: JSONDict, schema_name: str) -> JSONDict:
        if not self._initialized:
            self.initialize()

        schema = self._schemas.get(schema_name)
        if not schema:
            return {"valid": False, "errors": [f"Schema '{schema_name}' not found"]}

        try:
            import jsonschema
            from jsonschema import ValidationError
        except ImportError:
            return {"valid": False, "errors": ["jsonschema library not available for validation"]}

        try:
            jsonschema.validate(metadata, schema)
            return {"valid": True, "errors": []}
        except ValidationError as e:
            return {"valid": False, "errors": [str(e)]}

    def register_ai_app_metadata(self, ai_app_id: str, metadata: JSONDict) -> bool:
        if not self._initialized:
            self.initialize()

        validation_result = self.validate_metadata(metadata, "ai_app")
        valid = validation_result.get("valid")
        if not (isinstance(valid, bool) and valid):
            errors = validation_result.get("errors", [])
            raise FrameworkError(f"Invalid AI App metadata: {errors}")

        self._ai_apps[ai_app_id] = metadata
        return True

    def hot_reconfigure_ai_app(self, ai_app_id: str, new_metadata: JSONDict) -> bool:
        if not self._initialized:
            self.initialize()

        validation_result = self.validate_metadata(new_metadata, "ai_app")
        valid = validation_result.get("valid")
        if not (isinstance(valid, bool) and valid):
            errors = validation_result.get("errors", [])
            raise FrameworkError(f"Invalid AI App metadata for hot reconfiguration: {errors}")

        self._ai_apps[ai_app_id] = new_metadata
        return True

    def list_ai_apps(self) -> list[str]:
        if not self._initialized:
            self.initialize()
        return list(self._ai_apps.keys())

    def get_schema_names(self) -> list[str]:
        if not self._initialized:
            self.initialize()
        return list(self._schemas.keys())

    def get_template_patterns(self) -> JSONDict | None:
        if not self._initialized:
            self.initialize()

        return self._schemas.get("template_patterns")
