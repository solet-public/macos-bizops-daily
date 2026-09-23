"""Scheduling Service Public API.

AI-discoverable scheduling operations with @service_interface_process decorators.

Discoverability Policy (Task #47, 2026-05-24):
- EVERY method declares ``is_discoverable=True`` explicitly. The base decorator
  default for ``@service_interface_process`` is ``is_discoverable=False`` (service
  methods are presumed internal); scheduling operations are agent / user-callable
  (set a reminder, schedule a recurring task, cancel a schedule, ensure
  heartbeat), so the per-method flag overrides the default.
- Adding a new method without ``is_discoverable=True`` will SILENTLY exclude it
  from ``process_search`` and the agent will not be able to find it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.services.service_interface_decorator import service_interface_process


class SchedulingServiceAPI(ABC):
    """Public scheduling operations for the service interface."""

    _STATE_PARAM = ParameterMetadata(
        description="Runtime session context (auto-injected; do not set manually)",
        required=False,
        type=ParameterType.OBJECT,
    )

    @service_interface_process(
        name="create_cron_schedule",
        is_discoverable=True,
        provider="scheduling_service",
        parameters={
            "cron_expression": ParameterMetadata(
                description="Cron expression defining when wake-ups run (UTC).",
                required=True,
                type=ParameterType.STRING,
            ),
            "action_definitions": ParameterMetadata(
                description=(
                    "Non-empty list of syntactically valid action objects in the "
                    "canonical {process_key, arguments} shape. Registration validates "
                    "the shape and scheduled-action policy; execution resolves "
                    "process_key against processes registered at fire time. Provide "
                    "exactly one of action_definitions or memory_tag."
                ),
                required=False,
                type=ParameterType.LIST,
            ),
            "memory_tag": ParameterMetadata(
                description=(
                    "Memory tag to read on every run. The scheduled "
                    "get_memories_by_tag action is terminal: its result is written to "
                    "the action row and does not start a model turn. Provide exactly "
                    "one of memory_tag or action_definitions."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "label": ParameterMetadata(
                description="Optional human-readable label for the schedule.",
                required=False,
                type=ParameterType.STRING,
            ),
            "tags": ParameterMetadata(
                description="Optional tags for grouping schedules. When using memory_tag, the schedule is commonly tagged with memory_tag for cleanup.",
                required=False,
                type=ParameterType.LIST,
            ),
            "state": _STATE_PARAM,
        },
        return_value_schema=ReturnValueSchema(
            description="Cron schedule creation result.",
            type=ParameterType.OBJECT,
            properties={
                "schedule_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Unique schedule identifier."
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Human-readable confirmation message."
                ),
            },
            usage_patterns=[
                "Recurring async job monitoring.",
                "Periodic maintenance tasks.",
                "Scheduled notifications.",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def create_cron_schedule(
        self,
        cron_expression: str,
        actions: list[dict[str, Any]] | None = None,
        action_definitions: list[dict[str, Any]] | None = None,
        memory_tag: str | None = None,
        label: str | None = None,
        tags: list[str] | str | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="execute_in_seconds",
        is_discoverable=True,
        provider="scheduling_service",
        parameters={
            "seconds": ParameterMetadata(
                description="Delay in seconds before the wake-up fires (must be > 0).",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "action_definitions": ParameterMetadata(
                description=(
                    "Non-empty list of syntactically valid action objects in the "
                    "canonical {process_key, arguments} shape. Registration validates "
                    "the shape and scheduled-action policy; execution resolves "
                    "process_key against processes registered at fire time. Provide "
                    "exactly one of action_definitions or memory_tag."
                ),
                required=False,
                type=ParameterType.LIST,
            ),
            "memory_tag": ParameterMetadata(
                description=(
                    "Memory tag to read after the delay. The scheduled "
                    "get_memories_by_tag action is terminal: its result is written to "
                    "the action row and does not start a model turn. Provide exactly "
                    "one of memory_tag or action_definitions."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description=(
                    "Follow-up instructions to stash as a tagged memory. Content is "
                    "only valid with memory_tag; action-definition mode rejects it."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "label": ParameterMetadata(
                description="Optional human-readable label.",
                required=False,
                type=ParameterType.STRING,
            ),
            "tags": ParameterMetadata(
                description="Optional tags for identifying scheduled wake-ups. When using memory_tag, the schedule is commonly tagged with memory_tag for cleanup.",
                required=False,
                type=ParameterType.LIST,
            ),
            "state": _STATE_PARAM,
        },
        return_value_schema=ReturnValueSchema(
            description="Delayed execution schedule result.",
            type=ParameterType.OBJECT,
            properties={
                "schedule_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Unique schedule identifier."
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Confirmation message describing the scheduled delay.",
                ),
                "run_at": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="ISO8601 timestamp when actions will run.",
                ),
            },
            usage_patterns=[
                "Pattern 6a async monitoring delays.",
                "Timeout/retry orchestration.",
                "Delayed notifications.",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def execute_in_seconds(
        self,
        seconds: int,
        actions: list[dict[str, Any]] | None = None,
        action_definitions: list[dict[str, Any]] | None = None,
        memory_tag: str | None = None,
        content: str | None = None,
        label: str | None = None,
        tags: list[str] | str | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="clear_scheduled_action",
        is_discoverable=True,
        provider="scheduling_service",
        parameters={
            "schedule_id": ParameterMetadata(
                description="Identifier of the schedule to cancel.",
                required=True,
                type=ParameterType.STRING,
            ),
            "state": _STATE_PARAM,
        },
        return_value_schema=ReturnValueSchema(
            description="Schedule cancellation result.",
            type=ParameterType.OBJECT,
            properties={
                "schedule_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Cancelled schedule identifier."
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status message describing the cancellation.",
                ),
            },
            usage_patterns=[
                "Cancel pending async monitoring checks once a job completes.",
                "User-requested cancellation of scheduled tasks.",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def clear_scheduled_action(
        self, schedule_id: str, state: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="clear_scheduled_actions_by_tag",
        is_discoverable=True,
        provider="scheduling_service",
        parameters={
            "tag": ParameterMetadata(
                description="Tag used when the schedules were created.",
                required=True,
                type=ParameterType.STRING,
            ),
            "state": _STATE_PARAM,
        },
        return_value_schema=ReturnValueSchema(
            description="Bulk tag cancellation result.",
            type=ParameterType.OBJECT,
            properties={
                "cleared_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of schedules cancelled."
                ),
                "tag": ParameterMetadata(
                    type=ParameterType.STRING, description="Tag that was cleared."
                ),
            },
            usage_patterns=[
                "Pattern 6a cleanup when async job completes.",
                "Bulk cancellation of grouped schedules.",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def clear_scheduled_actions_by_tag(
        self, tag: str, state: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="get_schedules_by_tag",
        is_discoverable=True,
        provider="scheduling_service",
        parameters={
            "tag": ParameterMetadata(
                description="Tag to filter schedules by.",
                required=True,
                type=ParameterType.STRING,
            ),
            "state": _STATE_PARAM,
        },
        return_value_schema=ReturnValueSchema(
            description="Matching schedules with metadata.",
            type=ParameterType.OBJECT,
            properties={
                "tag": ParameterMetadata(
                    type=ParameterType.STRING, description="Tag used for filtering."
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of matching schedules."
                ),
                "schedules": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="List of matching schedule objects.",
                ),
            },
            usage_patterns=[
                "Verify heartbeat schedule exists (heartbeat:global).",
                "Detect duplicate schedules before cleanup.",
                "Inspect per-job/per-session monitoring schedules.",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def get_schedules_by_tag(
        self, tag: str, state: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="ensure_global_heartbeat",
        is_discoverable=True,
        provider="scheduling_service",
        parameters={
            "cadence_minutes": ParameterMetadata(
                description="Wake-up interval in minutes (default: 5).",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "tag": ParameterMetadata(
                description="Schedule tag (default: heartbeat:global).",
                required=False,
                type=ParameterType.STRING,
            ),
            "memory_tag": ParameterMetadata(
                description="Memory tag for wake-up recall (default: same as tag).",
                required=False,
                type=ParameterType.STRING,
            ),
            "state": _STATE_PARAM,
        },
        return_value_schema=ReturnValueSchema(
            description="Heartbeat ensure result.",
            type=ParameterType.OBJECT,
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="One of: created, already_present, normalized.",
                ),
                "schedule_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Active heartbeat schedule ID.",
                ),
                "tag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Schedule tag.",
                ),
                "cron_expression": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Cron expression for the heartbeat.",
                ),
                "cadence_minutes": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Wake-up interval in minutes.",
                ),
                "cleared_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of stale/duplicate schedules cleared during normalization.",
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Human-readable status message.",
                ),
            },
            usage_patterns=[
                "Bootstrap liveness on first user message after startup.",
                "Idempotent ensure — safe to call repeatedly.",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def ensure_global_heartbeat(
        self,
        cadence_minutes: int | None = None,
        tag: str | None = None,
        memory_tag: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
