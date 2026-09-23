"""
Action Processor

Responsibility: Process actions from various sources, normalize, and queue for execution
Dependencies: State management, action factory, action normalization service
Complexity: Medium - handles action extraction, normalization, and database submission

Extracted from EventOrchestrator process_actions (C-11) and _process_actions_from_result (C-14)
"""

import json
import logging
from typing import Protocol

from ananta.constants import NOTES_MAX_LENGTH
from ananta.core.orchestration.action_normalization_service import ActionNormalizationService

logger = logging.getLogger(__name__)


class StateManager(Protocol):
    """Protocol for state manager interface - minimal requirements for ActionProcessor."""

    # ActionProcessor doesn't actually call any StateManager methods
    # It just stores the reference for future use


class ActionFactory(Protocol):
    """Protocol for action factory interface."""

    def submit_action_definition(
        self, action_definition: dict[str, object], context: dict[str, object] | None = None
    ) -> str:
        """Submit an action definition and return action ID.

        Returns:
            str: The action_id of the submitted action

        Raises:
            FrameworkError: If submission fails
        """
        ...


class ActionProcessor:
    """
    Action processor for handling action extraction, normalization, and queueing.

    Design Principles:
    - Single Responsibility: Action processing and coordination only
    - Dependency Injection: Clean injection of required services
    - State Management: Handle action state updates properly
    - Database-First: Submit actions via ActionFactory for database persistence
    - Template Resolution: Track template resolution through processing
    """

    def __init__(
        self,
        state_manager: StateManager,
        action_factory: ActionFactory,
        session_id: str | None = None,
        flow_id: str | None = None,
    ) -> None:
        self.state_manager = state_manager
        self.action_factory = action_factory
        self.current_session_id = session_id
        self.current_flow_id = flow_id

    def update_session_flow(self, session_id: str | None, flow_id: str | None) -> None:
        """Update session and flow IDs for action processing context."""
        self.current_session_id = session_id
        self.current_flow_id = flow_id

    def _ensure_actions_list_in_state(self, state: dict[str, object]) -> list[dict[str, object]]:
        """Ensure actions list exists in state and return it."""
        if "actions" not in state:
            actions_list: list[dict[str, object]] = []
            state["actions"] = actions_list
            return actions_list

        actions = state["actions"]
        if not isinstance(actions, list):
            raise TypeError(f"Expected actions to be a list, got {type(actions)}")

        # Verify all items in the list are dicts
        for item in actions:
            if not isinstance(item, dict):
                raise TypeError(f"Expected action to be a dict, got {type(item)}")

        # After isinstance check, we know it's a list, but mypy needs help with the element type
        # We've verified each element is a dict, so this is safe
        result: list[dict[str, object]] = []
        for action in actions:
            if isinstance(action, dict):
                result.append(action)
        return result

    def _check_templates_in_action(self, action: dict[str, object], stage: str) -> bool:
        """Check if action contains templates at a given stage."""
        action_str = json.dumps(action)
        has_templates = "___ACTION_RESULT_FROM:" in action_str
        logger.debug(f"ActionProcessor: Action has templates {stage} normalization: {has_templates}")
        return has_templates

    def _set_parent_relationship(
        self, normalized_action: dict[str, object], parent_action_id: str | None
    ) -> None:
        """Set parent relationship for action if parent_action_id is provided."""
        if parent_action_id:
            normalized_action["parent_id"] = parent_action_id
            logger.debug(
                f"ActionProcessor: Set parent_id='{parent_action_id}' for action '{normalized_action.get('name', 'unnamed')}'"
            )

    async def _process_single_action(
        self,
        action: dict[str, object],
        parent_action_id: str | None,
        actions_list: list[dict[str, object]],
    ) -> bool:
        """Process a single action through normalization and add to actions list. Returns True if added."""
        logger.debug(
            f"ActionProcessor: Processing action for addition: {action.get('name', 'unnamed')}"
        )

        # Check for templates before normalization
        self._check_templates_in_action(action, "before")

        # Normalize action using ActionNormalizationService
        normalized = await ActionNormalizationService.normalize_action_for_events(action)
        if normalized:
            # Check if templates resolved during normalization
            self._check_templates_in_action(normalized, "after")

            # Set parent relationship for actions created from inference results
            self._set_parent_relationship(normalized, parent_action_id)

            actions_list.append(normalized)
            logger.debug(f"ActionProcessor: Action '{normalized.get('name')}' added to queue")
            return True
        return False

    async def process_actions_from_result(
        self,
        state: dict[str, object],
        result: dict[str, object],
        parent_action_id: str | None = None,
    ) -> None:
        """Process actions from plugin execution results."""
        logger.debug(
            f"ActionProcessor: Processing actions from result, parent_id: {parent_action_id}"
        )

        # Extract actions to add
        actions_to_add = self._extract_actions_from_result(result)

        if actions_to_add is None:
            logger.debug("ActionProcessor: No actions found in result")
            return

        if actions_to_add:
            actions_list = self._ensure_actions_list_in_state(state)
            logger.debug(
                f"ActionProcessor: Processing {len(actions_to_add)} actions, "
                f"current state has {len(actions_list)} existing actions"
            )

            added_count = 0
            for action in actions_to_add:
                if await self._process_single_action(action, parent_action_id, actions_list):
                    added_count += 1

            logger.debug(f"ActionProcessor: Successfully added {added_count} actions to state")

    def _validate_and_extract_action_dicts(
        self, raw_actions: list[object]
    ) -> list[dict[str, object]]:
        """Validate and extract dict actions from a raw list.

        Args:
            raw_actions: List of potentially mixed types

        Returns:
            List containing only valid dict items
        """
        actions_list: list[dict[str, object]] = []
        for item in raw_actions:
            if isinstance(item, dict):
                actions_list.append(item)
            else:
                logger.error(f"ActionProcessor: Skipping non-dict action: {type(item)}")
        return actions_list

    def _extract_actions_from_data_field(
        self, result: dict[str, object]
    ) -> list[dict[str, object]] | None:
        """Extract actions from result['data']['actions'] if present.

        Args:
            result: Result dictionary to search

        Returns:
            List of action dicts, or None if not found
        """
        if "data" not in result:
            return None
        if not isinstance(result["data"], dict):
            return None

        result_data = result["data"]
        logger.debug(f"ActionProcessor: result['data'] keys: {list(result_data.keys())}")

        if "actions" not in result_data:
            return None
        if not isinstance(result_data["actions"], list):
            return None

        actions_list = self._validate_and_extract_action_dicts(result_data["actions"])
        if not actions_list:
            return None

        logger.debug(f"ActionProcessor: Found {len(actions_list)} actions in result data")
        return actions_list

    def _extract_actions_from_root(
        self, result: dict[str, object]
    ) -> list[dict[str, object]] | None:
        """Extract actions from result['actions'] if present.

        Args:
            result: Result dictionary to search

        Returns:
            List of action dicts, or None if not found
        """
        if "actions" not in result:
            return None
        if not isinstance(result["actions"], list):
            return None

        actions_list = self._validate_and_extract_action_dicts(result["actions"])
        if not actions_list:
            return None

        logger.debug(f"ActionProcessor: Found {len(actions_list)} actions in result root")
        return actions_list

    def _extract_actions_from_result(
        self, result: dict[str, object]
    ) -> list[dict[str, object]] | None:
        """Extract actions from various result formats.

        Checks in order:
        1. result['data']['actions']
        2. result['actions']

        Args:
            result: Result dictionary to extract actions from

        Returns:
            List of action dicts, or None if no actions found
        """
        actions_to_add = self._extract_actions_from_data_field(result)
        if actions_to_add is not None:
            return actions_to_add

        return self._extract_actions_from_root(result)

    async def process_legacy_actions(self, state: dict[str, object]) -> int:
        """Process legacy actions from state and submit to database.

        Returns the number of actions processed.
        """
        legacy_actions_raw = state.get("actions", [])
        if not isinstance(legacy_actions_raw, list):
            raise TypeError(f"Expected actions to be a list, got {type(legacy_actions_raw)}")

        # Type narrow to list of dicts
        legacy_actions: list[dict[str, object]] = []
        for item in legacy_actions_raw:
            if isinstance(item, dict):
                legacy_actions.append(item)
            else:
                logger.debug(f"ActionProcessor: Skipping non-dict legacy action: {type(item)}")

        logger.debug(f"ActionProcessor: Processing {len(legacy_actions)} legacy actions")

        processed_count = 0
        for action_data in legacy_actions:
            logger.debug(
                f"ActionProcessor: Converting legacy action to database: {action_data.get('name')}"
            )

            # Extract provider from action data
            self._extract_provider(action_data)

            # Handle both "arguments" and "parameters" fields
            action_arguments = action_data.get("arguments", action_data.get("parameters", {}))
            logger.debug(
                f"ActionProcessor: Action arguments for {action_data.get('name')}: {action_arguments}"
            )

            # DATABASE-FIRST: Use ActionFactory to store action to database
            logger.debug(
                "ActionProcessor: Using ActionFactory.submit_action_definition for database-first approach"
            )

            # Convert runtime action to action definition format for submission
            action_definition = {
                "name": action_data.get("name"),
                "arguments": action_arguments,
                "process_key": action_data.get("process_key"),
                "session_id": self.current_session_id,
                "flow_id": self.current_flow_id,
                "notes": self._extract_notes(action_data),
            }

            # Submit to database via ActionFactory (will be picked up by ActionQueuePoller)
            submission_response = self.action_factory.submit_action_definition(action_definition)
            logger.debug(
                f"ActionProcessor: Submitted legacy action to database: {submission_response}"
            )
            processed_count += 1

        return processed_count

    def _extract_provider(self, action_data: dict[str, object]) -> str:
        """Extract provider from action data in various formats."""
        provider = "unknown"

        if "process_key" in action_data:
            # Format: "plugin::provider::function_name"
            process_key = action_data["process_key"]
            if not isinstance(process_key, str):
                raise TypeError(f"Expected process_key to be a string, got {type(process_key)}")
            parts = process_key.split("::")
            provider = parts[1] if len(parts) >= 2 else "unknown"
            logger.debug(
                f"ActionProcessor: Extracted provider '{provider}' from process_key: {process_key}"
            )

        elif "process" in action_data:
            # Action definition format
            process_data = action_data["process"]
            if not isinstance(process_data, dict):
                raise TypeError(f"Expected process to be a dict, got {type(process_data)}")
            provider_value = process_data.get("provider", "unknown")
            if isinstance(provider_value, str):
                provider = provider_value
            else:
                provider = "unknown"
            logger.debug(f"ActionProcessor: Extracted provider '{provider}' from process structure")

        else:
            # FAIL-FAST: No fallback for missing provider information
            raise ValueError(
                f"Action data missing both 'process_key' and 'process' fields required for provider extraction. "
                f"Action: {action_data.get('name', 'unknown')}"
            )

        return provider

    def _extract_notes(self, action_data: dict[str, object]) -> str:
        """Ensure action definitions submitted downstream include notes."""
        notes_value = action_data.get("notes")
        if isinstance(notes_value, str) and notes_value.strip():
            return notes_value.strip()[:NOTES_MAX_LENGTH]

        process_key = action_data.get("process_key")
        if isinstance(process_key, str):
            return f"Legacy action submitted for {process_key}"[:NOTES_MAX_LENGTH]

        return "Legacy action submitted without explicit notes."[:NOTES_MAX_LENGTH]

    def get_processor_summary(self) -> dict[str, object]:
        """Get summary of ActionProcessor for debugging."""
        return {
            "component": "ActionProcessor",
            "responsibility": "Action processing, normalization, and queueing",
            "dependencies": ["state_manager", "action_factory"],
            "features": ["Template tracking", "Parent relationships", "Database-first submission"],
        }
