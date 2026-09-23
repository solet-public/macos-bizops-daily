import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol, cast

if TYPE_CHECKING:
    from ananta.core.plugins.plugin_manager import PluginManager

from ananta.constants import (
    DEFAULT_LOG_RETENTION_DAYS,
    DEFAULT_MAX_ACTIONS_PER_CYCLE,
    DEFAULT_MAX_CONSECUTIVE_ERRORS,
    PLUGIN_CLI_PATTERN,
    STATE_VERSION,
)
from ananta.core.config.config_manager import initialize_config
from ananta.core.config.environment_config import EnvironmentConfig
from ananta.core.event_orchestrator import EventOrchestrator
from ananta.core.plugins.plugin_contracts import ActionStatus, ErrorCode, ErrorSeverity
from ananta.core.root_manifest.diagnostic import emit_startup_diagnostic
from ananta.error_handling import (
    AnantaError,
    ResourceError,
    SystemError,
    ValidationError,
)
from ananta.logging_setup import purge_old_logs, setup_logging


class HasPluginMethods(Protocol):
    """Protocol for plugin manager with get_all_plugin_names and get_plugin methods."""

    def get_all_plugin_names(self) -> list[str]: ...

    def get_plugin(self, plugin_name: str) -> object: ...


def parse_cli_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AnantaAI Framework",
        epilog="Example: python -m ananta.cli --app-home /path/to/myapp --actions start_console",
    )
    parser.add_argument(
        "--app-home",
        required=True,
        help="Application directory path (required).",
    )
    parser.add_argument(
        "--actions",
        required=False,
        default="",
        help="Initial prompt action (optional - defaults to starting_action_definitions.json)",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        default="info",
        help="Global logging level (debug, info, warning, error, critical). Default: info",
    )
    parser.add_argument(
        "--log-outputs",
        default=None,
        help="Comma-separated list of log outputs (console,file,state). Default: file",
    )
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_ERRORS,
        help="Maximum number of consecutive errors before halting action processing",
    )
    parser.add_argument(
        "--max-actions-per-cycle",
        type=int,
        default=DEFAULT_MAX_ACTIONS_PER_CYCLE,
        help="Maximum number of actions to process in a single execution cycle",
    )
    parser.add_argument(
        "--default-inference-provider",
        default=None,
        help="Default inference provider plugin to use for AI responses (e.g., claude_code_plugin, default_inference_plugin)",
    )
    parser.add_argument("--version", action="version", version=f"0.{STATE_VERSION}.0")

    args, unknown = parser.parse_known_args()

    args.plugin_config = parse_plugin_operational_parameters(unknown)

    return args


def parse_plugin_operational_parameters(unknown_args: list[str]) -> dict[str, dict[str, object]]:
    plugin_config: dict[str, dict[str, object]] = {}
    plugin_param_pattern = re.compile(PLUGIN_CLI_PATTERN)

    i = 0
    while i < len(unknown_args):
        arg = unknown_args[i]
        match = plugin_param_pattern.match(arg)

        if match:
            plugin_name, param_name = match.groups()

            if i + 1 >= len(unknown_args) or unknown_args[i + 1].startswith("--"):
                raise ValidationError(
                    message=f"Missing value for plugin operational parameter: {arg}",
                    error_code="cli.missing_parameter_value",
                    details={"parameter": arg, "parameter_type": "operational"},
                    severity=ErrorSeverity.ERROR,
                )

            param_value = unknown_args[i + 1]

            if plugin_name not in plugin_config:
                plugin_config[plugin_name] = {}

            plugin_config[plugin_name][param_name] = param_value
            i += 2
        else:
            if arg.startswith("--"):
                raise ValidationError(
                    message=f"Unknown parameter: {arg}",
                    error_code="cli.unknown_parameter",
                    details={"parameter": arg},
                    severity=ErrorSeverity.ERROR,
                )
            i += 1

    return plugin_config


def setup_environment(args: argparse.Namespace) -> Path:
    """Set up the application environment with validation and directory structure checks."""
    APP_HOME = Path(args.app_home).absolute()

    _validate_app_home_path(APP_HOME)
    _create_app_directory(APP_HOME)
    _validate_directory_structure(APP_HOME)
    _set_environment_variables(APP_HOME, args.actions)

    return APP_HOME


def _validate_app_home_path(app_home: Path) -> None:
    """Validate that APP_HOME path is not a nested 'app' directory."""
    if app_home.name == "app" and app_home.parent.name != "ananta_apps":
        raise ValidationError(
            message="Invalid app-home path: appears to be a nested 'app' directory",
            error_code=ErrorCode.VALIDATION_ERROR,
            details={
                "path": str(app_home),
                "hint": "Use the parent directory instead (e.g., --app-home /path/to/myapp instead of --app-home /path/to/myapp/app)",
                "action_status": ActionStatus.ERROR.value,
            },
            severity=ErrorSeverity.CRITICAL,
        )


