"""Utility modules for the Ananta platform.

This package provides shared utilities across all components:
- filesystem: Directory creation, JSON file handling, timestamps
- naming: Name normalization, external_id generation, validation
"""

from ananta.utils.dry_run import coerce_dry_run
from ananta.utils.filesystem import (
    JSONData,
    JSONValue,
    create_app_directories,
    create_directory,
    create_log_filename,
    create_plugin_directories,
    format_error_code,
    generate_timestamp,
    get_action_status_from_error,
    get_env_variable,
    is_valid_error_code_format,
    load_json_file,
    save_json_file,
    truncate_message,
)
from ananta.utils.naming import (
    NamingError,
    NamingErrorCode,
    NormalizedName,
    build_external_id,
    build_filename,
    normalize_name,
    normalize_with_extension,
    parse_filename,
    validate_display_name,
)

__all__ = [
    # Filesystem utilities
    "JSONData",
    "JSONValue",
    "create_app_directories",
    "create_directory",
    "create_log_filename",
    "create_plugin_directories",
    "format_error_code",
    "generate_timestamp",
    "get_action_status_from_error",
    "get_env_variable",
    "is_valid_error_code_format",
    "load_json_file",
    "save_json_file",
    "truncate_message",
    "coerce_dry_run",
    # Naming utilities
    "NamingError",
    "NamingErrorCode",
    "NormalizedName",
    "build_external_id",
    "build_filename",
    "normalize_name",
    "normalize_with_extension",
    "parse_filename",
    "validate_display_name",
]
