from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ananta.core.plugins.plugin_contracts import ActionStatus

from ..constants import SchedulerJobStatus
from ..utils.response_helpers import build_response

RELOAD_SAFE = True

if TYPE_CHECKING:
    from .scheduler_manager import SchedulerManager


def check_existing_heartbeat(
    matching: dict[str, Any],
    desired_cron: str,
    tag: str,
    cadence_minutes: int,
) -> dict[str, Any] | None:
    if len(matching) != 1:
        return None
    sid, data = next(iter(matching.items()))
    existing_cron = data.get("cron_expression", "")
    if (
        data.get("status") == SchedulerJobStatus.SCHEDULED
        and existing_cron == desired_cron
    ):
        return build_response(
            ActionStatus.COMPLETED.value,
            {
                "status": "already_present",
                "schedule_id": sid,
                "tag": tag,
                "cron_expression": existing_cron,
                "cadence_minutes": cadence_minutes,
                "message": f"Global heartbeat already exists: {sid}",
            },
        )
    return None


def clear_stale_heartbeats(
    matching: dict[str, Any],
    scheduler_manager: SchedulerManager | None,
    delete_fn: Callable[[str], bool],
) -> int:
    cleared_count = 0
    for sid in matching:
        if scheduler_manager:
            scheduler_manager.remove_job(sid)
        if delete_fn(sid):
            cleared_count += 1
    return cleared_count


def register_heartbeat_job(
    schedule_id: str,
    schedule_data: Any,
    desired_cron: str,
    scheduler_manager: SchedulerManager | None,
    execute_fn: Callable[[str, dict[str, Any]], None],
) -> None:
    # Heartbeat schedules bypass `validate_cron_action_def` because they are
    # built via `ScheduleFactory._parse_memory_tag_actions`, which constructs
    # `ActionData` with `result_processor_kind=None` by construction. The
    # canonical memory-tag heartbeat shape dispatches a non-inference read verb
    # (`get_memories_by_tag`) and the dispatcher's EDGE_SINK_SKIP branch
    # short-circuits before any session-context-requiring path fires. Wiring
    # the validator here would be redundant; revisit if a new heartbeat caller
    # ever introduces a non-`None` processor kind.
    if scheduler_manager:
        data_dict = schedule_data.model_dump()
        data_dict["id"] = schedule_id
        scheduler_manager.add_cron_job(
            lambda: execute_fn(schedule_id, data_dict),
            desired_cron,
            schedule_id,
        )
