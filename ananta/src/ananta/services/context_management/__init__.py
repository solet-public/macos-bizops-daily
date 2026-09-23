"""Context Management Service.

Model-agnostic context infrastructure for prompt continuity and cache optimization.

Public API:
- ContextManagementService: Main facade for context management
- ContextManagementConfig: Plugin configuration dataclass
- FileContextContentStorage: Plugin helper for file-based content storage

Types and constants:
- ContextMode, ContextEventType, ContextActorType, ContextCacheState
- ContextStatus, ContextType, ContextIdSource
- TABLE_* constants for table names

Compaction types (plugin-facing):
- CompactionPlan: Platform-internal plan for compaction execution
- CompactionRequest: Plugin-facing request for summary generation
- WarmingRequest: Plugin-facing request for cache warming
"""

from .compaction_types import CompactionPlan, CompactionRequest, WarmingRequest
from .config import ContextManagementConfig
from .content_storage import FileContextContentStorage
from .service import ContextManagementService
from .types import (
    CONTENT_EVENTS_SUBDIR,
    CONTENT_FILE_EXTENSION,
    CONTENT_FILENAME_PREFIX_EVENT,
    CONTENT_FILENAME_PREFIX_SNAPSHOT,
    CONTENT_SNAPSHOTS_SUBDIR,
    CONTENT_STORAGE_SUBDIR,
    CONTEXT_EVENT_TO_MESSAGE_ROLE,
    NAMESPACE,
    TABLE_CONTEXT_EVENTS,
    TABLE_CONTEXT_SESSIONS,
    TABLE_CONTEXT_SNAPSHOTS,
    TABLE_CONTEXT_STREAMS,
    ContextActorType,
    ContextCacheState,
    ContextEventType,
    ContextIdSource,
    ContextMode,
    ContextStatus,
    ContextType,
    MessageRole,
)

__all__ = [
    # Main service
    "ContextManagementService",
    # Config
    "ContextManagementConfig",
    # Content storage helper
    "FileContextContentStorage",
    # Compaction types (plugin-facing)
    "CompactionPlan",
    "CompactionRequest",
    "WarmingRequest",
    # Enums
    "ContextMode",
    "ContextEventType",
    "ContextActorType",
    "ContextCacheState",
    "ContextStatus",
    "ContextType",
    "ContextIdSource",
    "MessageRole",
    # Constants
    "NAMESPACE",
    "TABLE_CONTEXT_STREAMS",
    "TABLE_CONTEXT_EVENTS",
    "TABLE_CONTEXT_SESSIONS",
    "TABLE_CONTEXT_SNAPSHOTS",
    "CONTENT_STORAGE_SUBDIR",
    "CONTENT_EVENTS_SUBDIR",
    "CONTENT_SNAPSHOTS_SUBDIR",
    "CONTENT_FILE_EXTENSION",
    "CONTENT_FILENAME_PREFIX_EVENT",
    "CONTENT_FILENAME_PREFIX_SNAPSHOT",
    "CONTEXT_EVENT_TO_MESSAGE_ROLE",
]
