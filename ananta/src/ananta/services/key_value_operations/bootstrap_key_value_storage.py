"""Bootstrap Key-Value Storage Implementation.

Provides in-memory key-value operations for bootstrap mode operations.
Implements direct runtime values manipulation without plugin delegation.
"""

import logging
import re
from datetime import UTC, datetime
from typing import TypedDict

from ananta.core.domain.types import ActionResult

logger = logging.getLogger(__name__)


class StoredValue(TypedDict):
    """Type for values stored in runtime storage."""

    namespace: str
    key: str
    value: str | int | float | bool | dict[str, object] | list[object] | None
    scope: str
    ttl: int | None
    created_at: str


class BootstrapKeyValueStorage:
    """In-memory key-value operations for bootstrap mode.

    Provides direct runtime values manipulation without external dependencies.
    Used during system initialization before plugins are available.
    """

    def __init__(self, runtime_values: dict[str, StoredValue] | None = None) -> None:
        """Initialize bootstrap key-value storage.

        Args:
            runtime_values: Existing runtime values dict to use (created if None)
        """
        self._runtime_values: dict[str, StoredValue] = (
            runtime_values if runtime_values is not None else {}
        )
        logger.debug("BootstrapKeyValueStorage initialized with in-memory operations")

    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: str | int | float | bool | dict[str, object] | list[object] | None,
        scope: str = "GLOBAL",
        ttl: int | None = None,
    ) -> ActionResult:
        """Set key-value pair directly in memory.

        Args:
            namespace: Target namespace for the key-value pair
            key: Key identifier for the value
            value: The value to store
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")
            ttl: Time-to-live in seconds (None = permanent)

        Returns:
            ActionResult with set success status
        """

        # Create runtime key using the same pattern as StateService
        runtime_key = f"{namespace}.{key}.{scope}"
        self._runtime_values[runtime_key] = {
            "namespace": namespace,
            "key": key,
            "value": value,
            "scope": scope,
            "ttl": ttl,
            "created_at": datetime.now(UTC).isoformat(),
        }

        return {
            "action_status": "completed",
            "data": {},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """Get key-value pair directly from memory.

        Args:
            namespace: Target namespace to query
            key: Key identifier to retrieve
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")

        Returns:
            ActionResult with retrieved value or not found status
        """

        runtime_key = f"{namespace}.{key}.{scope}"

        if runtime_key in self._runtime_values:
            runtime_value = self._runtime_values[runtime_key]

            return {
                "action_status": "completed",
                "data": {
                    "key": runtime_value["key"],
                    "value": runtime_value["value"],
                    "namespace": runtime_value["namespace"],
                    "scope": runtime_value["scope"],
                    "ttl": runtime_value.get("ttl"),
                    "created_at": runtime_value.get("created_at"),
                },
                "actions": [],
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        else:
            return {
                "action_status": "completed",
                "data": {"key_found": False},
                "actions": [],
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }

    def delete_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """Delete key-value pair directly from memory.

        Args:
            namespace: Target namespace for deletion
            key: Key identifier to delete
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")

        Returns:
            ActionResult with deletion success status
        """

        runtime_key = f"{namespace}.{key}.{scope}"
        deleted = False

        if runtime_key in self._runtime_values:
            del self._runtime_values[runtime_key]
            deleted = True
        else:
            pass

        return {
            "action_status": "completed",
            "data": {"deleted": deleted},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def clear_key_values(
        self, namespace: str | None = None, scope: str | None = None
    ) -> ActionResult:
        """Clear key-value pairs by namespace and/or scope.

        Args:
            namespace: Optional namespace filter (None = all namespaces)
            scope: Optional scope filter (None = all scopes)

        Returns:
            ActionResult with clear operation status
        """

        keys_to_delete = []

        # Find matching keys based on filters
        for runtime_key in self._runtime_values.keys():
            key_parts = runtime_key.split(".")
            if len(key_parts) >= 3:
                key_namespace = ".".join(key_parts[:-2])  # Everything except last 2 parts
                key_scope = key_parts[-1]  # Last part is scope

                # Check if this key matches the filters
                namespace_match = namespace is None or key_namespace == namespace
                scope_match = scope is None or key_scope == scope

                if namespace_match and scope_match:
                    keys_to_delete.append(runtime_key)

        # Delete matching keys
        for runtime_key in keys_to_delete:
            del self._runtime_values[runtime_key]

        deleted_count = len(keys_to_delete)

        return {
            "action_status": "completed",
            "data": {"deleted_count": deleted_count},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def list_key_values(
        self,
        namespace: str | None = None,
        scope: str | None = None,
        pattern: str | None = None,
    ) -> ActionResult:
        """List key-value pairs with optional filtering.

        Args:
            namespace: Optional namespace filter (None = all namespaces)
            scope: Optional scope filter (None = all scopes)
            pattern: Optional key pattern filter (None = no pattern filtering)

        Returns:
            ActionResult with matching key-value pairs
        """

        matching_values = []

        # Compile pattern if provided
        compiled_pattern = re.compile(pattern) if pattern else None

        # Filter runtime values based on criteria
        for runtime_key, runtime_value in self._runtime_values.items():
            key_parts = runtime_key.split(".")
            if len(key_parts) >= 3:
                key_namespace = ".".join(key_parts[:-2])  # Everything except last 2 parts
                actual_key = key_parts[-2]  # Second to last part is the key
                key_scope = key_parts[-1]  # Last part is scope

                # Check filters
                namespace_match = namespace is None or key_namespace == namespace
                scope_match = scope is None or key_scope == scope
                pattern_match = compiled_pattern is None or compiled_pattern.search(actual_key)

                if namespace_match and scope_match and pattern_match:
                    matching_values.append(
                        {
                            "namespace": runtime_value["namespace"],
                            "key": runtime_value["key"],
                            "value": runtime_value["value"],
                            "scope": runtime_value["scope"],
                            "ttl": runtime_value.get("ttl"),
                            "created_at": runtime_value.get("created_at"),
                        }
                    )

        return {
            "action_status": "completed",
            "data": {"values": matching_values},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }
