"""Durable schema for the InferenceService per-flow deferred-vertex queue.

One ``core``-namespace table, ``inference_deferred_vertex``, owned by
:class:`~ananta.services.inference_service.InferenceService` (a core service →
a ``core__*`` table). It replaces the in-memory ``OrderedDict`` register that
was keyed by role and pop-then-set with a 512-entry LRU — a shape that held
only the LAST deferred ``flow_id`` per role, so N flows deferred while a role
was absent lost N−1 (Phase-5 ◆R2 register; INF-01 §D.9 NO-LOSS split, Day-ruled:
INF-01 owns NO-LOSS, SUB-05 owns re-drive completeness).

**NO-LOSS via one row per ``flow_id``.** ``flow_id`` is a UNIQUE, NOT-NULL
idempotency key: a flow defers once (a re-defer of the same ``flow_id`` is an
idempotent upsert, not a new row), and N distinct flows deferred against the
same role retain all N rows — enumerable via ``query_state`` by ``role``. The
table is durable, so deferrals survive a restart.

Standard fields (``id``, ``created_at``, ``updated_at``, ``is_deleted`` …) are
auto-injected by the ``SchemaStandardizer`` and MUST NOT be declared here;
``created_at`` gives the FIFO drain order the sub-slice-2 vacancy-fill trigger
reads.

The DRAIN (query by role on first-claim + hard-delete drained rows) is INF-01
**sub-slice 2** (it is Trigger-1 lifecycle work). Sub-slice 1 declares the
table and writes/enumerates rows — the NO-LOSS half — only.
"""

from __future__ import annotations

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

# InferenceService is a core service → its durable table lives in ``core``.
INFERENCE_DEFERRED_VERTEX_NAMESPACE = "core"
TABLE_INFERENCE_DEFERRED_VERTEX = "inference_deferred_vertex"
ID_PREFIX_DEFERRED_VERTEX = "dfv"

COL_ROLE = "role"
COL_AGENT_INSTANCE_ID = "agent_instance_id"
COL_METHOD = "method"
COL_FLOW_ID = "flow_id"
# INF-06 reliability carve: a per-row lifecycle STATE + forward stamps. A row is
# ``deferred`` (bound-but-no-live-holder — the original NO-LOSS case) or
# ``forwarded`` (forwarded to a LIVE sys:autonomic holder that may die/time-out
# before self-executing). Both are the SAME entity — an un-consumed Surface-1
# vertex keyed by ``flow_id`` — re-driven by the SAME SUB-05 RESUBMIT primitive.
COL_STATE = "state"
COL_FORWARDED_AT = "forwarded_at"
COL_ATTEMPTS = "attempts"
# Standard auto-injected soft-delete column — filtered on to enumerate live
# rows (the queue hard-deletes on drain in sub-slice 2, so live == is_deleted 0).
COL_IS_DELETED = "is_deleted"

# The two vertex methods that route through the fault-degrade edges (the only
# values ``method`` ever takes) — shared with ``_record_deferred_vertex`` so the
# CHECK constraint and the writer cannot drift.
METHOD_PROCESS_ERROR = "process_error"
METHOD_PROCESS_RESULTS = "process_results"

# The two lifecycle states (CHECK-constrained). ``deferred`` = the row is awaiting
# a live holder (vacancy-fill drain re-drives). ``forwarded`` = the row was
# forwarded to a live holder and carries ``forwarded_at`` (the serve-timeout sweep
# re-drives if the holder never self-executes). A vacancy-fill re-drive of a
# ``forwarded`` row flips it back to ``deferred`` (nulls ``forwarded_at``,
# PRESERVES ``attempts`` — attempts is monotone per flow occupancy).
STATE_DEFERRED = "deferred"
STATE_FORWARDED = "forwarded"
# Terminal: the re-drive attempts cap was hit — a durable stall RECORD (vs
# today's stall-with-nothing). The sweep skips these; a GC sweep hard-deletes
# aged terminal rows so the queue never grows unbounded (§8-bis retention rider).
STATE_FAILED = "failed"


def get_inference_deferred_vertex_schema() -> SchemaDefinition:
    """Declarative schema for ``core__inference_deferred_vertex`` (state-interface).

    Registered via ``CoreSchemaDefinitions.get_all_core_schemas`` (deferred
    import, matching the session-ledger pattern) so it is created at startup.
    """
    return SchemaDefinition(
        namespace=INFERENCE_DEFERRED_VERTEX_NAMESPACE,
        tables={
            TABLE_INFERENCE_DEFERRED_VERTEX: TableSchema(
                table_name=TABLE_INFERENCE_DEFERRED_VERTEX,
                id_prefix=ID_PREFIX_DEFERRED_VERTEX,
                description=(
                    "INF-01 per-flow deferred inference vertices (NO-LOSS "
                    "durable queue). One row per flow_id; drained by role on "
                    "first-claim in sub-slice 2."
                ),
                columns={
                    COL_ROLE: ColumnDefinition(
                        type=ColumnType.TEXT,
                        description=(
                            "The durable role the deferred flow was bound to "
                            "(e.g. sys:autonomic), or NULL for a roleless "
                            "instance-bound deferral. The sub-slice-2 "
                            "vacancy-fill drain queries by this."
                        ),
                    ),
                    COL_AGENT_INSTANCE_ID: ColumnDefinition(
                        type=ColumnType.TEXT,
                        description=(
                            "The absent holder's agent_instance_id (roleless "
                            "deferrals + observability); NULL when unknown."
                        ),
                    ),
                    COL_METHOD: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        check=(
                            f"{COL_METHOD} IN "
                            f"('{METHOD_PROCESS_ERROR}', '{METHOD_PROCESS_RESULTS}')"
                        ),
                        description="The vertex method deferred.",
                    ),
                    COL_FLOW_ID: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description=(
                            "Idempotency + re-drive key. UNIQUE: a flow defers "
                            "once (re-defer = idempotent upsert); the "
                            "sub-slice-2 / SUB-05 re-drive RESUBMITs from this."
                        ),
                    ),
                    COL_STATE: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default=STATE_DEFERRED,
                        check=(
                            f"{COL_STATE} IN ('{STATE_DEFERRED}', "
                            f"'{STATE_FORWARDED}', '{STATE_FAILED}')"
                        ),
                        description=(
                            "Lifecycle state: 'deferred' (awaiting a live holder — "
                            "vacancy-fill drain re-drives), 'forwarded' (forwarded "
                            "to a live holder — serve-timeout sweep re-drives if it "
                            "never self-executes), or 'failed' (re-drive attempts "
                            "cap hit — durable stall record, GC-swept when aged)."
                        ),
                    ),
                    COL_FORWARDED_AT: ColumnDefinition(
                        type=ColumnType.DATETIME,
                        description=(
                            "ISO-8601 forward timestamp for a 'forwarded' row — "
                            "the serve-timeout sweep re-queues rows forwarded "
                            "before the cutoff. NULL for 'deferred' rows."
                        ),
                    ),
                    COL_ATTEMPTS: ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        default=0,
                        description=(
                            "Monotone re-drive counter (never reset across "
                            "forwarded<->deferred flips); incremented at "
                            "sweep-fire. At the cap the row terminal-fails "
                            "(durable stall record + loud log)."
                        ),
                    ),
                },
                indexes=[
                    IndexDefinition(
                        name="idx_inference_deferred_vertex_flow_id",
                        columns=[COL_FLOW_ID],
                        unique=True,
                    ),
                    IndexDefinition(
                        name="idx_inference_deferred_vertex_role",
                        columns=[COL_ROLE],
                    ),
                ],
            ),
        },
    )
