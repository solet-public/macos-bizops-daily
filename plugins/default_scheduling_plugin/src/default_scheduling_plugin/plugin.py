import datetime
import logging
from datetime import UTC
from typing import Any, TypedDict, cast

from ananta.core.actions.action_metadata import (
    ContextHandling,
    ErrorCase,
    InvocationExample,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    TypicalWorkflow,
    UsageGuidance,
    platform_process,
)
from ananta.core.config.config_provider import ConfigProvider
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.decorators import service_lifecycle
from ananta.core.plugins.plugin_base import ServicePlugin
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.error_handling import PluginError
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)
from ananta.interfaces.state_aware_plugin import StateAwarePlugin
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.logging_setup import configure_plugin_logging
from ananta.types.schema_types import (
    ColumnDefinition,
    ColumnType,
    SchemaDefinition,
    TableSchema,
)

from .constants import (
    DEFAULT_HEARTBEAT_CADENCE_MINUTES,
    HEARTBEAT_FLOW_ID,
    HEARTBEAT_SESSION_ID,
    HEARTBEAT_TAG,
    KB_RETRIEVAL_AUDIT_CRON,
    KB_RETRIEVAL_AUDIT_FLOW_ID,
    KB_RETRIEVAL_AUDIT_LABEL,
    KB_RETRIEVAL_AUDIT_PROCESS_KEY,
    KB_RETRIEVAL_AUDIT_SESSION_ID,
    KB_RETRIEVAL_AUDIT_TAG,
    PLUGIN_NAME,
    SchedulerErrorCode,
    SchedulerJobStatus,
)
from .execution.action_executor import ActionExecutor
from .factories.schedule_factory import ScheduleFactory
from .models import ActionData, ScheduleData
from .protocols import ActionFactoryProtocol
from .scheduling.heartbeat_helpers import (
    check_existing_heartbeat,
    clear_stale_heartbeats,
    register_heartbeat_job,
)
from .scheduling.scheduler_manager import SchedulerManager
from .storage.schedule_repository import ScheduleRepository
from .tracking.universal_job_tracker import UniversalJobTracker
from .utils.response_helpers import build_response
from .validation import (
    normalize_cron_expression,
    normalize_tags,
    validate_cron_action_def,
    validate_cron_expression,
)

RELOAD_SAFE = True


# TypedDicts for structured parameters
class StateContext(TypedDict, total=False):
    """Context information passed to all actions."""

    session_id: str
    flow_id: str


def _build_plugin_error_response(error: PluginError) -> dict[str, Any]:
    """Preserve a deliberate plugin error code at the process boundary."""
    return build_response(
        ActionStatus.ERROR.value,
        {},
        {
            "type": error.error_type,
            "code": error.error_code,
            "message": error.message,
            "plugin_name": PLUGIN_NAME,
        },
    )


