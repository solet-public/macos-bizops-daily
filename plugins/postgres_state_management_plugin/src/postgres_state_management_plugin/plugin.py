"""
PostgreSQL State Plugin

PostgreSQL-based implementation of StateManagementInterface with connection pooling,
schema isolation, and full CRUD operations.
"""

import logging
import os
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, ClassVar, LiteralString, cast

import psycopg
from ananta.config.core_schemas import CoreSchemaDefinitions
from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.config.config_manager import get_config
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_base import ServicePlugin
from ananta.interfaces.edge_process_provider import EdgeProcessDefinition, EdgeProcessProvider
from ananta.interfaces.plugin_schema_service_interface import (
    PluginSchemaServiceInterface,
)
from ananta.interfaces.state_management_interface import (
    StateManagementInterface,
    StateTransaction,
)
from ananta.interfaces.vault_service_interface import VaultServiceInterface
from ananta.services.schema_management import SchemaRegistryService
from ananta.services.state_service.interfaces import StateManagementAPI, StateProvider
from ananta.services.state_service.ordered_query import parse_ordered_query
from ananta.services.state_service.read_bounds import (
    MAX_READ_ROWS,
    ReadBoundError,
    overflow_message,
    resolve_read_limit,
)
from ananta.types.schema_standardizer import SchemaStandardizer
from ananta.types.schema_types import SchemaDefinition, TableSchema

from postgres_state_management_plugin.postgres_backend.config import PostgresConfig
from postgres_state_management_plugin.postgres_backend.provider import (
    PostgresProvider,
    serialize_value_for_txn,
)

from .aggregate_ops import run_aggregate
from .config import get_plugin_config_schema
from .key_value_ops import kv_clear, kv_delete, kv_get, kv_list, kv_set
from .result_helpers import create_error_result, create_success_result
from .schema_creation import create_tables_from_schema
from .write_ops import write_multiple_records, write_single_record

logger = logging.getLogger(__name__)


# Additive vault account (RFC-2397 ``data:text/plain,<password>``) the new
# interface-only credential path reads. The legacy raw ``password`` account is
# left UNTOUCHED for old-code rollback + the offline _pg_credentials tools.
_VAULT_PASSWORD_CREDENTIAL = "db_password"


def _resolve_postgres_password_from_vault(
    vault_service: VaultServiceInterface | None, plugin_name: str
) -> str:
    """Read the Postgres password through the injected vault proxy.

    Operator mandate: interface-only credential access — the state plugin no
    longer reads the macOS Keychain directly. ``vault_service`` is the
    caller-bound ``VaultServiceProxy`` injected by
    ``_inject_state_vault_service`` (startup_sequence.py) BEFORE this
    foundational plugin's pool-open; its baked-in ``CallContext`` lets the
    own-namespace retrieve of ``<solet>.<plugin>.db_password`` pass vault
    ``enforce_namespace``. The value is an RFC-2397 ``data:text/plain,<P>``
    entry the vault substrate decodes back to the plaintext password. Fails
    loud, NO keyring fallback.
    """
    if vault_service is None:
        raise RuntimeError(
            f"{plugin_name}: vault_service proxy not injected before pool-open. "
            "_inject_state_vault_service must run before start_state_plugin "
            "(operator mandate: interface-only credential access).",
        )
    solet = os.environ.get("SOLET_NAME", "").strip()
    if not solet:
        raise RuntimeError(
            f"{plugin_name}: SOLET_NAME env var is required to resolve the "
            "per-solet vault key for the database password.",
        )
    password_key = f"{solet}.{plugin_name}.{_VAULT_PASSWORD_CREDENTIAL}"
    secret = vault_service.retrieve(key=password_key)
    if secret.get("action_status") != "completed":
        raise RuntimeError(
            f"{plugin_name}: failed to retrieve DB password from vault key "
            f"{password_key!r}: {secret.get('error')}. If the credential is "
            f"absent, the operator-run additive keychain seed has not yet "
            f"written it (account {_VAULT_PASSWORD_CREDENTIAL!r}).",
        )
    secret_data = secret.get("data")
    if not isinstance(secret_data, dict) or "value" not in secret_data:
        raise RuntimeError(
            f"{plugin_name}: vault retrieve for {password_key!r} returned no "
            "secret value in its result payload.",
        )
    return str(secret_data["value"])


def _strip_tz_from_params(
    params: Sequence[object] | None,
) -> Sequence[object] | None:
    """Convert any tz-aware ``datetime`` in ``params`` to naive UTC.

    Per the 2026-06-12 Tier 1.A audit-timestamp design's TZ-storage
    sub-finding (Option F1 seam strip): callers like
    ``SessionLedgerRepository._clock`` return ``datetime.now(UTC)`` —
    a timezone-aware datetime. psycopg2 serializes that as TIMESTAMPTZ,
    and storage into a ``timestamp without time zone`` column applies
    the server's session timezone, producing the 7-hour wall-clock
    skew observed in Phase 2 between ``__quarantine.restoration_at``
    (psycopg2 path, locally rendered) and ``__quarantine.updated_at``
    (Postgres trigger path, written as ``NOW() AT TIME ZONE 'UTC'``).
    Stripping the tz here lands the SAME wall-clock value across both
    write paths. Naive datetimes pass through unchanged; non-datetime
    values pass through unchanged. The seam mirrors the canonical
    ``_strip_nuls`` pattern documented at article
    ``19_session_ledger_02_nul_byte_sanitization_seam.md``.
    """
    if params is None:
        return None
    out: list[object] = []
    changed = False
    for value in params:
        if isinstance(value, datetime) and value.tzinfo is not None:
            out.append(value.astimezone(UTC).replace(tzinfo=None))
            changed = True
            continue
        out.append(value)
    return out if changed else params


