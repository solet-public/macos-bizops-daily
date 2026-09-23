"""Context Snapshot Store - Manage compacted context summaries.

Summary content is stored in files; database holds metadata + summary_path.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from ananta.core.domain.timestamps import to_naive_utc

from .types import NAMESPACE, TABLE_CONTEXT_SNAPSHOTS

if TYPE_CHECKING:
    from ananta.services.state_service import StateService


def _snapshot_sort_key(record: dict[str, Any]) -> tuple[datetime, str]:
    """Composite ``(created_at_value, id)`` sort key for snapshot ordering.

    ``created_at`` is coerced to a naive-UTC datetime VALUE (not its ISO
    spelling) so equal instants with different spellings order deterministically
    by the ``id`` tie-break rather than by spelling length; ``id`` is the
    deterministic tie-break (the migrated-away SQL ordered on ``created_at``
    alone, so a stable tie-break only strengthens determinism).
    """
    return (to_naive_utc(record["created_at"]), str(record["id"]))


class ContextSnapshotStore:
    """Manage context snapshot metadata. Summary storage is plugin-owned."""

    def __init__(self, state_service: "StateService") -> None:
        """Initialize with state service dependency."""
        self._state_service = state_service

    def create_snapshot(
        self,
        context_id: str,
        start_event_id: str,
        end_event_id: str,
        summary_path: str,
        summary_char_count: int,
        original_char_count: int,
        cache_key: str | None = None,
    ) -> str:
        """Create a new snapshot record.

        Plugin must store summary file first via FileContextContentStorage.

        Args:
            context_id: Foreign key to context_streams
            start_event_id: First event ID included in this snapshot
            end_event_id: Last event ID included in this snapshot
            summary_path: Relative path from APP_HOME to summary file
            summary_char_count: Character count of summary
            original_char_count: Original character count before compaction
            cache_key: Optional cache key for pre-warmed KV cache

        Returns:
            The generated snapshot_id.
        """
        result = self._state_service.write_state(
            namespace=NAMESPACE,
            data={
                "table": TABLE_CONTEXT_SNAPSHOTS,
                "record": {
                    "context_id": context_id,
                    "start_event_id": start_event_id,
                    "end_event_id": end_event_id,
                    "summary_path": summary_path,
                    "summary_char_count": summary_char_count,
                    "original_char_count": original_char_count,
                    "cache_key": cache_key,
                },
            },
        )
        data = cast(dict[str, Any], result.get("data", {}))
        inner_result = cast(dict[str, Any], data.get("result", {}))
        return str(inner_result["generated_id"])

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """Get snapshot by ID.

        Args:
            snapshot_id: The snapshot ID to look up.

        Returns:
            Snapshot record dict or None if not found.
        """
        result = self._state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": TABLE_CONTEXT_SNAPSHOTS,
                "filters": {"id": snapshot_id},
            },
        )
        data = cast(dict[str, Any], result.get("data", {}))
        records = cast(list[dict[str, Any]], data.get("records", []))
        return dict(records[0]) if records else None

    def get_latest_snapshot(
        self,
        context_id: str,
    ) -> dict[str, Any] | None:
        """Get latest snapshot for context.

        Args:
            context_id: Foreign key to context_streams

        Returns:
            Latest snapshot record dict or None if none exist.
        """
        # Bounded single-row "latest" read → query_ordered (LIMIT 1, within the
        # 100 cap). is_deleted=0 is applied by default (include_deleted=False).
        result = self._state_service.query_ordered(
            namespace=NAMESPACE,
            data={
                "table": TABLE_CONTEXT_SNAPSHOTS,
                "filters": {"context_id": context_id},
                "order_by": [["created_at", "desc"], ["id", "desc"]],
                "limit": 1,
            },
        )
        data = cast(dict[str, Any], result.get("data", {}))
        records = cast(list[dict[str, Any]], data.get("records", []))
        return dict(records[0]) if records else None

    def list_snapshots(
        self,
        context_id: str,
    ) -> list[dict[str, Any]]:
        """List all snapshots for context.

        Args:
            context_id: Foreign key to context_streams

        Returns:
            List of snapshot records in chronological order.
        """
        # Unbounded ordered read → query_state (uncapped) + Python sort.
        # query_ordered caps at 100 and would silently truncate; query_state does
        # NOT auto-exclude soft-deleted rows, so is_deleted=0 is passed explicitly.
        result = self._state_service.query_state(
            namespace=NAMESPACE,
            filters={
                "table": TABLE_CONTEXT_SNAPSHOTS,
                "filters": {"context_id": context_id, "is_deleted": 0},
            },
        )
        data = cast(dict[str, Any], result.get("data", {}))
        records = cast(list[dict[str, Any]], data.get("records", []))
        records.sort(key=_snapshot_sort_key)
        return [dict(record) for record in records]

    def soft_delete_snapshot(self, snapshot_id: str) -> None:
        """Soft-delete snapshot. Plugin must delete summary file separately.

        Args:
            snapshot_id: The snapshot ID to soft-delete.
        """
        # delete_records soft-deletes by default (is_deleted=1); the table's
        # BEFORE-UPDATE trigger maintains updated_at.
        self._state_service.delete_records(
            namespace=NAMESPACE,
            query={"table": TABLE_CONTEXT_SNAPSHOTS, "filters": {"id": snapshot_id}},
        )

    def soft_delete_older_snapshots(
        self,
        context_id: str,
        keep_snapshot_id: str,
    ) -> list[str]:
        """Soft-delete all snapshots older than keep_snapshot_id.

        Returns list of deleted snapshot IDs so plugin can clean up files.

        Args:
            context_id: Foreign key to context_streams
            keep_snapshot_id: Snapshot to keep (delete older ones)

        Returns:
            List of deleted snapshot IDs.
        """
        keep_snapshot = self.get_snapshot(keep_snapshot_id)
        if not keep_snapshot:
            return []

        keep_created_at = to_naive_utc(keep_snapshot["created_at"])

        # Range predicate (created_at < keep) is not expressible in the equality
        # filter grammar → read-then-route: read all live snapshots for the
        # context, Python-filter the strictly-older ones (by timestamp VALUE, not
        # ISO spelling), then soft-delete that id set in one =ANY delete_records
        # call.
        result = self._state_service.query_state(
            namespace=NAMESPACE,
            filters={
                "table": TABLE_CONTEXT_SNAPSHOTS,
                "filters": {"context_id": context_id, "is_deleted": 0},
            },
        )
        data = cast(dict[str, Any], result.get("data", {}))
        records = cast(list[dict[str, Any]], data.get("records", []))
        deleted_ids = [
            str(record["id"])
            for record in records
            if to_naive_utc(record["created_at"]) < keep_created_at
        ]

        if deleted_ids:
            self._state_service.delete_records(
                namespace=NAMESPACE,
                query={
                    "table": TABLE_CONTEXT_SNAPSHOTS,
                    "filters": {"id": deleted_ids},
                },
            )

        return deleted_ids
