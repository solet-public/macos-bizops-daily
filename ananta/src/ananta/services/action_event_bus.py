import json
import logging
import threading
import uuid
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from queue import Queue

from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.status import is_status_match
from ananta.error_handling import ValidationError
from ananta.interfaces.bootstrappable_service_interface import (
    BootstrappableServiceInterface,
)
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.services.action_event_validator import ActionEventValidator
from ananta.services.event_persistence_manager import EventPersistenceManager
from ananta.services.state_service.bounded_read import assert_within_ceiling

# `event_bus_events` is an append-only log with no pruning anywhere in EventBus,
# so it is the one table in the 2026-08-15 read-bound repair that is NOT small by
# construction — it is unbounded by design. The two halves of get_stats are
# therefore bounded differently, and deliberately:
#
#   total_events   -> the `count` verb. A scalar from the database, exact at any
#                     table size, no rows shipped, cannot overflow.
#   the breakdowns -> require the rows themselves, because grouping is the one
#                     thing the state interface cannot yet express. There is a
#                     `count`, a `max_value` and a `min_value`, but no GROUP BY,
#                     so "events per type" cannot be pushed into SQL.
#
# Rather than ship a silently-partial breakdown, the read declares a ceiling and
# refuses past it. A partial breakdown is the worst of the options: a set of
# real-looking counts that quietly stop matching total_events, with nothing in
# the returned dict showing which number to trust.
#
# The real fix is a grouped-aggregate verb on the state interface. Until that
# exists, this is honest about its limit instead of guessing past it.
#
# The breakdown read carries `unbounded: True`. That flag is badly named for what
# it does here: it means "larger than the platform DEFAULT row bound (100)", NOT
# "no bound". The bound is the explicit limit below, and assert_within_ceiling
# makes reaching it loud. Without the opt-in, resolve_read_limit refuses any
# limit over the default outright. A per-type breakdown computed from the first
# 100 events would be a set of real-looking numbers that had quietly stopped
# matching total_events — worse than refusing.
_STATS_BREAKDOWN_CEILING = 5_000

logger = logging.getLogger(__name__)


class ActionEventType(Enum):
    ACTION_REQUESTED = "action_requested"
    ACTION_ACCEPTED = "action_accepted"
    ACTION_COMPLETED = "action_completed"
    ACTION_FAILED = "action_failed"
    ACTION_TIMEOUT = "action_timeout"
    ACTION_CORRECTION_REQUESTED = "action_correction_requested"


