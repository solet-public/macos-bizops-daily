"""
Event Handler Manager

Responsibility: Centralized management of all EventOrchestrator event handlers
Dependencies: EventOrchestrator reference for state management and event handling

Note: Result processing for edge processes (result_processor + error_processor) is handled
by ActionQueuePoller._mark_action_completed() via the database-first architecture, NOT
through the event system. This module handles orchestration-level events only.
"""

import logging
from typing import TYPE_CHECKING, Protocol

from ananta.core.events import Event, EventResult, SystemEvent
from ananta.core.plugins.plugin_contracts import ActionStatus

if TYPE_CHECKING:
    from ananta.core.event_orchestrator import EventOrchestrator

logger = logging.getLogger(__name__)


class AsyncEventHandlerWrapper:
    """Wrapper to make async handler functions compatible with EventHandlerProtocol."""

    __name__: str

    def __init__(self, handler_func: object) -> None:
        self._handler_func = handler_func
        self.__name__ = getattr(handler_func, "__name__", "async_handler")

    def __call__(self, event: SystemEvent) -> EventResult | None:
        """Call the wrapped handler function."""
        import asyncio
        from collections.abc import Callable

        # Type narrow: ensure handler_func is callable
        if not callable(self._handler_func):
            return None

        handler: Callable[[SystemEvent], object] = self._handler_func
        result = handler(event)

        if asyncio.iscoroutine(result):
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return None
            else:
                result = loop.run_until_complete(result)

        return result if isinstance(result, EventResult) else None

    def handle(self, event: SystemEvent) -> EventResult | None:
        """Alternative handle method for protocol compatibility."""
        return self.__call__(event)


class OrchestratorProtocol(Protocol):
    """Protocol defining the interface required by EventHandlerManager."""

    state_manager: object
    event_handler_registry: object

    async def _process_actions_from_result(
        self,
        state: dict[str, object],
        result: dict[str, object],
        parent_action_id: str | None = None,
    ) -> None: ...

    async def _update_action_status_in_state(
        self,
        state: dict[str, object],
        action_name: str,
        status: str,
        error: dict[str, object] | None = None,
        result: dict[str, object] | None = None,
    ) -> None: ...

    async def process_actions(self) -> dict[str, object] | None: ...


