"""Typed context objects for action execution and template resolution.

These replace the ad-hoc dictionaries that mixed action context,
global config, and template variables into a single "junk drawer" dict.

Design principles:
1. Immutable (frozen dataclasses) - context should not change during execution
2. Single responsibility - each context type has a clear purpose
3. Typed - enables IDE support and static analysis
4. No environment reads - config is injected, not read from os.environ
5. Fail fast - required fields are validated at construction time
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ananta.constants import CONTEXT_KEY_ACTION_ID, CONTEXT_KEY_PROCESS_KEY
from ananta.error_handling import FrameworkError

if TYPE_CHECKING:
    from ananta.core.actions.action_processor import QueuedActionProtocol

logger = logging.getLogger(__name__)


class GlobalConfig(Protocol):
    """Protocol for global configuration access.

    Implementations must provide APP_HOME through proper DI,
    not via os.environ reads.
    """

    @property
    def APP_HOME(self) -> str:  # noqa: N802
        """Application home directory."""
        ...


@dataclass(frozen=True)
class ActionExecutionContext:
    """Context for action execution - immutable and typed.

    Contains only action-specific data. Does NOT include global config
    like APP_HOME - that belongs in GlobalConfig.

    flow_id is required - all actions must have a flow context.
    """

    action_id: str
    process_key: str
    session_id: str | None
    flow_id: str  # Required - no flowless actions
    parameters: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate required fields. Fail fast."""
        if not self.flow_id:
            raise FrameworkError(
                message="ActionExecutionContext requires flow_id",
                error_code="context.flow_id_required",
                details={
                    CONTEXT_KEY_ACTION_ID: self.action_id,
                    CONTEXT_KEY_PROCESS_KEY: self.process_key,
                },
            )

    @classmethod
    def from_action(cls, action: QueuedActionProtocol) -> ActionExecutionContext:
        """Create context from a queued action.

        Args:
            action: The queued action to extract context from.

        Returns:
            An immutable ActionExecutionContext with the action's identity.

        Raises:
            FrameworkError: If flow_id is missing or invalid.
        """
        params: dict[str, object] = {}
        if action.parameters:
            try:
                parsed = json.loads(action.parameters)
                if isinstance(parsed, dict):
                    params = parsed
            except json.JSONDecodeError:
                logger.error(f"Failed to parse parameters for action {action.id}")

        from ananta.core.contexts.normalization import normalize_flow_id, normalize_session_id

        flow_id = normalize_flow_id(action.flow_id)
        if not flow_id:
            raise FrameworkError(
                message="Cannot create ActionExecutionContext: action missing flow_id",
                error_code="context.flow_id_required",
                details={
                    CONTEXT_KEY_ACTION_ID: action.id,
                    CONTEXT_KEY_PROCESS_KEY: action.process_key,
                },
            )

        return cls(
            action_id=action.id,
            process_key=action.process_key,
            session_id=normalize_session_id(action.session_id),
            flow_id=flow_id,
            parameters=params,
        )


@dataclass(frozen=True)
class TemplateResolutionContext:
    """Context for template variable resolution.

    Provides explicit, typed sources for variable resolution.
    No ad-hoc dict usage - all sources are named and typed.
    """

    runtime_args: dict[str, object] = field(default_factory=dict)
    state: dict[str, object] = field(default_factory=dict)
    global_vars: dict[str, object] = field(default_factory=dict)
    user_state: dict[str, object] = field(default_factory=dict)
    environment: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TemplateFunctionContext:
    """Context for template function execution.

    This is the context passed to TemplateFunctionRegistry.execute_function().
    It combines action identity with resolved variables and injected config.

    flow_id is required - template functions that need flow context will fail
    fast if it's missing instead of producing invalid output.

    action_id is optional for pre-persistence contexts (before action is stored).
    context_id is optional - only set when platform-managed context is active.

    Note: app_home is included here because template functions may need to
    resolve file paths. It is passed via injection, never read from os.environ.
    """

    action_id: str | None  # Optional for pre-persistence contexts
    process_key: str
    session_id: str | None
    flow_id: str  # Required - no flowless template execution
    app_home: str
    context_id: str | None = None  # Optional for platform-managed context
    local_variables: dict[str, object] = field(default_factory=dict)
    global_variables: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate required fields. Fail fast."""
        if not self.flow_id:
            raise FrameworkError(
                message="TemplateFunctionContext requires flow_id",
                error_code="context.flow_id_required",
                details={
                    CONTEXT_KEY_PROCESS_KEY: self.process_key,
                    CONTEXT_KEY_ACTION_ID: self.action_id,
                },
            )

    @classmethod
    def from_action_and_config(
        cls,
        action: QueuedActionProtocol,
        app_home: str,
        local_variables: dict[str, object] | None = None,
        global_variables: dict[str, object] | None = None,
        context_id: str | None = None,
    ) -> TemplateFunctionContext:
        """Create context from action and injected config.

        Args:
            action: The queued action being executed.
            app_home: Application home directory (injected, not from os.environ).
            local_variables: Per-request template variables.
            global_variables: Shared template variables.
            context_id: Platform-managed context ID (optional).

        Returns:
            An immutable TemplateFunctionContext.

        Raises:
            FrameworkError: If flow_id is missing or invalid.
        """
        from ananta.core.contexts.normalization import normalize_flow_id, normalize_session_id

        flow_id = normalize_flow_id(action.flow_id)
        if not flow_id:
            raise FrameworkError(
                message="Cannot create TemplateFunctionContext: action missing flow_id",
                error_code="context.flow_id_required",
                details={
                    CONTEXT_KEY_ACTION_ID: action.id,
                    CONTEXT_KEY_PROCESS_KEY: action.process_key,
                },
            )

        return cls(
            action_id=action.id,
            process_key=action.process_key,
            session_id=normalize_session_id(action.session_id),
            flow_id=flow_id,
            app_home=app_home,
            context_id=context_id,
            local_variables=local_variables or {},
            global_variables=global_variables or {},
        )


@dataclass(frozen=True)
class TemplateExecutionContext:
    """Bundle of contexts for complete template resolution and function execution.

    This is the single typed context passed through the template resolution pipeline.
    It combines function context (action identity, flow, session) with resolution
    context (variable sources) without any dict conversion.
    """

    function_context: TemplateFunctionContext
    resolution_context: TemplateResolutionContext
