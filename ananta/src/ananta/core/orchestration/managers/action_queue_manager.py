import logging
from collections.abc import Sequence

from ananta.core.domain.error_codes import ErrorCode
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.error_handling import FrameworkError

from ..interfaces import IActionQueueManager

logger = logging.getLogger(__name__)


class ActionQueueManager(IActionQueueManager):
    def __init__(self, max_actions_per_cycle: int = 100):
        self.max_actions_per_cycle = max_actions_per_cycle

    async def get_next_pending_action(
        self, state: dict[str, object]
    ) -> tuple[dict[str, object] | None, int | None]:
        actions_obj = state.get("actions", [])
        if not isinstance(actions_obj, Sequence):
            actions_obj = []

        actions: Sequence[object] = actions_obj

        for index, action_obj in enumerate(actions):
            if not isinstance(action_obj, dict):
                continue

            action: dict[str, object] = action_obj
            status = action.get("action_status", ActionStatus.QUEUED.value)
            action.get("name", "unnamed")

            if status == ActionStatus.QUEUED.value:
                return (action, index)

        return (None, None)

    async def update_action_status(
        self, state: dict[str, object], action_index: int, status: ActionStatus
    ) -> None:
        actions_obj = state.get("actions", [])
        if not isinstance(actions_obj, Sequence):
            actions_obj = []

        actions: Sequence[object] = actions_obj

        if action_index < 0 or action_index >= len(actions):
            raise FrameworkError(
                message=f"Invalid action index: {action_index}",
                error_code=ErrorCode.VALIDATION_ERROR,
                details={"index": action_index, "total_actions": len(actions)},
            )

        action_obj = actions[action_index]
        if not isinstance(action_obj, dict):
            raise FrameworkError(
                message=f"Invalid action at index {action_index}: not a dict",
                error_code=ErrorCode.VALIDATION_ERROR,
                details={"index": action_index, "type": type(action_obj).__name__},
            )

        action: dict[str, object] = action_obj
        action.get("name", "unnamed")
        action.get("status", ActionStatus.QUEUED)

        action["status"] = status

    async def set_action_to_processing(self, state: dict[str, object], action_index: int) -> None:
        await self.update_action_status(state, action_index, ActionStatus.PROCESSING)

        actions_obj = state.get("actions", [])
        if not isinstance(actions_obj, Sequence):
            actions_obj = []

        actions: Sequence[object] = actions_obj
        action_obj = actions[action_index]
        if not isinstance(action_obj, dict):
            return

        action: dict[str, object] = action_obj
        action.get("name", "unnamed")

    async def process_action_queue(self, state: dict[str, object]) -> dict[str, object]:
        actions_processed = 0

        while actions_processed < self.max_actions_per_cycle:
            pending_result = await self.get_next_pending_action(state)
            pending_action, action_index = pending_result

            if not pending_action or action_index is None:
                break

            pending_action.get("name", "unnamed")

            await self.set_action_to_processing(state, action_index)

            await self.update_action_status(state, action_index, ActionStatus.COMPLETED)

            actions_processed += 1

            if actions_processed >= self.max_actions_per_cycle:
                break

        return state

    def get_pending_action_count(self, state: dict[str, object]) -> int:
        actions_obj = state.get("actions", [])
        if not isinstance(actions_obj, Sequence):
            actions_obj = []

        actions: Sequence[object] = actions_obj

        pending_count = 0
        for action_obj in actions:
            if isinstance(action_obj, dict):
                action: dict[str, object] = action_obj
                if action.get("status", ActionStatus.QUEUED) == ActionStatus.QUEUED:
                    pending_count += 1

        return pending_count

    def get_action_summary(self, state: dict[str, object]) -> dict[str, int]:
        actions_obj = state.get("actions", [])
        if not isinstance(actions_obj, Sequence):
            actions_obj = []

        actions: Sequence[object] = actions_obj
        summary = {"total": len(actions), "pending": 0, "processing": 0, "completed": 0, "error": 0}

        for action_obj in actions:
            if not isinstance(action_obj, dict):
                continue

            action: dict[str, object] = action_obj
            status = action.get("status", ActionStatus.QUEUED)
            if status == ActionStatus.QUEUED:
                summary["pending"] += 1
            elif status == ActionStatus.PROCESSING:
                summary["processing"] += 1
            elif status == ActionStatus.COMPLETED:
                summary["completed"] += 1
            elif status == ActionStatus.ERROR:
                summary["error"] += 1

        return summary