class EventHandlerManager:
    """Event handler manager for EventOrchestrator core event handling.

    Handles orchestration-level events (action success/error, state saves, triggers).
    Result/error processing for edge processes is handled separately by
    ActionQueuePoller._mark_action_completed() via the database-first architecture.
    """

    def __init__(self, orchestrator_ref: "EventOrchestrator") -> None:
        """Initialize EventHandlerManager with orchestrator reference for operations."""
        self.orchestrator = orchestrator_ref

    async def _store_result_in_state(
        self, state: dict[str, object], action_name: str, result: dict[str, object]
    ) -> None:
        """Store action result data in state if available."""
        if "data" in result:
            result_data = result["data"]
            if isinstance(result_data, dict):
                state[action_name] = result_data

    async def _process_action_completion(
        self, state: dict[str, object], action_name: str, result: dict[str, object]
    ) -> None:
        """Process action completion including status update and result processing."""
        current_action_id_obj = state.get("current_action_id")

        # Type narrow: current_action_id should be str or None
        current_action_id: str | None = None
        if isinstance(current_action_id_obj, str):
            current_action_id = current_action_id_obj

        await self.orchestrator._process_actions_from_result(state, result, current_action_id)

        # Type narrow: result.get("data") should be dict or use empty dict
        result_data_obj = result.get("data", {})
        result_data: dict[str, object] = {}
        if isinstance(result_data_obj, dict):
            result_data = result_data_obj

        await self.orchestrator._update_action_status_in_state(
            state, action_name, ActionStatus.COMPLETED.value, _result=result_data
        )

    def _create_orchestrator_trigger_event(
        self, pending_count: int, correlation_id: str
    ) -> SystemEvent:
        """Create orchestrator trigger event for pending actions."""
        return SystemEvent(
            system_event_type="orchestrator_triggered",
            context={
                "trigger_reason": "pending_actions_after_success",
                "pending_count": pending_count,
            },
            source="action_success_handler",
            correlation_id=correlation_id,
        )

    def _get_pending_actions(self, state: dict[str, object]) -> list[dict[str, object]]:
        """Get list of pending actions from state."""
        actions_obj = state.get("actions", [])
        if not isinstance(actions_obj, list):
            return []

        pending: list[dict[str, object]] = []
        for action in actions_obj:
            if (
                isinstance(action, dict)
                and action.get("action_status") == ActionStatus.QUEUED.value
            ):
                pending.append(action)
        return pending

    def register_all_core_handlers(self) -> None:
        """Register all core event handlers with the event handler registry.

        Note: Result/error processing for edge processes is handled by
        ActionQueuePoller._mark_action_completed() via the database-first architecture.
        These handlers cover orchestration-level state management only.
        """
        logger.debug("EventHandlerManager: Registering core event handlers")

        # Handler 1: Action Success (Async)
        async def handle_action_success(event: SystemEvent) -> EventResult:
            action_name_obj = event.context.get("action_name")
            result_obj = event.context.get("result", {})

            if not isinstance(action_name_obj, str):
                return EventResult(success=False, resulting_events=[])

            action_name: str = action_name_obj

            if not isinstance(result_obj, dict):
                return EventResult(success=False, resulting_events=[])

            result: dict[str, object] = result_obj

            try:
                state = await self.orchestrator.state_manager.load()

                await self._store_result_in_state(state, action_name, result)
                await self._process_action_completion(state, action_name, result)

                await self.orchestrator.state_manager.save(state)

                correlation_id = event.correlation_id if event.correlation_id is not None else ""
                resulting_events: list[Event] = []

                # Check for pending actions and trigger processing if needed
                pending_actions = self._get_pending_actions(state)
                if pending_actions:
                    resulting_events.append(
                        self._create_orchestrator_trigger_event(
                            len(pending_actions), correlation_id
                        )
                    )

                return EventResult(success=True, resulting_events=resulting_events)

            except Exception as e:
                return EventResult(success=False, error=e, resulting_events=[])

        # Handler 2: Action Error (Async)
        async def handle_action_error(event: SystemEvent) -> EventResult:
            action_name_obj = event.context.get("action_name")
            error_obj = event.context.get("error", {})

            if not isinstance(action_name_obj, str):
                return EventResult(success=False, resulting_events=[])

            action_name: str = action_name_obj

            error: dict[str, object] | None = None
            if isinstance(error_obj, dict):
                error = error_obj

            try:
                state = await self.orchestrator.state_manager.load()

                await self.orchestrator._update_action_status_in_state(
                    state, action_name, ActionStatus.ERROR.value, _error=error
                )

                await self.orchestrator.state_manager.save(state)

                resulting_events: list[Event] = [
                    SystemEvent(
                        system_event_type="error_state_save",
                        context={
                            "action_name": action_name,
                            "error": error if error is not None else {},
                        },
                        source="action_error_handler",
                        correlation_id=event.correlation_id,
                    )
                ]

                return EventResult(success=True, resulting_events=resulting_events)

            except Exception as e:
                return EventResult(success=False, error=e, resulting_events=[])

        # Handler 3: Action Empty Result (Async)
        async def handle_action_empty_result(event: SystemEvent) -> EventResult:
            action_name_obj = event.context.get("action_name")

            if not isinstance(action_name_obj, str):
                return EventResult(success=False, resulting_events=[])

            action_name: str = action_name_obj

            try:
                state = await self.orchestrator.state_manager.load()

                await self.orchestrator._update_action_status_in_state(
                    state, action_name, ActionStatus.COMPLETED.value, _result={}
                )

                await self.orchestrator.state_manager.save(state)

                return EventResult(success=True, resulting_events=[])

            except Exception as e:
                return EventResult(success=False, error=e, resulting_events=[])

        # Handler 4: Error State Save (Async)
        async def handle_error_state_save(event: SystemEvent) -> EventResult:
            _ = event  # Context available if needed for future error cleanup
            try:
                return EventResult(success=True, resulting_events=[])
            except Exception as e:
                return EventResult(success=False, error=e, resulting_events=[])

        # Handler 5: Orchestrator Trigger (Async)
        async def handle_orchestrator_trigger(event: SystemEvent) -> EventResult:
            _ = event  # Context available if needed
            try:
                await self.orchestrator.process_actions()
                return EventResult(success=True, resulting_events=[])
            except Exception as e:
                return EventResult(success=False, error=e, resulting_events=[])

        # Register async handlers
        self.orchestrator.event_handler_registry.register(
            "action_success", AsyncEventHandlerWrapper(handle_action_success)
        )
        self.orchestrator.event_handler_registry.register(
            "action_error", AsyncEventHandlerWrapper(handle_action_error)
        )
        self.orchestrator.event_handler_registry.register(
            "action_empty_result", AsyncEventHandlerWrapper(handle_action_empty_result)
        )
        self.orchestrator.event_handler_registry.register(
            "error_state_save", AsyncEventHandlerWrapper(handle_error_state_save)
        )
        self.orchestrator.event_handler_registry.register(
            "orchestrator_triggered", AsyncEventHandlerWrapper(handle_orchestrator_trigger)
        )

        logger.debug(
            "EventHandlerManager: Core event handlers registered (5 handlers)"
        )

    def get_manager_summary(self) -> dict[str, object]:
        """Get summary of EventHandlerManager for debugging."""
        return {
            "component": "EventHandlerManager",
            "responsibility": "Orchestration-level event handler registration",
            "handlers": [
                "action_success",
                "action_error",
                "action_empty_result",
                "error_state_save",
                "orchestrator_triggered",
            ],
        }
