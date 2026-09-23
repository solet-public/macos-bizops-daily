"""Memory service database schema definition.

Defines relational tables for ACT-R long-term memory storage,
replacing the previous Key-Value storage approach.
"""

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

# Namespace for ACT-R memory tables
# Tables will be: actr_memory_plugin__memory, actr_memory_plugin__memorization
NAMESPACE = "actr_memory_plugin"


def get_memory_schema() -> SchemaDefinition:
    """ACT-R long-term memory schema.

    Tables:
    - memory: Episodic and semantic memories with ACT-R activation
    - memorization: Spaced repetition queue for memory reinforcement
    """
    return SchemaDefinition(
        namespace=NAMESPACE,
        tables={
            "memory": TableSchema(
                table_name="memory",
                description="Long-term episodic and semantic memories with ACT-R activation",
                id_prefix="mem",
                columns={
                    "content": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Memory content text",
                    ),
                    "memory_type": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="episodic",
                        check="memory_type IN ('episodic', 'semantic_l1', 'semantic_l2')",
                        description="Memory classification: episodic (raw), semantic_l1 (consolidated), semantic_l2 (highly abstracted)",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'archived')",
                        description="Memory lifecycle status",
                    ),
                    "strength": ColumnDefinition(
                        type=ColumnType.REAL,
                        default=0.0,
                        description="ACT-R activation strength (cached, recomputed periodically)",
                    ),
                    "retrieval_count": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        default=1,
                        description="Times this memory has been retrieved",
                    ),
                    "source_file": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Source file path if from document ingestion",
                    ),
                    "source_lines": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Line range in source file (e.g., '1-50')",
                    ),
                    "session_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Session that created this memory",
                    ),
                    "tags": ColumnDefinition(
                        type=ColumnType.TEXT,
                        default="[]",
                        description="JSON array of tags for organization",
                    ),
                    "retrieval_times": ColumnDefinition(
                        type=ColumnType.TEXT,
                        default="[]",
                        description="JSON array of ISO timestamps when memory was retrieved",
                    ),
                    "source_memory_ids": ColumnDefinition(
                        type=ColumnType.TEXT,
                        default="[]",
                        description="JSON array of source memory IDs (for consolidated memories)",
                    ),
                    # Standard fields auto-provided: id, external_id, name, created_at, updated_at
                },
                indexes=[
                    IndexDefinition("idx_memory_type", ["memory_type"]),
                    IndexDefinition("idx_memory_status", ["status"]),
                    IndexDefinition("idx_memory_strength", ["strength"]),
                    IndexDefinition("idx_memory_session_id", ["session_id"]),
                ],
            ),
            "memorization": TableSchema(
                table_name="memorization",
                description="Spaced repetition queue for memory reinforcement",
                id_prefix="mzn",
                columns={
                    "actr_memory_plugin__memory_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="FK to memory table (CASCADE per W5.P §3.3)",
                        foreign_key=("actr_memory_plugin__memory", "id"),
                        on_delete="CASCADE",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'paused', 'completed', 'orphaned')",
                        description="Memorization status",
                    ),
                    "review_count": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        default=0,
                        description="Number of reviews completed",
                    ),
                    "interval_days": ColumnDefinition(
                        type=ColumnType.REAL,
                        default=1.0,
                        description="Days until next review (increases with each review)",
                    ),
                    "started_at": ColumnDefinition(
                        type=ColumnType.DATETIME,
                        description="Timestamp when memorization started",
                    ),
                    "last_review_at": ColumnDefinition(
                        type=ColumnType.DATETIME,
                        description="Timestamp of last review",
                    ),
                    "next_review_at": ColumnDefinition(
                        type=ColumnType.DATETIME,
                        description="Timestamp of next scheduled review",
                    ),
                    # Standard fields auto-provided: id, external_id, name, created_at, updated_at
                },
                indexes=[
                    IndexDefinition(
                        "idx_memorization_memory_id", ["actr_memory_plugin__memory_id"]
                    ),
                    IndexDefinition("idx_memorization_status", ["status"]),
                    IndexDefinition("idx_memorization_next_review", ["next_review_at"]),
                ],
            ),
            "focus_buffer": TableSchema(
                table_name="focus_buffer",
                description="Per-session pinned memories for guaranteed context inclusion (budget-capped at prompt assembly)",
                id_prefix="foc",
                columns={
                    "memory_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        unique=True,
                        description="FK to memory table — the pinned memory (CASCADE per W5.P §3.3)",
                        foreign_key=("actr_memory_plugin__memory", "id"),
                        on_delete="CASCADE",
                    ),
                    "session_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        # Nullable at the DDL layer so schema adoption succeeds on
                        # pre-JOS-02 rows; every write path REQUIRES a session and
                        # boot raises on NULL rows (JOS-02 §6 — NOT-NULL tightening
                        # is a registered follow-up rev after the migration).
                        description="Owning session — focus is session-scoped (JOS-02)",
                    ),
                    # Standard fields auto-provided: id, created_at, updated_at
                },
                indexes=[
                    IndexDefinition("idx_focus_buffer_session", ["session_id"]),
                ],
            ),
        },
    )
