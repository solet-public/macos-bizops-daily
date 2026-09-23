from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)


class CoreSchemaDefinitions:
    @staticmethod
    def get_job_schema() -> SchemaDefinition:
        """
        New unified job ledger schema with history support.
        Replaces core__asynchronous_jobs with richer metadata.
        """
        return SchemaDefinition(
            namespace="core",
            tables={
                "job": TableSchema(
                    table_name="job",
                    id_prefix="job",
                    description="Asynchronous work tracking for long-running tasks like image generation, API calls, and batch processing",
                    columns={
                        # Identity & provider
                        # Note: 'name' is provided by standard fields, defaults to ID if not set
                        "provider_type": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="provider_type IN ('plugin', 'service_interface', 'external_api')",
                            description="Type of provider handling this job: plugin, service_interface, or external_api",
                        ),
                        "provider_name": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Name of provider handling this job (e.g., 'default_image_generation_plugin')",
                        ),
                        # Presentation
                        "description": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Human-readable description of what this job is doing",
                        ),
                        "notes": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Additional notes or context about this job",
                        ),
                        # Lifecycle
                        "status": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            default="queued",
                            check="status IN ('queued', 'processing', 'completed', 'cancelled', 'error')",
                            description="Current job status: queued (waiting), processing (running), completed (success), cancelled (stopped), error (failed)",
                        ),
                        "status_reason": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Explanation of current status (especially useful for error/cancelled states)",
                        ),
                        "progress_percent": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            default=0,
                            description="Job completion percentage (0-100)",
                        ),
                        "expected_completion_at": ColumnDefinition(
                            type=ColumnType.DATETIME,
                            description="Estimated timestamp when job will complete",
                        ),
                        "completed_at": ColumnDefinition(
                            type=ColumnType.DATETIME,
                            description="Timestamp when job finished (completed, cancelled, or errored)",
                        ),
                        # Context
                        "group_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Group ID for related jobs (e.g., batch image generation)",
                        ),
                        "conversation_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Conversation/session ID that initiated this job",
                        ),
                        "metadata": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="JSON object with job-specific metadata and parameters",
                        ),
                        "flow_id_trace": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Flow ID for tracing all operations in this execution (inherited from action that created this job)",
                        ),
                        "flow_token_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="FRG token ID to resolve when job completes",
                        ),
                    },
                    indexes=[
                        IndexDefinition(
                            "idx_external_id",
                            ["provider_name", "external_id"],
                            unique=True,
                            where="external_id IS NOT NULL",
                        ),
                        IndexDefinition("idx_status", ["status"]),
                        IndexDefinition("idx_provider", ["provider_name"]),
                        IndexDefinition("idx_group_id", ["group_id"]),
                        IndexDefinition("idx_conversation_id", ["conversation_id"]),
                        IndexDefinition("idx_completed_at", ["completed_at"]),
                        IndexDefinition("idx_flow_id_trace", ["flow_id_trace"]),
                        IndexDefinition("idx_flow_token_id", ["flow_token_id"]),
                    ],
                    with_history=True,  # Enable automatic history snapshots
                )
            },
        )

    @staticmethod
    def get_job_payload_schema() -> SchemaDefinition:
        """
        Companion table for large payloads, kept separate from ledger.
        Allows multiple payload records per job (requests, results, errors).
        """
        return SchemaDefinition(
            namespace="core",
            tables={
                "job_payload": TableSchema(
                    table_name="job_payload",
                    id_prefix="jpl",
                    columns={
                        "job_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "payload_type": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="payload_type IN ('request', 'result', 'error')",
                        ),
                        "payload_data": ColumnDefinition(type=ColumnType.TEXT),
                        "sequence": ColumnDefinition(type=ColumnType.INTEGER),
                    },
                    indexes=[
                        IndexDefinition("idx_job_id", ["job_id"]),
                        IndexDefinition("idx_job_type", ["job_id", "payload_type"]),
                        IndexDefinition("idx_sequence", ["job_id", "sequence"]),
                    ],
                    with_history=False,  # Payload changes tracked via main job history
                )
            },
        )

    @staticmethod
    def get_asynchronous_jobs_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "asynchronous_jobs": TableSchema(
                    table_name="asynchronous_jobs",
                    id_prefix="ajb",
                    columns={
                        "external_id": ColumnDefinition(type=ColumnType.TEXT, unique=True),
                        "plugin_name": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "action_name": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "status": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            default="pending",
                            check="status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')",
                        ),
                        "priority": ColumnDefinition(type=ColumnType.INTEGER, default=100),
                        "request_data": ColumnDefinition(type=ColumnType.TEXT),
                        "result_data": ColumnDefinition(type=ColumnType.TEXT),
                        "error_message": ColumnDefinition(type=ColumnType.TEXT),
                    },
                    indexes=[
                        IndexDefinition("idx_status", ["status"]),
                        IndexDefinition("idx_plugin_action", ["plugin_name", "action_name"]),
                        IndexDefinition("idx_external_id", ["external_id"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_process_registry_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "process_registry": TableSchema(
                    table_name="process_registry",
                    id_prefix="proc",
                    description="Registry of all available processes (actions, services, plugins) exposed by the platform",
                    columns={
                        "provider_type": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="provider_type IN ('plugin', 'service_interface')",
                            description="Type of provider: 'plugin' for plugin functions, 'service_interface' for core services",
                        ),
                        "provider": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Name of the provider (e.g., 'claude_code_plugin', 'state_service')",
                        ),
                        "function_name": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Function name exposed by the provider (e.g., 'process_results', 'read_state')",
                        ),
                        "process_key": ColumnDefinition(
                            type=ColumnType.TEXT,
                            unique=True,
                            not_null=True,
                            description="Unique identifier for the process in format: provider_type::provider::function_name",
                        ),
                        "description": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Human-readable description of what this process does",
                        ),
                        # OLD COLUMNS (deprecated, kept for backward compatibility)
                        "parameter_schema": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="DEPRECATED: Legacy parameter schema (use input_contract instead)",
                        ),
                        "process_template": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="DEPRECATED: Legacy process template (use action_blueprint instead)",
                        ),
                        # NEW COLUMNS (Codex's design)
                        "input_contract": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="JSON schema defining expected input parameters and their types",
                        ),
                        "action_blueprint": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="Template defining how to construct actions from this process",
                        ),
                        # Context-specific documentation columns
                        "planning_docs": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="JSON documentation for action planning context: parameters, usage guidance, context handling, typical workflows",
                        ),
                        "error_handling_docs": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="JSON documentation for error handling context: error cases, recovery patterns, diagnostics",
                        ),
                        "response_handling_docs": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="JSON documentation for response handling context: success schemas, chaining guidance, result formatting",
                        ),
                        # Chaining guidance - controls LLM behavior after calling this action
                        "chaining_guidance": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="[]",
                            description="JSON array of guidance strings telling the LLM what to do after calling this action",
                        ),
                        # Invocation schema - JSON Schema defining how to invoke this process in a plan step
                        "invocation_schema": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="JSON Schema defining the exact structure for invoking this process in a plan step, including required arguments",
                        ),
                        # Action definition template - VERTEX processes provide this for edges to use
                        # When an EDGE completes, its customizations are merged into the VERTEX's
                        # action_definition_template to create the result/error processor action
                        "action_definition_template": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="Template for invoking this process. VERTEX inference processes provide this; EDGE processes reference it via customizations.",
                        ),
                        # Customizations for result/error processing (EDGE processes only)
                        # These get merged into the inference VERTEX's action_definition_template at runtime
                        "result_processor_customizations": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="JSON customizations merged into inference process's action_definition_template for result processing. Optional since the 2026-07-15 relax. Contains action_label, result_type, result_description, presentation_guidance, table_name.",
                        ),
                        "error_processor_customizations": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="JSON customizations merged into inference process's action_definition_template for error processing. Optional since the 2026-07-15 relax (presence required for deterministic_continuation verbs, §16). Contains action_context, error_interpretation, recovery_guidance, retryable flag.",
                        ),
                        # Existing flags
                        "is_inference_capable": ColumnDefinition(
                            type=ColumnType.BOOLEAN,
                            default=0,
                            description="Whether this process can perform LLM inference (1=yes, 0=no)",
                        ),
                        "is_enabled": ColumnDefinition(
                            type=ColumnType.BOOLEAN,
                            default=1,
                            description="Whether this process is currently enabled (1=yes, 0=no)",
                        ),
                        "work_count_impact": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            description="REQUIRED. Impact on flow's work_count when this process completes. +1 for edges (creates work), 0 for inference/status/post_message (neutral).",
                        ),
                        # Two-description architecture (Phase 2 refactor)
                        "embedding_description": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Keyword-dense description for semantic search (200-400 chars). Required for discoverable processes.",
                        ),
                        "is_discoverable": ColumnDefinition(
                            type=ColumnType.BOOLEAN,
                            default=1,
                            description="Whether this process appears in discovery results. Default True for plugins, False for service interfaces.",
                        ),
                        "display_name": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Human-readable display name for the process.",
                        ),
                        "is_long_running": ColumnDefinition(
                            type=ColumnType.BOOLEAN,
                            default=0,
                            description="LLM hint: process takes extended time. User should be notified before invocation.",
                        ),
                        "processor_policy_category": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="State machine category for processor policy enforcement (edge, vertex, edge_sink).",
                        ),
                        "include_in_system_prompt": ColumnDefinition(
                            type=ColumnType.BOOLEAN,
                            default=0,
                            description="Whether this process should be included in every system prompt. True for core service interfaces (blob storage, IO, memory, address book, discovery).",
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_provider_type", ["provider_type"]),
                        IndexDefinition("idx_provider", ["provider"]),
                        IndexDefinition("idx_function", ["function_name"]),
                        IndexDefinition("idx_enabled", ["is_enabled"]),
                        IndexDefinition("idx_process_key", ["process_key"]),
                        IndexDefinition("idx_discoverable", ["is_discoverable"]),
                        IndexDefinition("idx_include_in_system_prompt", ["include_in_system_prompt"]),
                    ],
                ),
                "action_definitions": TableSchema(
                    table_name="action_definitions",
                    id_prefix="ad",
                    columns={
                        "action_name": ColumnDefinition(
                            type=ColumnType.TEXT, unique=True, not_null=True
                        ),
                        "process_external_id": ColumnDefinition(
                            type=ColumnType.TEXT, not_null=True
                        ),
                        "description": ColumnDefinition(type=ColumnType.TEXT),
                        "default_parameters": ColumnDefinition(type=ColumnType.TEXT, default="{}"),
                        "is_enabled": ColumnDefinition(type=ColumnType.BOOLEAN, default=1),
                    },
                    indexes=[
                        IndexDefinition("idx_action_name", ["action_name"]),
                        IndexDefinition("idx_process_re", ["process_external_id"]),
                        IndexDefinition("idx_enabled", ["is_enabled"]),
                    ],
                ),
                "action_metrics": TableSchema(
                    table_name="action_metrics",
                    id_prefix="am",
                    columns={
                        "action_name": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "provider_type": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "provider": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "metric_type": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="metric_type IN ('execution_count', 'success_rate', 'avg_duration', 'error_count')",
                        ),
                        "metric_value": ColumnDefinition(type=ColumnType.REAL, not_null=True),
                        "period_start": ColumnDefinition(type=ColumnType.DATETIME, not_null=True),
                        "period_end": ColumnDefinition(type=ColumnType.DATETIME, not_null=True),
                    },
                    indexes=[
                        IndexDefinition("idx_action_type", ["action_name", "metric_type"]),
                        IndexDefinition("idx_period", ["period_start", "period_end"]),
                        IndexDefinition("idx_provider_metric", ["provider", "metric_type"]),
                    ],
                ),
            },
        )

    @staticmethod
    def get_schema_registry_schema() -> SchemaDefinition:
        """Schema registry table for storing all table/column definitions.

        This table is the authoritative source for schema information,
        eliminating the need for PRAGMA queries and enabling cross-database compatibility.
        """
        return SchemaDefinition(
            namespace="core",
            tables={
                "schema_registry": TableSchema(
                    table_name="schema_registry",
                    id_prefix="sr",
                    description="Registry of all table schemas and column definitions in the system",
                    columns={
                        "table_namespace": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Namespace of the table (e.g., 'core', 'plugin__my_plugin')",
                        ),
                        "table_name": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Name of the table without namespace prefix",
                        ),
                        "full_table_name": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Fully qualified table name (namespace__table)",
                        ),
                        "column_name": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Name of the column",
                        ),
                        "column_type": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="SQL column type (TEXT, INTEGER, REAL, BLOB, DATETIME, BOOLEAN)",
                        ),
                        "column_position": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            description="Ordinal position of column in table (0-indexed)",
                        ),
                        "is_primary_key": ColumnDefinition(
                            type=ColumnType.BOOLEAN,
                            default=0,
                            description="Whether this column is part of the primary key",
                        ),
                        "is_not_null": ColumnDefinition(
                            type=ColumnType.BOOLEAN,
                            default=0,
                            description="Whether this column has NOT NULL constraint",
                        ),
                        "is_unique": ColumnDefinition(
                            type=ColumnType.BOOLEAN,
                            default=0,
                            description="Whether this column has UNIQUE constraint",
                        ),
                        "default_value": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Default value for the column (as string)",
                        ),
                        "check_constraint": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="CHECK constraint expression if any",
                        ),
                        "is_standard_field": ColumnDefinition(
                            type=ColumnType.BOOLEAN,
                            default=0,
                            description="Whether this is a standard framework field (id, external_id, etc.)",
                        ),
                        "column_description": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Human-readable description of what this column stores",
                        ),
                        "schema_version": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            default=1,
                            description="Schema version number for migration tracking",
                        ),
                        "data_sensitivity": ColumnDefinition(
                            type=ColumnType.REAL,
                            default=1.0,
                            description="Data sensitivity rating (0.0=public, 1.0=restricted) — schema metadata",
                            data_sensitivity=0.0,  # This metadata field itself is public
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_full_table_name", ["full_table_name"]),
                        IndexDefinition(
                            "idx_table_column", ["full_table_name", "column_name"], unique=True
                        ),
                        IndexDefinition("idx_namespace_table", ["table_namespace", "table_name"]),
                    ],
                ),
            },
        )

    @staticmethod
    def get_key_value_store_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "key_value_store": TableSchema(
                    table_name="key_value_store",
                    id_prefix="kv",
                    columns={
                        "scope": ColumnDefinition(
                            type=ColumnType.TEXT, not_null=True, default="GLOBAL"
                        ),
                        "key": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "value": ColumnDefinition(type=ColumnType.TEXT),
                        "ttl": ColumnDefinition(type=ColumnType.INTEGER),
                        "expires_at": ColumnDefinition(type=ColumnType.DATETIME),
                    },
                    indexes=[
                        IndexDefinition(
                            "idx_namespace_scope_key", ["namespace", "scope", "key"], unique=True
                        ),
                        IndexDefinition("idx_namespace", ["namespace"]),
                        IndexDefinition("idx_expires", ["expires_at"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_logs_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "logs": TableSchema(
                    table_name="logs",
                    id_prefix="log",
                    description="System logging data capturing debug, info, warning, and error messages from all platform components",
                    columns={
                        "timestamp": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="ISO timestamp when log entry was created",
                        ),
                        "level": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Log severity level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
                        ),
                        "logger_name": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Name of the logger that created this entry",
                        ),
                        "plugin_name": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Name of plugin that generated the log (if applicable)",
                        ),
                        "message": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Log message content",
                        ),
                        "module": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Python module where log was generated",
                        ),
                        "function": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Function name where log was generated",
                        ),
                        "line_number": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            description="Line number in source code where log was generated",
                        ),
                        "thread_name": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Thread name that generated the log",
                        ),
                        "process_id": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            description="Process ID that generated the log",
                        ),
                        "exception_type": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Exception class name if log captured an exception",
                        ),
                        "exception_message": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Exception message if log captured an exception",
                        ),
                        "stack_trace": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Full stack trace if log captured an exception",
                        ),
                        "context_data": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Additional context data as JSON",
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_timestamp", ["timestamp"]),
                        IndexDefinition("idx_level", ["level"]),
                        IndexDefinition("idx_logger_name", ["logger_name"]),
                        IndexDefinition("idx_plugin_name", ["plugin_name"]),
                        IndexDefinition("idx_module", ["module"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_sessions_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "sessions": TableSchema(
                    table_name="sessions",
                    id_prefix="sess",
                    description="Namespace-bound conversation sessions with 90-minute timeout. Used for message routing.",
                    columns={
                        # NOTE: namespace is a standard field - uses platform default (TEXT NOT NULL)
                        "context_type": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="DEPRECATED: Use namespace instead. Kept for backward compatibility.",
                        ),
                        "metadata": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="JSON object containing session-specific metadata",
                        ),
                        "status": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="active",
                            check="status IN ('active', 'expired')",
                            description="Session status: active (valid) or expired (timed out after 90 min inactivity)",
                        ),
                        "expires_at": ColumnDefinition(
                            type=ColumnType.DATETIME,
                            description="Timestamp when session will expire (90 min from last_activity)",
                        ),
                        "started_at": ColumnDefinition(
                            type=ColumnType.DATETIME,
                            default="(NOW() AT TIME ZONE 'UTC')",
                            description="Timestamp when session was created",
                        ),
                        "last_activity": ColumnDefinition(
                            type=ColumnType.DATETIME,
                            default="(NOW() AT TIME ZONE 'UTC')",
                            description="Timestamp of most recent interaction; updated on every request to extend session",
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_namespace", ["namespace"]),
                        IndexDefinition("idx_status", ["status"]),
                        IndexDefinition("idx_expires_at", ["expires_at"]),
                        IndexDefinition("idx_last_activity", ["last_activity"]),
                        IndexDefinition("idx_namespace_status", ["namespace", "status"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_flows_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "flows": TableSchema(
                    table_name="flows",
                    id_prefix="flow",
                    columns={
                        "core__sessions_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "trigger_type": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "trigger_source": ColumnDefinition(type=ColumnType.TEXT),
                        "trigger_data": ColumnDefinition(type=ColumnType.TEXT, default="{}"),
                        "priority": ColumnDefinition(type=ColumnType.INTEGER, default=5),
                        "status": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="active",
                            check="status IN ('active', 'completed', 'failed', 'cancelled')",
                        ),
                        "work_count": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            default=0,
                            description="Running count of pending work. Incremented when actions queued, adjusted by process work_count_impact on completion. Flow completes when 0.",
                        ),
                        "duration_ms": ColumnDefinition(type=ColumnType.INTEGER),
                        "error_message": ColumnDefinition(type=ColumnType.TEXT),
                        "started_at": ColumnDefinition(
                            type=ColumnType.DATETIME, default="(NOW() AT TIME ZONE 'UTC')"
                        ),
                        "completed_at": ColumnDefinition(type=ColumnType.DATETIME),
                    },
                    indexes=[
                        IndexDefinition("idx_core__sessions_id", ["core__sessions_id"]),
                        IndexDefinition("idx_trigger_type", ["trigger_type"]),
                        IndexDefinition("idx_status", ["status"]),
                        IndexDefinition("idx_priority", ["priority"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_flow_tokens_schema() -> SchemaDefinition:
        """
        Flow Runtime Graph tokens for tracking outstanding work.

        Every piece of work (vertex inference, process invocation) gets an explicit token.
        Flow completes when token_count reaches zero.
        """
        return SchemaDefinition(
            namespace="core",
            tables={
                "flow_tokens": TableSchema(
                    table_name="flow_tokens",
                    id_prefix="ft",
                    description="FRG tokens tracking outstanding work units in a flow",
                    columns={
                        "core__flows_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Foreign key to flows table",
                        ),
                        "flow_id_trace": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Denormalized flow ID for fast queries",
                        ),
                        "owner_type": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="owner_type IN ('vertex', 'process', 'job')",
                            description="Type of work this token tracks",
                        ),
                        "owner_ref": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="ID of the owning entity (action_id, job_id, vertex_id)",
                        ),
                        "state": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            default="pending",
                            check="state IN ('pending', 'dispatched', 'waiting_job', 'completed', 'failed', 'cancelled', 'aborted')",
                            description="Current token state",
                        ),
                        "parent_token_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Parent token ID for hierarchy tracking (debugging)",
                        ),
                        "process_key": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Process key being executed (for process tokens)",
                        ),
                        "metadata": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="JSON payload with additional context",
                        ),
                        "result_summary": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="Compact completion info (success/error details)",
                        ),
                        "completed_at": ColumnDefinition(
                            type=ColumnType.DATETIME,
                            description="When token reached terminal state",
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_flow_id", ["core__flows_id"]),
                        IndexDefinition("idx_flow_id_trace", ["flow_id_trace"]),
                        IndexDefinition("idx_state", ["state"]),
                        IndexDefinition("idx_owner", ["owner_type", "owner_ref"]),
                        IndexDefinition("idx_parent_token", ["parent_token_id"]),
                        IndexDefinition("idx_pending_by_flow", ["flow_id_trace", "state"]),
                    ],
                    with_history=False,
                )
            },
        )

    @staticmethod
    def get_action_events_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "action_events": TableSchema(
                    table_name="action_events",
                    id_prefix="ae",
                    description="Execution history and status tracking for all actions submitted to the platform",
                    columns={
                        "core__sessions_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Foreign key to sessions table - session that created this action",
                        ),
                        "core__flows_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Foreign key to flows table - workflow that contains this action",
                        ),
                        "core__action_events_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Foreign key to parent action_event - creates action hierarchy for nested workflows",
                        ),
                        "context_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Platform context ID for context event correlation (ctx-...). Required for OUTPUT event storage in platform mode.",
                        ),
                        "sequence": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            description="Execution order within the flow (1, 2, 3...)",
                        ),
                        "depth": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            default=0,
                            description="Nesting depth for hierarchical workflows (0=top-level, 1=nested, etc.)",
                        ),
                        "name": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Unique action name (e.g., 'take_action_abc123xyz')",
                        ),
                        "process_key": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Process key from registry defining what this action executes",
                        ),
                        "parameters": ColumnDefinition(
                            type=ColumnType.TEXT,
                            default="{}",
                            description="JSON object containing runtime parameters for action execution",
                        ),
                        "notes": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Human-readable description of what this action is doing",
                        ),
                        "result_processor": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="JSON template defining follow-up actions to execute after this action completes",
                        ),
                        "result_processor_target": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Optional override: route result processor to this VERTEX process key instead of the default",
                        ),
                        "error_processor": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="JSON template defining follow-up actions to execute if this action fails",
                        ),
                        "result_processor_kind": ColumnDefinition(
                            type=ColumnType.TEXT,
                            check=(
                                "result_processor_kind IS NULL OR "
                                "result_processor_kind IN ("
                                "'inference', 'deterministic_continuation', "
                                "'bridge_delivery')"
                            ),
                            description=(
                                "Result-processor kind ('inference' / "
                                "'deterministic_continuation' / "
                                "'bridge_delivery').  NULL for EDGE_SINK "
                                "actions, direct user-input VERTEX actions, "
                                "and non-plan actions that don't dispatch.  "
                                "Required non-null on plan-derived EDGE "
                                "actions with process keys.  "
                                "'bridge_delivery' is platform-set on direct "
                                "MCP process_call invocations only."
                            ),
                        ),
                        "error_processor_kind": ColumnDefinition(
                            type=ColumnType.TEXT,
                            check=(
                                "error_processor_kind IS NULL OR "
                                "error_processor_kind IN ("
                                "'inference', 'bridge_delivery')"
                            ),
                            description=(
                                "Error-processor kind ('inference' / "
                                "'bridge_delivery').  Controls how an "
                                "execution failure or result-contract "
                                "violation is routed.  'inference' is the "
                                "default for plan-derived actions and the "
                                "historical default for direct invocations.  "
                                "'bridge_delivery' is platform-set on direct "
                                "MCP process_call invocations only and emits "
                                "a structured failure payload to the "
                                "originating bridge."
                            ),
                        ),
                        "status": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            default="queued",
                            check="status IN ('queued', 'processing', 'completed', 'failed')",
                            description="Current execution status: queued (waiting), processing (running), completed (success), failed (error)",
                        ),
                        "error_message": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Error details if action status is 'failed'",
                        ),
                        # Workflow compiler metadata (Phase 1)
                        "compiled_version": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Workflow compiler version used to validate this action (if compiled)",
                        ),
                        "validation_timestamp": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="ISO timestamp when action was validated by workflow compiler",
                        ),
                        # Processor policy metadata (Phase 1 - 2025-11-07)
                        "processor_depth": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            default=0,
                            description="Nesting level in processor chain (0=root action, 1=first processor, 2=nested processor)",
                        ),
                        "processor_origin": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Origin of result_processor: registry_default, llm_authored, runtime_generated, explicit_plan",
                        ),
                        "action_category": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Action category for processor policy: edge, vertex, edge_sink",
                        ),
                        "flow_id_trace": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Flow ID for tracing all operations in this execution (denormalized from core__flows_id for query performance)",
                        ),
                        "flow_token_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="FRG token ID tracking this action's completion",
                        ),
                        "job_result_ref": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Async job ID for post_message attachment routing — set by inference plugin, resolved by AQP",
                        ),
                        # Version-targeted dispatch (self-deployment plugin, addendum §K — 2026-05-30).
                        "excluded_versions": ColumnDefinition(
                            type=ColumnType.JSON,
                            description=(
                                "Optional JSON list of SOLET_VERSION values whose action "
                                "queue pollers must NOT claim this row. Used by the self-deployment "
                                "plugin to ensure the v(N)-side deploy_self enqueues complete_deploy "
                                "exclusively for v(N+1)'s poller; the v(N) container's poller filters "
                                "itself out via this column. NULL on all other actions (backward compatible)."
                            ),
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_core__sessions_id", ["core__sessions_id"]),
                        IndexDefinition("idx_core__flows_id", ["core__flows_id"]),
                        IndexDefinition("idx_core__action_events_id", ["core__action_events_id"]),
                        IndexDefinition("idx_context_id", ["context_id"]),
                        IndexDefinition("idx_sequence", ["core__flows_id", "sequence"]),
                        IndexDefinition("idx_name", ["core__flows_id", "name"]),
                        IndexDefinition("idx_status", ["status"]),
                        IndexDefinition("idx_flow_id_trace", ["flow_id_trace"]),
                        IndexDefinition("idx_flow_token_id", ["flow_token_id"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_orchestrator_state_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "orchestrator_state": TableSchema(
                    table_name="orchestrator_state",
                    id_prefix="orch",
                    columns={
                        "version": ColumnDefinition(type=ColumnType.INTEGER),
                        "actions": ColumnDefinition(type=ColumnType.TEXT),
                        "last_error": ColumnDefinition(type=ColumnType.TEXT),
                        "process_registry": ColumnDefinition(type=ColumnType.TEXT),
                    },
                    indexes=[
                        IndexDefinition("idx_version", ["version"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_workflow_patterns_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "workflow_patterns": TableSchema(
                    table_name="workflow_patterns",
                    id_prefix="wp",
                    columns={
                        "name": ColumnDefinition(type=ColumnType.TEXT, unique=True, not_null=True),
                        "description": ColumnDefinition(type=ColumnType.TEXT),
                        "category": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "steps": ColumnDefinition(type=ColumnType.TEXT, default="[]"),
                        "use_cases": ColumnDefinition(type=ColumnType.TEXT, default="[]"),
                        "success_indicators": ColumnDefinition(type=ColumnType.TEXT, default="[]"),
                        "failure_patterns": ColumnDefinition(type=ColumnType.TEXT, default="[]"),
                    },
                    indexes=[
                        IndexDefinition("idx_category", ["category"]),
                        IndexDefinition("idx_name", ["name"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_process_chains_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "process_chains": TableSchema(
                    table_name="process_chains",
                    id_prefix="pc",
                    columns={
                        "from_process_key": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "to_process_key": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "compatibility_score": ColumnDefinition(type=ColumnType.REAL, default=0.0),
                        "transformation_required": ColumnDefinition(
                            type=ColumnType.INTEGER, default=0
                        ),
                        "transformation_function": ColumnDefinition(type=ColumnType.TEXT),
                        "common_usage_count": ColumnDefinition(type=ColumnType.INTEGER, default=0),
                        "last_used_at": ColumnDefinition(type=ColumnType.DATETIME),
                    },
                    indexes=[
                        IndexDefinition("idx_from_key", ["from_process_key"]),
                        IndexDefinition("idx_to_key", ["to_process_key"]),
                        IndexDefinition("idx_compatibility", ["compatibility_score"]),
                        IndexDefinition("idx_usage", ["common_usage_count"]),
                        IndexDefinition(
                            "idx_unique_chain", ["from_process_key", "to_process_key"], unique=True
                        ),
                    ],
                )
            },
        )

    @staticmethod
    def get_event_bus_events_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "event_bus_events": TableSchema(
                    table_name="event_bus_events",
                    id_prefix="evt",
                    columns={
                        "event_id": ColumnDefinition(
                            type=ColumnType.TEXT, unique=True, not_null=True
                        ),
                        "event_type": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "source_plugin": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                        "target_plugin": ColumnDefinition(type=ColumnType.TEXT),
                        "correlation_id": ColumnDefinition(type=ColumnType.TEXT),
                        "priority": ColumnDefinition(type=ColumnType.INTEGER, default=5),
                        "action_data": ColumnDefinition(type=ColumnType.TEXT),
                        "response_data": ColumnDefinition(type=ColumnType.TEXT),
                        "error_info": ColumnDefinition(type=ColumnType.TEXT),
                        "metadata": ColumnDefinition(type=ColumnType.TEXT),
                        "last_read_at": ColumnDefinition(type=ColumnType.DATETIME),
                    },
                    indexes=[
                        IndexDefinition("idx_event_id", ["event_id"]),
                        IndexDefinition("idx_event_type", ["event_type"]),
                        IndexDefinition("idx_source_plugin", ["source_plugin"]),
                        IndexDefinition("idx_correlation_id", ["correlation_id"]),
                        IndexDefinition("idx_created_at", ["created_at"]),
                        IndexDefinition("idx_last_read_at", ["last_read_at"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_usage_stats_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "usage_stats": TableSchema(
                    table_name="usage_stats",
                    id_prefix="stat",
                    columns={
                        "process_key": ColumnDefinition(
                            type=ColumnType.TEXT, unique=True, not_null=True
                        ),
                        "total_executions": ColumnDefinition(type=ColumnType.INTEGER, default=0),
                        "last_used": ColumnDefinition(type=ColumnType.DATETIME),
                    },
                    indexes=[
                        IndexDefinition("idx_process_key", ["process_key"]),
                        IndexDefinition("idx_total_executions", ["total_executions"]),
                        IndexDefinition("idx_last_used", ["last_used"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_action_results_schema() -> SchemaDefinition:
        return SchemaDefinition(
            namespace="core",
            tables={
                "action_results": TableSchema(
                    table_name="action_results",
                    id_prefix="ar",
                    columns={
                        "core__action_events_id": ColumnDefinition(
                            type=ColumnType.TEXT, not_null=True
                        ),
                        "result_data": ColumnDefinition(type=ColumnType.TEXT),
                        "result_source": ColumnDefinition(type=ColumnType.TEXT),
                    },
                    indexes=[
                        IndexDefinition("idx_action_events_id", ["core__action_events_id"]),
                        IndexDefinition("idx_result_source", ["result_source"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_result_processing_violations_schema() -> SchemaDefinition:
        """Structured record of result-contract violations.

        Per handoff Section 11 — Contract-Violation Provenance Rules.
        A row here records a successful tool execution whose result
        violated the Joseki/WBS contract.  The originating action's
        status stays ``completed`` and its result row is preserved;
        recovery is owned by the process-level error handler action
        submitted in parallel.
        """
        return SchemaDefinition(
            namespace="core",
            tables={
                "result_processing_violations": TableSchema(
                    table_name="result_processing_violations",
                    id_prefix="rpv",
                    description=(
                        "Structured record of result-processing contract "
                        "violations raised by the validation gate; one row "
                        "per violation."
                    ),
                    columns={
                        "core__action_events_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description=(
                                "Foreign key to the completed action whose "
                                "result violated the contract"
                            ),
                        ),
                        "core__flows_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Foreign key to the originating flow",
                        ),
                        "core__sessions_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Foreign key to the originating session",
                        ),
                        "context_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Platform context ID for OUTPUT correlation",
                        ),
                        "process_key": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Process key of the completed action",
                        ),
                        "result_processor_kind": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check=(
                                "result_processor_kind IN ("
                                "'inference', 'deterministic_continuation', "
                                "'bridge_delivery')"
                            ),
                            description=(
                                "Step-level kind that produced the violation"
                            ),
                        ),
                        "invariant": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description=(
                                "Identifier of the failed invariant (see "
                                "ResultContractViolationDetails.invariant)"
                            ),
                        ),
                        "message": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description=(
                                "Human-readable invariant message; mirrors "
                                "FrameworkError.message on the violation"
                            ),
                        ),
                        "expected_json": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description=(
                                "Serialized expected payload from the "
                                "violation details"
                            ),
                        ),
                        "observed_json": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description=(
                                "Serialized observed payload from the "
                                "violation details"
                            ),
                        ),
                        # ``id`` and ``created_at`` are platform-managed
                        # standard fields; they are added automatically and
                        # must not be redeclared here.
                    },
                    indexes=[
                        IndexDefinition(
                            "idx_rpv_action_events_id",
                            ["core__action_events_id"],
                        ),
                        IndexDefinition(
                            "idx_rpv_flows_id", ["core__flows_id"],
                        ),
                        IndexDefinition(
                            "idx_rpv_invariant", ["invariant"],
                        ),
                    ],
                ),
            },
        )

    @staticmethod
    def get_testing_schemas() -> SchemaDefinition:
        """Test execution tracking schemas."""
        return SchemaDefinition(
            namespace="core",
            tables={
                "test_runs": TableSchema(
                    table_name="test_runs",
                    id_prefix="tr",
                    description="Test execution runs for tracking platform health",
                    columns={
                        "started_at": ColumnDefinition(
                            type=ColumnType.DATETIME,
                            not_null=True,
                            description="When test run started",
                        ),
                        "completed_at": ColumnDefinition(
                            type=ColumnType.DATETIME,
                            not_null=True,
                            description="When test run finished",
                        ),
                        "triggered_by": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="triggered_by IN ('ai', 'user', 'scheduled', 'ci')",
                            description="Who/what triggered the test run",
                        ),
                        "category": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Plugin/service category filter (null = all tests)",
                        ),
                        "total_tests": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            description="Total tests executed",
                        ),
                        "passed": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            description="Number of passing tests",
                        ),
                        "failed": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            description="Number of failing tests",
                        ),
                        "skipped": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            description="Number of skipped tests",
                        ),
                        "execution_time_ms": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            description="Total execution time in milliseconds",
                        ),
                        "report_format": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="report_format IN ('summary', 'detailed')",
                            description="Report format used",
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_test_runs_started_at", ["started_at"]),
                        IndexDefinition("idx_test_runs_category", ["category"]),
                        IndexDefinition("idx_test_runs_triggered_by", ["triggered_by"]),
                    ],
                ),
                "test_results": TableSchema(
                    table_name="test_results",
                    id_prefix="tres",
                    description="Individual test results linked to test runs",
                    columns={
                        "core__test_runs_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Foreign key to test_runs table",
                        ),
                        "test_name": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Test name from JSON file stem",
                        ),
                        "category": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Plugin/service category",
                        ),
                        "process_key": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Process key being tested",
                        ),
                        "status": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="status IN ('passed', 'failed', 'skipped')",
                            description="Test result status",
                        ),
                        "error_message": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Error message if test failed",
                        ),
                        "execution_time_ms": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            description="Test execution time in milliseconds",
                        ),
                        "core__action_events_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Links to actual action execution",
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_test_results_run_id", ["core__test_runs_id"]),
                        IndexDefinition("idx_test_results_status", ["status"]),
                        IndexDefinition("idx_test_results_process_key", ["process_key"]),
                        IndexDefinition("idx_test_results_category", ["category"]),
                    ],
                ),
            },
        )

    @staticmethod
    def get_memory_events_schema() -> SchemaDefinition:
        """Schema for memory service interaction events.

        Stores conversation history across all I/O interfaces (console, JSON-RPC,
        Telegram, REST, WebSocket) for cross-interface memory retrieval.
        """
        return SchemaDefinition(
            namespace="core",
            tables={
                "memory_events": TableSchema(
                    table_name="memory_events",
                    id_prefix="me",
                    description="Interaction history events for cross-interface conversation memory",
                    columns={
                        "session_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Session identifier for grouping related interactions",
                        ),
                        "source_namespace": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Plugin namespace that sent this event",
                        ),
                        "event_type": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="event_type IN ('user_input', 'assistant_response', 'system_message')",
                            description="Type of interaction event",
                        ),
                        "content": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Message content (user input or assistant response)",
                        ),
                        "metadata": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="JSON metadata for additional event context",
                        ),
                        "timestamp": ColumnDefinition(
                            type=ColumnType.DATETIME,
                            not_null=True,
                            description="ISO8601 timestamp when event occurred",
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_session_id", ["session_id"]),
                        IndexDefinition("idx_source_namespace", ["source_namespace"]),
                        IndexDefinition("idx_event_type", ["event_type"]),
                        IndexDefinition("idx_timestamp", ["timestamp"]),
                        IndexDefinition("idx_session_timestamp", ["session_id", "timestamp"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_plan_step_schema() -> SchemaDefinition:
        """Structured storage for execution plan steps.

        Replaces free-form text round-tripping with queryable rows.
        Each step belongs to a plan (pln- prefix) and owns zero or
        more sub-steps in ``core__plan_sub_step``.
        """
        return SchemaDefinition(
            namespace="core",
            tables={
                "plan_step": TableSchema(
                    table_name="plan_step",
                    id_prefix="pst",
                    description="Individual step within an execution plan",
                    columns={
                        "plan_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Parent plan ID (pln- prefix)",
                        ),
                        "step_number": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            description="Ordinal position within the plan",
                        ),
                        "marker": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            default=" ",
                            check="marker IN ('X', '>', ' ', '-')",
                            description="Step status: X=completed, >=current, ' '=pending, -=skip",
                        ),
                        "description": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Step title / description text",
                        ),
                        "guidance_article": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Knowledge base guidance article filename",
                        ),
                        "guidance_section_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Section within the guidance article",
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_plan_step_plan_id", ["plan_id"]),
                        IndexDefinition(
                            "idx_plan_step_plan_number",
                            ["plan_id", "step_number"],
                            unique=True,
                        ),
                    ],
                ),
                "plan_sub_step": TableSchema(
                    table_name="plan_sub_step",
                    id_prefix="pss",
                    description="Sub-step within a plan step",
                    columns={
                        "plan_step_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Parent plan step ID (pst- prefix)",
                        ),
                        "sort_order": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            description="Ordinal position within the step",
                        ),
                        "label": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Sub-step label (a, b, c, ...)",
                        ),
                        "description": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Sub-step description text",
                        ),
                        "process_key": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Process key referenced by this sub-step",
                        ),
                        "guidance_article": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Knowledge base guidance article filename",
                        ),
                        "guidance_section_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Section within the guidance article",
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_plan_sub_step_plan_step_id", ["plan_step_id"]),
                        IndexDefinition(
                            "idx_plan_sub_step_step_order",
                            ["plan_step_id", "sort_order"],
                            unique=True,
                        ),
                    ],
                ),
            },
        )

    @staticmethod
    def get_all_core_schemas() -> list[SchemaDefinition]:
        # Deferred imports to avoid circular imports with ananta.services / ananta.llm
        from ananta.llm.session_ledger.schema import (
            get_session_ledger_schema,
            get_session_ledger_summary_embeddings_schema,
        )
        from ananta.services.context_management.schema import ContextManagementSchemas
        from ananta.services.inference_service.completion_request_schema import (
            get_inference_completion_request_schema,
        )
        from ananta.services.inference_service.schema import (
            get_inference_deferred_vertex_schema,
        )

        return [
            CoreSchemaDefinitions.get_schema_registry_schema(),  # MUST be first - other schemas depend on it
            CoreSchemaDefinitions.get_job_schema(),
            CoreSchemaDefinitions.get_job_payload_schema(),
            CoreSchemaDefinitions.get_asynchronous_jobs_schema(),
            CoreSchemaDefinitions.get_process_registry_schema(),
            CoreSchemaDefinitions.get_key_value_store_schema(),
            CoreSchemaDefinitions.get_logs_schema(),
            CoreSchemaDefinitions.get_sessions_schema(),
            CoreSchemaDefinitions.get_flows_schema(),
            CoreSchemaDefinitions.get_flow_tokens_schema(),
            CoreSchemaDefinitions.get_action_events_schema(),
            CoreSchemaDefinitions.get_action_results_schema(),
            CoreSchemaDefinitions.get_result_processing_violations_schema(),
            CoreSchemaDefinitions.get_orchestrator_state_schema(),
            CoreSchemaDefinitions.get_workflow_patterns_schema(),
            CoreSchemaDefinitions.get_process_chains_schema(),
            CoreSchemaDefinitions.get_event_bus_events_schema(),
            CoreSchemaDefinitions.get_usage_stats_schema(),
            CoreSchemaDefinitions.get_testing_schemas(),
            CoreSchemaDefinitions.get_memory_events_schema(),
            CoreSchemaDefinitions.get_plan_step_schema(),
            # Context management schemas
            *ContextManagementSchemas.get_all_context_schemas(),
            # LLM session ledger (sources / sessions / events / quarantines / deployments)
            get_session_ledger_schema(),
            # M6 summary-vector store. Separate namespace so search_sessions
            # results aren't polluted by memory_service vectors that live
            # under the pgvector_service_plugin namespace.
            get_session_ledger_summary_embeddings_schema(),
            # INF-01 per-flow deferred-vertex NO-LOSS durable queue (replaces
            # the in-memory last-writer-per-role OrderedDict register).
            get_inference_deferred_vertex_schema(),
            # INF-02 autonomic-routed completion requests (durable
            # request/response queue; separate per-type table — the drain
            # shares the MECHANISM, never the deferred-vertex table).
            get_inference_completion_request_schema(),
        ]
