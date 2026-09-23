"""Bootstrap Schema Storage Implementation.

Provides in-memory schema storage for bootstrap mode operations.
Implements direct schema manipulation without plugin delegation.
"""

import logging
from datetime import UTC, datetime

from ananta.core.domain.types import ActionResult

logger = logging.getLogger(__name__)


class BootstrapSchemaStorage:
    """In-memory schema storage for bootstrap mode.

    Provides direct schema manipulation without external dependencies.
    Used during system initialization before plugins are available.
    """

    def __init__(self, schemas_dict: dict[str, dict[str, object]] | None = None) -> None:
        """Initialize bootstrap storage.

        Args:
            schemas_dict: Existing schemas dictionary to use (created if None)
        """
        self._schemas = schemas_dict if schemas_dict is not None else {}
        logger.debug("BootstrapSchemaStorage initialized with in-memory storage")

    def create_schema(self, namespace: str, standardized_schema: dict[str, object]) -> ActionResult:
        """Store schema directly in memory.

        Args:
            namespace: Target namespace for schema
            standardized_schema: Schema with standard fields already added

        Returns:
            ActionResult with creation success status
        """
        logger.debug(f"Bootstrap mode - storing schema for namespace: {namespace}")

        # Direct in-memory implementation - no plugin delegation needed
        self._schemas[namespace] = standardized_schema

        logger.debug(f"Bootstrap mode - stored standardized schema for namespace: {namespace}")

        return {
            "action_status": "completed",
            "data": {},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def describe_schema(self, namespace: str) -> ActionResult:
        """Retrieve schema from memory.

        Args:
            namespace: Target namespace to describe

        Returns:
            ActionResult with schema definition (empty dict if namespace not found)

        Note:
            Returns empty schema for non-existent namespaces as this is a query operation.
            This is intentional behavior, not a fallback code violation.
        """

        # Explicit behavior: return empty schema for non-existent namespaces
        # This is a query operation - returning empty result is semantically correct
        schema = self._schemas.get(namespace, {})

        return {
            "action_status": "completed",
            "data": {"schema": schema},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def initialize_database(self) -> ActionResult:
        """Initialize bootstrap storage (no-op).

        Bootstrap mode uses in-memory storage, so no database initialization needed.

        Returns:
            ActionResult indicating initialization success
        """
        logger.debug("Bootstrap mode - database initialization skipped (using in-memory storage)")

        return {
            "action_status": "completed",
            "data": {"message": "Bootstrap mode - no database initialization required"},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }
