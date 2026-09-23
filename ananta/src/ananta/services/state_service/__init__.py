import logging
import secrets
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Protocol, TypedDict, cast, runtime_checkable

from ananta.constants import DEFAULT_STATE_MANAGEMENT_PLUGIN as DEFAULT_STATE_MANAGEMENT_PLUGIN
from ananta.core.domain.enums import ErrorSeverity
from ananta.core.domain.types import ActionResult
from ananta.error_handling import FrameworkError
from ananta.interfaces.bootstrappable_service_interface import (
    BootstrappableServiceInterface,
)
from ananta.interfaces.state_management_interface import StateManagementInterface
from ananta.services.async_job_operations import (
    AsyncJobOperationService,
)
from ananta.services.database_operations import (
    BootstrapDatabaseStorage,
    DatabaseOperationService,
    PluginDatabaseStorage,
)
from ananta.services.key_value_operations import (
    BootstrapKeyValueStorage,
    KeyValueOperationService,
    KeyValueValidator,
    PluginKeyValueStorage,
)
from ananta.services.schema_management import (
    BootstrapSchemaStorage,
    NamespaceValidator,
    PluginSchemaStorage,
    SchemaManagementService,
)
from ananta.services.string_generation_operations import (
    StringGenerationService,
)
from ananta.types.schema_standardizer import (
    SchemaStandardizer,
)

logger = logging.getLogger(__name__)


# TypedDict definitions for complex data structures
class PluginActionConfig(TypedDict):
    database_name: str
    max_connections: int
    connection_timeout: int
    enable_wal_mode: bool
    enable_foreign_keys: bool
    log_level: str


class ActionParameters(TypedDict, total=False):
    action: dict[str, str]
    calling_service: str
    calling_namespace: str
    namespace: str
    schema: dict[str, object]
    query: dict[str, object]
    data: dict[str, object]
    filters: dict[str, object]
    sql: str
    job_id: str
    updates: dict[str, object]
    key: str
    value: object
    scope: str
    ttl: int | None


class BootstrapData(TypedDict):
    memory_data: dict[str, dict[str, list[object]]]
    schemas: dict[str, dict[str, object]]
    runtime_values: dict[str, object]


class AsyncJobData(TypedDict, total=False):
    job_id: str
    status: str
    result: dict[str, object] | None
    error: dict[str, object] | None
    created_at: str
    updated_at: str
    ttl: int | None


class KeyValueRecord(TypedDict):
    namespace: str
    key: str
    value: object
    scope: str
    ttl: int | None
    created_at: str
    updated_at: str


class GenerateStringResult(TypedDict):
    string: str
    actual_length: int


class ColumnDefinition(TypedDict, total=False):
    type: str
    primary_key: bool
    nullable: bool
    default: object
    unique: bool
    foreign_key: str
    auto_increment: bool


# Protocol interfaces for service dependencies
@runtime_checkable
class PluginManagerProtocol(Protocol):
    def execute_action(
        self,
        plugin_name: str,
        params: dict[str, object],
        state: dict[str, object],
        APP_HOME: str,
        plugin_config: dict[str, object],
    ) -> dict[str, object]: ...

    def get_plugin(self, plugin_name: str) -> object: ...


@runtime_checkable
class SchemaManagerProtocol(Protocol):
    """Protocol for SchemaManager to avoid circular imports."""

    def get_schema(self, namespace: str) -> object | None:
        """Get SchemaDefinition for namespace, or None if not found."""
        ...