def _create_app_directory(app_home: Path) -> None:
    """Create the APP_HOME directory with error handling."""
    try:
        app_home.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ResourceError(
            message=f"Failed to create app directory: {e}",
            error_code=ErrorCode.RESOURCE_GENERIC,
            details={"path": str(app_home), "action_status": ActionStatus.ERROR.value},
            original_error=e,
            severity=ErrorSeverity.CRITICAL,
        ) from e


AUTHORIZED_DIRECTORIES = frozenset({"app", "config", "data", "documents"})


def _validate_directory_structure(app_home: Path) -> None:
    """Validate that only authorized directories and files exist in APP_HOME."""
    unauthorized_dirs, unauthorized_files = _collect_unauthorized_items(app_home)

    if unauthorized_dirs or unauthorized_files:
        _raise_directory_structure_error(app_home, unauthorized_dirs, unauthorized_files)


def _collect_unauthorized_items(app_home: Path) -> tuple[list[str], list[str]]:
    """Collect unauthorized directories and files in app_home."""
    unauthorized_dirs: list[str] = []
    unauthorized_files: list[str] = []

    for item in app_home.iterdir():
        if item.name.startswith("."):
            continue
        if item.is_dir() and item.name not in AUTHORIZED_DIRECTORIES:
            unauthorized_dirs.append(item.name)
        elif item.is_file():
            unauthorized_files.append(item.name)

    return unauthorized_dirs, unauthorized_files


def _raise_directory_structure_error(
    app_home: Path,
    unauthorized_dirs: list[str],
    unauthorized_files: list[str],
) -> None:
    """Raise validation error for unauthorized items."""
    violations = []
    if unauthorized_dirs:
        violations.append(f"directories: {', '.join(unauthorized_dirs)}")
    if unauthorized_files:
        violations.append(f"files: {', '.join(unauthorized_files)}")

    raise ValidationError(
        message="FAIL-FAST: Unauthorized items detected in APP_HOME",
        error_code=ErrorCode.VALIDATION_ERROR,
        details={
            "path": str(app_home),
            "unauthorized_directories": unauthorized_dirs,
            "unauthorized_files": unauthorized_files,
            "authorized_directories": list(AUTHORIZED_DIRECTORIES),
            "solution": "Remove unauthorized files and directories from APP_HOME root",
            "violations": violations,
            "action_status": ActionStatus.ERROR.value,
        },
        severity=ErrorSeverity.CRITICAL,
    )


def _set_environment_variables(app_home: Path, actions: str) -> None:
    """Set required environment variables."""
    os.environ["APP_HOME"] = str(app_home)
    if actions:
        os.environ["STARTING_PROMPT"] = actions


def _create_bootstrap_plugin_manager(logger: logging.Logger) -> "PluginManager":
    """Create plugin manager via bootstrap manager."""
    from ananta.core.services.bootstrap_manager import BootstrapManager

    try:
        bootstrap_manager = BootstrapManager()
        bootstrap_services = bootstrap_manager.create_bootstrap_services()
        return bootstrap_manager.create_plugin_manager(bootstrap_services)
    except Exception as bootstrap_error:
        logger.error(f"Phase 1 bootstrap failed: {bootstrap_error}")
        raise


def _execute_service_transitions(
    orchestrator: EventOrchestrator, plugin_manager: "PluginManager"
) -> None:
    """Execute full service transition from bootstrap to plugin mode."""
    from ananta.core.services.service_transition_coordinator import (
        HasServicesCollection,
        ServiceTransitionCoordinator,
    )

    orchestrator_protocol = cast(HasServicesCollection, orchestrator)
    coordinator = ServiceTransitionCoordinator(
        orchestrator_protocol, plugin_manager, config_manager=orchestrator.config
    )
    coordinator.execute_full_transition_sync()


def _get_logging_outputs(log_outputs: str | None) -> list[str]:
    """Determine enabled logging outputs from CLI args or environment."""
    if log_outputs:
        return [output.strip() for output in log_outputs.split(",") if output.strip()]

    env_outputs = EnvironmentConfig.log_outputs()
    if env_outputs != ["console"]:
        return env_outputs

    return ["console", "file"]


def _reinitialize_logging_after_transition(
    orchestrator: EventOrchestrator,
    app_home: Path,
    log_level: str,
    log_outputs: str | None,
    logger: logging.Logger,
) -> None:
    """Reinitialize logging with state_service after Phase 2 transitions."""
    if not hasattr(orchestrator, "state_service") or not orchestrator.state_service:
        return

    try:
        enabled_outputs = _get_logging_outputs(log_outputs)
        logger.debug(f"Reinitializing logging with outputs: {enabled_outputs}")

        platform_logger = setup_logging(
            app_home=str(app_home),
            plugin_name="ananta",
            log_level=log_level,
            enabled_outputs=enabled_outputs,
        )
        platform_logger.debug("Platform logging reinitialized")
    except Exception as logging_error:
        logger.error(f"Failed to reinitialize logging: {logging_error}")


