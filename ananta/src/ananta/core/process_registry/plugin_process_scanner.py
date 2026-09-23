"""Plugin process scanning + registration.

Extracted from `ProcessRegistryBuilder` during the Step 9.A decomposition
(`workbench/2026-05-25_plugin_god_class_remediation.md` §9.1).

Responsibility: iterate the plugin manager's plugins, scan each for
`@platform_process`-decorated actions, validate the EdgeProcessProvider
contract, and register each plugin's actions as `plugin::<name>::<action>`
entries on the registry.

Depends on:
  - `PluginRegistrationValidator` for the EdgeProcessProvider contract.
  - `ServiceInterfaceMetadataGenerator` for input-contract / action-blueprint
    construction from `ActionMetadata`.
  - `InvocationSchemaGenerator` for the per-entry JSON Schema.
  - `plugin_manager` for the live plugin instances.
"""

from __future__ import annotations

import logging

from ananta.core.actions.action_metadata import ActionMetadata
from ananta.core.plugins.capabilities import is_service_provider
from ananta.core.plugins.plugin_base import PluginBase
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.core.process_registry.invocation_schema_generator import (
    InvocationSchemaGenerator,
)
from ananta.core.process_registry.plugin_registration_validator import (
    PluginRegistrationValidator,
)
from ananta.core.process_registry.service_interface_metadata_generator import (
    ServiceInterfaceMetadataGenerator,
)
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


