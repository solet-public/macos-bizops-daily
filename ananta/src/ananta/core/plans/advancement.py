"""Plan advancement gateway — guards and policy for step advancement.

Determines whether the inference plugin should advance the plan at the
start of a VERTEX.  The actual advancement is performed by the thinking
plugin's ``advance_current_plan_step()``; this module owns only the
guard logic.

Focus is session-scoped (JOS-02): every read of the focused plan keys by
the ACTING session.  A caller with no session is treated as having no
focused plan — skip + log, never raise — because plan-less system lanes
(cron, wake-ups) are quiet by design, not defective (the V-5 ruling).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from ananta.core.plans.parser import parse
from ananta.core.prompts.context import ACTIVE_PLAN_MARKER

logger = logging.getLogger(__name__)

_AWAIT_USER_RE = re.compile(r"Await USER message", re.IGNORECASE)

# Process key suffixes that indicate the step owns its own plan transition.
PLAN_MUTATING_PROCESS_SUFFIXES = (
    "::graft_work_breakdown_structure_segment",
)


class FocusedMemoryProvider(Protocol):
    """Narrow protocol for reading the acting session's focused memories."""

    def get_focused(self, *, session_id: str) -> dict[str, Any]:
        """Return the session's focus envelope ``{"memories": [...], "count": N}``."""
        ...


class ThinkingServiceLike(Protocol):
    """Narrow protocol for the thinking service advancement call."""

    def advance_current_plan_step(
        self, *, session_id: str,
    ) -> dict[str, Any] | None: ...


def _focused_memories(
    memory_provider: FocusedMemoryProvider,
    session_id: str,
) -> list[dict[str, Any]]:
    """The acting session's focused memories; [] for a session-less caller.

    The empty-session branch is the V-5 ruling: treat-no-session-as-no-plan
    (skip + log at debug). Raising here would turn every plan-less system
    lane into error spam.
    """
    if not session_id:
        logger.debug(
            "PLAN_FOCUS: no acting session — treating as no focused plan",
        )
        return []
    return memory_provider.get_focused(session_id=session_id)["memories"]


def should_skip_advancement(
    *,
    action_name: str,
    is_continuation: bool,
    memory_provider: FocusedMemoryProvider | None,
    session_id: str,
) -> str | None:
    """Determine whether plan advancement should be skipped.

    Returns a reason string if advancement should be skipped, or
    ``None`` if advancement should proceed.
    """
    if action_name == "process_error":
        return "process_error vertex must not advance past an incomplete step"

    if memory_provider is not None and _current_step_has_plan_mutation(
        memory_provider, session_id,
    ):
        return (
            "current step dispatched a plan-mutating action; "
            "deferring to graft materialization"
        )

    if (
        is_continuation
        and memory_provider is not None
        and _is_current_step_await_user(memory_provider, session_id)
    ):
        return "await-user step must be completed by the user's next message"

    return None


def maybe_advance_plan(
    *,
    action_name: str,
    is_continuation: bool,
    memory_provider: FocusedMemoryProvider | None,
    thinking_service: ThinkingServiceLike | None,
    session_id: str,
) -> None:
    """Advance the acting session's plan at the start of VERTEXes, unless a guard fires."""
    skip_reason = should_skip_advancement(
        action_name=action_name,
        is_continuation=is_continuation,
        memory_provider=memory_provider,
        session_id=session_id,
    )
    if skip_reason:
        logger.info("PLAN_ADVANCE: Skipped — %s", skip_reason)
        return

    if not thinking_service or not session_id:
        return

    try:
        thinking_service.advance_current_plan_step(session_id=session_id)
    except Exception:
        logger.warning("PLAN_ADVANCE: Failed to advance plan", exc_info=True)


def has_focused_plan(
    memory_provider: FocusedMemoryProvider | None,
    *,
    session_id: str,
) -> bool:
    """Check whether the acting session currently has a focused plan."""
    if memory_provider is None:
        return False
    try:
        focused = _focused_memories(memory_provider, session_id)
        return any(
            ACTIVE_PLAN_MARKER in item.get("content", "")
            for item in focused
        )
    except Exception:
        return False


def _current_step_has_plan_mutation(
    memory_provider: FocusedMemoryProvider,
    session_id: str,
) -> bool:
    """Check if the session's plan's current step emits a plan-mutating action."""
    try:
        focused = _focused_memories(memory_provider, session_id)
        plan_text = ""
        for item in focused:
            content = item.get("content", "")
            if isinstance(content, str) and ACTIVE_PLAN_MARKER in content:
                plan_text = content
                break
        if not plan_text:
            return False
        parsed = parse(plan_text)
        current = parsed.current_step
        if current is None:
            return False
        return any(
            pk.endswith(suffix)
            for pk in current.process_keys
            for suffix in PLAN_MUTATING_PROCESS_SUFFIXES
        )
    except Exception:
        return False


def _is_current_step_await_user(
    memory_provider: FocusedMemoryProvider,
    session_id: str,
) -> bool:
    """Check if the session's current ``[>]`` step is an await-user step."""
    try:
        focused = _focused_memories(memory_provider, session_id)
        for item in focused:
            content = item.get("content", "")
            if not (isinstance(content, str) and ACTIVE_PLAN_MARKER in content):
                continue
            parsed = parse(content)
            current = parsed.current_step
            if current is None or current.process_keys:
                return False
            return bool(_AWAIT_USER_RE.search(current.full_text()))
    except Exception:
        pass
    return False
