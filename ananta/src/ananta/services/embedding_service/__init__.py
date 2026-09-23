"""EmbeddingService - Service wrapper for embedding generation operations.

This service provides a stable interface for embedding generation, allowing the underlying
embedding provider plugin to be swapped without breaking consumer code.

Bootstrap Mode: NOT SUPPORTED (embeddings not needed during system startup)
Plugin Mode: Wraps local_embeddings_plugin (or configured alternative via env)
"""

import logging
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from ananta.constants import DEFAULT_EMBEDDING_PLUGIN as DEFAULT_EMBEDDING_PLUGIN
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.error_handling import FrameworkError
from ananta.interfaces.bootstrappable_service_interface import BootstrappableServiceInterface
from ananta.interfaces.embedding_service_interface import EmbeddingServiceInterface
from ananta.services.embedding_service.interfaces.public import EmbeddingServiceAPI

if TYPE_CHECKING:
    from ananta.core.plugins.plugin_manager import PluginManager

logger = logging.getLogger(__name__)


@runtime_checkable
class InitializablePluginProtocol(Protocol):
    """Protocol for plugins that support deferred initialization.

    Some embedding plugins require explicit initialization with configuration
    before they can be used. This protocol captures those optional capabilities.
    """

    _initialized: bool

    def initialize(self, config: dict[str, object]) -> None:
        """Initialize the plugin with configuration."""
        ...

    def set_as_active_provider(self, interface_name: str) -> None:
        """Notify plugin it's the active provider for an interface."""
        ...


