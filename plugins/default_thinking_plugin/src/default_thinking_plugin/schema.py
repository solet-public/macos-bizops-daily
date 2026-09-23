"""Thinking plugin database schema definition.

Defines tables for task metadata and playbook lifecycle.
"""

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

NAMESPACE = "default_thinking_plugin"


def get_thinking_schema() -> SchemaDefinition:
    """Thinking plugin metadata schema.

    Tables:
    - thinking_task: Task metadata with context references
    - thinking_playbook: Playbook metadata and lifecycle
    - thinking_intake_state: Resolved Intake State metadata and lifecycle
    - thinking_manifest: Work Manifest metadata and lifecycle
    - thinking_brief: Complete Brief Form metadata and lifecycle
    - thinking_composition_design: Composition Design Document metadata and lifecycle
    - thinking_pipeline_spec: Pipeline Spec metadata and lifecycle
    - thinking_wbs: Work Breakdown Structure metadata and lifecycle
    - thinking_movement_design_packet: Movement Design Packet metadata
    - thinking_phrase_design_ledger: Phrase Design Ledger metadata
    - thinking_phrase_continuity: Per-phrase realized continuity state
    - thinking_authored_joseki: Authored-by-value joseki lifecycle + run evidence
    - thinking_joseki_run: Platform-driven joseki run state (run_joseki driver)
    """
    return SchemaDefinition(
        namespace=NAMESPACE,
        tables={
            "thinking_task": TableSchema(
                table_name="thinking_task",
                description="Thinking task metadata with context and memory references",
                id_prefix="thk",
                columns={
                    "title": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Human-readable task description",
                    ),
                    "task_type": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        check="task_type IN ('plan', 'analysis', 'deliberation')",
                        description="Task type: plan, analysis, or deliberation",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'paused', 'completed', 'abandoned')",
                        description="Task lifecycle status",
                    ),
                    "context_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Context stream ID for this task's thinking history",
                    ),
                    "memory_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Associated memory ID (for plans stored via remember())",
                    ),
                    "plan_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Plan document ID (pln- prefix) for knowledge base retrieval",
                    ),
                    "latest_response": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Most recent thinking model output",
                    ),
                },
                indexes=[
                    IndexDefinition("idx_thinking_task_type", ["task_type"]),
                    IndexDefinition("idx_thinking_task_status", ["status"]),
                ],
            ),
            "thinking_playbook": TableSchema(
                table_name="thinking_playbook",
                description="Playbook metadata and lifecycle tracking",
                id_prefix="pbk",
                columns={
                    "planning_context_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Context stream ID for the planning inference loop",
                    ),
                    "plan_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Currently linked plan (pln- prefix), nullable until planning completes",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'paused', 'completed', 'abandoned')",
                        description="Playbook lifecycle status",
                    ),
                    "title": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Short goal summary",
                    ),
                    "knowledge_base_path": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Path within thinking_playbooks knowledge base",
                    ),
                },
                indexes=[
                    IndexDefinition("idx_thinking_playbook_status", ["status"]),
                    IndexDefinition("idx_thinking_playbook_plan_id", ["plan_id"]),
                ],
            ),
            "thinking_intake_state": TableSchema(
                table_name="thinking_intake_state",
                description="Resolved Intake State metadata and lifecycle tracking",
                id_prefix="intk",
                columns={
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'superseded')",
                        description="Intake state lifecycle status",
                    ),
                    "knowledge_base_path": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Path within thinking_plans knowledge base",
                    ),
                },
                indexes=[
                    IndexDefinition("idx_thinking_intake_state_status", ["status"]),
                ],
            ),
            "thinking_manifest": TableSchema(
                table_name="thinking_manifest",
                description="Work Manifest metadata and lifecycle tracking",
                id_prefix="wmf",
                columns={
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'paused', 'completed', 'superseded')",
                        description="Manifest lifecycle status",
                    ),
                    "title": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Short effort summary",
                    ),
                    "knowledge_base_path": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Path within thinking_plans knowledge base",
                    ),
                },
                indexes=[
                    IndexDefinition("idx_thinking_manifest_status", ["status"]),
                ],
            ),
            "thinking_brief": TableSchema(
                table_name="thinking_brief",
                description="Complete Brief Form metadata and lifecycle tracking",
                id_prefix="cbf",
                columns={
                    "manifest_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=False,
                        default="",
                        description="Parent Work Manifest ID if created (empty before manifest)",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'locked', 'superseded')",
                        description="Brief lifecycle status",
                    ),
                    "knowledge_base_path": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Path within thinking_plans knowledge base",
                    ),
                },
                indexes=[
                    IndexDefinition(
                        "idx_thinking_brief_status", ["status"],
                    ),
                ],
            ),
            "thinking_composition_design": TableSchema(
                table_name="thinking_composition_design",
                description=(
                    "Composition Design Document metadata and lifecycle "
                    "tracking. The Composition Design Document is the "
                    "composer-pass artifact between the Work Manifest and "
                    "the Pipeline Spec / Composition Sketch. It commits "
                    "musical material, development grammar, formal "
                    "architecture, functional architecture, and "
                    "revision criteria as pure musical thought (no "
                    "process keys, arguments, or filenames)."
                ),
                id_prefix="cdg",
                columns={
                    "manifest_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Parent Work Manifest ID (wmf- prefix)",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'superseded')",
                        description="Composition Design Document lifecycle status",
                    ),
                    "knowledge_base_path": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Path within thinking_plans knowledge base",
                    ),
                },
                indexes=[
                    IndexDefinition(
                        "idx_thinking_composition_design_status", ["status"],
                    ),
                    IndexDefinition(
                        "idx_thinking_composition_design_manifest", ["manifest_id"],
                    ),
                ],
            ),
            "thinking_pipeline_spec": TableSchema(
                table_name="thinking_pipeline_spec",
                description=(
                    "Pipeline Spec metadata and lifecycle tracking. "
                    "Spec content lives in blob storage; this table "
                    "tracks the blob_id and namespace for retrieval."
                ),
                id_prefix="psp",
                columns={
                    "manifest_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Parent Work Manifest ID (wmf- prefix)",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'superseded')",
                        description="Pipeline Spec lifecycle status",
                    ),
                    "blob_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Blob storage ID containing the spec JSON",
                    ),
                    "blob_namespace": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="pipeline_specs",
                        description="Blob storage namespace",
                    ),
                },
                indexes=[
                    IndexDefinition(
                        "idx_thinking_pipeline_spec_status", ["status"],
                    ),
                    IndexDefinition(
                        "idx_thinking_pipeline_spec_manifest", ["manifest_id"],
                    ),
                ],
            ),
            "thinking_wbs": TableSchema(
                table_name="thinking_wbs",
                description="Work Breakdown Structure metadata and lifecycle tracking",
                id_prefix="wbs",
                columns={
                    "manifest_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Parent Work Manifest ID (wmf- prefix)",
                    ),
                    "phase_number": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        description="Phase number within the manifest",
                    ),
                    "phase_name": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Phase name",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="drafted",
                        check="status IN ('drafted', 'ready', 'in_progress', 'paused', 'completed', 'superseded')",
                        description="WBS lifecycle status",
                    ),
                    "knowledge_base_path": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Path within thinking_plans knowledge base",
                    ),
                    "work_products_data": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=False,
                        description="Serialized WorkProductRegister JSON for this WBS run",
                    ),
                    "provenance": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=False,
                        description=(
                            "Authoring provenance; 'authored_by_value' when an "
                            "agent authored the document and registered it by "
                            "value (Phase 3 Seam A); NULL for "
                            "thinking-model-authored documents"
                        ),
                    ),
                },
                indexes=[
                    IndexDefinition("idx_thinking_wbs_status", ["status"]),
                    IndexDefinition("idx_thinking_wbs_manifest", ["manifest_id"]),
                ],
            ),
            "thinking_wbs_outline": TableSchema(
                table_name="thinking_wbs_outline",
                description="WBS outline — skeleton structure for detailed WBS authoring",
                id_prefix="wbso",
                columns={
                    "wbs_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Parent WBS ID (wbs- prefix)",
                    ),
                    "manifest_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Parent Work Manifest ID (wmf- prefix)",
                    ),
                    "phase_number": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        description="Phase number within the manifest",
                    ),
                    "phase_name": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Phase name",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'superseded')",
                        description="Outline lifecycle status",
                    ),
                    "knowledge_base_path": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Path within thinking_plans knowledge base",
                    ),
                    "item_count": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        description="Number of work items in the outline",
                    ),
                    "item_ids_json": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="JSON array of work item IDs in outline order",
                    ),
                    "support_articles_json": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="JSON array of support article filenames used during outline creation",
                    ),
                    "source_memory_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Memory ID of the focused memory entry for this outline",
                    ),
                },
                indexes=[
                    IndexDefinition("idx_wbso_wbs", ["wbs_id"]),
                    IndexDefinition(
                        "idx_wbso_manifest_phase_status",
                        ["manifest_id", "phase_number", "status"],
                    ),
                    IndexDefinition("idx_wbso_status", ["status"]),
                ],
            ),
            "thinking_wbs_work_item_detail": TableSchema(
                table_name="thinking_wbs_work_item_detail",
                description="WBS work item detail — per-item detailed steps",
                id_prefix="wbsd",
                columns={
                    "outline_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Parent WBS outline ID (wbso- prefix)",
                    ),
                    "wbs_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Parent WBS ID (wbs- prefix)",
                    ),
                    "item_number": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        description="Work item number within the outline",
                    ),
                    "item_key": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Work item key/slug for identification",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'superseded')",
                        description="Detail lifecycle status",
                    ),
                    "knowledge_base_path": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Path within thinking_plans knowledge base",
                    ),
                    "source_memory_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Memory ID of the focused memory entry",
                    ),
                },
                indexes=[
                    IndexDefinition(
                        "idx_wbsd_outline_item", ["outline_id", "item_number"],
                    ),
                    IndexDefinition("idx_wbsd_wbs_status", ["wbs_id", "status"]),
                    IndexDefinition("idx_wbsd_status", ["status"]),
                ],
            ),
            "thinking_movement_design_packet": TableSchema(
                table_name="thinking_movement_design_packet",
                description="Movement Design Packet — movement-level thesis and formal argument",
                id_prefix="mdp",
                columns={
                    "manifest_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Parent Work Manifest ID (wmf- prefix)",
                    ),
                    "movement_type": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Movement type (toccata, allemande, sarabande, gigue, etc.)",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'superseded')",
                        description="Packet lifecycle status",
                    ),
                    "knowledge_base_path": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Path within thinking_plans knowledge base",
                    ),
                },
                indexes=[
                    IndexDefinition("idx_mdp_manifest", ["manifest_id"]),
                ],
            ),
            "thinking_phrase_design_ledger": TableSchema(
                table_name="thinking_phrase_design_ledger",
                description="Phrase Design Ledger — per-phrase continuity obligations",
                id_prefix="pdl",
                columns={
                    "manifest_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Parent Work Manifest ID (wmf- prefix)",
                    ),
                    "movement_type": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Movement type (toccata, allemande, sarabande, gigue, etc.)",
                    ),
                    "packet_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Parent Movement Design Packet ID (mdp- prefix)",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="active",
                        check="status IN ('active', 'superseded')",
                        description="Ledger lifecycle status",
                    ),
                    "knowledge_base_path": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Path within thinking_plans knowledge base",
                    ),
                },
                indexes=[
                    IndexDefinition("idx_pdl_manifest", ["manifest_id"]),
                    IndexDefinition("idx_pdl_packet", ["packet_id"]),
                ],
            ),
            "thinking_phrase_continuity": TableSchema(
                table_name="thinking_phrase_continuity",
                description="Per-phrase realized summary for cross-phrase continuity",
                id_prefix="pcn",
                columns={
                    "wbs_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Parent WBS record ID (wbs- prefix)",
                    ),
                    "movement_type": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Movement type (toccata, allemande, sarabande, gigue, etc.)",
                    ),
                    "entry_lh": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="LH starting pitch (e.g. D2)",
                    ),
                    "entry_rh": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="RH starting pitch (e.g. D4)",
                    ),
                    "exit_lh": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="LH ending pitch",
                    ),
                    "exit_rh": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="RH ending pitch",
                    ),
                    "realized_cadence": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Realized cadence type (none, half, authentic, phrygian, deceptive)",
                    ),
                    "realized_key": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Realized key at phrase end",
                    ),
                    "motif_hits": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        description="Number of motif occurrences detected",
                    ),
                    "continuation_abc": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Last 2 bars per voice as ABC text (local generation hint)",
                    ),
                    "summary_json": ColumnDefinition(
                        type=ColumnType.JSON,
                        description="Full phrase summary for detailed analysis",
                    ),
                },
                indexes=[
                    IndexDefinition("idx_pcn_wbs", ["wbs_id"]),
                ],
            ),
            "thinking_authored_joseki": TableSchema(
                table_name="thinking_authored_joseki",
                description=(
                    "Lifecycle and run evidence for agent-authored-by-value "
                    "joseki cards (Phase 3 Seam A). The card markdown in the "
                    "knowledge base is the source of truth (Q14); this table "
                    "carries the lifecycle state and, from Phase 6 onward, "
                    "run evidence."
                ),
                id_prefix="ajk",
                columns={
                    "joseki_key": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Stable JOSEKI_KEY from the card header (unique)",
                    ),
                    "state": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="draft",
                        check=(
                            "state IN ('draft', 'candidate', 'proven', "
                            "'superseded', 'archived')"
                        ),
                        description=(
                            "Lifecycle state; registration writes 'draft', "
                            "transitions are Phase 6"
                        ),
                    ),
                    "provenance": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Authoring provenance ('authored_by_value')",
                    ),
                    "knowledge_base_path": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description=(
                            "Card path within the authored_joseki knowledge "
                            "base (dedicated, semantically searchable)"
                        ),
                    ),
                    "superseded_by": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=False,
                        description=(
                            "JOSEKI_KEY of the replacing card when "
                            "state='superseded'; NULL otherwise"
                        ),
                    ),
                    "run_count": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=False,
                        description="Run-evidence counter (populated by Phase 6)",
                    ),
                    "last_run_at": ColumnDefinition(
                        type=ColumnType.DATETIME,
                        not_null=False,
                        description="Last run timestamp (populated by Phase 6)",
                    ),
                },
                indexes=[
                    IndexDefinition(
                        "idx_thinking_authored_joseki_key",
                        ["joseki_key"],
                        unique=True,
                    ),
                    IndexDefinition(
                        "idx_thinking_authored_joseki_state", ["state"],
                    ),
                ],
            ),
            "thinking_joseki_run": TableSchema(
                table_name="thinking_joseki_run",
                description=(
                    "Platform-driven joseki run state (the run_joseki "
                    "driver). One thin row per run: run-level status and "
                    "pointers ONLY — per-step state stays in the "
                    "instantiated WBS document (the pull-engine durable "
                    "substrate), so a driver crash re-derives everything "
                    "from the WBS. All status transitions are predicated "
                    "update_state CAS; a lost race is a benign no-op, "
                    "never an error (design: workbench/"
                    "2026-07-05_run_joseki_driver_design_spec.md §3)."
                ),
                id_prefix="jrun",
                columns={
                    "joseki_key": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="JOSEKI_KEY of the card this run executes",
                    ),
                    "wbs_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description=(
                            "Instantiated joseki-scoped WBS ID (wbs- prefix); "
                            "run evidence and step state key on this"
                        ),
                    ),
                    "session_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description=(
                            "Synthetic run-scoped session (label "
                            "joseki-run:<run_id>); strictly one run per session"
                        ),
                    ),
                    "flow_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description=(
                            "Synthetic run flow; the reconciler reads run "
                            "progress signals keyed on this flow via owning-"
                            "service read verbs (never foreign query_state)"
                        ),
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="running",
                        check=(
                            "status IN ('running', 'awaiting_user', "
                            "'completed', 'failed')"
                        ),
                        description=(
                            "Run status; deferred inference vertices are NOT "
                            "a status (projected by get_joseki_run instead)"
                        ),
                    ),
                    "current_step": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=False,
                        description=(
                            "Last step number the driver submitted; NULL "
                            "before the first submission (filter in code, "
                            "never equality-match NULL)"
                        ),
                    ),
                    "failure_detail": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="",
                        description=(
                            "Typed failure detail on status='failed' "
                            "(violation invariant or action error + repair-"
                            "joseki reference); '' sentinel otherwise"
                        ),
                    ),
                    "requester": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="",
                        description=(
                            "Call-context principal that started the run; "
                            "'' sentinel for cron/no-context kickoffs"
                        ),
                    ),
                    "label": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="",
                        description="Optional human label; '' sentinel",
                    ),
                    "attempts": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        default=0,
                        description=(
                            "Reconciler re-drive counter; cap → terminal "
                            "failed LOUD"
                        ),
                    ),
                },
                indexes=[
                    IndexDefinition(
                        "idx_thinking_joseki_run_status", ["status"],
                    ),
                    IndexDefinition(
                        "idx_thinking_joseki_run_key", ["joseki_key"],
                    ),
                    IndexDefinition(
                        "idx_thinking_joseki_run_wbs", ["wbs_id"],
                    ),
                ],
            ),
        },
    )
