import logging
from pathlib import Path

from ananta.logging_setup import get_logger as core_get_logger
from ananta.logging_setup import setup_logging as core_setup_logging

from .constants import PLUGIN_NAME


def create_plugin_log_directory(app_home: str) -> str:
    log_dir = Path(app_home) / "logs" / "plugin_logs" / PLUGIN_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    return str(log_dir)


def setup_logging(
    app_home: str | None = None,
    log_file: str | None = None,
    log_level: int = logging.INFO,
    plugin_name: str = PLUGIN_NAME,
) -> logging.Logger:
    # Let the framework control log outputs via ANANTA_LOG_OUTPUTS environment variable
    # Plugins should only control log level, not outputs (file vs state vs console)
    return core_setup_logging(
        app_home=app_home,
        plugin_name=plugin_name,
        log_level=log_level,
    )


def get_logger() -> logging.Logger:
    return core_get_logger(PLUGIN_NAME)
