import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from .base import ActionEvent, Event, EventResult, SystemEvent
from .utils import generate_action_name

logger = logging.getLogger(__name__)


# Protocol classes for service interfaces
class ActionRecorderProtocol(Protocol):
    """Protocol for action recording service (update operations only)."""

    def update_action_completion(self, action_record_id: str, result: dict[str, object]) -> None:
        """Update an action event with completion result."""
        ...

    def update_action_error(self, action_record_id: str, error_message: str) -> None:
        """Update an action event with error information."""
        ...


class ActionFactoryProtocol(Protocol):
    """Protocol for action factory service (creation operations)."""

    def submit_action_definition(
        self, action_definition: dict[str, object], context: dict[str, object] | None = None
    ) -> str:
        """Submit action definition through ActionFactory.

        Returns:
            str: The action_id of the submitted action

        Raises:
            FrameworkError: If submission fails
        """
        ...


class EventHandlerProtocol(Protocol):
    """Protocol for event handlers."""

    def __call__(self, event: SystemEvent) -> EventResult | None:
        """Handle a system event."""
        ...

    def handle(self, event: SystemEvent) -> EventResult | None:
        """Alternative handle method for handlers."""
        ...


@dataclass
class EventHandlerRegistry:
    handlers: dict[str, list[EventHandlerProtocol]] = field(default_factory=dict)

    def register(self, event_type: str, handler: EventHandlerProtocol) -> None:
        if event_type not in self.handlers:
            self.handlers[event_type] = []
        self.handlers[event_type].append(handler)

    def get_handlers(self, event_type: str) -> list[EventHandlerProtocol]:
        handlers = self.handlers.get(event_type, [])
        return handlers

    def publish(self, event: SystemEvent) -> None:
        """
        Publish event to all registered handlers for the event type.
        ARCHITECTURAL FIX: No fallback mechanisms - event must be handled properly.
        """
        event_type = event.system_event_type
        handlers = self.get_handlers(event_type)

        if not handlers:
            return

        for handler in handlers:
            try:
                # Call the handler with the event
                result = handler(event)

                # ARCHITECTURAL FIX: Process resulting_events from handler
                if isinstance(result, EventResult) and result.resulting_events:
                    for resulting_event in result.resulting_events:
                        # Type narrow to SystemEvent
                        if isinstance(resulting_event, SystemEvent):
                            # Recursively publish resulting events
                            self.publish(resulting_event)

            except Exception:
                # FAIL-FAST: No fallback, let caller handle the error
                raise


