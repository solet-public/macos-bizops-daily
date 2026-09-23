"""
Action Registration Manager Service

Responsibility: Handle action registration, deregistration, and updates
Dependencies: StateService, ValidationService
Complexity: Medium - manages persistent action registry operations

Extracted from ActionManager god class during refactoring phases
"""

import json
import logging
from collections.abc import Callable
from typing import Protocol

from ananta.constants import FRAMEWORK_NAMESPACE
from ananta.core.domain.status import is_status_match
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.services.state_service.bounded_read import assert_within_ceiling

logger = logging.getLogger(__name__)

# `action_definitions` is a REGISTRY, not a log: it holds one row per action the
# platform can execute, written by registration and removed by deregistration.
# It is bounded by the size of the codebase — how many actions exist to register
# — and not by traffic, runtime, or user data. Nothing appends to it per request.
# 10,000 is roughly two orders of magnitude above the plausible number of
# distinct actions a deployment defines (measured 2026-08-15:
# core.action_definitions holds 0 rows on this deployment, and the process
# registry — same shape, populated — holds 756). The 756 is why this ceiling
# stays well above the platform's default bound of 100 and takes the explicit
# `unbounded` opt-in instead: a registry of this shape plausibly runs to
# hundreds of rows, so capping it at the default would refuse legitimate reads.
#
# Stating the reason rather than only the number is the point: if this ever
# trips, the assumption that broke is "one row per defined action" — meaning
# something is writing per-execution rows into a registry — and that is the
# actual bug to go fix, not the ceiling.
_ACTION_DEFINITIONS_CEILING = 10_000
_ACTION_DEFINITIONS_CEILING_REASON = (
    "action_definitions holds one row per registered action, so it is bounded by "
    "how many actions the codebase defines and never by traffic or runtime."
)


class ValidationServiceProtocol(Protocol):
    """Protocol for ValidationService dependency."""

    pass