class StateService(BootstrappableServiceInterface):
    def __init__(
        self,
        plugin_manager: PluginManagerProtocol | None = None,
        app_home: str = "",
        state_plugin_name: str | None = None,
    ) -> None:
        """
        Initialize StateService.

        Args:
            plugin_manager: Plugin manager instance (None for bootstrap mode)
            app_home: Application home directory
            state_plugin_name: Name of state management plugin to use (required in plugin mode)
                              If None in plugin mode, will fail fast with clear error.
        """
        self.app_home = app_home

        # ARCHITECTURE: Plugin name explicitly provided or from environment
        # Fail fast if plugin_manager exists but no plugin name available
        if plugin_manager is not None:
            if state_plugin_name is None:
                # Try environment variable set by launch script
                import os

                state_plugin_name = os.environ.get("ANANTA_STATE_PLUGIN")

            if state_plugin_name is None:
                raise ValueError(
                    "state_plugin_name must be provided when using plugin mode. "
                    "Set ANANTA_STATE_PLUGIN environment variable or pass state_plugin_name parameter. "
                    "The launch script should detect and set the correct plugin name."
                )

        # NO FALLBACK - fail fast if not provided
        # In bootstrap mode (plugin_manager=None), state_plugin_name can be None
        self._state_plugin_name = state_plugin_name
        self._transitioning = False  # Flag to track state transitions
        self._schema_standardizer = SchemaStandardizer()  # Handle standard field creation
        self._plugin_validated = False  # Track if plugin validation has been done

        # State management plugin instance (initialized in _init_plugin)
        self._state_plugin: StateManagementInterface | None = None

        # Initialize SchemaManagementService (will be configured in _init_bootstrap/_init_plugin)
        self._schema_management_service: SchemaManagementService | None = None

        # Initialize DatabaseOperationService (will be configured in _init_bootstrap/_init_plugin)
        self._database_operation_service: DatabaseOperationService | None = None

        # Initialize KeyValueOperationService (will be configured in _init_bootstrap/_init_plugin)
        self._key_value_operation_service: KeyValueOperationService | None = None

        # Initialize AsyncJobOperationService (will be configured in _init_bootstrap/_init_plugin)
        self._async_job_operation_service: AsyncJobOperationService | None = None

        # Initialize StringGenerationService (stateless service, initialized once)
        self._string_generation_service = StringGenerationService()

        # SchemaManager reference for looking up table metadata like id_prefix
        # Set after SchemaManager is created in startup sequence
        self._schema_manager: SchemaManagerProtocol | None = None

        # ARCHITECTURE: StateService operates in single-threaded action processing model
        # No locks needed - ActionQueuePoller processes actions sequentially

        # Call parent constructor which handles bootstrap vs plugin mode
        super().__init__(plugin_manager)

    def _init_bootstrap(self) -> None:
        self.memory_data: defaultdict[str, defaultdict[str, list[object]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.schemas: dict[str, dict[str, object]] = {}
        self.runtime_values: dict[str, object] = {}

        # Initialize SchemaManagementService with bootstrap storage strategy
        bootstrap_storage = BootstrapSchemaStorage(self.schemas)
        self._schema_management_service = SchemaManagementService(
            storage_strategy=bootstrap_storage,
            namespace_validator=NamespaceValidator(),
            schema_standardizer=self._schema_standardizer,
        )

        # Initialize DatabaseOperationService with bootstrap storage strategy
        bootstrap_db_storage = BootstrapDatabaseStorage(self.memory_data)
        self._database_operation_service = DatabaseOperationService(
            storage_strategy=bootstrap_db_storage,
            namespace_validator=NamespaceValidator(),
        )

        # Initialize KeyValueOperationService with bootstrap storage strategy
        bootstrap_kv_storage = BootstrapKeyValueStorage(self.runtime_values)  # type: ignore[arg-type]
        # SAFE: runtime_values dict has correct StoredValue structure at runtime, explicit cast adds overhead
        self._key_value_operation_service = KeyValueOperationService(
            storage_strategy=bootstrap_kv_storage,
            validator=KeyValueValidator(),
        )

        # Initialize AsyncJobOperationService with database operations delegation
        self._async_job_operation_service = AsyncJobOperationService(
            database_operation_service=self._database_operation_service,
        )

        logger.debug("StateService initialized in bootstrap mode with in-memory storage")
        logger.debug("SchemaManagementService configured with bootstrap storage strategy")
        logger.debug("DatabaseOperationService configured with bootstrap storage strategy")
        logger.debug("KeyValueOperationService configured with bootstrap storage strategy")
        logger.debug("AsyncJobOperationService configured with database operations delegation")

    def _init_plugin(self) -> None:
        # Get state management plugin instance from plugin manager
        if self.plugin_manager is None:
            raise RuntimeError(
                "Cannot initialize StateService in plugin mode without PluginManager"
            )

        plugin_manager = cast(PluginManagerProtocol, self.plugin_manager)
        plugin_name = self._state_plugin_name
        if plugin_name is None:
            raise RuntimeError("State plugin name not configured")
        self._state_plugin = cast(StateManagementInterface, plugin_manager.get_plugin(plugin_name))

        logger.debug(f"StateService got plugin instance: {self._state_plugin_name}")

        # CRITICAL: Notify plugin it's an active interface provider
        setter = getattr(self._state_plugin, "set_as_active_provider", None)
        if callable(setter):
            setter("StateManagementInterface")
            logger.debug(
                f"Notified {self._state_plugin_name} that it's active StateManagementInterface provider"
            )

        # Initialize SchemaManagementService with plugin storage strategy
        plugin_storage = PluginSchemaStorage(self._state_plugin)
        self._schema_management_service = SchemaManagementService(
            storage_strategy=plugin_storage,
            namespace_validator=NamespaceValidator(),
            schema_standardizer=self._schema_standardizer,
        )

        # Initialize DatabaseOperationService with plugin storage strategy
        plugin_db_storage = PluginDatabaseStorage(self._state_plugin)
        self._database_operation_service = DatabaseOperationService(
            storage_strategy=plugin_db_storage,
            namespace_validator=NamespaceValidator(),
        )

        # Initialize KeyValueOperationService with plugin storage strategy
        plugin_kv_storage = PluginKeyValueStorage(self._state_plugin)
        self._key_value_operation_service = KeyValueOperationService(
            storage_strategy=plugin_kv_storage,
            validator=KeyValueValidator(),
        )

        # Initialize AsyncJobOperationService with database operations delegation
        self._async_job_operation_service = AsyncJobOperationService(
            database_operation_service=self._database_operation_service,
        )

        logger.debug(
            "StateService initialized in plugin mode (plugin validation deferred until first use)"
        )
        logger.debug("SchemaManagementService configured with plugin storage strategy")
        logger.debug("DatabaseOperationService configured with plugin storage strategy")
        logger.debug("KeyValueOperationService configured with plugin storage strategy")
        logger.debug("AsyncJobOperationService configured with database operations delegation")

    def set_schema_manager(self, schema_manager: SchemaManagerProtocol) -> None:
        """Set the SchemaManager reference for looking up table metadata like id_prefix.

        Called by startup sequence after SchemaManager is created.
        """
        self._schema_manager = schema_manager

    def _validate_state_plugin(self) -> StateManagementInterface:
        """Validate that state plugin exists and is available.

        Returns:
            The state plugin typed as StateManagementInterface

        Raises:
            FrameworkError: If plugin not found or doesn't implement interface
        """
        if self._state_plugin is None:
            raise FrameworkError(
                message="State plugin not initialized",
                error_code="state_service.plugin_not_initialized",
                details={"plugin_name": self._state_plugin_name},
                severity=ErrorSeverity.ERROR,
            )

        return self._state_plugin

    def _ensure_ready(self) -> StateManagementInterface:
        """Ensure state plugin exists, implements interface, and is ready.

        Returns:
            The state plugin typed as StateManagementInterface

        Raises:
            FrameworkError: If plugin not found, doesn't implement interface, or not ready
        """
        plugin = self._validate_state_plugin()

        # READINESS CONTRACT: Verify plugin is ready before use
        if not plugin.is_ready():
            error = plugin.readiness_error or "Unknown readiness error"
            raise FrameworkError(f"State plugin '{self._state_plugin_name}' not ready: {error}")

        return plugin

    def initialize_database(self, config: dict[str, object] | None = None) -> ActionResult:
        """Initialize database using SchemaManagementService.

        Delegates to SchemaManagementService which handles appropriate
        initialization strategy (bootstrap vs plugin mode).

        Args:
            config: Reserved interface parameter, unused in this implementation

        Returns:
            ActionResult with initialization status

        Raises:
            FrameworkError: If schema management service is not initialized
            RuntimeError: If database initialization fails
        """
        # Note: config is unused in this implementation
        _ = config
        if self._schema_management_service is None:
            raise FrameworkError(
                message="SchemaManagementService not initialized",
                error_code="state_service.schema_service_not_initialized",
                details={"method": "initialize_database"},
                severity=ErrorSeverity.ERROR,
            )

        logger.debug(
            "PHASE3: StateService delegating database initialization to SchemaManagementService"
        )

        try:
            result = self._schema_management_service.initialize_database()

            if result.get("action_status") == "completed":
                logger.debug("PHASE3: Database initialization completed successfully")
            else:
                error = result.get("error", "Unknown error")
                logger.error(f"PHASE3: Database initialization failed: {error}")
                raise RuntimeError(f"Database initialization failed: {error}")

            return result

        except Exception as e:
            logger.error(f"PHASE3: Database initialization error: {e}")
            raise

    def _capture_bootstrap_state(self) -> dict[str, object]:
        # ARCHITECTURE: Direct access to bootstrap data (single-threaded processing)

        data_dict = dict(self.memory_data)
        schemas_dict = dict(self.schemas)
        runtime_dict = dict(self.runtime_values)

        result: dict[str, object] = {
            "data": data_dict,
            "schemas": schemas_dict,
            "runtime_values": runtime_dict,
        }
        return result

    def _restore_bootstrap_data(self, data: dict[str, object]) -> None:
        logger.debug("Skipping bootstrap data restoration to avoid circular dependency deadlock")

        # NOTE: The bootstrap data has already served its purpose during the bootstrap phase.
        # During the transition to plugin mode, we should NOT try to call plugin methods
        # as this creates the circular dependency deadlock:
        # 1. Plugin is initializing (holds database locks)
        # 2. We try to call plugin methods to restore data
        # 3. Plugin methods try to access the same database
        # 4. Deadlock occurs
        #
        # The correct approach is to let the plugin initialize naturally and recreate
        # any necessary data through normal operation, not forced restoration.

        schemas = data.get("schemas", {})
        schemas_count = len(schemas) if isinstance(schemas, dict) else 0

        data_obj = data.get("data", {})
        data_tables = sum(
            len(tables) if isinstance(tables, dict) else 0
            for tables in (data_obj.values() if isinstance(data_obj, dict) else [])
        )

        runtime_values = data.get("runtime_values", {})
        runtime_values_count = len(runtime_values) if isinstance(runtime_values, dict) else 0

        logger.debug(
            f"Bootstrap transition completed - {schemas_count} schemas, {data_tables} data tables, {runtime_values_count} runtime values were preserved in memory during bootstrap phase"
        )
        logger.debug("Plugin will recreate necessary data structures during normal operation")

    def transition_to_plugin(self, plugin_manager: object) -> None:
        """Override to set transitioning flag during the transition."""
        self._transitioning = True
        try:
            # Call parent transition method
            super().transition_to_plugin(plugin_manager)
        finally:
            # Always clear the flag, even if transition fails
            self._transitioning = False

    def _validate_namespace(self, namespace: str) -> None:
        if not namespace:
            raise FrameworkError(
                message="Namespace must be a non-empty string",
                error_code="state_service.invalid_namespace",
                details={"provided_namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        if not namespace.replace("_", "").replace("-", "").isalnum():
            raise FrameworkError(
                message="Namespace must contain only alphanumeric characters, hyphens, and underscores",
                error_code="state_service.invalid_namespace_format",
                details={"provided_namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

    def _add_standard_field_values_to_data(
        self, data: dict[str, object], namespace: str
    ) -> dict[str, object]:
        """
        ARCHITECTURAL CONTROL #1: StateService populates standard field values
        Plugin receives data with all standard field values already populated
        """
        enhanced_data = dict(data)

        # Extract table name for ID generation
        table_name = enhanced_data.get("table")
        logger.debug(f"STANDARD-FIELDS: Processing namespace={namespace}, table={table_name}")

        # Handle both single record and multiple records
        if "record" in enhanced_data:
            # Single record
            record_obj = enhanced_data["record"]
            if isinstance(record_obj, dict):
                record = dict(record_obj)
                logger.debug(f"STANDARD-FIELDS: Before populate - record has id={record.get('id')}")
                record = self._populate_standard_fields_in_record(
                    record, namespace, str(table_name) if table_name else None
                )
                logger.debug(f"STANDARD-FIELDS: After populate - record has id={record.get('id')}")
                enhanced_data["record"] = record
        elif "records" in enhanced_data:
            # Multiple records
            records_obj = enhanced_data["records"]
            if isinstance(records_obj, list):
                records: list[dict[str, object]] = []
                for record_item in records_obj:
                    if isinstance(record_item, dict):
                        enhanced_record = dict(record_item)
                        enhanced_record = self._populate_standard_fields_in_record(
                            enhanced_record, namespace, str(table_name) if table_name else None
                        )
                        records.append(enhanced_record)
                enhanced_data["records"] = records

        return enhanced_data

    def _populate_standard_fields_in_record(
        self, record: dict[str, object], namespace: str, table_name: str | None = None
    ) -> dict[str, object]:
        """Populate standard field values in a single record."""
        enhanced_record = dict(record)
        self._ensure_namespace(enhanced_record, namespace)
        self._ensure_id(enhanced_record, namespace, table_name)
        self._ensure_name_from_id(enhanced_record)
        self._ensure_audit_fields(enhanced_record)
        return enhanced_record

    def _ensure_namespace(self, record: dict[str, object], namespace: str) -> None:
        """Add namespace if missing (required NOT NULL field)."""
        if "namespace" not in record:
            record["namespace"] = namespace

    def _ensure_id(self, record: dict[str, object], namespace: str, table_name: str | None) -> None:
        """Generate ID if missing and table context is available."""
        if "id" in record and record.get("id") is not None:
            return

        logger.debug(f"STANDARD-FIELDS: ID is missing, table_name={table_name}")
        if table_name:
            generated_id = self._generate_table_id(namespace, table_name)
            logger.debug(
                f"STANDARD-FIELDS: Generated ID={generated_id} for {namespace}__{table_name}"
            )
            record["id"] = generated_id
        else:
            logger.error("STANDARD-FIELDS: Cannot generate ID - no table_name provided")

    def _ensure_name_from_id(self, record: dict[str, object]) -> None:
        """Default name to ID if not provided (standard table behavior)."""
        if "name" in record and record.get("name") is not None:
            return
        if "id" in record and record.get("id") is not None:
            record["name"] = record["id"]

    def _ensure_audit_fields(self, record: dict[str, object]) -> None:
        """Add created_by/updated_by audit fields if not present."""
        if "created_by" not in record:
            record["created_by"] = "ananta.services.state_service"
        if "updated_by" not in record:
            record["updated_by"] = "ananta.services.state_service"

    def _get_table_id_prefix(self, namespace: str, table_name: str) -> str | None:
        """Look up id_prefix for a table from SchemaManager's registry.

        Args:
            namespace: Plugin namespace
            table_name: Table name within namespace

        Returns:
            The id_prefix if defined, None otherwise
        """
        if self._schema_manager is None:
            return None

        # SchemaManager.get_schema returns SchemaDefinition with tables dict
        schema_def = self._schema_manager.get_schema(namespace)
        if schema_def is None:
            return None

        # Access tables attribute from SchemaDefinition
        tables = getattr(schema_def, "tables", None)
        if not isinstance(tables, dict):
            return None

        # Get TableSchema for the table
        table_schema = tables.get(table_name)
        if table_schema is None:
            return None

        # Access id_prefix from TableSchema
        id_prefix = getattr(table_schema, "id_prefix", None)
        return id_prefix if isinstance(id_prefix, str) else None

    def _generate_table_id(self, namespace: str, table_name: str) -> str:
        """Generate ID using id_prefix from schema.

        Core table prefixes are hardcoded (available at compile time).
        Plugin table prefixes are looked up from schema registry.
        """
        # Core table prefixes - hardcoded because they're needed before
        # schema registry is available at startup
        CORE_TABLE_PREFIXES = {
            "job": "job",
            "job_payload": "jpl",
            "asynchronous_jobs": "ajb",
            "process_registry": "proc",
            "action_definitions": "ad",
            "action_metrics": "am",
            "schema_registry": "sr",
            "key_value_store": "kv",
            "logs": "log",
            "sessions": "sess",
            "flows": "flow",
            "flow_tokens": "ft",
            "action_events": "ae",
            "orchestrator_state": "orch",
            "workflow_patterns": "wp",
            "process_chains": "pc",
            "event_bus_events": "evt",
            "usage_stats": "stat",
            "action_results": "ar",
            "result_processing_violations": "rpv",
            "test_runs": "tr",
            "test_results": "tres",
            "memory_events": "me",
            # Context management tables
            "context_streams": "ctx",
            "context_events": "ctxe",
            "context_sessions": "cs",
            "context_snapshots": "cxs",
        }

        # Create full table name (namespace__table)
        full_table_name = (
            f"{namespace}__{table_name}"
            if not table_name.startswith(f"{namespace}__")
            else table_name
        )

        # Check core tables first (by table_name, not full_table_name)
        prefix = CORE_TABLE_PREFIXES.get(table_name) if namespace == "core" else None

        # For non-core tables, look up id_prefix from schema registry
        if prefix is None:
            prefix = self._get_table_id_prefix(namespace, table_name)
            if prefix is None:
                raise FrameworkError(
                    message=f"No id_prefix defined for table '{full_table_name}'",
                    error_code="state_service.missing_id_prefix",
                    details={"namespace": namespace, "table": table_name},
                )

        # Epoch offset for shorter timestamps (2020-01-01)
        EPOCH = 1577836800000
        timestamp_ms = int(time.time() * 1000) - EPOCH
        timestamp_b36 = self._string_generation_service.to_base36(timestamp_ms).zfill(8)

        # Random component (5 chars in base36 = ~60M possibilities)
        random_b36 = self._string_generation_service.to_base36(secrets.randbits(26)).zfill(5)[:5]

        return f"{prefix}-{timestamp_b36}{random_b36}"

    def create_schema(self, namespace: str, schema: dict[str, object]) -> ActionResult:
        """Create schema using SchemaManagementService.

        Delegates to SchemaManagementService which handles validation, standardization,
        and appropriate storage strategy (bootstrap vs plugin mode).

        Args:
            namespace: Target namespace for schema creation
            schema: Schema definition to create

        Returns:
            ActionResult with creation status

        Raises:
            FrameworkError: If schema management service is not initialized or creation fails
        """
        if self._schema_management_service is None:
            raise FrameworkError(
                message="SchemaManagementService not initialized",
                error_code="state_service.schema_service_not_initialized",
                details={"method": "create_schema", "namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        logger.debug(
            f"Delegating schema creation to SchemaManagementService for namespace: {namespace}"
        )
        return self._schema_management_service.create_schema(namespace, schema)

    def write_state(
        self,
        namespace: str,
        data: dict[str, object],
        calling_service: str | None = None,
        calling_namespace: str | None = None,
    ) -> ActionResult:
        """Write state using DatabaseOperationService.

        Delegates to DatabaseOperationService which handles validation and
        appropriate storage strategy (bootstrap vs plugin mode).
        Maintains architectural controls like transition detection and field enhancement.

        Args:
            namespace: Target namespace for data writing
            data: Data to write
            calling_service: Optional calling service identifier
            calling_namespace: Optional calling namespace

        Returns:
            ActionResult with write status and details

        Raises:
            FrameworkError: If database operation service is not initialized or write fails
        """
        # CRITICAL: Prevent state writes during transitions to avoid circular dependency
        if self._transitioning:
            # Silently drop writes during transition to prevent deadlock
            return {
                "action_status": "completed",
                "data": {},
                "actions": [],
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }

        if self._database_operation_service is None:
            raise FrameworkError(
                message="DatabaseOperationService not initialized",
                error_code="state_service.database_service_not_initialized",
                details={"method": "write_state", "namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        logger.debug(f"StateService.write_state called for namespace: {namespace}")

        # ARCHITECTURAL CONTROL #1: StateService populates standard field values
        enhanced_data = self._add_standard_field_values_to_data(data, namespace)
        logger.debug("Enhanced data with standard field values")

        # Delegate to DatabaseOperationService
        logger.debug("Delegating write operation to DatabaseOperationService")
        return self._database_operation_service.write_state(
            namespace, enhanced_data, calling_service, calling_namespace
        )

    def read_state(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Read state using DatabaseOperationService.

        Delegates to DatabaseOperationService which handles validation and
        appropriate storage access (bootstrap vs plugin mode).

        Args:
            namespace: Target namespace to read from
            query: Query parameters for data retrieval

        Returns:
            ActionResult with retrieved data

        Raises:
            FrameworkError: If database operation service is not initialized or read fails
        """
        if self._database_operation_service is None:
            raise FrameworkError(
                message="DatabaseOperationService not initialized",
                error_code="state_service.database_service_not_initialized",
                details={"method": "read_state", "namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        return self._database_operation_service.read_state(namespace, query)

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> ActionResult:
        """Update state using DatabaseOperationService.

        Delegates to DatabaseOperationService which handles validation and
        appropriate storage operations (bootstrap vs plugin mode).

        Args:
            namespace: Target namespace for updates
            query: Query to identify records to update
            updates: Update operations to apply

        Returns:
            ActionResult with update status and details

        Raises:
            FrameworkError: If database operation service is not initialized or update fails
        """
        if self._database_operation_service is None:
            raise FrameworkError(
                message="DatabaseOperationService not initialized",
                error_code="state_service.database_service_not_initialized",
                details={"method": "update_state", "namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        logger.debug(
            f"Delegating update operation to DatabaseOperationService for namespace: {namespace}"
        )
        return self._database_operation_service.update_state(namespace, query, updates)

    def upsert_state(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Upsert state using DatabaseOperationService.

        Delegates to DatabaseOperationService which handles validation and
        appropriate storage operations (bootstrap vs plugin mode).

        IMPORTANT: This method expects the caller to provide an ID for conflict detection.
        For inserting new records without an ID, use write_state() instead.

        Args:
            namespace: Target namespace for upsert
            data: Data containing table, record, and conflict_columns

        Returns:
            ActionResult with upsert status and details

        Raises:
            FrameworkError: If database operation service is not initialized or upsert fails
        """
        if self._database_operation_service is None:
            raise FrameworkError(
                message="DatabaseOperationService not initialized",
                error_code="state_service.database_service_not_initialized",
                details={"method": "upsert_state", "namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        logger.debug(
            f"Delegating upsert operation to DatabaseOperationService for namespace: {namespace}"
        )
        return self._database_operation_service.upsert_state(namespace, data)

    def delete_records(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Delete records using DatabaseOperationService.

        Delegates to DatabaseOperationService which handles validation and
        appropriate storage access (bootstrap vs plugin mode).

        Args:
            namespace: Target namespace for deletions
            query: Query to identify records to delete

        Returns:
            ActionResult with deletion status and details

        Raises:
            FrameworkError: If database operation service is not initialized or deletion fails
        """
        if self._database_operation_service is None:
            raise FrameworkError(
                message="DatabaseOperationService not initialized",
                error_code="state_service.database_service_not_initialized",
                details={"method": "delete_records", "namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        return self._database_operation_service.delete_records(namespace, query)

    def query_state(self, namespace: str, filters: dict[str, object]) -> ActionResult:
        """DEPRECATED alias for :meth:`read_state` — prefer ``read_state``.

        ``query_state`` is a second NAME for the ``read_state`` primitive, not a
        second primitive: every backend implements it by delegating (the
        postgres and rds plugins literally ``return self.read_state(...)``, and
        ``BootstrapDatabaseStorage`` now does too). New code should call
        :meth:`read_state` directly, or :meth:`query_ordered` when it wants a
        page rather than a complete filtered set. Existing call sites are being
        migrated in waves; this is not scheduled for removal until they are done.

        **The ``filters`` parameter is the WHOLE QUERY, not just its filter
        clause.** The name is historical and actively misleading — the dict is
        passed through unchanged and becomes ``read_state``'s ``query``, so it
        takes the same envelope::

            {"table": str,
             "filters": dict,        # the actual filter clause
             "limit": int,           # optional — compiled into SQL LIMIT
             "unbounded": bool}      # optional — consent to a scan over the cap

        In particular ``limit`` and ``unbounded`` WORK HERE and always have. A
        2026-08-15 census recorded these call sites as structurally unable to
        bound themselves; that was wrong, and it was wrong because nothing in
        this signature or docstring said the slot existed. It does. Pass a
        ``limit``. See ``ananta.services.state_service.read_bounds`` for the
        bound and why an over-cap read is refused rather than truncated.

        Args:
            namespace: Target namespace to query
            filters: The ``{table, filters, limit?, unbounded?}`` query envelope
                described above — NOT merely a filter mapping.

        Returns:
            ActionResult with the records, or error details

        Raises:
            FrameworkError: If database operation service is not initialized or query fails
        """
        if self._database_operation_service is None:
            raise FrameworkError(
                message="DatabaseOperationService not initialized",
                error_code="state_service.database_service_not_initialized",
                details={"method": "query_state", "namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        return self._database_operation_service.query_state(namespace, filters)

    def query_ordered(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Ordered, bounded, tie-safe query using DatabaseOperationService.

        Delegates to DatabaseOperationService which handles validation and
        appropriate storage access (bootstrap vs plugin mode).

        Args:
            namespace: Target namespace to query
            data: ``{table, filters, order_by, limit, after?, include_deleted?}``

        Returns:
            ActionResult with the ordered page in ``data.records``

        Raises:
            FrameworkError: If database operation service is not initialized or query fails
        """
        if self._database_operation_service is None:
            raise FrameworkError(
                message="DatabaseOperationService not initialized",
                error_code="state_service.database_service_not_initialized",
                details={"method": "query_ordered", "namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        return self._database_operation_service.query_ordered(namespace, data)

    def acquire_lease(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Atomically acquire an expiry-fenced lease on a single row.

        Delegates directly to the bound state plugin's ``acquire_lease`` —
        like :meth:`transactional`, this is a runtime concurrency primitive
        (the compare-and-set is meaningless in bootstrap mode and needs no
        standard-field injection), so it bypasses the DatabaseOperationService
        bootstrap/standard-field path. See
        :meth:`ananta.interfaces.state_management_interface.StateManagementInterface.acquire_lease`
        for the ``data`` contract and the ``{"acquired": bool}`` result.

        Raises:
            FrameworkError: If no plugin is bound (bootstrap mode).
        """
        if self._state_plugin is None:
            raise FrameworkError(
                message="StateService.acquire_lease requires a bound state plugin",
                error_code="state_service.acquire_lease_unavailable",
                details={"namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )
        return self._state_plugin.acquire_lease(namespace, data)

    def count(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Count rows in a filtered set (single scalar; no rows shipped).

        Like :meth:`acquire_lease`/:meth:`transactional`, the aggregate verbs
        BYPASS the DatabaseOperationService bootstrap path and delegate
        directly to the bound state plugin's facade. Their only consumers run
        in plugin mode (flow execution / inside ``transactional()``), so a
        bootstrap aggregate would be unreachable-by-construction and could only
        return a silently-wrong count off the crude in-memory shim — fail loud
        instead (Architect ruling A', 2026-06-22). See
        :meth:`ananta.interfaces.state_management_interface.StateManagementInterface.count`
        for the ``data`` contract and the ``data.result.value`` result.

        Raises:
            FrameworkError: If no plugin is bound (bootstrap mode).
        """
        return self._validate_state_plugin().count(namespace, data)

    def max_value(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Largest value of a column over a filtered set (bypass to plugin; A').

        See :meth:`count` for the bypass rationale and
        :meth:`ananta.interfaces.state_management_interface.StateManagementInterface.max_value`
        for the contract.

        Raises:
            FrameworkError: If no plugin is bound (bootstrap mode).
        """
        return self._validate_state_plugin().max_value(namespace, data)

    def min_value(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Smallest value of a column over a filtered set (bypass to plugin; A').

        See :meth:`count` for the bypass rationale and
        :meth:`ananta.interfaces.state_management_interface.StateManagementInterface.min_value`
        for the contract.

        Raises:
            FrameworkError: If no plugin is bound (bootstrap mode).
        """
        return self._validate_state_plugin().min_value(namespace, data)

    def transactional(self) -> object:
        """Open an atomic multi-statement transaction.

        Delegates to the underlying state-management plugin's
        ``transactional()`` context manager (see
        :class:`ananta.interfaces.state_management_interface.StateTransaction`).
        Required by callers that need read-modify-write atomicity (e.g.
        agent_messaging cursor allocation).

        Raises:
            FrameworkError: If no plugin is bound (bootstrap mode) or
                the bound plugin does not expose ``transactional()``.
        """
        if self._state_plugin is None:
            raise FrameworkError(
                message="StateService.transactional requires a bound state plugin",
                error_code="state_service.transactional_unavailable",
                details={},
                severity=ErrorSeverity.ERROR,
            )
        fn = getattr(self._state_plugin, "transactional", None)
        if not callable(fn):
            raise FrameworkError(
                message=(
                    f"State plugin {type(self._state_plugin).__name__} does not "
                    "implement transactional()"
                ),
                error_code="state_service.transactional_not_implemented",
                details={"plugin": type(self._state_plugin).__name__},
                severity=ErrorSeverity.ERROR,
            )
        return fn()

    def execute_sql(
        self,
        sql_query: str,
        sql_params: list[object] | None = None,
        calling_service: str = "StateService",
        calling_namespace: str = "ananta.services.state_service",
    ) -> ActionResult:
        """Execute SQL using DatabaseOperationService.

        Delegates to DatabaseOperationService which handles appropriate
        storage access (bootstrap vs plugin mode).

        Args:
            sql_query: SQL query string to execute
            sql_params: Optional parameters for the SQL query
            calling_service: Reserved interface parameter, unused in this implementation
            calling_namespace: Reserved interface parameter, unused in this implementation

        Returns:
            ActionResult with query results or error details

        Raises:
            FrameworkError: If database operation service is not initialized or SQL execution fails
        """
        if self._database_operation_service is None:
            raise FrameworkError(
                message="DatabaseOperationService not initialized",
                error_code="state_service.database_service_not_initialized",
                details={"method": "execute_sql", "sql_query": sql_query[:100]},
                severity=ErrorSeverity.ERROR,
            )

        logger.debug(f"Delegating SQL execution to DatabaseOperationService: {sql_query[:100]}...")
        if sql_params:
            logger.debug(f"With parameters: {sql_params}")

        return self._database_operation_service.execute_sql(sql_query, sql_params)

    def list_namespaces(self) -> ActionResult:
        if self.bootstrap_mode:
            # ARCHITECTURE: Direct bootstrap namespace access (single-threaded processing)
            namespaces = list(self.memory_data.keys())
            return {
                "action_status": "completed",
                "data": {"namespaces": namespaces},
                "actions": [],
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        else:
            # Plugin mode - delegate to state plugin
            plugin = self._ensure_ready()
            return plugin.list_namespaces()

    def describe_schema(self, namespace: str) -> ActionResult:
        """Describe schema using SchemaManagementService.

        Delegates to SchemaManagementService which handles validation and
        appropriate storage access (bootstrap vs plugin mode).

        Args:
            namespace: Target namespace to describe

        Returns:
            ActionResult with schema definition

        Raises:
            FrameworkError: If schema management service is not initialized or retrieval fails
        """
        if self._schema_management_service is None:
            raise FrameworkError(
                message="SchemaManagementService not initialized",
                error_code="state_service.schema_service_not_initialized",
                details={"method": "describe_schema", "namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        return self._schema_management_service.describe_schema(namespace)

    def mark_as_read(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Mark records as read/processed.

        Delegates to state plugin which handles record updates.

        Args:
            namespace: Target namespace
            query: Query dict containing table and record_ids

        Returns:
            ActionResult with update status

        Raises:
            FrameworkError: If in bootstrap mode (not supported) or plugin not initialized
        """
        if self.bootstrap_mode:
            # Bootstrap mode doesn't support mark_as_read (no read_at timestamps)
            return {
                "action_status": "completed",
                "data": {"updated": 0, "message": "mark_as_read not supported in bootstrap mode"},
                "actions": [],
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }

        plugin = self._ensure_ready()
        return plugin.mark_as_read(namespace, query)

    def generate_unique_string(self, length: int = 13, encoding: str = "base36") -> ActionResult:
        """Generate cryptographically secure random string using StringGenerationService.

        Delegates to StringGenerationService which handles string generation with
        proper validation, encoding support, and cryptographic security.

        Args:
            length: Length of random string (1-64 chars, default: 13 to match existing patterns)
            encoding: Encoding format ('base36', 'hex', or 'uuid')

        Returns:
            ActionResult with generated random string and metadata

        Security:
            Uses secrets module for cryptographically secure random generation
        """
        return self._string_generation_service.generate_unique_string(length, encoding)

    def generate_id(self, length: int = 13, prefix: str = "") -> str:
        """Generate a unique ID string.

        Convenience method that returns the string directly instead of ActionResult.
        Raises RuntimeError on failure.

        Args:
            length: Length of random portion (1-64 chars, default: 13)
            prefix: Optional prefix for the ID (e.g., "voice-", "flow-")

        Returns:
            Generated ID string, optionally prefixed

        Raises:
            RuntimeError: If string generation fails
        """
        result = self.generate_unique_string(length=length, encoding="base36")

        if result.get("action_status") != "completed":
            error_msg: object = result.get("error", {})
            raise RuntimeError(f"Failed to generate ID: {error_msg}")

        data = result.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Invalid response format from generate_unique_string")

        random_string = data.get("random_string")
        if not isinstance(random_string, str) or not random_string:
            raise RuntimeError("Missing or invalid random_string in response")

        return f"{prefix}{random_string}" if prefix else random_string

    def _validate_key_value_parameters(
        self, namespace: str, key: str, scope: str, ttl: int | None
    ) -> None:
        """Validate all parameters for set_key_value method."""
        self._validate_key_value_namespace(namespace)
        self._validate_key_value_key(key)
        self._validate_key_value_scope(scope)
        self._validate_key_value_ttl(ttl)

    def _validate_key_value_namespace(self, namespace: str) -> None:
        """Validate namespace parameter for key-value operations."""
        if not namespace:
            raise FrameworkError(
                message="Namespace must be a non-empty string",
                error_code="state_service.invalid_runtime_namespace",
                details={"provided_namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

    def _validate_key_value_key(self, key: str) -> None:
        """Validate key parameter for key-value operations."""
        if not key:
            raise FrameworkError(
                message="Key must be a non-empty string",
                error_code="state_service.invalid_runtime_key",
                details={"provided_key": key},
                severity=ErrorSeverity.ERROR,
            )

    def _validate_key_value_scope(self, scope: str) -> None:
        """Validate scope parameter for key-value operations."""
        if scope not in ["GLOBAL", "SESSION", "FLOW"]:
            raise FrameworkError(
                message="Scope must be one of: GLOBAL, SESSION, FLOW",
                error_code="state_service.invalid_runtime_scope",
                details={
                    "provided_scope": scope,
                    "valid_scopes": ["GLOBAL", "SESSION", "FLOW"],
                },
                severity=ErrorSeverity.ERROR,
            )

    def _validate_key_value_ttl(self, ttl: int | None) -> None:
        """Validate TTL parameter for key-value operations."""
        if ttl is not None and ttl <= 0:
            raise FrameworkError(
                message="TTL must be a positive integer or None",
                error_code="state_service.invalid_runtime_ttl",
                details={"provided_ttl": ttl},
                severity=ErrorSeverity.ERROR,
            )

    # Async Job Management Interface - Delegates to AsyncJobOperationService
    def create_async_job(
        self,
        job_id: str,
        provider_type: str,
        provider: str,
        action_name: str,
        request_data: dict[str, object],
    ) -> ActionResult:
        """Create a new async job entry using AsyncJobOperationService.

        Delegates to AsyncJobOperationService which handles job creation
        through database operations.

        Args:
            job_id: Unique identifier for the job
            provider_type: Type of provider ("service" or "plugin")
            provider: Name of the provider handling the job
            action_name: Name of the action being executed
            request_data: Job request parameters and metadata

        Returns:
            ActionResult indicating success or failure

        Raises:
            FrameworkError: If async job operation service is not initialized or creation fails
        """
        if self._async_job_operation_service is None:
            raise FrameworkError(
                message="AsyncJobOperationService not initialized",
                error_code="state_service.async_job_service_not_initialized",
                details={"method": "create_async_job", "job_id": job_id},
                severity=ErrorSeverity.ERROR,
            )

        return self._async_job_operation_service.create_async_job(
            job_id, provider_type, provider, action_name, request_data
        )

    def get_async_job(self, job_id: str) -> ActionResult:
        """Retrieve async job by job ID using AsyncJobOperationService.

        Delegates to AsyncJobOperationService which handles job retrieval
        through database operations.

        Args:
            job_id: Unique identifier for the job to retrieve

        Returns:
            ActionResult with job data or error information

        Raises:
            FrameworkError: If async job operation service is not initialized or retrieval fails
        """
        if self._async_job_operation_service is None:
            raise FrameworkError(
                message="AsyncJobOperationService not initialized",
                error_code="state_service.async_job_service_not_initialized",
                details={"method": "get_async_job", "job_id": job_id},
                severity=ErrorSeverity.ERROR,
            )

        return self._async_job_operation_service.get_async_job(job_id)

    def update_async_job(self, job_id: str, updates: dict[str, object]) -> ActionResult:
        """Update async job with new data using AsyncJobOperationService.

        Delegates to AsyncJobOperationService which handles job updates
        through database operations.

        Args:
            job_id: Unique identifier for the job to update
            updates: Dictionary of fields to update

        Returns:
            ActionResult indicating update success or failure

        Raises:
            FrameworkError: If async job operation service is not initialized or update fails
        """
        if self._async_job_operation_service is None:
            raise FrameworkError(
                message="AsyncJobOperationService not initialized",
                error_code="state_service.async_job_service_not_initialized",
                details={"method": "update_async_job", "job_id": job_id},
                severity=ErrorSeverity.ERROR,
            )

        return self._async_job_operation_service.update_async_job(job_id, updates)

    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: str | int | float | bool | dict[str, object] | list[object] | None,
        scope: str = "GLOBAL",
        ttl: int | None = None,
    ) -> ActionResult:
        """Set key-value pair using KeyValueOperationService.

        Delegates to KeyValueOperationService which handles validation and
        appropriate storage strategy (bootstrap vs plugin mode).

        Args:
            namespace: Namespace for the value (e.g., "console.aliases", "template.variables")
            key: Key identifier for the value
            value: The value to store (will be JSON-serialized if complex)
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")
            ttl: Time-to-live in seconds (None = permanent)

        Returns:
            ActionResult with success/failure status

        Raises:
            FrameworkError: If key-value operation service is not initialized or operation fails
        """
        if self._key_value_operation_service is None:
            raise FrameworkError(
                message="KeyValueOperationService not initialized",
                error_code="state_service.key_value_service_not_initialized",
                details={"method": "set_key_value", "namespace": namespace, "key": key},
                severity=ErrorSeverity.ERROR,
            )

        return self._key_value_operation_service.set_key_value(namespace, key, value, scope, ttl)

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """Get key-value pair using KeyValueOperationService.

        Delegates to KeyValueOperationService which handles validation and
        appropriate storage strategy (bootstrap vs plugin mode).

        Args:
            namespace: Target namespace to query
            key: Key identifier to retrieve
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")

        Returns:
            ActionResult with retrieved value or error details

        Raises:
            FrameworkError: If key-value operation service is not initialized or operation fails
        """
        if self._key_value_operation_service is None:
            raise FrameworkError(
                message="KeyValueOperationService not initialized",
                error_code="state_service.key_value_service_not_initialized",
                details={"method": "get_key_value", "namespace": namespace, "key": key},
                severity=ErrorSeverity.ERROR,
            )

        return self._key_value_operation_service.get_key_value(namespace, key, scope)

    def delete_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """Delete key-value pair using KeyValueOperationService.

        Delegates to KeyValueOperationService which handles validation and
        appropriate storage strategy (bootstrap vs plugin mode).

        Args:
            namespace: Target namespace for deletion
            key: Key identifier to delete
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")

        Returns:
            ActionResult with operation status and details

        Raises:
            FrameworkError: If key-value operation service is not initialized or operation fails
        """
        if self._key_value_operation_service is None:
            raise FrameworkError(
                message="KeyValueOperationService not initialized",
                error_code="state_service.key_value_service_not_initialized",
                details={"method": "delete_key_value", "namespace": namespace, "key": key},
                severity=ErrorSeverity.ERROR,
            )

        return self._key_value_operation_service.delete_key_value(namespace, key, scope)

    def clear_key_values(
        self, namespace: str | None = None, scope: str | None = None
    ) -> ActionResult:
        """Clear key-value pairs using KeyValueOperationService.

        Delegates to KeyValueOperationService which handles validation and
        appropriate storage strategy (bootstrap vs plugin mode).

        Args:
            namespace: Namespace to clear (None = all namespaces)
            scope: Scope to clear (None = all scopes)

        Returns:
            ActionResult with data.deleted_count showing number of values cleared

        Raises:
            FrameworkError: If key-value operation service is not initialized or operation fails
        """
        if self._key_value_operation_service is None:
            raise FrameworkError(
                message="KeyValueOperationService not initialized",
                error_code="state_service.key_value_service_not_initialized",
                details={"method": "clear_key_values", "namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        return self._key_value_operation_service.clear_key_values(namespace, scope)

    def _validate_clear_key_values_parameters(
        self, namespace: str | None, scope: str | None
    ) -> None:
        """Validate parameters for clear_key_values operation."""
        if namespace is not None and not namespace:
            raise FrameworkError(
                message="Namespace must be a non-empty string or None",
                error_code="state_service.invalid_runtime_namespace",
                details={"provided_namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        if scope is not None and scope not in ["GLOBAL", "SESSION", "FLOW"]:
            raise FrameworkError(
                message="Scope must be one of: GLOBAL, SESSION, FLOW, or None",
                error_code="state_service.invalid_runtime_scope",
                details={
                    "provided_scope": scope,
                    "valid_scopes": ["GLOBAL", "SESSION", "FLOW", None],
                },
                severity=ErrorSeverity.ERROR,
            )

    def _clear_bootstrap_runtime_values(self, namespace: str | None, scope: str | None) -> int:
        """Clear runtime values in bootstrap mode and return count."""
        deleted_count = 0
        keys_to_delete = []

        for runtime_key, runtime_data in self.runtime_values.items():
            if not isinstance(runtime_data, dict):
                continue
            match_namespace = namespace is None or runtime_data.get("namespace") == namespace
            match_scope = scope is None or runtime_data.get("scope") == scope

            if match_namespace and match_scope:
                keys_to_delete.append(runtime_key)

        for key in keys_to_delete:
            del self.runtime_values[key]
            deleted_count += 1

        return deleted_count

    def _create_clear_key_values_response(self, deleted_count: int) -> dict[str, object]:
        """Create response for clear_key_values operation."""
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
        """List key-value pairs using KeyValueOperationService.

        Delegates to KeyValueOperationService which handles validation and
        appropriate storage strategy (bootstrap vs plugin mode).

        Args:
            namespace: Filter by namespace (None = all namespaces)
            scope: Filter by scope (None = all scopes)
            pattern: Key pattern to match (supports * wildcard)

        Returns:
            ActionResult with data.values containing list of runtime values

        Raises:
            FrameworkError: If key-value operation service is not initialized or operation fails
        """
        if self._key_value_operation_service is None:
            raise FrameworkError(
                message="KeyValueOperationService not initialized",
                error_code="state_service.key_value_service_not_initialized",
                details={"method": "list_key_values", "namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        return self._key_value_operation_service.list_key_values(namespace, scope, pattern)

    def _validate_list_key_values_parameters(
        self, namespace: str | None, scope: str | None, _pattern: str | None
    ) -> None:
        """Validate parameters for list_key_values operation.

        Args:
            namespace: Namespace to validate
            scope: Scope to validate
            _pattern: Reserved interface parameter, unused in this implementation
        """
        if namespace is not None and not namespace:
            raise FrameworkError(
                message="Namespace must be a non-empty string or None",
                error_code="state_service.invalid_runtime_namespace",
                details={"provided_namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        if scope is not None and scope not in ["GLOBAL", "SESSION", "FLOW"]:
            raise FrameworkError(
                message="Scope must be one of: GLOBAL, SESSION, FLOW, or None",
                error_code="state_service.invalid_runtime_scope",
                details={
                    "provided_scope": scope,
                    "valid_scopes": ["GLOBAL", "SESSION", "FLOW", None],
                },
                severity=ErrorSeverity.ERROR,
            )

    def _filter_bootstrap_runtime_values(
        self, namespace: str | None, scope: str | None, pattern: str | None
    ) -> list[object]:
        """Filter runtime values in bootstrap mode."""
        matching_values: list[object] = []

        for _runtime_key, runtime_data in self.runtime_values.items():
            if not isinstance(runtime_data, dict):
                continue
            match_namespace = namespace is None or runtime_data.get("namespace") == namespace
            match_scope = scope is None or runtime_data.get("scope") == scope
            match_pattern = pattern is None  # Simple pattern matching for bootstrap

            if match_namespace and match_scope and match_pattern:
                matching_values.append(runtime_data)

        return matching_values

    def _create_list_key_values_response(self, matching_values: list[object]) -> dict[str, object]:
        """Create response for list_key_values operation."""
        return {
            "action_status": "completed",
            "data": {"values": matching_values},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }
