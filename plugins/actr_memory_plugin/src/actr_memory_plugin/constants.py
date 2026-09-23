"""Constants for ACT-R Memory Plugin."""

from typing import Final

PLUGIN_NAME = "actr_memory_plugin"

# Export/import workspace-root containment (unified-memory-passthrough Slice 1(d),
# 2026-07-16). Mirrors the salesforce / external_postgres export_allowed_roots
# gate: the operator opts workspace roots in via plugin config, and the empty
# default REFUSES every export/import until they do.
CONFIG_KEY_EXPORT_ALLOWED_ROOTS: Final[str] = "export_allowed_roots"
ERROR_EXPORT_PATH_REFUSED: Final[str] = "memory.export_path_refused"

# Schedule intervals
MEMORIZATION_QUEUE_INTERVAL_HOURS = 6
STRENGTH_RECOMPUTE_INTERVAL_HOURS = 24
CONSOLIDATION_INTERVAL_HOURS = 168  # 7 days

# Cron expressions for scheduled operations
MEMORIZATION_QUEUE_CRON = "0 */6 * * *"  # Every 6 hours
STRENGTH_RECOMPUTE_CRON = "0 3 * * *"  # Daily at 3 AM
CONSOLIDATION_CRON = "0 4 * * 0"  # Weekly on Sunday at 4 AM


class ErrorCode:
    """Error codes for the plugin."""

    MEMORY_SERVICE_NOT_AVAILABLE = f"{PLUGIN_NAME}.memory_service_not_available"
    SCHEDULING_SERVICE_NOT_AVAILABLE = f"{PLUGIN_NAME}.scheduling_service_not_available"
    PARAMETER_ERROR = f"{PLUGIN_NAME}.parameter_error"
    OPERATION_FAILED = f"{PLUGIN_NAME}.operation_failed"
