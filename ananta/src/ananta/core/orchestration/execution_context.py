"""
ExecutionContext - Runtime placeholder resolution for multi-step workflows.

Stores action results and resolves placeholders before action dispatch.
Implements fail-fast error handling with clear error messages.

Phase 1 Implementation:
    pass
- Flat placeholder resolution (<<KEY>> only)
- Schema-based result normalization (lowercase → UPPERCASE)
- Per-flow lifecycle management
- Type-safe storage (Python objects, not JSON strings)

Phase 2 (Future):
    pass
- Step-specific references (<<STEP_1.KEY>>)
- Dotted path navigation (<<STEP_1.RESULT.field>>)
- Case-insensitive matching
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PlaceholderResolutionError(Exception):
    """
    Raised when a placeholder cannot be resolved at runtime.

    Provides clear context for debugging:
        pass
    - Which placeholder failed
    - Which step requested it
    - What placeholders are available
    - Suggestions for fixing
    """

    def __init__(
        self,
        placeholder: str,
        step_id: str | None = None,
        available_keys: list[str] | None = None,
        suggestion: str | None = None,
    ) -> None:
        self.placeholder = placeholder
        self.step_id = step_id
        self.available_keys = available_keys or []
        self.suggestion = suggestion

        # Build detailed error message
        parts = [f"Placeholder '{placeholder}' not found in execution context"]

        if step_id:
            parts.append(f"  Requested by step: {step_id}")

        if available_keys:
            keys_str = ", ".join(sorted(available_keys))
            parts.append(f"  Available placeholders: {keys_str}")
        else:
            parts.append("  No placeholders available in context")

        if suggestion:
            parts.append(f"  Suggestion: {suggestion}")

        super().__init__("\n".join(parts))


class ExecutionContext:
    """
    Stores action results and resolves placeholders for workflow execution.

    Lifecycle:
        pass
    - Created when first action in a flow is processed
    - Destroyed when flow completes or fails
    - Keyed by flow_id (one context per concurrent flow)

    Storage format:
        pass
    - Flat dictionary mapping UPPERCASE keys to typed Python values
    - Keys derived from return_value_schema properties (lowercase → UPPERCASE)
    - Example: {"PROCESS_COUNT": 60, "PROCESS_KEYS": ["plugin::...", ...]}

    Phase 1 limitations:
        pass
    - Flat keys only (no dotted paths)
    - Latest binding semantics (newer results overwrite older)
    - No step-specific references (all placeholders resolve to latest value)

    Example usage:
        >>> context = ExecutionContext("flow_123")
        >>> result = {"process_count": 60, "process_keys": ["a", "b"]}
        >>> schema = {"properties": {"process_count": {}, "process_keys": {}}}
        >>> context.store_result("node_0", result, schema)
        >>> context.resolve_placeholder("<<PROCESS_COUNT>>")
        60
        >>> context.resolve_placeholder("<<PROCESS_KEYS>>")
        ['a', 'b']
    """

    def __init__(self, flow_id: str) -> None:
        """
        Initialize execution context for a flow.

        Args:
            flow_id: Unique flow identifier
        """
        self.flow_id = flow_id
        self._context: dict[str, Any] = {}
        logger.debug(f"ExecutionContext: Created context for flow {flow_id}")

    def store_result(
        self, step_id: str, result: dict[str, Any], schema: dict[str, Any] | None
    ) -> None:
        """
        Store action result with schema-based normalization to UPPERCASE keys.

        Args:
            step_id: Unique step identifier (e.g., "node_0", "action_123")
            result: Action result dictionary (Python objects, not JSON strings)
            schema: Return value schema with properties definition (optional)

        Behavior:
            pass
        - Extract properties from schema.properties
        - For each present property in schema, extract value from result[property_name]
        - Store as context[PROPERTY_NAME.upper()] = value (latest binding)
        - Phase 2: Also store as context[STEP_ID.PROPERTY_NAME.upper()] (step-specific binding)
        - Phase 2: Store full result as context[STEP_ID.RESULT] for dotted path navigation
        - If schema is None, store entire result as context["RESULT"] = result
        - Skip missing schema properties declared optional; fail fast for required properties

        Example:
            >>> schema = {
            ...     "properties": {
            ...         "process_count": {"type": "integer"},
            ...         "process_keys": {"type": "array"}
            ...     }
            ... }
            >>> result = {"process_count": 60, "process_keys": ["a", "b"]}
            >>> context.store_result("node_0", result, schema)
            >>> # Phase 1: {"PROCESS_COUNT": 60, "PROCESS_KEYS": ["a", "b"]}
            >>> # Phase 2: {"PROCESS_COUNT": 60, "PROCESS_KEYS": ["a", "b"],
            >>> #            "node_0.PROCESS_COUNT": 60, "node_0.PROCESS_KEYS": ["a", "b"],
            >>> #            "node_0.RESULT": {"process_count": 60, "process_keys": ["a", "b"]}}

        Raises:
            PlaceholderResolutionError: If a required schema property is not found in result
        """
        logger.debug(f"ExecutionContext[{self.flow_id}]: Storing result for step {step_id}")

        # Phase 2: Always store full result for dotted path navigation
        step_result_key = f"{step_id}.RESULT"
        self._context[step_result_key] = result

        if schema is None or "properties" not in schema:
            # No schema - store entire result as RESULT placeholder
            self._context["RESULT"] = result
            logger.debug(
                f"ExecutionContext[{self.flow_id}]: No schema provided, stored as <<RESULT>>"
            )
            return

        properties = schema.get("properties")
        if not isinstance(properties, dict):
            # Invalid schema - store entire result as fallback
            self._context["RESULT"] = result
            logger.error(
                f"ExecutionContext[{self.flow_id}]: Invalid schema properties (not dict), stored as <<RESULT>>"
            )
            return

        # Extract each property from result based on schema
        for prop_name, property_metadata in properties.items():
            if prop_name not in result:
                is_optional = (
                    isinstance(property_metadata, dict)
                    and property_metadata.get("required", False) is False
                )
                if is_optional:
                    logger.debug(
                        f"ExecutionContext[{self.flow_id}]: Optional schema property "
                        f"'{prop_name}' not present in result; skipping storage"
                    )
                    continue

                # Property defined in schema but not present in result
                available_keys = ", ".join(sorted(result.keys()))
                logger.error(
                    f"ExecutionContext[{self.flow_id}]: Schema property '{prop_name}' not found in result. "
                    f"Available keys: {available_keys}"
                )
                raise PlaceholderResolutionError(
                    placeholder=f"<<{prop_name.upper()}>>",
                    step_id=step_id,
                    available_keys=list(result.keys()),
                    suggestion=f"Check that the action returns '{prop_name}' in its result. "
                    f"The return_value_schema may be outdated or incorrect.",
                )

            # Phase 1: Store with UPPERCASE key (latest binding)
            uppercase_key = prop_name.upper()
            value = result[prop_name]
            self._context[uppercase_key] = value

            # Phase 2: Also store with step-specific key
            step_specific_key = f"{step_id}.{uppercase_key}"
            self._context[step_specific_key] = value

            # Log type for debugging
            value_type = type(value).__name__
            if isinstance(value, list | dict):
                logger.debug(
                    f"ExecutionContext[{self.flow_id}]: Stored <<{uppercase_key}>> and <<{step_specific_key}>> = {value_type} "
                    f"(len={len(value)})"
                )
            else:
                logger.debug(
                    f"ExecutionContext[{self.flow_id}]: Stored <<{uppercase_key}>> and <<{step_specific_key}>> = {value_type}"
                )

    def resolve_placeholder(self, placeholder: str) -> Any:
        """
        Resolve <<PLACEHOLDER>> to its value in the execution context.

        Args:
            placeholder: Placeholder token WITH delimiters (e.g., "<<PROCESS_COUNT>>")

        Returns:
            Resolved value (any Python type: str, int, list, dict, etc.)

        Behavior:
            pass
        - Strip << >> delimiters
        - Phase 1: Simple lookup for flat keys (<<PROCESS_COUNT>>)
        - Phase 2: Navigate dotted paths (<<node_0.RESULT.process_count>>)
        - Return typed Python value (NOT stringified JSON)
        - Fail fast if placeholder not found

        Examples:
            >>> context.resolve_placeholder("<<PROCESS_COUNT>>")
            60
            >>> context.resolve_placeholder("<<node_0.PROCESS_COUNT>>")
            60
            >>> context.resolve_placeholder("<<node_0.RESULT.process_count>>")
            60

        Raises:
            PlaceholderResolutionError: If placeholder not found in context
        """
        # Strip << >> delimiters
        if placeholder.startswith("<<") and placeholder.endswith(">>"):
            key = placeholder[2:-2]
        else:
            key = placeholder

        # Phase 2: Check if this is a dotted path
        if "." in key:
            return self._resolve_dotted_path(key, placeholder)

        # Phase 1: Simple flat key lookup (convert to uppercase)
        uppercase_key = key.upper()

        # Check if key exists in context
        if uppercase_key not in self._context:
            available_keys = list(self._context.keys())
            logger.error(
                f"ExecutionContext[{self.flow_id}]: Placeholder '{placeholder}' not found. "
                f"Available: {', '.join(available_keys)}"
            )
            raise PlaceholderResolutionError(
                placeholder=placeholder,
                available_keys=available_keys,
                suggestion="Ensure a previous step provides this value in its return_value_schema, "
                "or check that the placeholder name matches the schema property (case-insensitive).",
            )

        # Return value (preserve type)
        value = self._context[uppercase_key]
        _value_type = type(value).__name__

        return value

    def _resolve_dotted_path(self, path: str, original_placeholder: str) -> Any:
        """Resolve a dotted path placeholder like node_0.RESULT.process_count."""
        parts = path.split(".")

        # Try direct lookup for 2-part paths (step_id.FIELD)
        if len(parts) == 2:
            result = self._try_direct_two_part_lookup(parts, original_placeholder)
            if result is not None:
                return result

        # Try step.RESULT pattern for 2+ part paths
        if len(parts) >= 2 and parts[1].upper() == "RESULT":
            return self._resolve_step_result_path(parts, original_placeholder)

        # Fallback: Navigate step by step
        return self._resolve_fallback_path(parts, original_placeholder)

    def _try_direct_two_part_lookup(
        self, parts: list[str], original_placeholder: str
    ) -> Any | None:
        """Try direct lookup for 2-part paths. Returns None if not found."""
        lookup_key = f"{parts[0]}.{parts[1].upper()}"
        if lookup_key in self._context:
            value = self._context[lookup_key]
            return value
        return None

    def _resolve_step_result_path(self, parts: list[str], original_placeholder: str) -> Any:
        """Resolve step.RESULT path pattern."""
        step_result_key = f"{parts[0]}.RESULT"
        if step_result_key not in self._context:
            raise PlaceholderResolutionError(
                placeholder=original_placeholder,
                available_keys=list(self._context.keys()),
                suggestion=f"Could not find RESULT for step '{parts[0]}'. "
                f"Available keys: {', '.join(k for k in self._context.keys() if k.startswith(parts[0]))}",
            )

        current = self._context[step_result_key]

        if len(parts) > 2:
            current = self._navigate_dict_path(current, parts[2:], original_placeholder)

        return current

    def _resolve_fallback_path(self, parts: list[str], original_placeholder: str) -> Any:
        """Resolve path using step-by-step navigation."""
        first_part = parts[0]
        if first_part.upper() in self._context:
            current = self._context[first_part.upper()]
        elif first_part in self._context:
            current = self._context[first_part]
        else:
            raise PlaceholderResolutionError(
                placeholder=original_placeholder,
                available_keys=list(self._context.keys()),
                suggestion=f"Path segment '{first_part}' not found in context. "
                f"Ensure the step has completed and stored results.",
            )

        if len(parts) > 1:
            current = self._navigate_dict_path(current, parts[1:], original_placeholder)

        return current

    def _navigate_dict_path(
        self, current: Any, remaining_parts: list[str], original_placeholder: str
    ) -> Any:
        """Navigate through nested dict using remaining path parts."""
        for part in remaining_parts:
            if not isinstance(current, dict):
                raise PlaceholderResolutionError(
                    placeholder=original_placeholder,
                    suggestion=f"Cannot navigate into {type(current).__name__} at path segment '{part}'. "
                    f"Expected dict/object type.",
                )

            current = self._get_dict_field_case_insensitive(current, part, original_placeholder)

        return current

    def _get_dict_field_case_insensitive(
        self, data: dict[str, Any], field: str, original_placeholder: str
    ) -> Any:
        """Get field from dict with case-insensitive fallback."""
        if field in data:
            return data[field]
        if field.lower() in data:
            return data[field.lower()]
        if field.upper() in data:
            return data[field.upper()]

        available_fields = list(data.keys())
        raise PlaceholderResolutionError(
            placeholder=original_placeholder,
            available_keys=available_fields,
            suggestion=f"Field '{field}' not found in result object. "
            f"Available fields: {', '.join(available_fields)}",
        )

    def has_placeholder(self, placeholder: str) -> bool:
        """
        Check if a placeholder exists in the context.

        Args:
            placeholder: Placeholder token WITH delimiters (e.g., "<<PROCESS_COUNT>>")

        Returns:
            True if placeholder can be resolved, False otherwise

        Example:
            >>> context.has_placeholder("<<PROCESS_COUNT>>")
            True
            >>> context.has_placeholder("<<UNKNOWN>>")
            False
        """
        # Strip << >> delimiters
        if placeholder.startswith("<<") and placeholder.endswith(">>"):
            key = placeholder[2:-2]
        else:
            key = placeholder

        # Convert to uppercase for lookup
        uppercase_key = key.upper()

        return uppercase_key in self._context

    def get_context_for_step(self, step_id: str) -> dict[str, Any]:
        """
        Get all available context for a specific step.

        Args:
            step_id: Step identifier

        Returns:
            Dictionary of all available placeholder values

        Behavior:
            pass
        - Phase 1: Returns entire context (no step-specific filtering)
        - Phase 2: Will return only context available at step's position

        Example:
            >>> context.get_context_for_step("node_1")
            {"PROCESS_COUNT": 60, "PROCESS_KEYS": [...]}
        """
        # Phase 1: Return entire context (no step-specific filtering)
        return dict(self._context)  # Return a copy to prevent external modification

    def clear(self) -> None:
        """
        Clear all stored results from context.

        Behavior:
            pass
        - Called when flow completes or fails
        - Frees memory by removing all stored results
        """
        num_keys = len(self._context)
        self._context.clear()
        logger.debug(f"ExecutionContext[{self.flow_id}]: Cleared context ({num_keys} keys removed)")

    def __repr__(self) -> str:
        """String representation for debugging."""
        keys = ", ".join(sorted(self._context.keys()))
        return f"ExecutionContext(flow_id={self.flow_id}, keys=[{keys}])"


class ExecutionContextManager:
    """
    Manages ExecutionContext instances for concurrent flows.

    Responsibilities:
        pass
    - Create context when flow starts
    - Retrieve context for action processing
    - Destroy context when flow completes
    - Prevent memory leaks from abandoned flows

    Example usage:
        >>> manager = ExecutionContextManager()
        >>> manager.create_context("flow_123")
        >>> context = manager.get_context("flow_123")
        >>> context.store_result(...)
        >>> manager.destroy_context("flow_123")
    """

    def __init__(self) -> None:
        """Initialize context manager with empty context registry."""
        self._contexts: dict[str, ExecutionContext] = {}
        logger.debug("ExecutionContextManager: Initialized")

    def create_context(self, flow_id: str) -> ExecutionContext:
        """
        Create new execution context for a flow.

        Args:
            flow_id: Unique flow identifier

        Returns:
            New ExecutionContext instance

        Behavior:
            pass
        - If context already exists for flow_id, reuse existing (idempotent)
        - Log creation for observability
        """
        if flow_id in self._contexts:
            return self._contexts[flow_id]

        context = ExecutionContext(flow_id)
        self._contexts[flow_id] = context
        logger.debug(f"ExecutionContextManager: Created context for flow {flow_id}")
        return context

    def get_context(self, flow_id: str) -> ExecutionContext | None:
        """
        Get execution context for a flow.

        Args:
            flow_id: Flow identifier

        Returns:
            ExecutionContext instance, or None if not found

        Example:
            >>> context = manager.get_context("flow_123")
            >>> if context:
                pass
            ...     value = context.resolve_placeholder("<<KEY>>")
        """
        return self._contexts.get(flow_id)

    def has_context(self, flow_id: str) -> bool:
        """
        Check if context exists for a flow.

        Args:
            flow_id: Flow identifier

        Returns:
            True if context exists, False otherwise
        """
        return flow_id in self._contexts

    def destroy_context(self, flow_id: str) -> None:
        """
        Destroy execution context for a completed flow.

        Args:
            flow_id: Flow identifier

        Behavior:
            pass
        - Clear context data
        - Remove from registry
        - Log destruction for observability
        - If context doesn't exist, log warning but don't error
        """
        if flow_id in self._contexts:
            self._contexts[flow_id].clear()
            del self._contexts[flow_id]
            logger.debug(f"ExecutionContextManager: Destroyed context for flow {flow_id}")
        else:
            logger.error(f"ExecutionContextManager: No context found for flow {flow_id} to destroy")

    def get_active_flows(self) -> list[str]:
        """
        Get list of active flow IDs with contexts.

        Returns:
            List of flow_id strings

        Example:
            >>> manager.get_active_flows()
            ['flow_123', 'flow_456']
        """
        return list(self._contexts.keys())

    def cleanup_stale_contexts(
        self, _max_age_seconds: int = 3600
    ) -> None:  # Reserved for interface compatibility
        """
        Clean up contexts older than max_age_seconds.

        Args:
            max_age_seconds: Maximum age for contexts (default: 1 hour)

        Note:
            Phase 1: Not implemented (contexts destroyed when flows complete)
            Phase 2: Will track creation time and clean up stale contexts
        """
        # Future enhancement: track context creation time and clean up stale ones

    def __repr__(self) -> str:
        """String representation for debugging."""
        return f"ExecutionContextManager(active_flows={len(self._contexts)})"