class _PostgresStateTransaction(StateTransaction):
    """psycopg-backed implementation of :class:`StateTransaction`.

    Holds a non-autocommit connection for the lifetime of the
    surrounding context manager.  Each call opens a fresh cursor so
    callers can interleave reads and writes without worrying about
    cursor exhaustion.

    Every write/read path applies ``_strip_tz_from_params`` per the
    2026-06-12 TZ-storage seam invariant (Option F1) so the
    psycopg2-bound TIMESTAMPTZ → ``timestamp without time zone``
    timezone-conversion does not skew the stored wall-clock value.
    """

    def __init__(
        self, conn: psycopg.Connection[Any], provider: PostgresProvider
    ) -> None:
        self._conn = conn
        self._provider = provider

    def execute(
        self, sql: str, params: Sequence[object] | None = None,
    ) -> None:
        # psycopg types `query` as LiteralString to discourage SQL
        # injection at the type level.  The repository callers compose
        # SQL from in-tree string literals; cast at the trust boundary.
        query = cast(LiteralString, sql)
        clean_params = _strip_tz_from_params(params)
        with self._conn.cursor() as cur:
            cur.execute(query, tuple(clean_params) if clean_params is not None else None)

    def executemany(
        self, sql: str, params_seq: Sequence[Sequence[object]],
    ) -> None:
        query = cast(LiteralString, sql)
        with self._conn.cursor() as cur:
            cur.executemany(
                query,
                [tuple(_strip_tz_from_params(p) or ()) for p in params_seq],
            )

    def fetch_one(
        self, sql: str, params: Sequence[object] | None = None,
    ) -> dict[str, object] | None:
        query = cast(LiteralString, sql)
        clean_params = _strip_tz_from_params(params)
        with self._conn.cursor() as cur:
            cur.execute(query, tuple(clean_params) if clean_params is not None else None)
            row = cur.fetchone()
        if row is None:
            return None
        # Provider sets dict_row factory; psycopg returns mappings here.
        return dict(row)

    def fetch_all(
        self, sql: str, params: Sequence[object] | None = None,
    ) -> list[dict[str, object]]:
        query = cast(LiteralString, sql)
        clean_params = _strip_tz_from_params(params)
        with self._conn.cursor() as cur:
            cur.execute(query, tuple(clean_params) if clean_params is not None else None)
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    # Typed, non-SQL ops (RAISE-not-ActionResult). SQL composition is delegated
    # to the injected provider's pure builders so autocommit + txn share ONE
    # composition site; values flow through serialize_value_for_txn (F1 seam).

    def write_state(self, namespace: str, data: dict[str, object]) -> str:
        table = data.get("table")
        record = data.get("record")
        if not isinstance(table, str) or not isinstance(record, dict):
            raise ValueError(
                "write_state requires data={'table': str, 'record': dict}"
            )
        composed, params = self._provider.build_insert_sql(
            namespace, table, record, serialize=serialize_value_for_txn
        )
        with self._conn.cursor() as cur:
            cur.execute(composed, params)
            row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"write_state INSERT returned no id for {namespace}.{table}"
            )
        return cast(str, dict(row)["id"])

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
    ) -> int:
        table = query.get("table")
        filters = query.get("filters")
        if not isinstance(table, str) or not isinstance(filters, dict):
            raise ValueError(
                "update_state requires query={'table': str, 'filters': dict}"
            )
        composed, params = self._provider.build_update_sql(
            namespace, table, filters, updates, serialize=serialize_value_for_txn
        )
        with self._conn.cursor() as cur:
            cur.execute(composed, params)
            return cur.rowcount

    def query_state(
        self, namespace: str, filters: dict[str, object]
    ) -> list[dict[str, object]]:
        """Non-locking SELECT within the txn, bounded like ``read_state``.

        The autocommit ``read_state`` has carried the ``MAX_READ_ROWS`` bound
        since D1 (INCIDENT.md 2026-08-15) and this module already imports
        ``resolve_read_limit`` / ``overflow_message`` for it — but the bound was
        applied only there, leaving the transaction-scoped read unbounded. That
        gap mattered more than it looks: ``StateTransaction`` exposes no
        ``read_state``, so inside a transaction this is the ONLY row-returning
        read primitive there is, and its callers had no bounded alternative to
        migrate to.

        This is the same policy applied in a second place, NOT a second policy
        (``read_bounds`` module docstring). Semantics are byte-for-byte
        ``read_state``'s: an explicit ``limit`` is honoured as-is; over the cap
        without ``unbounded=True`` is refused before any SQL runs; and with no
        limit the query fetches ``cap + 1`` rows so that overflow can be PROVED
        and REFUSED rather than silently truncated to a prefix.
        """
        table = filters.get("table")
        inner = filters.get("filters")
        if not isinstance(table, str) or not isinstance(inner, dict):
            raise ValueError(
                "query_state requires filters={'table': str, 'filters': dict}"
            )
        fetch_limit, overflow_is_error = resolve_read_limit(
            filters.get("limit"),
            unbounded=filters.get("unbounded", False),
            table=table,
        )
        composed, params = self._provider.build_select_sql(
            namespace,
            table,
            inner,
            fetch_limit or None,
            serialize=serialize_value_for_txn,
        )
        with self._conn.cursor() as cur:
            cur.execute(composed, params)
            rows = cur.fetchall()
        # Raise rather than return a prefix: a truncated read corrupts every
        # analysis built on it, where a loud refusal stops one caller. Raising
        # also rolls the surrounding transaction back, which is the correct
        # outcome for a read-modify-write that cannot see all of its input.
        if overflow_is_error and len(rows) > MAX_READ_ROWS:
            raise ReadBoundError(overflow_message(table=table))
        return [dict(row) for row in rows]

    def increment_and_return(self, namespace: str, data: dict[str, object]) -> int:
        table = data.get("table")
        filters = data.get("filters")
        column = data.get("column")
        by = data.get("by", 1)
        if (
            not isinstance(table, str)
            or not isinstance(filters, dict)
            or not isinstance(column, str)
        ):
            raise ValueError(
                "increment_and_return requires data={'table': str, "
                "'filters': dict, 'column': str, 'by'?: int}"
            )
        if not isinstance(by, int) or isinstance(by, bool):
            raise ValueError("increment_and_return 'by' must be an int (not bool)")
        composed, params = self._provider.build_increment_returning(
            namespace, table, filters, column, by
        )
        with self._conn.cursor() as cur:
            cur.execute(composed, params)
            row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"increment_and_return matched 0 rows for {namespace}.{table}"
            )
        return cast(int, dict(row)[column])

    def delete_records(self, namespace: str, query: dict[str, object]) -> int:
        table = query.get("table")
        filters = query.get("filters")
        # Reject an empty/non-dict filter UP-FRONT (fail-fast). An empty filter
        # would compile to an empty WHERE — today that is a loud SQL syntax
        # error (rollback), but for a DELETE primitive an explicit guard is the
        # standing defense against a future ``build_delete_sql`` change that
        # tolerates an empty WHERE and silently deletes the whole table.
        if not isinstance(table, str) or not isinstance(filters, dict) or not filters:
            raise ValueError(
                "delete_records requires query={'table': str, 'filters': "
                "<non-empty dict>, 'soft_delete'?: bool}; an empty filter is "
                "rejected to prevent a delete-all"
            )
        soft_delete = query.get("soft_delete", True)
        composed, params = self._provider.build_delete_sql(
            namespace, table, filters,
            soft_delete=bool(soft_delete), serialize=serialize_value_for_txn,
        )
        with self._conn.cursor() as cur:
            cur.execute(composed, params)
            return cur.rowcount

    def _txn_aggregate(
        self, namespace: str, data: dict[str, object], op: str, column: str | None
    ) -> object:
        """Run a single-scalar aggregate on the open txn connection.

        Shared by ``count``/``max_value``/``min_value`` so SQL composition stays
        at the provider's ONE site; values flow through ``serialize_value_for_txn``
        (F1 seam). Returns the scalar VERBATIM (a ``TIMESTAMP`` ``MAX`` stays a
        NAIVE datetime). Raises on a missing row (rolls the txn back).
        """
        table = data.get("table")
        filters = data.get("filters", {})
        if not isinstance(table, str) or not isinstance(filters, dict):
            raise ValueError(
                f"{op} requires data={{'table': str, 'filters'?: dict}}"
            )
        composed, params = self._provider.build_aggregate_query(
            namespace, table, op, column, filters, serialize=serialize_value_for_txn
        )
        with self._conn.cursor() as cur:
            cur.execute(composed, params)
            row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"{op} aggregate returned no row for {namespace}.{table}"
            )
        return dict(row)["value"]

    def count(self, namespace: str, data: dict[str, object]) -> int:
        return cast(int, self._txn_aggregate(namespace, data, "count", None))

    def max_value(self, namespace: str, data: dict[str, object]) -> object:
        column = data.get("column")
        if not isinstance(column, str):
            raise ValueError(
                "max_value requires data={'table': str, 'column': str, "
                "'filters'?: dict}"
            )
        return self._txn_aggregate(namespace, data, "max", column)

    def min_value(self, namespace: str, data: dict[str, object]) -> object:
        column = data.get("column")
        if not isinstance(column, str):
            raise ValueError(
                "min_value requires data={'table': str, 'column': str, "
                "'filters'?: dict}"
            )
        return self._txn_aggregate(namespace, data, "min", column)