def _execute_infrastructure_setup(
    orchestrator: EventOrchestrator,
    plugin_manager: "PluginManager",
    app_home: Path,
    log_level: str,
    log_outputs: str | None,
    logger: logging.Logger,
) -> None:
    """Execute Phase 2 infrastructure setup."""
    try:
        _execute_service_transitions(orchestrator, plugin_manager)
        orchestrator.initialize_database_after_schema_creation()
        _reinitialize_logging_after_transition(
            orchestrator, app_home, log_level, log_outputs, logger
        )
    except Exception as setup_error:
        logger.error(f"Infrastructure setup failed: {setup_error}")
        raise


def initialize_components_sync(
    app_home: Path, args: argparse.Namespace
) -> tuple[logging.Logger, EventOrchestrator]:
    """Initialize all components synchronously."""
    logger = _initialize_logging_sync(
        app_home=app_home, log_level=args.log_level, log_outputs=args.log_outputs
    )
    logger.debug(f"Starting Ananta with app directory: {app_home}")
    if args.actions:
        logger.debug(f"Starting prompt: {args.actions}")
    else:
        pass

    if args.plugin_config:
        pass

    orchestrator = _initialize_orchestrator_sync(
        app_home=app_home,
        starting_prompt=args.actions,
        max_consecutive_errors=args.max_consecutive_errors,
        max_actions_per_cycle=args.max_actions_per_cycle,
        plugin_config=args.plugin_config,
        default_inference_provider=args.default_inference_provider,
    )

    plugin_manager = _create_bootstrap_plugin_manager(logger)
    _execute_infrastructure_setup(
        orchestrator, plugin_manager, app_home, args.log_level, args.log_outputs, logger
    )

    return logger, orchestrator


def _initialize_logging_sync(
    *, app_home: Path, log_level: str, log_outputs: str | None = None
) -> logging.Logger:
    log_level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }
    numeric_log_level = log_level_map.get(log_level.lower(), logging.WARNING)

    # Priority: CLI argument > Environment variable > Default (no file logging)
    if log_outputs:
        enabled_outputs = [output.strip() for output in log_outputs.split(",") if output.strip()]
    else:
        # Use environment variable outputs or default to file logging
        env_outputs = EnvironmentConfig.log_outputs()
        if env_outputs != ["console"]:  # Not the default
            enabled_outputs = env_outputs
        else:
            # CRITICAL FIX: Default to file logging only during bootstrap to prevent circular dependency
            enabled_outputs = ["file"]

    try:
        initialize_config(str(app_home))
        logger = setup_logging(
            app_home=str(app_home),
            plugin_name="ananta",
            log_level=numeric_log_level,
            enabled_outputs=enabled_outputs,
        )
        files_deleted, bytes_freed = purge_old_logs(str(Path(app_home) / "data" / "logs"))
        if files_deleted:
            logger.info(
                f"Log retention sweep: deleted {files_deleted} file(s) "
                f"older than {DEFAULT_LOG_RETENTION_DAYS} days ({bytes_freed / 1024 / 1024:.1f} MB freed)"
            )
        return logger
    except Exception as e:
        raise SystemError(
            message=f"Failed to initialize logging: {e}",
            error_code=ErrorCode.SYSTEM_GENERIC,
            details={"app_home": str(app_home), "action_status": ActionStatus.ERROR.value},
            original_error=e,
            severity=ErrorSeverity.CRITICAL,
        ) from e


def _initialize_orchestrator_sync(
    *,
    app_home: Path,
    starting_prompt: str,
    max_consecutive_errors: int,
    max_actions_per_cycle: int,
    plugin_config: dict[str, dict[str, object]],
    default_inference_provider: str | None = None,
) -> EventOrchestrator:
    logger = logging.getLogger(__name__)
    try:
        # Always use EventOrchestrator (legacy orchestrator removed)
        orchestrator = EventOrchestrator(
            starting_prompt=starting_prompt,
            max_consecutive_errors=max_consecutive_errors,
            max_actions_per_cycle=max_actions_per_cycle,
            plugin_config=plugin_config,
            default_inference_provider=default_inference_provider,
        )

        # Load initial state while services are still in bootstrap mode
        try:
            orchestrator.state_manager.load_sync()
        except Exception as load_error:
            logger.error(f"Failed to load initial state: {load_error}")
            import traceback

            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

        # Service transitions will be handled in Phase 2 by initialize_components_sync

        return orchestrator
    except Exception as e:
        logger.error(f"Exception in _initialize_orchestrator: {e}")
        import traceback

        logger.error(f"Full traceback: {traceback.format_exc()}")
        raise SystemError(
            message=f"Failed to initialize orchestrator: {e}",
            error_code=ErrorCode.SYSTEM_GENERIC,
            details={"app_home": str(app_home), "action_status": ActionStatus.ERROR.value},
            original_error=e,
            severity=ErrorSeverity.CRITICAL,
        ) from e


async def shutdown_components(
    orchestrator: EventOrchestrator | None = None,
    run_task: Optional["asyncio.Task[None]"] = None,
) -> None:
    """Gracefully shut down all components."""
    logger = logging.getLogger(__name__)
    logger.debug("Starting component shutdown...")

    if orchestrator:
        # Signal shutdown
        orchestrator.shutdown_event.set()
        orchestrator.event.set()  # Wake up any waiting

        # Give orchestrator time to finish current action
        await asyncio.sleep(0.5)

        # Run cleanup
        try:
            await orchestrator.cleanup()
        except Exception as e:
            logger.error(f"Error during orchestrator cleanup: {e}")

    if run_task and not run_task.done():
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    logger.debug("Component shutdown complete")


def _setup_environment_or_exit(args: argparse.Namespace) -> Path:
    """Set up environment, exit on failure.

    Mirrors every FATAL stderr write into ``logger.critical`` so the
    failure lands in the file logger too (2026-06-02 wedge diagnosis:
    silent FATALs cost four launch cycles when ``tail`` of the file log
    showed nothing while stderr had the answer).
    """
    fatal_logger = logging.getLogger(__name__)
    try:
        return setup_environment(args)
    except ValidationError as e:
        import traceback

        header = f"FATAL ValidationError during setup: {e}"
        print(header, file=sys.stderr)
        fatal_logger.critical(header)
        if e.details:
            detail_line = f"Details: {e.details}"
            print(detail_line, file=sys.stderr)
            fatal_logger.critical(detail_line)
        traceback.print_exc()
        fatal_logger.critical("FATAL traceback:\n%s", traceback.format_exc())
        sys.exit(1)
    except Exception as e:
        import traceback

        header = f"FATAL Exception during setup: {e}"
        print(header, file=sys.stderr)
        fatal_logger.critical(header)
        traceback.print_exc()
        fatal_logger.critical("FATAL traceback:\n%s", traceback.format_exc())
        sys.exit(1)


def _run_orchestrator_or_exit(app_home: Path, args: argparse.Namespace) -> None:
    """Initialize components and run orchestrator, exit on failure.

    Mirrors every FATAL stderr write into ``logger.critical`` so the
    failure lands in the file logger too (2026-06-02 wedge diagnosis:
    Slice A+B decorator misplacement raised inside ``run`` and the FATAL
    only hit stderr — invisible to ``tail profile/data/logs/*.log``).
    By the time we get here ``setup_logging`` has wired the file handler,
    so ``logger.critical`` reliably reaches the file.
    """
    fatal_logger = logging.getLogger(__name__)
    try:
        logger, orchestrator = initialize_components_sync(app_home, args)
        # F1 startup diagnostic — NEVER BLOCKING per the 2026-06-15 PT lock.
        # REPO_ROOT derives from app_home (canonical layout: <repo_root>/profile/...).
        emit_startup_diagnostic(logger, app_home.resolve().parent)
        logger.debug("Starting Phase 3: Event-driven runtime processing")
        asyncio.run(orchestrator.run())
        # Propagate the SIGTERM disposition as the process exit code: 0 for a
        # normal completion or an intentional blue-green drain (launchd must NOT
        # respawn this color), non-zero for a stray SIGTERM of a live color
        # (launchd respawns = correct supervision). ``SystemExit`` derives from
        # BaseException, so it is NOT caught by the ``except Exception`` below —
        # this is the clean process-exit point on the success path.
        sys.exit(orchestrator.sigterm_exit_code)
    except AnantaError as e:
        import traceback

        header = f"FATAL AnantaError during runtime: {e}"
        print(header, file=sys.stderr)
        fatal_logger.critical(header)
        traceback.print_exc()
        fatal_logger.critical("FATAL traceback:\n%s", traceback.format_exc())
        sys.exit(1)
    except Exception as e:
        import traceback

        header = f"FATAL Exception during runtime: {e}"
        print(header, file=sys.stderr)
        fatal_logger.critical(header)
        traceback.print_exc()
        fatal_logger.critical("FATAL traceback:\n%s", traceback.format_exc())
        sys.exit(1)


def sync_main() -> None:
    """Main entry point for synchronous CLI execution."""
    args = parse_cli_arguments()
    app_home = _setup_environment_or_exit(args)
    _run_orchestrator_or_exit(app_home, args)


def main() -> int:
    sync_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
