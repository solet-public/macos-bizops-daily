"""Plugin interface contract registry and validation."""

from typing import Final

from ananta.core.domain.enums import ActionStatus, ErrorSeverity, ErrorType
from ananta.core.domain.error_codes import ErrorCode
from ananta.core.domain.types import (
    ActionDefinition,
    ActionParameter,
    ActionResult,
    ErrorDetail,
)
from ananta.core.plugins.plugin_utils import (
    extract_actions_from_data,
    extract_operational_settings,
    get_default_operational_config,
    merge_operational_configs,
    validate_error_code_format,
    validate_operational_config_format,
)
from ananta.interfaces.state_management_interface import StateManagementInterface

# Plugin interface contract registry
PLUGIN_CONTRACTS: Final[dict[str, type]] = {
    "state_management": StateManagementInterface,
}

# Plugin name patterns for automatic contract detection
PLUGIN_NAME_PATTERNS: Final[dict[str, str]] = {
    "_state_management_plugin": "state_management",
    "state_management_plugin": "state_management",
}


def get_plugin_type(plugin_name: str) -> str | None:
    """Determine plugin type from plugin name."""
    for pattern, plugin_type in PLUGIN_NAME_PATTERNS.items():
        if pattern in plugin_name:
            return plugin_type
    return None


def get_required_interface(plugin_name: str) -> type | None:
    """Get required interface for plugin based on its name."""
    plugin_type = get_plugin_type(plugin_name)
    return PLUGIN_CONTRACTS.get(plugin_type) if plugin_type else None


# Re-export all for backward compatibility
__all__ = [
    # Contract validation
    "PLUGIN_CONTRACTS",
    "PLUGIN_NAME_PATTERNS",
    "get_plugin_type",
    "get_required_interface",
    # Enums
    "ActionStatus",
    "ErrorType",
    "ErrorSeverity",
    # Error codes
    "ErrorCode",
    # Types
    "ActionParameter",
    "ActionDefinition",
    "ErrorDetail",
    "ActionResult",
    # Utility functions
    "extract_actions_from_data",
    "validate_error_code_format",
    "validate_operational_config_format",
    "extract_operational_settings",
    "merge_operational_configs",
    "get_default_operational_config",
]
