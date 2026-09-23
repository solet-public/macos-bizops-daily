from pathlib import Path
from typing import Any

from .constants import (
    BLOBS_DIRECTORY_NAME,
    DEFAULT_CLEANUP_ON_STARTUP,
    DEFAULT_MAX_FILE_SIZE,
)


def get_default_config() -> dict[str, Any]:
    return {
        "name": "default_blob_storage_plugin",
        "version": "0.1.0",
        "enabled": True,
        "log_level": "info",
        "timeout": 30,
        "retry_count": 3,
        "max_file_size": DEFAULT_MAX_FILE_SIZE,
        "cleanup_on_startup": DEFAULT_CLEANUP_ON_STARTUP,
        "enable_compression": False,
        "enable_deduplication": False,
    }


def validate_config(config: dict[str, Any]) -> None:
    if "max_file_size" in config and not isinstance(config["max_file_size"], int):
        raise ValueError("max_file_size must be an integer")

    if "max_file_size" in config and config["max_file_size"] <= 0:
        raise ValueError("max_file_size must be positive")

    if "cleanup_on_startup" in config and not isinstance(config["cleanup_on_startup"], bool):
        raise ValueError("cleanup_on_startup must be a boolean")


def get_blobs_directory(app_home: str) -> Path:
    return Path(app_home) / "data" / BLOBS_DIRECTORY_NAME


def get_plugin_data_directory(app_home: str) -> Path:
    return Path(app_home) / "data" / "plugin_data" / "default_blob_storage_plugin"
