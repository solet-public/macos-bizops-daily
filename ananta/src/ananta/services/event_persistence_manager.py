"""
Event Persistence Manager

Responsibility: Handle all event persistence operations for ActionEventBus
Dependencies: StateService, ActionRequestEvent, logging
Complexity: Medium - focused on database operations and schema management

Extracted from ActionEventBus god class (829 lines)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from ananta.config.schema_factory import get_standardized_schema
from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.status import is_status_match
from ananta.interfaces.state_service_protocol import StateServiceProtocol

if TYPE_CHECKING:
    from ananta.services.action_event_bus import ActionRequestEvent

logger = logging.getLogger(__name__)


class EventPersistenceManager:
    """
    Service for managing event persistence and database operations.

    ARCHITECTURAL ROLE: Supporting service that extracts event persistence logic
    from ActionEventBus while maintaining event bus integrity.

    This service handles:
    - Event database schema initialization
    - Event persistence to database
    - Loading pending events from database
    - Schema validation and management
    """

    def __init__(self, state_service: StateServiceProtocol | None = None) -> None:
        """Initialize EventPersistenceManager.

        Note: Call initialize_schema() after construction to set up the database schema.
        """
        self.state_service = state_service
        self._schema_initialized = False

    def initialize_schema(self) -> None:
        """Initialize the database schema. Must be called before using persistence methods.

        This replaces the old lazy initialization pattern. Call this explicitly
        after the state_service is ready.

        Raises:
            RuntimeError: If state_service is not available
        """
        if self._schema_initialized:
            return

        if not self.state_service:
            raise RuntimeError(
                "Cannot initialize schema: state_service not available. "
                "Ensure state_service is set before calling initialize_schema()."
            )

        self._init_schema_impl()
        self._schema_initialized = True

    def _verify_schema_initialized(self) -> None:
        """Verify schema is initialized. Fails fast if not."""
        if not self._schema_initialized:
            raise RuntimeError(
                "EventPersistenceManager schema not initialized. "
                "Call initialize_schema() before using persistence methods."
            )

    def _init_schema_impl(self) -> None:
        """Internal implementation of schema initialization.

        EXTRACTED FROM: EventBus._init_schema() - B(7) complexity

        Raises:
            Exception: If schema creation fails
        """
        # Use standardized schema from SchemaFactory which includes proper system fields
        event_bus_schema = get_standardized_schema("event_bus_events")

        # Convert TableSchema objects to dictionary format expected by state service
        schema_tables: dict[str, object] = {}
        for table_name, table_schema in event_bus_schema.tables.items():
            column_definitions: dict[str, str] = {}
            for col_name, col_def in table_schema.columns.items():
                # Use the ColumnDefinition.to_sql() method to generate complete SQL with constraints
                full_sql_definition = col_def.to_sql(col_name)

                # Extract just the type and constraints part (without column name)
                parts = full_sql_definition.split(" ", 1)
                if len(parts) > 1:
                    column_sql = parts[1]  # Everything after column name
                else:
                    column_sql = col_def.type.value  # Fallback to just type

                column_definitions[col_name] = column_sql

            # Build table definition with id_prefix and columns
            schema_tables[table_name] = {
                "id_prefix": table_schema.id_prefix,
                "columns": column_definitions,
            }

        schema: dict[str, object] = {"tables": schema_tables}

        try:
            if self.state_service is None:
                raise Exception("State service not available for schema creation")
            result = self.state_service.create_schema("core", schema)

            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                raise Exception(f"Failed to create action events schema: {result}")

        except Exception as e:
            raise Exception(f"Failed to initialize action event bus schema: {e}") from e

    def persist_event(self, event: ActionRequestEvent) -> None:
        """
        Persist an event to the database.

        EXTRACTED FROM: EventBus._persist_event() - B(8) complexity

        Args:
            event: ActionRequestEvent to persist

        Raises:
            Exception: If persistence fails
        """
        # Verify schema is initialized (fail fast if not)
        self._verify_schema_initialized()

        record = {
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "source_plugin": event.source_plugin,
            "target_plugin": event.target_plugin,
            "correlation_id": event.correlation_id,
            "priority": event.priority,
            "action_data": json.dumps(event.action_data) if event.action_data else None,
            "response_data": json.dumps(event.response_data) if event.response_data else None,
            "error_info": json.dumps(event.error_info) if event.error_info else None,
            "metadata": json.dumps(event.metadata) if event.metadata else None,
        }

        try:
            if self.state_service is None:
                return
            result = self.state_service.write_state(
                "core", {"table": "event_bus_events", "record": record}
            )
            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                raise Exception(f"Failed to persist event: {result}")
        except Exception as e:
            raise Exception(f"Failed to persist action event: {e}") from e

    def load_pending_events_from_database(self) -> list[ActionRequestEvent]:
        """
        Load pending events from the database.

        EXTRACTED FROM: EventBus._load_pending_events_from_database() - B(10) complexity

        Returns:
            List of ActionRequestEvent objects loaded from database
        """
        try:
            self._verify_schema_initialized()
            records = self._query_pending_events()
            if records is None:
                return []
            return self._build_events_from_records(records)
        except Exception as e:
            logger.error(f"Failed to load pending events from database: {e}")
            return []

    def _query_pending_events(self) -> list[dict[str, object]] | None:
        """Query database for pending events."""
        logger.debug("Loading pending events from database...")
        if self.state_service is None:
            logger.error("State service not available for loading pending events")
            return None

        result = self.state_service.read_state(
            "core",
            {
                "table": "event_bus_events",
                "filters": {"last_read_at": {"is": None}},
                "order_by": "created_at ASC",
            },
        )

        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            logger.error(f"Failed to read pending events: {result}")
            return None

        data = result.get("data")
        if not isinstance(data, dict):
            logger.error("Invalid data structure in result")
            return None

        records = data.get("records", [])
        if not isinstance(records, list):
            logger.error("Invalid records structure in data")
            return None

        logger.debug(f"Found {len(records)} pending events in database")
        return records

    def _build_events_from_records(
        self, records: list[dict[str, object]]
    ) -> list[ActionRequestEvent]:
        """Build ActionRequestEvent objects from database records."""
        from ananta.services.action_event_bus import ActionEventType, ActionRequestEvent

        loaded_events: list[ActionRequestEvent] = []

        for record in records:
            try:
                event = self._build_single_event(record, ActionEventType, ActionRequestEvent)
                loaded_events.append(event)
                logger.debug(
                    f"Loaded event {event.event_id} ({event.event_type.value}) from {event.source_plugin}"
                )
            except Exception as e:
                logger.error(f"Failed to load event {record.get('event_id', 'unknown')}: {e}")

        logger.debug(f"Loaded {len(loaded_events)} events from database")
        return loaded_events

    def _build_single_event(
        self,
        record: dict[str, object],
        action_event_type: type[Any],
        action_request_event_class: type[Any],
    ) -> ActionRequestEvent:
        """Build a single ActionRequestEvent from a database record."""
        created_at = record["created_at"]
        event = action_request_event_class(
            event_id=record["event_id"],
            event_type=action_event_type(record["event_type"]),
            timestamp=datetime.fromisoformat(cast(str, created_at)),
            source_plugin=record["source_plugin"],
            target_plugin=record.get("target_plugin"),
            correlation_id=record.get("correlation_id"),
            priority=record.get("priority", 5),
            action_data=self._parse_json_field(record.get("action_data"), {}),
            response_data=self._parse_json_field(record.get("response_data")),
            error_info=self._parse_json_field(record.get("error_info")),
            metadata=self._parse_json_field(record.get("metadata")),
        )
        return cast("ActionRequestEvent", event)

    def _parse_json_field(
        self, value: object, default: dict[str, object] | None = None
    ) -> dict[str, object] | None:
        """Parse a JSON string field into a dict."""
        if value is None:
            return default
        if isinstance(value, str):
            return cast(dict[str, object], json.loads(value))
        return default

    def mark_event_as_read(self, event_id: str) -> None:
        """
        Mark an event as read in the database.

        Args:
            event_id: ID of the event to mark as read
        """
        if self.state_service is None:
            return

        try:
            result = self.state_service.update_state(
                "core",
                {
                    "table": "event_bus_events",
                    "filters": {"event_id": event_id},
                },
                {"last_read_at": datetime.now(UTC).isoformat()},
            )

            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                logger.error(f"Failed to mark event {event_id} as read: {result}")

        except Exception as e:
            logger.error(f"Error marking event {event_id} as read: {e}")

    def cleanup_old_events(self, days: int = 7) -> int:
        """
        Clean up old events from the database.

        Args:
            days: Number of days to retain events (default: 7)

        Returns:
            Number of events deleted
        """
        if self.state_service is None:
            return 0

        try:
            from datetime import timedelta

            cutoff_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()

            result = self.state_service.delete_records(
                "core",
                {
                    "table": "event_bus_events",
                    "filters": {"created_at": {"<": cutoff_date}},
                },
            )

            if is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                data = result.get("data", {})
                deleted_count = data.get("deleted_count", 0)
                if isinstance(deleted_count, int):
                    logger.debug(f"Cleaned up {deleted_count} old events")
                    return deleted_count
                logger.error("Invalid data structure in cleanup result")
                return 0
            else:
                logger.error(f"Failed to cleanup old events: {result}")
                return 0

        except Exception as e:
            logger.error(f"Error cleaning up old events: {e}")
            return 0
