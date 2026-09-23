"""Types and constants for context management service.

No magic strings - use these enums and constants throughout the codebase.
"""

from enum import StrEnum


class ContextMode(StrEnum):
    """Context management mode for a model."""

    PLATFORM = "platform"
    DELEGATED = "delegated"


class ContextEventType(StrEnum):
    """Types of events in a context stream."""

    INPUT = "input"
    OUTPUT = "output"
    OBSERVATION = "observation"
    ACTION = "action"
    RESULT = "result"
    SYSTEM = "system"


class ContextActorType(StrEnum):
    """Types of actors that can generate context events."""

    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"
    SYSTEM = "system"


class ContextCacheState(StrEnum):
    """Cache state for a model's context."""

    COLD = "cold"
    WARMING = "warming"
    WARM = "warm"
    EXPIRED = "expired"


class ContextStatus(StrEnum):
    """Status of a context stream."""

    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


class ContextType(StrEnum):
    """Types of context streams."""

    HOMUNCULUS = "homunculus"
    WORKFLOW = "workflow"
    TASK = "task"
    SYSTEM = "system"


class ContextIdSource(StrEnum):
    """How context_id is resolved for a plugin."""

    EXPLICIT = "explicit"
    ADDRESS_BOOK = "address_book"
    PLUGIN_ROOT = "plugin_root"
    # REMOVED: ROOT - was an antipattern (shared solet context)
    # Use PLUGIN_ROOT instead - each plugin owns its context


class MessageRole(StrEnum):
    """Roles for conversation messages."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# Mapping from context event types to message roles
CONTEXT_EVENT_TO_MESSAGE_ROLE: dict[ContextEventType, MessageRole] = {
    ContextEventType.INPUT: MessageRole.USER,
    ContextEventType.OUTPUT: MessageRole.ASSISTANT,
    ContextEventType.SYSTEM: MessageRole.SYSTEM,
    ContextEventType.OBSERVATION: MessageRole.SYSTEM,
    ContextEventType.ACTION: MessageRole.ASSISTANT,
    ContextEventType.RESULT: MessageRole.SYSTEM,
}


# Table names (without namespace prefix)
TABLE_CONTEXT_STREAMS = "context_streams"
TABLE_CONTEXT_EVENTS = "context_events"
TABLE_CONTEXT_SESSIONS = "context_sessions"
TABLE_CONTEXT_SNAPSHOTS = "context_snapshots"

# REMOVED: KV_SOLET_CONTEXT_ID - solet root context was an antipattern
# Contexts are plugin-specific. Use get_or_create_plugin_root_context() instead.

# Content storage subdirectories under plugin data
CONTENT_STORAGE_SUBDIR = "context"
CONTENT_EVENTS_SUBDIR = "events"
CONTENT_SNAPSHOTS_SUBDIR = "snapshots"
CONTENT_FILE_EXTENSION = ".txt"
CONTENT_FILENAME_PREFIX_EVENT = "event"
CONTENT_FILENAME_PREFIX_SNAPSHOT = "snapshot"

# Namespace for context management tables
NAMESPACE = "core"

# Event types that carry immutable timestamps in prompt content
TIMESTAMPED_EVENT_TYPES: frozenset[ContextEventType] = frozenset({
    ContextEventType.INPUT,
    ContextEventType.OUTPUT,
})

# Timestamp labels by event type (no magic strings)
EVENT_TIMESTAMP_LABELS: dict[ContextEventType, str] = {
    ContextEventType.INPUT: "Received",
    ContextEventType.OUTPUT: "Sent",
}

# Default provider name for platform-managed sessions
DEFAULT_SESSION_PROVIDER = "platform"
