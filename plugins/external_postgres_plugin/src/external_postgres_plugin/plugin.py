"""External Postgres plugin entry point — the "super Datagrip" over foreign DBs.

Query (and, per the operator's 2026-08-09 posture reversal + Amendment 1, write
to) FOREIGN Postgres databases the operator registers as ``external_pg::<name>``
address-book entries. Every read verb stays READ-ONLY, HARD via the psycopg3
connection read-only characteristic (connection.py) — the developer-proof
write-stopper. The one write verb opens a NON-read_only connection instead and
performs no plugin-side access control of its own; the registered credential's
server-side Postgres GRANTs are the entire control plane for what it can
actually do (vendor RBAC, not a plugin re-implementation — see run_statement's
own docstring). Every verb takes a connection NAME (never a DSN); the
platform's own DB instance is refused role-independently, for every verb
(connection.assert_foreign_target).

Verbs (all EDGE):
  - list_schemas / list_tables / describe_table / test_connection / run_query /
    export_query / run_statement — all on the D0.3 deferred-completion shape
    (workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md):
    the dispatch handler returns ``{"job_id", "status": "queued"}`` in
    milliseconds; ``async_jobs.py``'s single background worker thread does the
    real connect + query I/O and completes the job. run_query/export_query's
    result — and run_statement's, when its statement produces one (e.g. a
    RETURNING clause) — is always written to the caller-supplied
    output_tsv_path when the job completes, never returned inline (default
    500 rows, up to 1000/50,000 with an acknowledged override — see
    query_actions.py for the full contract). run_statement is the write verb:
    single-statement contract, explicit per-call commit semantics, no
    statement-leader classification of any kind.
  - list_connections — the registered external_pg::* connection names (still
    synchronous — a single address-book scan, not a foreign-DB round trip).

No plugin-owned vault keys (the per-connection password is chain-consumed through
the address book's ``resolve_with_secrets``), so this plugin needs NO vault
binding — only address_book_service (the connection registry). Blob storage is
no longer used anywhere (bulk data lands as workspace TSV files; interactive
overflows fail loud).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.config.config_provider import ConfigProvider
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

from . import async_jobs, connection, export_containment
from .app_config import AppConfigLoader, ExternalPgConfigError
from .connection import ExternalPgGuardError
from .constants import (
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    CONFIG_KEY_PLATFORM_PG_PORT,
    CONFIG_KEY_STATEMENT_TIMEOUT_MS,
    DEFAULT_ROW_LIMIT,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    EXPORT_ROW_CAP,
    MAX_ROWS_HARD_CAP,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    PLATFORM_PG_PORT_DEFAULT,
    PLUGIN_NAME,
    RESULT_TYPE_DESCRIBE_TABLE,
    RESULT_TYPE_EXPORT_QUERY,
    RESULT_TYPE_LIST_CONNECTIONS,
    RESULT_TYPE_LIST_SCHEMAS,
    RESULT_TYPE_LIST_TABLES,
    RESULT_TYPE_RUN_QUERY,
    RESULT_TYPE_RUN_STATEMENT,
    RESULT_TYPE_TEST_CONNECTION,
    STATEMENT_TIMEOUT_MS_DEFAULT,
)
from .statement_guard import StatementGuardError


class ExternalPostgresPlugin(PluginBase, EdgeProcessProvider):
    """Read-only foreign-Postgres query plugin (the "super Datagrip")."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.logger: logging.Logger | None = None
        self._address_book_service: Any | None = None
        self._app_config_loader: AppConfigLoader | None = None
        # D0.3 deferred-completion machinery (async_jobs.py) — lazily acquired /
        # started on first async-shaped dispatch, mirroring
        # comfyui_image_generation_plugin's _try_acquire_job_manager (boot order
        # does not guarantee orchestrator_ref.async_job_manager is set yet at
        # prepare_for_readiness time, but it always is by first dispatch).
        self._async_job_manager: Any | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    # ------------------------------------------------------------------
    # VaultKeysProvider — no plugin-owned keys (password chain-consumed)
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required — per-connection passwords are chain-consumed.

        Each connection's password lives in the address book RESOLVER's namespace
        (``<solet>.default_address_book_plugin.external_pg_<name>_password``)
        and is read only through ``resolve_with_secrets`` under the resolver's
        identity — never a direct vault verb under this plugin. Post-2026-06-07
        namespace enforcement would reject such a key, so it is declared nowhere.
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """No scoped vault keys are read or written directly by this plugin."""
        return []

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def initialize(self, config: dict[str, object]) -> None:
        """Bind config_provider so yaml defaults + operator overrides take effect."""
        self.config_provider = ConfigProvider(self.name, config)

    def _config(self) -> ConfigProvider:
        """The bound config provider — fail loud if boot never called initialize().

        A missing binding is a lifecycle fault (a boot or re-instantiation path
        that skipped ``initialize``), never a license to guess: the prior
        ``or {}`` fallback silently turned exactly that fault into refuse-all
        exports on the snowflake sibling's live boot (2026-07-16).
        """
        if self.config_provider is None:
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                "config_provider not bound — plugin.initialize() was never "
                "called (boot/lifecycle fault)",
            )
        return self.config_provider

    def prepare_for_readiness(self) -> None:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")
        self.logger = logging.getLogger(self.name)
        self._address_book_service = self.orchestrator_ref.get_service("address_book_service")
        if self._address_book_service is None:
            raise RuntimeError(
                f"{ERROR_ADDRESS_BOOK_NOT_AVAILABLE}: {self.name} requires "
                "address_book_service as the connection registry"
            )
        self._app_config_loader = AppConfigLoader(self._address_book_service)
        self.set_ready()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _success(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": data,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _error(self, code: str, message: str) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.ERROR.value,
            "data": {},
            "actions": [],
            "error": {"code": code, "message": message},
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _statement_timeout_ms(self) -> int:
        """The per-statement timeout (ms) from plugin config — enforced POSITIVE, fail-loud.

        The timeout is the belt-tier DoS bound and is NON-DISABLEABLE by design:
        ``statement_timeout=0`` (or a negative value) disables the server-side
        cancel, so a non-positive / non-integer config value is REFUSED at parse
        (fast-fail) rather than silently opening a timeout-less connection. This is
        evaluated before every ``connection.connect`` call in ``_run_on_connection``,
        so a misconfigured plugin never opens a connection without the bound.
        """
        config = self._config()
        raw = config.get(CONFIG_KEY_STATEMENT_TIMEOUT_MS, STATEMENT_TIMEOUT_MS_DEFAULT)
        try:
            timeout_ms = int(str(raw))
        except ValueError as exc:
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_STATEMENT_TIMEOUT_MS} must be a positive integer (got {raw!r})",
            ) from exc
        if timeout_ms <= 0:
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_STATEMENT_TIMEOUT_MS} must be > 0 (got {timeout_ms}); a "
                "non-positive value would disable the statement-timeout DoS bound",
            )
        return timeout_ms

    def _platform_pg_port(self) -> int:
        """The platform's own Postgres port from plugin config — enforced valid, fail-loud.

        This value feeds the §8.4 containment guard's ``(host, port, dbname)`` INSTANCE
        compare, so a fat-fingered non-integer or out-of-range value would silently
        WEAKEN the guard's port arm (guard-integrity class). Mirrors F2's timeout
        bound: a present-but-invalid value (non-integer, or outside the TCP port
        range 1–65535) is REFUSED at parse (fast-fail) rather than silently degrading
        containment. Evaluated before every ``connection.connect`` in
        ``_run_on_connection``. An absent value keeps the sane 5432 default (the
        platform's own port), matching ``_statement_timeout_ms``'s absent→default.
        """
        config = self._config()
        raw = config.get(CONFIG_KEY_PLATFORM_PG_PORT, PLATFORM_PG_PORT_DEFAULT)
        try:
            port = int(str(raw))
        except ValueError as exc:
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_PLATFORM_PG_PORT} must be an integer in [1, 65535] (got {raw!r})",
            ) from exc
        if not 1 <= port <= 65535:
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_PLATFORM_PG_PORT} must be in [1, 65535] (got {port}); an "
                "out-of-range value would weaken the platform-DB containment guard's port arm",
            )
        return port

    def _export_path_gate(self, output_tsv_path: str) -> str:
        """Admit an export path via workspace-root containment; return the realpath.

        Binds the operator's ``export_allowed_roots`` config (yaml default
        ``[]`` = refuse-all; no hardcoded callsite default per authoring trap
        #10) to the own-copy containment gate. A malformed config value is a
        loud config fault, never a silent admit-all or refuse-all.
        """
        config = self._config()
        raw_roots = config.get(CONFIG_KEY_EXPORT_ALLOWED_ROOTS)
        roots: list[str] = []
        if raw_roots is not None:
            if not isinstance(raw_roots, list) or not all(
                isinstance(entry, str) for entry in raw_roots
            ):
                raise ExternalPgConfigError(
                    ERROR_NOT_CONFIGURED,
                    f"{CONFIG_KEY_EXPORT_ALLOWED_ROOTS} must be a list of directory "
                    "path strings",
                )
            roots = list(raw_roots)
        return export_containment.assert_export_path_allowed(
            output_tsv_path,
            roots,
            config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
            plugin_name=self.name,
        )

    def _run_on_connection(
        self,
        params: dict[str, Any],
        action: Callable[[Any], dict[str, Any]],
        verb: str,
        *,
        read_only: bool = True,
    ) -> dict[str, Any]:
        """Resolve a connection NAME, open a hardened connection, run the action.

        ``read_only`` defaults to ``True`` (every read verb); a write verb passes
        ``read_only=False`` explicitly — the server's own GRANTs on the registered
        credential then decide what the connection can actually do, never a
        plugin-side check.

        Error classification is TOPOLOGY-SAFE: connection/auth/permission classes
        return a generic fixed message; only the caller's-own-query classes carry
        driver detail (connection.classify_pg_error). The connection is always
        closed in ``finally`` — fresh connection per call (§8.6).
        """
        if self._app_config_loader is None:
            return self._error(ERROR_NOT_CONFIGURED, f"{self.name} is not ready")
        raw_name = params.get("connection_name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return self._error(ERROR_INVALID_PARAMS, "'connection_name' is required")
        conn: Any = None
        try:
            dsn = self._app_config_loader.resolve(raw_name)
            conn = connection.connect(
                dsn,
                read_only=read_only,
                statement_timeout_ms=self._statement_timeout_ms(),
                platform_pg_port=self._platform_pg_port(),
            )
            return self._success(action(conn))
        except Exception as exc:  # our coded guards + any driver fault -> typed
            code, message = self._classify_run_error(exc, verb)
            return self._error(code, message)
        finally:
            if conn is not None:
                conn.close()

    def _classify_run_error(self, exc: Exception, verb: str) -> tuple[str, str]:
        """Map a ``_run_on_connection`` exception to a typed, topology-safe (code, message).

        Our own coded guard errors surface their (safe) message; a ValueError is a
        bad param; anything else is a driver fault classified generically for the
        connection/auth/permission classes (connection.classify_pg_error).
        """
        if isinstance(
            exc,
            (
                ExternalPgConfigError,
                ExternalPgGuardError,
                StatementGuardError,
                export_containment.ExportPathRefusedError,
            ),
        ):
            return exc.code, str(exc)
        if isinstance(exc, ValueError):
            return ERROR_INVALID_PARAMS, str(exc)
        code, message = connection.classify_pg_error(exc)
        if self.logger:
            self.logger.warning("%s failed: %s", verb, code)
        return code, message

    def _dispatch_async(
        self, action_name: str, params: dict[str, Any], state: dict[str, Any],
    ) -> dict[str, Any]:
        """D0.3 ms-scale dispatch: create the job, return immediately — no I/O here."""
        try:
            create_result = async_jobs.create_job(
                self, action_name=action_name, params=params, state=state,
            )
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except RuntimeError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        if create_result.get("action_status") != "completed":
            error = create_result.get("error", {})
            message = str(error.get("message", "failed to create async job"))
            return self._error(ERROR_API_ERROR, message)
        return self._success(create_result["data"])

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "run_query": _edge("run_query", RESULT_TYPE_RUN_QUERY),
            "list_connections": _edge(
                "list_connections", RESULT_TYPE_LIST_CONNECTIONS),
            "list_schemas": _edge(
                "list_schemas", RESULT_TYPE_LIST_SCHEMAS),
            "list_tables": _edge(
                "list_tables", RESULT_TYPE_LIST_TABLES),
            "describe_table": _edge(
                "describe_table", RESULT_TYPE_DESCRIBE_TABLE),
            "export_query": _edge(
                "export_query", RESULT_TYPE_EXPORT_QUERY),
            "test_connection": _edge(
                "test_connection", RESULT_TYPE_TEST_CONNECTION),
            "run_statement": _edge(
                "run_statement", RESULT_TYPE_RUN_STATEMENT),
        }

    # ------------------------------------------------------------------
    # @platform_process implementations
    # ------------------------------------------------------------------

    @platform_process(
        name="run_query",
        display_name="External Postgres: Run Query",
        description=(
            "Run ONE read-only SQL statement against a registered foreign Postgres connection "
            "(by connection_name). Read leaders only (SELECT/WITH/EXPLAIN/SHOW/VALUES/TABLE); the "
            "connection is read-only at the server, so writes/DDL are refused. Returns immediately "
            "with a job_id and status 'queued' (D0.3 deferred-completion shape) — the dispatch "
            "returning is NOT the same as the job finishing. When the job completes, the result is "
            "ALWAYS written to the caller-supplied output_tsv_path, never returned inline — this "
            "connection is an arbitrary customer database, so there is no vendor-imposed row "
            f"ceiling to defer to; the limit below is entirely our own policy. Defaults to "
            f"{DEFAULT_ROW_LIMIT} rows to avoid exhausting the target database and disk, and to "
            "discourage pulling all rows for client-side filtering that a WHERE clause should do "
            "instead. To fetch more, pass acknowledge_default_limit_override=true together with "
            f"an explicit row_limit (up to {MAX_ROWS_HARD_CAP}) — both are required together, and "
            f"a row_limit above {MAX_ROWS_HARD_CAP} is refused rather than silently clamped. For "
            f"pulls beyond {MAX_ROWS_HARD_CAP} rows, use export_query instead (same override "
            f"mechanism, hard cap {EXPORT_ROW_CAP}). Use list_connections to see registered names. "
            "When the goal is validating that records exist or picking one to act on next, prefer "
            "selecting stable ID columns over email addresses or other PII-bearing fields — the "
            "query decides what columns come back, so a narrower SELECT is both cheaper and "
            "lower-exposure."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name (from list_connections).",
            ),
            "sql": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="A single read-only SQL statement.",
            ),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry."
                ),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    "Must be exactly true, together with row_limit, to fetch more than the "
                    f"default {DEFAULT_ROW_LIMIT} rows. Requires understanding why the default "
                    "exists: avoiding exhausted rate limits/disk, and pulling all rows to filter "
                    "client-side instead of writing a proper WHERE clause."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit row ceiling, up to {MAX_ROWS_HARD_CAP}. Only honored together with "
                    f"acknowledge_default_limit_override=true; refused (not clamped) above "
                    f"{MAX_ROWS_HARD_CAP}."
                ),
            ),
        },
        output_type="object",
        output_description="Job ID and status for async query tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the TSV handle itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def run_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("run_query", params, state)

    @platform_process(
        name="list_connections",
        display_name="External Postgres: List Connections",
        description=(
            "List the registered foreign-Postgres connection names (external_pg::* address-book "
            "entries). Names only — never hosts, users, or passwords. Use a name with the other verbs."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="The registered connection names.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_connections(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        if self._app_config_loader is None:
            return self._error(ERROR_NOT_CONFIGURED, f"{self.name} is not ready")
        try:
            names, truncated = self._app_config_loader.list_connection_names()
        except Exception:  # address-book fault -> generic
            return self._error(ERROR_API_ERROR, "could not list connections")
        if truncated and self.logger:
            self.logger.warning("list_connections truncated at the address-book scan limit")
        return self._success({"connections": names})

    @platform_process(
        name="list_schemas",
        display_name="External Postgres: List Schemas",
        description=(
            "List the non-system schemas in a registered foreign Postgres connection. Requires "
            "connection_name. Returns immediately with a job_id and status 'queued' — the actual "
            "schema list is delivered when the job completes (D0.3 deferred-completion shape); the "
            "dispatch returning is NOT the same as the job finishing."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name.",
            ),
        },
        output_type="object",
        output_description="Job ID and status for async schema-list tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the schema list itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_schemas(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("list_schemas", params, state)

    @platform_process(
        name="list_tables",
        display_name="External Postgres: List Tables",
        description=(
            "List tables and views in a schema of a registered foreign Postgres connection. "
            "Requires connection_name and schema. Returns immediately with a job_id and status "
            "'queued' — the actual table list is delivered when the job completes (D0.3 "
            "deferred-completion shape); the dispatch returning is NOT the same as the job finishing."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name.",
            ),
            "schema": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The schema to list tables from.",
            ),
        },
        output_type="object",
        output_description="Job ID and status for async table-list tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the table list itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_tables(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("list_tables", params, state)

    @platform_process(
        name="describe_table",
        display_name="External Postgres: Describe Table",
        description=(
            "Describe a table's columns (name, type, nullability, default) in a registered foreign "
            "Postgres connection. Requires connection_name, schema, and table. Returns immediately "
            "with a job_id and status 'queued' — the column description is delivered when the job "
            "completes (D0.3 deferred-completion shape); the dispatch returning is NOT the same as "
            "the job finishing."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name.",
            ),
            "schema": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The table's schema.",
            ),
            "table": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The table name.",
            ),
        },
        output_type="object",
        output_description="Job ID and status for async table-describe tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the column list itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def describe_table(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("describe_table", params, state)

    @platform_process(
        name="export_query",
        display_name="External Postgres: Export Query",
        description=(
            "The N>>500 route: run a read-only query against a registered foreign Postgres "
            "connection and write the result as ONE tab-separated .tsv file at an ABSOLUTE "
            "output_tsv_path in the operator's workspace. Returns immediately with a job_id and "
            "status 'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the "
            "same as the job finishing. The path must lie under an operator-configured "
            "export_allowed_roots entry (empty config refuses every export). Same read-only rules "
            "and override mechanism as run_query, with a higher hard cap: this connection is an "
            "arbitrary customer database, so there is no vendor-imposed row ceiling, only our own "
            f"policy. Defaults to {DEFAULT_ROW_LIMIT} rows absent an acknowledged override — for "
            "that common small/default case, run_query has an identical interface with a lower "
            "ceiling. To fetch more, pass acknowledge_default_limit_override=true together with an "
            f"explicit row_limit (up to {EXPORT_ROW_CAP}) — both are required together, and a "
            f"row_limit above {EXPORT_ROW_CAP} is refused rather than silently clamped. Requires "
            "connection_name, sql, and output_tsv_path. When the goal is validating that records "
            "exist rather than inspecting their content, prefer selecting stable ID columns over "
            "email addresses or other PII-bearing fields — the query decides what columns land in "
            "the file."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name.",
            ),
            "sql": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="A single read-only SQL statement.",
            ),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry."
                ),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    "Must be exactly true, together with row_limit, to fetch more than the "
                    f"default {DEFAULT_ROW_LIMIT} rows. Requires understanding why the default "
                    "exists: avoiding exhausted rate limits/disk, and pulling all rows to filter "
                    "client-side instead of writing a proper WHERE clause."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit row ceiling, up to {EXPORT_ROW_CAP}. Only honored together with "
                    f"acknowledge_default_limit_override=true; refused (not clamped) above "
                    f"{EXPORT_ROW_CAP}."
                ),
            ),
        },
        output_type="object",
        output_description="Job ID and status for async export tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the TSV handle itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def export_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("export_query", params, state)

    @platform_process(
        name="test_connection",
        display_name="External Postgres: Test Connection",
        description=(
            "Open a registered foreign Postgres connection and confirm it: the resolved host it "
            "connected to, the server version, the current role, and the read-only flag (always "
            "true). Use this to verify a connection points where you expect (the host you "
            "registered) before trusting it. Requires connection_name. Returns immediately with a "
            "job_id and status 'queued' — the confirmation is delivered when the job completes "
            "(D0.3 deferred-completion shape); the dispatch returning is NOT the same as the job "
            "finishing."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name.",
            ),
        },
        output_type="object",
        output_description="Job ID and status for async connection-test tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the confirmation itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def test_connection(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("test_connection", params, state)

    @platform_process(
        name="run_statement",
        display_name="External Postgres: Run Statement",
        description=(
            "Run ONE SQL statement against a registered foreign Postgres connection (by "
            "connection_name) on a WRITE-CAPABLE (non-read-only) connection — INSERT/UPDATE/"
            "DELETE/DDL/anything, not just reads. What the statement is actually allowed to do "
            "is decided entirely by the registered credential's own server-side Postgres GRANTs "
            "— this verb performs no read/write classification or permission check of its own "
            "(vendor RBAC is the control plane, operator ruling 2026-08-09 + Amendment 1). "
            "Single-statement contract for v1 (an engineering convention for predictable commit "
            "semantics, not a permission gate) — no multi-statement scripts. A statement with no "
            "result set (the common INSERT/UPDATE/DELETE/DDL case, no RETURNING) commits and "
            "returns rowcount inline. A statement that DOES produce a result set (a RETURNING "
            "clause) routes through the SAME always-TSV export path as run_query: rows are never "
            "returned inline, at any size, so output_tsv_path is then required — its absence "
            "rolls the whole statement back rather than silently discarding the returned rows "
            "while still committing the write. Returns immediately with a job_id and status "
            "'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same "
            "as the job finishing. Use list_connections to see registered names."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name (from list_connections).",
            ),
            "sql": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "A single SQL statement — any statement the registered credential's own "
                    "server-side GRANTs permit, not just reads."
                ),
            ),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=(
                    "ABSOLUTE .tsv destination path, contained under an export_allowed_roots "
                    "entry. Required ONLY if the statement produces a result set (e.g. a "
                    "RETURNING clause) — omit it for a plain INSERT/UPDATE/DELETE/DDL with no "
                    "RETURNING, which returns rowcount inline instead."
                ),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    "Only relevant when the statement has a RETURNING clause: must be exactly "
                    f"true, together with row_limit, to fetch more than the default "
                    f"{DEFAULT_ROW_LIMIT} returned rows."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Only relevant when the statement has a RETURNING clause: explicit row "
                    f"ceiling on the RETURNING rows, up to {MAX_ROWS_HARD_CAP}. Only honored "
                    "together with acknowledge_default_limit_override=true."
                ),
            ),
        },
        output_type="object",
        output_description="Job ID and status for async statement-execution tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the statement's own result.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def run_statement(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("run_statement", params, state)


def _edge(
    name: str,
    result_type: str,
) -> EdgeProcessDefinition:
    return EdgeProcessDefinition(
        name=name,
        result_processor_template_customizations=MergeResultProcessorCustomizations(
            result_type=result_type,
        ),
        error_processor_template_customizations=MergeErrorProcessorCustomizations(
            retryable=True,
        ),
    )
