"""Snowflake plugin entry point — a warehouse query (and write) connector.

Query — and, per the operator's 2026-08-09 posture reversal + Amendment 1,
write to — the operator-registered Snowflake account ("snowflake_account"
address book entry). Every read verb stays READ-ONLY via the statement-leader
guard; Snowflake has NO session-level read-only flag, so that guard is
FAST-FAIL ONLY — the TRUE developer-proof boundary for those verbs is the
read-only ROLE the connection is pinned to. The one write verb,
``run_statement``, performs no plugin-side access control of its own; the
registered credential's server-side role grants are the entire control plane
for what it can actually do (vendor RBAC, not a plugin re-implementation —
see run_statement's own docstring).

Verbs (all EDGE), all on the D0.3 deferred-completion shape
(workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md):
the dispatch handler returns ``{"job_id", "status": "queued"}`` in
milliseconds; ``async_jobs.py``'s single background worker thread does the
real connect + query I/O and completes the job.
  - run_query       — one read-only statement; result written as a TSV file
    at the caller's output_tsv_path (default 500 rows, up to 1000 with an
    acknowledged override) — never rows inline, at any size
  - list_databases  — databases visible to the current role
  - list_schemas / list_tables / describe_table — introspection
  - export_query    — the N>>500 route: full result written as a TSV file in
    the operator's workspace (absolute output_tsv_path, contained under the
    export_allowed_roots config; refuse-all when unset), same override
    mechanism as run_query with a higher hard cap (50,000)
  - test_connection — account, user, role, warehouse, version
  - run_statement   — the write verb: single-statement contract, explicit
    per-call commit semantics, no statement-leader classification of any kind

No plugin-owned vault keys (the private key is chain-consumed through the
address book's ``resolve_with_secrets``), so this plugin needs NO vault
binding — only address_book_service (credential resolution). Blob storage is
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
from .app_config import AppConfigLoader, SnowflakeConfigError
from .constants import (
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    CONFIG_KEY_LOGIN_TIMEOUT_SECONDS,
    CONFIG_KEY_STATEMENT_TIMEOUT_SECONDS,
    DEFAULT_ROW_LIMIT,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    EXPORT_ROW_CAP,
    LOGIN_TIMEOUT_SECONDS_DEFAULT,
    MAX_ROWS_HARD_CAP,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    PLUGIN_NAME,
    RESULT_TYPE_DESCRIBE_TABLE,
    RESULT_TYPE_EXPORT_QUERY,
    RESULT_TYPE_LIST_DATABASES,
    RESULT_TYPE_LIST_SCHEMAS,
    RESULT_TYPE_LIST_TABLES,
    RESULT_TYPE_RUN_QUERY,
    RESULT_TYPE_RUN_STATEMENT,
    RESULT_TYPE_TEST_CONNECTION,
    STATEMENT_TIMEOUT_SECONDS_DEFAULT,
)
from .statement_guard import StatementGuardError


class SnowflakePlugin(PluginBase, EdgeProcessProvider):
    """Read-only Snowflake warehouse query plugin."""

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
    # VaultKeysProvider — no plugin-owned keys (private key chain-consumed)
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required — the private key is chain-consumed.

        It lives in the address book RESOLVER's namespace
        (``<solet>.default_address_book_plugin.snowflake_private_key``)
        and is read only through ``resolve_with_secrets`` under the
        resolver's identity — never a direct vault verb under this plugin.
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
        exports on a live boot (2026-07-16).
        """
        if self.config_provider is None:
            raise SnowflakeConfigError(
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
                "address_book_service to resolve the Snowflake account"
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

    def _statement_timeout_seconds(self) -> int:
        """The session statement timeout (seconds) from plugin config — enforced positive."""
        config = self._config()
        raw = config.get(CONFIG_KEY_STATEMENT_TIMEOUT_SECONDS, STATEMENT_TIMEOUT_SECONDS_DEFAULT)
        try:
            timeout_s = int(str(raw))
        except ValueError as exc:
            raise SnowflakeConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_STATEMENT_TIMEOUT_SECONDS} must be a positive integer (got {raw!r})",
            ) from exc
        if timeout_s <= 0:
            raise SnowflakeConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_STATEMENT_TIMEOUT_SECONDS} must be > 0 (got {timeout_s}); a "
                "non-positive value would disable the statement-timeout DoS bound",
            )
        return timeout_s

    def _login_timeout_seconds(self) -> int:
        config = self._config()
        raw = config.get(CONFIG_KEY_LOGIN_TIMEOUT_SECONDS, LOGIN_TIMEOUT_SECONDS_DEFAULT)
        try:
            timeout_s = int(str(raw))
        except ValueError as exc:
            raise SnowflakeConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_LOGIN_TIMEOUT_SECONDS} must be a positive integer (got {raw!r})",
            ) from exc
        if timeout_s <= 0:
            raise SnowflakeConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_LOGIN_TIMEOUT_SECONDS} must be > 0 (got {timeout_s})",
            )
        return timeout_s

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
                raise SnowflakeConfigError(
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
        action: Callable[[Any], dict[str, Any]],
        verb: str,
    ) -> dict[str, Any]:
        """Resolve the account, open a hardened connection, run the action.

        Error classification is TOPOLOGY-SAFE: auth/connection/permission/
        timeout/warehouse classes return a generic fixed message; only the
        caller's-own-query/object classes carry driver detail
        (connection.classify_snowflake_error). Fresh connection per call.
        """
        if self._app_config_loader is None:
            return self._error(ERROR_NOT_CONFIGURED, f"{self.name} is not ready")
        conn: Any = None
        try:
            config = self._app_config_loader.resolve()
            conn = connection.connect(config, login_timeout_seconds=self._login_timeout_seconds())
            connection.apply_session_hardening(
                conn, statement_timeout_seconds=self._statement_timeout_seconds()
            )
            return self._success(action(conn))
        except Exception as exc:  # our coded guards + any driver fault -> typed
            code, message = self._classify_run_error(exc, verb)
            return self._error(code, message)
        finally:
            if conn is not None:
                conn.close()

    def _classify_run_error(self, exc: Exception, verb: str) -> tuple[str, str]:
        """Map a ``_run_on_connection`` exception to a typed, topology-safe (code, message)."""
        if isinstance(
            exc,
            (
                SnowflakeConfigError,
                StatementGuardError,
                export_containment.ExportPathRefusedError,
            ),
        ):
            return exc.code, str(exc)
        if isinstance(exc, ValueError):
            return ERROR_INVALID_PARAMS, str(exc)
        code, message = connection.classify_snowflake_error(exc)
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
            "list_databases": _edge(
                "list_databases", RESULT_TYPE_LIST_DATABASES),
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
        display_name="Snowflake: Run Query",
        description=(
            "Run ONE read-only SQL statement against the configured Snowflake account. Read "
            "leaders only (SELECT/SHOW/DESCRIBE/EXPLAIN/WITH); the operator-granted role is "
            "expected to be read-only. Returns immediately with a job_id and status 'queued' "
            "(D0.3 deferred-completion shape) — the dispatch returning is NOT the same as the "
            "job finishing. When the job completes, the result is ALWAYS written to the "
            "caller-supplied output_tsv_path, never returned inline — Snowflake's own Python "
            "connector documentation imposes no vendor row ceiling on a query result (arraysize/"
            f"client_prefetch_threads are client-side performance knobs, not caps); the limit "
            f"below is entirely our own policy. Defaults to {DEFAULT_ROW_LIMIT} rows to avoid "
            "exhausting warehouse compute and disk, and to discourage pulling all rows for "
            "client-side filtering that a WHERE clause should do instead. To fetch more, pass "
            "acknowledge_default_limit_override=true together with an explicit row_limit (up to "
            f"{MAX_ROWS_HARD_CAP}) — both are required together, and a row_limit above "
            f"{MAX_ROWS_HARD_CAP} is refused rather than silently clamped. For pulls beyond "
            f"{MAX_ROWS_HARD_CAP} rows, use export_query instead (same override mechanism, hard "
            f"cap {EXPORT_ROW_CAP}). When the goal is validating that records exist or picking "
            "one to act on next, prefer selecting stable ID columns over email addresses or "
            "other PII-bearing fields — the query decides what columns come back, so a narrower "
            "SELECT is both cheaper and lower-exposure."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
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
                    "exists: avoiding exhausted warehouse compute/disk, and pulling all rows to "
                    "filter client-side instead of writing a proper WHERE clause."
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
        name="list_databases",
        display_name="Snowflake: List Databases",
        description=(
            "List databases visible to the configured role. Returns immediately with a job_id "
            "and status 'queued' (D0.3 deferred-completion shape) — the dispatch returning is "
            "NOT the same as the job finishing; the database list itself is delivered when the "
            "job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        output_type="object",
        output_description="Job ID and status for async listing tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the database list itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_databases(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("list_databases", params, state)

    @platform_process(
        name="list_schemas",
        display_name="Snowflake: List Schemas",
        description=(
            "List schemas in a database. Requires database. Returns immediately with a job_id "
            "and status 'queued' (D0.3 deferred-completion shape) — the dispatch returning is "
            "NOT the same as the job finishing; the schema list itself is delivered when the job "
            "completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "database": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The database to list schemas from."
            ),
        },
        output_type="object",
        output_description="Job ID and status for async listing tracking.",
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
        display_name="Snowflake: List Tables",
        description=(
            "List tables in a database.schema. Requires database and schema. Returns immediately "
            "with a job_id and status 'queued' (D0.3 deferred-completion shape) — the dispatch "
            "returning is NOT the same as the job finishing; the table list itself is delivered "
            "when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "database": ParameterMetadata(type=ParameterType.STRING, required=True, description="The database."),
            "schema": ParameterMetadata(type=ParameterType.STRING, required=True, description="The schema."),
        },
        output_type="object",
        output_description="Job ID and status for async listing tracking.",
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
        display_name="Snowflake: Describe Table",
        description=(
            "Describe a table's columns (name, type, nullability, default). Requires database, "
            "schema, and table. Returns immediately with a job_id and status 'queued' (D0.3 "
            "deferred-completion shape) — the dispatch returning is NOT the same as the job "
            "finishing; the column list itself is delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "database": ParameterMetadata(type=ParameterType.STRING, required=True, description="The database."),
            "schema": ParameterMetadata(type=ParameterType.STRING, required=True, description="The table's schema."),
            "table": ParameterMetadata(type=ParameterType.STRING, required=True, description="The table name."),
        },
        output_type="object",
        output_description="Job ID and status for async introspection tracking.",
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
        display_name="Snowflake: Export Query",
        description=(
            "The N>>500 route: run a read-only query and write the result as ONE tab-separated "
            ".tsv file at an ABSOLUTE output_tsv_path in the operator's workspace. Returns "
            "immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — "
            "the dispatch returning is NOT the same as the job finishing. The path must lie "
            "under an operator-configured export_allowed_roots entry (empty config refuses every "
            "export). Same read-only rules and override mechanism as run_query, with a higher hard cap: "
            "Snowflake's own Python connector documentation imposes no vendor row ceiling, only "
            f"our own policy. Defaults to {DEFAULT_ROW_LIMIT} rows absent an acknowledged override "
            "— for that common small/default case, run_query has an identical interface with a "
            "lower ceiling. To fetch more, pass acknowledge_default_limit_override=true together "
            f"with an explicit row_limit (up to {EXPORT_ROW_CAP}) — both are required together, "
            f"and a row_limit above {EXPORT_ROW_CAP} is refused rather than silently clamped. "
            "Requires sql and output_tsv_path. When the goal is validating that records exist "
            "rather than inspecting their content, prefer selecting stable ID columns over email "
            "addresses or other PII-bearing fields — the query decides what columns land in the "
            "file."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sql": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="A single read-only SQL statement."
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
                    "exists: avoiding exhausted warehouse compute/disk, and pulling all rows to "
                    "filter client-side instead of writing a proper WHERE clause."
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
        display_name="Snowflake: Test Connection",
        description=(
            "Open the configured Snowflake account and confirm it: account, user, role, warehouse, "
            "and server version. Use this to verify the connector is reachable and the granted role "
            "is what you expect. Returns immediately with a job_id and status 'queued' (D0.3 "
            "deferred-completion shape) — the dispatch returning is NOT the same as the job "
            "finishing; the connection details are delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        output_type="object",
        output_description="Job ID and status for async connection-test tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the connection details themselves.",
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
        display_name="Snowflake: Run Statement",
        description=(
            "Run ONE SQL statement against the configured Snowflake account — INSERT/UPDATE/"
            "DELETE/MERGE/DDL/anything, not just reads. What the statement is actually allowed "
            "to do is decided entirely by the registered role's own server-side grants — this "
            "verb performs no read/write classification or permission check of its own (vendor "
            "RBAC is the control plane, operator ruling 2026-08-09 + Amendment 1). "
            "Single-statement is enforced natively by the Snowflake driver, not by this plugin "
            "(unlike external_postgres_plugin's run_statement, which reuses a shape guard) — "
            "no multi-statement scripts. A statement with no result set (the common INSERT/"
            "UPDATE/DELETE/DDL case) commits and returns rowcount inline. A statement that DOES "
            "produce a result set (e.g. a RETURNING clause, where the target object supports "
            "one) routes through the SAME always-TSV export path as run_query: rows are never "
            "returned inline, at any size, so output_tsv_path is then required — its absence "
            "rolls the whole statement back rather than silently discarding the returned rows "
            "while still committing the write. Returns immediately with a job_id and status "
            "'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same "
            "as the job finishing."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sql": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "A single SQL statement — any statement the registered role's own "
                    "server-side grants permit, not just reads."
                ),
            ),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=(
                    "ABSOLUTE .tsv destination path, contained under an export_allowed_roots "
                    "entry. Required ONLY if the statement produces a result set (e.g. a "
                    "RETURNING clause) — omit it for a plain INSERT/UPDATE/DELETE/DDL with no "
                    "result set, which returns rowcount inline instead."
                ),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    "Only relevant when the statement produces a result set: must be exactly "
                    f"true, together with row_limit, to fetch more than the default "
                    f"{DEFAULT_ROW_LIMIT} returned rows."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Only relevant when the statement produces a result set: explicit row "
                    f"ceiling, up to {MAX_ROWS_HARD_CAP}. Only honored together with "
                    "acknowledge_default_limit_override=true."
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