class PostgresStatePlugin(
    ServicePlugin,
    StateManagementInterface,
    StateManagementAPI,
    StateProvider,
    PluginSchemaServiceInterface,
    EdgeProcessProvider,
):
    """PostgreSQL-based state management plugin.

    Implements:
    - StateManagementInterface (legacy - will be removed)
    - StateManagementAPI (public, decorated read methods)
    - StateProvider (internal, non-decorated write/lifecycle methods)

    Features:
    - Connection pooling for high concurrency
    - Schema isolation (all tables in 'state' schema)
    - Auto-ID generation with table prefixes
    - Auto-timestamps on create and update
    - Parameterized queries for security
    - Transaction management
    """

    service_interfaces: ClassVar[tuple[type, ...]] = (
        StateManagementInterface,
        PluginSchemaServiceInterface,
    )
    supported_interface_versions: ClassVar[dict[type, str]] = {
        StateManagementInterface: StateManagementInterface.INTERFACE_VERSION,
        PluginSchemaServiceInterface: PluginSchemaServiceInterface.INTERFACE_VERSION,
    }

    def __init__(self) -> None:
        """Initialize PostgreSQL state plugin."""
        super().__init__()
        self.name = "postgres_state_management_plugin"
        self._provider: PostgresProvider | None = None
        self._lifecycle: object | None = None
        # Caller-bound VaultServiceProxy, injected by
        # ``_inject_state_vault_service`` (startup_sequence.py) before pool-open.
        self._vault_service: VaultServiceInterface | None = None

    def get_readiness_error(self) -> str | None:
        return self.readiness_error

    def set_vault_service(self, vault_service: VaultServiceInterface) -> None:
        """Receive the caller-bound VaultServiceProxy (injected pre-readiness)."""
        self._vault_service = vault_service

    def prepare_for_readiness(self) -> None:
        """Initialize PostgreSQL provider before plugin becomes ready.

        This method is called during the plugin readiness phase to ensure
        the database connection is established before any operations.
        """
        logger.debug("Preparing postgres_state_management_plugin for readiness")

        # Load config from ConfigManager
        config = get_config().get_plugin_config(self.name, default_config={})
        logger.debug("Loaded postgres config with keys: %s", list(config.keys()))

        # Initialize the plugin (creates provider and connection pool)
        self._initialize(config)
        logger.debug("PostgreSQL provider initialized and ready")

    def _initialize(self, config: dict[str, object]) -> None:
        """
        Initialize plugin with configuration (private method).

        Args:
            config: Plugin configuration dictionary

        Raises:
            ValueError: If configuration is invalid
            RuntimeError: If PostgreSQL connection fails
        """
        # Check readiness state instead of internal flag
        if self.is_ready():
            return

        try:
            # Prepare PostgreSQL-specific configuration with defaults
            # Extract PostgreSQL-specific fields from generic config
            logger.debug(
                "Initializing postgres_state_management_plugin with config keys: %s",
                list(config.keys()),
            )

            # Extract and cast config values with proper type safety
            port_val = config.get("port", 5432)
            pool_size_val = config.get("pool_size", config.get("max_connections", 20))
            timeout_val = config.get("connection_timeout", 30)

            postgres_config_dict: dict[str, Any] = {
                "host": str(config.get("host", "localhost")),
                "port": int(port_val) if isinstance(port_val, int | str) else 5432,
                "user": str(config.get("user", "ananta_user")),
                "password": _resolve_postgres_password_from_vault(
                    self._vault_service, self.name
                ),
                "pool_size": int(pool_size_val) if isinstance(pool_size_val, int | str) else 20,
                "connection_timeout": int(timeout_val)
                if isinstance(timeout_val, int | str)
                else 30,
            }
            # Only include pg_schema if explicitly provided - let Pydantic default to SOLET_NAME
            if "pg_schema" in config:
                postgres_config_dict["pg_schema"] = str(config["pg_schema"])
            elif "schema" in config:
                postgres_config_dict["pg_schema"] = str(config["schema"])

            # database: identity-defaults to SOLET_NAME like pg_schema (old 'ananta_db' fallback retired)
            if "database" in config:
                postgres_config_dict["database"] = str(config["database"])

            logger.debug(
                "Creating PostgresConfig with: host=%s, port=%s, database=%s",
                postgres_config_dict["host"],
                postgres_config_dict["port"],
                postgres_config_dict.get("database", "<default: SOLET_NAME>"),
            )

            # Parse and validate configuration
            postgres_config = PostgresConfig(**postgres_config_dict)

            # Create provider
            logger.debug("Creating PostgresProvider...")
            self._provider = PostgresProvider(postgres_config)

            # Initialize connection pool and schema
            logger.debug("Initializing PostgresProvider (creating pool and schema)...")
            self._provider.initialize()

            # Mark plugin as ready (single source of truth)
            self.set_ready()
            logger.debug("%s initialized successfully and marked as READY", self.name)

        except Exception as e:
            error_msg = f"Failed to initialize {self.name}: {e}"
            logger.exception(error_msg)
            self.set_error(error_msg)
            raise RuntimeError(error_msg) from e

    async def start_services(self) -> ActionResult:
        """Start PostgreSQL services.

        Since initialization happens in prepare_for_readiness(), this just
        marks services as started, bootstraps the plugin-schema ownership
        table, and returns success.

        Returns:
            ActionResult with status
        """
        if self._services_started:
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"message": "Services already started"},
                "actions": [],
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }

        # Provider owns the lifecycle substrate end-to-end: ownership table
        # bootstrap + id_prefix cache hydrate from existing ownership rows.
        # Plugin no longer reaches into the DB to set this up.
        self._get_provider().bootstrap_for_lifecycle()

        self._services_started = True
        logger.debug(f"{self.name} services started")

        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {"message": "PostgreSQL services started"},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    async def stop_services(self) -> ActionResult:
        """Stop PostgreSQL services and close connection pool.

        Returns:
            ActionResult with status
        """
        if not self._services_started:
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"message": "Services already stopped"},
                "actions": [],
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }

        try:
            if self._provider:
                self._provider.close()
                logger.debug(f"{self.name} connection pool closed")

            self._services_started = False
            logger.debug(f"{self.name} services stopped")

            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"message": "PostgreSQL services stopped"},
                "actions": [],
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        except Exception as e:
            error_msg = f"Failed to stop services: {e}"
            logger.exception(error_msg)
            return {
                "action_status": ActionStatus.ERROR.value,
                "data": {},
                "actions": [],
                "error": {
                    "type": "plugin_error",
                    "code": "plugin.stop_failed",
                    "message": error_msg,
                    "details": {},
                    "severity": "error",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                "timestamp": datetime.now(UTC).isoformat(),
            }

    # --- PluginSchemaServiceInterface implementation ---------------------
    # Thin delegates to PluginSchemaLifecycle. Lazy-init the lifecycle so it
    # references a fully-initialized provider.

    def _get_lifecycle(self) -> Any:
        if self._lifecycle is None:
            from postgres_state_management_plugin.postgres_backend.lifecycle import (
                PluginSchemaLifecycle,
            )

            self._lifecycle = PluginSchemaLifecycle(self._get_provider())
        return self._lifecycle

    def install_plugin_schema(
        self, plugin_namespace: str, declared_schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        return self._get_lifecycle().install_plugin_schema(
            plugin_namespace, declared_schema_json
        )

    def update_plugin_schema(
        self, plugin_namespace: str, declared_schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        return self._get_lifecycle().update_plugin_schema(
            plugin_namespace, declared_schema_json
        )

    def uninstall_plugin_schema(self, plugin_namespace: str) -> dict[str, Any]:
        return self._get_lifecycle().uninstall_plugin_schema(plugin_namespace)

    def purge_plugin_schema(
        self, plugin_namespace: str, force: bool = False
    ) -> dict[str, Any]:
        return self._get_lifecycle().purge_plugin_schema(plugin_namespace, force=force)

    def get_installed_schema(self, plugin_namespace: str) -> dict[str, Any]:
        return self._get_lifecycle().get_installed_schema(plugin_namespace)

    # --- end PluginSchemaServiceInterface implementation -----------------

    def _get_provider(self) -> PostgresProvider:
        """
        Get PostgreSQL provider instance (internal helper).

        Lazy-initializes the provider if not already initialized.

        Returns:
            PostgresProvider instance

        Raises:
            RuntimeError: If provider initialization fails
        """
        if not self._provider:
            # Lazy initialization - load config and initialize
            logger.debug("Lazy-initializing %s provider from _get_provider()", self.name)
            try:
                config = get_config().get_plugin_config(self.name, default_config={})
                logger.debug("Loaded config with keys: %s", list(config.keys()))
                self._initialize(config)
                logger.debug("Successfully lazy-initialized %s provider", self.name)
            except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
                error_msg = f"Failed to lazy-initialize {self.name} provider: {e}"
                logger.error(error_msg)
                raise RuntimeError(error_msg) from e

        if self._provider is None:
            raise RuntimeError("Provider should be initialized at this point")
        return self._provider

    # StateManagementInterface implementation

    def create_schema(self, namespace: str, schema: dict[str, object]) -> ActionResult:
        """Create database schema for namespace."""
        try:
            tables = schema.get("tables", {})
            if not isinstance(tables, dict):
                return create_error_result(
                    "Invalid schema: 'tables' must be a dictionary",
                    error_code="schema.invalid_format",
                )
            provider = self._get_provider()
            created_tables = create_tables_from_schema(provider, namespace, tables)
            return create_success_result(
                {
                    "namespace": namespace,
                    "tables_created": created_tables,
                    "count": len(created_tables),
                }
            )
        except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
            logger.exception("Failed to create schema")
            return create_error_result(
                str(e), error_code="schema.creation_failed", details={"namespace": namespace}
            )

    def read_state(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """
        Read state data from namespace.

        Args:
            namespace: Namespace identifier
            query: Query parameters with table and filters

        Returns:
            ActionResult with query results
        """
        try:
            provider = self._get_provider()

            table = query.get("table")
            if not isinstance(table, str):
                return create_error_result(
                    "Missing or invalid 'table' in query",
                    error_code="query.invalid_table",
                )

            filters = query.get("filters", {})

            # D1 (INCIDENT.md 2026-08-15): an unbounded read of a 109k-row
            # table returned every row, became an 82 MB action payload, and
            # froze the platform for 3h20m. Resolve the row bound BEFORE the
            # query runs — the cap is compiled into the SQL, so an over-large
            # result never leaves Postgres. Mirrors ordered_query.py's Gap-C
            # policy rather than introducing a second one.
            try:
                fetch_limit, overflow_is_error = resolve_read_limit(
                    query.get("limit"),
                    unbounded=query.get("unbounded", False),
                    table=table,
                )
            except ReadBoundError as exc:
                return create_error_result(
                    str(exc),
                    error_code="query.limit_over_cap",
                    details={"namespace": namespace, "table": table},
                )

            # Execute query
            rows = provider.select(
                namespace=namespace,
                table=table,
                conditions=filters if isinstance(filters, dict) else None,
                limit=fetch_limit or None,
            )

            # The no-explicit-limit path fetched cap+1 rows purely to detect
            # overflow. Getting that many back PROVES more rows exist without
            # ever materialising them all, so refuse — never return the prefix.
            if overflow_is_error and len(rows) > MAX_READ_ROWS:
                return create_error_result(
                    overflow_message(table=table),
                    error_code="query.unbounded_read_over_cap",
                    details={
                        "namespace": namespace,
                        "table": table,
                        "cap_rows": MAX_READ_ROWS,
                    },
                )

            # Return flat structure matching return_value_schema
            # Schema expects: records, count, namespace, table at top level
            return create_success_result(
                {
                    "records": rows,
                    "count": len(rows),
                    "namespace": namespace,
                    "table": table,
                }
            )

        except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
            logger.exception("Failed to read state")
            return create_error_result(
                str(e),
                error_code="state.read_failed",
                details={"namespace": namespace},
            )

    def write_state(
        self,
        namespace: str,
        data: dict[str, object],
        calling_service: str | None = None,  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
        calling_namespace: str | None = None,  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> ActionResult:
        """
        Write state data to namespace.

        Args:
            namespace: Namespace identifier
            data: Data to write with table and record
            calling_service: Interface contract; not yet consumed by this
                implementation. Reserved for future auditing.
            calling_namespace: Interface contract; not yet consumed by this
                implementation. Reserved for future auditing.

        Returns:
            ActionResult with write status
        """
        try:
            provider = self._get_provider()

            table = data.get("table")
            if not isinstance(table, str):
                return create_error_result(
                    "Missing or invalid 'table' in data",
                    error_code="write.invalid_table",
                )

            # Support BOTH single record and batch records (SQLite compatibility)
            if "record" in data:
                return write_single_record(provider, namespace, table, data.get("record"))

            if "records" in data:
                return write_multiple_records(provider, namespace, table, data.get("records"))

            return create_error_result(
                "Data must contain either 'record' or 'records'",
                error_code="write.missing_data",
            )

        except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
            logger.exception("Failed to write state")
            return create_error_result(
                str(e),
                error_code="state.write_failed",
                details={"namespace": namespace},
            )

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> ActionResult:
        """
        Update state data in namespace.

        Args:
            namespace: Namespace identifier
            query: Query to identify records (table and filters)
            updates: Updates to apply

        Returns:
            ActionResult with update status
        """
        try:
            provider = self._get_provider()

            table = query.get("table")
            if not isinstance(table, str):
                return create_error_result(
                    "Missing or invalid 'table' in query",
                    error_code="update.invalid_table",
                )

            filters = query.get("filters", {})
            if not isinstance(filters, dict):
                return create_error_result(
                    "'filters' must be a dictionary",
                    error_code="update.invalid_filters",
                )

            # Update records
            updated_count = provider.update(
                namespace=namespace,
                table=table,
                conditions=filters,
                updates=updates,
            )

            return create_success_result(
                {
                    "namespace": namespace,
                    "result": {
                        "updated": updated_count,
                    },
                }
            )

        except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
            logger.exception("Failed to update state")
            return create_error_result(
                str(e),
                error_code="state.update_failed",
                details={"namespace": namespace},
            )

    def acquire_lease(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Atomically acquire an expiry-fenced lease on a single row.

        See :meth:`StateManagementInterface.acquire_lease` for the contract.
        """
        try:
            provider = self._get_provider()

            table = data.get("table")
            if not isinstance(table, str):
                return create_error_result(
                    "Missing or invalid 'table' in data",
                    error_code="acquire_lease.invalid_table",
                )

            filters = data.get("filters")
            if not isinstance(filters, dict):
                return create_error_result(
                    "Missing or invalid 'filters' in data",
                    error_code="acquire_lease.invalid_filters",
                )

            lease_column = data.get("lease_column")
            if not isinstance(lease_column, str):
                return create_error_result(
                    "Missing or invalid 'lease_column' in data",
                    error_code="acquire_lease.invalid_lease_column",
                )

            now = data.get("now")
            if not isinstance(now, datetime):
                return create_error_result(
                    "Missing or invalid 'now' in data - must be a datetime",
                    error_code="acquire_lease.invalid_now",
                )

            set_values = data.get("set")
            if not isinstance(set_values, dict) or not set_values:
                return create_error_result(
                    "Missing or invalid 'set' in data - must be a non-empty dict",
                    error_code="acquire_lease.invalid_set",
                )

            acquired = provider.acquire_lease(
                namespace=namespace,
                table=table,
                filters=filters,
                lease_column=lease_column,
                now=now,
                set_values=set_values,
            )

            return create_success_result(
                {
                    "namespace": namespace,
                    "result": {"acquired": acquired},
                }
            )

        except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
            logger.exception("Failed to acquire lease")
            return create_error_result(
                str(e),
                error_code="state.acquire_lease_failed",
                details={"namespace": namespace},
            )

    def upsert_state(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Insert or update a record based on conflict columns.

        If a record with matching conflict columns exists, updates it.
        Otherwise, inserts a new record.

        Args:
            namespace: Namespace identifier
            data: Must contain:
                - table: Target table name
                - record: Record data to insert/update
                - conflict_columns: List of columns to check for conflicts (e.g., ["id"])

        Returns:
            ActionResult with the record ID (generated or existing)
        """
        try:
            provider = self._get_provider()

            table = data.get("table")
            if not isinstance(table, str):
                return create_error_result(
                    "Missing or invalid 'table' in data",
                    error_code="upsert.invalid_table",
                )

            record = data.get("record")
            if not isinstance(record, dict):
                return create_error_result(
                    "Missing or invalid 'record' in data",
                    error_code="upsert.invalid_record",
                )

            conflict_columns = data.get("conflict_columns")
            if not isinstance(conflict_columns, list) or not conflict_columns:
                return create_error_result(
                    "Missing or invalid 'conflict_columns' in data - must be a non-empty list",
                    error_code="upsert.invalid_conflict_columns",
                )

            on_conflict = data.get("on_conflict")
            conflict_predicate = data.get("conflict_predicate")
            if on_conflict is not None or conflict_predicate is not None:
                return self._upsert_do_nothing(
                    provider,
                    namespace,
                    table,
                    record,
                    conflict_columns,
                    on_conflict,
                    conflict_predicate,
                )

            record_id = provider.upsert(
                namespace=namespace,
                table=table,
                data=record,
                conflict_columns=conflict_columns,
            )

            return create_success_result(
                {
                    "namespace": namespace,
                    "result": {
                        "generated_id": record_id,
                        "upserted": 1,
                    },
                }
            )

        except psycopg.Error as e:
            logger.exception(f"Database error during upsert: {e}")
            return create_error_result(
                f"Database error: {e}",
                error_code="state.database_error",
                details={"namespace": namespace, "error_type": type(e).__name__},
            )
        except (OSError, RuntimeError, ValueError) as e:
            logger.exception("Failed to upsert state")
            return create_error_result(
                str(e),
                error_code="state.upsert_failed",
                details={"namespace": namespace},
            )

    def _upsert_do_nothing(
        self,
        provider: PostgresProvider,
        namespace: str,
        table: str,
        record: dict[str, Any],
        conflict_columns: list[str],
        on_conflict: object,
        conflict_predicate: object,
    ) -> ActionResult:
        """``upsert_state``'s DO-NOTHING path (partial ``ON CONFLICT`` predicate).

        Returns ``{"inserted": bool, "id": str | None}`` — distinct from the
        default DO-UPDATE path's ``{"generated_id", "upserted"}`` shape. The
        structured ``conflict_predicate`` AST keeps SQL text out of the caller;
        the provider compiles it.
        """
        if on_conflict != "do_nothing":
            return create_error_result(
                "on_conflict must be 'do_nothing' when provided",
                error_code="upsert.invalid_on_conflict",
            )
        if conflict_predicate is not None and not isinstance(conflict_predicate, list):
            return create_error_result(
                "conflict_predicate must be a list of {column, op, value?} entries",
                error_code="upsert.invalid_conflict_predicate",
            )
        inserted, record_id = provider.upsert_conditional(
            namespace=namespace,
            table=table,
            data=record,
            conflict_columns=conflict_columns,
            conflict_predicate=conflict_predicate,
        )
        return create_success_result(
            {
                "namespace": namespace,
                "result": {"inserted": inserted, "id": record_id},
            }
        )

    def delete_records(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """
        Delete state data from namespace.

        Args:
            namespace: Namespace identifier
            query: Query to identify records (table and filters)

        Returns:
            ActionResult with deletion status
        """
        try:
            provider = self._get_provider()

            table = query.get("table")
            if not isinstance(table, str):
                return create_error_result(
                    "Missing or invalid 'table' in query",
                    error_code="delete.invalid_table",
                )

            # Reject an empty/non-dict filter UP-FRONT. An empty filter would
            # compile to an empty WHERE (today a loud SQL syntax error); for a
            # DELETE primitive an explicit fail-fast is the standing defense
            # against a future builder that tolerates an empty WHERE and
            # silently deletes the whole table. Mirrors the typed-txn
            # delete_records guard.
            filters = query.get("filters", {})
            if not isinstance(filters, dict) or not filters:
                return create_error_result(
                    "'filters' must be a non-empty dictionary (an empty filter "
                    "is rejected to prevent a delete-all)",
                    error_code="delete.invalid_filters",
                )

            # Use soft delete by default
            soft_delete = query.get("soft_delete", True)

            # Delete records
            deleted_count = provider.delete(
                namespace=namespace,
                table=table,
                conditions=filters,
                soft_delete=bool(soft_delete),
            )

            return create_success_result(
                {
                    "namespace": namespace,
                    "result": {
                        "deleted": deleted_count,
                        "soft_delete": bool(soft_delete),
                    },
                }
            )

        except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
            logger.exception("Failed to delete state")
            return create_error_result(
                str(e),
                error_code="state.delete_failed",
                details={"namespace": namespace},
            )

    def query_state(self, namespace: str, filters: dict[str, object]) -> ActionResult:
        """DEPRECATED alias for :meth:`read_state` — prefer ``read_state``.

        A second NAME for one primitive, not a second primitive. ``filters`` is
        forwarded verbatim and becomes ``read_state``'s ``query``, so it is the
        whole ``{table, filters, limit?, unbounded?}`` envelope: an explicit
        ``limit`` compiles into the SQL and the ``MAX_READ_ROWS`` bound applies
        here exactly as it does to ``read_state``, because it IS ``read_state``.

        Args:
            namespace: Namespace identifier
            filters: The ``{table, filters, limit?, unbounded?}`` query envelope.

        Returns:
            ActionResult with query results
        """
        return self.read_state(namespace, filters)

    def query_ordered(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Ordered, bounded, tie-safe query (the sanctioned widening).

        Validates + hardens the request via ``parse_ordered_query`` (a
        malformed contract fails fast, and tz-aware cursor timestamps are
        normalized to naive UTC there per the TZ-storage seam), then
        composes the SQL inside ``provider.select_ordered`` — the one
        operator-approved site where SQL is built.

        Args:
            namespace: Namespace identifier
            data: ``{table, filters, order_by, limit, after?, include_deleted?}``

        Returns:
            ActionResult with the ordered page in ``data.records``
        """
        spec = parse_ordered_query(data)
        provider = self._get_provider()

        rows = provider.select_ordered(
            namespace=namespace,
            table=spec.table,
            conditions=spec.filters,
            order_columns=spec.order_columns,
            direction=spec.direction,
            limit=spec.limit,
            after=spec.after,
            include_deleted=spec.include_deleted,
        )

        return create_success_result(
            {
                "records": rows,
                "count": len(rows),
                "namespace": namespace,
                "table": spec.table,
            }
        )

    def count(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Count rows matching a filtered set (single scalar; no rows shipped).

        See :meth:`StateManagementInterface.count` for the contract.
        """
        return run_aggregate(
            self._get_provider(), namespace, data,
            op="count", requires_column=False, error_ns="count",
        )

    def max_value(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Largest value of a column over a filtered set (single scalar).

        See :meth:`StateManagementInterface.max_value` for the contract.
        """
        return run_aggregate(
            self._get_provider(), namespace, data,
            op="max", requires_column=True, error_ns="max_value",
        )

    def min_value(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Smallest value of a column over a filtered set (single scalar).

        See :meth:`StateManagementInterface.min_value` for the contract.
        """
        return run_aggregate(
            self._get_provider(), namespace, data,
            op="min", requires_column=True, error_ns="min_value",
        )

    def execute_sql(
        self,
        sql_query: str,
        sql_params: list[object] | None = None,
        calling_service: str = "StateService",  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
        calling_namespace: str = "ananta.services.state_service",  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> ActionResult:
        """
        Execute direct SQL query.

        CRITICAL: Required by ActionQueuePoller for queue management.

        Args:
            sql_query: SQL query string to execute
            sql_params: Optional parameters for parameterized queries
            calling_service: Interface contract; not yet consumed by this
                implementation. Reserved for future auditing.
            calling_namespace: Interface contract; not yet consumed by this
                implementation. Reserved for future auditing.

        Returns:
            ActionResult with query results in data.records
        """
        try:
            provider = self._get_provider()

            # Strip tz from any datetime params per the 2026-06-12 TZ-storage
            # seam invariant (Option F1) so wall-clock values land identically
            # whether the write goes through the Postgres trigger path
            # (NOW() AT TIME ZONE 'UTC') or the psycopg2 binding path
            # (TIMESTAMPTZ → timestamp without time zone, server-tz applied).
            if sql_params:
                clean_params = _strip_tz_from_params(sql_params)
                rows = provider.execute_query(sql_query, tuple(clean_params or ()))
            else:
                rows = provider.execute_query(sql_query)

            return create_success_result(
                {
                    "records": rows,
                    "count": len(rows),
                }
            )

        except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
            logger.exception("Failed to execute SQL query")
            return create_error_result(
                str(e),
                error_code="sql.execution_failed",
                details={"query": sql_query[:100]},  # Truncate for safety
            )

    @contextmanager
    def transactional(self) -> Generator[StateTransaction]:
        """Yield a :class:`StateTransaction` backed by a non-autocommit conn.

        Delegates to :meth:`PostgresProvider.get_transactional_connection`,
        which commits on clean exit and rolls back on exception.
        """
        provider = self._get_provider()
        with provider.get_transactional_connection() as conn:
            yield _PostgresStateTransaction(conn, provider)

    def describe_schema(self, namespace: str) -> ActionResult:
        """
        Get schema definition for namespace.

        Returns table and column information for all tables in the namespace.

        Args:
            namespace: Namespace to describe

        Returns:
            ActionResult with schema information
        """
        try:
            provider = self._get_provider()

            # Query PostgreSQL information_schema for tables in this namespace
            tables_query = """
                SELECT
                    table_name,
                    column_name,
                    data_type,
                    is_nullable,
                    column_default
                FROM information_schema.columns
                WHERE table_schema = %s
                    AND table_name LIKE %s
                ORDER BY table_name, ordinal_position
            """

            # Execute query directly to get dict rows (execute_query returns lists)
            with provider.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(tables_query, (provider.config.pg_schema, f"{namespace}__%"))
                rows = cursor.fetchall()

            # Group by table
            schema_def: dict[str, dict[str, object]] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                table_name = str(row["table_name"])
                if table_name not in schema_def:
                    schema_def[table_name] = {"columns": {}}

                columns = schema_def[table_name]["columns"]
                if isinstance(columns, dict):
                    columns[str(row["column_name"])] = {
                        "type": row["data_type"],
                        "nullable": row["is_nullable"] == "YES",
                        "default": row["column_default"],
                    }

            return create_success_result(
                {
                    "namespace": namespace,
                    "tables": schema_def,
                }
            )

        except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
            logger.exception("Failed to describe schema for %s", namespace)
            return create_error_result(
                str(e),
                error_code="schema.describe_failed",
                details={"namespace": namespace},
            )

    def list_namespaces(self) -> ActionResult:
        """
        List all available namespaces.

        Namespaces are derived from table name prefixes (e.g., "core__" -> "core").

        Returns:
            ActionResult with list of namespaces
        """
        try:
            provider = self._get_provider()

            # Query all tables in the state schema
            query = """
                SELECT DISTINCT
                    SUBSTRING(table_name FROM '^([^_]+)__') as namespace
                FROM information_schema.tables
                WHERE table_schema = %s
                    AND table_name LIKE '%%__%%'
                ORDER BY namespace
            """

            # Execute query directly to get dict rows (execute_query returns lists)
            with provider.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, (provider.config.pg_schema,))
                rows = cursor.fetchall()

            namespaces = [
                str(row["namespace"])
                for row in rows
                if isinstance(row, dict) and row.get("namespace")
            ]

            return create_success_result(
                {
                    "namespaces": namespaces,
                    "count": len(namespaces),
                }
            )

        except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
            logger.exception("Failed to list namespaces")
            return create_error_result(
                str(e),
                error_code="namespace.list_failed",
            )

    def mark_as_read(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """
        Mark records as read/processed.

        Updates records matching query to set read_at timestamp.

        Args:
            namespace: Namespace identifier
            query: Query to identify records to mark

        Returns:
            ActionResult indicating success/failure
        """
        try:
            # Use update_state to set read_at
            return self.update_state(
                namespace=namespace,
                query=query,
                updates={"read_at": datetime.now(UTC).isoformat()},
            )

        except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
            logger.exception("Failed to mark records as read")
            return create_error_result(
                str(e),
                error_code="state.mark_read_failed",
                details={"namespace": namespace},
            )

    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: object,
        scope: str = "GLOBAL",
        ttl: int | None = None,  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> ActionResult:
        return kv_set(self._get_provider(), namespace, key, value, scope)

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        return kv_get(self._get_provider(), namespace, key, scope)

    def delete_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        return kv_delete(self._get_provider(), namespace, key, scope)

    def clear_key_values(
        self, namespace: str | None = None, scope: str | None = None
    ) -> ActionResult:
        return kv_clear(self._get_provider(), namespace, scope)

    def list_key_values(
        self,
        namespace: str | None = None,
        scope: str | None = None,
        pattern: str | None = None,  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> ActionResult:
        return kv_list(self._get_provider(), namespace, scope)

    def _ensure_plugin_ready(self, config: dict[str, object] | None) -> dict[str, object]:
        """Ensure plugin is initialized and ready."""
        if not self.is_ready():
            # Load config from config manager if not provided
            if config is None:
                logger.debug("Loading postgres_state_management_plugin config from ConfigManager")
                config = get_config().get_plugin_config(self.name, default_config={})
                logger.debug("Loaded config with keys: %s", list(config.keys()))

            logger.debug(
                "Lazy-initializing postgres_state_management_plugin from initialize_database"
            )
            try:
                self._initialize(config)
                logger.debug("Successfully lazy-initialized postgres_state_management_plugin")
            except (psycopg.Error, OSError, RuntimeError, ValueError):
                logger.exception("Failed to lazy-initialize in initialize_database")
                raise

        return config or {}

    def _create_database_schemas(
        self,
    ) -> tuple[list[str], list[str], list[tuple[str, dict[str, TableSchema], SchemaDefinition]]]:
        """Create all core database schemas (Pass 1)."""
        all_core_schemas = CoreSchemaDefinitions.get_all_core_schemas()
        logger.debug("Found %s core schemas to create", len(all_core_schemas))

        created_schemas = []
        failed_schemas = []
        schema_definitions_for_persistence = []

        logger.debug("PASS 1: Creating all database tables...")
        for schema_def in all_core_schemas:
            try:
                # CRITICAL FIX: Standardize schema to add all 9 standard fields
                standardizer = SchemaStandardizer()
                standardized_def = standardizer.standardize_schema(schema_def)

                # SchemaDefinition is a dataclass, access attributes directly
                namespace = standardized_def.namespace
                tables = standardized_def.tables

                logger.debug(
                    "Creating schema for namespace '%s' with %s tables...",
                    namespace,
                    len(tables),
                )

                # Convert SchemaDefinition to dict format for create_schema
                schema_dict: dict[str, object] = {
                    "namespace": namespace,
                    "tables": {
                        table_name: {
                            "id_prefix": table_schema.id_prefix,
                            "columns": {
                                col_name: {
                                    "type": col.type,
                                    "primary_key": col.primary_key,
                                    "not_null": col.not_null,
                                    "default": col.default,
                                    "unique": col.unique,
                                    "check": col.check,
                                }
                                for col_name, col in table_schema.columns.items()
                            },
                        }
                        for table_name, table_schema in tables.items()
                    },
                }

                # Create the schema using the plugin's create_schema method
                result = self.create_schema(namespace, schema_dict)

                if result.get("action_status") == ActionStatus.COMPLETED:
                    created_schemas.append(namespace)
                    logger.debug("Successfully created schema: %s", namespace)
                    schema_definitions_for_persistence.append((namespace, tables, standardized_def))
                else:
                    error_info = result.get("error", "Unknown error")
                    failed_schemas.append(f"{namespace}: {error_info}")
                    logger.error("Failed to create schema %s: %s", namespace, error_info)

            except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
                schema_name = (
                    schema_def.namespace if hasattr(schema_def, "namespace") else "unknown"
                )
                failed_schemas.append(f"{schema_name}: {e!s}")
                logger.exception("Exception creating schema %s", schema_name)

        return created_schemas, failed_schemas, schema_definitions_for_persistence

    def _persist_schema_metadata(
        self, schema_definitions: list[tuple[str, dict[str, TableSchema], SchemaDefinition]]
    ) -> tuple[int, int]:
        """Persist schema metadata to registry (Pass 2)."""
        logger.debug("PASS 2: Persisting schema metadata for %s schemas...", len(schema_definitions))
        schema_registry_service = SchemaRegistryService(self)

        metadata_persisted_count = 0

        for namespace, tables, schema_def in schema_definitions:
            for table_name in tables:
                schema_registry_service.persist_schema(namespace, table_name, schema_def)
                logger.debug("Persisted schema metadata for %s__%s", namespace, table_name)
                metadata_persisted_count += 1

        logger.debug(
            "Schema metadata persistence complete: %s succeeded",
            metadata_persisted_count,
        )

        return metadata_persisted_count, 0

    def initialize_database(self, config: dict[str, object] | None = None) -> ActionResult:
        """
        Initialize the database (Phase 3 Database Operations).

        This is called during system startup to ensure the database is ready.

        Args:
            config: Optional plugin configuration for lazy initialization

        Returns:
            ActionResult with initialization status
        """
        try:
            # Initialize plugin if not already initialized (lazy initialization)
            self._ensure_plugin_ready(config)

            # If still no provider after initialization attempt, it's a fatal error
            if not self._provider:
                error_msg = (
                    "Database initialization failed: PostgreSQL provider could not be initialized"
                )
                logger.error(error_msg)
                return create_error_result(
                    error_msg,
                    error_code="database.init_failed",
                    details={"exception": error_msg},
                )

            # Provider is initialized - now create core schemas
            logger.debug("Creating core schemas for PostgreSQL database...")

            # TWO-PASS APPROACH:
            # Pass 1: Create all tables (including schema_registry)
            # Pass 2: Persist metadata for all tables (now schema_registry exists)
            created_schemas, failed_schemas, schema_defs = self._create_database_schemas()
            self._persist_schema_metadata(schema_defs)

            # Return success if at least the critical schemas were created
            if len(created_schemas) > 0:
                logger.debug(
                    "Database initialization complete: %s schemas created", len(created_schemas)
                )
                if failed_schemas:
                    logger.error("Some schemas failed: %s", failed_schemas)

                return create_success_result(
                    {
                        "initialized": True,
                        "phase": "Phase 3 - Database Operations",
                        "schemas_created": created_schemas,
                        "schemas_failed": failed_schemas,
                    }
                )

            error_msg = f"Failed to create any core schemas: {failed_schemas}"
            logger.error(error_msg)
            return create_error_result(
                error_msg,
                error_code="database.schema_creation_failed",
                details={"failed_schemas": failed_schemas},
            )
        except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
            logger.exception("Failed to initialize database")
            return create_error_result(
                str(e),
                error_code="database.init_failed",
                details={"exception": str(e)},
            )

    async def _cleanup(self) -> None:
        """Cleanup resources on plugin shutdown (private method)."""
        if self._provider:
            self._provider.close()
            logger.debug("%s cleanup complete", self.name)

    def get_config_schema(self) -> dict[str, object]:
        return get_plugin_config_schema()

    # =========================================================================
    # Platform Process Methods (Direct Plugin Access)
    # =========================================================================
    # These methods expose the same functionality as the interface implementations
    # but are directly accessible via plugin::postgres_state_management_plugin::*
    # process keys. This allows direct plugin invocation with customized result
    # processing.

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/read_state.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="read_state",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "namespace": ParameterMetadata(
                description="Target namespace to query",
                required=True,
                type=ParameterType.STRING,
            ),
            "query": ParameterMetadata(
                description="Query parameters including table, filters, and optional limit",
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Query results with records and metadata",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Query results containing records, count, namespace, table",
                    required=False,
                ),
            },
            usage_patterns=[
                "Query state database for records",
                "Filter and retrieve application data",
            ],
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def read_state_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> ActionResult:
        """Query PostgreSQL state database via direct plugin access."""
        namespace = params.get("namespace", "")
        query = params.get("query", {})
        return self.read_state(namespace, query)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/delete_records.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="delete_records",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace containing the records to delete",
                required=True,
                type=ParameterType.STRING,
            ),
            "query": ParameterMetadata(
                description="Deletion query including table, filters, and optional soft_delete flag",
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Deletion status with metadata about removed records",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Deletion result payload with result.deleted count",
                    required=False,
                ),
            },
            usage_patterns=[
                "Clean up deterministic test data",
                "Purge stale records before reloading fixtures",
            ],
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def delete_records_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> ActionResult:
        """Delete records from PostgreSQL database via direct plugin access."""
        namespace = params.get("namespace", "")
        query = params.get("query", {})
        return self.delete_records(namespace, query)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/describe_schema.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="describe_schema",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace to describe",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Schema definition with table structures",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Schema definition containing namespace and tables",
                    required=False,
                ),
            },
            usage_patterns=[
                "Inspect database schema",
                "Understand data structure",
            ],
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def describe_schema_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> ActionResult:
        """Get schema definition for namespace via direct plugin access."""
        namespace = params.get("namespace", "")
        return self.describe_schema(namespace)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/write_state.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="write_state",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "namespace": ParameterMetadata(
                description="Target namespace to write to",
                required=True,
                type=ParameterType.STRING,
            ),
            "data": ParameterMetadata(
                description="Data to write including table name and record(s)",
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Write result with generated IDs and insertion count",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Write result containing namespace and result with generated_id/inserted count",
                    required=False,
                ),
            },
            usage_patterns=[
                "Insert new records into the database",
                "Batch insert multiple records",
            ],
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def write_state_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> ActionResult:
        """Write data to PostgreSQL state database via direct plugin access."""
        namespace = params.get("namespace", "")
        data = params.get("data", {})
        return self.write_state(namespace, data)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/list_namespaces.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="list_namespaces",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="List of all namespaces",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Namespace list containing namespaces array and count",
                    required=False,
                ),
            },
            usage_patterns=[
                "Discover available namespaces",
                "List data partitions",
            ],
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def list_namespaces_action(
        self,
        params: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> ActionResult:
        """List all namespaces via direct plugin access."""
        return self.list_namespaces()

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/execute_sql.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="execute_sql",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sql_query": ParameterMetadata(
                description="SQL query to execute",
                required=True,
                type=ParameterType.STRING,
            ),
            "sql_params": ParameterMetadata(
                description="Query parameters for safe SQL parameterization",
                required=False,
                type=ParameterType.LIST,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="SQL query results",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Query results containing records and count",
                    required=False,
                ),
            },
            usage_patterns=[
                "Execute advanced SQL queries",
                "Perform complex database operations",
            ],
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def execute_sql_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> ActionResult:
        """Execute raw SQL query via direct plugin access."""
        sql_query = params.get("sql_query", "")
        sql_params = params.get("sql_params")
        return self.execute_sql(sql_query, sql_params)

    # =========================================================================
    # EdgeProcessProvider Implementation
    # =========================================================================

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """Return all edge process definitions for PostgreSQL state plugin.

        Returns:
            Dictionary mapping process names to their EdgeProcessDefinition.
        """
        return {
            "read_state": EdgeProcessDefinition(
                name="read_state",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "delete_records": EdgeProcessDefinition(
                name="delete_records",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "describe_schema": EdgeProcessDefinition(
                name="describe_schema",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "write_state": EdgeProcessDefinition(
                name="write_state",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "list_namespaces": EdgeProcessDefinition(
                name="list_namespaces",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "execute_sql": EdgeProcessDefinition(
                name="execute_sql",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
        }
