import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


# Type definitions for JSON data structures
# Use TypeAlias for dict-based structures that come from JSON
type ActionData = dict[str, object]


class ValidationResult(TypedDict):
    valid: bool
    errors: list[str]
    warnings: list[str]
    structure_check: dict[str, bool]


class ManifestSummary(TypedDict):
    app_id: str
    version: str
    name: str


class AppSummary(TypedDict):
    manifest: ManifestSummary
    actions_count: int
    capabilities_count: int
    dependencies_count: int
    initialized: bool


@dataclass
class AppManifest:
    app_id: str
    version: str
    name: str
    description: str
    author: str | None = None


@dataclass
class AppCapability:
    name: str
    description: str
    category: str
    parameters: list[str]


@dataclass
class AppDependency:
    plugin_id: str
    min_version: str | None = None
    required: bool = True


class AppMetadataManager:
    def __init__(self, app_path: str):
        self.app_path = Path(app_path)
        self._manifest: AppManifest | None = None
        self._actions: dict[str, ActionData] = {}
        self._capabilities: list[AppCapability] = []
        self._dependencies: list[AppDependency] = []
        self._initialized = False

    def initialize(self) -> bool:
        if self._initialized:
            return True

        try:
            if not self.app_path.exists():
                return False

            self._load_manifest()

            self._load_actions()

            self._load_capabilities()

            self._load_dependencies()

            self._initialized = True
            return True

        except Exception:
            return False

    def _load_manifest(self) -> None:
        manifest_path = self.app_path / "manifest.json"

        if not manifest_path.exists():
            self._create_default_manifest()
            return

        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest_data = json.load(f)

            self._manifest = AppManifest(
                app_id=manifest_data.get("app_id", "unknown_app"),
                version=manifest_data.get("version", "1.0.0"),
                name=manifest_data.get("name", "Unknown App"),
                description=manifest_data.get("description", "No description provided"),
                author=manifest_data.get("author"),
            )

        except Exception as e:
            raise FrameworkError(f"Failed to load app manifest: {e}") from e

    def _create_default_manifest(self) -> None:
        app_name = self.app_path.parent.name

        self._manifest = AppManifest(
            app_id=app_name,
            version="1.0.0",
            name=app_name.replace("_", " ").title(),
            description="AI App created from existing actions",
        )

    def _load_actions(self) -> None:
        actions_path = self.app_path / "actions"

        if not actions_path.exists():
            return

        action_count = 0
        for action_file in actions_path.glob("*.json"):
            try:
                with open(action_file, encoding="utf-8") as f:
                    action_data_raw: object = json.load(f)

                # Type narrowing: ensure we have a dict before using as ActionData
                if isinstance(action_data_raw, dict):
                    action_name = action_file.stem
                    self._actions[action_name] = action_data_raw
                    action_count += 1

                else:
                    pass

            except Exception:
                pass

    def _load_capabilities(self) -> None:
        capabilities_path = self.app_path / "capabilities.json"

        if not capabilities_path.exists():
            self._infer_capabilities_from_actions()
            return

        try:
            with open(capabilities_path, encoding="utf-8") as f:
                capabilities_data = json.load(f)

            # Handle both list format and dict with "capabilities" key
            if isinstance(capabilities_data, list):
                caps_list = capabilities_data
            else:
                caps_list = capabilities_data.get("capabilities", [])

            for cap_data in caps_list:
                capability = AppCapability(
                    name=cap_data.get("name", "unknown"),
                    description=cap_data.get("description", ""),
                    category=cap_data.get("category", "general"),
                    parameters=cap_data.get("parameters", []),
                )
                self._capabilities.append(capability)

        except Exception:
            self._infer_capabilities_from_actions()

    def _infer_capabilities_from_actions(self) -> None:
        for action_name, action_data in self._actions.items():
            description = action_data.get("description", f"Execute {action_name} action")
            description_str = (
                description if isinstance(description, str) else f"Execute {action_name} action"
            )

            parameters_data = action_data.get("parameters", {})
            parameter_keys = (
                list(parameters_data.keys()) if isinstance(parameters_data, dict) else []
            )

            capability = AppCapability(
                name=action_name,
                description=description_str,
                category="action",
                parameters=parameter_keys,
            )
            self._capabilities.append(capability)

    def _load_dependencies(self) -> None:
        dependencies_path = self.app_path / "dependencies.json"

        if not dependencies_path.exists():
            self._infer_dependencies_from_actions()
            return

        try:
            with open(dependencies_path, encoding="utf-8") as f:
                dependencies_data = json.load(f)

            # Handle both list format and dict with "dependencies" key
            if isinstance(dependencies_data, list):
                deps_list = dependencies_data
            else:
                deps_list = dependencies_data.get("dependencies", [])

            for dep_data in deps_list:
                dependency = AppDependency(
                    plugin_id=dep_data.get("plugin_id", "unknown"),
                    min_version=dep_data.get("min_version"),
                    required=dep_data.get("required", True),
                )
                self._dependencies.append(dependency)

        except Exception:
            self._infer_dependencies_from_actions()

    def _infer_dependencies_from_actions(self) -> None:
        plugin_references: set[str] = set()

        for _action_name, action_data in self._actions.items():
            process = action_data.get("process", {})
            if isinstance(process, dict):
                plugin_name = process.get("plugin")

                if isinstance(plugin_name, str) and plugin_name != "core":
                    plugin_references.add(plugin_name)

        for plugin_name in plugin_references:
            dependency = AppDependency(plugin_id=plugin_name, required=True)
            self._dependencies.append(dependency)

    def get_manifest(self) -> AppManifest | None:
        if not self._initialized:
            self.initialize()
        return self._manifest

    def get_actions(self) -> dict[str, ActionData]:
        if not self._initialized:
            self.initialize()
        return self._actions.copy()

    def get_action(self, action_name: str) -> ActionData | None:
        if not self._initialized:
            self.initialize()
        return self._actions.get(action_name)

    def get_capabilities(self) -> list[AppCapability]:
        if not self._initialized:
            self.initialize()
        return self._capabilities.copy()

    def get_dependencies(self) -> list[AppDependency]:
        if not self._initialized:
            self.initialize()
        return self._dependencies.copy()

    def validate_app_structure(self) -> ValidationResult:
        if not self._initialized:
            self.initialize()

        errors: list[str] = []
        warnings: list[str] = []
        structure_check: dict[str, bool] = {}

        required_structure = {
            "app": self.app_path,
            "actions": self.app_path / "actions",
            "manifest.json": self.app_path / "manifest.json",
        }

        valid = True
        for name, path in required_structure.items():
            exists = path.exists()
            structure_check[name] = exists

            if not exists:
                if name == "manifest.json":
                    warnings.append(f"Missing {name} - using default")
                else:
                    errors.append(f"Missing required {name}")
                    valid = False

        validation_result: ValidationResult = {
            "valid": valid,
            "errors": errors,
            "warnings": warnings,
            "structure_check": structure_check,
        }

        return validation_result

    def get_summary(self) -> AppSummary:
        if not self._initialized:
            self.initialize()

        manifest_summary: ManifestSummary = {
            "app_id": self._manifest.app_id if self._manifest else "unknown",
            "version": self._manifest.version if self._manifest else "unknown",
            "name": self._manifest.name if self._manifest else "unknown",
        }

        return {
            "manifest": manifest_summary,
            "actions_count": len(self._actions),
            "capabilities_count": len(self._capabilities),
            "dependencies_count": len(self._dependencies),
            "initialized": self._initialized,
        }
