"""Filesystem and general utility functions for the Ananta platform."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar, cast

from ananta.constants import (
    DATA_DIRECTORY_NAME,
    DEBUG_LOGS_DIRECTORY_NAME,
    LOGS_DIRECTORY_NAME,
    PLUGIN_DATA_DIRECTORY_NAME,
    PLUGIN_LOGS_DIRECTORY_NAME,
)
from ananta.core.domain.enums import ActionStatus, ErrorSeverity
from ananta.core.domain.error_codes import ErrorCode
from ananta.error_handling import AnantaError, FrameworkError

T = TypeVar("T")
JSONValue = dict[str, object] | list[object] | str | int | float | bool | None
JSONData = dict[str, JSONValue] | list[JSONValue]


def get_env_variable[T](name: str, default: T | None = None) -> str | T:
    return os.environ.get(name, cast(T, default))


def create_directory(directory_path: str | Path) -> Path:
    """Create a directory and all parent directories.

    Args:
        directory_path: Path to the directory to create

    Returns:
        Path object for the created directory

    Raises:
        FrameworkError: If directory creation fails (permission denied, OS error)
    """
    path = Path(directory_path)
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except PermissionError as e:
        raise FrameworkError(
            message=f"Permission denied creating directory {directory_path}",
            error_code=ErrorCode.PERMISSION_GENERIC,
            details={"path": str(directory_path), "action_status": ActionStatus.ERROR.value},
            original_error=e,
            severity=ErrorSeverity.ERROR,
        ) from e
    except OSError as e:
        raise FrameworkError(
            message=f"Error creating directory {directory_path}",
            error_code=ErrorCode.RESOURCE_GENERIC,
            details={"path": str(directory_path), "action_status": ActionStatus.ERROR.value},
            original_error=e,
            severity=ErrorSeverity.ERROR,
        ) from e


def generate_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def create_log_filename(directory: str | Path, prefix: str = "ananta") -> str:
    timestamp = generate_timestamp()
    return str(Path(directory) / f"{prefix}_{timestamp}.log")


def _safely_create_directories(directories: dict[str, str]) -> dict[str, str]:
    try:
        for directory in directories.values():
            create_directory(directory)
        return directories
    except FrameworkError as e:
        raise FrameworkError(
            message=f"Failed to create required directories: {e.message}",
            error_code=ErrorCode.SYSTEM_GENERIC,
            details={
                "directories": list(directories.keys()),
                "original_error": str(e),
                "action_status": ActionStatus.ERROR.value,
            },
            original_error=e,
            severity=ErrorSeverity.CRITICAL,
        ) from e


def create_app_directories(APP_HOME: str | Path) -> dict[str, str]:
    base_path = Path(APP_HOME)

    directories = {
        "data_directory": str(base_path / DATA_DIRECTORY_NAME),
        "logs_directory": str(base_path / LOGS_DIRECTORY_NAME),
        "temp_directory": str(base_path / DATA_DIRECTORY_NAME / "temp"),
        "plugin_logs_directory": str(
            base_path / LOGS_DIRECTORY_NAME / PLUGIN_LOGS_DIRECTORY_NAME / "ananta"
        ),
        "plugin_data_directory": str(
            base_path / DATA_DIRECTORY_NAME / PLUGIN_DATA_DIRECTORY_NAME / "ananta"
        ),
    }

    return _safely_create_directories(directories)


def create_plugin_directories(APP_HOME: str | Path, plugin_name: str) -> dict[str, str]:
    base_path = Path(APP_HOME)

    directories = {
        "data_directory": str(
            base_path / DATA_DIRECTORY_NAME / PLUGIN_DATA_DIRECTORY_NAME / plugin_name
        ),
        "logs_directory": str(
            base_path / LOGS_DIRECTORY_NAME / PLUGIN_LOGS_DIRECTORY_NAME / plugin_name
        ),
        "debug_directory": str(
            base_path
            / LOGS_DIRECTORY_NAME
            / PLUGIN_LOGS_DIRECTORY_NAME
            / plugin_name
            / DEBUG_LOGS_DIRECTORY_NAME
        ),
    }

    try:
        return _safely_create_directories(directories)
    except FrameworkError as e:
        raise FrameworkError(
            message=f"Failed to create plugin directories for {plugin_name}: {e.message}",
            error_code=ErrorCode.SYSTEM_GENERIC,
            details={
                "APP_HOME": str(APP_HOME),
                "plugin_name": plugin_name,
                "action_status": ActionStatus.ERROR.value,
            },
            original_error=e,
            severity=ErrorSeverity.CRITICAL,
        ) from e


def load_json_file(file_path: str | Path, default: JSONData | None = None) -> JSONData:
    if default is None:
        default = cast(JSONData, {})

    path = Path(file_path)
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return cast(JSONData, json.load(f))
        return default
    except json.JSONDecodeError as e:
        raise FrameworkError(
            message=f"Invalid JSON format in file {file_path}",
            error_code=ErrorCode.JSON_PARSE_ERROR,
            details={"file_path": str(file_path), "action_status": ActionStatus.ERROR.value},
            original_error=e,
            severity=ErrorSeverity.ERROR,
        ) from e
    except (PermissionError, FileNotFoundError, OSError) as e:
        error_code = (
            ErrorCode.PERMISSION_GENERIC
            if isinstance(e, PermissionError)
            else (
                ErrorCode.FILE_NOT_FOUND
                if isinstance(e, FileNotFoundError)
                else ErrorCode.FILE_ACCESS_ERROR
            )
        )

        raise FrameworkError(
            message=f"Error reading file {file_path}: {str(e)}",
            error_code=error_code,
            details={"file_path": str(file_path), "action_status": ActionStatus.ERROR.value},
            original_error=e,
            severity=ErrorSeverity.ERROR,
        ) from e
    except Exception as e:
        raise FrameworkError(
            message=f"Unexpected error reading file {file_path}",
            error_code=ErrorCode.UNKNOWN_ERROR,
            details={"file_path": str(file_path), "action_status": ActionStatus.ERROR.value},
            original_error=e,
            severity=ErrorSeverity.CRITICAL,
        ) from e


def save_json_file(file_path: str | Path, data: JSONData) -> None:
    path = Path(file_path)
    try:
        create_directory(path.parent)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except TypeError as e:
        raise FrameworkError(
            message="Invalid data type for JSON serialization",
            error_code=ErrorCode.JSON_VALIDATION_ERROR,
            details={"file_path": str(file_path), "action_status": ActionStatus.ERROR.value},
            original_error=e,
            severity=ErrorSeverity.ERROR,
        ) from e
    except (PermissionError, OSError) as e:
        error_code = (
            ErrorCode.PERMISSION_GENERIC
            if isinstance(e, PermissionError)
            else ErrorCode.FILE_WRITE_ERROR
        )

        raise FrameworkError(
            message=f"Error writing file {file_path}: {str(e)}",
            error_code=error_code,
            details={"file_path": str(file_path), "action_status": ActionStatus.ERROR.value},
            original_error=e,
            severity=ErrorSeverity.ERROR,
        ) from e
    except Exception as e:
        raise FrameworkError(
            message=f"Unexpected error writing file {file_path}",
            error_code=ErrorCode.UNKNOWN_ERROR,
            details={"file_path": str(file_path), "action_status": ActionStatus.ERROR.value},
            original_error=e,
            severity=ErrorSeverity.CRITICAL,
        ) from e


def is_valid_error_code_format(error_code: str) -> bool:
    parts = error_code.split(".")
    return len(parts) == 2 and all(parts)


def format_error_code(namespace: str, code: str) -> str:
    return f"{namespace}.{code}"


def truncate_message(message: str, max_length: int = 100) -> str:
    if len(message) <= max_length:
        return message
    return message[: max_length - 3] + "..."


def get_action_status_from_error(error: AnantaError | None = None) -> str:
    return ActionStatus.ERROR.value if error else ActionStatus.COMPLETED.value
