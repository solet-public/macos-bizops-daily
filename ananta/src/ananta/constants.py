import os
from enum import Enum, IntEnum
from typing import Final


class ProviderType(Enum):
    PLUGIN = "plugin"
    SERVICE_INTERFACE = "service_interface"


class ExitCodes(IntEnum):
    SUCCESS = 0
    KEYBOARD_INTERRUPT = 130
    CONNECTION_ERROR = 2
    PERMISSION_ERROR = 3
    FILE_NOT_FOUND = 4
    OS_ERROR = 5
    TIMEOUT_ERROR = 6
    FRAMEWORK_ERROR = 10
    PLUGIN_ERROR = 11
    EXTERNAL_ERROR = 12
    UNKNOWN_ERROR = 1


DEFAULT_MAX_CONSECUTIVE_ERRORS: Final[int] = 3
DEFAULT_MAX_ACTIONS_PER_CYCLE: Final[int] = 50

PLUGIN_CLI_PATTERN: Final[str] = r"^--plugin\.([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)$"
ENV_PREFIX: Final[str] = "ANANTA_"
ENV_VAR_FORMAT: Final[str] = "PLUGIN_{0}_{1}"

# Solet Identity
SOLET_NAME_ENV_VAR: Final[str] = "SOLET_NAME"

DATA_DIRECTORY_NAME: Final[str] = "data"
PROMPTS_DIRECTORY_NAME: Final[str] = "prompts"
STATE_FILENAME: Final[str] = "state.json"
CONFIG_DIRECTORY_NAME: Final[str] = "config"
PLUGINS_CONFIG_DIRECTORY_NAME: Final[str] = "plugins"
LOGS_DIRECTORY_NAME: Final[str] = "logs"
PLUGIN_LOGS_DIRECTORY_NAME: Final[str] = "plugin_logs"
PLUGIN_DATA_DIRECTORY_NAME: Final[str] = "plugin_data"
DEBUG_LOGS_DIRECTORY_NAME: Final[str] = "logs"

# Application Structure Constants
APP_DIRECTORY_NAME: Final[str] = "app"

PLUGIN_DISCOVERY_TIMEOUT: Final[int] = 300

STATE_VERSION: Final[int] = 1

# Legacy constants for backward compatibility - prefer ExitCodes enum
ERROR_EXIT_CODE_FRAMEWORK: Final[int] = ExitCodes.FRAMEWORK_ERROR
ERROR_EXIT_CODE_PLUGIN: Final[int] = ExitCodes.PLUGIN_ERROR
ERROR_EXIT_CODE_EXTERNAL: Final[int] = ExitCodes.EXTERNAL_ERROR

CLI_ERROR_MISSING_PARAMETER_VALUE: Final[str] = "cli.missing_parameter_value"
CLI_ERROR_UNKNOWN_PARAMETER: Final[str] = "cli.unknown_parameter"

DEFAULT_LOG_MAX_SIZE: Final[int] = 500 * 1024 * 1024  # 500 MB
DEFAULT_LOG_BACKUP_COUNT: Final[int] = 5
DEFAULT_LOG_RETENTION_DAYS: Final[int] = 7
DEFAULT_LOG_LEVEL: Final[str] = "info"
DEFAULT_LOG_FORMAT: Final[str] = (
    "%(asctime)s - [%(name)s:%(filename)s:%(lineno)d] - %(levelname)s - %(message)s"
)
FRAMEWORK_NAMESPACE: Final[str] = "core"
FRAMEWORK_SCHEMA_TABLE: Final[str] = "schema_registry"
FRAMEWORK_ACTION_EXECUTIONS_TABLE: Final[str] = "action_executions"
FRAMEWORK_PROCESS_REGISTRY_TABLE: Final[str] = "process_registry"
FRAMEWORK_JOB_TABLE: Final[str] = "job"
FRAMEWORK_JOB_PAYLOAD_TABLE: Final[str] = "job_payload"
FRAMEWORK_ASYNC_JOBS_TABLE: Final[str] = "job"  # Now points to new unified job ledger

NOTES_MAX_LENGTH: Final[int] = 512

# ID Prefix Constants - authoritative source for generated IDs
# These match the id_prefix values in core_schemas.py table definitions
# Format: {prefix}-{timestamp_base36}{random_base36}
ID_PREFIX_FLOW: Final[str] = "flow"
ID_PREFIX_SESSION: Final[str] = "sess"

# Known invalid ID values that should be normalized to None
# These are strings that sometimes appear due to serialization bugs or placeholder leakage
INVALID_ID_VALUES: Final[frozenset[str]] = frozenset(
    {"", "None", "null", "undefined", "FLOW_ID", "SESSION_ID"}
)

# Runtime Context Variable Names (for template resolution)
# CANONICAL FORM: All internal lookups use lowercase. Template placeholders (SESSION_ID)
# are normalized to lowercase at parsing boundaries via normalize_context_key().
CONTEXT_KEY_SESSION_ID: Final[str] = "session_id"
CONTEXT_KEY_FLOW_ID: Final[str] = "flow_id"
CONTEXT_KEY_TIMESTAMP: Final[str] = "timestamp"
CONTEXT_KEY_DATE: Final[str] = "date"
CONTEXT_KEY_TIME: Final[str] = "time"
CONTEXT_KEY_TIMEZONE: Final[str] = "timezone"
CONTEXT_KEY_TIMEZONE_OFFSET: Final[str] = "timezone_offset"

