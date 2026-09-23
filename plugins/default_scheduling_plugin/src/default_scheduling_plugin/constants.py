from typing import Final

RELOAD_SAFE = True

PLUGIN_NAME: Final[str] = "default_scheduling_plugin"


class SchedulerErrorCode:
    UNKNOWN_ERROR: Final[str] = f"{PLUGIN_NAME}.unknown_error"
    PARAMETER_ERROR: Final[str] = f"{PLUGIN_NAME}.parameter_error"
    SCHEDULE_CREATE_ERROR: Final[str] = f"{PLUGIN_NAME}.schedule_create_error"
    SCHEDULE_UPDATE_ERROR: Final[str] = f"{PLUGIN_NAME}.schedule_update_error"
    SCHEDULE_DELETE_ERROR: Final[str] = f"{PLUGIN_NAME}.schedule_delete_error"
    SCHEDULE_NOT_FOUND: Final[str] = f"{PLUGIN_NAME}.schedule_not_found"
    PERSISTENCE_ERROR: Final[str] = f"{PLUGIN_NAME}.persistence_error"
    SCHEDULER_INITIALIZATION_ERROR: Final[str] = f"{PLUGIN_NAME}.scheduler_initialization_error"
    ALREADY_SCHEDULED: Final[str] = f"{PLUGIN_NAME}.already_scheduled"
    INVALID_CRON_EXPRESSION: Final[str] = f"{PLUGIN_NAME}.invalid_cron_expression"
    INVALID_SCHEDULE_ID: Final[str] = f"{PLUGIN_NAME}.invalid_schedule_id"
    INVALID_SCHEDULE_STATE: Final[str] = f"{PLUGIN_NAME}.invalid_schedule_state"
    THREAD_ERROR: Final[str] = f"{PLUGIN_NAME}.thread_error"
    SERIALIZATION_ERROR: Final[str] = f"{PLUGIN_NAME}.serialization_error"
    SCHEDULE_TIME_ERROR: Final[str] = f"{PLUGIN_NAME}.schedule_time_error"
    EXECUTION_ERROR: Final[str] = f"{PLUGIN_NAME}.execution_error"
    INVALID_PARAMETERS: Final[str] = f"{PLUGIN_NAME}.invalid_parameters"


class SchedulerJobStatus:
    SCHEDULED: Final[str] = "scheduled"
    RUNNING: Final[str] = "running"
    PAUSED: Final[str] = "paused"
    CANCELLED: Final[str] = "cancelled"
    COMPLETED: Final[str] = "completed"
    ERROR: Final[str] = "error"


SCHEDULER_ACTION_TYPES: Final[dict[str, str]] = {
    "schedule_one_time": "one_time",
    "schedule_recurring": "recurring",
    "schedule_cron": "cron",
    "schedule_conditional": "conditional",
    "cancel_scheduled": "cancel",
    "list_scheduled": "list",
    "pause_schedule": "pause",
    "resume_schedule": "resume",
}

DATA_FOLDER_NAME: Final[str] = f"plugin_data/{PLUGIN_NAME}"

# Memory-driven scheduling conventions
HEARTBEAT_TAG: Final[str] = "heartbeat:global"
DEFAULT_HEARTBEAT_CADENCE_MINUTES: Final[int] = 5

# Heartbeat schedules must not inherit the creating user's session/flow.
# Runtime components (scheduler callback, inference pipeline) expect valid IDs,
# so we use stable system-owned identifiers instead.
HEARTBEAT_SESSION_ID: Final[str] = "sess-heartbeat-global"
HEARTBEAT_FLOW_ID: Final[str] = "flow-heartbeat-global"

# KB retrieval-drift audit cron (SMIA Phase-0 baseline instrument).
#
# The predecessor trigger (`sch-2l18uk4q9et3a`, created 2026-06-01) used the
# memory-tag heartbeat shape, whose action_def carries
# ``result_processor_kind=None`` by construction; the action-queue poller's
# EDGE_SINK_SKIP branch terminates it with no dispatch, so the audit never ran
# — it fired daily for two months and executed zero times. The repaired target
# is the EDGE_SINK verb ``audit_retrieval_corpus_cron``, which returns a
# started/already-running receipt immediately and does the walk on a background
# daemon thread.
#
# Distinct session/flow constants (not the heartbeat pair) so audit fires are
# independently attributable in audit logs, matching the session_ledger
# periodic-schedule convention. They must be system-owned: a cron that inherits
# the creating session's id outlives that session and is left pointing at a
# dead one — which is what the predecessor did.
KB_RETRIEVAL_AUDIT_TAG: Final[str] = "kb_retrieval_audit:edge_sink"
KB_RETRIEVAL_AUDIT_CRON: Final[str] = "0 6 * * *"
KB_RETRIEVAL_AUDIT_LABEL: Final[str] = "KB retrieval drift audit (EDGE_SINK)"
KB_RETRIEVAL_AUDIT_SESSION_ID: Final[str] = "sess-kb-retrieval-audit"
KB_RETRIEVAL_AUDIT_FLOW_ID: Final[str] = "flow-kb-retrieval-audit"
KB_RETRIEVAL_AUDIT_PROCESS_KEY: Final[str] = (
    "service_interface::knowledge_service::audit_retrieval_corpus_cron"
)
