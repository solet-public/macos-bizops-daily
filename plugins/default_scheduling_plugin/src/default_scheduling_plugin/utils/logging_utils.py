"""Safe logging utilities to eliminate logger guard checks.

This module provides wrapper functions that safely handle None loggers,
eliminating the need for 'if self.logger:' checks throughout the codebase.
"""

from __future__ import annotations

import logging
from typing import Any

RELOAD_SAFE = True


def safe_log_debug(logger: logging.Logger | None, message: str, *args: Any, **kwargs: Any) -> None:
    """Safely log a debug message, handling None logger.

    Args:
        logger: Logger instance or None
        message: Log message format string
        *args: Positional arguments for message formatting
        **kwargs: Keyword arguments passed to logger.debug()
    """
    if logger is not None:
        logger.debug(message, *args, **kwargs)


def safe_log_info(logger: logging.Logger | None, message: str, *args: Any, **kwargs: Any) -> None:
    """Safely log an info message, handling None logger.

    Args:
        logger: Logger instance or None
        message: Log message format string
        *args: Positional arguments for message formatting
        **kwargs: Keyword arguments passed to logger.debug()
    """
    if logger is not None:
        logger.debug(message, *args, **kwargs)


def safe_log_warning(
    logger: logging.Logger | None, message: str, *args: Any, **kwargs: Any
) -> None:
    """Safely log a warning message, handling None logger.

    Args:
        logger: Logger instance or None
        message: Log message format string
        *args: Positional arguments for message formatting
        **kwargs: Keyword arguments passed to logger.warning()
    """
    if logger is not None:
        logger.warning(message, *args, **kwargs)


def safe_log_error(logger: logging.Logger | None, message: str, *args: Any, **kwargs: Any) -> None:
    """Safely log an error message, handling None logger.

    Args:
        logger: Logger instance or None
        message: Log message format string
        *args: Positional arguments for message formatting
        **kwargs: Keyword arguments passed to logger.error()
    """
    if logger is not None:
        logger.error(message, *args, **kwargs)