@dataclass
class ActionRequestEvent:
    event_id: str
    event_type: ActionEventType
    timestamp: datetime
    action_data: dict[str, object]
    source_plugin: str
    target_plugin: str | None = None
    correlation_id: str | None = None
    reply_to: str | None = None
    priority: int = 5
    timeout_seconds: int | None = None
    response_data: dict[str, object] | None = None
    error_info: dict[str, object] | None = None
    metadata: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}
        if not self.event_id:
            self.event_id = str(uuid.uuid4())

    def set_source_context(
        self,
        plugin_level: str,
        request_level: str,
        action_level: str,
        chain_depth: int = 1,
        trigger_type: str = "user_input",
        session_id: str | None = None,
        parent_action_id: str | None = None,
    ) -> None:
        if self.metadata is None:
            self.metadata = {}
        if "source_context" not in self.metadata:
            self.metadata["source_context"] = {}

        # Type narrowing: we know source_context is a dict after the above check
        source_context = self.metadata["source_context"]
        if isinstance(source_context, dict):
            source_context.update(
                {
                    "plugin_level": plugin_level,
                    "request_level": request_level,
                    "action_level": action_level,
                    "chain_depth": chain_depth,
                    "origin_timestamp": self.timestamp.isoformat(),
                    "trigger_type": trigger_type,
                    "session_id": session_id,
                    "parent_action_id": parent_action_id,
                }
            )

    def get_source_context(self) -> dict[str, object]:
        if self.metadata is None:
            return {}
        source_context = self.metadata.get("source_context", {})
        # Type narrowing: ensure we return a dict
        if isinstance(source_context, dict):
            return source_context
        return {}

    def get_originating_plugin(self) -> str | None:
        plugin_level = self.get_source_context().get("plugin_level")
        # Type narrowing: ensure we return str | None
        if isinstance(plugin_level, str):
            return plugin_level
        return None

    def increment_chain_depth(self) -> None:
        if self.metadata is not None and "source_context" in self.metadata:
            source_context = self.metadata["source_context"]
            # Type narrowing: ensure source_context is a dict
            if isinstance(source_context, dict):
                current_depth = source_context.get("chain_depth", 0)
                # Type narrowing: ensure current_depth is an int
                if isinstance(current_depth, int):
                    source_context["chain_depth"] = current_depth + 1
                else:
                    source_context["chain_depth"] = 1

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["event_type"] = self.event_type.value
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ActionRequestEvent":
        data_copy = data.copy()

        event_type_obj = cls._parse_event_type(data_copy)
        timestamp_obj = cls._parse_timestamp(data_copy)

        return cls(
            event_id=cls._narrow_str(data_copy.get("event_id", ""), ""),
            event_type=event_type_obj,
            timestamp=timestamp_obj,
            action_data=cls._narrow_dict(data_copy.get("action_data", {}), {}),
            source_plugin=cls._narrow_str(data_copy.get("source_plugin", ""), ""),
            target_plugin=cls._narrow_optional_str(data_copy.get("target_plugin")),
            correlation_id=cls._narrow_optional_str(data_copy.get("correlation_id")),
            reply_to=cls._narrow_optional_str(data_copy.get("reply_to")),
            priority=cls._narrow_int(data_copy.get("priority", 5), 5),
            timeout_seconds=cls._narrow_optional_int(data_copy.get("timeout_seconds")),
            response_data=cls._narrow_optional_dict(data_copy.get("response_data")),
            error_info=cls._narrow_optional_dict(data_copy.get("error_info")),
            metadata=cls._narrow_optional_dict(data_copy.get("metadata")),
        )

    @staticmethod
    def _parse_event_type(data: dict[str, object]) -> ActionEventType:
        """Parse event_type from data dict."""
        event_type_value = data.get("event_type")
        if isinstance(event_type_value, str):
            return ActionEventType(event_type_value)
        if isinstance(event_type_value, ActionEventType):
            return event_type_value
        raise ValueError("event_type must be ActionEventType or string")

    @staticmethod
    def _parse_timestamp(data: dict[str, object]) -> datetime:
        """Parse timestamp from data dict."""
        timestamp_value = data.get("timestamp")
        if isinstance(timestamp_value, str):
            return datetime.fromisoformat(timestamp_value)
        if isinstance(timestamp_value, datetime):
            return timestamp_value
        raise ValueError("timestamp must be datetime or ISO string")

    @staticmethod
    def _narrow_str(value: object, default: str) -> str:
        """Narrow value to string."""
        return value if isinstance(value, str) else default

    @staticmethod
    def _narrow_optional_str(value: object) -> str | None:
        """Narrow value to optional string."""
        return value if isinstance(value, str) else None

    @staticmethod
    def _narrow_int(value: object, default: int) -> int:
        """Narrow value to int."""
        return value if isinstance(value, int) else default

    @staticmethod
    def _narrow_optional_int(value: object) -> int | None:
        """Narrow value to optional int."""
        return value if isinstance(value, int) else None

    @staticmethod
    def _narrow_dict(value: object, default: dict[str, object]) -> dict[str, object]:
        """Narrow value to dict."""
        return value if isinstance(value, dict) else default

    @staticmethod
    def _narrow_optional_dict(value: object) -> dict[str, object] | None:
        """Narrow value to optional dict."""
        return value if isinstance(value, dict) else None


def create_action_correction_event(
    action_name: str,
    original_action_data: dict[str, object],
    validation_error: str,
    source_plugin: str,
    target_plugin: str,
    suggested_actions: list[str] | None = None,
    correction_attempt: int = 1,
    original_context: dict[str, object] | None = None,
) -> ActionRequestEvent:
    """
    Helper function to create an ActionCorrectionEvent for routing invalid actions back to inference providers.
    """
    correction_data: dict[str, object] = {
        "action_name": action_name,
        "original_action": original_action_data,
        "validation_error": validation_error,
        "suggested_actions": suggested_actions or [],
        "correction_attempt": correction_attempt,
        "original_context": original_context or {},
    }

    error_info: dict[str, object] = {
        "error_type": "validation_failure",
        "validation_error": validation_error,
        "correction_attempt": correction_attempt,
    }

    return ActionRequestEvent(
        event_id=str(uuid.uuid4()),
        event_type=ActionEventType.ACTION_CORRECTION_REQUESTED,
        timestamp=datetime.now(UTC),
        action_data=correction_data,
        source_plugin=source_plugin,
        target_plugin=target_plugin,
        error_info=error_info,
        priority=3,  # High priority for correction requests
        metadata={
            "correction_request": True,
            "original_action_name": action_name,
            "correction_attempt": correction_attempt,
        },
    )


