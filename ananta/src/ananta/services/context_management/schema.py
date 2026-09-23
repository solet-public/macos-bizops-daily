"""Schema definitions for context management tables.

All schemas define business fields only. Standard fields are injected automatically
by SchemaStandardizer (id, external_id, namespace, created_at, updated_at, etc.).
"""

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)


class ContextManagementSchemas:
    """Schema definitions for context management service."""

    @staticmethod
    def get_context_streams_schema() -> SchemaDefinition:
        """Schema for context streams.

        Purpose: Define context IDs independent of sessions/channels.
        Each context stream is an independent event log that can be
        shared across multiple models or kept model-specific.
        """
        return SchemaDefinition(
            namespace="core",
            tables={
                "context_streams": TableSchema(
                    table_name="context_streams",
                    id_prefix="ctx",
                    description="Context streams for model-agnostic prompt continuity",
                    columns={
                        "context_type": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="context_type IN ('homunculus', 'workflow', 'task', 'system')",
                            description="Type of context: solet (main), workflow, task, or system",
                        ),
                        "label": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Human-readable label for the context",
                        ),
                        "status": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="status IN ('active', 'paused', 'closed')",
                            description="Current status of the context stream",
                        ),
                        "metadata": ColumnDefinition(
                            type=ColumnType.JSON,
                            description="Additional context metadata as JSON",
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_context_streams_type", ["context_type"]),
                        IndexDefinition("idx_context_streams_status", ["status"]),
                    ],
                )
            },
        )

    @staticmethod
    def get_context_events_schema() -> SchemaDefinition:
        """Schema for context events.

        Purpose: Ordered ledger for a specific context.
        Content is stored in files; database holds metadata + content_path.
        """
        return SchemaDefinition(
            namespace="core",
            tables={
                "context_events": TableSchema(
                    table_name="context_events",
                    id_prefix="ctxe",
                    description="Ordered event ledger for context streams (file-only content)",
                    columns={
                        "context_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Foreign key to context_streams",
                        ),
                        "event_type": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="event_type IN ('input', 'output', 'observation', 'action', 'result', 'system')",
                            description="Type of context event",
                        ),
                        "actor_type": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="actor_type IN ('human', 'agent', 'service', 'system')",
                            description="Type of actor that generated this event",
                        ),
                        "actor_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Identifier of the specific actor (optional)",
                        ),
                        "content_path": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Relative path from APP_HOME to content file",
                        ),
                        "content_char_count": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            check="content_char_count >= 0",
                            description="Character count of content (for usage tracking)",
                        ),
                        "metadata": ColumnDefinition(
                            type=ColumnType.JSON,
                            description="Additional event metadata as JSON",
                        ),
                    },
                    indexes=[
                        IndexDefinition("idx_context_events_context_id", ["context_id"]),
                        IndexDefinition(
                            "idx_context_events_context_created_at",
                            ["context_id", "created_at"],
                        ),
                    ],
                )
            },
        )

    @staticmethod
    def get_context_sessions_schema() -> SchemaDefinition:
        """Schema for per-context tracking.

        Purpose: Track each context's state.
        Created on first use; stores cursor position and usage stats.
        """
        return SchemaDefinition(
            namespace="core",
            tables={
                "context_sessions": TableSchema(
                    table_name="context_sessions",
                    id_prefix="cs",
                    description="Per-context tracking and usage statistics",
                    columns={
                        "context_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Foreign key to context_streams",
                        ),
                        "provider": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Provider name (e.g., plugin name)",
                        ),
                        "context_mode": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="context_mode IN ('platform', 'delegated')",
                            description="How context is managed: platform stores events, delegated tracks session ID only",
                        ),
                        "backend_session_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Backend session ID for delegated context models",
                        ),
                        "last_event_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="ID of last processed event (cursor)",
                        ),
                        "last_event_created_at": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Timestamp of last processed event (cursor)",
                        ),
                        "cache_state": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            check="cache_state IN ('cold', 'warming', 'warm', 'expired')",
                            description="Current cache state for this context",
                        ),
                        "event_count": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            default=0,
                            description="Total events processed",
                        ),
                        "char_count": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            default=0,
                            description="Total characters processed",
                        ),
                        "input_tokens": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            description="Input tokens used (optional, if provider reports)",
                        ),
                        "output_tokens": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            description="Output tokens used (optional, if provider reports)",
                        ),
                        "total_tokens": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            description="Total tokens used (optional, if provider reports)",
                        ),
                        "active_snapshot_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Currently active snapshot ID (if any)",
                        ),
                    },
                    indexes=[
                        IndexDefinition(
                            "idx_context_sessions_context_id",
                            ["context_id"],
                            unique=True,
                        ),
                    ],
                )
            },
        )

    @staticmethod
    def get_context_snapshots_schema() -> SchemaDefinition:
        """Schema for context snapshots.

        Purpose: Store compacted summaries for platform-managed contexts.
        Summary content is stored in files; database holds metadata + summary_path.
        """
        return SchemaDefinition(
            namespace="core",
            tables={
                "context_snapshots": TableSchema(
                    table_name="context_snapshots",
                    id_prefix="cxs",
                    description="Compacted context summaries for platform-managed contexts",
                    columns={
                        "context_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Foreign key to context_streams",
                        ),
                        "start_event_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="First event ID included in this snapshot",
                        ),
                        "end_event_id": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Last event ID included in this snapshot",
                        ),
                        "summary_path": ColumnDefinition(
                            type=ColumnType.TEXT,
                            not_null=True,
                            description="Relative path from APP_HOME to summary file",
                        ),
                        "summary_char_count": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            check="summary_char_count >= 0",
                            description="Character count of summary",
                        ),
                        "original_char_count": ColumnDefinition(
                            type=ColumnType.INTEGER,
                            not_null=True,
                            check="original_char_count >= 0",
                            description="Original character count before compaction",
                        ),
                        "cache_key": ColumnDefinition(
                            type=ColumnType.TEXT,
                            description="Optional cache key for pre-warmed KV cache",
                        ),
                    },
                    indexes=[
                        IndexDefinition(
                            "idx_context_snapshots_context_id",
                            ["context_id"],
                        ),
                    ],
                )
            },
        )

    @staticmethod
    def get_all_context_schemas() -> list[SchemaDefinition]:
        """Get all context management schemas."""
        return [
            ContextManagementSchemas.get_context_streams_schema(),
            ContextManagementSchemas.get_context_events_schema(),
            ContextManagementSchemas.get_context_sessions_schema(),
            ContextManagementSchemas.get_context_snapshots_schema(),
        ]
