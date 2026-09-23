"""Context Session Registry - Per-context tracking.

Manages context_sessions table: cursor position, usage stats,
and backend session IDs.
"""

from typing import TYPE_CHECKING, Any, cast

from .types import (
    NAMESPACE,
    TABLE_CONTEXT_SESSIONS,
    ContextCacheState,
)

if TYPE_CHECKING:
    from ananta.services.state_service import StateService


class ContextSessionRegistry:
    """Per-context tracking and usage statistics."""

    def __init__(self, state_service: "StateService") -> None:
        """Initialize with state service dependency."""
        self._state_service = state_service

    def ensure_session_exists(
        self,
        context_id: str,
        context_mode: str,
    ) -> None:
        """Ensure session record exists for context.

        Creates session with default values if it doesn't exist.
        Does NOT update existing sessions (preserves existing provider, etc.).

        FAIL-FAST: Raises if session creation fails.

        Note: event_count/char_count are not tracked per-event since limit
        checking uses fresh event counts. These columns stay at 0.

        Args:
            context_id: Foreign key to context_streams
            context_mode: How context is managed (platform or delegated)

        Raises:
            FrameworkError: If session creation fails.
        """
        from ananta.core.domain import ActionStatus, is_status_match
        from ananta.error_handling import FrameworkError
        from ananta.services.context_management.types import DEFAULT_SESSION_PROVIDER

        # Check if session already exists - do NOT overwrite existing
        existing = self.get_session(context_id)
        if existing:
            return  # Session exists, preserve its values

        # Create new session with default values
        result = self._state_service.write_state(
            namespace=NAMESPACE,
            data={
                "table": TABLE_CONTEXT_SESSIONS,
                "record": {
                    "context_id": context_id,
                    "provider": DEFAULT_SESSION_PROVIDER,
                    "context_mode": context_mode,
                    "cache_state": ContextCacheState.COLD.value,
                    "event_count": 0,
                    "char_count": 0,
                },
            },
        )

        # FAIL-FAST: Verify write succeeded (ActionResult uses action_status, not success)
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            raise FrameworkError(
                message="Failed to create context session",
                error_code="context_sessions.create_failed",
                details={
                    "context_id": context_id,
                    "error": result.get("error"),
                },
            )

    def get_or_create_session(
        self,
        context_id: str,
        provider: str,
        context_mode: str,
    ) -> dict[str, Any]:
        """Get or create session record.

        Creates on first use with default values.

        Args:
            context_id: Foreign key to context_streams
            provider: Provider name (e.g., plugin name)
            context_mode: How context is managed (platform or delegated)

        Returns:
            Session record dict.
        """
        result = self._state_service.upsert_state(
            namespace=NAMESPACE,
            data={
                "table": TABLE_CONTEXT_SESSIONS,
                "record": {
                    "context_id": context_id,
                    "provider": provider,
                    "context_mode": context_mode,
                    "cache_state": ContextCacheState.COLD.value,
                    "event_count": 0,
                    "char_count": 0,
                },
                "conflict_columns": ["context_id"],
            },
        )

        data = cast(dict[str, Any], result.get("data", {}))
        inner_result = cast(dict[str, Any], data.get("result", {}))
        generated_id = inner_result.get("generated_id")
        if generated_id:
            return self.get_session_by_id(str(generated_id)) or {}

        return self.get_session(context_id) or {}

    def get_session(
        self,
        context_id: str,
    ) -> dict[str, Any] | None:
        """Get session by context_id.

        Args:
            context_id: Foreign key to context_streams

        Returns:
            Session record dict or None if not found.
        """
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": TABLE_CONTEXT_SESSIONS,
                "filters": {
                    "context_id": context_id,
                },
            },
        )
        data = cast(dict[str, Any], result.get("data", {}))
        records = cast(list[dict[str, Any]], data.get("records", []))
        return dict(records[0]) if records else None

    def get_session_by_id(self, session_id: str) -> dict[str, Any] | None:
        """Get session by ID.

        Args:
            session_id: The session record ID.

        Returns:
            Session record dict or None if not found.
        """
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": TABLE_CONTEXT_SESSIONS,
                "filters": {"id": session_id},
            },
        )
        data = cast(dict[str, Any], result.get("data", {}))
        records = cast(list[dict[str, Any]], data.get("records", []))
        return dict(records[0]) if records else None

    def update_cursor(
        self,
        context_id: str,
        last_event_id: str,
        last_event_created_at: str,
    ) -> None:
        """Update cursor position after compaction.

        Note: event_count/char_count columns are not updated because:
        1. Limit checking uses fresh event counts computed from events table
        2. Without per-event increments, decrementing during compaction
           would cause negative counts (invalid state)
        3. These columns remain at their initial value (0)

        Args:
            context_id: Foreign key to context_streams
            last_event_id: ID of last processed event (for cursor tracking)
            last_event_created_at: Timestamp of last processed event
        """
        # updated_at is maintained by the table's BEFORE-UPDATE trigger (matches
        # the sibling update_* methods below — no explicit timestamp set needed).
        self._state_service.update_state(
            namespace=NAMESPACE,
            query={
                "table": TABLE_CONTEXT_SESSIONS,
                "filters": {"context_id": context_id},
            },
            updates={
                "last_event_id": last_event_id,
                "last_event_created_at": last_event_created_at,
            },
        )

    def update_backend_session(
        self,
        context_id: str,
        backend_session_id: str,
    ) -> None:
        """Update backend session ID for delegated context.

        Args:
            context_id: Foreign key to context_streams
            backend_session_id: Session ID from the backend model
        """
        self._state_service.update_state(
            namespace=NAMESPACE,
            query={
                "table": TABLE_CONTEXT_SESSIONS,
                "filters": {
                    "context_id": context_id,
                },
            },
            updates={"backend_session_id": backend_session_id},
        )

    def update_usage(
        self,
        context_id: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        """Update token usage if provided by model.

        Args:
            context_id: Foreign key to context_streams
            input_tokens: Input tokens used (optional)
            output_tokens: Output tokens used (optional)
            total_tokens: Total tokens used (optional)
        """
        updates: dict[str, Any] = {}
        if input_tokens is not None:
            updates["input_tokens"] = input_tokens
        if output_tokens is not None:
            updates["output_tokens"] = output_tokens
        if total_tokens is not None:
            updates["total_tokens"] = total_tokens

        if not updates:
            return

        self._state_service.update_state(
            namespace=NAMESPACE,
            query={
                "table": TABLE_CONTEXT_SESSIONS,
                "filters": {
                    "context_id": context_id,
                },
            },
            updates=updates,
        )

    def update_cache_state(
        self,
        context_id: str,
        cache_state: str,
    ) -> None:
        """Update cache state.

        Args:
            context_id: Foreign key to context_streams
            cache_state: New cache state (cold, warming, warm, expired)
        """
        self._state_service.update_state(
            namespace=NAMESPACE,
            query={
                "table": TABLE_CONTEXT_SESSIONS,
                "filters": {
                    "context_id": context_id,
                },
            },
            updates={"cache_state": cache_state},
        )

    def update_active_snapshot(
        self,
        context_id: str,
        snapshot_id: str | None,
    ) -> None:
        """Update active snapshot ID.

        Args:
            context_id: Foreign key to context_streams
            snapshot_id: Active snapshot ID or None to clear
        """
        self._state_service.update_state(
            namespace=NAMESPACE,
            query={
                "table": TABLE_CONTEXT_SESSIONS,
                "filters": {
                    "context_id": context_id,
                },
            },
            updates={"active_snapshot_id": snapshot_id},
        )

    def reset_counts(
        self,
        context_id: str,
    ) -> None:
        """Reset event and char counts after compaction.

        Args:
            context_id: Foreign key to context_streams
        """
        self._state_service.update_state(
            namespace=NAMESPACE,
            query={
                "table": TABLE_CONTEXT_SESSIONS,
                "filters": {
                    "context_id": context_id,
                },
            },
            updates={
                "event_count": 0,
                "char_count": 0,
                "last_event_id": None,
                "last_event_created_at": None,
            },
        )
