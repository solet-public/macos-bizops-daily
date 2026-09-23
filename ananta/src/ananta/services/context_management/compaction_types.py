"""Compaction types - platform internal and plugin-facing."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    """Platform-internal plan for compaction execution."""

    context_id: str
    reason: str
    start_event_id: str
    end_event_id: str
    messages_to_summarize: list[dict[str, str]]
    existing_summary: str | None
    summary_budget_chars: int
    summary_max_tokens: int
    summary_temperature: float
    compacted_event_count: int
    compacted_char_count: int
    remaining_event_count: int
    remaining_char_count: int
    last_kept_event_id: str
    last_kept_event_created_at: str
    recent_events_for_warming: list[dict[str, str]]


@dataclass(frozen=True, slots=True)
class CompactionRequest:
    """Plugin-facing request for summary generation."""

    context_id: str
    messages_to_summarize: list[dict[str, str]]
    existing_summary: str | None
    summary_budget_chars: int
    max_tokens: int
    temperature: float
    reason: str


@dataclass(frozen=True, slots=True)
class WarmingRequest:
    """Plugin-facing request for cache warming."""

    context_id: str
    snapshot_id: str
    messages: list[dict[str, str]]
    max_tokens: int
    temperature: float
