"""Shared @ command processor for I/O interface plugins.

This service provides consistent @ command handling across all I/O interfaces
(console, JSON-RPC, Telegram, REST, etc.).

Supports:
- @file.json - Load action from file
- @{"action": "inline"} - Inline JSON action definition
- @[{...}, {...}] - Array of actions

Single Responsibility: Parse @ commands and convert to action definitions.
Complexity: A (simple parsing, minimal branching).
"""

import json
from pathlib import Path
from typing import Any


class AtCommandProcessor:
    """Process @ commands from I/O interfaces into action definitions.

    This service is injected into all I/O interface plugins to provide
    consistent @ command handling.
    """

    def __init__(self, app_home: Path | None = None) -> None:
        """Initialize @ command processor.

        Args:
            app_home: Optional APP_HOME path for resolving relative file paths
        """
        self.app_home = app_home

    def is_at_command(self, user_input: str) -> bool:
        """Check if input is an @ command.

        Args:
            user_input: User input string

        Returns:
            True if input starts with @, False otherwise
        """
        return user_input.strip().startswith("@")

    def parse_at_command(
        self,
        user_input: str,
        session_id: str | None = None,
        flow_id: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Parse @ command into action definition(s).

        Args:
            user_input: Input starting with @ (e.g., @/path/to/action.json)
            session_id: Optional session identifier to add to action context
            flow_id: Optional flow identifier to add to action context

        Returns:
            Action definition dict or list of action defs

        Raises:
            RuntimeError: If file not found, invalid JSON, or parse error
        """
        if not self.is_at_command(user_input):
            raise ValueError(f"Input does not start with @: {user_input}")

        # Remove @ prefix and strip whitespace
        content = user_input[1:].strip()

        # Check if inline JSON (starts with { or [)
        if content.startswith("{") or content.startswith("["):
            return self._parse_inline_json(content, session_id, flow_id)

        # Otherwise treat as file path
        return self._parse_file_reference(content, session_id, flow_id)

    def _parse_inline_json(
        self,
        json_str: str,
        session_id: str | None,
        flow_id: str | None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Parse inline JSON action definition.

        Args:
            json_str: JSON string (object or array)
            session_id: Optional session identifier
            flow_id: Optional flow identifier

        Returns:
            Action definition dict or list

        Raises:
            RuntimeError: If invalid JSON
        """
        try:
            action_def = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON in @ command: {e}") from e

        # Add session/flow context to top level only (single source of truth)
        # ActionExecutionEngine merges this into params before calling plugins
        if isinstance(action_def, dict):
            if session_id:
                action_def["session_id"] = session_id
            if flow_id:
                action_def["flow_id"] = flow_id
            return action_def
        elif isinstance(action_def, list):
            for item in action_def:
                if isinstance(item, dict):
                    if session_id:
                        item["session_id"] = session_id
                    if flow_id:
                        item["flow_id"] = flow_id
            return action_def
        else:
            raise RuntimeError(f"@ command JSON must be object or array, got: {type(action_def)}")

    def _parse_file_reference(
        self,
        file_path_str: str,
        session_id: str | None,
        flow_id: str | None,
    ) -> dict[str, Any]:
        """Parse file reference and load action definition.

        Args:
            file_path_str: Path to JSON file (absolute or relative)
            session_id: Optional session identifier
            flow_id: Optional flow identifier

        Returns:
            Action definition dict

        Raises:
            RuntimeError: If file not found or invalid JSON
        """
        # Resolve path (absolute or relative to APP_HOME/actions)
        file_path = Path(file_path_str)

        if not file_path.is_absolute() and self.app_home:
            # Try relative to APP_HOME/actions
            app_home_path = self.app_home / "actions" / file_path_str
            if app_home_path.exists():
                file_path = app_home_path

        if not file_path.exists():
            raise RuntimeError(f"Action file not found: {file_path}")

        # Load JSON
        try:
            with open(file_path) as f:
                action_def = json.load(f)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON in {file_path}: {e}") from e

        if not isinstance(action_def, dict):
            raise RuntimeError(f"Action definition must be object, got: {type(action_def)}")

        # Add session and flow context to top level only (single source of truth)
        # ActionExecutionEngine merges this into params before calling plugins
        if session_id:
            action_def["session_id"] = session_id
        if flow_id:
            action_def["flow_id"] = flow_id

        return action_def
