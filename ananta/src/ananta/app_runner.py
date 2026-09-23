import asyncio
import logging
import sys
import traceback
from collections.abc import Callable, Coroutine
from typing import NoReturn

from ananta.constants import (
    ERROR_EXIT_CODE_EXTERNAL,
    ERROR_EXIT_CODE_FRAMEWORK,
    ERROR_EXIT_CODE_PLUGIN,
    ExitCodes,
)
from ananta.core.domain.enums import ErrorSeverity
from ananta.core.domain.error_codes import ErrorCode
from ananta.error_handling import (
    AnantaError,
    ExternalError,
    FrameworkError,
    log_exception,
)


def _handle_runtime_error(e: RuntimeError) -> int:
    """Handle RuntimeError and return appropriate exit code."""
    error_severity = ErrorSeverity.CRITICAL

    if "asyncio.run() cannot be called from a running event loop" in str(e):
        error_message = f"Asyncio runtime error: {e}"
        error_code: str = "app_runner.running_event_loop"
    else:
        error_message = f"Runtime error: {e}"
        error_code = ErrorCode.RUNTIME_ERROR
        traceback.print_exc()

    error = FrameworkError(
        message=error_message,
        error_code=error_code,
        original_error=e,
        severity=error_severity,
        details={"context": "run_async_app"},
    )
    logging.error(error.message)
    return ExitCodes.FRAMEWORK_ERROR


def _handle_connection_error(e: ConnectionError) -> int:
    """Handle ConnectionError and return appropriate exit code."""
    connection_error = ExternalError(
        message=f"Connection error: {e}",
        error_code=ErrorCode.NETWORK_GENERIC,
        original_error=e,
        severity=ErrorSeverity.ERROR,
        details={"context": "run_async_app"},
    )
    logging.error(connection_error.message)
    return ExitCodes.CONNECTION_ERROR


def _handle_permission_error(e: PermissionError) -> int:
    """Handle PermissionError and return appropriate exit code."""
    error = FrameworkError(
        message=f"Permission denied: {e}",
        error_code=ErrorCode.PERMISSION_GENERIC,
        original_error=e,
        severity=ErrorSeverity.ERROR,
        details={"context": "run_async_app", "operation": "file_access"},
    )
    logging.error(error.message)
    return ExitCodes.PERMISSION_ERROR


def _handle_file_not_found_error(e: FileNotFoundError) -> int:
    """Handle FileNotFoundError and return appropriate exit code."""
    error = FrameworkError(
        message=f"File not found: {e}",
        error_code=ErrorCode.FILE_NOT_FOUND,
        original_error=e,
        severity=ErrorSeverity.ERROR,
        details={"context": "run_async_app", "path": str(e)},
    )
    logging.error(error.message)
    return ExitCodes.FILE_NOT_FOUND


def _handle_timeout_error(e: TimeoutError) -> int:
    """Handle TimeoutError and return appropriate exit code."""
    timeout_error = ExternalError(
        message=f"Operation timed out: {e}",
        error_code=ErrorCode.TIMEOUT_ERROR,
        original_error=e,
        severity=ErrorSeverity.ERROR,
        details={"context": "run_async_app"},
    )
    logging.error(timeout_error.message)
    return ExitCodes.TIMEOUT_ERROR


def _handle_os_error(e: OSError) -> int:
    """Handle OSError and return appropriate exit code."""
    error = FrameworkError(
        message=f"OS error: {e}",
        error_code=ErrorCode.RESOURCE_GENERIC,
        original_error=e,
        severity=ErrorSeverity.ERROR,
        details={"context": "run_async_app"},
    )
    logging.error(error.message)
    return ExitCodes.OS_ERROR


def _handle_ananta_error(e: AnantaError) -> int:
    """Handle AnantaError and return appropriate exit code."""
    log_exception(e, logger=logging.getLogger(), level=logging.ERROR)
    return _get_exit_code_for_error_type(error_type=e.error_type)


def _handle_unknown_error(e: Exception) -> int:
    """Handle unknown Exception and return appropriate exit code."""
    error = FrameworkError(
        message=f"Unhandled error: {e}",
        error_code=ErrorCode.UNKNOWN_ERROR,
        original_error=e,
        severity=ErrorSeverity.CRITICAL,
        details={"context": "run_async_app"},
    )
    log_exception(error, logger=logging.getLogger(), level=logging.ERROR)
    traceback.print_exc()
    return ExitCodes.UNKNOWN_ERROR


def run_async_app(async_main_func: Callable[[], Coroutine[object, object, object]]) -> NoReturn:
    """Run async application with comprehensive error handling."""
    exit_code = _execute_async_main(async_main_func)
    logging.info(f"Ananta exiting with code: {exit_code}")
    sys.exit(exit_code)


def _execute_async_main(async_main_func: Callable[[], Coroutine[object, object, object]]) -> int:
    """Execute async main function and handle all exceptions."""
    try:
        asyncio.run(async_main_func())
        return 0
    except KeyboardInterrupt:
        logging.info("\nShutting down Ananta framework...")
        return ExitCodes.KEYBOARD_INTERRUPT
    except asyncio.CancelledError:
        logging.info("Ananta execution was cancelled")
        return ExitCodes.KEYBOARD_INTERRUPT
    except BaseException as e:
        return _dispatch_exception_handler(e)


def _dispatch_exception_handler(e: BaseException) -> int:
    """Dispatch exception to appropriate handler based on type."""
    if isinstance(e, RuntimeError):
        return _handle_runtime_error(e)
    if isinstance(e, AnantaError):
        return _handle_ananta_error(e)
    return _dispatch_os_exception_handler(e)


def _dispatch_os_exception_handler(e: BaseException) -> int:
    """Dispatch OS-related exceptions to appropriate handlers."""
    if isinstance(e, ConnectionError):
        return _handle_connection_error(e)
    if isinstance(e, PermissionError):
        return _handle_permission_error(e)
    if isinstance(e, FileNotFoundError):
        return _handle_file_not_found_error(e)
    if isinstance(e, TimeoutError):
        return _handle_timeout_error(e)
    if isinstance(e, OSError):
        return _handle_os_error(e)
    if isinstance(e, Exception):
        return _handle_unknown_error(e)
    raise e


def _get_exit_code_for_error_type(*, error_type: str) -> int:
    error_exit_codes = {
        "framework_error": ERROR_EXIT_CODE_FRAMEWORK,
        "plugin_error": ERROR_EXIT_CODE_PLUGIN,
        "external_error": ERROR_EXIT_CODE_EXTERNAL,
    }

    return error_exit_codes.get(error_type, ExitCodes.UNKNOWN_ERROR)
