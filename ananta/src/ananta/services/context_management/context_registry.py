"""Context Registry Service - Create and resolve context IDs.

Manages context_streams table and plugin root contexts.
Contexts are plugin-specific - there is no system-wide "homunculus" context.
"""

import logging
from typing import TYPE_CHECKING, Any, cast

from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.status import is_status_match
from ananta.error_handling import FrameworkError

from .types import (
    NAMESPACE,
    TABLE_CONTEXT_STREAMS,
    ContextStatus,
    ContextType,
)

if TYPE_CHECKING:
    from ananta.services.state_service import StateService

logger = logging.getLogger(__name__)


class ContextRegistryService:
    """Create and resolve context_id values."""

    def __init__(self, state_service: "StateService") -> None:
        """Initialize with state service dependency."""
        self._state_service = state_service

    def get_or_create_plugin_root_context(self, plugin_namespace: str) -> str:
        """Get or create a plugin-specific root context.

        Uses plugin namespace directly as key with GLOBAL scope.
        No prefix needed - namespace IS the unique identifier.

        FAIL-FAST: Verifies that any existing context_id actually exists in
        context_streams. Orphaned IDs indicate data corruption.

        Args:
            plugin_namespace: The plugin's namespace (from plugin.name).

        Returns:
            The context_id for the plugin's root context.

        Raises:
            FrameworkError: If stored context_id doesn't exist in context_streams.
        """
        result = self._state_service.get_key_value(
            namespace=NAMESPACE,
            key=plugin_namespace,
            scope="GLOBAL",
        )
        data = cast(dict[str, Any], result.get("data", {}))
        existing = data.get("value")

        if existing:
            existing_id = str(existing)
            context = self.get_context(existing_id)
            if context:
                logger.debug(
                    f"Plugin root context for '{plugin_namespace}' found: {existing_id}"
                )
                return existing_id
            # Orphaned key-value entry - context_streams was cleared but key-value wasn't.
            # Auto-recover by creating a fresh context (fall through to creation below).
            logger.warning(
                f"STALE_CONTEXT_DETECTED: Plugin root context for '{plugin_namespace}' "
                f"has orphaned key-value entry pointing to non-existent context_streams "
                f"record. Old context_id: {existing_id}. Creating fresh context."
            )

        context_id = self.create_context(
            context_type=ContextType.SOLET,
            label=f"Plugin Root Context: {plugin_namespace}",
        )

        self._state_service.set_key_value(
            namespace=NAMESPACE,
            key=plugin_namespace,
            scope="GLOBAL",
            value=context_id,
        )
        logger.debug(
            f"Created plugin root context for '{plugin_namespace}': {context_id} "
            f"(was_refresh={existing is not None})"
        )
        return context_id

    def create_context(
        self,
        context_type: str,
        label: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = ContextStatus.ACTIVE,
    ) -> str:
        """Create a new context stream.

        FAIL-FAST: Verifies write_state succeeded and generated_id is returned.
        This prevents returning invalid context_ids that don't exist in the database.

        Args:
            context_type: Type of context (homunculus, workflow, task, system)
            label: Human-readable label for the context
            metadata: Additional metadata as dict
            status: Initial status (active, paused, closed)

        Returns:
            The generated context_id.

        Raises:
            FrameworkError: If write_state fails or generated_id is missing.
        """
        result = self._state_service.write_state(
            namespace=NAMESPACE,
            data={
                "table": TABLE_CONTEXT_STREAMS,
                "record": {
                    "context_type": context_type,
                    "label": label,
                    "status": status,
                    "metadata": metadata,
                },
            },
        )

        # FAIL-FAST: Verify write succeeded
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            raise FrameworkError(
                message="Failed to create context stream in database",
                error_code="context_registry.create_failed",
                details={
                    "context_type": context_type,
                    "label": label,
                    "result": result,
                },
            )

        # FAIL-FAST: Verify generated_id is present
        data = cast(dict[str, Any], result.get("data", {}))
        inner_result = cast(dict[str, Any], data.get("result", {}))
        generated_id = inner_result.get("generated_id")

        if not generated_id:
            raise FrameworkError(
                message="Context creation succeeded but no generated_id returned",
                error_code="context_registry.missing_generated_id",
                details={
                    "context_type": context_type,
                    "label": label,
                    "result": result,
                },
            )

        return str(generated_id)

    def get_context(self, context_id: str) -> dict[str, Any] | None:
        """Get context by ID.

        Args:
            context_id: The context ID to look up.

        Returns:
            Context record dict or None if not found.
        """
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={"table": TABLE_CONTEXT_STREAMS, "filters": {"id": context_id}},
        )
        data = cast(dict[str, Any], result.get("data", {}))
        records = cast(list[dict[str, Any]], data.get("records", []))
        return dict(records[0]) if records else None

    def update_context_status(self, context_id: str, status: str) -> None:
        """Update context status.

        Args:
            context_id: The context ID to update.
            status: New status (active, paused, closed).
        """
        self._state_service.update_state(
            namespace=NAMESPACE,
            query={"table": TABLE_CONTEXT_STREAMS, "filters": {"id": context_id}},
            updates={"status": status},
        )
