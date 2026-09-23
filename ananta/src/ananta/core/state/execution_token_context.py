"""
Execution Token Context - Async-safe context propagation for FRG tokens.

This module uses Python's contextvars to propagate flow_token_id through the
call stack without requiring explicit parameter passing. This is critical for:

1. AsyncJobManager.create_job() - Automatically links jobs to tokens
2. ActionEventRecorder - Automatically sets parent_token_id for child actions

contextvars is:
- Async-safe: Each asyncio Task gets its own copy
- Thread-safe: Each thread gets its own copy
- Stack-aware: Supports nested contexts via token restore

NO PLUGIN CHANGES REQUIRED - The platform sets context before plugin execution,
and platform components read from context automatically.
"""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token

# Current flow_token_id for the executing action
# Set by ActionProcessor before calling plugin handler
# Read by AsyncJobManager.create_job() to link jobs to tokens
_current_flow_token_id: ContextVar[str | None] = ContextVar("current_flow_token_id", default=None)

# Stack of parent token IDs for nested result_processor chains
# When processing a result_processor, the current token becomes parent for children
_parent_token_stack: ContextVar[list[str]] = ContextVar("parent_token_stack")


def get_current_flow_token_id() -> str | None:
    """
    Get the flow_token_id for the currently executing action.

    Called by AsyncJobManager.create_job() to automatically link jobs to tokens.
    Returns None if called outside of action execution context.
    """
    return _current_flow_token_id.get()


def get_current_parent_token_id() -> str | None:
    """
    Get the parent token ID for new actions created in this context.

    Called by ActionEventRecorder when creating tokens for child actions
    spawned by result_processor templates.
    Returns None if not in a result_processor context.
    """
    try:
        stack = _parent_token_stack.get()
        return stack[-1] if stack else None
    except LookupError:
        # ContextVar not set in this context
        return None


@contextmanager
def action_execution_context(flow_token_id: str | None) -> Generator[None]:
    """
    Context manager for action execution scope.

    Sets flow_token_id for the duration of plugin handler execution.
    Any calls to create_job() within this scope will automatically
    capture the flow_token_id.

    Usage in ActionProcessor._execute_plugin_method():
        with action_execution_context(action.flow_token_id):
            result = plugin_handler(params, state, ...)
    """
    token: Token[str | None] = _current_flow_token_id.set(flow_token_id)
    try:
        yield
    finally:
        _current_flow_token_id.reset(token)


@contextmanager
def result_processor_context(parent_token_id: str | None) -> Generator[None]:
    """
    Context manager for result_processor template processing.

    Pushes current token onto parent stack. Any child actions created
    during template processing will have this as their parent_token_id.

    Supports recursive nesting: if result_processor A triggers result_processor B,
    the stack correctly tracks A as parent of B's children.

    Usage in ActionQueuePoller._execute_action_factory_template_processing():
        with result_processor_context(flow_token_id):
            for template in templates:
                action_factory.submit_result_with_template(...)
    """
    if parent_token_id is None:
        # No parent context - just yield
        yield
        return

    # Get current stack (or empty list if not set)
    try:
        current_stack = _parent_token_stack.get()
    except LookupError:
        current_stack = []

    # Create new stack with parent pushed
    new_stack = current_stack.copy()
    new_stack.append(parent_token_id)

    # Set the new stack
    token: Token[list[str]] = _parent_token_stack.set(new_stack)
    try:
        yield
    finally:
        _parent_token_stack.reset(token)


def clear_all_context() -> None:
    """
    Clear all token context. Used for testing and cleanup.

    WARNING: Only call this at flow boundaries or in tests.
    """
    _current_flow_token_id.set(None)
    _parent_token_stack.set([])
