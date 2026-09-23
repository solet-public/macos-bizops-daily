import logging
from typing import TYPE_CHECKING, Any

from ananta.core.domain.error_codes import ErrorCode
from ananta.error_handling import FrameworkError
from ananta.types.schema_standardizer import SchemaStandardizer
from ananta.types.schema_types import SchemaDefinition

if TYPE_CHECKING:
    from ananta.services.state_service import StateService

logger = logging.getLogger(__name__)


class SchemaManager:
    def __init__(
        self,
        state_service: "StateService",
        plugin_schema_service: Any,
    ):
        """SchemaManager orchestrates startup schema initialization.

        Args:
            state_service: The platform state service.
            plugin_schema_service: The plugin-schema lifecycle service. Every
                namespace's schema is installed via ``install_plugin_schema``,
                which emits Postgres-native DDL, writes ownership rows in the
                same transaction, and adopts pre-existing live tables instead
                of failing. Required — there is no legacy direct-create fallback.
        """
        self._state_service = state_service
        self._plugin_schema_service = plugin_schema_service
        self._initialized_schemas: set[str] = set()
        self._schema_registry: dict[str, SchemaDefinition] = {}
        self._standardizer = SchemaStandardizer()

    def initialize_schemas(
        self, schema_definitions: list[SchemaDefinition], already_standardized: bool = False
    ) -> None:
        for schema_def in schema_definitions:
            # Standardize schema to add standard fields (id, external_id, etc.)
            # Skip if schemas are already standardized (e.g., from get_standardized_core_schemas)
            if already_standardized:
                standardized_schema = schema_def
            else:
                standardized_schema = self._standardizer.standardize_schema(schema_def)

            errors = standardized_schema.validate()
            if errors:
                raise FrameworkError(
                    message=f"Invalid schema '{standardized_schema.namespace}': {'; '.join(errors)}",
                    error_code=ErrorCode.VALIDATION_ERROR,
                    details={"namespace": standardized_schema.namespace, "errors": errors},
                )

            self._install_via_lifecycle(standardized_schema)

            self._schema_registry[standardized_schema.namespace] = standardized_schema

    def _install_via_lifecycle(self, schema_def: SchemaDefinition) -> None:
        """Install one namespace via the plugin-schema lifecycle service.

        The lifecycle handles: CREATE TABLE / trigger / indexes (resolved
        physical names) / ownership-row writes — all in one transaction.
        On pre-existing live tables, it adopts (column-type normalization +
        index reconciliation). Idempotent: re-running on identical shape
        bumps ``updated_at`` only.
        """
        from ananta.services.plugin_schema_service.serialization import to_json

        schema_key = self._schema_key(schema_def)
        if schema_key in self._initialized_schemas:
            return

        try:
            result = self._plugin_schema_service.install_plugin_schema(
                schema_def.namespace, to_json(schema_def)
            )
            logger.debug(
                "lifecycle install: %s — %s", schema_def.namespace, result.get("status")
            )
            self._initialized_schemas.add(schema_key)
        except Exception as e:
            raise FrameworkError(
                message=f"Failed to install schema '{schema_key}' via plugin_schema_service",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={"namespace": schema_key, "error": str(e)},
                original_error=e,
            ) from e

    def _schema_key(self, schema_def: SchemaDefinition) -> str:
        table_names = sorted(schema_def.tables.keys())
        return f"{schema_def.namespace}::{','.join(table_names)}"

    def get_schema(self, namespace: str) -> SchemaDefinition | None:
        return self._schema_registry.get(namespace)

    def is_initialized(self, namespace: str) -> bool:
        return namespace in self._initialized_schemas

    def list_schemas(self) -> list[str]:
        return list(self._schema_registry.keys())

    def list_initialized_schemas(self) -> list[str]:
        return list(self._initialized_schemas)