class EmbeddingService(EmbeddingServiceAPI, BootstrappableServiceInterface):
    """Service wrapper for embedding plugin providers.

    Provides stable interface for embedding generation operations, enabling provider
    swapping (local models, API services, etc.) without breaking consumer code.

    This is a "simple wrapper" - no bootstrap mode, no complex business logic,
    just provider abstraction for swappability.
    """

    # Override plugin_manager with proper type annotation
    plugin_manager: PluginManager

    def __init__(
        self, plugin_manager: PluginManager | None = None, embedding_plugin_name: str | None = None
    ):
        """Initialize EmbeddingService.

        Args:
            plugin_manager: Plugin manager instance (REQUIRED)
            embedding_plugin_name: Override plugin name (default: from constants)

        Raises:
            FrameworkError: If plugin_manager is None
        """
        if plugin_manager is None:
            raise FrameworkError(
                "EmbeddingService requires plugin_manager. "
                "Bootstrap mode not supported for embedding operations."
            )

        # Validation: plugin_manager is not None, so embedding_plugin_name must be provided
        if embedding_plugin_name is None:
            # Try environment variable set by launch script
            import os

            embedding_plugin_name = os.environ.get("ANANTA_EMBEDDING_PLUGIN")

        if embedding_plugin_name is None:
            raise ValueError(
                "embedding_plugin_name must be provided when using plugin mode. "
                "Set ANANTA_EMBEDDING_PLUGIN environment variable or pass embedding_plugin_name parameter."
            )

        # NO FALLBACK - fail fast if not provided
        # In bootstrap mode (plugin_manager=None), embedding_plugin_name can be None
        self._embedding_plugin_name = embedding_plugin_name
        self._embedding_plugin: EmbeddingServiceInterface | None = None

        # Initialize via BootstrappableServiceInterface pattern
        super().__init__(plugin_manager)

    def _init_bootstrap(self) -> None:
        """Bootstrap mode not supported for embedding service.

        Raises:
            FrameworkError: Always (bootstrap mode not supported)
        """
        raise FrameworkError(
            "EmbeddingService does not support bootstrap mode. "
            "Embedding operations require plugin provider."
        )

    def _init_plugin(self) -> None:
        """Initialize plugin mode - validation deferred until first use."""
        logger.debug(f"EmbeddingService initializing with plugin: {self._embedding_plugin_name}")

    def _validate_embedding_plugin(self) -> EmbeddingServiceInterface:
        """Validate that embedding plugin exists and is available.

        Initializes the plugin if it has an initialize() method and hasn't been initialized yet.

        Returns:
            The embedding plugin typed as EmbeddingServiceInterface

        Raises:
            FrameworkError: If plugin not found, doesn't implement interface, or initialization fails
        """
        if self._embedding_plugin is None:
            plugin = self.plugin_manager.get_plugin(self._embedding_plugin_name)

            if not plugin:
                raise FrameworkError(
                    f"Embedding plugin '{self._embedding_plugin_name}' not found. "
                    f"Ensure plugin is installed and configured."
                )

            if not isinstance(plugin, EmbeddingServiceInterface):
                raise FrameworkError(
                    f"Embedding plugin '{self._embedding_plugin_name}' does not implement EmbeddingServiceInterface. "
                    f"Plugin type: {type(plugin)}"
                )

            # Initialize plugin if it supports deferred initialization and hasn't been initialized
            if isinstance(plugin, InitializablePluginProtocol):
                if not plugin._initialized:
                    self._initialize_plugin_with_config(plugin)

            self._embedding_plugin = cast(EmbeddingServiceInterface, plugin)

            # CRITICAL: Notify plugin it's an active interface provider
            if isinstance(plugin, InitializablePluginProtocol):
                plugin.set_as_active_provider("EmbeddingServiceInterface")
                logger.debug(
                    f"Notified {self._embedding_plugin_name} that it's active EmbeddingServiceInterface provider"
                )

        return self._embedding_plugin

    def _initialize_plugin_with_config(self, plugin: InitializablePluginProtocol) -> None:
        """Initialize plugin with configuration from JSON file.

        Args:
            plugin: Plugin instance supporting InitializablePluginProtocol

        Raises:
            FrameworkError: If initialization fails
        """
        import json
        import os
        from pathlib import Path

        # Find the plugin config file
        # Assuming APP_HOME structure: APP_HOME/config/plugins/{plugin_name}.json
        config_path = (
            Path(os.environ.get("ANANTA_APP_HOME", "."))
            / "config"
            / "plugins"
            / f"{self._embedding_plugin_name}.json"
        )

        if config_path.exists():
            try:
                with open(config_path) as f:
                    plugin_config = json.load(f)
                logger.debug(
                    f"Initializing {self._embedding_plugin_name} with config from {config_path}"
                )
                plugin.initialize(plugin_config)
                logger.debug(f"{self._embedding_plugin_name} initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize {self._embedding_plugin_name}: {e}")
                raise FrameworkError(f"Failed to initialize embedding plugin: {e}") from e
        else:
            logger.error(f"No config file found at {config_path}, initializing with defaults")
            plugin.initialize({})

    def _ensure_ready(self) -> EmbeddingServiceInterface:
        """Ensure embedding plugin exists, implements interface, and is ready.

        Returns:
            The embedding plugin typed as EmbeddingServiceInterface

        Raises:
            FrameworkError: If plugin not found, doesn't implement interface, or not ready
        """
        plugin = self._validate_embedding_plugin()

        # READINESS CONTRACT: Verify plugin is ready before use
        if not plugin.is_ready():
            error = plugin.readiness_error or "Unknown readiness error"
            raise FrameworkError(
                f"Embedding plugin '{self._embedding_plugin_name}' not ready: {error}"
            )

        return plugin

    def generate_embeddings(
        self, inputs: list[str], model: str | None = None, input_type: str = "text"
    ) -> ActionResult:
        """Generate embeddings for text inputs via configured embedding provider.

        Args:
            inputs: List of text strings to generate embeddings for
            model: Optional model identifier (uses provider default if not specified)
            input_type: Type of input data (text, image, audio)

        Returns:
            ActionResult with embeddings in data.result field

        Raises:
            FrameworkError: If plugin not available or request fails
        """
        plugin = self._ensure_ready()

        # Call plugin with interface signature
        return plugin.generate_embeddings(inputs=inputs, model=model, input_type=input_type)

    def get_embedding_dimension(self, model: str | None = None) -> ActionResult:
        """Get embedding dimension for a model.

        Args:
            model: Optional model identifier (uses provider default if not specified)

        Returns:
            ActionResult with dimension information in data.result field

        Raises:
            FrameworkError: If plugin not available
        """
        plugin = self._ensure_ready()

        # Call plugin with interface signature
        return plugin.get_embedding_dimension(model=model)

    def list_models(self) -> ActionResult:
        """List available embedding models.

        Returns:
            ActionResult with models list in data.result field

        Raises:
            FrameworkError: If plugin not available
        """
        plugin = self._ensure_ready()

        # Call plugin with interface signature
        return plugin.list_models()

    def get_default_dimensions(self) -> int:
        """Return the embedding service's default output dimension.

        Synchronous; does NOT require plugin readiness. Used at schema-init
        time (startup step 8) to declare the discovery embedding column's
        vector shape before the embedding plugin's full readiness pass
        runs in a later startup step. Bypasses _ensure_ready() and the
        config-driven initialize() path — the plugin's get_default_dimensions()
        returns its declared default without doing network I/O.
        """
        plugin = self.plugin_manager.get_plugin(self._embedding_plugin_name)
        if not plugin:
            raise FrameworkError(
                f"Embedding plugin '{self._embedding_plugin_name}' not found. "
                f"Ensure plugin is installed and configured."
            )
        if not isinstance(plugin, EmbeddingServiceInterface):
            raise FrameworkError(
                f"Embedding plugin '{self._embedding_plugin_name}' does not "
                f"implement EmbeddingServiceInterface. Plugin type: {type(plugin)}"
            )
        return plugin.get_default_dimensions()

    def _capture_bootstrap_state(self) -> dict[str, object]:
        """No bootstrap state to capture (bootstrap mode not supported)."""
        return {}

    def _restore_bootstrap_data(self, data: dict[str, object]) -> None:
        """No bootstrap data to restore (bootstrap mode not supported)."""
        pass
