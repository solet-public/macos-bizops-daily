"""Configuration schema for the ACT-R memory plugin."""

from .constants import CONSOLIDATION_CRON, MEMORIZATION_QUEUE_CRON, STRENGTH_RECOMPUTE_CRON


def get_plugin_config_schema() -> dict[str, object]:
    """Return JSON Schema for plugin configuration."""
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "ACT-R Memory Plugin Configuration",
        "description": "Biologically-inspired memory system with decay, consolidation, and spaced repetition",
        "type": "object",
        "required": [],
        "properties": {
            "enable_scheduled_operations": {
                "type": "boolean",
                "title": "Enable Scheduled Operations",
                "description": "Enable automatic memory maintenance tasks (consolidation, strength recompute, memorization queue)",
                "default": True,
                "x-group": "advanced",
                "x-order": 1,
            },
            "memorization_queue_cron": {
                "type": "string",
                "title": "Memorization Queue Schedule",
                "description": "Cron expression for processing memorization queue (default: every 6 hours)",
                "default": MEMORIZATION_QUEUE_CRON,
                "x-group": "advanced",
                "x-order": 2,
            },
            "strength_recompute_cron": {
                "type": "string",
                "title": "Strength Recompute Schedule",
                "description": "Cron expression for recomputing memory activation strengths (default: daily at 3am)",
                "default": STRENGTH_RECOMPUTE_CRON,
                "x-group": "advanced",
                "x-order": 3,
            },
            "consolidation_cron": {
                "type": "string",
                "title": "Consolidation Schedule",
                "description": "Cron expression for consolidating weak episodic memories to semantic summaries (default: weekly on Sunday)",
                "default": CONSOLIDATION_CRON,
                "x-group": "advanced",
                "x-order": 4,
            },
            "export_allowed_roots": {
                "type": "array",
                "items": {"type": "string"},
                "title": "Export/Import Allowed Roots",
                "description": (
                    "Absolute workspace directories that export_memories / "
                    "import_memories file paths must be contained under "
                    "(realpath + commonpath containment). Empty (the default) "
                    "REFUSES every export and import until the operator opts a "
                    "root in — the same refuse-all default the connector "
                    "export_allowed_roots gates use."
                ),
                "default": [],
                "x-group": "advanced",
                "x-order": 5,
            },
        },
    }
