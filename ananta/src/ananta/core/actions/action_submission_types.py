"""
ActionFactory submission response types for proper separation of concerns.

This module defines standardized response types for action submission operations,
ensuring consistent behavior across the platform and eliminating the need for
plugins to handle platform-level concerns like service availability and queuing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ActionSubmissionStatus(Enum):
    """Status of action submission."""

    SUBMITTED = "submitted"
    QUEUED = "queued"
    DEFERRED = "deferred"
    FAILED = "failed"


@dataclass
class ActionSubmissionResponse:
    """Standardized response for action submission operations.

    This provides a consistent interface for plugins to handle submission results
    without needing to know about platform internals like service availability,
    queuing mechanisms, or retry logic.
    """

    success: bool
    action_id: str | None
    status: ActionSubmissionStatus
    message: str
    retry_after: int | None = None  # seconds until retry recommended
    details: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = {}


@dataclass
class QueuedAction:
    """Represents an action queued for later processing."""

    action_definition: dict[str, object]
    context: dict[str, object]
    timestamp: float
    retry_count: int = 0
    max_retries: int = 3

    @property
    def id(self) -> str:
        """Generate unique ID for queued action."""
        import hashlib
        import json

        # Create deterministic ID based on action content and timestamp
        content = json.dumps(self.action_definition, sort_keys=True) + str(self.timestamp)
        return f"queued_{hashlib.sha256(content.encode()).hexdigest()[:12]}"

    @property
    def should_retry(self) -> bool:
        """Check if action should be retried."""
        return self.retry_count < self.max_retries

    def increment_retry(self) -> None:
        """Increment retry count."""
        self.retry_count += 1
