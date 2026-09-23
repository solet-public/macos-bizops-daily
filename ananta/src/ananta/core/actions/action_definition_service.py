"""
Action Definition Service

Responsibility: Handle all action definition retrieval and management operations for ActionExecutionEngine
Dependencies: StateService, ActionStatus, logging
Complexity: Medium - focused on action definition retrieval from registry and legacy sources

Extracted from ActionExecutionEngine god class (3 methods, including B(6) complexity method)
"""

import json
import logging
from collections.abc import Callable

from ananta.constants import FRAMEWORK_NAMESPACE
from ananta.core.domain.status import is_status_match
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.interfaces.state_service_protocol import StateServiceProtocol

logger = logging.getLogger(__name__)


class ActionDefinitionService:
    """
    Service for managing action definition retrieval and processing.

    ARCHITECTURAL ROLE: Supporting service that extracts action definition logic
    from ActionExecutionEngine while maintaining execution engine integrity.

    This service handles:
    - Retrieving action definitions from persistent registry
    - Creating legacy action definitions from process keys
    - Merging default parameters with provided parameters
    - Registry database operations and error handling
    """

    def __init__(self, state_service: StateServiceProtocol | None = None) -> None:
        """Initialize ActionDefinitionService."""
        self.state_service = state_service

    async def get_action_definition_from_registry(
        self, action_name: str
    ) -> dict[str, object] | None:
        """
        Get action definition from persistent registry.

        EXTRACTED FROM: ActionExecutionEngine._get_action_definition_from_registry() - B(6) complexity

        Args:
            action_name: Name of the action to retrieve definition for

        Returns:
            Action definition dictionary if found, None if not found or on error
        """
        try:
            if not self.state_service:
                return None

            result = self.state_service.read_state(
                namespace=FRAMEWORK_NAMESPACE,
                query={"table": "action_definitions", "filters": {"action_name": action_name}},
            )

            if is_status_match(result.get("action_status"), ActionStatus.COMPLETED) and result.get(
                "data"
            ):
                data_obj = result.get("data", {})
                records_obj = data_obj.get("records", [])
                # Type narrow records from object to list using isinstance check
                if isinstance(records_obj, list) and records_obj:
                    record_obj = records_obj[0]
                    # Type narrow record from object to dict using isinstance check
                    if isinstance(record_obj, dict):
                        record = record_obj
                        default_params_str = record.get("default_parameters", "{}")
                        # Type narrow from object to str using isinstance check
                        if not isinstance(default_params_str, str):
                            default_params_str = "{}"
                        return {
                            "name": record.get("action_name"),
                            "description": record.get("description", ""),
                            "parameters": json.loads(default_params_str),
                            "process": {
                                "provider_type": "plugin",  # Default assumption
                                "provider": "unknown",  # Will be resolved from process_external_id
                                "function_name": "execute_action",
                            },
                        }

        except Exception as e:
            logger.error(f"Error retrieving action definition for '{action_name}': {e}")

        return None

    def get_legacy_action_definition(
        self,
        action_name: str,
        process_key: str,
        parse_process_key_func: Callable[[str, str], tuple[str, str, str]],
    ) -> dict[str, object]:
        """
        Create legacy action definition from process key.

        EXTRACTED FROM: ActionExecutionEngine._get_legacy_action_definition() - A complexity

        Args:
            action_name: Name of the action
            process_key: Process key to parse
            parse_process_key_func: Function to parse process key components

        Returns:
            Dictionary containing legacy action definition
        """
        provider_type, provider, function_name = parse_process_key_func(process_key, action_name)

        return {
            "name": action_name,
            "description": f"Legacy action for {action_name}",
            "parameters": {},
            "process": {
                "provider_type": provider_type,
                "provider": provider,
                "function_name": function_name,
            },
        }

    def extract_merged_parameters(
        self, action_def: dict[str, object], action_parameters: dict[str, object]
    ) -> dict[str, object]:
        """
        Merge action definition parameters with provided parameters.

        EXTRACTED FROM: ActionExecutionEngine._extract_merged_parameters() - A complexity

        Args:
            action_def: Action definition containing default parameters
            action_parameters: Parameters provided at execution time

        Returns:
            Dictionary with merged parameters (provided parameters override defaults)
        """
        default_params_obj = action_def.get("parameters", {})
        # Type narrow from object to dict using isinstance check
        if isinstance(default_params_obj, dict):
            default_params = default_params_obj
        else:
            default_params = {}
        merged_params = {**default_params, **action_parameters}
        return merged_params