class EventProcessor:
    def __init__(
        self,
        action_manager: object,
        event_handler_registry: EventHandlerRegistry,
        state_service: object,
        action_preparation_service: object | None = None,
        state_manager: object | None = None,
        action_recorder: ActionRecorderProtocol | None = None,
        action_factory: ActionFactoryProtocol | None = None,
        session_manager: object | None = None,
        flow_manager: object | None = None,
    ):
        self.action_manager = action_manager
        self.event_handler_registry = event_handler_registry
        self.state_service = state_service
        self.action_preparation_service = action_preparation_service
        self.state_manager = state_manager
        # action_recorder is for update operations only (completion, error)
        self.action_recorder: ActionRecorderProtocol | None = action_recorder
        # action_factory is for action creation (routes through ActionFactory validation)
        self.action_factory: ActionFactoryProtocol | None = action_factory
        self.session_manager = session_manager
        self.flow_manager = flow_manager

    async def process_event(self, event: Event) -> EventResult:
        # Handle different event types appropriately
        if isinstance(event, ActionEvent):
            return await self._process_action_event(event)
        elif isinstance(event, SystemEvent):
            return await self._process_system_event(event)
        else:
            return EventResult(
                success=False,
                error=Exception(f"Unknown event type: {type(event).__name__}"),
            )

    def _trace_sql_execution_start(self, event: ActionEvent) -> None:
        """Trace SQL execution start with detailed logging."""
        if "execute_sql" not in event.action_name:
            return

        logger.debug(f"SQL-TRACE-001: execute_sql action detected: {event.action_name}")
        logger.debug(
            f"SQL-TRACE-002: Action parameters keys: {list(event.parameters.keys()) if event.parameters else 'None'}"
        )
        if hasattr(event, "parameters") and event.parameters and "sql_query" in event.parameters:
            logger.debug(f"SQL-TRACE-003: SQL query: {event.parameters['sql_query']}")
        if event.action_definition and "result_processor" in event.action_definition:
            logger.debug(
                f"SQL-TRACE-004: Has result_processor: {event.action_definition['result_processor']}"
            )

    def _trace_sql_execution_result(self, event: ActionEvent, result: dict[str, object]) -> None:
        """Trace SQL execution result with detailed logging."""
        if "execute_sql" not in event.action_name:
            return

        logger.debug("SQL-TRACE-005: execute_sql result received")
        logger.debug(f"SQL-TRACE-006: Result keys: {list(result.keys())}")
        if "status" in result:
            logger.debug(f"SQL-TRACE-007: SQL status: {result['status']}")
        if "data" in result:
            logger.debug(f"SQL-TRACE-008: SQL data type: {type(result['data'])}")
            data_value = result["data"]
            if isinstance(data_value, dict) and "results" in data_value:
                results_value = data_value["results"]
                logger.debug(
                    f"SQL-TRACE-009: SQL results count: {len(results_value) if results_value is not None else 0}"
                )
        if "actions" in result:
            actions_value = result["actions"]
            actions_count = len(actions_value) if isinstance(actions_value, list) else 0
            logger.debug(f"SQL-TRACE-010: SQL returned {actions_count} follow-up actions")

    def _record_action_start(
        self, event: ActionEvent, action_name: str, process_key: str | None
    ) -> str:
        """Record action start by routing through ActionFactory.

        ActionFactory handles validation, flow_id enforcement, and persistence.
        This ensures all actions go through a single validated creation path.

        Raises:
            ValueError: If action_factory or flow_id is missing
            RuntimeError: If action submission fails

        Returns:
            The action_id from successful submission
        """
        # Fail fast: action_factory is required
        if not self.action_factory:
            raise ValueError(
                f"EVENTPROCESSOR: action_factory not available - cannot create action '{action_name}'"
            )

        # Fail fast: flow_id is required for all actions
        if not event.flow_id:
            raise ValueError(
                f"EVENTPROCESSOR: flow_id required for action '{action_name}'"
            )

        # Build action definition from event
        # Use event.action_definition if available, otherwise construct from event fields
        action_definition: dict[str, object] = dict(event.action_definition or {})

        # Ensure required fields are present
        action_definition["name"] = action_name
        if process_key:
            action_definition["process_key"] = process_key
        action_definition["session_id"] = event.session_id
        action_definition["flow_id"] = event.flow_id
        if event.parameters:
            action_definition["arguments"] = event.parameters

        # Submit through ActionFactory (handles validation + persistence)
        # ActionFactory raises on failure, returns action_id on success
        return self.action_factory.submit_action_definition(action_definition)

    def _record_action_completion(
        self, action_record_id: str, result: dict[str, object]
    ) -> None:
        """Record action completion in correlation database."""
        if not self.action_recorder:
            return

        try:
            self.action_recorder.update_action_completion(action_record_id, result)
        except Exception as e:
            logger.warning(f"EVENTPROCESSOR: Failed to record action completion: {e}")

    def _validate_and_extract_action_definition(
        self, event: ActionEvent
    ) -> tuple[dict[str, object], str, str | None]:
        """Validate event has action_definition and extract action details."""
        # DEBUG-002: Trace action_definition preservation

        # Use the complete original action instead of reconstructing
        action_def = event.action_definition
        if not action_def:
            raise ValueError(f"ActionEvent for '{event.action_name}' missing action_definition")

        # Type narrow the name field
        name_value = action_def.get("name", event.action_name)
        action_name = name_value if isinstance(name_value, str) else event.action_name

        # Type narrow the process_key field
        process_key_value = action_def.get("process_key")
        process_key = process_key_value if isinstance(process_key_value, str) else None

        return action_def, action_name, process_key

    def _execute_database_first_processing(
        self, action_name: str, _process_key: str | None
    ) -> dict[str, object]:  # Reserved for interface compatibility
        """Execute database-first processing, returning queued status."""

        # ARCHITECTURAL BREAKTHROUGH: Replace immediate execution with database-first trigger processing
        # The action was already stored in core__action_events above (line 108: self.action_recorder.store_action_event)
        # Database trigger will fire and execute via ActionEventsHandler
        # This implements true database-first action processing

        # Return queued status - trigger will handle actual execution
        result: dict[str, object] = {
            "status": "queued",
            "action_status": "queued",
            "data": {"message": f"Action {action_name} queued for database trigger execution"},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        return result

    def _create_resulting_events(
        self, event: ActionEvent, result: dict[str, object]
    ) -> list[Event]:
        """Create resulting events from action processing."""
        resulting_events: list[Event] = []

        # Always create SystemEvent for action completion

        resulting_events.append(
            SystemEvent(
                system_event_type="action_completed",
                context={
                    "action_name": event.action_name,
                    "provider": event.provider,
                    "result": result,
                    "original_event_id": event.event_id,
                },
                source="event_processor",
                correlation_id=event.correlation_id,
            )
        )

        # CRITICAL FIX: Restore follow-up ActionEvent creation for result.actions
        # This is essential for Process Registry Query and other multi-step workflows
        actions_value = result.get("actions")
        if isinstance(actions_value, list) and actions_value:
            for _i, action_item in enumerate(actions_value):
                if not isinstance(action_item, dict):
                    continue

                action_data: dict[str, object] = action_item
                # DRY: Use centralized action name generation
                action_name = generate_action_name(action_data, "result_processing")

                # ARCHITECTURAL FIX: Ensure action_definition has required 'name' field for validation
                complete_action_definition = dict(action_data)
                if "name" not in complete_action_definition:
                    complete_action_definition["name"] = action_name

                # Type narrow the parameters/arguments
                params_value = action_data.get("parameters", action_data.get("arguments", {}))
                parameters = params_value if isinstance(params_value, dict) else {}

                # Type narrow the provider
                provider_value = action_data.get("provider", "unknown")
                provider = provider_value if isinstance(provider_value, str) else "unknown"

                resulting_events.append(
                    ActionEvent(
                        action_name=action_name,
                        parameters=parameters,
                        provider=provider,
                        session_id=event.session_id,
                        flow_id=event.flow_id,
                        parent_event_id=event.event_id,
                        source="action_result",
                        correlation_id=event.correlation_id,
                        action_definition=complete_action_definition,  # CRITICAL: Include name field for validation
                    )
                )

        return resulting_events

    def _handle_action_error(
        self, event: ActionEvent, error: Exception, action_record_id: str
    ) -> EventResult:
        """Handle action processing errors and record them."""
        # Record action error in correlation database
        if self.action_recorder:
            try:
                self.action_recorder.update_action_error(action_record_id, str(error))
            except Exception as e:
                logger.warning(f"EVENTPROCESSOR: Failed to record action error: {e}")

        return EventResult(
            success=False,
            error=error,
            resulting_events=[
                SystemEvent(
                    system_event_type="action_event_failed",
                    context={
                        "action_name": event.action_name,
                        "error_message": str(error),
                        "original_event_id": event.event_id,
                    },
                    source="event_processor",
                    correlation_id=event.correlation_id,
                )
            ],
        )

    async def _process_action_event(self, event: ActionEvent) -> EventResult:
        """
        Process an action event through database-first architecture.

        COMPLEXITY REDUCED: C(12) → A(3) through focused method extraction
        """

        # SQL EXECUTION TRACING - Step 1: Action Entry
        self._trace_sql_execution_start(event)

        # Step 1: Validate and extract action definition
        _action_def, action_name, process_key = self._validate_and_extract_action_definition(event)

        # Step 2: Record action start in correlation database
        action_record_id = self._record_action_start(event, action_name, process_key)

        try:
            # Step 3: Execute database-first processing
            result = self._execute_database_first_processing(action_name, process_key)

            # Step 4: Record completion and trace result
            self._record_action_completion(action_record_id, result)
            self._trace_sql_execution_result(event, result)

            # Step 5: Create resulting events
            resulting_events = self._create_resulting_events(event, result)

            return EventResult(
                success=True,
                resulting_events=resulting_events,
                data=result,
            )

        except Exception as e:
            return self._handle_action_error(event, e, action_record_id)

    async def _process_system_event(self, event: SystemEvent) -> EventResult:
        # DATABASE-FIRST RESULT PROCESSOR: Handle result_processor for action_completed events
        # NOTE: _process_action_completed_event method was removed - using standard handler flow

        handlers = self.event_handler_registry.get_handlers(event.system_event_type)

        resulting_events = []
        errors = []

        for _i, handler in enumerate(handlers):
            try:
                handler_result = (
                    handler.handle(event) if hasattr(handler, "handle") else handler(event)
                )

                if handler_result and hasattr(handler_result, "resulting_events"):
                    resulting_events.extend(handler_result.resulting_events)

            except Exception as e:
                errors.append(e)

        if errors:
            for i, error in enumerate(errors):
                resulting_events.append(
                    SystemEvent(
                        system_event_type="system_event_handler_failed",
                        context={
                            "original_event_type": event.system_event_type,
                            "original_event_id": event.event_id,
                            "handler_error": str(error),
                            "error_index": i,
                        },
                        source="event_processor",
                        correlation_id=event.correlation_id,
                    )
                )

        success = len(errors) == 0

        return EventResult(
            success=success,
            resulting_events=resulting_events,
            error=errors[0] if errors else None,
        )
