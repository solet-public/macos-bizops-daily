import logging
from typing import NoReturn, TypedDict

from ananta.constants import ProviderType
from ananta.core.plugins.plugin_contracts import ErrorCode
from ananta.core.process_registry.util import ProcessRegistryUtil
from ananta.error_handling import FrameworkError
from ananta.services.state_service import StateService

logger = logging.getLogger(__name__)


class ProcessDict(TypedDict, total=False):
    """Typed dictionary for process information in action definitions."""

    provider_type: str
    provider: str
    function_name: str


class ActionDataDict(TypedDict, total=False):
    """Typed dictionary for action data records from database."""

    action_name: str
    description: str
    default_parameters: str
    process_external_id: str


class StateResultDict(TypedDict, total=False):
    """Typed dictionary for state service result structure."""

    action_status: str
    data: object


class ActionDefinitionManager:
    """
    Service for managing action definitions and validation.

    ARCHITECTURAL ROLE: Supporting service that extracts action definition management logic
    from ActionValidator while maintaining validation pipeline integrity.

    This service handles:
    - Action definition structure validation
    - Action definition database retrieval
    - Process definition validation within actions
    - Provider type and format validation
    """

    def __init__(self, state_service: StateService | None = None):
        """Initialize ActionDefinitionManager."""
        self.state_service = state_service
        self.process_registry_util = ProcessRegistryUtil(state_service) if state_service else None

    def validate_action_definition(self, action_def: dict[str, object]) -> tuple[bool, str | None]:
        """
        Validate action definition structure and content.

        EXTRACTED FROM: ActionValidator.validate_action_definition() - B(9) complexity

        Args:
            action_def: Action definition dictionary to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            if not action_def.get("name"):
                return False, "Action definition missing 'name' field"

            if not action_def.get("process"):
                return False, "Action definition missing 'process' field"

            process_obj = action_def["process"]

            # Type narrowing: ensure process is a dict
            if not isinstance(process_obj, dict):
                return False, "Action definition 'process' field must be a dictionary"

            process: dict[str, object] = process_obj

            provider_type = process.get("provider_type")
            provider = process.get("provider")
            function_name = process.get("function_name")

            if not provider_type:
                return (
                    False,
                    f"Process missing 'provider_type' field (must be '{ProviderType.PLUGIN.value}' or '{ProviderType.SERVICE_INTERFACE.value}')",
                )

            if provider_type not in [
                ProviderType.PLUGIN.value,
                ProviderType.SERVICE_INTERFACE.value,
            ]:
                return (
                    False,
                    f"Invalid provider_type '{provider_type}'. Must be '{ProviderType.PLUGIN.value}' or '{ProviderType.SERVICE_INTERFACE.value}'",
                )

            if not provider:
                return False, "Process missing 'provider' field"

            if not function_name:
                return False, "Process missing 'function' field"

            process_key = f"{provider_type}::{provider}::{function_name}"
            process_exists = self._check_process_exists(process_key)
            if not process_exists:
                return False, f"Process '{process_key}' not found in process registry"

            return True, None

        except Exception as e:
            return False, f"Error validating action definition: {str(e)}"

    def get_action_definition(self, action_name: str) -> dict[str, object] | None:
        """
        Retrieve action definition from database.

        EXTRACTED FROM: ActionValidator._get_action_definition() - B(9) complexity

        Args:
            action_name: Name of the action to retrieve

        Returns:
            Action definition dictionary or None if not found

        Raises:
            FrameworkError: If state service unavailable or database errors
                occur during retrieval.
        """
        if not self.state_service:
            from ananta.error_handling import FrameworkError

            raise FrameworkError(
                message="State service not available for action definition lookup",
                error_code="action_definition.state_service_unavailable",
            )

        try:
            result = self._query_action_definition(action_name)
            action_data = self._extract_action_data(result)
            if action_data is None:
                return None
            return self._build_action_definition(action_data)
        except Exception as e:
            self._handle_retrieval_error(action_name, e)

    def _query_action_definition(self, action_name: str) -> object:
        """Query state service for action definition."""
        if not self.state_service:
            return None
        return self.state_service.read_state(
            namespace="core",
            query={
                "table": "action_definitions",
                "filters": {"action_name": action_name},
                "limit": 1,
            },
        )

    def _extract_action_data(self, result: object) -> dict[str, object] | None:
        """Extract action data from nested state service result."""
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            return None

        data_obj = result.get("data")
        if not isinstance(data_obj, dict):
            return None

        result_obj = data_obj.get("result")
        if not isinstance(result_obj, dict):
            return None

        records_obj = result_obj.get("records")
        if not isinstance(records_obj, list) or not records_obj:
            return None

        action_data_obj = records_obj[0]
        if not isinstance(action_data_obj, dict):
            return None

        return action_data_obj

    def _build_action_definition(self, action_data: dict[str, object]) -> dict[str, object]:
        """Build action definition from database record."""
        action_def: dict[str, object] = {
            "name": action_data.get("action_name"),
            "description": action_data.get("description", ""),
            "parameters": action_data.get("default_parameters", "{}"),
        }

        process_external_id = action_data.get("process_external_id")
        if process_external_id and isinstance(process_external_id, str):
            process_info = self._get_process_info(process_external_id)
            if process_info:
                action_def["process"] = {
                    "provider_type": process_info.get("provider_type"),
                    "provider": process_info.get("provider"),
                    "function_name": process_info.get("function_name"),
                }

        return action_def

    def _handle_retrieval_error(self, action_name: str, e: Exception) -> NoReturn:
        """Handle errors during action definition retrieval. Always raises."""
        error_msg = str(e).lower()

        if "no such table" in error_msg and "action_definitions" in error_msg:
            logger.error("CRITICAL: Core schemas not initialized before ActionValidator use")
            raise FrameworkError(
                message="ActionValidator cannot function: Core database schemas not initialized. This indicates a startup sequence error - schemas must be created before ActionValidator is used.",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={
                    "missing_table": "action_definitions",
                    "action_name": action_name,
                    "original_error": str(e),
                },
                original_error=e,
            ) from e

        logger.error(f"Failed to retrieve action definition for '{action_name}': {e}")
        raise FrameworkError(
            message=f"Database error while validating action '{action_name}'",
            error_code=ErrorCode.SYSTEM_GENERIC,
            details={"action_name": action_name, "error": str(e)},
            original_error=e,
        ) from e

    def _get_process_info(self, process_external_id: str) -> dict[str, object] | None:
        """
        Get process information by external ID.

        EXTRACTED FROM: ActionValidator._get_process_info() helper method

        Args:
            process_external_id: External ID of the process

        Returns:
            Process information dictionary or None if not found
        """
        if not self.process_registry_util:
            logger.error("ProcessRegistryUtil not available for process lookup")
            return None

        try:
            process_info = self.process_registry_util.get_process_info(process_external_id)
            if not process_info:
                return None

            # Convert dict[str, str] to dict[str, object] for compatibility
            result: dict[str, object] = dict(process_info.items())
            return result

        except Exception as e:
            logger.error(
                f"Failed to retrieve process info for external_id '{process_external_id}': {e}"
            )
            return None

    def _check_process_exists(self, process_key: str) -> bool:
        """
        Check if process exists in the process registry.

        EXTRACTED FROM: ActionValidator._check_process_exists() helper method

        Args:
            process_key: Process key to check

        Returns:
            True if process exists, False otherwise
        """
        if not self.process_registry_util:
            return True  # Skip validation if no process registry util

        try:
            # Use ProcessRegistryUtil for centralized process registry operations
            result = self.process_registry_util.query_by_process_key(process_key)

            if result:
                records = result.get("records", [])
                if isinstance(records, list):
                    return len(records) > 0

            return False

        except Exception as e:
            logger.error(f"Error checking process existence for '{process_key}': {e}")
            return False  # Assume process doesn't exist if we can't check
