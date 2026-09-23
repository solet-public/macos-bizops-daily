import datetime
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, cast

from ananta.constants import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_FORMAT,
    DEFAULT_LOG_LEVEL,
    DEFAULT_LOG_MAX_SIZE,
    DEFAULT_LOG_OUTPUTS,
    DEFAULT_LOG_RETENTION_DAYS,
)
from ananta.core.config.config_provider import ConfigProvider
from ananta.core.plugins.plugin_contracts import ErrorCode, ErrorSeverity
from ananta.error_handling import FrameworkError

# Silence noisy third-party library loggers at module load time
# This must happen before any of these libraries are imported/used
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "requests",
    "openai",
    "anthropic",
    "asyncio",
    "apscheduler",
    "slack_sdk",
    "discord",
)
for _logger_name in _NOISY_LOGGERS:
    logging.getLogger(_logger_name).setLevel(logging.WARNING)

LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class DeferredLogger:
    def __init__(
        self,
        outputs: list[str],
        app_home: str = "",
        plugin_name: str = "ananta",
        log_level: int | str = DEFAULT_LOG_LEVEL,
        config_provider: ConfigProvider | None = None,
    ) -> None:
        self._initialize_attributes(outputs, app_home, plugin_name, log_level, config_provider)
        available_outputs = self._determine_available_outputs(outputs)
        self._setup_logger(available_outputs)
        self._log_initial_status(outputs)

    def _initialize_attributes(
        self,
        outputs: list[str],
        app_home: str,
        plugin_name: str,
        log_level: int | str,
        config_provider: ConfigProvider | None,
    ) -> None:
        """Initialize instance attributes."""
        self.requested_outputs = outputs.copy()
        self.pending_outputs: list[str] = []
        self.app_home = app_home
        self.plugin_name = plugin_name
        # Resolve string log level to int
        if isinstance(log_level, str):
            self.log_level = LOG_LEVELS.get(log_level.lower(), logging.INFO)
        else:
            self.log_level = log_level
        self.config_provider = config_provider

    def _determine_available_outputs(self, outputs: list[str]) -> list[str]:
        """Determine which outputs are immediately available."""
        available_outputs = []

        if "console" in outputs:
            available_outputs.append("console")

        if "file" in outputs:
            available_outputs.append("file")

        return available_outputs

    def _setup_logger(self, available_outputs: list[str]) -> None:
        """Set up the logger with available outputs."""
        self.logger = setup_logging(
            app_home=self.app_home,
            plugin_name=self.plugin_name,
            log_level=self.log_level,
            config_provider=self.config_provider,
            enabled_outputs=available_outputs,
        )

    def _log_initial_status(self, outputs: list[str]) -> None:
        """Log initial status after logger is created."""
        # No deferred logging needed anymore
        pass

    # Delegate all logging methods to the underlying logger
    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        return self.logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        return self.logger.debug(msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        return self.logger.error(msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        return self.logger.error(msg, *args, **kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        return self.logger.critical(msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        return self.logger.exception(msg, *args, **kwargs)


def create_log_directory(directory_path: str | Path) -> str:
    """Create a log directory. Fails if creation is not possible.

    Args:
        directory_path: Path to the directory to create

    Returns:
        String path to the created directory

    Raises:
        FrameworkError: If directory creation fails
    """
    path = Path(directory_path)
    try:
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    except PermissionError as e:
        raise FrameworkError(
            message=f"Permission denied creating log directory: {directory_path}",
            error_code=ErrorCode.PERMISSION_GENERIC,
            details={"path": str(directory_path)},
            original_error=e,
            severity=ErrorSeverity.ERROR,
        ) from e
    except OSError as e:
        raise FrameworkError(
            message=f"OS error creating log directory: {directory_path}",
            error_code=ErrorCode.RESOURCE_GENERIC,
            details={"path": str(directory_path)},
            original_error=e,
            severity=ErrorSeverity.ERROR,
        ) from e


def get_log_filename(plugin_name: str, _suffix: str = "", app_home: str = "") -> str:
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")

    # Extract app name from APP_HOME path (e.g., /path/to/default_console -> default_console)
    if app_home:
        app_name = Path(app_home).name
    else:
        app_name = "application"

    return f"{timestamp}_{app_name}.log"


def create_app_log_directory(app_home: str) -> str:
    """Create the main application log directory."""
    log_dir = Path(app_home) / "data" / "logs"
    return create_log_directory(log_dir)


def purge_old_logs(
    log_dir: str, retention_days: int = DEFAULT_LOG_RETENTION_DAYS
) -> tuple[int, int]:
    """Delete every file under ``log_dir`` (recursively) whose mtime is older
    than ``retention_days``.

    ``RotatingFileHandler``'s ``maxBytes``/``backupCount`` only bounds a single
    already-open log file; the daily ``{date}_{app_name}.log`` naming scheme
    means each new day accumulates a fresh file that rotation never touches,
    and the per-attempt logs (``green_spawn_*``, ``preflight_probe_*``,
    ``supervisor_spawn_*``) accumulate the same way. This is the sweep that
    bounds the directory as a whole. Returns (files_deleted, bytes_freed).
    """
    root = Path(log_dir)
    if not root.is_dir():
        return (0, 0)

    cutoff = datetime.datetime.now().timestamp() - (retention_days * 86400)
    files_deleted = 0
    bytes_freed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat_result = path.stat()
            if stat_result.st_mtime >= cutoff:
                continue
            size = stat_result.st_size
            path.unlink()
        except OSError:
            continue
        files_deleted += 1
        bytes_freed += size
    return (files_deleted, bytes_freed)


def create_plugin_log_directory(app_home: str, plugin_name: str) -> str:
    """Create a plugin's log directory."""
    log_dir = Path(app_home) / "data" / "logs" / "plugin_logs" / plugin_name
    return create_log_directory(log_dir)


def create_plugin_debug_directory(app_home: str, plugin_name: str) -> str:
    """Create a plugin's debug log directory."""
    debug_dir = Path(app_home) / "data" / "logs" / "plugin_logs" / plugin_name / "debug"
    return create_log_directory(debug_dir)


def create_plugin_data_directory(app_home: str, plugin_name: str) -> str:
    """Create a plugin's data directory."""
    data_dir = Path(app_home) / "data" / "plugin_data" / plugin_name
    return create_log_directory(data_dir)


def _setup_console_handler(
    root_logger: logging.Logger,
    enabled_outputs: list[str],
    log_level: int,
    formatter: logging.Formatter,
) -> None:
    """Set up console logging handler if enabled."""
    if "console" not in enabled_outputs:
        return

    # Check if console handler already exists
    existing_handler = next(
        (h for h in root_logger.handlers if isinstance(h, logging.StreamHandler)),
        None,
    )
    if existing_handler:
        # Update level if requested level is lower (more verbose)
        if log_level < existing_handler.level:
            existing_handler.setLevel(log_level)
        return

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)


def _setup_file_handler(
    root_logger: logging.Logger,
    enabled_outputs: list[str],
    app_home: str | None,
    log_dir: str | None,
    log_file: str | None,
    plugin_name: str,
    log_level: int,
    formatter: logging.Formatter,
    max_size: int,
    backup_count: int,
    add_console_handler: bool,
    logger: logging.Logger,
) -> None:
    """Set up file logging handler if enabled."""
    if "file" not in enabled_outputs:
        return

    try:
        log_path = _determine_log_file_path(app_home, log_dir, log_file, plugin_name)
        _add_file_handler_if_needed(
            root_logger, log_path, log_level, formatter, max_size, backup_count
        )
    except Exception as e:
        _handle_file_handler_error(e, add_console_handler, logger)


def _determine_log_file_path(
    app_home: str | None, log_dir: str | None, log_file: str | None, plugin_name: str
) -> str:
    """Determine the full path for the log file."""
    effective_app_home = _get_effective_app_home(app_home, log_dir, plugin_name)
    directory_path = _get_directory_path(effective_app_home, log_dir)
    log_filename = log_file or get_log_filename(plugin_name, app_home=effective_app_home or "")
    return str(Path(directory_path) / log_filename)


def _get_effective_app_home(
    app_home: str | None, log_dir: str | None, plugin_name: str
) -> str | None:
    """Get effective app home, checking environment if needed."""
    if app_home is not None or log_dir is not None:
        return app_home

    effective_app_home = os.environ.get("APP_HOME")
    if not effective_app_home:
        raise FrameworkError(
            message="APP_HOME not specified. Either pass app_home parameter or set APP_HOME environment variable",
            error_code=ErrorCode.CONFIGURATION_ERROR,
            details={"plugin_name": plugin_name},
            severity=ErrorSeverity.ERROR,
        )
    return effective_app_home


def _get_directory_path(effective_app_home: str | None, log_dir: str | None) -> str:
    """Get directory path for log files."""
    if effective_app_home is not None:
        return create_app_log_directory(effective_app_home)
    return cast(str, log_dir)


def _add_file_handler_if_needed(
    root_logger: logging.Logger,
    log_path: str,
    log_level: int,
    formatter: logging.Formatter,
    max_size: int,
    backup_count: int,
) -> None:
    """Add file handler if it doesn't already exist, or update level if lower."""
    existing_handler = _get_existing_file_handler(root_logger, log_path)
    if existing_handler:
        # Update handler level if requested level is lower (more verbose)
        if log_level < existing_handler.level:
            existing_handler.setLevel(log_level)
        return

    file_handler = RotatingFileHandler(log_path, maxBytes=max_size, backupCount=backup_count)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def _get_existing_file_handler(
    root_logger: logging.Logger, log_path: str
) -> RotatingFileHandler | None:
    """Get existing file handler for this path, if any."""
    abs_log_path = os.path.abspath(log_path)
    for handler in root_logger.handlers:
        if isinstance(handler, RotatingFileHandler) and hasattr(handler, "baseFilename"):
            if handler.baseFilename == abs_log_path:
                return handler
    return None


def _handle_file_handler_error(
    error: Exception, add_console_handler: bool, logger: logging.Logger
) -> None:
    """Handle errors during file handler setup."""
    if add_console_handler:
        logger.error(f"Failed to set up file logging: {error}")


def setup_logging(
    app_home: str | None = None,
    log_dir: str | None = None,
    log_file: str | None = None,
    log_level: int | str = DEFAULT_LOG_LEVEL,
    log_format: str = DEFAULT_LOG_FORMAT,
    max_size: int = DEFAULT_LOG_MAX_SIZE,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
    add_console_handler: bool = True,
    _add_file_handler: bool = True,
    plugin_name: str = "ananta",
    config_provider: ConfigProvider | None = None,
    enabled_outputs: list[str] | None = None,
) -> logging.Logger:
    """Set up logging with configurable outputs (console, file, state).

    REFACTORED: Extracted handler creation methods to reduce complexity from D(30).
    """
    # Get root and plugin loggers without destroying existing handlers
    root_logger = logging.getLogger()
    logger = logging.getLogger(plugin_name)

    if config_provider:
        log_level = config_provider.get_log_level(default="info")
        log_format = config_provider.get_log_format(default=DEFAULT_LOG_FORMAT)
        max_size = config_provider.get_log_max_size(default=DEFAULT_LOG_MAX_SIZE)
        backup_count = config_provider.get_log_backup_count(default=DEFAULT_LOG_BACKUP_COUNT)
        # FRAMEWORK ARCHITECTURE: Plugins can only control log LEVEL, not OUTPUTS
        # Log outputs are controlled exclusively by framework via enabled_outputs parameter

    if enabled_outputs is None:
        enabled_outputs = DEFAULT_LOG_OUTPUTS.copy()

    resolved_log_level: int
    if isinstance(log_level, str):
        resolved_log_level = LOG_LEVELS.get(log_level.lower(), logging.INFO)
    else:
        resolved_log_level = log_level

    logger.setLevel(resolved_log_level)
    formatter = logging.Formatter(log_format)

    # Set up different logging handlers based on enabled outputs
    _setup_console_handler(root_logger, enabled_outputs, resolved_log_level, formatter)

    _setup_file_handler(
        root_logger,
        enabled_outputs,
        app_home,
        log_dir,
        log_file,
        plugin_name,
        resolved_log_level,
        formatter,
        max_size,
        backup_count,
        add_console_handler,
        logger,
    )

    # Set root logger level to ensure all child loggers inherit correctly
    root_logger.setLevel(resolved_log_level)

    # Silence noisy third-party library loggers
    noisy_loggers = [
        "httpx",
        "httpcore",
        "urllib3",
        "requests",
        "openai",
        "anthropic",
        "asyncio",
    ]
    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    # CRITICAL: Redirect Python warnings to logging system
    # This ensures third-party library warnings (diffusers, torch, pyworld, etc.)
    # appear in log files instead of polluting the console
    logging.captureWarnings(True)

    # Configure the warnings logger to use the same level as the root logger
    warnings_logger = logging.getLogger("py.warnings")
    warnings_logger.setLevel(resolved_log_level)

    # CRITICAL: Disable Python's lastResort console handler
    if "console" not in enabled_outputs:
        # Add a null handler to prevent lastResort console logging
        null_handler = logging.NullHandler()
        root_logger.addHandler(null_handler)
        # Completely disable lastResort handler
        logging.lastResort = None

    # CRITICAL DEBUG: Log successful completion using sys.stderr since normal logging may be broken
    import sys

    sys.stderr.flush()

    return logger


def get_plugin_directories(app_home: str, plugin_name: str) -> dict[str, str]:
    """Get (and create) standard plugin directories."""
    directories = {
        "log_dir": create_plugin_log_directory(app_home, plugin_name),
        "debug_dir": create_plugin_debug_directory(app_home, plugin_name),
        "data_dir": create_plugin_data_directory(app_home, plugin_name),
    }
    return directories


def get_logger(plugin_name: str = "ananta") -> logging.Logger:
    return logging.getLogger(plugin_name)


def configure_plugin_logging(
    app_home: str,
    plugin_name: str,
    config_provider: ConfigProvider | None = None,
) -> logging.Logger:
    return setup_logging(
        app_home=app_home,
        plugin_name=plugin_name,
        config_provider=config_provider,
    )


def create_plugin_directories(app_home: str, plugin_name: str) -> dict[str, str]:
    """Create all standard directories for a plugin.

    This is an alias for get_plugin_directories() for clarity.
    """
    return get_plugin_directories(app_home, plugin_name)
