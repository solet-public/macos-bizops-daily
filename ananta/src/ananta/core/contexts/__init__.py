"""Context types for action execution and template resolution.

This module provides typed, immutable context objects that replace
the ad-hoc dictionaries previously used throughout the system.
"""

from ananta.core.contexts.action_contexts import (
    ActionExecutionContext,
    GlobalConfig,
    TemplateFunctionContext,
    TemplateResolutionContext,
)
from ananta.core.contexts.normalization import (
    normalize_flow_id,
    normalize_session_id,
)

__all__ = [
    "ActionExecutionContext",
    "GlobalConfig",
    "TemplateFunctionContext",
    "TemplateResolutionContext",
    "normalize_flow_id",
    "normalize_session_id",
]
