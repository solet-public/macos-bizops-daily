import logging
from dataclasses import dataclass
from enum import Enum

from ananta.error_handling import FrameworkError

from .app_metadata_manager import AppMetadataManager
from .metadata_registry import MetadataRegistry
from .plugin_metadata_manager import PluginMetadataManager

logger = logging.getLogger(__name__)


class MetadataLayer(Enum):
    PLATFORM = "platform"
    PLUGIN = "plugin"
    APP = "app"


@dataclass
class MetadataRequest:
    layer: MetadataLayer | None = None
    resource_type: str = "action"  # "action", "schema", "capability", etc.
    resource_id: str = ""
    parameters: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.parameters is None:
            self.parameters = {}


@dataclass
class MetadataResponse:
    found: bool
    layer: MetadataLayer | None
    data: object
    source: str
    metadata: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


class UnifiedMetadataRegistry:
    def __init__(self, platform_path: str, plugins_path: str, app_path: str):
        self.platform_path = platform_path
        self.plugins_path = plugins_path
        self.app_path = app_path

        self._platform_metadata: MetadataRegistry | None = None
        self._plugin_metadata: PluginMetadataManager | None = None
        self._app_metadata: AppMetadataManager | None = None

        self._initialized = False
        self._layer_initialization_results: dict[str, bool] = {
            "platform": False,
            "plugin": False,
            "app": False,
        }
        self._layer_precedence = [MetadataLayer.APP, MetadataLayer.PLUGIN, MetadataLayer.PLATFORM]

    def initialize(self) -> bool:
        if self._initialized:
            return True

        self._layer_initialization_results = {"platform": False, "plugin": False, "app": False}
        try:
            # CRITICAL FIX: platform_path contains ".../schemas" but MetadataRegistry adds "/schemas" internally
            # Remove trailing "/schemas" to prevent double schemas directory
            platform_metadata_path = str(self.platform_path)
            if platform_metadata_path.endswith("/schemas"):
                platform_metadata_path = platform_metadata_path[:-8]  # Remove "/schemas"
            elif platform_metadata_path.endswith("\\schemas"):
                platform_metadata_path = platform_metadata_path[:-8]  # Remove "\schemas" on Windows
            self._platform_metadata = MetadataRegistry(platform_metadata_path)
            platform_success = self._platform_metadata.initialize()
            self._layer_initialization_results["platform"] = platform_success

            self._plugin_metadata = PluginMetadataManager(self.plugins_path)
            plugin_success = self._plugin_metadata.initialize()
            self._layer_initialization_results["plugin"] = plugin_success

            self._app_metadata = AppMetadataManager(self.app_path)
            app_success = self._app_metadata.initialize()
            self._layer_initialization_results["app"] = app_success

            initialization_results = {
                "platform": platform_success,
                "plugin": plugin_success,
                "app": app_success,
            }

            overall_success = all(initialization_results.values())

            if overall_success:
                self._initialized = True
            else:
                self._initialized = False

            return overall_success

        except Exception:
            self._initialized = False
            logger.exception(
                "Unified metadata registry initialization raised; layer_results=%s",
                self._layer_initialization_results,
            )
            return False

    def get_initialization_results(self) -> dict[str, bool]:
        """Return the latest per-layer initialization results."""
        return self._layer_initialization_results.copy()

    def resolve_metadata(self, request: MetadataRequest) -> MetadataResponse:
        if not self._initialized:
            self.initialize()

        if request.layer:
            layers_to_search = [request.layer]
        else:
            layers_to_search = self._layer_precedence

        for layer in layers_to_search:
            try:
                response = self._search_layer(layer, request)

                if response.found:
                    return response

            except Exception:
                pass

        return MetadataResponse(found=False, layer=None, data=None, source="not_found")

    def _search_layer(self, layer: MetadataLayer, request: MetadataRequest) -> MetadataResponse:
        if layer == MetadataLayer.PLATFORM:
            return self._search_platform_layer(request)
        elif layer == MetadataLayer.PLUGIN:
            return self._search_plugin_layer(request)
        elif layer == MetadataLayer.APP:
            return self._search_app_layer(request)
        else:
            raise FrameworkError(f"Unknown metadata layer: {layer}")

    def _search_platform_layer(self, request: MetadataRequest) -> MetadataResponse:
        if request.resource_type == "action":
            return MetadataResponse(
                found=False, layer=MetadataLayer.PLATFORM, data=None, source="platform_registry"
            )

        return MetadataResponse(
            found=False, layer=MetadataLayer.PLATFORM, data=None, source="platform_registry"
        )

    def _search_plugin_layer(self, request: MetadataRequest) -> MetadataResponse:
        if request.resource_type == "capability":
            if self._plugin_metadata is None:
                return MetadataResponse(
                    found=False, layer=None, data=None, source="plugin_metadata_unavailable"
                )
            matching_plugins = self._plugin_metadata.get_plugins_providing_capability(
                request.resource_id
            )
            if matching_plugins:
                return MetadataResponse(
                    found=True,
                    layer=MetadataLayer.PLUGIN,
                    data=matching_plugins,
                    source="plugin_manager",
                    metadata={"plugin_count": len(matching_plugins)},
                )

        elif request.resource_type == "plugin":
            if self._plugin_metadata is None:
                return MetadataResponse(
                    found=False, layer=None, data=None, source="plugin_metadata_unavailable"
                )
            plugin = self._plugin_metadata.get_plugin(request.resource_id)
            if plugin:
                return MetadataResponse(
                    found=True, layer=MetadataLayer.PLUGIN, data=plugin, source="plugin_manager"
                )

        return MetadataResponse(
            found=False, layer=MetadataLayer.PLUGIN, data=None, source="plugin_manager"
        )

    def _search_app_layer(self, request: MetadataRequest) -> MetadataResponse:
        if request.resource_type == "action":
            if self._app_metadata is None:
                return MetadataResponse(
                    found=False, layer=None, data=None, source="app_metadata_unavailable"
                )
            action_data = self._app_metadata.get_action(request.resource_id)
            if action_data:
                return MetadataResponse(
                    found=True, layer=MetadataLayer.APP, data=action_data, source="app_manager"
                )

        elif request.resource_type == "manifest":
            if self._app_metadata is None:
                return MetadataResponse(
                    found=False, layer=None, data=None, source="app_metadata_unavailable"
                )
            manifest = self._app_metadata.get_manifest()
            if manifest:
                return MetadataResponse(
                    found=True, layer=MetadataLayer.APP, data=manifest, source="app_manager"
                )

        elif request.resource_type == "capabilities":
            if self._app_metadata is None:
                return MetadataResponse(
                    found=False, layer=None, data=None, source="app_metadata_unavailable"
                )
            capabilities = self._app_metadata.get_capabilities()
            return MetadataResponse(
                found=True,
                layer=MetadataLayer.APP,
                data=capabilities,
                source="app_manager",
                metadata={"capability_count": len(capabilities)},
            )

        return MetadataResponse(
            found=False, layer=MetadataLayer.APP, data=None, source="app_manager"
        )

    def validate_complete_system(self) -> dict[str, object]:
        if not self._initialized:
            self.initialize()

        errors_list: list[object] = []
        warnings_list: list[object] = []
        layer_status_dict: dict[str, object] = {}
        integration_checks_dict: dict[str, object] = {}

        validation_result: dict[str, object] = {
            "valid": True,
            "errors": errors_list,
            "warnings": warnings_list,
            "layer_status": layer_status_dict,
            "dependency_issues": [],
            "integration_checks": integration_checks_dict,
        }

        self._populate_layer_status(layer_status_dict)
        self._validate_plugin_layer(validation_result, errors_list)
        self._validate_app_layer(validation_result, errors_list, warnings_list)
        self._check_integration_dependencies(
            validation_result, errors_list, warnings_list, integration_checks_dict
        )

        return validation_result

    def _populate_layer_status(self, layer_status: dict[str, object]) -> None:
        """Populate initialization status for each layer."""
        for layer_name, initialized in self._layer_initialization_results.items():
            layer_status[layer_name] = {"initialized": initialized}

    def _validate_plugin_layer(self, result: dict[str, object], errors: list[object]) -> None:
        """Validate plugin layer dependencies."""
        if not self._plugin_metadata:
            return

        plugin_validation = self._plugin_metadata.validate_plugin_dependencies()
        result["dependency_issues"] = plugin_validation.get("errors", [])

        plugin_valid = plugin_validation.get("valid", True)
        if not plugin_valid:
            result["valid"] = False
            plugin_errors = plugin_validation.get("errors", [])
            errors.extend(plugin_errors)

    def _validate_app_layer(
        self, result: dict[str, object], errors: list[object], warnings: list[object]
    ) -> None:
        """Validate app layer structure."""
        if not self._app_metadata:
            return

        app_validation = self._app_metadata.validate_app_structure()

        app_valid = app_validation.get("valid", True)
        if not app_valid:
            result["valid"] = False
            app_errors = app_validation.get("errors", [])
            errors.extend(app_errors)

        app_warnings = app_validation.get("warnings", [])
        warnings.extend(app_warnings)

    def _check_integration_dependencies(
        self,
        result: dict[str, object],
        errors: list[object],
        warnings: list[object],
        integration_checks: dict[str, object],
    ) -> None:
        """Check app dependencies against available plugins."""
        app_dependencies = self._app_metadata.get_dependencies() if self._app_metadata else []
        available_plugins = (
            self._plugin_metadata.get_discovered_plugins() if self._plugin_metadata else {}
        )

        for dependency in app_dependencies:
            is_available = dependency.plugin_id in available_plugins
            integration_checks[dependency.plugin_id] = {
                "available": is_available,
                "required": dependency.required,
            }

            if not is_available:
                if dependency.required:
                    result["valid"] = False
                    errors.append(f"Required plugin not available: {dependency.plugin_id}")
                else:
                    warnings.append(f"Optional plugin not available: {dependency.plugin_id}")

    def get_unified_summary(self) -> dict[str, object]:
        if not self._initialized:
            self.initialize()

        layers_dict: dict[str, object] = {}
        summary: dict[str, object] = {
            "unified_registry": {
                "initialized": self._initialized,
                "layer_precedence": [layer.value for layer in self._layer_precedence],
            },
            "layers": layers_dict,
        }

        if self._platform_metadata:
            layers_dict["platform"] = {
                "type": "MetadataRegistry",
                "path": self.platform_path,
                "schemas": len(self._platform_metadata.get_schema_names()),
            }

        if self._plugin_metadata:
            layers_dict["plugin"] = self._plugin_metadata.get_summary()

        if self._app_metadata:
            layers_dict["app"] = self._app_metadata.get_summary()

        return summary

    def get_layer_manager(
        self, layer: MetadataLayer
    ) -> MetadataRegistry | PluginMetadataManager | AppMetadataManager | None:
        if not self._initialized:
            self.initialize()

        if layer == MetadataLayer.PLATFORM:
            return self._platform_metadata
        elif layer == MetadataLayer.PLUGIN:
            return self._plugin_metadata
        else:  # layer == MetadataLayer.APP
            return self._app_metadata

    def resolve_action_with_precedence(self, action_name: str) -> MetadataResponse:
        request = MetadataRequest(resource_type="action", resource_id=action_name)

        return self.resolve_metadata(request)

    def get_all_available_capabilities(self) -> dict[str, list[object]]:
        if not self._initialized:
            self.initialize()

        app_caps_list: list[object] = []
        plugin_caps_list: list[object] = []
        platform_caps_list: list[object] = []

        all_capabilities: dict[str, list[object]] = {
            "app": app_caps_list,
            "plugin": plugin_caps_list,
            "platform": platform_caps_list,
        }

        if self._app_metadata:
            app_capabilities = self._app_metadata.get_capabilities()
            app_caps_list.extend(app_capabilities)

        if self._plugin_metadata:
            for plugin_package in self._plugin_metadata.get_discovered_plugins().values():
                plugin_caps_list.extend(plugin_package.capabilities)

        return all_capabilities

    def get_template_patterns(self) -> dict[str, object] | None:
        if not self._initialized:
            self.initialize()

        if self._platform_metadata:
            template_patterns = self._platform_metadata.get_template_patterns()
            if template_patterns:
                # Cast JSONDict to dict[str, object] for return type compatibility
                return dict(template_patterns)
            else:
                pass
        else:
            pass

        return None