# Additional context keys for action execution
CONTEXT_KEY_ACTION_ID: Final[str] = "action_id"
CONTEXT_KEY_PROCESS_KEY: Final[str] = "process_key"
CONTEXT_KEY_APP_HOME: Final[str] = "APP_HOME"
CONTEXT_KEY_RUNTIME_ARGS: Final[str] = "runtime_args"
CONTEXT_KEY_STATE: Final[str] = "state"
CONTEXT_KEY_ACTION: Final[str] = "action"
CONTEXT_KEY_GLOBAL_VARS: Final[str] = "global_vars"
CONTEXT_KEY_USER_STATE: Final[str] = "user_state"
CONTEXT_KEY_ENVIRONMENT: Final[str] = "environment"

# Template variable names (for result/post-execution templates)
TEMPLATE_VAR_RESULT: Final[str] = "RESULT"
TEMPLATE_VAR_ACTION_STATUS: Final[str] = "ACTION_STATUS"
TEMPLATE_VAR_USER_INPUT: Final[str] = "USER_INPUT"
TEMPLATE_VAR_SESSION_ID: Final[str] = "SESSION_ID"
TEMPLATE_VAR_FLOW_ID: Final[str] = "FLOW_ID"
TEMPLATE_VAR_NOTES: Final[str] = "NOTES"
TEMPLATE_VAR_ERROR: Final[str] = "ERROR"
TEMPLATE_VAR_ERROR_MESSAGE: Final[str] = "ERROR_MESSAGE"
TEMPLATE_VAR_ERROR_DETAILS: Final[str] = "ERROR_DETAILS"
TEMPLATE_VAR_ACTION_ID: Final[str] = "ACTION_ID"
TEMPLATE_VAR_PROCESS_KEY: Final[str] = "PROCESS_KEY"
TEMPLATE_VAR_FAILED_ACTION: Final[str] = "FAILED_ACTION"
TEMPLATE_VAR_FAILED_PROCESS_KEY: Final[str] = "FAILED_PROCESS_KEY"
TEMPLATE_VAR_CANONICAL_SCHEMA: Final[str] = "CANONICAL_SCHEMA"
TEMPLATE_VAR_AVAILABLE_ATTACHMENTS: Final[str] = "AVAILABLE_ATTACHMENTS"
TEMPLATE_VAR_ACTION_ARGUMENTS: Final[str] = "ACTION_ARGUMENTS"


def normalize_context_key(key: str) -> str:
    """Normalize a context key to canonical lowercase form.

    Templates may use SESSION_ID, session_id, or Session_Id - all normalize to 'session_id'.
    This eliminates case-sensitivity bugs in template variable resolution.

    Args:
        key: The key to normalize (e.g., "SESSION_ID", "session_id", "Session_Id")

    Returns:
        Lowercase canonical form (e.g., "session_id")
    """
    return key.lower()


DEFAULT_LOG_OUTPUTS: Final[list[str]] = ["console", "file"]

VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"debug", "info", "warning", "error", "critical"}
)

VALID_LOG_OUTPUTS: Final[frozenset[str]] = frozenset({"console", "file"})

PLUGIN_NAME_PATTERN: Final[str] = r"^[a-z][a-z0-9_]*[a-z0-9]$|^[a-z]$"
VERSION_PATTERN: Final[str] = r"^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$"

# Service Plugin Configuration
# NOTE: This default is used for bootstrap mode only.
# In plugin mode, the launch script detects the active plugin and sets ANANTA_STATE_PLUGIN env var.
# StateService will fail fast if plugin_manager exists but no plugin name is provided.
DEFAULT_STATE_MANAGEMENT_PLUGIN: Final[str] = "default_state_management_plugin"
POSTGRES_STATE_MANAGEMENT_PLUGIN: Final[str] = "postgres_state_management_plugin"
# All state management plugins - used for startup sequence filtering
STATE_MANAGEMENT_PLUGINS: Final[frozenset[str]] = frozenset(
    {
        DEFAULT_STATE_MANAGEMENT_PLUGIN,
        POSTGRES_STATE_MANAGEMENT_PLUGIN,
    }
)
DEFAULT_BLOB_STORAGE_PLUGIN: Final[str] = "default_blob_storage_plugin"
DEFAULT_ADDRESS_BOOK_PLUGIN: Final[str] = "default_address_book_plugin"
DEFAULT_INFERENCE_PLUGIN: Final[str] = "default_inference_plugin"
DEFAULT_EMBEDDING_PLUGIN: Final[str] = "local_embeddings_plugin"
DEFAULT_VECTOR_PLUGIN: Final[str] = "pgvector_service_plugin"
DEFAULT_MEMORY_PLUGIN: Final[str] = "actr_memory_plugin"
DEFAULT_KNOWLEDGE_PLUGIN: Final[str] = "default_knowledge_plugin"
DEFAULT_THINKING_PLUGIN: Final[str] = "default_thinking_plugin"
DEFAULT_SCHEDULING_PLUGIN: Final[str] = "default_scheduling_plugin"
SERVICE_PLUGIN_PRIORITY: Final[int] = 0


# Path Builder Functions - USE THESE INSTEAD OF os.getcwd()
def get_app_path(app_home: str) -> str:
    """Get the path to the app directory."""
    return os.path.join(app_home, APP_DIRECTORY_NAME)


def get_data_path(app_home: str) -> str:
    """Get the path to the data directory."""
    return os.path.join(app_home, DATA_DIRECTORY_NAME)


def get_config_path(app_home: str) -> str:
    """Get the path to the config directory."""
    return os.path.join(app_home, CONFIG_DIRECTORY_NAME)


def get_prompts_path(app_home: str) -> str:
    """Get the path to the config/prompts directory."""
    return os.path.join(app_home, CONFIG_DIRECTORY_NAME, PROMPTS_DIRECTORY_NAME)