class PluginProcessScanner:
    """Scan loaded plugins and register their `@platform_process` actions.

    Single instance per `build_process_registry` call. Holds references
    to its collaborators (validator, metadata generator, schema generator)
    and the plugin manager.
    """

    def __init__(
        self,
        plugin_manager: PluginManager,
        validator: PluginRegistrationValidator,
        metadata_generator: ServiceInterfaceMetadataGenerator,
        schema_generator: InvocationSchemaGenerator,
    ) -> None:
        self._plugin_manager = plugin_manager
        self._validator = validator
        self._metadata_generator = metadata_generator
        self._schema_generator = schema_generator

    def debug_plugin_manager_state(self) -> None:
        """Debug plugin manager state with comprehensive logging."""

        if not self._plugin_manager:
            return

        # Check each plugin's readiness and basic info
        for plugin_name, plugin_instance in self._plugin_manager.plugins.items():
            self._debug_individual_plugin(plugin_name, plugin_instance)

    def _debug_individual_plugin(
        self, plugin_name: str, plugin_instance: PluginBase
    ) -> None:
        """Debug individual plugin information."""

    def scan_and_register(self, registry: dict[str, object]) -> int:
        """Scan and register plugin processes, returning count."""
        plugin_processes = 0

        for plugin_name, plugin_instance in self._plugin_manager.plugins.items():
            if self._should_skip_plugin(plugin_name, plugin_instance):
                continue

            try:
                processes_count = self._process_individual_plugin(
                    plugin_name, plugin_instance, registry
                )
                plugin_processes += processes_count
            except FrameworkError as e:
                # FrameworkError from signature validation or plugin introspection is FATAL
                # Re-raise immediately to fail system startup - no recovery, no continue
                # This enforces fail-fast: plugins with invalid signatures MUST be fixed
                logger.error(
                    f"FATAL: Plugin '{plugin_name}' failed registration validation. "
                    f"This is a critical error that prevents system startup. Error: {e}"
                )
                raise
            except Exception as e:
                # Unexpected errors during plugin processing are also fatal
                logger.error(f"FATAL: Unexpected error processing plugin '{plugin_name}': {e}")
                raise

        return plugin_processes

    def _should_skip_plugin(self, plugin_name: str, plugin_instance: PluginBase) -> bool:
        """Check if plugin should be skipped from plugin:: namespace.

        See: ananta_build/2025-12-06_service_binding_architecture.md

        Only skip plugins that are:
            pass
        1. ServiceProviders (declare service_interface property)
        2. AND are BOUND to a service (via service_bindings)

        Non-bound ServiceProviders remain in plugin:: namespace.

        Raises:
            RuntimeError: If orchestrator_ref or service_bindings not available (startup bug)
        """
        # Not a service provider - don't skip
        if not is_service_provider(plugin_instance):
            return False

        # Validate the plugin actually inherits from its declared interface
        from ananta.core.plugins.capabilities import validate_service_provider

        validate_service_provider(plugin_instance)

        # Access service_bindings through plugin_manager's orchestrator_ref
        # Both MUST be available at this point - if not, it's a startup sequence bug
        orchestrator = self._plugin_manager.orchestrator_ref
        if orchestrator is None:
            raise RuntimeError(
                f"orchestrator_ref not set when processing {plugin_name}. "
                "This indicates a startup sequence bug."
            )

        # Only skip if this plugin IS the bound provider for some service
        if orchestrator.service_bindings.is_plugin_bound_to_service(plugin_name):
            return True

        # Plugin declares service_interfaces but isn't bound - keep in plugin:: namespace
        return False

    def _process_individual_plugin(
        self, plugin_name: str, plugin_instance: PluginBase, registry: dict[str, object]
    ) -> int:
        """Process individual plugin and register its processes."""

        actions = self._scan_plugin_processes(plugin_instance)

        # Validate EdgeProcessProvider implementation if plugin implements it
        self._validator.validate_edge_process_provider(plugin_name, plugin_instance, actions)

        processes_count = 0
        for action in actions:
            if self._should_skip_action(action):
                continue

            self._register_plugin_process(plugin_name, action, registry)
            processes_count += 1

        return processes_count

    def _scan_plugin_processes(self, plugin_instance: PluginBase) -> list[ActionMetadata]:
        try:
            actions = plugin_instance.get_available_actions()

            # RECOVERY MODE: Warn about missing return_value_schema but don't fail
            for action in actions:
                if not action.return_value_schema:
                    logger.error(
                        f'RECOVERY-WARNING: Action "{action.name}" in plugin "{plugin_instance.name}" missing return_value_schema. This should be fixed after recovery.'
                    )
                    # TODO: Re-enable fail-fast validation after recovery
                    # raise FrameworkError(...

            return actions
        except Exception as e:
            raise FrameworkError(
                message=f"Plugin {plugin_instance.name} failed to provide action metadata",
                error_code="process_registry.plugin_introspection_failed",
                details={
                    "plugin_name": plugin_instance.name,
                    "plugin_class": plugin_instance.__class__.__name__,
                    "error": str(e),
                },
                original_error=e,
            ) from e

    def _should_skip_action(self, action: ActionMetadata) -> bool:
        """Check if action should be skipped (action definitions)."""
        if action.name == "evaluate_input":
            return True
        return False

    def _register_plugin_process(
        self, plugin_name: str, action: ActionMetadata, registry: dict[str, object]
    ) -> None:
        """Register individual plugin process in registry."""
        process_key = f"plugin::{plugin_name}::{action.name}"
        processes_dict = registry["processes"]
        assert isinstance(processes_dict, dict), "Expected processes to be a dict"

        parameter_schema_dict = self._serialize_plugin_parameters(plugin_name, action)
        process_entry = self._build_plugin_process_entry(
            process_key, plugin_name, action, parameter_schema_dict
        )
        self._add_optional_plugin_fields(process_entry, action)
        self._add_plugin_documentation(process_entry, process_key, action, parameter_schema_dict)

        processes_dict[process_key] = process_entry

    def _serialize_plugin_parameters(
        self, plugin_name: str, action: ActionMetadata
    ) -> dict[str, object]:
        """Serialize parameter_schema at registration time.

        Args:
            plugin_name: Name of the plugin
            action: ActionMetadata with parameter definitions

        Returns:
            Dictionary of parameter name to serialized parameter dict

        Raises:
            TypeError: If a parameter doesn't have to_dict method
        """
        parameter_schema_dict: dict[str, object] = {}
        for param_name, param_meta in action.parameters.items():
            if not hasattr(param_meta, "to_dict"):
                raise TypeError(
                    f"Action '{action.name}' in plugin '{plugin_name}' has parameter '{param_name}' "
                    f"that is a {type(param_meta).__name__} instead of ParameterMetadata. "
                    f"Value: {param_meta!r}"
                )
            parameter_schema_dict[param_name] = param_meta.to_dict()
        return parameter_schema_dict

    def _build_plugin_process_entry(
        self,
        process_key: str,
        plugin_name: str,
        action: ActionMetadata,
        parameter_schema_dict: dict[str, object],
    ) -> dict[str, object]:
        """Build the base plugin process entry with required fields.

        Args:
            process_key: The process key for registration
            plugin_name: Name of the plugin
            action: ActionMetadata with action definitions
            parameter_schema_dict: Pre-serialized parameters dictionary

        Returns:
            Base process entry dict
        """
        return {
            "provider_type": "plugin",
            "provider": plugin_name,
            "function_name": action.name,
            "name": action.name,
            "display_name": action.display_name,
            "description": action.description,
            "embedding_description": action.embedding_description,
            "is_discoverable": action.is_discoverable,
            "parameters": parameter_schema_dict,
            "parameter_schema": parameter_schema_dict,
            "return_value_schema": action.return_value_schema,
            "is_inference_capable": action.is_inference_capable,
            "is_enabled": True,
            "is_long_running": action.is_long_running,
            "is_async": action.is_async,
            "input_contract": self._metadata_generator.generate_input_contract(action),
            "action_blueprint": self._metadata_generator.generate_action_blueprint(
                process_key, action
            ),
            "requires_result_processor": action.requires_result_processor,
            "processor_policy_category": action.processor_policy_category,
            "chaining_guidance": action.chaining_guidance if action.chaining_guidance else [],
            "work_count_impact": action.work_count_impact,
            "include_in_system_prompt": False,  # Only service_interface processes can be in system prompt
        }

    def _add_optional_plugin_fields(
        self, process_entry: dict[str, object], action: ActionMetadata
    ) -> None:
        """Add optional fields to plugin process entry.

        Args:
            process_entry: Process entry dict to modify in place
            action: ActionMetadata with action definitions
        """
        if action.default_result_processor:
            process_entry["result_processor"] = action.default_result_processor

        if action.result_processor_customizations:
            process_entry["result_processor_customizations"] = (
                action.result_processor_customizations.to_dict()
            )

        if action.error_processor_customizations:
            process_entry["error_processor_customizations"] = (
                action.error_processor_customizations.to_dict()
            )

    def _add_plugin_documentation(
        self,
        process_entry: dict[str, object],
        process_key: str,
        action: ActionMetadata,
        parameter_schema_dict: dict[str, object],
    ) -> None:
        """Add documentation and invocation schema to plugin process entry.

        Args:
            process_entry: Process entry dict to modify in place
            process_key: The process key for registration
            action: ActionMetadata with action definitions
            parameter_schema_dict: Pre-serialized parameters dictionary
        """
        process_entry["planning_docs"] = action.to_planning_dict()
        process_entry["error_handling_docs"] = action.to_error_handling_dict()
        process_entry["response_handling_docs"] = action.to_response_handling_dict()
        process_entry["invocation_schema"] = self._schema_generator.generate(
            process_key=process_key,
            parameters_dict=parameter_schema_dict,
        )
