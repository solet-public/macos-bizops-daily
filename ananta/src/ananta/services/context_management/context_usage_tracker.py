"""Context Usage Tracker - Track event and character counts.

Computes usage deltas for cursor updates. Token usage is optional
(only stored if provider reports it).
"""

from typing import Any


class ContextUsageTracker:
    """Compute usage deltas from event lists."""

    def compute_usage_delta(
        self,
        events: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Compute event count and char count from event list.

        Args:
            events: List of event metadata dicts with content_char_count

        Returns:
            Tuple of (event_count, char_count)
        """
        event_count = len(events)
        char_count = sum(int(e.get("content_char_count", 0)) for e in events)
        return event_count, char_count

    def compute_snapshot_compression_ratio(
        self,
        summary_char_count: int,
        original_char_count: int,
    ) -> float:
        """Compute compression ratio for a snapshot.

        Args:
            summary_char_count: Character count after compaction
            original_char_count: Character count before compaction

        Returns:
            Compression ratio (0.0 to 1.0, lower is better compression)
        """
        if original_char_count == 0:
            return 0.0
        return summary_char_count / original_char_count

    def should_compact(
        self,
        char_count: int,
        soft_max_char_count: int,
    ) -> bool:
        """Check if compaction should be triggered.

        Compaction is purely char-count based.

        Args:
            char_count: Current character count
            soft_max_char_count: Threshold for character count

        Returns:
            True if char_count exceeds soft_max_char_count
        """
        return char_count > soft_max_char_count
