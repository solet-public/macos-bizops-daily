"""
Runtime Manager

Responsibility: Handle EventOrchestrator runtime operations after three-phase initialization
Dependencies: Orchestrator reference for Post-Phase-3 database operations and runtime coordination
Complexity: Medium - handles session/flow creation, action initialization, and event loop coordination

Extracted from EventOrchestrator.run() method (60 lines)
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


class RuntimeManager:
    """
    Runtime manager for EventOrchestrator post-initialization operations.

    Design Principles:
        pass
    - Single Responsibility: Post-Phase-3 + Runtime operations only
    - Database Dependencies: Session/flow creation, action initialization
    - Event Loop Coordination: Main loop management, ActionQueuePoller coordination
    - Error Handling: Comprehensive runtime error handling
    - Clean Separation: Initialization vs Runtime boundaries
    """

    def __init__(self, orchestrator_ref) -> None:  # type: ignore[no-untyped-def]
        """Initialize RuntimeManager with orchestrator reference for Post-Phase-3 operations.

        SAFE: Orchestrator ref type intentionally untyped to avoid circular import with EventOrchestrator.
        """
        self.orchestrator = orchestrator_ref

    async def execute_runtime_operations(self) -> None:
        """
        Execute complete runtime operations.

        This method runs AFTER startup sequence is complete.
        Service plugins are already started by the startup sequence.
        Handles database-dependent operations and runtime event loop coordination.
        """
        logger.debug("RuntimeManager: Starting runtime operations")

        # Set main loop and signal ready
        self.orchestrator._main_loop = asyncio.get_running_loop()
        self.orchestrator.ready_event.set()

        # NOTE: Service plugins already started in startup sequence
        # No need to start them here

        # RUNTIME: Create session and flow for correlation tracking (requires database)
        await self._ensure_session_and_flow_created()

        # RUNTIME: Initialize actions ONCE at startup (requires database)
        await self._initialize_startup_actions()

        # RUNTIME: Start ActionQueuePoller (requires database)
        await self._start_action_queue_poller()

        # RUNTIME: Coordinate main event loop
        await self._coordinate_main_event_loop()

    async def _ensure_session_and_flow_created(self) -> None:
        """Create session and flow for correlation tracking if not exists."""
        # FIX: Create session and flow for correlation tracking
        if not self.orchestrator.current_session_id:
            self.orchestrator.current_session_id = self.orchestrator.create_session(
                namespace="event_orchestrator",
                context_type="orchestration_session",
                metadata={"starting_prompt": self.orchestrator.starting_prompt},
            )

        if not self.orchestrator.current_flow_id:
            self.orchestrator.current_flow_id = self.orchestrator.create_flow(
                session_id=self.orchestrator.current_session_id,
                trigger_type="system_startup",
                trigger_data={"starting_prompt": self.orchestrator.starting_prompt},
                priority=5,
            )

    async def _initialize_startup_actions(self) -> None:
        """Initialize actions ONCE at startup - requires database to be online."""
        # CRITICAL FIX: Initialize actions ONCE at startup only
        try:
            await self.orchestrator.process_actions()
        except Exception:
            raise

    async def _start_action_queue_poller(self) -> None:
        """Start action dispatch, then arm the target registration deadline.

        The self-deployment heartbeat waits for the bridge created by a
        starting action.  Startup preparation also contains synchronous
        inference prewarm and knowledge-base hydration, neither of which is a
        bridge-registration failure.  Arming after the poller starts preserves
        the existing strict-I2 budget for its intended bridge-bind/register
        interval.
        """
        # Start polling-based action processing (replaces trigger system)
        await self.orchestrator.action_queue_poller.start()
        plugin_manager = self.orchestrator.plugin_manager
        mark_serving = getattr(plugin_manager, "_mark_serving", None)
        if not callable(mark_serving):
            raise RuntimeError(
                "PluginManager must expose _mark_serving before action dispatch starts"
            )
        mark_serving()
        self_deployment_plugin = plugin_manager.plugins.get(
            "macos_self_deployment_plugin",
        )
        if self_deployment_plugin is None:
            return
        arm_registration_deadline = getattr(
            self_deployment_plugin,
            "arm_registration_deadline",
            None,
        )
        if not callable(arm_registration_deadline):
            raise RuntimeError(
                "macos_self_deployment_plugin must expose "
                "arm_registration_deadline after action dispatch starts",
            )
        arm_registration_deadline()

    async def _coordinate_main_event_loop(self) -> None:
        """Coordinate main event loop with ActionQueuePoller."""
        # ARCHITECTURAL CHANGE: Polling-based system runs continuously

        # ASYNC ARCHITECTURE: Keep the event loop running concurrently with ActionQueuePoller
        # The ActionQueuePoller runs as an async task, no thread management needed
        if self.orchestrator.action_queue_poller.poller_task:
            try:
                # Keep the main event loop alive by waiting for a shutdown signal.
                # The ActionQueuePoller runs as a concurrent async task. The
                # ``shutdown_event`` break lets the SIGTERM handler
                # (EventOrchestrator._on_sigterm) stop the loop promptly (<=1s)
                # so a drained color exits well inside the swap finisher's
                # SIGKILL grace window — a SIGKILL'd color can't exit 0, which
                # would defeat respawn-suppression and ghost-respawn under
                # launchd KeepAlive.
                while (
                    self.orchestrator.action_queue_poller.running
                    and not self.orchestrator.shutdown_event.is_set()
                ):
                    await asyncio.sleep(1)
            except Exception:
                raise
        else:
            pass

    def get_runtime_summary(self) -> dict[str, object]:
        """Get summary of RuntimeManager for debugging."""
        return {
            "component": "RuntimeManager",
            "responsibility": "Runtime operations only",
            "operations": [
                "Session/flow creation",
                "Action initialization",
                "ActionQueuePoller startup",
                "Main event loop coordination",
            ],
            "dependencies": [
                "Orchestrator reference",
                "Database online",
                "Startup sequence complete",
                "Service plugins already started",
            ],
        }
