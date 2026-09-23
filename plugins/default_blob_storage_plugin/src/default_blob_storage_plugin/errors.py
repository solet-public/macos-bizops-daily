from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast

from ananta.core.domain.types import ActionResult


class BlobStorageErrorCode(Enum):
    INVALID_BLOB_ID = "blob_storage.invalid_blob_id"
    INVALID_METADATA = "blob_storage.invalid_metadata"
    BLOB_NOT_FOUND = "blob_storage.blob_not_found"
    BLOB_ALREADY_EXISTS = "blob_storage.blob_already_exists"
    BLOB_TOO_LARGE = "blob_storage.blob_too_large"
    INVALID_CONTENT = "blob_storage.invalid_content"
    STORAGE_ERROR = "blob_storage.storage_error"
    METADATA_STORAGE_ERROR = "blob_storage.metadata_storage_error"
    VALIDATION_ERROR = "blob_storage.validation_error"
    PLUGIN_CONFIG_ERROR = "blob_storage.plugin_config_error"
    PLUGIN_MISSING_CONFIG = "blob_storage.plugin_missing_config"
    PLUGIN_INVALID_CONFIG_FORMAT = "blob_storage.plugin_invalid_config_format"
    EXTERNAL_ID_CONFLICT = "blob_storage.external_id_conflict"


# Patterns for detecting unique constraint violations across database backends
UNIQUE_CONSTRAINT_PATTERNS = (
    "unique constraint",
    "duplicate key",
    "UNIQUE constraint failed",
)


def is_unique_constraint_error(error_message: str) -> bool:
    """Check if error message indicates a unique constraint violation."""
    error_lower = error_message.lower()
    return any(pattern in error_lower for pattern in UNIQUE_CONSTRAINT_PATTERNS)


class BlobValidationError(Exception):
    def __init__(
        self,
        message: str,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code or BlobStorageErrorCode.VALIDATION_ERROR.value
        self.details = details or {}


def create_plugin_config_validation_error(
    plugin_name: str, invalid_keys: list[str]
) -> ActionResult:
    return cast(
        ActionResult,
        {
            "status": "error",
            "action_status": "error",
            "data": {},
            "actions": [],
            "error": {
                "type": "PluginConfigError",
                "code": BlobStorageErrorCode.PLUGIN_CONFIG_ERROR.value,
                "message": f"Plugin '{plugin_name}' configuration validation failed",
                "details": {
                    "plugin_name": plugin_name,
                    "invalid_keys": invalid_keys,
                },
                "severity": "error",
                "timestamp": datetime.now(UTC).isoformat(),
            },
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


def create_plugin_missing_config_error(plugin_name: str, missing_keys: list[str]) -> ActionResult:
    return cast(
        ActionResult,
        {
            "status": "error",
            "action_status": "error",
            "data": {},
            "actions": [],
            "error": {
                "type": "PluginConfigError",
                "code": BlobStorageErrorCode.PLUGIN_MISSING_CONFIG.value,
                "message": f"Plugin '{plugin_name}' missing required configuration",
                "details": {
                    "plugin_name": plugin_name,
                    "missing_keys": missing_keys,
                },
                "severity": "error",
                "timestamp": datetime.now(UTC).isoformat(),
            },
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


def create_plugin_invalid_config_format_error(
    plugin_name: str, config_key: str, expected_type: str
) -> ActionResult:
    return cast(
        ActionResult,
        {
            "status": "error",
            "action_status": "error",
            "data": {},
            "actions": [],
            "error": {
                "type": "PluginConfigError",
                "code": BlobStorageErrorCode.PLUGIN_INVALID_CONFIG_FORMAT.value,
                "message": f"Plugin '{plugin_name}' configuration format error",
                "details": {
                    "plugin_name": plugin_name,
                    "config_key": config_key,
                    "expected_type": expected_type,
                },
                "severity": "error",
                "timestamp": datetime.now(UTC).isoformat(),
            },
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


def create_error_response(
    error_code: str, message: str, details: dict[str, Any] | None = None
) -> ActionResult:
    return cast(
        ActionResult,
        {
            "status": "error",
            "action_status": "error",
            "data": {},
            "actions": [],
            "error": {
                "type": "BlobStorageError",
                "code": error_code,
                "message": message,
                "details": details or {},
                "severity": "error",
                "timestamp": datetime.now(UTC).isoformat(),
            },
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


def create_success_response(data: dict[str, Any] | None = None) -> ActionResult:
    return cast(
        ActionResult,
        {
            "status": "completed",
            "action_status": "completed",
            "data": data or {},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


def create_error_response_from_exception(
    e: Exception, action_name: str = "unknown"
) -> ActionResult:
    error_code = BlobStorageErrorCode.STORAGE_ERROR.value
    if isinstance(e, BlobValidationError):
        error_code = e.error_code

    return cast(
        ActionResult,
        {
            "status": "error",
            "action_status": "error",
            "data": {},
            "actions": [],
            "error": {
                "type": type(e).__name__,
                "code": error_code,
                "message": str(e),
                "details": {
                    "action": action_name,
                    "exception_type": type(e).__name__,
                },
                "severity": "error",
                "timestamp": datetime.now(UTC).isoformat(),
            },
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )
