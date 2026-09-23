"""PGVector service plugin for vector storage and similarity search.

Standalone plugin — owns its own @platform_process surface.
Provider machinery lives in pgvector_service_plugin.postgres_backend.vector.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, cast

from ananta.core.actions.action_metadata import (
    platform_process,
)
from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.decorators import service_lifecycle
from ananta.core.plugins.plugin_base import ServicePlugin
from ananta.interfaces.state_aware_plugin import StateAwarePlugin
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.interfaces.vault_service_interface import VaultServiceInterface
from ananta.interfaces.vector_service_interface import VectorServiceInterface
from ananta.services.vector_service.interfaces import VectorProvider, VectorServiceAPI
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    SchemaDefinition,
    TableSchema,
)

from pgvector_service_plugin.postgres_backend.vector.config import PGVectorConfig
from pgvector_service_plugin.postgres_backend.vector.constants import (
    DEFAULT_DISTANCE_METRIC,
    TABLE_EMBEDDINGS,
    DistanceMetric,
)
from pgvector_service_plugin.postgres_backend.vector.provider import (
    PGVectorProvider,
    make_local_pool,
)

PLUGIN_NAMESPACE: str = "pgvector_service_plugin"

# SQL-lockdown credential wiring (2026-06-23): non-secret connection params
# (host/port/database/db_schema/user) come from the ``pgvector_service_db``
# address-book entry; the password comes from this plugin's OWN vault namespace
# (``<solet>.pgvector_service_plugin.password``). Nothing credential-bearing
# lives in plugin config. The bundled ``resolve_with_secrets`` path is NOT used:
# caller-enforcement denies the address-book plugin reading another plugin's
# scoped secret, so the password is fetched directly (own-namespace read passes).
_DB_ADDRESS_BOOK_ENTRY: str = "pgvector_service_db"

logger = logging.getLogger(__name__)


class PGVectorServicePlugin(
    ServicePlugin, VectorServiceAPI, VectorProvider, VectorServiceInterface, StateAwarePlugin
):
    """PGVector service plugin for vector storage and similarity search."""

    def __init__(self) -> None:
        super().__init__()
        self.name = PLUGIN_NAMESPACE
        self._provider: PGVectorProvider | None = None
        self._state_service: StateServiceProtocol | None = None
        # Caller-bound VaultServiceProxy injected by lifecycle
        # (_inject_vault_service in startup_sequence.py) BEFORE the readiness
        # loop. Its baked-in CallContext lets own-namespace secret retrieval
        # pass vault caller-enforcement; the raw get_service("vault_service")
        # handle carries no context and is rejected with VaultAccessDeniedError.
        self._vault_service: VaultServiceInterface | None = None

    def get_readiness_error(self) -> str | None:
        return self.readiness_error

    def set_vault_service(self, vault_service: VaultServiceInterface) -> None:
        """Receive the caller-bound VaultServiceProxy from lifecycle injection.

        Built by ``_inject_vault_service`` (startup_sequence.py) with this
        plugin's ``CallContext`` baked in, so ``retrieve`` passes vault
        caller-enforcement for the plugin's own namespace. Injected before
        ``prepare_for_readiness`` runs.
        """
        self._vault_service = vault_service

    def prepare_for_readiness(self) -> None:
        """Acquire state_service and ensure required Postgres extensions.

        DB connection params (host/port/database/user/password) are NOT
        resolved here — they are resolved in ``initialize`` at provider-build
        time, where the platform's lifecycle guarantees the address book +
        vault proxy are available (see ``initialize`` docstring).
        """
        if not self.orchestrator_ref:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")

        state_service = self.orchestrator_ref.get_service("state_service")
        if state_service is None:
            raise RuntimeError(f"{self.name}: state_service not available")
        self._state_service = cast(StateServiceProtocol, state_service)

        logger.debug("%s: state_service acquired from orchestrator", self.name)

        # Ensure pgvector (this plugin owns it) and pg_trgm (M21 keyword search
        # across __event.content_text; lives alongside pgvector in the same
        # full-text-adjacent extension family). pg_trgm is added here for local-
        # Postgres parity with the RDS path (rds_postgres_state_management_plugin
        # installs both via the master pool); on local Postgres the app role can
        # CREATE EXTENSION if the binary is available, which both pgvector and
        # pg_trgm typically are on a brew-installed PostgreSQL.
        for ext_name in ("vector", "pg_trgm"):
            ext_result = self._state_service.execute_sql(
                sql_query=f"CREATE EXTENSION IF NOT EXISTS {ext_name};",
                sql_params=None,
                calling_service=self.name,
                calling_namespace=self.name,
            )
            if ext_result.get("action_status") != "completed":
                error = ext_result.get("error", "unknown error")
                raise RuntimeError(
                    f"{self.name}: failed to ensure {ext_name} extension: {error}",
                )
            logger.debug("%s: %s extension ensured", self.name, ext_name)

    def _resolve_connection_overrides(self) -> dict[str, object]:
        """Resolve DB connection params from the address book + vault.

        Non-secret params (host/port/database/db_schema/user) come from the
        ``pgvector_service_db`` address-book entry; the password comes from
        this plugin's own vault namespace. The bundled ``resolve_with_secrets``
        is unusable (caller-enforcement denies the address-book plugin reading
        another plugin's scoped secret), so the password is fetched via this
        plugin's injected ``VaultServiceProxy`` (``self._vault_service``) — an
        own-namespace read whose baked-in ``CallContext`` passes
        ``enforce_namespace`` (the raw ``get_service('vault_service')`` handle
        carries no context and is rejected with ``VaultAccessDeniedError``).
        """
        if not self.orchestrator_ref:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")
        address_book = self.orchestrator_ref.get_service("address_book_service")
        if address_book is None:
            raise RuntimeError(f"{self.name}: address_book_service not available")
        if self._vault_service is None:
            raise RuntimeError(
                f"{self.name}: vault_service proxy not injected "
                "(set_vault_service runs before prepare_for_readiness)",
            )
        overrides = self._resolve_address_book_overrides(address_book)
        overrides["password"] = self._resolve_db_password(self._vault_service)
        return overrides

    def _resolve_address_book_overrides(self, address_book: Any) -> dict[str, object]:
        """Non-secret connection params from the ``pgvector_service_db`` entry."""
        resolved = address_book.resolve(name=_DB_ADDRESS_BOOK_ENTRY)
        if resolved.get("action_status") != "completed":
            raise RuntimeError(
                f"{self.name}: failed to resolve address-book entry "
                f"{_DB_ADDRESS_BOOK_ENTRY!r}: {resolved.get('error')}",
            )
        entries = resolved.get("data", {}).get("entries", [])
        overrides: dict[str, object] = {
            str(entry["description"]): entry["value"]
            for entry in entries
            if entry.get("field_type") != "password"
        }
        if "port" in overrides:
            overrides["port"] = int(cast(str, overrides["port"]))
        return overrides

    def _resolve_db_password(self, vault_service: VaultServiceInterface) -> object:
        """The DB password from this plugin's own vault namespace."""
        solet = os.environ.get("SOLET_NAME", "").strip()
        if not solet:
            raise RuntimeError(
                f"{self.name}: SOLET_NAME required to resolve the vault "
                "key for the database password.",
            )
        password_key = f"{solet}.{PLUGIN_NAMESPACE}.password"
        secret = vault_service.retrieve(key=password_key)
        if secret.get("action_status") != "completed":
            raise RuntimeError(
                f"{self.name}: failed to retrieve DB password from vault key "
                f"{password_key!r}: {secret.get('error')}",
            )
        secret_data = secret.get("data")
        if not isinstance(secret_data, dict) or "value" not in secret_data:
            raise RuntimeError(
                f"{self.name}: vault retrieve for {password_key!r} returned no "
                "secret value in its result payload",
            )
        return secret_data["value"]

    service_interfaces: tuple[type, ...] = (VectorServiceInterface,)
    supported_interface_versions: dict[type, str] = {
        VectorServiceInterface: VectorServiceInterface.INTERFACE_VERSION
    }

    def initialize(self, config: dict[str, object]) -> None:
        """Build the provider; resolve DB connection params at build time.

        The platform calls ``initialize`` once EARLY (in
        ``_initialize_plugin_configs``, before services are wired) and again
        from ``start_services`` AFTER ``prepare_for_readiness`` has run. The
        connection params (host/port/database/db_schema/user/password) are
        resolved from the address book + vault HERE — at the build call where
        the services are available — rather than in ``prepare_for_readiness``,
        so the provider's pool always uses the resolved credentials regardless
        of which call builds it (a stale split previously built the pool with
        ``PGVectorConfig`` defaults → ``role "ananta_user" does not exist``).
        ``config`` supplies only the non-credential tuning params
        (pool_size, hnsw_*).
        """
        if self.is_ready():
            return
        if self._state_service is None or self._vault_service is None:
            # Early initialize() before service wiring — defer the provider
            # build to the start_services() call that runs after
            # prepare_for_readiness (when state_service + vault are available).
            logger.debug(
                "%s: services not yet wired; deferring provider build", self.name,
            )
            return
        try:
            overrides = self._resolve_connection_overrides()
            merged_config = {**config, **overrides}
            vector_config = PGVectorConfig(**merged_config)  # type: ignore[arg-type]
            self._provider = PGVectorProvider(
                vector_config,
                PLUGIN_NAMESPACE,
                pool_builder=make_local_pool,
                state_service=self._state_service,
            )
            self._provider.initialize()
            self.set_ready()
            logger.debug("%s initialized successfully", self.name)
        except Exception as e:
            error_msg = f"Plugin initialization failed: {e}"
            logger.error("Failed to initialize %s: %s", self.name, e)
            self.set_error(error_msg)
            raise RuntimeError(error_msg) from e

    @service_lifecycle(operation="start")
    async def start_services(self) -> ActionResult:
        """Start the pgvector service."""
        if self._services_started:
            return {
                "action_status": "completed",
                "data": {"message": "Service already running"},
                "actions": [],
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }

        logger.debug("Starting %s service...", self.name)

        import os
        from pathlib import Path

        config_path = (
            Path(os.environ.get("APP_HOME", ".")) / "config" / "plugins" / f"{self.name}.json"
        )

        if not config_path.exists():
            return {
                "action_status": "error",
                "data": {},
                "actions": [],
                "error": {
                    "type": "ConfigurationError",
                    "code": f"{self.name}.config_not_found",
                    "message": f"Configuration file not found: {config_path}",
                    "details": {"config_path": str(config_path)},
                    "severity": "ERROR",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                "timestamp": datetime.now(UTC).isoformat(),
            }

        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception as e:
            return {
                "action_status": "error",
                "data": {},
                "actions": [],
                "error": {
                    "type": "ConfigurationError",
                    "code": f"{self.name}.config_load_failed",
                    "message": f"Failed to load config from {config_path}: {e}",
                    "details": {"config_path": str(config_path), "exception": str(e)},
                    "severity": "ERROR",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                "timestamp": datetime.now(UTC).isoformat(),
            }

        try:
            self.initialize(config)
        except Exception as e:
            return {
                "action_status": "error",
                "data": {},
                "actions": [],
                "error": {
                    "type": "InitializationError",
                    "code": f"{self.name}.initialization_failed",
                    "message": f"Failed to initialize provider: {e}",
                    "details": {"exception": str(e)},
                    "severity": "ERROR",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                "timestamp": datetime.now(UTC).isoformat(),
            }

        self._services_started = True
        self._service_started_at = datetime.now(UTC).isoformat()
        self._service_error = None

        logger.debug("%s service started successfully", self.name)

        return {
            "action_status": "completed",
            "data": {"message": "Service started successfully", "started_at": self._service_started_at},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @service_lifecycle(operation="stop")
    async def stop_services(self) -> ActionResult:
        """Stop the pgvector service and release resources."""
        if not self._services_started:
            return {
                "action_status": "completed",
                "data": {"message": "Service already stopped"},
                "actions": [],
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }

        if self.is_active_interface_provider():
            supported = ", ".join(self.get_supported_interfaces())
            return {
                "action_status": "error",
                "data": {},
                "actions": [],
                "error": {
                    "type": "ServicePluginError",
                    "code": f"{self.name}.cannot_stop_active_provider",
                    "message": f"Cannot stop: currently supporting active interfaces: {supported}",
                    "details": {"supporting_interfaces": list(self.get_supported_interfaces())},
                    "severity": "ERROR",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                "timestamp": datetime.now(UTC).isoformat(),
            }

        logger.debug("Stopping %s service...", self.name)

        if self._provider:
            try:
                self._provider.close()
                logger.debug("Provider connection closed")
            except Exception as e:
                logger.error("Error closing provider: %s", e)
            self._provider = None

        self._services_started = False
        self._service_started_at = None

        logger.debug("%s service stopped", self.name)

        return {
            "action_status": "completed",
            "data": {"message": "Service stopped successfully"},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        """Return schema definition for the embeddings table."""
        embeddings_table = TableSchema(
            table_name=TABLE_EMBEDDINGS,
            id_prefix="emb",
            columns={
                "embedding": ColumnDefinition(
                    type=ColumnType.VECTOR,
                    type_params={},
                    not_null=True,
                    description="Vector embedding stored as native pgvector type",
                ),
                "dimension": ColumnDefinition(
                    type=ColumnType.INTEGER,
                    not_null=True,
                    description="Vector dimension for validation and metadata",
                ),
                "metadata": ColumnDefinition(
                    type=ColumnType.TEXT,
                    not_null=False,
                    description="JSON metadata associated with the vector",
                ),
                "distance_metric": ColumnDefinition(
                    type=ColumnType.TEXT,
                    not_null=False,
                    description="Distance metric used for similarity search",
                ),
            },
        )
        return [SchemaDefinition(
            namespace=self.name,
            tables={TABLE_EMBEDDINGS: embeddings_table},
            version="1.0.0",
            description="Vector storage schema for pgvector service",
        )]

    def set_state_service(self, state_service: StateServiceProtocol) -> None:
        self._state_service = state_service
        if self._provider:
            self._provider._state_service = state_service

    def _get_provider(self) -> PGVectorProvider:
        if not self._provider:
            raise RuntimeError("Provider not initialized. Call initialize() first.")
        return self._provider

    def _create_success_result(self, data: dict[str, Any]) -> ActionResult:
        return ActionResult(
            action_status=ActionStatus.COMPLETED.value,
            data={"result": data},
            actions=[],
            error=None,
            timestamp=datetime.now(UTC).isoformat(),
        )

    def _create_error_result(self, error_message: str) -> ActionResult:
        return ActionResult(
            action_status=ActionStatus.ERROR.value,
            data={},
            actions=[],
            error={
                "type": "PluginError",
                "code": "pgvector.operation_failed",
                "message": error_message,
                "details": {},
                "severity": ActionStatus.ERROR.value,
                "timestamp": datetime.now(UTC).isoformat(),
            },
            timestamp=datetime.now(UTC).isoformat(),
        )

    def store_vectors(self, namespace: str, vectors: list[dict[str, object]]) -> ActionResult:
        """Store vectors with metadata."""
        logger.debug("store_vectors called with namespace=%s, vector_count=%d", namespace, len(vectors))
        try:
            provider = self._get_provider()
            typed_vectors: list[dict[str, Any]] = []
            for v in vectors:
                payload = v
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"Invalid vector payload string: {payload}") from exc
                if not isinstance(payload, dict):
                    raise ValueError(f"Invalid vector payload type: {type(payload)} - {payload}")
                typed_vectors.append(dict(payload))
            result = provider.store_vectors(namespace, typed_vectors)
            return self._create_success_result(result)
        except Exception as e:
            logger.error("Failed to store vectors: %s", e)
            return self._create_error_result(str(e))

    def search_similar(
        self,
        namespace: str,
        query_vector: list[float],
        top_k: int = 10,
        filters: dict[str, object] | None = None,
        distance_metric: DistanceMetric = DEFAULT_DISTANCE_METRIC,
    ) -> ActionResult:
        """Search for similar vectors in a namespace."""
        try:
            provider = self._get_provider()
            typed_filters: dict[str, Any] | None = dict(filters) if filters else None
            result = provider.search_similar(
                namespaces=[namespace],
                query_vector=query_vector,
                top_k=top_k,
                filters=typed_filters,
                distance_metric=distance_metric,
            )
            return self._create_success_result(result)
        except (ValueError, TypeError) as e:
            logger.error("Invalid parameters for search_similar: %s", e)
            return self._create_error_result(str(e))
        except Exception as e:
            logger.error("Failed to search similar vectors: %s", e)
            return self._create_error_result(str(e))

    def get_vector(self, namespace: str, vector_id: str) -> ActionResult:
        """Retrieve specific vector by ID."""
        try:
            result = self._get_provider().get_vector(namespace, vector_id)
            return self._create_success_result(result)
        except Exception as e:
            logger.error("Failed to get vector: %s", e)
            return self._create_error_result(str(e))

    def delete_vectors(
        self,
        namespace: str,
        vector_ids: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> ActionResult:
        """Delete vectors by IDs or filters."""
        if vector_ids is None and filters is None:
            return self._create_error_result("Either vector_ids or filters must be provided")
        try:
            result = self._get_provider().delete_vectors(namespace, vector_ids)
            return self._create_success_result(result)
        except Exception as e:
            logger.error("Failed to delete vectors: %s", e)
            return self._create_error_result(str(e))

    def delete_all_in_namespace(self, namespace: str) -> ActionResult:
        """Hard-delete every vector row in a namespace."""
        try:
            result = self._get_provider().delete_all_in_namespace(namespace)
            return self._create_success_result(result)
        except Exception as e:
            logger.error("Failed to delete_all_in_namespace(%s): %s", namespace, e)
            return self._create_error_result(str(e))

    def delete_by_external_ids(self, namespace: str, external_ids: list[str]) -> ActionResult:
        """Delete vectors by their external_id field."""
        if not self._provider:
            return self._create_error_result("Vector provider not initialized")
        try:
            result = self._provider.delete_by_external_ids(namespace=namespace, external_ids=external_ids)
            return self._create_success_result(result)
        except Exception as e:
            logger.error("Failed to delete by external_ids: %s", e)
            return self._create_error_result(str(e))

    def find_missing_external_ids(
        self, namespace: str, candidate_external_ids: list[str]
    ) -> ActionResult:
        """Return the external_ids with no active vector (orphan-reconcile read)."""
        if not self._provider:
            return self._create_error_result("Vector provider not initialized")
        try:
            result = self._provider.find_missing_external_ids(
                namespace=namespace, candidate_external_ids=candidate_external_ids
            )
            return self._create_success_result(result)
        except Exception as e:
            logger.error("Failed to find missing external_ids: %s", e)
            return self._create_error_result(str(e))

    def update_metadata(
        self, namespace: str, vector_id: str, metadata: dict[str, object]
    ) -> ActionResult:
        """Update vector metadata."""
        try:
            result = self._get_provider().update_metadata(namespace, vector_id, dict(metadata))
            return self._create_success_result(result)
        except Exception as e:
            logger.error("Failed to update metadata: %s", e)
            return self._create_error_result(str(e))

    def list_namespaces(self) -> ActionResult:
        """List all vector namespaces."""
        try:
            result = self._get_provider().list_namespaces()
            return self._create_success_result(result)
        except Exception as e:
            logger.error("Failed to list namespaces: %s", e)
            return self._create_error_result(str(e))

    def get_namespace_stats(self, namespace: str) -> ActionResult:
        """Get statistics for namespace."""
        try:
            result = self._get_provider().get_namespace_stats(namespace)
            return self._create_success_result(result)
        except Exception as e:
            logger.error("Failed to get namespace stats: %s", e)
            return self._create_error_result(str(e))

    def get_config_schema(self) -> dict[str, object]:
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "PGVector Service Plugin",
            "description": "PostgreSQL with pgvector extension for vector storage and similarity search",
            "type": "object",
            "required": ["host", "port", "database", "user", "password"],
            "properties": {
                "host": {"type": "string", "title": "PostgreSQL Host", "default": "localhost", "x-group": "connection", "x-order": 1},
                "port": {"type": "integer", "title": "PostgreSQL Port", "default": 5432, "minimum": 1, "maximum": 65535, "x-group": "connection", "x-order": 2},
                "database": {"type": "string", "title": "Database Name", "default": "ananta_db", "x-group": "connection", "x-order": 3},
                "user": {"type": "string", "title": "Database User", "default": "ananta_user", "x-group": "connection", "x-order": 4},
                "password": {"type": "string", "title": "Database Password", "default": "change_me", "x-secret": True, "x-group": "security", "x-order": 1},
                "db_schema": {"type": "string", "title": "Database Schema", "default": "state", "x-group": "advanced", "x-order": 1},
                "pool_size": {"type": "integer", "title": "Connection Pool Size", "default": 10, "minimum": 1, "x-group": "advanced", "x-order": 2},
                "hnsw_m": {"type": "integer", "title": "HNSW Max Connections", "default": 16, "minimum": 1, "x-group": "advanced", "x-order": 3},
                "hnsw_ef_construction": {"type": "integer", "title": "HNSW Construction Size", "default": 64, "minimum": 1, "x-group": "advanced", "x-order": 4},
            },
        }

    @platform_process(
        name="store_vectors_action",
        output_type="object",
        summary="Store vector embeddings for semantic search",
        requires_result_processor=True,
    )
    def store_vectors_action(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Store vectors - action handler."""
        result = self.store_vectors(params.get("namespace", ""), params.get("vectors", []))
        return dict(result)

    @platform_process(
        name="search_similar_action",
        output_type="object",
        summary="Search for similar vectors using semantic similarity",
        requires_result_processor=True,
    )
    def search_similar_action(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Search similar vectors - action handler."""
        distance_metric_str = params.get("distance_metric", "COSINE")
        distance_metric = (
            DistanceMetric[distance_metric_str] if distance_metric_str else DEFAULT_DISTANCE_METRIC
        )
        result = self.search_similar(
            namespace=params.get("namespace", ""),
            query_vector=params.get("query_vector", []),
            top_k=params.get("top_k", 10),
            filters=params.get("filters"),
            distance_metric=distance_metric,
        )
        return dict(result)

    @platform_process(
        name="get_vector_action",
        output_type="object",
        summary="Retrieve a specific vector by ID",
        requires_result_processor=True,
    )
    def get_vector_action(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Get vector by ID - action handler."""
        result = self.get_vector(params.get("namespace", ""), params.get("vector_id", ""))
        return dict(result)

    @platform_process(
        name="delete_vectors_action",
        output_type="object",
        summary="Delete vectors by IDs or filters",
        requires_result_processor=True,
    )
    def delete_vectors_action(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Delete vectors - action handler."""
        result = self.delete_vectors(
            params.get("namespace", ""), params.get("vector_ids"), params.get("filters")
        )
        return dict(result)

    @platform_process(
        name="update_metadata_action",
        output_type="object",
        summary="Update metadata for an existing vector",
        requires_result_processor=True,
    )
    def update_metadata_action(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Update vector metadata - action handler."""
        result = self.update_metadata(
            params.get("namespace", ""), params.get("vector_id", ""), params.get("metadata", {})
        )
        return dict(result)

    @platform_process(
        name="list_namespaces_action",
        output_type="object",
        summary="List all vector namespaces",
        requires_result_processor=True,
    )
    def list_namespaces_action(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """List namespaces - action handler."""
        return dict(self.list_namespaces())

    @platform_process(
        name="get_namespace_stats_action",
        output_type="object",
        summary="Get statistics for a vector namespace",
        requires_result_processor=True,
    )
    def get_namespace_stats_action(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Get namespace stats - action handler."""
        return dict(self.get_namespace_stats(params.get("namespace", "")))
