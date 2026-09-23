"""Service Bindings - Maps service interfaces to plugin implementations.

See: ananta_build/2025-12-06_service_binding_architecture.md

The platform defines service interfaces. The application configuration
binds plugins to those interfaces. This module handles the resolution.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ananta.core.plugins.plugin_base import PluginBase
    from ananta.core.plugins.plugin_manager import PluginManager

logger = logging.getLogger(__name__)


class ServiceName(StrEnum):
    """Canonical service names for binding resolution.

    These are the service identifiers used in:
    - config/service_bindings.json
    - Environment variables: ANANTA_{SERVICE_NAME}
    - orchestrator.get_service(service_name)
    """

    # Core platform services (required)
    STATE_SERVICE = "state_service"
    INFERENCE_SERVICE = "inference_service"
    EMBEDDING_SERVICE = "embedding_service"
    VECTOR_SERVICE = "vector_service"
    BLOB_STORAGE_SERVICE = "blob_storage_service"

    # Optional platform services
    VAULT_SERVICE = "vault_service"
    ADDRESS_BOOK_SERVICE = "address_book_service"
    MEMORY_SERVICE = "memory_service"
    KNOWLEDGE_SERVICE = "knowledge_service"
    THINKING_SERVICE = "thinking_service"
    PLUGIN_SCHEMA_SERVICE = "plugin_schema_service"
    AGENT_MESSAGING_SERVICE = "agent_messaging_service"
    SELF_DEPLOYMENT_SERVICE = "self_deployment_service"
    LOCAL_SELF_DEPLOYMENT_SERVICE = "local_self_deployment_service"
    CLOUD_SELF_DEPLOYMENT_SERVICE = "cloud_self_deployment_service"
    IAM_PROVISIONING_SERVICE = "iam_provisioning_service"
    CODING_AGENT_SESSION_SERVICE = "coding_agent_session_service"
    # B3 dev-surface primitives (read-only over the local repo + toolchain).
    # Optional — NOT in REQUIRED_SERVICES; bound to platform_dev_surface_plugin.
    QUALITY_SERVICE = "quality_service"
    REPO_SERVICE = "repo_service"
    # NOTE: midwife + undertaker are NOT exclusive service interfaces.
    # The plugins (macos/aws_midwife_plugin, aws_undertaker_plugin) are
    # callable directly via plugin-namespace verbs, e.g.
    # plugin::aws_midwife_plugin::birth_solet. Caller picks the
    # substrate; multiple birther/undertaker plugins can coexist in one
    # solet. The interface contracts (the ABCs at
    # ananta/interfaces/{midwife,undertaker}_service_interface.py) still
    # exist as TYPING + STRUCTURAL contracts plugins implement; what was
    # retired is the singleton service-binding plumbing.

    # Platform infrastructure (not plugin-backed)
    SCHEDULING_SERVICE = "scheduling_service"
    DISCOVERY_SERVICE = "discovery_service"
    CONTEXT_SERVICE = "context_service"
    IO_INTERFACE_SERVICE = "io_interface_service"
    PROMPT_ASSEMBLY_SERVICE = "prompt_assembly_service"
    PLAN_LIFECYCLE_SERVICE = "plan_lifecycle_service"
    WBS_LIFECYCLE_SERVICE = "wbs_lifecycle_service"


class BindingSource(StrEnum):
    """Source of a service binding."""

    ENV = "env"
    CONFIG = "config"


# Services that MUST have a valid binding at startup
REQUIRED_SERVICES: frozenset[ServiceName] = frozenset(
    {
        ServiceName.STATE_SERVICE,
        ServiceName.BLOB_STORAGE_SERVICE,
        ServiceName.MEMORY_SERVICE,
        # The embedder is a required, inference-INDEPENDENT service (POR §1.3
        # ◆R2): every profile binds an embedding plugin (Mac nomic, AWS Bedrock)
        # or the platform must error. Declaring it required enforces the
        # bind-or-error invariant here rather than as a startup side effect —
        # discovery/retrieval (and the context_service briefing) depend on it
        # regardless of whether any reasoner is bound.
        ServiceName.EMBEDDING_SERVICE,
        # The state plugin's own schema-lifecycle interface — schema init has no
        # legacy fallback, so a missing binding is a fatal misconfig surfaced here.
        ServiceName.PLUGIN_SCHEMA_SERVICE,
    }
)

# Map service names to their interface module paths (for validation)
SERVICE_INTERFACE_MAP: dict[ServiceName, str] = {
    ServiceName.STATE_SERVICE: "ananta.interfaces.state_management_interface.StateManagementInterface",
    ServiceName.INFERENCE_SERVICE: "ananta.services.inference_service.interfaces.provider.InferenceProvider",
    ServiceName.EMBEDDING_SERVICE: "ananta.interfaces.embedding_service_interface.EmbeddingServiceInterface",
    ServiceName.VECTOR_SERVICE: "ananta.interfaces.vector_service_interface.VectorServiceInterface",
    ServiceName.VAULT_SERVICE: "ananta.interfaces.vault_service_interface.VaultServiceInterface",
    ServiceName.ADDRESS_BOOK_SERVICE: "ananta.interfaces.address_book_service_interface.AddressBookServiceInterface",
    ServiceName.MEMORY_SERVICE: "ananta.interfaces.memory_service_interface.MemoryServiceInterface",
    ServiceName.BLOB_STORAGE_SERVICE: "ananta.interfaces.blob_storage_service_interface.BlobStorageServiceInterface",
    ServiceName.KNOWLEDGE_SERVICE: "ananta.interfaces.knowledge_service_interface.KnowledgeServiceInterface",
    ServiceName.THINKING_SERVICE: "ananta.interfaces.thinking_provider_interface.ThinkingProvider",
    ServiceName.PLUGIN_SCHEMA_SERVICE: "ananta.interfaces.plugin_schema_service_interface.PluginSchemaServiceInterface",
    ServiceName.AGENT_MESSAGING_SERVICE: "ananta.interfaces.agent_messaging_service_interface.AgentMessagingServiceInterface",
    ServiceName.SELF_DEPLOYMENT_SERVICE: "ananta.interfaces.self_deployment_service_interface.SelfDeploymentServiceInterface",
    ServiceName.LOCAL_SELF_DEPLOYMENT_SERVICE: "ananta.interfaces.local_self_deployment_service_interface.LocalSelfDeploymentServiceInterface",
    ServiceName.CLOUD_SELF_DEPLOYMENT_SERVICE: "ananta.interfaces.cloud_self_deployment_service_interface.CloudSelfDeploymentServiceInterface",
    ServiceName.IAM_PROVISIONING_SERVICE: "ananta.interfaces.iam_provisioning_service_interface.IamProvisioningServiceInterface",
    ServiceName.CODING_AGENT_SESSION_SERVICE: "ananta.interfaces.coding_agent_session_service_interface.CodingAgentSessionServiceInterface",
    ServiceName.QUALITY_SERVICE: "ananta.interfaces.quality_service_interface.QualityServiceInterface",
    ServiceName.REPO_SERVICE: "ananta.interfaces.repo_service_interface.RepoServiceInterface",
}


class ServiceBindingError(Exception):
    """Raised when service binding resolution fails."""

    def __init__(self, service_name: str, message: str) -> None:
        self.service_name = service_name
        super().__init__(f"Service binding error for '{service_name}': {message}")


@dataclass(frozen=True, slots=True)
class ServiceBinding:
    """Immutable binding of a service to a plugin."""

    service_name: ServiceName
    plugin_name: str
    source: BindingSource


class ServiceBindings:
    """Resolves service-to-plugin bindings from configuration.

    Resolution order (highest priority first):
    1. Environment variable: ANANTA_{SERVICE_NAME} (e.g., ANANTA_STATE_SERVICE)
    2. Config file: config/service_bindings.json
    3. No default - fail if required service has no binding
    """

    CONFIG_FILENAME = "service_bindings.json"

    def __init__(self, app_home: str | Path) -> None:
        self._app_home = Path(app_home)
        self._bindings: dict[ServiceName, ServiceBinding] = {}
        self._loaded = False

    def load(self) -> None:
        """Load bindings from environment and config file.

        Call this during startup BEFORE validating plugins.
        """
        if self._loaded:
            raise RuntimeError("ServiceBindings.load() called twice")

        # Load from config file first (lowest priority)
        self._load_from_config_file()

        # Override with environment variables (highest priority)
        self._load_from_environment()

        self._loaded = True

    def _load_from_config_file(self) -> None:
        """Load bindings from config/service_bindings.json."""
        config_path = self._app_home / "config" / self.CONFIG_FILENAME

        if not config_path.exists():
            return

        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ServiceBindingError(
                "config_file",
                f"Invalid JSON in {config_path}: {e}",
            ) from e

        if not isinstance(data, dict):
            raise ServiceBindingError(
                "config_file",
                f"Expected dict in {config_path}, got {type(data).__name__}",
            )

        for service_name_str, plugin_name in data.items():
            try:
                service_name = ServiceName(service_name_str)
            except ValueError as e:
                # Fail fast on unknown service names - no defensive skipping
                raise ServiceBindingError(
                    service_name_str,
                    f"Unknown service name in {config_path}",
                ) from e

            if not isinstance(plugin_name, str):
                raise ServiceBindingError(
                    service_name_str,
                    f"Plugin name must be string, got {type(plugin_name).__name__}",
                )

            self._bindings[service_name] = ServiceBinding(
                service_name=service_name,
                plugin_name=plugin_name,
                source=BindingSource.CONFIG,
            )

    def _load_from_environment(self) -> None:
        """Load bindings from environment variables (overrides config)."""
        for service_name in ServiceName:
            env_var = f"ANANTA_{service_name.value.upper()}"
            plugin_name = os.environ.get(env_var)
            if plugin_name:
                self._bindings[service_name] = ServiceBinding(
                    service_name=service_name,
                    plugin_name=plugin_name,
                    source=BindingSource.ENV,
                )

    def get_binding(self, service_name: ServiceName) -> ServiceBinding | None:
        """Get the binding for a service, or None if not bound."""
        if not self._loaded:
            raise RuntimeError("ServiceBindings.load() not called")
        return self._bindings.get(service_name)

    def get_plugin_name(self, service_name: ServiceName) -> str | None:
        """Get the plugin name bound to a service, or None if not bound."""
        binding = self.get_binding(service_name)
        return binding.plugin_name if binding else None

    def is_bound(self, service_name: ServiceName) -> bool:
        """Check if a service has a binding."""
        return self.get_binding(service_name) is not None

    def get_all_bindings(self) -> dict[ServiceName, ServiceBinding]:
        """Get all bindings (for debugging/logging)."""
        if not self._loaded:
            raise RuntimeError("ServiceBindings.load() not called")
        return dict(self._bindings)

    def validate_required_services(self) -> None:
        """Validate that all required services have bindings.

        Raises:
            ServiceBindingError: If a required service has no binding.
        """
        if not self._loaded:
            raise RuntimeError("ServiceBindings.load() not called")

        missing = []
        for service_name in REQUIRED_SERVICES:
            if not self.is_bound(service_name):
                missing.append(service_name.value)

        if missing:
            raise ServiceBindingError(
                "required_services",
                f"Missing bindings for required services: {', '.join(missing)}",
            )

    def validate_plugin_exists(
        self,
        service_name: ServiceName,
        plugin_manager: PluginManager,
    ) -> None:
        """Validate that the bound plugin exists and is loaded.

        Raises:
            ServiceBindingError: If bound plugin doesn't exist.
        """
        binding = self.get_binding(service_name)
        if not binding:
            return  # Not bound, nothing to validate

        plugin_manager.get_plugin(binding.plugin_name)  # Will raise if not found

    def validate_plugin_interface(
        self,
        service_name: ServiceName,
        plugin: PluginBase,
    ) -> None:
        """Validate that the plugin implements the expected interface.

        Checks:
        1. Plugin declares service_interfaces (is a ServiceProvider)
        2. Expected interface is in the plugin's service_interfaces tuple
        3. Plugin inherits from the expected interface
        4. Plugin's supported_interface_versions matches the interface's INTERFACE_VERSION

        Raises:
            ServiceBindingError: If plugin doesn't implement the interface.
        """
        from ananta.core.plugins.capabilities import is_service_provider

        if not is_service_provider(plugin):
            raise ServiceBindingError(
                service_name.value,
                f"Plugin '{plugin.name}' does not declare service_interfaces property",
            )

        # Get expected interface from map
        interface_path = SERVICE_INTERFACE_MAP.get(service_name)
        if not interface_path:
            # Not a plugin-backed service
            return

        # Import the interface class
        module_path, class_name = interface_path.rsplit(".", 1)
        try:
            import importlib

            module = importlib.import_module(module_path)
            expected_interface = getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            raise ServiceBindingError(
                service_name.value,
                f"Cannot import interface {interface_path}: {e}",
            ) from e

        # Check plugin declares the expected interface in its service_interfaces tuple
        if expected_interface not in plugin.service_interfaces:
            declared_names = [iface.__name__ for iface in plugin.service_interfaces]
            raise ServiceBindingError(
                service_name.value,
                f"Plugin '{plugin.name}' declares {declared_names}, "
                f"expected {expected_interface.__name__}",
            )

        # Check plugin actually inherits from interface
        if not isinstance(plugin, expected_interface):
            raise ServiceBindingError(
                service_name.value,
                f"Plugin '{plugin.name}' does not inherit from {expected_interface.__name__}",
            )

        # Per-interface version validation
        required_version: str | None = getattr(expected_interface, "INTERFACE_VERSION", None)
        if required_version is not None:
            plugin_version = plugin.supported_interface_versions.get(expected_interface)
            if plugin_version != required_version:
                raise ServiceBindingError(
                    service_name.value,
                    f"Plugin '{plugin.name}' version mismatch for {expected_interface.__name__}: "
                    f"implements {plugin_version}, required {required_version}",
                )

    def is_plugin_bound_to_service(self, plugin_name: str) -> bool:
        """Check if a plugin is bound to ANY service.

        Used by builder.py to determine namespace exclusion.
        """
        if not self._loaded:
            raise RuntimeError("ServiceBindings.load() not called")

        for binding in self._bindings.values():
            if binding.plugin_name == plugin_name:
                return True
        return False

    def get_services_for_plugin(self, plugin_name: str) -> list[ServiceName]:
        """Get all service names a plugin is bound to."""
        if not self._loaded:
            raise RuntimeError("ServiceBindings.load() not called")

        return [
            service_name
            for service_name, binding in self._bindings.items()
            if binding.plugin_name == plugin_name
        ]
