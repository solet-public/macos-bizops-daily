"""
Initialization Manager

Responsibility: Handle complete EventOrchestrator initialization using deterministic startup sequence
Dependencies: StartupSequenceRunner
Complexity: Low - delegates to StartupSequenceRunner

Replaces three-phase architecture with deterministic startup sequence
"""

import asyncio
import json
import logging
import os
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from ananta.core.orchestration.action_processor import ActionProcessor
from ananta.core.orchestration.startup_sequence import (
    STARTUP_SEQUENCE,
    StartupError,
    StartupSequenceRunner,
)

_PROBE_MODE_ENV_VAR = "SOLET_PROBE_MODE"
_PROBE_FAILURE_FILENAME = "probe_failure.json"

if TYPE_CHECKING:
    from ananta.core.event_orchestrator import EventOrchestrator

logger = logging.getLogger(__name__)


class InitializationResult(NamedTuple):
    """Complete initialization result with all EventOrchestrator components."""

    # Configuration and state
    config: object
    APP_HOME: str
    starting_prompt: str
    max_consecutive_errors: int | None
    max_actions_per_cycle: int | None
    plugin_operational_config: dict[str, dict[str, object]]
    default_inference_provider: str | None
    current_session_id: str | None
    current_flow_id: str | None
    session_timeout_hours: int

    # Event system
    event_queue: object
    event_handler_registry: object
    event_bus: object
    event: asyncio.Event
    shutdown_event: asyncio.Event
    ready_event: asyncio.Event
    main_loop: object | None

    # Processors
    action_processor: ActionProcessor | None


