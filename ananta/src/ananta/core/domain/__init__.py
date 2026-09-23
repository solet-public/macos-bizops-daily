"""
Domain Types & Contracts - Foundation types for AnantaAI framework.

This package provides the core domain types, protocols, enums, and error codes
used throughout the AnantaAI framework. These define the ubiquitous language
of the system.

Public API organized by module:
- types: TypedDict definitions for actions, definitions, results, and state
- protocols: Protocol interfaces for plugin system
- enums: Status and error classification enumerations
- error_codes: Framework-wide error code definitions
- status: Status normalization and matching utilities
"""

# Types
# Enums
from ananta.core.domain.enums import ActionStatus, ErrorSeverity, ErrorType, SessionStatus

# Error codes
from ananta.core.domain.error_codes import ErrorCode

# Protocols — imported from ananta.core.domain.protocols directly by consumers.
# NOT re-exported here to avoid circular import (protocols → config → error_handling → domain).
# Status utilities
from ananta.core.domain.status import (
    AsyncJobStatus,
    create_error_response,
    create_success_response,
    is_status_match,
    normalize_status,
)
from ananta.core.domain.types import (
    ActionDefinition,
    ActionDefinitionFile,
    ActionObject,
    ActionParameter,
    ActionProcess,
    ActionResult,
    ErrorDetail,
    ModelConfig,
    PluginConfig,
    PromptConfig,
    StateRoot,
)

__all__ = [
    # Types
    "ActionProcess",
    "ActionDefinitionFile",
    "ActionObject",
    "StateRoot",
    "PluginConfig",
    "ModelConfig",
    "PromptConfig",
    "ActionParameter",
    "ActionDefinition",
    "ErrorDetail",
    "ActionResult",
    # Enums
    "ActionStatus",
    "ErrorSeverity",
    "ErrorType",
    "SessionStatus",
    # Error codes
    "ErrorCode",
    # Status functions
    "AsyncJobStatus",
    "normalize_status",
    "is_status_match",
    "create_success_response",
    "create_error_response",
]
