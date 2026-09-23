"""Context Event Store - Manage context event metadata.

Content storage is plugin-owned via FileContextContentStorage.
This service only manages database records with content_path references.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from ananta.core.domain.timestamps import to_naive_utc
from ananta.error_handling import FrameworkError

from .types import NAMESPACE, TABLE_CONTEXT_EVENTS, TABLE_CONTEXT_STREAMS, ContextEventType

if TYPE_CHECKING:
    from ananta.services.state_service import StateService


def _event_sort_key(record: dict[str, Any]) -> tuple[datetime, str]:
    """Composite ``(created_at_value, id)`` sort/cursor key for event ordering.

    Reproduces the migrated-away SQL ``ORDER BY created_at ASC, id ASC`` and the
    ``(created_at, id)`` row-value cursor. ``created_at`` is coerced to a
    naive-UTC datetime VALUE (not its ISO spelling) so equal instants with
    different spellings compare equal and fall through to the ``id`` tie-break —
    a lexical spelling compare would silently drop equal-instant rows at a page
    boundary. ``id`` is the total-order tie-break.
    """
    return (to_naive_utc(record["created_at"]), str(record["id"]))


class ContextEventStore:
    """Manage context event metadata. Content storage is plugin-owned."""

    def __init__(self, state_service: "StateService") -> None:
        """Initialize with state service dependency."""
        self._state_service = state_service

    def _verify_context_exists(self, context_id: str) -> None:
        """Verify context exists in context_streams table.

        FAIL-FAST: Prevents orphaned events from being stored to non-existent contexts.
        This catches bugs where context_ids are used without being registered.

        Args:
            context_id: The context ID to verify.

        Raises:
            FrameworkError: If context_id does not exist in context_streams.
        """
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={"table": TABLE_CONTEXT_STREAMS, "filters": {"id": context_id}},
        )
        data = cast(dict[str, Any], result.get("data", {}))
        records = cast(list[dict[str, Any]], data.get("records", []))

        if not records:
            raise FrameworkError(
                message=f"Context '{context_id}' not found in registry. "
                "Cannot store events to non-existent context.",
                error_code="context_event_store.context_not_found",
                details={"context_id": context_id},
            )

    def append_event(
        self,
        context_id: str,
        event_type: str,
        actor_type: str,
        content_path: str,
        content_char_count: int,
        actor_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append event metadata. Plugin must store content file first.

        FAIL-FAST: Verifies context exists in context_streams before writing.
        Prevents orphaned events from being stored to non-existent contexts.

        Args:
            context_id: Foreign key to context_streams (must exist)
            event_type: Type of event (input, output, observation, action, result, system)
            actor_type: Type of actor (human, agent, service, system)
            content_path: Relative path from APP_HOME to content file
            content_char_count: Character count of content
            actor_id: Optional identifier of specific actor
            metadata: Optional additional metadata

        Returns:
            The ActionResult from write_state.

        Raises:
            FrameworkError: If context_id does not exist in context_streams.
        """
        # FAIL-FAST: Verify context exists before storing events
        self._verify_context_exists(context_id)

        result = self._state_service.write_state(
            namespace=NAMESPACE,
            data={
                "table": TABLE_CONTEXT_EVENTS,
                "record": {
                    "context_id": context_id,
                    "event_type": event_type,
                    "actor_type": actor_type,
                    "actor_id": actor_id,
                    "content_path": content_path,
                    "content_char_count": content_char_count,
                    "metadata": metadata,
                },
            },
        )
        return dict(result)

    def get_event_by_id(self, event_id: str) -> dict[str, Any] | None:
        """Get event by ID.

        Args:
            event_id: The event ID to look up.

        Returns:
            Event record dict or None if not found.
        """
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={"table": TABLE_CONTEXT_EVENTS, "filters": {"id": event_id}},
        )
        data = cast(dict[str, Any], result.get("data", {}))
        records = cast(list[dict[str, Any]], data.get("records", []))
        return dict(records[0]) if records else None

    def get_event_created_at(self, event_id: str) -> str | None:
        """Get created_at for an event (for cursor updates).

        Args:
            event_id: The event ID to look up.

        Returns:
            ISO timestamp string or None if not found.
        """
        event = self.get_event_by_id(event_id)
        return str(event["created_at"]) if event else None

    def list_events_since(
        self,
        context_id: str,
        last_created_at: str | None = None,
        last_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List event metadata after cursor.

        Uses (created_at, id) for stable cursor pagination.
        Plugin reads content from content_path.

        Args:
            context_id: Context to query events for
            last_created_at: Cursor timestamp (exclusive)
            last_id: Cursor ID for tie-breaking
            limit: Maximum events to return

        Returns:
            List of event metadata dicts in chronological order.
        """
        # Unbounded/large ordered read with a row-value cursor → query_state
        # (uncapped) + Python sort/cursor/limit. query_ordered caps at 100 and
        # callers request up to 1000, so it would silently truncate; the
        # (created_at, id) cursor is a range compare the equality grammar cannot
        # express, so it is applied in Python. is_deleted=0 is passed explicitly
        # (query_state does not auto-exclude soft-deleted rows).
        records = self._live_events_for_context(context_id)
        records.sort(key=_event_sort_key)

        if last_created_at and last_id:
            cursor = (to_naive_utc(last_created_at), str(last_id))
            records = [
                record for record in records if _event_sort_key(record) > cursor
            ]

        return [dict(record) for record in records[:limit]]

    def _live_events_for_context(self, context_id: str) -> list[dict[str, Any]]:
        """Read all live (non-soft-deleted) event metadata rows for a context.

        Shared read seam for the ordered/range methods: a single uncapped
        ``query_state`` over one context, filtered/ordered in Python by the
        caller. The per-context live working set is the post-compaction tail.
        """
        result = self._state_service.query_state(
            namespace=NAMESPACE,
            filters={
                "table": TABLE_CONTEXT_EVENTS,
                "filters": {"context_id": context_id, "is_deleted": 0},
            },
        )
        data = cast(dict[str, Any], result.get("data", {}))
        return cast(list[dict[str, Any]], data.get("records", []))

    def list_all_events(self, context_id: str) -> list[dict[str, Any]]:
        """List all event metadata for context.

        Args:
            context_id: Context to query events for

        Returns:
            List of all event metadata dicts in chronological order.
        """
        # Unbounded ordered read → query_state (uncapped) + Python sort.
        records = self._live_events_for_context(context_id)
        records.sort(key=_event_sort_key)
        return [dict(record) for record in records]

    def has_system_events(self, context_id: str) -> bool:
        """Check if context has any SYSTEM events stored.

        Used to determine if system messages need to be stored on first request.

        Args:
            context_id: Context to check

        Returns:
            True if SYSTEM events exist, False otherwise.
        """
        # Existence check → bounded query_state (limit=1) + len(); a single-table
        # COUNT(*)>0 reduces to "is there at least one row".
        result = self._state_service.query_state(
            namespace=NAMESPACE,
            filters={
                "table": TABLE_CONTEXT_EVENTS,
                "filters": {
                    "context_id": context_id,
                    "event_type": ContextEventType.SYSTEM.value,
                    "is_deleted": 0,
                },
                "limit": 1,
            },
        )
        data = cast(dict[str, Any], result.get("data", {}))
        records = cast(list[dict[str, Any]], data.get("records", []))
        return len(records) > 0

    def list_events_after_snapshot(
        self,
        context_id: str,
        end_event_id: str,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """List events after a snapshot's end_event_id.

        Used for building prompt prefix: snapshot summary + events after.

        Args:
            context_id: Context to query events for
            end_event_id: Last event ID included in snapshot
            limit: Maximum events to return

        Returns:
            List of event metadata dicts after the snapshot.
        """
        # First get the end event to find its created_at
        end_event = self.get_event_by_id(end_event_id)
        if not end_event:
            return self.list_events_since(context_id, limit=limit)

        end_created_at = str(end_event["created_at"])
        return self.list_events_since(
            context_id,
            last_created_at=end_created_at,
            last_id=end_event_id,
            limit=limit,
        )

    def soft_delete_event(self, event_id: str) -> None:
        """Soft-delete event. Plugin must delete content file separately.

        Args:
            event_id: The event ID to soft-delete.
        """
        # delete_records soft-deletes by default (is_deleted=1); the table's
        # BEFORE-UPDATE trigger maintains updated_at.
        self._state_service.delete_records(
            namespace=NAMESPACE,
            query={"table": TABLE_CONTEXT_EVENTS, "filters": {"id": event_id}},
        )

    def soft_delete_events_before(
        self,
        context_id: str,
        end_event_id: str,
    ) -> int:
        """Soft-delete events up to and including end_event_id.

        Used during compaction to mark old events as deleted.
        Plugin must delete corresponding content files.

        Args:
            context_id: Context containing the events
            end_event_id: Delete events up to and including this ID

        Returns:
            Number of events deleted (approximate - based on pre-count).
        """
        # Get the end event's created_at for the query
        end_event = self.get_event_by_id(end_event_id)
        if not end_event:
            return 0

        end_cursor = (to_naive_utc(end_event["created_at"]), str(end_event_id))

        # Range predicate `(created_at, id) <= (end_created_at, end_event_id)` is
        # not expressible in the equality filter grammar → read-then-route: read
        # all live events for the context, Python-filter the in-range ids, then
        # soft-delete that id set in one =ANY delete_records call. The returned
        # count is the size of that set (the pre-count the old path reported).
        records = self._live_events_for_context(context_id)
        ids_to_delete = [
            str(record["id"])
            for record in records
            if _event_sort_key(record) <= end_cursor
        ]

        if ids_to_delete:
            self._state_service.delete_records(
                namespace=NAMESPACE,
                query={
                    "table": TABLE_CONTEXT_EVENTS,
                    "filters": {"id": ids_to_delete},
                },
            )

        return len(ids_to_delete)

    def get_process_keys_in_events_before(
        self,
        context_id: str,
        end_event_id: str,
    ) -> list[str]:
        """Get all process keys contained in events up to end_event_id.

        Used during summarization to identify which process descriptions
        will no longer be visible in context after truncation.

        Args:
            context_id: Context containing the events
            end_event_id: Get process keys up to and including this event

        Returns:
            List of unique process keys from event metadata.
        """
        end_event = self.get_event_by_id(end_event_id)
        if not end_event:
            return []

        end_cursor = (to_naive_utc(end_event["created_at"]), str(end_event_id))

        # Range read → read-then-route over the live events (same shape as
        # soft_delete_events_before). metadata is a JSONB column the provider
        # returns as a dict.
        records = self._live_events_for_context(context_id)

        all_keys: set[str] = set()
        for record in records:
            if _event_sort_key(record) > end_cursor:
                continue
            metadata = record.get("metadata")
            if isinstance(metadata, dict):
                keys = metadata.get("contains_process_keys")
                if isinstance(keys, list):
                    all_keys.update(str(k) for k in keys)

        return list(all_keys)
