"""Process registry orchestrator.

Build pipeline assembled from focused collaborator classes
(`workbench/2026-05-25_plugin_god_class_remediation.md` §9.1):

  1. `PluginProcessScanner` — discover and register plugin processes.
  2. `ServiceInterfaceScanner` — discover and register service-interface
     processes from `*/interfaces/public.py` modules.
  3. `KnowledgeBaseOverlayLoader` — merge per-process knowledge-base JSON
     overlays into the registry, then enforce the post-merge
     EDGE-customizations contract.
  4. `PluginRegistrationValidator.validate_all_embedding_descriptions` —
     sweep the post-merge registry for discoverable processes lacking
     embedding descriptions.
  5. `InvocationSchemaGenerator.add_introspection_metadata` — attach
     top-level discovery / AI-usage-guide / introspection blocks.

This module owns the registry skeleton (`_initialize_registry_structure`),
the orchestrator's exception handling, and final completion logging.
Everything else lives in a focused collaborator.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ananta.core.domain.enums import ErrorSeverity
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.core.process_registry.invocation_schema_generator import (
    InvocationSchemaGenerator,
)
from ananta.core.process_registry.kb_overlay_loader import KnowledgeBaseOverlayLoader
from ananta.core.process_registry.plugin_process_scanner import PluginProcessScanner
from ananta.core.process_registry.plugin_registration_validator import (
    PluginRegistrationValidator,
)
from ananta.core.process_registry.service_interface_metadata_generator import (
    ServiceInterfaceMetadataGenerator,
)
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


def build_process_registry(plugin_manager: PluginManager) -> dict[str, object]:
    """Standalone function wrapper for ProcessRegistryBuilder.build_process_registry"""
    builder = ProcessRegistryBuilder(plugin_manager)
    return builder.build_process_registry()


class ProcessRegistryBuilder:
    """Assembles the process registry by wiring focused collaborators.

    Construction is cheap — the collaborators are stateless except for
    their constructor-injected dependencies, so `build_process_registry`
    can be called multiple times against the same builder if needed.
    """

    def __init__(self, plugin_manager: PluginManager) -> None:
        self.plugin_manager = plugin_manager
        self._schema_generator = InvocationSchemaGenerator()
        self._metadata_generator = ServiceInterfaceMetadataGenerator()
        self._validator = PluginRegistrationValidator()
        self._plugin_scanner = PluginProcessScanner(
            plugin_manager=plugin_manager,
            validator=self._validator,
            metadata_generator=self._metadata_generator,
            schema_generator=self._schema_generator,
        )
        self._service_interface_scanner = ServiceInterfaceScanner(
            schema_generator=self._schema_generator,
        )
        self._kb_loader = KnowledgeBaseOverlayLoader(
            plugin_manager=plugin_manager,
            schema_generator=self._schema_generator,
        )

    def build_process_registry(self) -> dict[str, object]:
        try:
            registry = self._initialize_registry_structure()

            self._plugin_scanner.debug_plugin_manager_state()

            plugin_processes = self._plugin_scanner.scan_and_register(registry)
            service_processes = self._service_interface_scanner.scan(registry)

            self._kb_loader.apply(registry)
            self._validator.validate_all_embedding_descriptions(registry)
            self._schema_generator.add_introspection_metadata(registry)

            self._log_registry_completion(plugin_processes, service_processes)

            return registry

        except Exception as e:
            logger.error(f"Exception in build_process_registry: {e}", exc_info=True)
            self._handle_registry_build_error(e)
            # Return empty registry if error handling doesn't raise
            return {
                "processes": {},
                "last_updated": datetime.now(UTC).isoformat(),
                "version": "2.0.0",
            }

    def _initialize_registry_structure(self) -> dict[str, object]:
        """Initialize the basic registry structure."""
        return {
            "processes": {},
            "last_updated": datetime.now(UTC).isoformat(),
            "version": "2.0.0",
            "architecture": "service_interface_only",
        }

    def _log_registry_completion(self, plugin_processes: int, service_processes: int) -> None:
        """Log completion information for registry building."""
        total_processes = plugin_processes + service_processes
        logger.debug(
            f"Process registry built with {total_processes} total processes ({plugin_processes} plugin processes, {service_processes} service interface processes)"
        )
        logger.debug("Service interface plugins encapsulated - regular plugins accessible directly")

    def _handle_registry_build_error(self, error: Exception) -> None:
        """Handle registry build error with comprehensive error information."""
        framework_error = FrameworkError(
            message=f"Failed to build process registry: {str(error)}",
            error_code="process_registry.build_failed",
            details={
                "plugin_count": len(self.plugin_manager.plugins),
                "loaded_plugins": list(self.plugin_manager.plugins.keys()),
            },
            original_error=error,
            severity=ErrorSeverity.ERROR,
        )
        logger.error(framework_error.message)
        raise framework_error

    def add_known_process_metadata(self, registry: dict[str, object]) -> None:
        """Public no-op preserved for backward compatibility with callers.

        NOTE: Result processor template generation has been moved to runtime
        merge logic. EDGE processes now store only customizations
        (`result_processor_customizations`, `error_processor_customizations`).
        At runtime, these are merged with the inference VERTEX's
        `action_definition_template` by the centralized merge logic.
        """
