"""Core domain constants for Ananta platform.

This module defines shared string literals and default values used across
the platform to avoid magic strings and ensure consistency.
"""

from typing import Final

from ananta.core.domain.enums import ActionStatus

# ============================================================================
# ACTION RESULT KEYS
# ============================================================================

KEY_ACTION_STATUS: Final[str] = "action_status"
KEY_DATA: Final[str] = "data"
KEY_RESULT: Final[str] = "result"
KEY_ERROR: Final[str] = "error"
KEY_COUNT: Final[str] = "count"
KEY_RESULTS: Final[str] = "results"
KEY_QUERY: Final[str] = "query"
KEY_NAMESPACES: Final[str] = "namespaces"
KEY_NAMESPACES_SEARCHED: Final[str] = "namespaces_searched"

# ============================================================================
# ACTION STATUS VALUES
# ============================================================================

STATUS_COMPLETED: Final[str] = ActionStatus.COMPLETED.value
STATUS_ERROR: Final[str] = ActionStatus.ERROR.value
STATUS_QUEUED: Final[str] = ActionStatus.QUEUED.value
STATUS_PROCESSING: Final[str] = ActionStatus.PROCESSING.value

# ============================================================================
# POSTGRESQL METADATA CONSTANTS
# ============================================================================

SCHEMA_INFORMATION_SCHEMA: Final[str] = "information_schema"
TABLE_TABLES: Final[str] = "tables"
COLUMN_TABLE_NAME: Final[str] = "table_name"
COLUMN_TABLE_SCHEMA: Final[str] = "table_schema"

# ============================================================================
# TABLE NAMING CONVENTIONS
# ============================================================================

TABLE_SUFFIX: Final[str] = "__embeddings"

# ============================================================================
# DEFAULT VALUES
# ============================================================================

DEFAULT_SEARCH_LIMIT: Final[int] = 10

# ============================================================================
# SESSION CONFIGURATION
# ============================================================================

# Session timeout in minutes (sessions expire after this period of inactivity)
SESSION_TIMEOUT_MINUTES: Final[int] = 90

# How often to run session cleanup (in minutes)
SESSION_CLEANUP_INTERVAL_MINUTES: Final[int] = 15