@dataclass
class SubscriptionInfo:
    subscription_id: str
    event_types: set[ActionEventType]
    callback: Callable[[ActionRequestEvent], None]
    plugin_filter: str | None
    created_at: datetime


class ActionEventBus(BootstrappableServiceInterface):
    @abstractmethod
    def publish(self, event: ActionRequestEvent) -> bool:
        pass

    @abstractmethod
    def subscribe(
        self,
        event_types: set[ActionEventType],
        callback: Callable[[ActionRequestEvent], None],
        plugin_filter: str | None = None,
    ) -> str:
        pass

    @abstractmethod
    def unsubscribe(self, subscription_id: str) -> bool:
        pass

    @abstractmethod
    def get_pending_events(self, since: datetime | None = None) -> list[ActionRequestEvent]:
        pass

    @abstractmethod
    def start(self) -> None:
        pass

    @abstractmethod
    def stop(self) -> None:
        pass


class EventBus(ActionEventBus):
    def __init__(
        self,
        plugin_manager: object | None = None,
        state_service: StateServiceProtocol | None = None,
        event_ttl_hours: int = 24,
    ):
        import logging

        logging.getLogger("ananta.services.action_event_bus")

        self.state_service = state_service
        self.event_ttl_hours = event_ttl_hours
        self._subscribers: dict[str, SubscriptionInfo] = {}
        self._lock = threading.RLock()
        self._event_queue: Queue[ActionRequestEvent | None] = Queue()
        self._running = False
        self._worker_thread: threading.Thread | None = None
        self._validator = ActionEventValidator()  # EXTRACTED: Event validation service
        # Type narrowing for Protocol compatibility
        state_service_arg: object = state_service
        self._persistence_manager = EventPersistenceManager(state_service_arg)  # type: ignore[arg-type]

        # Initialize via BootstrappableServiceInterface pattern
        super().__init__(plugin_manager)

    def _init_bootstrap(self) -> None:
        import logging

        logging.getLogger("ananta.services.action_event_bus")

        # In-memory storage for bootstrap mode
        self.memory_events: list[ActionRequestEvent] = []
        self.memory_subscriptions: dict[str, SubscriptionInfo] = {}
        self.schema_initialized = False

    def _init_plugin(self) -> None:
        import logging

        logging.getLogger("ananta.services.action_event_bus")

        # IMPORTANT: Do NOT initialize schema during transition to avoid circular dependency deadlock
        # The same issue that affected StateService: calling state_service.create_schema() during
        # transition creates a deadlock when the state management plugin is still initializing.
        # Schema will be initialized on first actual use instead.
        self.schema_initialized = False

    def _capture_bootstrap_state(self) -> dict[str, object]:
        return {
            "events": [event.to_dict() for event in self.memory_events],
            "subscriptions": {
                sub_id: {
                    "event_types": [et.value for et in sub.event_types],
                    "plugin_filter": sub.plugin_filter,
                    "created_at": sub.created_at.isoformat(),
                }
                for sub_id, sub in self.memory_subscriptions.items()
            },
        }

    def _restore_bootstrap_data(self, data: dict[str, object]) -> None:
        import logging

        logging.getLogger("ananta.services.action_event_bus")

        # TODO: Implement bootstrap data restoration to replay events from data
        # Restore events to database via operation replay
        # Subscriptions need to be re-registered by plugins after transition
        _ = data  # Acknowledge parameter is part of public API

    def _verify_schema_initialized(self) -> None:
        """Verify schema is initialized. Fails fast if not."""
        if not self.bootstrap_mode:
            self._persistence_manager._verify_schema_initialized()

    def start(self) -> None:
        self._log_operation("start")

        with self._lock:
            if self._running:
                return

            self._running = True

            if not self.bootstrap_mode:
                # Initialize schema at startup (explicit, not lazy)
                self._persistence_manager.initialize_schema()

                self._worker_thread = threading.Thread(
                    target=self._event_worker, name="ActionEventBus-Worker", daemon=True
                )
                self._worker_thread.start()
                self._load_pending_events_from_persistence()

    def _load_pending_events_from_persistence(self) -> None:
        """Load pending events from database - delegated to EventPersistenceManager."""
        import logging

        _logger = logging.getLogger("ananta.services.action_event_bus")

        events = self._persistence_manager.load_pending_events_from_database()
        for event in events:
            self._event_queue.put(event)

        logger.debug(f"Loaded {len(events)} events from persistence into event queue")

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return

            self._running = False
            self._event_queue.put(None)

            if self._worker_thread:
                self._worker_thread.join(timeout=5.0)
                self._worker_thread = None

    def publish(self, event: ActionRequestEvent) -> bool:
        self._log_operation("publish", event)

        try:
            errors = self._validator.validate_event(event)
            if errors:
                raise ValidationError(f"Event validation failed: {', '.join(errors)}")

            if not self._validator.validate_plugin_auth(event, event.source_plugin):
                raise ValidationError(f"Plugin {event.source_plugin} not authorized for this event")

            if not self._validator.check_rate_limit(event.source_plugin):
                raise ValidationError(f"Rate limit exceeded for plugin {event.source_plugin}")

            if self.bootstrap_mode:
                # Store in memory for bootstrap
                self.memory_events.append(event)
                # Dispatch to memory subscriptions
                self._dispatch_event_bootstrap(event)
            else:
                # Persist to database and dispatch
                self._persistence_manager.persist_event(event)
                self._event_queue.put(event)

            return True

        except Exception:
            return False

    def _persist_event(self, event: ActionRequestEvent) -> None:
        """Persist an event to the database - delegated to EventPersistenceManager."""
        self._persistence_manager.persist_event(event)

    def subscribe(
        self,
        event_types: set[ActionEventType],
        callback: Callable[[ActionRequestEvent], None],
        plugin_filter: str | None = None,
    ) -> str:
        self._log_operation("subscribe", event_types, plugin_filter)

        subscription_id = str(uuid.uuid4())

        with self._lock:
            subscription_info = SubscriptionInfo(
                subscription_id=subscription_id,
                event_types=event_types,
                callback=callback,
                plugin_filter=plugin_filter,
                created_at=datetime.now(UTC),
            )

            if self.bootstrap_mode:
                self.memory_subscriptions[subscription_id] = subscription_info
            else:
                self._subscribers[subscription_id] = subscription_info

        return subscription_id

    def unsubscribe(self, subscription_id: str) -> bool:
        self._log_operation("unsubscribe", subscription_id)

        with self._lock:
            if self.bootstrap_mode:
                return self.memory_subscriptions.pop(subscription_id, None) is not None
            else:
                return self._subscribers.pop(subscription_id, None) is not None

    def get_pending_events(self, since: datetime | None = None) -> list[ActionRequestEvent]:
        """
        Get pending events with optional timestamp filtering.

        REFACTORED: Extracted helper methods to reduce complexity from C(14).
        """
        # Phase 1: Handle bootstrap mode
        if self.bootstrap_mode:
            return self._get_bootstrap_events(since)

        # Phase 2: Prepare database query
        since_timestamp = self._prepare_since_timestamp(since)

        # Phase 3: Execute database query and build events
        try:
            return self._fetch_and_build_events(since_timestamp)
        except Exception as e:
            raise Exception(f"Failed to get pending events: {e}") from e

    def _get_bootstrap_events(self, since: datetime | None) -> list[ActionRequestEvent]:
        """Return memory events for bootstrap mode with optional timestamp filtering."""
        if since is None:
            return self.memory_events[:]
        else:
            return [event for event in self.memory_events if event.timestamp >= since]

    def _prepare_since_timestamp(self, since: datetime | None) -> datetime:
        """Prepare the since timestamp, defaulting to start of today if None."""
        self._verify_schema_initialized()

        if since is None:
            return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return since

    def _fetch_and_build_events(self, since: datetime) -> list[ActionRequestEvent]:
        """Fetch events from database and build ActionRequestEvent objects."""
        if self.state_service is None:
            return []

        # Fetch data from database
        result = self.state_service.read_state(
            "core",
            {
                "table": "event_bus_events",
                "filters": {"created_at": {"gt": since.isoformat()}},
                "order_by": "created_at ASC",
            },
        )

        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            raise Exception(f"Failed to read events: {result}")

        # Build event objects from records
        events = []
        data = result.get("data", {})
        records = data.get("records", [])
        if isinstance(records, list):
            for record in records:
                if isinstance(record, dict):
                    event = self._build_action_request_event(record)
                    events.append(event)

        return events

    def _build_action_request_event(self, record: dict[str, object]) -> ActionRequestEvent:
        """Build ActionRequestEvent from database record."""
        event_id = self._narrow_required_str(record["event_id"])
        event_type = ActionEventType(self._narrow_required_str(record["event_type"]))
        timestamp = datetime.fromisoformat(self._narrow_required_str(record["created_at"]))
        source_plugin = self._narrow_required_str(record["source_plugin"])

        target_plugin = self._narrow_optional_str(record.get("target_plugin"))
        correlation_id = self._narrow_optional_str(record.get("correlation_id"))

        priority = record.get("priority", 5)
        if not isinstance(priority, int):
            priority = 5

        action_data = self._parse_json_dict(record.get("action_data")) or {}
        response_data = self._parse_json_dict(record.get("response_data"))
        error_info = self._parse_json_dict(record.get("error_info"))
        metadata = self._parse_json_dict(record.get("metadata"))

        return ActionRequestEvent(
            event_id=event_id,
            event_type=event_type,
            timestamp=timestamp,
            source_plugin=source_plugin,
            target_plugin=target_plugin,
            correlation_id=correlation_id,
            priority=priority,
            action_data=action_data,
            response_data=response_data,
            error_info=error_info,
            metadata=metadata,
        )

    def _narrow_required_str(self, value: object) -> str:
        """Narrow any value to string."""
        return value if isinstance(value, str) else str(value)

    def _narrow_optional_str(self, value: object) -> str | None:
        """Narrow optional value to string or None."""
        if value is None:
            return None
        return value if isinstance(value, str) else str(value)

    def _parse_json_dict(self, value: object) -> dict[str, object] | None:
        """Parse JSON string or dict to dict, returns None if not parseable."""
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        return None

    def _event_worker(self) -> None:
        while self._running:
            try:
                event = self._event_queue.get(block=True)  # Block until event available
                if event is None:
                    break

                self._dispatch_event(event)

            except Exception:
                # Continue running even if individual event processing fails
                pass

    def _dispatch_event_bootstrap(self, event: ActionRequestEvent) -> None:
        import logging

        logger = logging.getLogger("ananta.services.action_event_bus")

        with self._lock:
            subscribers = list(self.memory_subscriptions.values())

        logger.debug(
            f"Bootstrap: Dispatching event {event.event_id} ({event.event_type.value}) from {event.source_plugin} to {len(subscribers)} subscribers"
        )

        for sub in subscribers:
            try:
                if event.event_type not in sub.event_types:
                    continue
                if sub.plugin_filter and event.source_plugin != sub.plugin_filter:
                    continue

                logger.debug(
                    f"Bootstrap: Dispatching event {event.event_id} to subscriber {sub.subscription_id}"
                )
                sub.callback(event)

            except Exception as e:
                logger.error(
                    f"Bootstrap: Failed to dispatch event {event.event_id} to subscriber {sub.subscription_id}: {e}"
                )

    def _dispatch_event(self, event: ActionRequestEvent) -> None:
        import logging

        logger = logging.getLogger("ananta.services.action_event_bus")

        with self._lock:
            subscribers = list(self._subscribers.values())

        logger.debug(
            f"Dispatching event {event.event_id} ({event.event_type.value}) from {event.source_plugin} to {len(subscribers)} subscribers"
        )

        dispatched = False
        for sub in subscribers:
            try:
                if event.event_type not in sub.event_types:
                    continue

                if sub.plugin_filter and event.source_plugin != sub.plugin_filter:
                    continue

                logger.debug(
                    f"Dispatching event {event.event_id} to subscriber {sub.subscription_id}"
                )
                sub.callback(event)
                dispatched = True

            except Exception as e:
                logger.error(
                    f"Failed to dispatch event {event.event_id} to subscriber {sub.subscription_id}: {e}"
                )

        if dispatched:
            logger.debug(f"Event {event.event_id} dispatched successfully, marking as read")
            self._mark_event_as_read(event.event_id)
        else:
            logger.error(f"Event {event.event_id} not dispatched to any subscribers")

    def _mark_event_as_read(self, event_id: str) -> None:
        """Mark an event as read in the database - delegated to EventPersistenceManager."""
        self._persistence_manager.mark_event_as_read(event_id)

    def cleanup_old_events(self) -> int:
        """Clean up old events from the database - delegated to EventPersistenceManager."""
        # Convert hours to days for EventPersistenceManager (minimum 1 day)
        days_to_retain = max(1, self.event_ttl_hours // 24)
        return self._persistence_manager.cleanup_old_events(days_to_retain)

    def get_stats(self) -> dict[str, object]:
        self._verify_schema_initialized()

        try:
            records = self._fetch_event_records()
            if records is None:
                return {"total_events": 0, "active_subscriptions": len(self._subscribers)}

            stats = self._build_stats_from_records(records, self._total_event_count())

            with self._lock:
                stats["active_subscriptions"] = len(self._subscribers)

            return stats

        except Exception as e:
            return {
                "total_events": 0,
                "events_by_type": {},
                "events_by_plugin": {},
                "active_subscriptions": 0,
                "error": str(e),
            }

    def _fetch_event_records(self) -> list[dict[str, object]] | None:
        """Fetch event records for the per-type/per-plugin breakdown."""
        if self.state_service is None:
            return None

        result = self.state_service.read_state(
            "core",
            # `unbounded` here means "larger than the DEFAULT bound", not
            # "unbounded" — see _STATS_BREAKDOWN_CEILING above.
            {
                "table": "event_bus_events",
                "limit": _STATS_BREAKDOWN_CEILING,
                "unbounded": True,
            },
        )

        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            raise Exception(f"Failed to read events for stats: {result}")

        data = result.get("data", {})
        records_raw = data.get("records", [])
        if not isinstance(records_raw, list):
            return []

        records = [r for r in records_raw if isinstance(r, dict)]
        return assert_within_ceiling(
            records,
            table="event_bus_events",
            ceiling=_STATS_BREAKDOWN_CEILING,
            reason=(
                "the per-type and per-plugin breakdowns must be computed in Python "
                "because the state interface has no grouped-aggregate verb, so this "
                "read only stays sane while the event log is small; nothing in this "
                "class prunes it."
            ),
        )

    def _total_event_count(self) -> int:
        """Exact row count for ``event_bus_events`` — a scalar, not a table read."""
        if self.state_service is None:
            return 0

        result = self.state_service.count("core", {"table": "event_bus_events"})
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            raise Exception(f"Failed to count events for stats: {result}")

        data = result.get("data", {})
        result_obj = data.get("result")
        value = result_obj.get("value") if isinstance(result_obj, dict) else None
        return value if isinstance(value, int) else 0

    def _build_stats_from_records(
        self, records: list[dict[str, object]], total_events: int
    ) -> dict[str, object]:
        """Build stats dict from event records and the authoritative total."""
        events_by_type: dict[str, int] = {}
        events_by_plugin: dict[str, int] = {}

        for record in records:
            event_type = self._get_str_field(record, "event_type", "unknown")
            source_plugin = self._get_str_field(record, "source_plugin", "unknown")

            events_by_type[event_type] = events_by_type.get(event_type, 0) + 1
            events_by_plugin[source_plugin] = events_by_plugin.get(source_plugin, 0) + 1

        return {
            # From COUNT(*), not len(records): the two agree today and would
            # diverge the moment the breakdown read were ever partial. Taking the
            # total from the scalar means the number that most callers actually
            # want stays correct independently of the breakdown's ceiling.
            "total_events": total_events,
            "events_by_type": events_by_type,
            "events_by_plugin": events_by_plugin,
        }

    def _get_str_field(self, record: dict[str, object], field: str, default: str) -> str:
        """Get string field from record with default."""
        value = record.get(field, default)
        return value if isinstance(value, str) else default
