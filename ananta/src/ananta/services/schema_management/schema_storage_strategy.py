"""Schema Storage Strategy Protocol.

Defines the interface for different schema storage implementations.
Implements Strategy pattern for bootstrap vs plugin mode operations.
"""

from typing import Protocol

from ananta.core.domain.types import ActionResult


class SchemaStorageStrategy(Protocol):
    """Protocol defining schema storage operations.

    Implementations handle bootstrap mode (in-memory) vs plugin mode (delegation).
    """

    def create_schema(self, namespace: str, standardized_schema: dict[str, object]) -> ActionResult:
        """Create schema in the appropriate storage backend.

        Args:
            namespace: Target namespace for schema
            standardized_schema: Schema with standard fields already added

        Returns:
            ActionResult with operation status and details

        Raises:
            FrameworkError: If schema creation fails
        """
        ...

    def describe_schema(self, namespace: str) -> ActionResult:
        """Retrieve schema definition from storage backend.

        Args:
            namespace: Target namespace to describe

        Returns:
            ActionResult with schema definition or error details

        Raises:
            FrameworkError: If schema retrieval fails
        """
        ...

    def initialize_database(self) -> ActionResult:
        """Initialize the database backend.

        Returns:
            ActionResult indicating initialization success/failure

        Raises:
            FrameworkError: If database initialization fails
        """
        ...
