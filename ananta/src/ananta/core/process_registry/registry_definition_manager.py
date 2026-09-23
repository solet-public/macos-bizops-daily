"""
Registry Definition Manager Service

Responsibility: Handle all action definition creation from various sources (registry, storage, discovery)
Dependencies: StateService, DiscoveryService, ProcessKeyResolver, ActionStatus, logging
Complexity: High - focused on complex definition management with multiple data sources

Extracted from ActionManager god class (B10, B6, B7 complexity methods)
"""

import json
import logging
from typing import Protocol, runtime_checkable

from ananta.constants import FRAMEWORK_NAMESPACE, NOTES_MAX_LENGTH, ProviderType
from ananta.core.domain.status import is_status_match
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.error_handling import FrameworkError
from ananta.interfaces.state_service_protocol import StateServiceProtocol

logger = logging.getLogger(__name__)


@runtime_checkable
class DiscoveryServiceProtocol(Protocol):
    """Protocol for DiscoveryService interface."""

    def get_process_by_key(self, process_key: str) -> dict[str, object] | None: ...

    def get_process_by_name(self, name: str) -> dict[str, object] | None: ...


@runtime_checkable
class ProcessKeyResolverProtocol(Protocol):
    """Protocol for ProcessKeyResolver interface."""

    def get_process_info(self, process_external_id: str) -> dict[str, str]: ...


class RegistryDefinitionManager:
    """
    Service for managing action definition creation from multiple sources.

    ARCHITECTURAL ROLE: Supporting service that extracts definition management logic
    from ActionManager while maintaining action management integrity.

    This service handles:
    - Creating action definitions from process registry with complex lookup logic
    - Retrieving stored action definitions from persistent storage
    - Building definitions from discovery service with process data conversion
    - Parameter format conversion between registry and action definition formats
    - Multi-source definition resolution with fallback strategies
    """

    def __init__(
        self,
        state_service: StateServiceProtocol | None = None,
        discovery_service: DiscoveryServiceProtocol | None = None,
        process_key_resolver: ProcessKeyResolverProtocol | None = None,
    ) -> None:
        """Initialize RegistryDefinitionManager with required dependencies."""
        self.state_service = state_service
        self.discovery_service = discovery_service
        self.process_key_resolver = process_key_resolver

    def create_definition_from_registry(
        self, action_name: str, state: dict[str, object], process_key: str | None = None
    ) -> dict[str, object] | None:
        """
        Create action definition from process registry with complex lookup logic.

        EXTRACTED FROM: ActionManager._create_definition_from_registry() - B(10) complexity

        Args:
            action_name: Name of the action to create definition for
            state: Current state containing process registry
            process_key: Optional process key for direct lookup

        Returns:
            Action definition dictionary or None if not found

        Raises:
            FrameworkError: If process registry data is invalid
        """
        try:
            processes = self._extract_processes_from_state(state)
            if processes is None:
                return None

            matching_process, resolved_key = self._find_matching_process(
                processes, action_name, process_key
            )
            if matching_process is None:
                return None

            return self._build_registry_action_definition(
                matching_process, action_name, resolved_key
            )

        except FrameworkError:
            raise
        except Exception as e:
            logger.error(
                f"Failed to create action definition from process registry for '{action_name}': {e}"
            )
            return None

    def _extract_processes_from_state(self, state: dict[str, object]) -> dict[str, object] | None:
        """Extract processes dict from state's process registry."""
        process_registry = state.get("process_registry", {})
        if not isinstance(process_registry, dict):
            logger.error(
                f"Invalid process_registry type: expected dict, got {type(process_registry)}"
            )
            return None

        processes = process_registry.get("processes", {})
        if not isinstance(processes, dict):
            logger.error(f"Invalid processes type: expected dict, got {type(processes)}")
            return None
        return processes

    def _find_matching_process(
        self,
        processes: dict[str, object],
        action_name: str,
        process_key: str | None,
    ) -> tuple[dict[str, object] | None, str | None]:
        """Find matching process by key or name."""
        if process_key:
            return self._find_by_key(processes, process_key)
        return self._find_by_name(processes, action_name)

    def _find_by_key(
        self, processes: dict[str, object], process_key: str
    ) -> tuple[dict[str, object] | None, str | None]:
        """Find process by composite key."""
        matching_process = processes.get(process_key)
        if matching_process and isinstance(matching_process, dict):
            logger.debug(f"DEBUG: Found process via direct composite key lookup: {process_key}")
            return matching_process, process_key
        logger.error(f"Process with composite key '{process_key}' not found in registry")
        return None, None

    def _find_by_name(
        self, processes: dict[str, object], action_name: str
    ) -> tuple[dict[str, object] | None, str | None]:
        """Find process by name field."""
        for key, process in processes.items():
            if isinstance(process, dict) and process.get("name") == action_name:
                logger.debug(f"DEBUG: Found process via name lookup: {action_name} -> {key}")
                return process, key
        logger.error(f"Process with name '{action_name}' not found in registry")
        return None, None

    def _build_registry_action_definition(
        self,
        matching_process: dict[str, object],
        action_name: str,
        process_key: str | None,
    ) -> dict[str, object]:
        """Build action definition from registry process data."""
        function_name = matching_process.get("function_name")
        if not function_name:
            logger.error(
                f"Process registry entry missing required 'function_name' field for action '{action_name}'"
            )
            raise FrameworkError(
                message=f"Process registry entry missing required 'function_name' field for action '{action_name}'",
                error_code="action_manager.missing_function_name",
                details={
                    "action_name": action_name,
                    "process_key": process_key,
                    "process_data": matching_process,
                },
            )

        action_def: dict[str, object] = {
            "name": action_name,
            "description": matching_process.get("description", ""),
            "process": {
                "provider_type": matching_process.get("provider_type", ProviderType.PLUGIN.value),
                "provider": matching_process.get("provider"),
                "function_name": function_name,
            },
            "parameters": self._convert_registry_parameters(matching_process),
        }

        logger.debug(f"Generated action definition for '{action_name}' from process registry")
        return action_def

    def _convert_registry_parameters(
        self, matching_process: dict[str, object]
    ) -> dict[str, object]:
        """Convert registry parameter format to action definition format."""
        registry_params = matching_process.get("parameters", {})
        parameters: dict[str, object] = {}
        if not isinstance(registry_params, dict):
            return parameters
        for param_name, param_info in registry_params.items():
            if isinstance(param_info, dict):
                parameters[param_name] = {
                    "type": param_info.get("type", "string"),
                    "required": param_info.get("required", False),
                    "description": param_info.get("description", ""),
                    "default": param_info.get("default"),
                }
        return parameters

    def get_stored_action_definition(self, action_name: str) -> dict[str, object] | None:
        """
        Retrieve stored action definition from persistent storage.

        EXTRACTED FROM: ActionManager._get_stored_action_definition() - B(6) complexity

        Args:
            action_name: Name of the action to retrieve definition for

        Returns:
            Action definition dictionary or None if not found

        Raises:
            FrameworkError: If retrieval operation fails
        """
        try:
            if not self.state_service:
                logger.error("State service not available for retrieving stored action definition")
                return None

            result = self._query_stored_action(action_name)
            action_data = self._extract_action_data_from_result(result)
            if action_data is None:
                return None

            return self._build_stored_action_definition(action_data, action_name)

        except FrameworkError:
            raise
        except Exception as e:
            raise FrameworkError(
                message=f"Failed to retrieve stored action definition for '{action_name}'",
                error_code="action_manager.definition_retrieval_failed",
                details={"action_name": action_name, "operation": "load_stored_definition"},
            ) from e

    def _query_stored_action(self, action_name: str) -> ActionResult:
        """Query state service for stored action definition."""
        if not self.state_service:
            return {}
        return self.state_service.read_state(
            namespace=FRAMEWORK_NAMESPACE,
            query={
                "table": "action_definitions",
                "filters": {"action_name": action_name},
                "limit": 1,
            },
        )

    def _extract_action_data_from_result(self, result: ActionResult) -> dict[str, object] | None:
        """Extract action data from state service result."""
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return None

        data = result.get("data")
        if not isinstance(data, dict):
            return None

        result_data = data.get("result")
        if not isinstance(result_data, dict):
            return None

        records = result_data.get("records")
        if not isinstance(records, list) or not records:
            return None

        action_data_raw = records[0]
        if not isinstance(action_data_raw, dict):
            logger.error(f"Invalid action_data type: expected dict, got {type(action_data_raw)}")
            return None
        return action_data_raw

    def _build_stored_action_definition(
        self, action_data: dict[str, object], action_name: str
    ) -> dict[str, object] | None:
        """Build action definition from stored action data."""
        process_info = self._get_process_info_from_action_data(action_data)

        default_params = action_data.get("default_parameters", "{}")
        if not isinstance(default_params, str):
            default_params = "{}"

        try:
            action_def = {
                "name": action_data.get("action_name"),
                "description": action_data.get("description", ""),
                "parameters": json.loads(default_params),
                "category": action_data.get("category", ""),
                "process": process_info,
            }
            return action_def
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse default_parameters for action '{action_name}': {e}")
            return None

    def _get_process_info_from_action_data(self, action_data: dict[str, object]) -> dict[str, str]:
        """Extract process info from action data."""
        process_external_id = action_data.get("process_external_id")
        if process_external_id is None:
            return {}
        if not isinstance(process_external_id, str):
            logger.error(
                f"Invalid process_external_id type: expected str, got {type(process_external_id)}"
            )
            return {}
        return self._get_process_info(process_external_id)

    def create_definition_from_discovery_service(
        self, action_name: str, process_key: str | None = None
    ) -> dict[str, object] | None:
        """
        Create action definition from discovery service.

        EXTRACTED FROM: ActionManager._create_definition_from_discovery_service() - B(7) complexity

        Args:
            action_name: Name of the action to create definition for
            process_key: Optional process key for direct lookup

        Returns:
            Action definition dictionary or None if not found
        """
        try:
            # Direct lookup by process_key (deterministic)
            if process_key and self.discovery_service:
                logger.debug(f"Looking for process with key '{process_key}' in discovery service")
                process_data = self.discovery_service.get_process_by_key(process_key)
                if process_data:
                    logger.debug(f"Found process via discovery service key lookup: {process_key}")
                    return self.build_action_definition(process_data)
                else:
                    logger.error(f"Process with key '{process_key}' not found in discovery service")
                    return None
            elif self.discovery_service:
                # Lookup by name (deterministic)
                logger.debug(f"Looking for process with name '{action_name}' in discovery service")
                process_data = self.discovery_service.get_process_by_name(action_name)
                if process_data:
                    logger.debug(f"Found process via discovery service name lookup: {action_name}")
                    return self.build_action_definition(process_data)
                else:
                    logger.error(
                        f"Process with name '{action_name}' not found in discovery service"
                    )
                    return None
            else:
                logger.error("Discovery service not available")
                return None

        except Exception as e:
            logger.error(f"Error getting process definition from discovery service: {e}")
            return None

    def build_action_definition(self, process_data: dict[str, object]) -> dict[str, object]:
        """
        Build action definition from process data with clean architectural separation.

        EXTRACTED FROM: ActionManager._build_action_definition() - A complexity helper

        Args:
            process_data: Process data from discovery service

        Returns:
            Action definition dictionary
        """
        logger.debug("BOUNDARY_001: Building action definition with clean architectural separation")

        # ✅ CORRECT: Extract only action-layer fields
        action_definition = {
            "name": process_data.get("name"),
            "description": process_data.get("description", ""),
            "process": {
                "provider_type": process_data.get("provider_type"),
                "provider": process_data.get("provider"),
                "function_name": process_data.get("function_name"),
            },
            "arguments": process_data.get("arguments", {}),
            "notes": self._derive_notes(process_data),
        }

        logger.debug(f"Built action definition for '{process_data.get('name')}'")
        return action_definition

    def _derive_notes(self, process_data: dict[str, object]) -> str:
        """Derive a reasonable notes field from process metadata."""
        notes_value = process_data.get("notes")
        if isinstance(notes_value, str) and notes_value.strip():
            return notes_value.strip()[:NOTES_MAX_LENGTH]

        process_name = process_data.get("name")
        if isinstance(process_name, str) and process_name.strip():
            return f"Execute {process_name.strip()} via registry definition."[:NOTES_MAX_LENGTH]

        function_name = process_data.get("function_name")
        if isinstance(function_name, str):
            return f"Execute {function_name} via registry definition."[:NOTES_MAX_LENGTH]

        return "Execute process via registry definition."[:NOTES_MAX_LENGTH]

    def _get_process_info(self, process_external_id: str) -> dict[str, str]:
        """
        Get process info from process key resolver.

        EXTRACTED FROM: ActionManager._get_process_info() - A complexity delegation

        Args:
            process_external_id: External ID of the process

        Returns:
            Process information dictionary
        """
        if self.process_key_resolver:
            return self.process_key_resolver.get_process_info(process_external_id)
        # Explicitly return typed empty dict to satisfy mypy
        empty_dict: dict[str, str] = {}
        return empty_dict