class SchedulingPlugin(ServicePlugin, StateAwarePlugin, EdgeProcessProvider):
    def __init__(self) -> None:
        super().__init__()
        self.name = PLUGIN_NAME
        self.logger: logging.Logger = logging.getLogger(self.name)
        self.state_service: StateServiceProtocol | None = None
        self.action_factory: ActionFactoryProtocol | None = None  # type: ignore[assignment]
        self.config_provider: ConfigProvider | None = None
        self._services_started = False
        self._action_executor: ActionExecutor | None = None
        self._repository: ScheduleRepository | None = None
        self._scheduler_manager: SchedulerManager | None = None
        self._job_tracker: UniversalJobTracker | None = None
        self._memory_service: Any | None = None
        self._memory_schedules: dict[str, ScheduleData] = {}
        self._memory_next_id = 1000

    def get_default_config(self) -> dict[str, Any]:
        return {}

    def get_config_schema(self) -> dict[str, object]:
        """Declare configuration schema for the scheduling plugin.

        Returns JSON Schema for setup flow to generate UI/prompts.
        This plugin has no configurable parameters - it uses hardcoded scheduling behavior.
        """
        return {}

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        schedules_table = TableSchema(
            table_name="schedules",
            columns={
                "label": ColumnDefinition(type=ColumnType.TEXT),
                "tags": ColumnDefinition(type=ColumnType.TEXT),
                "type": ColumnDefinition(type=ColumnType.TEXT),
                "cron_expression": ColumnDefinition(type=ColumnType.TEXT),
                "run_at": ColumnDefinition(type=ColumnType.TEXT),
                "actions": ColumnDefinition(type=ColumnType.TEXT),
                "action_name": ColumnDefinition(type=ColumnType.TEXT),
                "action_parameters": ColumnDefinition(type=ColumnType.TEXT),
                "status": ColumnDefinition(type=ColumnType.TEXT),
                "error_message": ColumnDefinition(type=ColumnType.TEXT),
                "session_id": ColumnDefinition(type=ColumnType.TEXT),
                "flow_id": ColumnDefinition(type=ColumnType.TEXT),
            },
            id_prefix="sch",
        )

        return [
            SchemaDefinition(
                namespace="default_scheduling_plugin",
                tables={
                    "schedules": schedules_table,
                },
            )
        ]

    def prepare_for_readiness(self) -> None:
        """Initialize plugin. Fail-fast if dependencies unavailable.

        Uses orchestrator.get_service() to request state_service.
        """
        if not self.orchestrator_ref:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")

        APP_HOME = getattr(self.orchestrator_ref, "APP_HOME", None)
        if not APP_HOME:
            raise RuntimeError(f"{self.name}: Application directory not configured - plugin cannot initialize")

        self.config_provider = ConfigProvider(self.name, {})
        self.logger = configure_plugin_logging(APP_HOME, self.name, self.config_provider)
        self.logger.debug(f"Initializing {self.name}")

        # Request state_service via new service binding architecture
        state_service = self.orchestrator_ref.get_service("state_service")
        if not state_service:
            raise RuntimeError(f"{self.name}: state_service not available - check service_bindings.json")
        # get_service returns object - cast to StateServiceProtocol after validation
        self.set_state_service(cast(StateServiceProtocol, state_service))

        # Memory service for memory-driven scheduling
        memory_service = self.orchestrator_ref.get_service("memory_service")
        if not memory_service:
            raise RuntimeError(f"{self.name}: memory_service not available - check service_bindings.json")
        self._memory_service = memory_service

    def set_action_factory(self, action_factory: ActionFactoryProtocol) -> None:
        """ActionFactory injection method for ActionFactory-centered architecture."""
        self.action_factory = action_factory  # pyright: ignore[reportIncompatibleVariableOverride]
        self._action_executor = ActionExecutor(action_factory, self.logger)
        self.logger.debug(f"ActionFactory injected into {self.name}")
        self._start_scheduler_when_ready()

    def set_state_service(self, state_service: StateServiceProtocol) -> None:
        self.state_service = state_service
        self._repository = ScheduleRepository(state_service=state_service, namespace=self.name, logger=self.logger)
        self._job_tracker = UniversalJobTracker(state_service=state_service, plugin_name=self.name, logger=self.logger)
        self.logger.debug(f"State service injected into {self.name}")

    def _configure_scheduler(self) -> None:
        """Initialize and configure APScheduler instance."""
        # Initialize SchedulerManager if not already done
        if not self._scheduler_manager:
            self._scheduler_manager = SchedulerManager(logger=self.logger)

        # Initialize the scheduler
        self._scheduler_manager.initialize()

        # Load persisted schedules from database
        # state_service is guaranteed available (fail-fast in prepare_for_readiness)
        self._load_persisted_schedules()
        self.logger.debug("Persisted schedules loaded")

    def _register_scheduler_listeners(self) -> None:
        """Register all APScheduler event listeners."""
        if self._scheduler_manager:
            self._scheduler_manager.register_listeners()

    def _start_scheduler_when_ready(self) -> bool:
        """Start APScheduler once lifecycle startup and action injection both completed.

        The platform starts service plugins before ``init_actions`` injects the
        ActionFactory. Persisted jobs may be due immediately, so starting
        APScheduler during that interval would let a callback observe an absent
        ActionExecutor. Either ordering is safe: the lifecycle marks the
        service started here, and ActionFactory injection retries the same
        idempotent helper after constructing the executor.
        """
        if not self._services_started or not self._action_executor:
            return False

        if not self._scheduler_manager:
            raise RuntimeError(f"{self.name}: scheduler manager not configured")

        if self._scheduler_manager.scheduler and self._scheduler_manager.scheduler.running:
            return True

        self._scheduler_manager.start()
        return True

    @service_lifecycle(operation="start")
    async def start_services(self) -> ActionResult:
        """Start scheduling service - called by platform after Phase 3.

        Returns:
            ActionResult with status and optional error details
        """
        # Idempotency guard
        if self._services_started:
            self._start_scheduler_when_ready()
            return {
                "action_status": "completed",
                "data": {"message": "Service already running"},
                "actions": [],
                "error": None,
                "timestamp": datetime.datetime.now(UTC).isoformat(),
            }

        self.logger.debug(f"Starting {self.name} service")
        try:
            # Configure scheduler and load persisted schedules
            self._configure_scheduler()

            # Register event listeners for monitoring
            self._register_scheduler_listeners()

            # Mark the service ready after it has configured and restored its
            # schedules.  Startup's readiness check runs before init_actions;
            # SchedulerManager records separately whether callbacks may run.
            self._services_started = True
            self._service_started_at = datetime.datetime.now(UTC).isoformat()
            self._service_error = None

            # APScheduler itself waits until init_actions supplies an
            # ActionFactory. The same helper is also called by
            # set_action_factory, covering either ordering.
            self._start_scheduler_when_ready()

            return {
                "action_status": "completed",
                "data": {
                    "message": "Service started successfully",
                    "started_at": self._service_started_at,
                },
                "actions": [],
                "error": None,
                "timestamp": datetime.datetime.now(UTC).isoformat(),
            }

        except Exception as e:
            self._services_started = False
            self._service_started_at = None
            self.logger.critical(f"Failed to start scheduling service: {e}", exc_info=True)
            return {
                "action_status": "error",
                "data": {},
                "actions": [],
                "error": {
                    "type": "PluginError",
                    "code": f"{PLUGIN_NAME}.service_start_failed",
                    "message": f"Failed to start scheduling service: {e}",
                    "details": {"exception": str(e)},
                    "severity": "CRITICAL",
                    "timestamp": datetime.datetime.now(UTC).isoformat(),
                },
                "timestamp": datetime.datetime.now(UTC).isoformat(),
            }

    @service_lifecycle(operation="stop")
    async def stop_services(self) -> ActionResult:
        """Stop scheduling service gracefully.

        Returns:
            ActionResult with status and optional error details
        """
        # Idempotency guard
        if not self._services_started:
            return {
                "action_status": "completed",
                "data": {"message": "Service already stopped"},
                "actions": [],
                "error": None,
                "timestamp": datetime.datetime.now(UTC).isoformat(),
            }

        self.logger.debug(f"Stopping {self.name} service")
        try:
            if self._scheduler_manager:
                self._scheduler_manager.stop(wait=True)  # Wait for running jobs to complete

            # Mark as stopped
            self._services_started = False
            self._service_started_at = None

            return {
                "action_status": "completed",
                "data": {"message": "Service stopped successfully"},
                "actions": [],
                "error": None,
                "timestamp": datetime.datetime.now(UTC).isoformat(),
            }

        except Exception as e:
            self.logger.error(f"Error stopping scheduling service: {e}", exc_info=True)
            return {
                "action_status": "error",
                "data": {},
                "actions": [],
                "error": {
                    "type": "PluginError",
                    "code": f"{PLUGIN_NAME}.service_stop_failed",
                    "message": f"Error stopping scheduling service: {e}",
                    "details": {"exception": str(e)},
                    "severity": "ERROR",
                    "timestamp": datetime.datetime.now(UTC).isoformat(),
                },
                "timestamp": datetime.datetime.now(UTC).isoformat(),
            }

    def set_active(self, active: bool) -> None:
        """Pause/resume APScheduler when color-active state flips (L3 Slice D).

        Inactive color: APScheduler.pause() — no jobs fire. The scheduler
        thread stays alive and the schedule store keeps state, so resuming
        is instant. M6 auto-summarize cron and pass-2 ingest are scheduled
        actions; pausing the scheduler quiesces them transitively.

        Active color: APScheduler.resume() — jobs fire again. No-op when
        the scheduler isn't started yet (start_services will start active).
        The active/paused flag lives in APScheduler's ``state`` enum — the
        plugin does not duplicate it as a separate instance attribute.
        """
        if self._scheduler_manager is None:
            return
        if active:
            self._scheduler_manager.resume()
        else:
            self._scheduler_manager.pause()

    def _update_universal_schedule_job(self, schedule_id: str, updates: dict[str, Any]) -> bool:
        """Update a schedule job in the universal asynchronous_jobs table."""
        if not self._job_tracker:
            self.logger.error("UniversalJobTracker not initialized")
            return False
        return self._job_tracker.update_job(schedule_id, updates)

    # Helper methods for state management (Session 4 refactoring)
    def _load_schedules(self) -> dict[str, Any]:
        """Load schedules from repository.

        Returns:
            Dict with 'next_id' and 'scheduled_actions' keys.
            Note: scheduled_actions values are dicts (converted from ScheduleData models)
        """
        scheduled_actions: dict[str, Any] = {}

        if self._repository:
            schedule_models = self._repository.load_all_schedules()
            for schedule_id, schedule in schedule_models.items():
                scheduled_actions[schedule_id] = schedule.model_dump()

        # Include in-memory schedules (used for test harness or DB fallback)
        for schedule_id, schedule in self._memory_schedules.items():
            if schedule_id not in scheduled_actions:
                scheduled_actions[schedule_id] = schedule.model_dump()

        if not scheduled_actions:
            return {"next_id": self._memory_next_id, "scheduled_actions": {}}

        next_id = 1000
        for schedule_id_str in scheduled_actions.keys():
            try:
                schedule_num = int(schedule_id_str)
                if schedule_num >= next_id:
                    next_id = schedule_num + 1
            except (ValueError, TypeError):
                continue

        self._memory_next_id = max(self._memory_next_id, next_id)
        return {"next_id": next_id, "scheduled_actions": scheduled_actions}

    def _save_schedule_in_memory(self, schedule_data: ScheduleData) -> str:
        """Persist schedule in local memory store (used for tests/fallback)."""
        schedule_id = schedule_data.id
        if schedule_id and schedule_id.isdigit():
            self._memory_next_id = max(self._memory_next_id, int(schedule_id) + 1)
        else:
            schedule_id = str(self._memory_next_id)
            self._memory_next_id += 1

        schedule_copy = schedule_data.model_copy(deep=True)
        schedule_copy.id = schedule_id
        self._memory_schedules[schedule_id] = schedule_copy
        return schedule_id

    def _save_schedule(self, schedule_data: ScheduleData) -> str | None:
        """Save schedule and return generated ID.

        Args:
            schedule_data: ScheduleData model to persist

        Returns:
            Generated ID if successful, None otherwise
        """
        schedule_id: str | None = None

        if self._repository:
            schedule_id = self._repository.save_schedule(schedule_data)
            if not schedule_id:
                self.logger.error("Failed to persist schedule via repository; falling back to in-memory store")

        if schedule_id:
            return schedule_id

        return self._save_schedule_in_memory(schedule_data)

    def _delete_schedule(self, schedule_id: str) -> bool:
        """Delete schedule using repository.

        Args:
            schedule_id: ID of the schedule to delete
        """
        deleted = False
        if self._repository:
            deleted = self._repository.delete_schedule(schedule_id)
            if deleted:
                self._memory_schedules.pop(schedule_id, None)
                return True

        if schedule_id in self._memory_schedules:
            del self._memory_schedules[schedule_id]
            return True

        return deleted

    def _update_schedule_status(self, schedule_id: str, status: str, error_message: str | None = None) -> None:
        """Update schedule status using repository.

        Args:
            schedule_id: ID of the schedule to update
            status: New status value
            error_message: Optional error message if status is ERROR
        """
        updated = False
        if self._repository:
            updated = self._repository.update_schedule_status(schedule_id, status, error_message)

        if not updated and schedule_id in self._memory_schedules:
            schedule = self._memory_schedules[schedule_id].model_copy(deep=True)
            schedule.status = status
            schedule.error_message = error_message
            self._memory_schedules[schedule_id] = schedule

    def _mark_schedule_failed(self, schedule_id: str, error_message: str) -> None:
        """Mark schedule as failed with error details."""
        self._update_schedule_status(schedule_id, SchedulerJobStatus.ERROR, error_message)
        self.logger.error(f"Schedule {schedule_id} marked as error: {error_message}")

    def _execute_action(self, schedule_id: str, data: dict[str, Any]) -> None:  # type: ignore[override]
        """Execute scheduled action using ActionExecutor."""
        # Update universal job status to processing
        self._update_universal_schedule_job(schedule_id, {"status": "processing"})

        # Convert dict data to ScheduleData model for ActionExecutor
        try:
            # Convert actions from dict format to ActionData models
            actions = []
            if "actions" in data and data["actions"]:
                actions = [ActionData(**action) for action in data["actions"]]

            schedule = ScheduleData(
                id=schedule_id,  # Changed from schedule_id to id
                label=data.get("label", ""),
                tags=data.get("tags", []),
                type=data.get("type", "one_time"),
                actions=actions,
                action_name=data.get("action_name", ""),
                action_parameters=data.get("action_parameters", {}),
                run_at=data.get("run_at"),
                cron_expression=data.get("cron_expression"),
                status=data.get("status", "scheduled"),
                session_id=data.get("session_id"),
                flow_id=data.get("flow_id"),
                error_message=data.get("error_message"),
            )
        except Exception as e:
            error_msg = f"Failed to parse schedule data: {e}"
            self.logger.error(f"SCHEDULER-CALLBACK-ERROR: {error_msg}")
            self._mark_schedule_failed(schedule_id, error_msg)
            self._update_universal_schedule_job(schedule_id, {"status": "failed"})
            return

        # Execute via ActionExecutor
        if not self._action_executor:
            error_msg = "ActionExecutor not initialized"
            # Deliberately NOT _mark_schedule_failed: that persists
            # schedules.status=error, which SchedulerManager.restore_schedules
            # reads to SKIP the schedule on the next process restart — turning
            # one fire landing in the set_action_factory injection gap (open
            # during every blue-green swap's startup window) into a
            # permanently dead schedule. Measured 2026-08-16 from
            # profile/data/logs/2026-08-15_profile.log: sch-2n0h8hh6jp9te
            # never fired again after exactly this write. Leaving
            # schedules.status untouched lets the schedule survive restore;
            # for a recurring schedule its own next cron tick is the retry
            # (sub-minute for high-frequency crons, up to one period for
            # low-frequency ones — a disclosed, bounded miss, not a silent
            # drop). "pending" (not "failed") on the universal job tracker
            # keeps the row inside asynchronous_jobs.status's allowed set
            # (core_schemas.py CHECK) without a schema change.
            self.logger.error(f"SCHEDULER-CALLBACK-DEFERRED: {error_msg} — schedule {schedule_id} left at its current status; retried on its own next scheduled fire instead of being dropped by a future restore")
            self._update_universal_schedule_job(schedule_id, {"status": "pending"})
            return

        success, error = self._action_executor.execute_scheduled_actions(schedule)

        if not success:
            self._mark_schedule_failed(schedule_id, error or "Unknown error")
            self._update_universal_schedule_job(schedule_id, {"status": "failed"})
            return

        # Handle completion based on schedule type
        if data["type"] == "one_time":
            self._update_schedule_status(schedule_id, SchedulerJobStatus.COMPLETED)
            self._cleanup_completed_one_time_schedules()
            self._update_universal_schedule_job(schedule_id, {"status": "completed"})
        else:
            # For recurring schedules, mark as completed for this execution
            self._update_universal_schedule_job(schedule_id, {"status": "completed"})

    def _load_persisted_schedules(self) -> None:
        """Load and restore persisted schedules from database."""
        if not self._scheduler_manager:
            return

        # Load schedules from database
        schedules = self._load_schedules()
        scheduled_actions = schedules.get("scheduled_actions", {})

        # Delegate restoration to SchedulerManager
        self._scheduler_manager.restore_schedules(
            scheduled_actions,
            self._execute_action,
            SchedulerJobStatus.SCHEDULED,
        )

    def _cleanup_completed_one_time_schedules(self) -> None:
        """Clean up completed one-time schedules using repository."""
        removed_count = 0
        if self._repository:
            removed_count = self._repository.cleanup_completed_one_time_schedules()
        else:
            for schedule_id, schedule in list(self._memory_schedules.items()):
                if schedule.type == "one_time" and schedule.status == SchedulerJobStatus.COMPLETED:
                    del self._memory_schedules[schedule_id]
                    removed_count += 1

        if removed_count > 0:
            self.logger.debug(f"Cleaned up {removed_count} completed one-time schedules")

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/create_cron_schedule.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="create_cron_schedule",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        parameters={
            "cron_expression": ParameterMetadata(
                description="Cron expression defining the schedule (e.g., '*/5 * * * *' for every 5 minutes)",
                required=True,
                type=ParameterType.STRING,
            ),
            "action_definitions": ParameterMetadata(
                description=(
                    "Non-empty list of syntactically valid action objects in the canonical {process_key, arguments} shape. Registration validates the shape and scheduled-action policy; execution resolves process_key against processes registered at fire time. Provide exactly one of action_definitions or memory_tag."
                ),
                required=False,
                type=ParameterType.LIST,
            ),
            "memory_tag": ParameterMetadata(
                description=("Memory tag to read on each run. The scheduled get_memories_by_tag action is terminal: its result is written to the action row and does not start a model turn. Provide exactly one of memory_tag or action_definitions."),
                required=False,
                type=ParameterType.STRING,
            ),
            "label": ParameterMetadata(
                description="Human-readable label for the scheduled job",
                required=False,
                type=ParameterType.STRING,
            ),
            "tags": ParameterMetadata(
                description="Tags for grouping schedules (used by clear_scheduled_actions_by_tag)",
                required=False,
                type=ParameterType.LIST,
            ),
        },
        output_type="object",
        output_description="Scheduling result with job ID and status information",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Cron job creation result with scheduling details",
            properties={
                "schedule_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Unique identifier for the scheduled job",
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Confirmation message",
                ),
            },
            examples=[
                {
                    "action_status": "COMPLETED",
                    "timestamp": "2025-10-12T09:00:00Z",
                    "data": {
                        "schedule_id": "1001",
                        "message": "Created cron schedule 1001",
                    },
                    "actions": [],
                    "error": None,
                },
                {
                    "action_status": "COMPLETED",
                    "timestamp": "2025-10-12T14:30:00Z",
                    "data": {
                        "schedule_id": "1002",
                        "message": "Created cron schedule 1002",
                    },
                    "actions": [],
                    "error": None,
                },
            ],
            usage_patterns=["Schedule recurring tasks", "Automate periodic operations"],
            chain_compatible_processes=[
                "plugin::default_scheduling_plugin::get_scheduled_jobs",
            ],
        ),
        chaining_guidance=[
            "Commonly chained with the active IO plugin's post_message to notify users of scheduled task results",
            "Used for recurring tasks that run on fixed schedules (daily, hourly, weekly, etc.)",
            "Common use cases: daily reports, periodic cleanup, scheduled monitoring, automated backups",
            "Accepts standard cron expressions with format: minute hour day month weekday (e.g., '0 9 * * *' for daily at 9am)",
            "Can schedule validated action definitions to run repeatedly at specified intervals",
            "Unlike execute_in_seconds which runs once, cron schedules repeat indefinitely until cleared",
        ],
        summary="Create recurring scheduled jobs using cron expressions for automated periodic execution",
        usage=UsageGuidance(
            when_to_use=[
                "To automate tasks that run on fixed, predictable schedules (daily, weekly, hourly, etc.)",
                "For recurring operations like daily reports, periodic cleanup, or scheduled backups",
                "When you need a job to run at specific times without manual intervention",
                "For workflows that repeat at consistent intervals indefinitely",
                "When using standard cron syntax is more natural than calculating seconds",
            ],
            when_not_to_use=[
                "For one-time delayed execution (use execute_in_seconds instead)",
                "When you need dynamic or conditional scheduling based on runtime events",
                "For immediate execution (just call the action directly)",
                "When the schedule is complex or event-driven rather than time-based",
            ],
            best_practices=[
                "Use descriptive labels to easily identify scheduled jobs later",
                "Tag related schedules for batch management with clear_scheduled_actions_by_tag",
                "Validate cron expressions before scheduling to avoid errors",
                "Test the actions array independently before scheduling them",
                "Consider timezone implications - all times are in UTC",
                "Use appropriate intervals to avoid resource contention (e.g., don't schedule heavy jobs every minute)",
            ],
        ),
        context_handling=ContextHandling(
            session_awareness="This action captures the current session_id and flow_id from state and associates them with the scheduled job for execution context",
            conversation_history="Scheduled jobs execute in their own context but can access the original session_id and flow_id for continuity",
            context_passing="Session and flow context is automatically preserved when the scheduled action executes",
        ),
        typical_workflows=[
            TypicalWorkflow(
                scenario="Schedule daily report generation every morning at 9 AM",
                steps=[
                    "Define the cron expression for daily 9 AM execution: '0 9 * * *'",
                    "Specify the actions to execute (e.g., generate report, post message)",
                    "Call create_cron_schedule with expression and actions",
                    "Receive schedule_id confirmation",
                ],
                example={
                    "user_request": "Generate a daily status report every morning at 9 AM",
                    "plan": {
                        "steps": [
                            {
                                "process_key": "plugin::default_scheduling_plugin::create_cron_schedule",
                                "reason": "Set up recurring daily report generation",
                                "arguments": {
                                    "cron_expression": "0 9 * * *",
                                    "label": "Daily Status Report",
                                    "tags": ["reports", "daily"],
                                    "actions": [
                                        {
                                            "process_key": "plugin::reporting_plugin::generate_status_report",
                                            "arguments": {
                                                "report_type": "daily",
                                                "include_metrics": True,
                                            },
                                        },
                                        {
                                            "process_key": "plugin::<active_io_plugin>::post_message",
                                            "arguments": {"message": "Daily status report has been generated and is available."},
                                        },
                                    ],
                                },
                            }
                        ]
                    },
                },
            ),
            TypicalWorkflow(
                scenario="Schedule periodic cleanup every 6 hours",
                steps=[
                    "Define the cron expression for every 6 hours: '0 */6 * * *'",
                    "Specify cleanup actions to execute",
                    "Call create_cron_schedule with the expression",
                    "Store schedule_id for later management",
                ],
                example={
                    "user_request": "Run cleanup tasks every 6 hours",
                    "plan": {
                        "steps": [
                            {
                                "process_key": "plugin::default_scheduling_plugin::create_cron_schedule",
                                "reason": "Set up recurring cleanup on 6-hour intervals",
                                "arguments": {
                                    "cron_expression": "0 */6 * * *",
                                    "label": "Periodic Cleanup Job",
                                    "tags": ["cleanup", "maintenance"],
                                    "actions": [
                                        {
                                            "process_key": "plugin::cleanup_plugin::remove_temp_files",
                                            "arguments": {"older_than_hours": 24},
                                        }
                                    ],
                                },
                            }
                        ]
                    },
                },
            ),
        ],
        complete_examples=[
            InvocationExample(
                description="Create a daily weekday schedule at 9 AM for sending reports",
                invocation={
                    "process_key": "plugin::default_scheduling_plugin::create_cron_schedule",
                    "arguments": {
                        "cron_expression": "0 9 * * 1-5",
                        "label": "Weekday Morning Report",
                        "tags": ["reports", "morning", "weekdays"],
                        "actions": [
                            {
                                "process_key": "plugin::reporting_plugin::generate_report",
                                "arguments": {"report_type": "summary"},
                            }
                        ],
                    },
                },
                response={
                    "action_status": "COMPLETED",
                    "timestamp": "2025-11-14T08:00:00Z",
                    "data": {
                        "schedule_id": "1001",
                        "message": "Created cron schedule 1001",
                    },
                    "actions": [],
                    "error": None,
                },
            ),
            InvocationExample(
                description="Create hourly monitoring schedule",
                invocation={
                    "process_key": "plugin::default_scheduling_plugin::create_cron_schedule",
                    "arguments": {
                        "cron_expression": "0 * * * *",
                        "label": "Hourly System Check",
                        "tags": ["monitoring", "system"],
                        "actions": [
                            {
                                "process_key": "plugin::monitoring_plugin::check_system_health",
                                "arguments": {"check_type": "full"},
                            }
                        ],
                    },
                },
                response={
                    "action_status": "COMPLETED",
                    "timestamp": "2025-11-14T08:05:00Z",
                    "data": {
                        "schedule_id": "1002",
                        "message": "Created cron schedule 1002",
                    },
                    "actions": [],
                    "error": None,
                },
            ),
        ],
        error_cases=[
            ErrorCase(
                condition="Invalid cron expression format",
                error_response={
                    "action_status": "ERROR",
                    "data": {},
                    "error": {
                        "type": "plugin_error",
                        "code": "scheduling.invalid_cron_expression",
                        "message": "Invalid cron expression: 0 25 * * *",
                        "plugin_name": "default_scheduling_plugin",
                    },
                },
            ),
            ErrorCase(
                condition="Missing required actions parameter",
                error_response={
                    "action_status": "ERROR",
                    "data": {},
                    "error": {
                        "type": "plugin_error",
                        "code": "scheduling.parameter_error",
                        "message": "actions parameter is required",
                        "plugin_name": "default_scheduling_plugin",
                    },
                },
            ),
            ErrorCase(
                condition="Scheduler service not available",
                error_response={
                    "action_status": "ERROR",
                    "data": {},
                    "error": {
                        "type": "plugin_error",
                        "code": "scheduling.schedule_create_error",
                        "message": "Scheduler not initialized - cannot create schedule",
                        "plugin_name": "default_scheduling_plugin",
                    },
                },
            ),
        ],
    )
    def create_cron_schedule(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            p = ScheduleFactory.extract_params(params)

            cron = normalize_cron_expression(str(p.get("cron_expression", "") or ""))

            if not validate_cron_expression(cron):
                error_msg = f"Invalid cron expression: {cron}"
                self.logger.error(error_msg)
                raise PluginError(
                    error_msg,
                    SchedulerErrorCode.INVALID_CRON_EXPRESSION,
                    plugin_name=PLUGIN_NAME,
                )

            label = p.get("label", "Untitled Schedule")
            tags = normalize_tags(p.get("tags", []))

            try:
                actions_list, legacy_action_name, legacy_action_params = ScheduleFactory.parse_actions_from_params(p)
                for action_def in actions_list:
                    validate_cron_action_def(action_def)
            except ValueError as e:
                raise PluginError(
                    str(e),
                    SchedulerErrorCode.PARAMETER_ERROR,
                    plugin_name=PLUGIN_NAME,
                ) from e

            self.logger.debug(f"Creating cron schedule: {label} with expression '{cron}'")
            # Build ScheduleData using factory
            schedule_data = ScheduleFactory.create_cron_schedule_data(
                cron_expression=cron,
                label=label,
                tags=tags,
                actions=actions_list,
                action_name=legacy_action_name,
                action_parameters=legacy_action_params,
                session_id=state.get("session_id"),
                flow_id=state.get("flow_id"),
            )

            # Save and get generated ID
            schedule_id = self._save_schedule(schedule_data)
            if not schedule_id:
                raise PluginError(
                    "Failed to save schedule",
                    SchedulerErrorCode.SCHEDULE_CREATE_ERROR,
                    plugin_name=PLUGIN_NAME,
                )

            # Build data dict for scheduler callback
            data = schedule_data.model_dump()
            data["id"] = schedule_id  # Ensure ID is in the data

            # Add to APScheduler with generated ID
            if self._scheduler_manager:
                self._scheduler_manager.add_cron_job(
                    lambda: self._execute_action(schedule_id, data),
                    cron,
                    schedule_id,
                )

            # Also track in universal asynchronous_jobs table
            if self._job_tracker:
                self._job_tracker.create_job(schedule_id, "create_cron_schedule", data)

            self.logger.debug(f"Successfully created cron schedule {schedule_id} ({label})")
            return build_response(
                ActionStatus.COMPLETED.value,
                {
                    "schedule_id": schedule_id,
                    "message": f"Created cron schedule {schedule_id}",
                },
            )

        except PluginError as e:
            self.logger.error(f"Failed to create cron schedule: {e}", exc_info=True)
            return _build_plugin_error_response(e)
        except Exception as e:
            self.logger.error(f"Failed to create cron schedule: {e}", exc_info=True)
            return build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": SchedulerErrorCode.SCHEDULE_CREATE_ERROR,
                    "message": str(e),
                    "plugin_name": PLUGIN_NAME,
                },
            )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/execute_in_seconds.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="execute_in_seconds",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        parameters={
            "seconds": ParameterMetadata(
                description="Number of seconds to wait before the wake-up fires",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "action_definitions": ParameterMetadata(
                description=(
                    "Non-empty list of syntactically valid action objects in the canonical {process_key, arguments} shape. Registration validates the shape and scheduled-action policy; execution resolves process_key against processes registered at fire time. Provide exactly one of action_definitions or memory_tag."
                ),
                required=False,
                type=ParameterType.LIST,
            ),
            "memory_tag": ParameterMetadata(
                description=("Memory tag to read after the delay. The scheduled get_memories_by_tag action is terminal: its result is written to the action row and does not start a model turn. Provide exactly one of memory_tag or action_definitions."),
                required=False,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description=("Follow-up instructions to stash as a tagged memory. Content is only valid with memory_tag; action-definition mode rejects it."),
                required=False,
                type=ParameterType.STRING,
            ),
            "tags": ParameterMetadata(
                description="Tags for grouping delayed schedules",
                required=False,
                type=ParameterType.LIST,
            ),
        },
        output_type="object",
        output_description="Scheduling result with job ID and execution time",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Delayed execution job creation result",
            properties={
                "schedule_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Unique identifier for the scheduled job",
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Confirmation message",
                ),
                "run_at": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="ISO timestamp when job will execute",
                ),
                "delay_seconds": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Delay in seconds before execution",
                ),
            },
            examples=[
                {
                    "action_status": "COMPLETED",
                    "timestamp": "2025-10-12T14:30:00Z",
                    "data": {
                        "schedule_id": "1001",
                        "message": "Scheduled in 30s",
                        "run_at": "2025-10-12T14:30:30Z",
                        "delay_seconds": 30,
                    },
                    "actions": [],
                    "error": None,
                },
                {
                    "action_status": "COMPLETED",
                    "timestamp": "2025-10-12T14:35:00Z",
                    "data": {
                        "schedule_id": "1002",
                        "message": "Scheduled in 60s",
                        "run_at": "2025-10-12T14:36:00Z",
                        "delay_seconds": 60,
                    },
                    "actions": [],
                    "error": None,
                },
            ],
            usage_patterns=[
                "Schedule delayed actions",
                "Implement timeouts and delays",
            ],
            chain_compatible_processes=[
                "plugin::default_scheduling_plugin::get_scheduled_jobs",
            ],
        ),
        chaining_guidance=[
            "Commonly chained with the active IO plugin's post_message to notify users after delays",
            "CRITICAL for Pattern 6a async monitoring workflows - enables delayed status checks",
            "Schedules full action definitions (with process_key and arguments) to execute after a delay",
            "Common pattern: schedule monitoring orchestrator at intervals (e.g., check job status every 30s)",
            "Can chain with inference_service processes for timed decision-making based on async job status",
        ],
        summary="Schedule one-time delayed execution of actions after a specified number of seconds",
        usage=UsageGuidance(
            when_to_use=[
                "For Pattern 6a async monitoring - schedule periodic status checks of long-running jobs",
                "To implement delays between sequential operations in workflows",
                "For timeout mechanisms where an action should execute after a waiting period",
                "When you need one-time execution at a future time (not recurring)",
                "To retry operations after a delay in error handling scenarios",
            ],
            when_not_to_use=[
                "For recurring schedules at fixed intervals (use create_cron_schedule instead)",
                "When immediate execution is needed (just call the action directly)",
                "For complex time-based scheduling (hour-of-day, day-of-week) - use cron",
                "When the delay is so short (< 1 second) that synchronous execution is better",
            ],
            best_practices=[
                "Use descriptive labels to track what each delayed job is for",
                "Tag monitoring jobs with the job_id being monitored for easy bulk cancellation",
                "Set reasonable delay intervals - too short wastes resources, too long delays feedback",
                "For async monitoring (Pattern 6a), typical intervals are 5-30 seconds",
                "Always handle the case where the delayed action may fail or become obsolete",
                "Consider canceling scheduled checks if the monitored job completes early",
            ],
        ),
        context_handling=ContextHandling(
            session_awareness="This action captures session_id and flow_id from the current state and associates them with the scheduled action for execution continuity",
            conversation_history="The delayed action executes in its own context but retains access to the original session_id and flow_id",
            context_passing="Session context is automatically preserved and passed to the scheduled action when it executes",
        ),
        typical_workflows=[
            TypicalWorkflow(
                scenario="Pattern 6a: Async job monitoring with delayed status checks",
                steps=[
                    "Submit a long-running asynchronous job (e.g., inference request)",
                    "Receive job_id in the response",
                    "Schedule execute_in_seconds to check status after 30 seconds",
                    "Tag the schedule with the job_id for later cancellation",
                    "When scheduled action runs, check job status and decide next step",
                ],
                example={
                    "user_request": "Monitor async inference job and report when complete",
                    "plan": {
                        "steps": [
                            {
                                "process_key": "service_interface::inference_service::process_results",
                                "reason": "Analyze latest async status and decide user-facing next step",
                                "arguments": {
                                    "model": {"temperature": 0.7, "max_tokens": 4096},
                                    "prompt": {"user": "Review async job status and determine next action"},
                                    "session_id": "session_123",
                                    "flow_id": "flow_456",
                                },
                            },
                            {
                                "process_key": "plugin::default_scheduling_plugin::execute_in_seconds",
                                "reason": "Schedule status check in 30 seconds",
                                "arguments": {
                                    "seconds": 30,
                                    "label": "Monitor inference job",
                                    "tags": ["monitor_inference_job_789"],
                                    "actions": [
                                        {
                                            "process_key": "service_interface::asynchronous_jobs::get_job_status",
                                            "arguments": {
                                                "job_id": "inference_job_789",
                                                "session_id": "session_123",
                                                "flow_id": "flow_456",
                                            },
                                        }
                                    ],
                                },
                            },
                        ]
                    },
                },
            ),
            TypicalWorkflow(
                scenario="Delayed notification after operation completes",
                steps=[
                    "Start a background operation",
                    "Schedule a notification to the user after expected completion time",
                    "When delay expires, post message to user about completion",
                ],
                example={
                    "user_request": "Process files and notify me in 5 minutes",
                    "plan": {
                        "steps": [
                            {
                                "process_key": "plugin::file_processor::process_batch",
                                "reason": "Start batch file processing",
                                "arguments": {"file_count": 100},
                            },
                            {
                                "process_key": "plugin::default_scheduling_plugin::execute_in_seconds",
                                "reason": "Schedule notification after 5 minutes",
                                "arguments": {
                                    "seconds": 300,
                                    "label": "Processing completion notification",
                                    "tags": ["notification"],
                                    "actions": [
                                        {
                                            "process_key": "plugin::<active_io_plugin>::post_message",
                                            "arguments": {"message": "Your batch processing should be complete now. Check the results."},
                                        }
                                    ],
                                },
                            },
                        ]
                    },
                },
            ),
        ],
        complete_examples=[
            InvocationExample(
                description="Schedule status check 30 seconds in the future for async job monitoring",
                invocation={
                    "process_key": "plugin::default_scheduling_plugin::execute_in_seconds",
                    "arguments": {
                        "seconds": 30,
                        "label": "Check inference job status",
                        "tags": ["monitor_job_12345"],
                        "actions": [
                            {
                                "process_key": "service_interface::asynchronous_jobs::get_job_status",
                                "arguments": {
                                    "job_id": "inference_job_12345",
                                    "session_id": "session_abc",
                                    "flow_id": "flow_xyz",
                                },
                            }
                        ],
                    },
                },
                response={
                    "action_status": "COMPLETED",
                    "timestamp": "2025-11-14T10:00:00Z",
                    "data": {
                        "schedule_id": "1001",
                        "message": "Scheduled in 30s",
                        "run_at": "2025-11-14T10:00:30Z",
                        "delay_seconds": 30,
                    },
                    "actions": [],
                    "error": None,
                },
            ),
            InvocationExample(
                description="Schedule delayed user notification",
                invocation={
                    "process_key": "plugin::default_scheduling_plugin::execute_in_seconds",
                    "arguments": {
                        "seconds": 60,
                        "label": "Remind user to check results",
                        "tags": ["reminder"],
                        "actions": [
                            {
                                "process_key": "plugin::<active_io_plugin>::post_message",
                                "arguments": {"message": "Your requested operation should be complete now."},
                            }
                        ],
                    },
                },
                response={
                    "action_status": "COMPLETED",
                    "timestamp": "2025-11-14T10:05:00Z",
                    "data": {
                        "schedule_id": "1002",
                        "message": "Scheduled in 60s",
                        "run_at": "2025-11-14T10:06:00Z",
                        "delay_seconds": 60,
                    },
                    "actions": [],
                    "error": None,
                },
            ),
        ],
        error_cases=[
            ErrorCase(
                condition="Invalid seconds value (zero or negative)",
                error_response={
                    "action_status": "ERROR",
                    "data": {},
                    "error": {
                        "type": "plugin_error",
                        "code": "scheduling.parameter_error",
                        "message": "Seconds must be > 0",
                        "plugin_name": "default_scheduling_plugin",
                    },
                },
            ),
            ErrorCase(
                condition="Missing required actions parameter",
                error_response={
                    "action_status": "ERROR",
                    "data": {},
                    "error": {
                        "type": "plugin_error",
                        "code": "scheduling.parameter_error",
                        "message": "actions parameter is required",
                        "plugin_name": "default_scheduling_plugin",
                    },
                },
            ),
            ErrorCase(
                condition="Scheduler service not initialized",
                error_response={
                    "action_status": "ERROR",
                    "data": {},
                    "error": {
                        "type": "plugin_error",
                        "code": "scheduling.schedule_create_error",
                        "message": "Failed to save schedule",
                        "plugin_name": "default_scheduling_plugin",
                    },
                },
            ),
        ],
    )
    def execute_in_seconds(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            p = ScheduleFactory.extract_params(params)
            seconds = p.get("seconds", 0)
            if seconds <= 0:
                error_msg = "Seconds must be > 0"
                self.logger.error(error_msg)
                raise PluginError(
                    error_msg,
                    SchedulerErrorCode.PARAMETER_ERROR,
                    plugin_name=PLUGIN_NAME,
                )

            # Parse the full action/memory/content contract before any memory or
            # schedule write, then apply the same scheduled-action validator used
            # by recurring registration and both restoration paths.
            try:
                actions_list, legacy_action_name, legacy_action_params = ScheduleFactory.parse_delayed_actions_from_params(p)
                for action_def in actions_list:
                    validate_cron_action_def(action_def)
            except ValueError as e:
                raise PluginError(
                    str(e),
                    SchedulerErrorCode.PARAMETER_ERROR,
                    plugin_name=PLUGIN_NAME,
                ) from e

            label = p.get("label", "Untitled Schedule")
            run_at = datetime.datetime.now(UTC) + datetime.timedelta(seconds=seconds)
            tags = normalize_tags(p.get("tags", []))

            content = p.get("content")
            if content:
                memory_tag = legacy_action_params["tag"]
                assert self._memory_service is not None  # guaranteed by readiness
                self._memory_service.remember(
                    content=content,
                    tags=[memory_tag],
                    session_id=state.get("session_id"),
                )
                self.logger.info(f"Stashed memory for scheduled recall: tag={memory_tag!r}")

            self.logger.debug(f"Creating delayed execution: {label} in {seconds}s (at {run_at.isoformat()})")

            # Build ScheduleData using factory
            schedule_data = ScheduleFactory.create_one_time_schedule_data(
                run_at=run_at,
                label=label,
                tags=tags,
                actions=actions_list,
                action_name=legacy_action_name,
                action_parameters=legacy_action_params,
                session_id=state.get("session_id"),
                flow_id=state.get("flow_id"),
            )

            # Save and get generated ID
            schedule_id = self._save_schedule(schedule_data)
            if not schedule_id:
                raise PluginError(
                    "Failed to save schedule",
                    SchedulerErrorCode.SCHEDULE_CREATE_ERROR,
                    plugin_name=PLUGIN_NAME,
                )

            # Build data dict for scheduler callback
            data = schedule_data.model_dump()
            data["id"] = schedule_id  # Ensure ID is in the data

            # Add to APScheduler with generated ID
            if self._scheduler_manager:
                self._scheduler_manager.add_one_time_job(
                    lambda: self._execute_action(schedule_id, data),
                    run_at,
                    schedule_id,
                )

            self.logger.debug(f"Successfully scheduled {schedule_id} ({label}) for execution in {seconds}s")
            return build_response(
                ActionStatus.COMPLETED.value,
                {
                    "schedule_id": schedule_id,
                    "message": f"Scheduled in {seconds}s",
                    "run_at": run_at.isoformat(),
                    "delay_seconds": seconds,
                },
            )

        except PluginError as e:
            self.logger.error(f"Failed to schedule delayed execution: {e}", exc_info=True)
            return _build_plugin_error_response(e)
        except Exception as e:
            self.logger.error(f"Failed to schedule delayed execution: {e}", exc_info=True)
            return build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": SchedulerErrorCode.SCHEDULE_CREATE_ERROR,
                    "message": str(e),
                    "plugin_name": PLUGIN_NAME,
                },
            )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/clear_scheduled_action.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="clear_scheduled_action",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        parameters={
            "schedule_id": ParameterMetadata(
                description="Unique identifier of the scheduled job to cancel",
                required=True,
                type=ParameterType.STRING,
            )
        },
        output_type="object",
        output_description="Cancellation result with status information",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Job cancellation result",
            properties={
                "schedule_id": ParameterMetadata(type=ParameterType.STRING, description="ID of the cancelled job"),
                "cancelled": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether cancellation was successful",
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status message about cancellation",
                ),
            },
            examples=[
                {
                    "action_status": "COMPLETED",
                    "timestamp": "2025-07-19T11:30:00Z",
                    "data": {"schedule_id": "1001", "message": "Cleared 1001"},
                    "actions": [],
                    "error": None,
                },
                {
                    "action_status": "ERROR",
                    "timestamp": "2025-07-19T11:35:00Z",
                    "data": {},
                    "actions": [],
                    "error": {
                        "type": "plugin_error",
                        "code": "scheduling.schedule_not_found",
                        "message": "Not found: 9999",
                        "plugin_name": "default_scheduling_plugin",
                    },
                },
            ],
            usage_patterns=[
                "Cancel unwanted scheduled tasks",
                "Clean up scheduled jobs",
            ],
            chain_compatible_processes=[],
        ),
        chaining_guidance=[
            "Commonly chained with the active IO plugin's post_message to confirm cancellation to users",
            "Cancels scheduled jobs by job_id before they execute",
            "Critical for Pattern 6a: when monitored job completes before next monitor check, cancel pending monitor schedules",
            "Prevents unnecessary scheduled executions after completion or error conditions",
            "Typically follows job completion or error detection to clean up pending schedules",
        ],
        summary="Cancel and remove a specific scheduled job by its unique schedule ID",
        usage=UsageGuidance(
            when_to_use=[
                "When an async job completes early and monitoring schedules are no longer needed (Pattern 6a cleanup)",
                "To cancel a scheduled job that is no longer relevant due to changed conditions",
                "When user explicitly requests cancellation of a specific scheduled task",
                "To prevent duplicate or redundant scheduled actions from executing",
                "For cleanup after errors where pending scheduled actions should not run",
            ],
            when_not_to_use=[
                "To cancel multiple jobs at once (use clear_scheduled_actions_by_tag for bulk operations)",
                "To cancel jobs that have already executed (they auto-cleanup)",
                "When you don't have the schedule_id (use clear_scheduled_actions_by_tag with tags instead)",
            ],
            best_practices=[
                "Always store schedule_id from execute_in_seconds or create_cron_schedule responses for later cancellation",
                "Cancel monitoring schedules as soon as the monitored job completes to save resources",
                "Handle schedule_not_found errors gracefully - job may have already executed or been cancelled",
                "For Pattern 6a, cancel monitoring schedules when job status becomes terminal (completed/failed)",
                "Consider using tags for related schedules to enable batch cancellation with clear_scheduled_actions_by_tag",
            ],
        ),
        context_handling=ContextHandling(
            session_awareness="This action operates on scheduled jobs across all sessions - schedule_id is globally unique",
            conversation_history="No conversation context needed - operates purely on schedule_id",
            context_passing="No context is passed - cancellation is idempotent based on schedule_id alone",
        ),
        typical_workflows=[
            TypicalWorkflow(
                scenario="Pattern 6a: Cancel monitoring schedule when async job completes",
                steps=[
                    "Monitor async job status via scheduled checks",
                    "Detect job has completed (status: completed or failed)",
                    "Cancel remaining scheduled monitoring checks using their schedule_id",
                    "Post completion message to user",
                ],
                example={
                    "scenario": "Async job completes before next scheduled check",
                    "plan": {
                        "steps": [
                            {
                                "process_key": "service_interface::asynchronous_jobs::get_job_status",
                                "reason": "Check current job status",
                                "arguments": {
                                    "job_id": "inference_job_789",
                                    "session_id": "session_123",
                                    "flow_id": "flow_456",
                                },
                            },
                            {
                                "process_key": "plugin::default_scheduling_plugin::clear_scheduled_action",
                                "reason": "Job is complete - cancel next scheduled check",
                                "arguments": {"schedule_id": "1001"},
                            },
                            {
                                "process_key": "plugin::<active_io_plugin>::post_message",
                                "reason": "Notify user of job completion",
                                "arguments": {"message": "Your inference job has completed successfully."},
                            },
                        ]
                    },
                },
            ),
            TypicalWorkflow(
                scenario="User requests cancellation of a scheduled task",
                steps=[
                    "User provides schedule_id to cancel",
                    "Call clear_scheduled_action with the schedule_id",
                    "Confirm cancellation to user",
                ],
                example={
                    "user_request": "Cancel the scheduled job 1002",
                    "plan": {
                        "steps": [
                            {
                                "process_key": "plugin::default_scheduling_plugin::clear_scheduled_action",
                                "reason": "Cancel the requested scheduled job",
                                "arguments": {"schedule_id": "1002"},
                            },
                            {
                                "process_key": "plugin::<active_io_plugin>::post_message",
                                "reason": "Confirm cancellation to user",
                                "arguments": {"message": "Scheduled job 1002 has been cancelled successfully."},
                            },
                        ]
                    },
                },
            ),
        ],
        complete_examples=[
            InvocationExample(
                description="Cancel a specific scheduled monitoring check",
                invocation={
                    "process_key": "plugin::default_scheduling_plugin::clear_scheduled_action",
                    "arguments": {"schedule_id": "1001"},
                },
                response={
                    "action_status": "COMPLETED",
                    "timestamp": "2025-11-14T10:15:00Z",
                    "data": {"schedule_id": "1001", "message": "Cleared 1001"},
                    "actions": [],
                    "error": None,
                },
            ),
            InvocationExample(
                description="Cancel a delayed notification",
                invocation={
                    "process_key": "plugin::default_scheduling_plugin::clear_scheduled_action",
                    "arguments": {"schedule_id": "1003"},
                },
                response={
                    "action_status": "COMPLETED",
                    "timestamp": "2025-11-14T10:20:00Z",
                    "data": {"schedule_id": "1003", "message": "Cleared 1003"},
                    "actions": [],
                    "error": None,
                },
            ),
        ],
        error_cases=[
            ErrorCase(
                condition="Schedule ID not found (already executed or never existed)",
                error_response={
                    "action_status": "ERROR",
                    "data": {},
                    "error": {
                        "type": "plugin_error",
                        "code": "scheduling.schedule_not_found",
                        "message": "Not found: 9999",
                        "plugin_name": "default_scheduling_plugin",
                    },
                },
            ),
            ErrorCase(
                condition="Missing required schedule_id parameter",
                error_response={
                    "action_status": "ERROR",
                    "data": {},
                    "error": {
                        "type": "plugin_error",
                        "code": "scheduling.parameter_error",
                        "message": "schedule_id required",
                        "plugin_name": "default_scheduling_plugin",
                    },
                },
            ),
            ErrorCase(
                condition="Scheduler service error during cancellation",
                error_response={
                    "action_status": "ERROR",
                    "data": {},
                    "error": {
                        "type": "plugin_error",
                        "code": "scheduling.schedule_delete_error",
                        "message": "Failed to remove job from scheduler",
                        "plugin_name": "default_scheduling_plugin",
                    },
                },
            ),
        ],
    )
    def clear_scheduled_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            p = ScheduleFactory.extract_params(params)

            schedule_id = p.get("schedule_id", "")
            if not schedule_id:
                raise PluginError(
                    "schedule_id required",
                    SchedulerErrorCode.PARAMETER_ERROR,
                    plugin_name=PLUGIN_NAME,
                )

            schedules = self._load_schedules()
            scheduled_actions = schedules.get("scheduled_actions", {})
            schedule_exists = schedule_id in scheduled_actions

            # Always attempt to remove the live APScheduler job, even when the
            # DB/metadata row is already gone. Otherwise an orphaned job (row
            # hard-deleted but the in-memory job still firing) is unkillable by
            # any verb and survives until a solet restart. remove_job is
            # exception-safe (no-op when the job is absent); get_job lets us
            # report cancellation accurately for the orphan-recovery case.
            job_removed = False
            if self._scheduler_manager:
                job_removed = self._scheduler_manager.get_job(schedule_id) is not None
                self._scheduler_manager.remove_job(schedule_id)

            db_deleted = self._delete_schedule(schedule_id) if schedule_exists else False
            cancelled = db_deleted or job_removed

            message = f"Cleared {schedule_id}" if cancelled else f"Schedule {schedule_id} not found; nothing to clear"

            return build_response(
                ActionStatus.COMPLETED.value,
                {
                    "schedule_id": schedule_id,
                    "message": message,
                    "cancelled": cancelled,
                },
            )

        except Exception as e:
            return build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "plugin_error",
                    "code": SchedulerErrorCode.SCHEDULE_DELETE_ERROR,
                    "message": str(e),
                    "plugin_name": PLUGIN_NAME,
                },
            )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/clear_scheduled_actions_by_tag.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="clear_scheduled_actions_by_tag",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        parameters={
            "tag": ParameterMetadata(
                description="Tag to match for batch cancellation of scheduled jobs",
                required=True,
                type=ParameterType.STRING,
            )
        },
        output_type="object",
        output_description="Batch cancellation result with count of cleared jobs",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Batch job cancellation result",
            properties={
                "tag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Tag used for batch cancellation",
                ),
                "cleared_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of jobs successfully cancelled",
                ),
                "cleared_ids": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="List of schedule IDs that were cancelled",
                ),
            },
            examples=[
                {
                    "action_status": "COMPLETED",
                    "timestamp": "2025-10-12T09:15:00Z",
                    "data": {
                        "cleared_count": 5,
                        "cleared_ids": ["1001", "1002", "1003", "1004", "1005"],
                        "tag": "monitor_job_12345",
                    },
                    "actions": [],
                    "error": None,
                },
                {
                    "action_status": "COMPLETED",
                    "timestamp": "2025-10-12T09:20:00Z",
                    "data": {
                        "cleared_count": 0,
                        "cleared_ids": [],
                        "tag": "nonexistent_tag",
                    },
                    "actions": [],
                    "error": None,
                },
            ],
            usage_patterns=[
                "Bulk cancel related scheduled tasks",
                "Clean up jobs by category",
            ],
            chain_compatible_processes=[],
        ),
        chaining_guidance=[
            "Commonly chained with the active IO plugin's post_message to confirm bulk cancellation to users",
            "Used to cancel multiple scheduled jobs at once by tag",
            "Efficient for bulk cleanup when workflow completes or errors",
            "Pattern 6a can tag all monitor checks with job_id for bulk cancellation",
            "More efficient than clearing individual jobs when many exist",
        ],
        summary="Batch cancel and remove all scheduled jobs that match a specific tag",
        usage=UsageGuidance(
            when_to_use=[
                "For Pattern 6a: cancel ALL monitoring schedules at once when async job completes (tag with job_id)",
                "To bulk-cancel a group of related scheduled tasks in one operation",
                "When cleaning up after workflow completion or error where multiple schedules are obsolete",
                "To remove all scheduled jobs for a specific category or purpose",
                "When you don't know exact schedule_ids but have tagged jobs with a common identifier",
            ],
            when_not_to_use=[
                "To cancel a single specific job (use clear_scheduled_action for better precision)",
                "When jobs don't have tags (tag them during creation)",
                "To cancel unrelated jobs - risk of over-cancellation",
            ],
            best_practices=[
                "Tag all monitoring schedules for the same async job with a unique identifier (e.g., 'monitor_job_12345')",
                "Use descriptive, unique tags that clearly identify the job category or purpose",
                "For Pattern 6a, tag format should be 'monitor_<job_id>' for easy bulk cancellation",
                "Always check cleared_count in response to verify expected number of jobs were cancelled",
                "Consider that cleared_count of 0 is not an error - just means no matching jobs found",
                "Prefer bulk cancellation over individual when canceling 3+ related jobs",
            ],
        ),
        context_handling=ContextHandling(
            session_awareness="This action operates on scheduled jobs across all sessions - tags are globally scoped",
            conversation_history="No conversation context needed - operates purely on tag matching",
            context_passing="No context is passed - cancellation is based on tag matching across all schedules",
        ),
        typical_workflows=[
            TypicalWorkflow(
                scenario="Pattern 6a: Bulk cancel all monitoring schedules when async job completes",
                steps=[
                    "Multiple monitoring schedules were created with tag 'monitor_job_12345'",
                    "Async job completes (detected via status check)",
                    "Call clear_scheduled_actions_by_tag with 'monitor_job_12345' to cancel all pending checks",
                    "Report completion to user",
                ],
                example={
                    "scenario": "Async job completes - cleanup all monitoring schedules",
                    "plan": {
                        "steps": [
                            {
                                "process_key": "service_interface::asynchronous_jobs::get_job_status",
                                "reason": "Check async job status",
                                "arguments": {
                                    "job_id": "inference_job_789",
                                    "session_id": "session_123",
                                    "flow_id": "flow_456",
                                },
                            },
                            {
                                "process_key": "plugin::default_scheduling_plugin::clear_scheduled_actions_by_tag",
                                "reason": "Job complete - cancel all pending monitoring schedules",
                                "arguments": {"tag": "monitor_job_789"},
                            },
                            {
                                "process_key": "plugin::<active_io_plugin>::post_message",
                                "reason": "Notify user of completion and cleanup",
                                "arguments": {"message": "Inference job completed. Cancelled 3 pending monitoring schedules."},
                            },
                        ]
                    },
                },
            ),
            TypicalWorkflow(
                scenario="Cancel all scheduled tasks for a specific workflow category",
                steps=[
                    "User requests cancellation of all jobs in a category (e.g., 'daily_reports')",
                    "Call clear_scheduled_actions_by_tag with the category tag",
                    "Report results to user",
                ],
                example={
                    "user_request": "Cancel all daily report schedules",
                    "plan": {
                        "steps": [
                            {
                                "process_key": "plugin::default_scheduling_plugin::clear_scheduled_actions_by_tag",
                                "reason": "Bulk cancel all daily report schedules",
                                "arguments": {"tag": "daily_reports"},
                            },
                            {
                                "process_key": "plugin::<active_io_plugin>::post_message",
                                "reason": "Confirm cancellation to user",
                                "arguments": {"message": "Cancelled all daily report schedules. Total cancelled: {cleared_count}"},
                            },
                        ]
                    },
                },
            ),
        ],
        complete_examples=[
            InvocationExample(
                description="Bulk cancel all monitoring schedules for a completed async job",
                invocation={
                    "process_key": "plugin::default_scheduling_plugin::clear_scheduled_actions_by_tag",
                    "arguments": {"tag": "monitor_job_12345"},
                },
                response={
                    "action_status": "COMPLETED",
                    "timestamp": "2025-11-14T10:30:00Z",
                    "data": {
                        "cleared_count": 5,
                        "cleared_ids": ["1001", "1002", "1003", "1004", "1005"],
                        "tag": "monitor_job_12345",
                    },
                    "actions": [],
                    "error": None,
                },
            ),
            InvocationExample(
                description="Cancel all schedules in a specific category",
                invocation={
                    "process_key": "plugin::default_scheduling_plugin::clear_scheduled_actions_by_tag",
                    "arguments": {"tag": "cleanup_tasks"},
                },
                response={
                    "action_status": "COMPLETED",
                    "timestamp": "2025-11-14T10:35:00Z",
                    "data": {
                        "cleared_count": 2,
                        "cleared_ids": ["1006", "1007"],
                        "tag": "cleanup_tasks",
                    },
                    "actions": [],
                    "error": None,
                },
            ),
        ],
        error_cases=[
            ErrorCase(
                condition="No schedules found with the specified tag (not an error - returns count 0)",
                error_response={
                    "action_status": "COMPLETED",
                    "data": {
                        "cleared_count": 0,
                        "cleared_ids": [],
                        "tag": "nonexistent_tag",
                    },
                    "actions": [],
                    "error": None,
                },
            ),
            ErrorCase(
                condition="Missing required tag parameter",
                error_response={
                    "action_status": "ERROR",
                    "data": {},
                    "error": {
                        "type": "plugin_error",
                        "code": "scheduling.invalid_parameters",
                        "message": "tag required",
                        "plugin_name": "default_scheduling_plugin",
                    },
                },
            ),
            ErrorCase(
                condition="Scheduler service error during bulk cancellation",
                error_response={
                    "action_status": "ERROR",
                    "data": {},
                    "error": {
                        "type": "scheduling_error",
                        "code": "scheduling.execution_error",
                        "message": "Failed to clear schedules by tag",
                        "plugin_name": "default_scheduling_plugin",
                    },
                },
            ),
        ],
    )
    def clear_scheduled_actions_by_tag(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            p = ScheduleFactory.extract_params(params)

            tag = p.get("tag", "")
            if not tag:
                raise PluginError(
                    "tag required",
                    SchedulerErrorCode.INVALID_PARAMETERS,
                    plugin_name=PLUGIN_NAME,
                    details={"missing_field": "tag"},
                )

            schedules = self._load_schedules()
            cleared_count = 0
            cleared_ids = []

            scheduled_actions = schedules.get("scheduled_actions", {})
            for schedule_id, schedule_data in list(scheduled_actions.items()):
                schedule_tags = schedule_data.get("tags", [])
                if tag in schedule_tags:
                    if self._scheduler_manager:
                        self._scheduler_manager.remove_job(schedule_id)

                    if self._delete_schedule(schedule_id):
                        cleared_count += 1
                        cleared_ids.append(schedule_id)
                        self.logger.debug(f"Cleared schedule {schedule_id} with tag {tag}")

            return build_response(
                ActionStatus.COMPLETED.value,
                {
                    "cleared_count": cleared_count,
                    "cleared_ids": cleared_ids,
                    "tag": tag,
                },
            )

        except Exception as e:
            self.logger.error(f"Error clearing scheduled actions by tag: {str(e)}")
            return build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "scheduling_error",
                    "code": SchedulerErrorCode.EXECUTION_ERROR,
                    "message": str(e),
                    "plugin_name": PLUGIN_NAME,
                },
            )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/get_schedules_by_tag.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="get_schedules_by_tag",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        parameters={
            "tag": ParameterMetadata(
                description="Tag to filter schedules by",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="Matching schedules with metadata",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Schedules matching the tag",
            properties={
                "tag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Tag used for filtering",
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of matching schedules",
                ),
                "schedules": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="List of matching schedule objects",
                ),
            },
        ),
    )
    def get_schedules_by_tag(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            p = ScheduleFactory.extract_params(params)

            tag = p.get("tag", "")
            if not tag:
                raise PluginError(
                    "tag required",
                    SchedulerErrorCode.INVALID_PARAMETERS,
                    plugin_name=PLUGIN_NAME,
                    details={"missing_field": "tag"},
                )

            schedules = self._load_schedules()
            scheduled_actions = schedules.get("scheduled_actions", {})

            matching: list[dict[str, Any]] = []
            for schedule_id, schedule_data in scheduled_actions.items():
                if tag in schedule_data.get("tags", []):
                    matching.append(
                        {
                            "schedule_id": schedule_id,
                            "type": schedule_data.get("type"),
                            "status": schedule_data.get("status"),
                            "label": schedule_data.get("label"),
                            "cron_expression": schedule_data.get("cron_expression"),
                            "run_at": schedule_data.get("run_at"),
                            "tags": schedule_data.get("tags", []),
                            # Useful for debugging heartbeat scope: global schedules should not
                            # inherit a user session_id.
                            "session_id": schedule_data.get("session_id"),
                            "flow_id": schedule_data.get("flow_id"),
                        }
                    )

            return build_response(
                ActionStatus.COMPLETED.value,
                {
                    "schedules": matching,
                    "count": len(matching),
                    "tag": tag,
                },
            )

        except Exception as e:
            self.logger.error(f"Error listing schedules by tag: {e}")
            return build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "scheduling_error",
                    "code": SchedulerErrorCode.EXECUTION_ERROR,
                    "message": str(e),
                    "plugin_name": PLUGIN_NAME,
                },
            )

    @platform_process(
        name="list_schedules",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        parameters={
            "status": ParameterMetadata(
                description=("Optional status filter; return only schedules in this status (scheduled, running, completed, cancelled, error, paused). Omit to return every schedule."),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="All scheduled jobs with metadata",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Every scheduled job, optionally filtered by status",
            properties={
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of schedules returned",
                ),
                "schedules": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="List of schedule objects",
                ),
                "status_filter": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status filter applied, or null when unfiltered",
                ),
            },
        ),
    )
    def list_schedules(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Enumerate every scheduled job; optional status filter.

        Tag-agnostic companion to :meth:`get_schedules_by_tag`: returns ALL
        schedules (cron + one-time) so a caller that does not already know a
        tag or id can find a schedule — e.g. to locate and clear a stale cron.
        Reads the same ``_load_schedules`` source; read-only, mutates nothing.
        """
        try:
            p = ScheduleFactory.extract_params(params)
            status_filter = p.get("status") or None

            schedules = self._load_schedules()
            scheduled_actions = schedules.get("scheduled_actions", {})

            entries: list[dict[str, Any]] = []
            for schedule_id, schedule_data in scheduled_actions.items():
                if status_filter is not None and schedule_data.get("status") != status_filter:
                    continue
                entries.append(
                    {
                        "schedule_id": schedule_id,
                        "type": schedule_data.get("type"),
                        "status": schedule_data.get("status"),
                        "label": schedule_data.get("label"),
                        "cron_expression": schedule_data.get("cron_expression"),
                        "run_at": schedule_data.get("run_at"),
                        "tags": schedule_data.get("tags", []),
                        # Useful for debugging heartbeat scope: global schedules
                        # should not inherit a user session_id.
                        "session_id": schedule_data.get("session_id"),
                        "flow_id": schedule_data.get("flow_id"),
                    }
                )

            return build_response(
                ActionStatus.COMPLETED.value,
                {
                    "schedules": entries,
                    "count": len(entries),
                    "status_filter": status_filter,
                },
            )

        except Exception as e:
            self.logger.error(f"Error listing schedules: {e}")
            return build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "scheduling_error",
                    "code": SchedulerErrorCode.EXECUTION_ERROR,
                    "message": str(e),
                    "plugin_name": PLUGIN_NAME,
                },
            )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/ensure_global_heartbeat.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="ensure_global_heartbeat",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        parameters={
            "cadence_minutes": ParameterMetadata(
                description="Wake-up interval in minutes (default: 5)",
                required=False,
                type=ParameterType.INTEGER,
                default=DEFAULT_HEARTBEAT_CADENCE_MINUTES,
            ),
            "tag": ParameterMetadata(
                description="Schedule tag (default: heartbeat:global)",
                required=False,
                type=ParameterType.STRING,
                default=HEARTBEAT_TAG,
            ),
            "memory_tag": ParameterMetadata(
                description="Memory tag for wake-up recall (default: same as tag)",
                required=False,
                type=ParameterType.STRING,
                default=HEARTBEAT_TAG,
            ),
        },
        output_type="object",
        output_description="Heartbeat status with schedule details",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Heartbeat ensure result",
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="One of: created, already_present, normalized",
                ),
                "schedule_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Active heartbeat schedule ID",
                ),
                "tag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Schedule tag",
                ),
                "cron_expression": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Cron expression for the heartbeat",
                ),
                "cadence_minutes": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Wake-up interval in minutes",
                ),
                "cleared_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of stale/duplicate schedules cleared during normalization",
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Human-readable status message",
                ),
            },
        ),
    )
    def ensure_global_heartbeat(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            p = ScheduleFactory.extract_params(params)

            tag = p.get("tag", HEARTBEAT_TAG)
            memory_tag = p.get("memory_tag", tag)
            cadence_minutes = int(p.get("cadence_minutes", DEFAULT_HEARTBEAT_CADENCE_MINUTES))
            if not 1 <= cadence_minutes <= 59:
                raise PluginError(
                    "cadence_minutes must be between 1 and 59",
                    SchedulerErrorCode.PARAMETER_ERROR,
                    plugin_name=PLUGIN_NAME,
                )

            # 1. Check existing schedules with this tag
            schedules = self._load_schedules()
            scheduled_actions = schedules.get("scheduled_actions", {})
            matching = {sid: data for sid, data in scheduled_actions.items() if tag in data.get("tags", [])}

            # 2. If exactly one active heartbeat exists with matching cadence, return it
            desired_cron = f"*/{cadence_minutes} * * * *"
            already_present = check_existing_heartbeat(matching, desired_cron, tag, cadence_minutes)
            if already_present is not None:
                return already_present

            # 3. Normalize: clear any existing (duplicates or stale)
            cleared_count = clear_stale_heartbeats(matching, self._scheduler_manager, self._delete_schedule)

            # 4. Create new heartbeat cron schedule
            actions_list, legacy_action_name, legacy_action_params = ScheduleFactory.parse_actions_from_params({"memory_tag": memory_tag})

            schedule_data = ScheduleFactory.create_cron_schedule_data(
                cron_expression=desired_cron,
                label="Global Heartbeat",
                tags=[tag],
                actions=actions_list,
                action_name=legacy_action_name,
                action_parameters=legacy_action_params,
                # Global heartbeat must not inherit the creating user's session/flow.
                # Use system-owned identifiers so runtime components (scheduler callback,
                # inference pipeline) have valid IDs without coupling to any user session.
                session_id=HEARTBEAT_SESSION_ID,
                flow_id=HEARTBEAT_FLOW_ID,
            )

            schedule_id = self._save_schedule(schedule_data)
            if not schedule_id:
                raise PluginError(
                    "Failed to save heartbeat schedule",
                    SchedulerErrorCode.SCHEDULE_CREATE_ERROR,
                    plugin_name=PLUGIN_NAME,
                )

            register_heartbeat_job(schedule_id, schedule_data, desired_cron, self._scheduler_manager, self._execute_action)

            result_status = "normalized" if cleared_count > 0 else "created"
            self.logger.info(f"Global heartbeat {result_status}: {schedule_id} ({desired_cron})")

            return build_response(
                ActionStatus.COMPLETED.value,
                {
                    "status": result_status,
                    "schedule_id": schedule_id,
                    "tag": tag,
                    "cron_expression": desired_cron,
                    "cadence_minutes": cadence_minutes,
                    "cleared_count": cleared_count,
                    "message": f"Global heartbeat {result_status}: {schedule_id} ({desired_cron})",
                },
            )

        except Exception as e:
            self.logger.error(f"Failed to ensure global heartbeat: {e}", exc_info=True)
            return build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "scheduling_error",
                    "code": SchedulerErrorCode.EXECUTION_ERROR,
                    "message": str(e),
                    "plugin_name": PLUGIN_NAME,
                },
            )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/ensure_kb_retrieval_audit_schedule.json — the builder merges
    # them at startup, overwriting any values set here in the decorator.
    @platform_process(
        name="ensure_kb_retrieval_audit_schedule",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_discoverable=False,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        parameters={
            "cron_expression": ParameterMetadata(
                description="Cron expression (UTC). Default: 0 6 * * * (daily 06:00).",
                required=False,
                type=ParameterType.STRING,
                default=KB_RETRIEVAL_AUDIT_CRON,
            ),
            "tag": ParameterMetadata(
                description=("Schedule tag. Default kb_retrieval_audit:edge_sink — deliberately NOT the predecessor's tag, so this never clears the old trigger."),
                required=False,
                type=ParameterType.STRING,
                default=KB_RETRIEVAL_AUDIT_TAG,
            ),
        },
        output_type="object",
        output_description="Ensure result for the KB retrieval-drift audit cron",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="KB retrieval-audit schedule ensure result",
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="One of: created, already_present, normalized",
                ),
                "schedule_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Active KB retrieval-audit schedule ID",
                ),
                "tag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Schedule tag",
                ),
                "cron_expression": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Cron expression for the audit",
                ),
                "process_key": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Cron-target process key the schedule invokes",
                ),
                "cleared_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of stale/duplicate schedules cleared during normalization",
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Human-readable status message",
                ),
            },
        ),
    )
    def ensure_kb_retrieval_audit_schedule(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Idempotently install the KB retrieval-drift audit cron with system-owned IDs.

        Mirrors :meth:`ensure_global_heartbeat`, with one deliberate difference:
        the action is an EDGE_SINK ``action_definitions`` entry naming
        ``audit_retrieval_corpus_cron``, not a memory-tag heartbeat. The
        memory-tag shape is precisely what made the predecessor trigger
        (``sch-2l18uk4q9et3a``) fire daily for two months and execute the audit
        zero times: that shape sets ``result_processor_kind=None`` by
        construction, and the poller's EDGE_SINK_SKIP branch terminates it with
        no dispatch, forever, for headless crons.

        ``session_id``/``flow_id`` are system-owned constants rather than the
        caller's. A cron that inherits the creating session's id outlives that
        session and is left pointing at a dead one — which is what the
        predecessor did (``sess-2l18il2eddp47``, dead since June). There is no
        caller-supplied session/flow parameter here by design: letting a caller
        name a session identity is a wider blast radius than the defect it fixes.

        The default tag is deliberately NOT the predecessor's
        (``kb_retrieval_audit``), so this verb creates the new schedule
        **alongside** the old one and never clears it. Retiring the predecessor
        is a separate, explicit step, taken only after one real firing of this
        schedule has been confirmed — a bare swap would leave a silent failure
        indistinguishable from a fix.
        """
        try:
            p = ScheduleFactory.extract_params(params)

            tag = str(p.get("tag", KB_RETRIEVAL_AUDIT_TAG))
            cron_expression = str(p.get("cron_expression", KB_RETRIEVAL_AUDIT_CRON))
            if not validate_cron_expression(cron_expression):
                raise PluginError(
                    f"Invalid cron expression: {cron_expression!r}",
                    SchedulerErrorCode.PARAMETER_ERROR,
                    plugin_name=PLUGIN_NAME,
                )

            # 1. Find existing schedules carrying this tag.
            schedules = self._load_schedules()
            scheduled_actions = schedules.get("scheduled_actions", {})
            matching = {sid: data for sid, data in scheduled_actions.items() if tag in data.get("tags", [])}

            # 2. Exactly one, already correct, already pointed at the EDGE_SINK
            #    verb -> nothing to do. Checked against the action's process key
            #    and not only the cron, because a schedule with the right cadence
            #    and the wrong target is the defect being repaired.
            already_present = self._check_existing_audit_schedule(matching, cron_expression, tag)
            if already_present is not None:
                return already_present

            # 3. Normalize: clear duplicates or a stale/mis-targeted entry.
            cleared_count = clear_stale_heartbeats(matching, self._scheduler_manager, self._delete_schedule)

            # 4. Create the EDGE_SINK cron. No result_processor_kind: omitting it
            #    is canonical for EDGE_SINK and is what validate_cron_action_def
            #    requires (it rejects only 'inference', which needs a session
            #    context a headless cron does not have).
            actions_list = [
                ActionData(
                    name=KB_RETRIEVAL_AUDIT_PROCESS_KEY,
                    parameters={},
                    result_processor=None,
                    result_processor_kind=None,
                )
            ]

            schedule_data = ScheduleFactory.create_cron_schedule_data(
                cron_expression=cron_expression,
                label=KB_RETRIEVAL_AUDIT_LABEL,
                tags=[tag],
                actions=actions_list,
                action_name=KB_RETRIEVAL_AUDIT_PROCESS_KEY,
                action_parameters={},
                # System-owned, never the caller's: see the docstring and the
                # sibling constants beside HEARTBEAT_SESSION_ID/FLOW_ID.
                session_id=KB_RETRIEVAL_AUDIT_SESSION_ID,
                flow_id=KB_RETRIEVAL_AUDIT_FLOW_ID,
            )

            schedule_id = self._save_schedule(schedule_data)
            if not schedule_id:
                raise PluginError(
                    "Failed to save KB retrieval-audit schedule",
                    SchedulerErrorCode.SCHEDULE_CREATE_ERROR,
                    plugin_name=PLUGIN_NAME,
                )

            register_heartbeat_job(
                schedule_id,
                schedule_data,
                cron_expression,
                self._scheduler_manager,
                self._execute_action,
            )

            result_status = "normalized" if cleared_count > 0 else "created"
            message = f"KB retrieval-audit schedule {result_status}: {schedule_id} ({cron_expression}) -> {KB_RETRIEVAL_AUDIT_PROCESS_KEY}"
            self.logger.info(message)

            return build_response(
                ActionStatus.COMPLETED.value,
                {
                    "status": result_status,
                    "schedule_id": schedule_id,
                    "tag": tag,
                    "cron_expression": cron_expression,
                    "process_key": KB_RETRIEVAL_AUDIT_PROCESS_KEY,
                    "cleared_count": cleared_count,
                    "message": message,
                },
            )

        except Exception as e:
            self.logger.error(f"Failed to ensure KB retrieval-audit schedule: {e}", exc_info=True)
            return build_response(
                ActionStatus.ERROR.value,
                {},
                {
                    "type": "scheduling_error",
                    "code": SchedulerErrorCode.EXECUTION_ERROR,
                    "message": str(e),
                    "plugin_name": PLUGIN_NAME,
                },
            )

    @staticmethod
    def _check_existing_audit_schedule(
        matching: dict[str, dict[str, Any]],
        desired_cron: str,
        tag: str,
    ) -> dict[str, Any] | None:
        """Return an ``already_present`` response when exactly one correct schedule exists.

        "Correct" means: one schedule, the desired cron, AND an action naming the
        EDGE_SINK cron target. The process-key check is load-bearing rather than
        defensive — the defect this whole path repairs is a schedule with the
        right cadence and the wrong target, which a cron-only comparison would
        report as already present and leave dead.
        """
        if len(matching) != 1:
            return None

        schedule_id, data = next(iter(matching.items()))
        if data.get("cron_expression") != desired_cron:
            return None

        actions = data.get("actions") or []
        targets = {action.get("name") for action in actions if isinstance(action, dict)}
        if KB_RETRIEVAL_AUDIT_PROCESS_KEY not in targets:
            return None

        message = f"KB retrieval-audit schedule already present: {schedule_id} ({desired_cron}) -> {KB_RETRIEVAL_AUDIT_PROCESS_KEY}"
        return build_response(
            ActionStatus.COMPLETED.value,
            {
                "status": "already_present",
                "schedule_id": schedule_id,
                "tag": tag,
                "cron_expression": desired_cron,
                "process_key": KB_RETRIEVAL_AUDIT_PROCESS_KEY,
                "cleared_count": 0,
                "message": message,
            },
        )

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """Return edge process definitions for Scheduling plugin.

        Returns:
            Dictionary mapping process names to their EdgeProcessDefinition.
        """
        return {
            "create_cron_schedule": EdgeProcessDefinition(
                name="create_cron_schedule",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(retryable=True),
            ),
            "execute_in_seconds": EdgeProcessDefinition(
                name="execute_in_seconds",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(retryable=True),
            ),
            "clear_scheduled_action": EdgeProcessDefinition(
                name="clear_scheduled_action",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(retryable=True),
            ),
            "clear_scheduled_actions_by_tag": EdgeProcessDefinition(
                name="clear_scheduled_actions_by_tag",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(retryable=True),
            ),
            "get_schedules_by_tag": EdgeProcessDefinition(
                name="get_schedules_by_tag",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(retryable=True),
            ),
            "list_schedules": EdgeProcessDefinition(
                name="list_schedules",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(retryable=True),
            ),
            "ensure_global_heartbeat": EdgeProcessDefinition(
                name="ensure_global_heartbeat",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(retryable=True),
            ),
            # Declared-side half of the EdgeProcessProvider decorated<->declared
            # parity contract. An EDGE process that is decorated but not declared
            # here fails `edge_definition_customizations_smoke` at boot parity.
            "ensure_kb_retrieval_audit_schedule": EdgeProcessDefinition(
                name="ensure_kb_retrieval_audit_schedule",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(retryable=True),
            ),
        }