class InitializationManager:
    """
    Initialization manager for EventOrchestrator using deterministic startup sequence.

    Design Principles:
    - Single Responsibility: Complete orchestrator initialization only
    - Deterministic Sequence: Explicit dependency-ordered steps
    - Dependency Injection: Clean injection of initialization parameters
    - State Encapsulation: Return complete initialization state
    - Error Handling: Comprehensive initialization error handling
    """

    def _configure_orchestrator_state(
        self,
        orchestrator: "EventOrchestrator",
        starting_prompt: str,
        max_consecutive_errors: int | None,
        max_actions_per_cycle: int | None,
        plugin_config: dict[str, dict[str, object]] | None,
        default_inference_provider: str | None,
    ) -> None:
        """Configure orchestrator state needed by startup sequence."""
        orchestrator.starting_prompt = starting_prompt
        orchestrator.max_consecutive_errors = max_consecutive_errors or 5
        orchestrator.max_actions_per_cycle = max_actions_per_cycle or 10
        orchestrator._state_plugin_name = (  # type: ignore[attr-defined]
            plugin_config.get("_state_plugin_name") if plugin_config else None
        )
        orchestrator.plugin_operational_config = plugin_config or {}
        orchestrator.default_inference_provider = default_inference_provider
        orchestrator._session_timeout_hours = 1

    def _run_startup_sequence(self, orchestrator: "EventOrchestrator") -> None:
        """Execute the startup sequence, raising StartupError on failure.

        When running as an L2 probe (``SOLET_PROBE_MODE=1``), a startup
        failure is additionally captured to
        ``<APP_HOME>/probe_failure.json`` so the parent
        ``macos_self_deployment_plugin`` swap orchestrator can surface
        the structured failure detail in its rejection envelope without
        scraping stderr. Production boots are unaffected.
        """
        runner = StartupSequenceRunner(STARTUP_SEQUENCE)
        try:
            runner.run(orchestrator)
        except StartupError as e:
            logger.critical(f"FATAL: Startup sequence failed: {e}")
            self._stop_lifecycle_services_after_startup_failure(orchestrator)
            self._capture_probe_failure_if_applicable(orchestrator, e)
            raise

    @staticmethod
    def _stop_lifecycle_services_after_startup_failure(orchestrator: "EventOrchestrator") -> None:
        """Stop every lifecycle service before propagating a startup fatal.

        ``start_service_plugins`` can start non-daemon worker threads before a
        later startup step fails.  Letting that failure reach ``sys.exit`` with
        those workers still alive strands the interpreter in finalization and
        prevents launchd from supervising the failed boot.
        """
        from ananta.core.plugins.capabilities import stop_lifecycle_plugins

        try:
            stop_lifecycle_plugins(orchestrator.plugin_manager.plugins)
        except Exception:
            # The original startup failure remains the process disposition, but
            # cleanup failure must be visible rather than silently masking it.
            logger.exception("Failed to stop all lifecycle services after startup failure")

    def _capture_probe_failure_if_applicable(
        self,
        orchestrator: "EventOrchestrator",
        error: StartupError,
    ) -> None:
        """Write probe_failure.json when this boot is an L2 probe.

        Best-effort: any I/O failure during capture is logged and
        swallowed; the original ``StartupError`` re-raises normally so
        the probe process exits non-zero either way.
        """
        if os.environ.get(_PROBE_MODE_ENV_VAR) != "1":
            return
        app_home = getattr(orchestrator, "APP_HOME", None)
        if not app_home:
            logger.warning(
                "Probe mode active but orchestrator.APP_HOME unset; "
                "cannot emit probe_failure.json",
            )
            return
        failure_path = Path(app_home) / _PROBE_FAILURE_FILENAME
        payload = {
            "step": error.step_name,
            "error_class": type(error.__cause__).__name__ if error.__cause__ else type(error).__name__,
            "message": str(error),
            "traceback": "".join(
                traceback.format_exception(error.__cause__) if error.__cause__ else traceback.format_exception(error),
            ),
            "failed_at": datetime.now(UTC).isoformat(),
        }
        try:
            failure_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            logger.info("Wrote probe failure detail to %s", failure_path)
        except OSError as io_exc:
            logger.warning(
                "Could not write probe_failure.json: %s: %s",
                type(io_exc).__name__,
                io_exc,
            )

    def _validate_orchestrator_state(
        self,
        orchestrator: "EventOrchestrator",
        starting_prompt: str,
        plugin_config: dict[str, dict[str, object]] | None,
    ) -> dict[str, dict[str, object]]:
        """Validate orchestrator state and return plugin operational config."""
        if not isinstance(orchestrator.max_consecutive_errors, int):
            raise TypeError("max_consecutive_errors must be an int")
        if not isinstance(orchestrator.max_actions_per_cycle, int):
            raise TypeError("max_actions_per_cycle must be an int")

        plugin_operational_config = plugin_config or {}
        return plugin_operational_config

    def _build_initialization_result(
        self,
        orchestrator: "EventOrchestrator",
        starting_prompt: str,
        plugin_operational_config: dict[str, dict[str, object]],
        default_inference_provider: str | None,
    ) -> InitializationResult:
        """Build the final initialization result."""
        return InitializationResult(
            config=orchestrator.config_manager,  # type: ignore[attr-defined]
            APP_HOME=orchestrator.APP_HOME,
            starting_prompt=starting_prompt,
            max_consecutive_errors=orchestrator.max_consecutive_errors,
            max_actions_per_cycle=orchestrator.max_actions_per_cycle,
            plugin_operational_config=plugin_operational_config,
            default_inference_provider=default_inference_provider,
            current_session_id=None,
            current_flow_id=None,
            session_timeout_hours=1,
            event_queue=orchestrator.event_queue,
            event_handler_registry=orchestrator.event_handler_registry,
            event_bus=orchestrator.event_bus,
            event=asyncio.Event(),
            shutdown_event=asyncio.Event(),
            ready_event=asyncio.Event(),
            main_loop=None,
            action_processor=None,
        )

    def initialize_orchestrator(
        self,
        orchestrator: "EventOrchestrator",
        starting_prompt: str,
        max_consecutive_errors: int | None = None,
        max_actions_per_cycle: int | None = None,
        plugin_config: dict[str, dict[str, object]] | None = None,
        default_inference_provider: str | None = None,
    ) -> InitializationResult:
        """
        Complete EventOrchestrator initialization using deterministic startup sequence.

        Returns InitializationResult with all components ready for use.
        """
        logger.debug("InitializationManager: Starting deterministic startup sequence")

        self._configure_orchestrator_state(
            orchestrator,
            starting_prompt,
            max_consecutive_errors,
            max_actions_per_cycle,
            plugin_config,
            default_inference_provider,
        )

        self._run_startup_sequence(orchestrator)

        plugin_operational_config = self._validate_orchestrator_state(
            orchestrator, starting_prompt, plugin_config
        )

        logger.debug("InitializationManager: Deterministic startup sequence completed successfully")

        return self._build_initialization_result(
            orchestrator, starting_prompt, plugin_operational_config, default_inference_provider
        )

    def get_initialization_summary(self) -> dict[str, object]:
        """Get summary of InitializationManager for debugging."""
        return {
            "component": "InitializationManager",
            "responsibility": "Complete EventOrchestrator deterministic initialization",
            "approach": "Deterministic startup sequence with explicit dependencies",
            "features": [
                "Dependency-ordered steps",
                "Service plugins started before wrappers",
                "Readiness verification",
                "Complete state setup",
            ],
        }