class ActionRegistrationManager:
    """
    Service for managing action registration operations.

    ARCHITECTURAL ROLE: Supporting service that extracts action registry management logic
    from ActionManager while maintaining registration operation integrity.

    This service handles:
    - Registering new actions in persistent storage
    - Updating existing action definitions
    - Deregistering actions with proper cleanup
    - Retrieving registered actions from storage
    """

    def __init__(
        self,
        state_service: StateServiceProtocol,
        validation_service: ValidationServiceProtocol | None = None,
    ) -> None:
        """Initialize ActionRegistrationManager with required dependencies."""
        self.state_service = state_service
        self.validation_service = validation_service
        self.process_external_id_getter: Callable[[str], str | None] | None = None

    def set_process_external_id_getter(self, getter: Callable[[str], str | None]) -> None:
        """Set the process external ID getter function."""
        self.process_external_id_getter = getter

    async def register_action(self, action_def: dict[str, object]) -> bool:
        """
        Register a new action in the persistent registry.

        Args:
            action_def: Action definition dictionary

        Returns:
            True if registration successful, False otherwise
        """
        if not self._validate_registration_prerequisites(action_def):
            return False

        action_name = action_def.get("action_name")
        if not action_name:
            logger.error("Action definition missing required 'action_name' field")
            return False

        # Resolve process external ID
        process_external_id = self._resolve_process_external_id_for_registration(action_def)
        if process_external_id is None:
            logger.error(f"Failed to resolve process external ID for action '{action_name}'")
            return False

        # Prepare registration data
        registration_data: dict[str, object] = {
            "table": "action_definitions",
            "records": [
                {
                    "action_name": action_name,
                    "process_external_id": process_external_id,
                    "description": action_def.get("description", ""),
                    "default_parameters": json.dumps(action_def.get("parameters", {})),
                    "is_enabled": 1,
                }
            ],
        }

        # Persist to registry
        result = self.state_service.write_state(
            namespace=FRAMEWORK_NAMESPACE, data=registration_data
        )

        if is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return True
        else:
            logger.error(f"Failed to register action '{action_name}': {result}")
            return False

    async def update_action(self, action_name: str, updates: dict[str, object]) -> bool:
        """
        Update an existing action definition in the persistent registry.

        Args:
            action_name: Name of the action to update
            updates: Dictionary of updates to apply

        Returns:
            True if update successful, False otherwise
        """
        if not self._validate_update_prerequisites(action_name):
            return False

        # Normalize updates to schema format
        normalized_updates = self._normalize_action_updates(updates, action_name)
        if normalized_updates is None:
            return False

        # Persist updates
        return self._persist_action_updates(action_name, normalized_updates)

    async def deregister_action(self, action_name: str) -> bool:
        """
        Remove action from the registry with proper cleanup.

        Args:
            action_name: Name of the action to deregister

        Returns:
            True if deregistration successful, False otherwise
        """
        if not action_name:
            logger.error("Action name is required for deregistration")
            return False

        result = self.state_service.delete_records(
            namespace=FRAMEWORK_NAMESPACE,
            query={"table": "action_definitions", "filters": {"action_name": action_name}},
        )

        if is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return True
        else:
            logger.error(f"Failed to deregister action '{action_name}': {result}")
            return False

    async def get_registered_actions(self) -> dict[str, object]:
        """
        Retrieve all registered actions from persistent storage.

        Returns:
            Dictionary containing registered actions or error information
        """
        result = self.state_service.read_state(
            namespace=FRAMEWORK_NAMESPACE,
            query={
                "table": "action_definitions",
                "limit": _ACTION_DEFINITIONS_CEILING,
                # Conscious opt-in to a scan larger than the platform DEFAULT row
                # bound (100). This does NOT mean "no bound" — the bound is the
                # explicit limit above, and assert_within_ceiling makes it loud.
                # It means "this caller genuinely wants the whole registry and has
                # said so", which is exactly what resolve_read_limit requires
                # before honouring a limit over the default.
                "unbounded": True,
            },
        )

        if is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            actions_data: dict[str, object] = result.get("data", {})
            records = actions_data.get("records", [])
            if isinstance(records, list):
                # The limit above bounds the read; this makes the bound
                # self-enforcing. An explicit limit turns the provider's loud
                # overflow refusal OFF (read_bounds.resolve_read_limit returns
                # overflow_is_error=False for an explicit limit), so without this
                # check a table that outgrew the ceiling would hand back a silent
                # prefix — every caller would see a registry that had quietly
                # lost its tail.
                assert_within_ceiling(
                    records,
                    table="action_definitions",
                    ceiling=_ACTION_DEFINITIONS_CEILING,
                    reason=_ACTION_DEFINITIONS_CEILING_REASON,
                )
            return actions_data
        else:
            logger.error(f"Failed to retrieve registered actions: {result}")
            return {"error": "Failed to retrieve registered actions", "details": result}

    def _validate_registration_prerequisites(self, action_def: dict[str, object]) -> bool:
        """Validate that prerequisites for action registration are met."""
        if not action_def:
            logger.error("Action definition is required for registration")
            return False

        return True

    def _validate_update_prerequisites(self, action_name: str) -> bool:
        """Validate that prerequisites for action update are met."""
        if not action_name:
            logger.error("Action name is required for update")
            return False

        return True

    def _resolve_process_external_id_for_registration(
        self, action_def: dict[str, object]
    ) -> str | None:
        """Resolve process external ID for action registration."""
        # Extract process information from action definition
        process_obj = action_def.get("process", {})

        # Type narrow to dict
        if not isinstance(process_obj, dict):
            logger.error("Action definition 'process' field must be a dict")
            return None

        process: dict[str, object] = process_obj
        provider_type_obj = process.get("provider_type", "plugin")
        provider_type = str(provider_type_obj) if provider_type_obj is not None else "plugin"

        provider_obj = process.get("provider") or process.get("plugin")
        provider = str(provider_obj) if provider_obj is not None else None

        function_name_obj = process.get("function_name", "execute_action")
        function_name = (
            str(function_name_obj) if function_name_obj is not None else "execute_action"
        )

        if not provider:
            logger.error("Action definition missing required provider information")
            return None

        # Build process key
        process_key = f"{provider_type}::{provider}::{function_name}"

        # Use the process external ID getter if available
        if self.process_external_id_getter:
            process_external_id = self.process_external_id_getter(process_key)
            if process_external_id:
                return process_external_id

        # Fallback: generate external ID from process key
        logger.error(f"Using fallback process external ID generation for '{process_key}'")
        return process_key.replace("::", "_")

    def _normalize_action_updates(
        self, updates: dict[str, object], action_name: str
    ) -> dict[str, object] | None:
        """Convert action updates to normalized schema format."""
        normalized_updates: dict[str, object] = {}

        # Handle standard field updates
        if "description" in updates:
            normalized_updates["description"] = updates["description"]
        if "parameters" in updates:
            normalized_updates["default_parameters"] = json.dumps(updates["parameters"])

        # Handle process changes (requires lookup of new process_external_id)
        if "process" in updates:
            process_obj = updates["process"]

            # Type narrow to dict
            if not isinstance(process_obj, dict):
                logger.error(f"Process field in updates for action '{action_name}' must be a dict")
                return None

            process_external_id = self._resolve_process_external_id_for_update(
                process_obj, action_name
            )
            if process_external_id is None:
                return None  # Process lookup failed
            normalized_updates["process_external_id"] = process_external_id

        return normalized_updates

    def _resolve_process_external_id_for_update(
        self, process: dict[str, object], action_name: str
    ) -> str | None:
        """Resolve process external ID for action update process changes."""
        provider_type_obj = process.get("provider_type", "plugin")
        provider_type = str(provider_type_obj) if provider_type_obj is not None else "plugin"

        provider_obj = process.get("provider") or process.get("plugin")
        provider = str(provider_obj) if provider_obj is not None else None

        function_name_obj = process.get("function_name", "execute_action")
        function_name = (
            str(function_name_obj) if function_name_obj is not None else "execute_action"
        )

        if provider and function_name:
            process_key = f"{provider_type}::{provider}::{function_name}"

            if self.process_external_id_getter:
                process_external_id = self.process_external_id_getter(process_key)
                if process_external_id:
                    return process_external_id

            logger.error(f"Process '{process_key}' not found when updating action '{action_name}'")
            return None

        logger.error(f"Invalid process specification in action update for '{action_name}'")
        return None

    def _persist_action_updates(
        self, action_name: str, normalized_updates: dict[str, object]
    ) -> bool:
        """Persist action updates to the registry and return success status."""
        result = self.state_service.update_state(
            namespace=FRAMEWORK_NAMESPACE,
            query={"table": "action_definitions", "filters": {"action_name": action_name}},
            updates=normalized_updates,
        )

        if is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return True
        else:
            logger.error(f"Failed to update action '{action_name}': {result}")
            return False
