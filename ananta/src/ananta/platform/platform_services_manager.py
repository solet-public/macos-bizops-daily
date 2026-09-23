import logging
import os
from pathlib import Path

from ananta.core.orchestration.feature_flags import OrchestrationFeatureFlags

from .code_generator import CodeGenerator
from .json_schema_validator import JSONSchemaValidator
from .metadata_registry import MetadataRegistry
from .unified_metadata_registry import UnifiedMetadataRegistry

logger = logging.getLogger(__name__)


class PlatformServicesManager:
    def __init__(self, metadata_folder: str | None = None, output_folder: str | None = None):
        self.metadata_folder = metadata_folder
        self.output_folder = output_folder

        self._metadata_registry: MetadataRegistry | None = None
        self._json_validator: JSONSchemaValidator | None = None
        self._code_generator: CodeGenerator | None = None
        self._initialized = False

        # Step 3: Create UnifiedMetadataRegistry object but don't call initialize()
        self._unified_metadata_registry: UnifiedMetadataRegistry | None = None
        self._unified_metadata_registry_status: dict[str, object] = {
            "enabled": False,
            "initialized": None,
            "layer_results": {},
        }

    def initialize(self) -> bool:
        try:
            self._initialize_unified_metadata_registry()
            self._initialize_optional_services()
            self._initialized = True
            return True
        except Exception:
            # Surface the REAL cause with its stack. Swallowing it here left
            # callers raising a misleading feature-flag error while the actual
            # failure (e.g. a missing profile/app dir) was invisible.
            logger.exception("PlatformServicesManager.initialize() failed")
            return False

    def _initialize_unified_metadata_registry(self) -> None:
        """Initialize UnifiedMetadataRegistry if enabled."""
        new_metadata_system = os.getenv("ANANTA_USE_NEW_METADATA_SYSTEM", "true")
        if new_metadata_system.lower() != "true":
            return

        self._unified_metadata_registry_status["enabled"] = True

        app_home_path = self._get_app_home_path()
        platform_path = str(Path(__file__).parent / "metadata")
        plugins_path = self._get_plugins_path()
        app_path = self._get_app_path(app_home_path)

        self._unified_metadata_registry = UnifiedMetadataRegistry(
            platform_path=platform_path,
            plugins_path=plugins_path,
            app_path=app_path,
        )
        registry_initialized = self._unified_metadata_registry.initialize()
        layer_results = self._unified_metadata_registry.get_initialization_results()
        self._unified_metadata_registry_status = {
            "enabled": True,
            "initialized": registry_initialized,
            "layer_results": layer_results,
        }
        if not registry_initialized:
            logger.error(
                "Unified metadata registry initialization failed; layer_results=%s",
                layer_results,
            )

    def _get_app_home_path(self) -> Path:
        """Get and validate APP_HOME path."""
        app_home = os.environ.get("APP_HOME")
        if not app_home:
            raise RuntimeError("APP_HOME environment variable is required but not set")

        app_home_path = Path(app_home)
        if not app_home_path.exists():
            raise RuntimeError(f"APP_HOME directory does not exist: {app_home}")
        return app_home_path

    def _get_plugins_path(self) -> str:
        """Get plugins path from env var or derive from package location."""
        plugins_path_env = os.environ.get("ANANTA_PLUGINS_PATH")
        if plugins_path_env:
            if not Path(plugins_path_env).exists():
                raise RuntimeError(f"Plugins directory does not exist: {plugins_path_env}")
            return plugins_path_env

        ananta_package_dir = Path(__file__).parent.parent.parent.parent.parent
        for candidate in ["plugins", "ananta_plugins"]:
            candidate_path = ananta_package_dir / candidate
            if candidate_path.exists():
                return str(candidate_path)

        raise RuntimeError(
            f"Plugins directory not found. Set ANANTA_PLUGINS_PATH or ensure "
            f"'plugins' or 'ananta_plugins' exists at {ananta_package_dir}"
        )

    def _get_app_path(self, app_home_path: Path) -> str:
        """Get and validate app path."""
        app_path = str(app_home_path / "app")
        if not Path(app_path).exists():
            raise RuntimeError(f"App directory not found at {app_path}")
        return app_path

    def _initialize_optional_services(self) -> None:
        """Initialize optional services (metadata registry, validator, generator)."""
        if OrchestrationFeatureFlags.use_metadata_registry():
            self._initialize_metadata_registry()
        self._initialize_json_validator()
        self._initialize_code_generator()

    def _initialize_metadata_registry(self) -> None:
        try:
            if self.metadata_folder:
                metadata_path = self.metadata_folder
            else:
                metadata_path = str(Path.cwd() / "metadata")

            self._metadata_registry = MetadataRegistry(metadata_path)

        except Exception:
            raise

    def _initialize_json_validator(self) -> None:
        try:
            if self.metadata_folder:
                schema_path = str(Path(self.metadata_folder) / "schemas")
            else:
                schema_path = str(Path.cwd() / "metadata" / "schemas")

            self._json_validator = JSONSchemaValidator(schema_path)

        except Exception:
            raise

    def _initialize_code_generator(self) -> None:
        try:
            if self.output_folder:
                output_path = self.output_folder
            else:
                output_path = str(Path.cwd() / "generated")

            self._code_generator = CodeGenerator(output_path)

        except Exception:
            raise

    @property
    def metadata_registry(self) -> MetadataRegistry | None:
        return self._metadata_registry

    @property
    def unified_metadata_registry(self) -> UnifiedMetadataRegistry | None:
        return self._unified_metadata_registry

    @property
    def json_validator(self) -> JSONSchemaValidator | None:
        return self._json_validator

    @property
    def code_generator(self) -> CodeGenerator | None:
        return self._code_generator

    def is_initialized(self) -> bool:
        return self._initialized

    def get_service_status(self) -> dict[str, object]:
        return {
            "initialized": self._initialized,
            "metadata_registry_enabled": OrchestrationFeatureFlags.use_metadata_registry(),
            "metadata_registry_available": self._metadata_registry is not None,
            "json_validator_available": self._json_validator is not None,
            "code_generator_available": self._code_generator is not None,
            "unified_metadata_registry": self._unified_metadata_registry_status.copy(),
            "feature_flags": {
                "use_metadata_registry": OrchestrationFeatureFlags.use_metadata_registry(),
                "use_new_template_engine": OrchestrationFeatureFlags.use_new_template_engine(),
                "use_system_platform_manager": OrchestrationFeatureFlags.use_system_platform_manager(),
                "use_service_coordinator": OrchestrationFeatureFlags.use_service_coordinator(),
            },
        }

    def validate_platform_readiness(self) -> bool:
        if not self._initialized:
            return False

        # Basic readiness checks
        if self._json_validator is None:
            return False

        if self._code_generator is None:
            return False

        # MetadataRegistry is optional based on feature flag
        if OrchestrationFeatureFlags.use_metadata_registry() and self._metadata_registry is None:
            return False

        return True
