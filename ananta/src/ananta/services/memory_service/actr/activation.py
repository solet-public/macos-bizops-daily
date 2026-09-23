"""ACT-R Activation (Strength) Computation.

Implements the ACT-R base-level activation formula:

    B_i = ln(Σ t_j^(-d))

Where:
- B_i = base-level activation (strength) of memory i
- t_j = time since jth retrieval (in hours)
- d = decay factor (typically 0.5)

Key insight: Each retrieval adds a term to the sum, so memories that
are retrieved frequently AND recently have the highest activation.
"""

import math
from datetime import UTC, datetime
from typing import Any

from .constants import DECAY_FACTOR, MIN_AGE_HOURS


def compute_strength(
    memory: dict[str, Any],
    current_time: datetime | None = None,
    decay_factor: float = DECAY_FACTOR,
) -> float:
    """Compute ACT-R base-level activation for a memory.

    Args:
        memory: Memory dict with 'retrieval_times' and optionally 'created_at'
        current_time: Reference time for age calculation (default: now)
        decay_factor: ACT-R decay parameter (default: 0.5)

    Returns:
        Activation level (strength). Higher = more accessible.
        - 0.0 = just created (ln(1) = 0)
        - Positive = frequently/recently retrieved
        - Negative = old and rarely retrieved

    Example activation values:
        - Just created: ~0.0
        - Retrieved once yesterday: ~-1.7
        - Retrieved 5 times today: ~1.6
        - Retrieved once a week ago: ~-3.1
    """
    current_time = _ensure_tz_aware(current_time or datetime.now(UTC))
    retrieval_times = _get_retrieval_times(memory)

    if not retrieval_times:
        return -10.0

    total = _compute_decay_sum(retrieval_times, current_time, decay_factor)

    return math.log(total) if total > 0 else -10.0


def _ensure_tz_aware(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _get_retrieval_times(memory: dict[str, Any]) -> list[str | datetime]:
    """Get retrieval times from memory, falling back to created_at."""
    retrieval_times = memory.get("retrieval_times")
    if isinstance(retrieval_times, list) and retrieval_times:
        return list(retrieval_times)

    created_at = memory.get("created_at")
    if created_at is not None:
        return [created_at]
    return []


def _compute_decay_sum(
    retrieval_times: list[Any], current_time: datetime, decay_factor: float
) -> float:
    """Compute sum of decay factors for all retrieval times."""
    total = 0.0
    for retrieval_time_value in retrieval_times:
        parsed_time = _parse_retrieval_time(retrieval_time_value)
        if parsed_time is None:
            continue

        age_hours = _compute_age_hours(parsed_time, current_time)
        if age_hours > 0:
            total += age_hours ** (-decay_factor)

    return total


def _parse_retrieval_time(value: Any) -> datetime | None:
    """Parse retrieval time from string or datetime."""
    if isinstance(value, datetime):
        return _ensure_tz_aware(value)

    if isinstance(value, str):
        time_str = value.replace("Z", "+00:00")
        try:
            return _ensure_tz_aware(datetime.fromisoformat(time_str))
        except ValueError:
            return None

    return None


def _compute_age_hours(retrieval_time: datetime, current_time: datetime) -> float:
    """Compute age in hours, clamped to minimum."""
    age_seconds = (current_time - retrieval_time).total_seconds()
    return max(age_seconds / 3600, MIN_AGE_HOURS)


def sigmoid(x: float) -> float:
    """Sigmoid function to map strength to 0-1 range.

    Used for combining strength with similarity scores.
    """
    return 1.0 / (1.0 + math.exp(-x))


def compute_final_score(similarity: float, strength: float) -> float:
    """Combine vector similarity with ACT-R strength.

    Args:
        similarity: Cosine similarity from vector search (0-1)
        strength: ACT-R activation level (unbounded, typically -10 to +5)

    Returns:
        Combined score that balances semantic relevance with memory strength.
        Range is approximately 0-1.
    """
    # Map strength to 0-1 range using sigmoid
    strength_factor = sigmoid(strength)

    # Multiply similarity by strength factor
    # This means highly relevant but weak memories still score lower
    # than somewhat relevant but strong memories
    return similarity * strength_factor
