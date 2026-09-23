import json
import logging
from collections.abc import Callable

from ananta.core.actions.action_registration_manager import ActionRegistrationManager
from ananta.core.process_registry.constants import SYSTEM_PROMPT_PROCESS_KEYS
from ananta.core.process_registry.key_resolver import ProcessKeyResolver
from ananta.core.process_registry.manager import ProcessRegistryManager
from ananta.core.process_registry.registry_definition_manager import (
    DiscoveryServiceProtocol,
    ProcessKeyResolverProtocol,
    RegistryDefinitionManager,
)
from ananta.core.process_registry.util import ProcessRegistryUtil
from ananta.core.validation.validation_service import ValidationService
from ananta.interfaces.state_service_protocol import StateServiceProtocol

logger = logging.getLogger(__name__)


class RegistryManager:
    """
    Manages all registry operations including action registration, process key resolution,
    and definition management.

    EXTRACTED FROM: ActionManager god class decomposition - Phase 4.1
    RESPONSIBILITY: Clean interface for all registry-related operations with proper service delegation
    """

    def __init__(
        self,
        state_service: StateServiceProtocol,
        validation_service: ValidationService,
        discovery_service: object = None,
    ) -> None:
        """Initialize registry manager with required dependencies."""
        self.state_service = state_service
        self.validation_service = validation_service
        self.discovery_service = discovery_service

        # Registry utility and key resolution services
        self.process_registry_util = ProcessRegistryUtil(state_service)
        self.process_key_resolver = ProcessKeyResolver(self.process_registry_util)

        # Type narrowing: verify discovery_service implements DiscoveryServiceProtocol
        discovery_service_typed: DiscoveryServiceProtocol | None = None
        if discovery_service is not None and isinstance(
            discovery_service, DiscoveryServiceProtocol
        ):
            discovery_service_typed = discovery_service

        # Type narrowing: verify process_key_resolver implements ProcessKeyResolverProtocol
        process_key_resolver_typed: ProcessKeyResolverProtocol | None = None
        if isinstance(self.process_key_resolver, ProcessKeyResolverProtocol):
            process_key_resolver_typed = self.process_key_resolver

        # Registry management services
        self.registry_definition_manager = RegistryDefinitionManager(
            state_service=state_service,
            discovery_service=discovery_service_typed,
            process_key_resolver=process_key_resolver_typed,
        )

        self.process_registry_manager = ProcessRegistryManager(
            state_service=state_service,
            validation_service=validation_service,
            process_registry_util=self.process_registry_util,
            discovery_service=discovery_service,
        )

        self.registration_manager = ActionRegistrationManager(
            state_service=state_service, validation_service=validation_service
        )

    def set_process_external_id_getter(self, getter_func: Callable[[str], str | None]) -> None:
        """Configure process external ID getter for registration manager."""
        self.registration_manager.set_process_external_id_getter(getter_func)

    # =============================================================================
    # Action Registration Operations
    # =============================================================================

    async def register_action(self, action_def: dict[str, object]) -> bool:
        """Register an action definition in the persistent registry."""
        return await self.registration_manager.register_action(action_def)

    async def update_action(self, action_name: str, updates: dict[str, object]) -> bool:
        """Update an existing action definition in the persistent registry."""
        return await self.registration_manager.update_action(action_name, updates)

    async def deregister_action(self, action_name: str) -> bool:
        """Remove action from the registry with proper cleanup."""
        return await self.registration_manager.deregister_action(action_name)

    async def get_registered_actions(self) -> dict[str, object]:
        """Retrieve all registered actions from persistent storage."""
        return await self.registration_manager.get_registered_actions()

    # =============================================================================
    # Process Registry Operations
    # =============================================================================

    async def sync_process_registry(self) -> bool:
        """Synchronize process registry with discovery service."""
        return await self.process_registry_manager.sync_process_registry()

    def validate_and_prepare_process_record(
        self, process_key: str, process_data: dict[str, object]
    ) -> dict[str, object] | None:
        """Validate process data and prepare record for database storage."""
        try:
            # Get data with validation
            provider_type = process_data.get("provider_type", "")
            provider = process_data.get("provider", "")
            function_name = process_data.get("function_name", "")

            # Validate required fields are not empty
            if not provider_type or not provider or not function_name:
                logger.error(
                    f"❌ PROCESS_SYNC_008A: Invalid process data for {process_key}: missing required fields - "
                    f"provider_type='{provider_type}', provider='{provider}', function_name='{function_name}'"
                )
                return None

            # Validate provider_type is allowed by schema constraint
            if provider_type not in ["plugin", "service_interface"]:
                logger.error(
                    f"❌ PROCESS_SYNC_008B: Invalid provider_type '{provider_type}' for {process_key}: "
                    f"must be 'plugin' or 'service_interface'"
                )
                return None

            # Prepare record for database storage (EXACT schema match only)
            # Use pre-serialized parameter_schema from builder
            schema_data: object = process_data.get("parameter_schema")
            if not isinstance(schema_data, dict):
                raise TypeError(
                    f"parameter_schema must be a dict (JSON-ready), got {type(schema_data)} for {process_key}"
                )

            # Serialize customizations for result/error processing (EDGE processes)
            result_processor_customizations = process_data.get("result_processor_customizations")
            result_processor_customizations_json = (
                json.dumps(result_processor_customizations, ensure_ascii=False)
                if result_processor_customizations
                else "{}"
            )
            error_processor_customizations = process_data.get("error_processor_customizations")
            error_processor_customizations_json = (
                json.dumps(error_processor_customizations, ensure_ascii=False)
                if error_processor_customizations
                else "{}"
            )

            # Generate external_id from process_key (matches process_registry_manager.py format)
            external_id = f"proc_{process_key.replace('::', '_')}"

            # Determine if this is a core system prompt process
            include_in_system_prompt = process_key in SYSTEM_PROMPT_PROCESS_KEYS

            return {
                "external_id": external_id,
                "process_key": process_key,
                "provider_type": provider_type,
                "provider": provider,
                "function_name": function_name,
                "name": process_data.get("name", ""),
                "display_name": process_data.get("display_name", ""),
                "description": process_data.get("description", ""),
                "embedding_description": process_data.get("embedding_description", ""),
                "is_discoverable": process_data.get("is_discoverable", True),
                "parameter_schema": json.dumps(schema_data, ensure_ascii=False),
                "result_processor_customizations": result_processor_customizations_json,
                "error_processor_customizations": error_processor_customizations_json,
                "is_enabled": True,
                "include_in_system_prompt": include_in_system_prompt,
            }

        except Exception as e:
            logger.error(f"❌ PROCESS_SYNC_008: Error preparing record for {process_key}: {e}")
            return None

    # =============================================================================
    # Process Key Resolution Operations
    # =============================================================================

    def get_process_external_id(self, process_key: str) -> str | None:
        """Get process external ID from process key."""
        return self.process_registry_util.lookup_external_id_by_process_key(process_key)

    def get_process_info(self, process_external_id: str) -> dict[str, str]:
        """Get process information from external ID."""
        return self.process_registry_util.get_process_info(process_external_id)

    # =============================================================================
    # Definition Management Operations
    # =============================================================================

    def get_action_definition(
        self,
        action_name: str,
        state: dict[str, object] | None = None,
        process_key: str | None = None,
    ) -> dict[str, object] | None:
        """Get action definition with fallback chain from discovery service to registry."""
        logger.debug(f"Getting action definition for '{action_name}' from discovery service")

        # Actions are now defined via discovery service - unified process storage
        if hasattr(self, "discovery_service") and self.discovery_service:
            return self.registry_definition_manager.create_definition_from_discovery_service(
                action_name, process_key
            )

        # Fallback to state-based lookup for backward compatibility
        if state:
            return self.registry_definition_manager.create_definition_from_registry(
                action_name, state, process_key
            )

        return None

    def get_safe_action_definition(
        self, action_name: str, state: dict[str, object] | None, process_key: str | None
    ) -> dict[str, object] | None:
        """
        Safe action definition getter for process key resolution (avoids circular dependencies).

        Used specifically for process key resolution to prevent infinite recursion loops.
        """
        # Direct lookup without process key resolution to avoid infinite recursion
        if hasattr(self, "discovery_service") and self.discovery_service:
            return self.registry_definition_manager.create_definition_from_discovery_service(
                action_name, process_key
            )

        # Fallback to state-based lookup
        if state:
            return self.registry_definition_manager.create_definition_from_registry(
                action_name, state, process_key
            )

        return None
