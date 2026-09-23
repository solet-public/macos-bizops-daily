"""Constants for Default Thinking Plugin."""

PLUGIN_NAME = "default_thinking_plugin"

# Shipped-default system prompt (plugin-owned, ships in every seed that
# includes this plugin — unlike profile/config/, which genesis excludes).
# Lives under knowledge_base/prompts/, NOT under plans/**, manifests/**, or
# wbs/** (see knowledge_base/manifest.yaml's include patterns), so it is
# never ingested as a searchable KB article; it is a raw text asset this
# plugin's own code reads directly.
SHIPPED_SYSTEM_PROMPT_RELATIVE_PATH = "knowledge_base/prompts/thinking_system_prompt.md"

# Valid task types
TASK_TYPE_PLAN = "plan"
TASK_TYPE_ANALYSIS = "analysis"
TASK_TYPE_DELIBERATION = "deliberation"
VALID_TASK_TYPES = frozenset({TASK_TYPE_PLAN, TASK_TYPE_ANALYSIS, TASK_TYPE_DELIBERATION})

# Valid task statuses
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_ABANDONED = "abandoned"
VALID_STATUSES = frozenset({STATUS_ACTIVE, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ABANDONED})

# Knowledge base names
KB_THINKING_PLANS = "thinking_plans"
KB_THINKING_PLAYBOOKS = "thinking_playbooks"
# Dedicated SEMANTICALLY-SEARCHABLE home for authored-by-value joseki cards
# (Phase 3 Seam A). NOT thinking_plans: that KB sits in the knowledge
# plugin's SEARCH_EXCLUDED_KB_NAMES (its plan chunks pollute semantic
# search), which would make registered cards undiscoverable by the
# plain-English retrieval §4.3 requires.
KB_AUTHORED_JOSEKI = "authored_joseki"
# Dedicated SEMANTICALLY-SEARCHABLE home for plan-template cards (SUB-01,
# POR §4.5). A plan template is a reusable joseki-program skeleton
# searched like a joseki card; its curation-lifecycle state lives in the
# card's front-matter (there is NO state table — the deliberate §4.5
# asymmetry with the joseki lifecycle row). Same rationale as
# KB_AUTHORED_JOSEKI: NOT thinking_plans (that KB is search-excluded).
KB_PLAN_TEMPLATES = "plan_templates"

# ID prefixes for Work Manifest and Work Breakdown Structure documents
WBS_ID_PREFIX = "wbs-"
MANIFEST_ID_PREFIX = "wmf-"

# Provenance marker for agent-authored-by-value registrations (Phase 3 Seam A)
PROVENANCE_AUTHORED_BY_VALUE = "authored_by_value"

# Authored-joseki lifecycle states (Phase 6 §4.3). CANONICAL list — the
# ``thinking_authored_joseki.state`` CHECK constraint in ``schema.py`` mirrors
# these at the DB level; keep the two in lock-step. Registration writes
# ``draft``; ``candidate`` is validation-gated; ``proven`` is EARNED via a
# recorded successful run (never a manual transition); ``superseded`` points to
# a replacement; ``archived`` is retirement (terminal).
JOSEKI_STATE_DRAFT = "draft"
JOSEKI_STATE_CANDIDATE = "candidate"
JOSEKI_STATE_PROVEN = "proven"
JOSEKI_STATE_SUPERSEDED = "superseded"
JOSEKI_STATE_ARCHIVED = "archived"
JOSEKI_STATES = frozenset(
    {
        JOSEKI_STATE_DRAFT,
        JOSEKI_STATE_CANDIDATE,
        JOSEKI_STATE_PROVEN,
        JOSEKI_STATE_SUPERSEDED,
        JOSEKI_STATE_ARCHIVED,
    },
)

# Joseki-run statuses (the run_joseki driver). CANONICAL list — the
# ``thinking_joseki_run.status`` CHECK constraint in ``schema.py`` mirrors
# these; keep the two in lock-step. Deferred inference vertices are NOT a
# status (projected by get_joseki_run instead); ``completed``/``failed`` are
# terminal. Design: workbench/2026-07-05_run_joseki_driver_design_spec.md §3.
JOSEKI_RUN_STATUS_RUNNING = "running"
JOSEKI_RUN_STATUS_AWAITING_USER = "awaiting_user"
JOSEKI_RUN_STATUS_COMPLETED = "completed"
JOSEKI_RUN_STATUS_FAILED = "failed"


class ErrorCode:
    """Error codes for the plugin."""

    AUTHORED_CONTENT_REQUIRED = f"{PLUGIN_NAME}.authored_content_required"
    BACKEND_NOT_AVAILABLE = f"{PLUGIN_NAME}.backend_not_available"
    INFERENCE_UNUSABLE = f"{PLUGIN_NAME}.inference_unusable"
    PARAMETER_ERROR = f"{PLUGIN_NAME}.parameter_error"
    OPERATION_FAILED = f"{PLUGIN_NAME}.operation_failed"
    INTERNAL_ERROR = f"{PLUGIN_NAME}.internal_error"
    TASK_NOT_FOUND = f"{PLUGIN_NAME}.task_not_found"
    CONTEXT_SERVICES_MISSING = f"{PLUGIN_NAME}.context_services_missing"
    PLAYBOOK_NOT_FOUND = f"{PLUGIN_NAME}.playbook_not_found"
    SECTION_NOT_FOUND = f"{PLUGIN_NAME}.section_not_found"
    WBS_NOT_FOUND = f"{PLUGIN_NAME}.wbs_not_found"
    MANIFEST_NOT_FOUND = f"{PLUGIN_NAME}.manifest_not_found"
    JOSEKI_NOT_FOUND = f"{PLUGIN_NAME}.joseki_not_found"
    JOSEKI_STATE_CONFLICT = f"{PLUGIN_NAME}.joseki_state_conflict"
    JOSEKI_RUN_NOT_FOUND = f"{PLUGIN_NAME}.joseki_run_not_found"
    JOSEKI_RUN_WRITE_FAILED = f"{PLUGIN_NAME}.joseki_run_write_failed"
    JOSEKI_NOT_MECHANIZABLE = f"{PLUGIN_NAME}.joseki_not_mechanizable"
    JOSEKI_CARD_DEFECT = f"{PLUGIN_NAME}.joseki_card_defect"
    PLAN_TEMPLATE_NOT_FOUND = f"{PLUGIN_NAME}.plan_template_not_found"
    PLAN_TEMPLATE_STATE_CONFLICT = f"{PLUGIN_NAME}.plan_template_state_conflict"
