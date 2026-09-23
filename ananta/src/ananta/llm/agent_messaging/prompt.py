"""Deterministic prompt assembly for agent turns.

Bounded by ``MAX_TRANSCRIPT_MESSAGES`` (20) and
``MAX_TRANSCRIPT_CHARS`` (24,000).  Walk backward from the message
preceding the current request, collect until either bound is hit, then
restore chronological order before rendering.

The assembled prompt is persisted on the originator message's metadata
so it can be inspected from ``agent_messages`` output (workbench doc
§13).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .models import (
    AgentMessageRow,
    AgentThreadRow,
    MessageContent,
    MessageRole,
)

# Hard-coded bounds in the first slice; promote to config if an
# operator needs to tune them (workbench doc §13).
MAX_TRANSCRIPT_MESSAGES: int = 20
MAX_TRANSCRIPT_CHARS: int = 24_000


@dataclass(frozen=True, slots=True)
class AssembledPrompt:
    """Result of :func:`assemble_prompt`.

    The prompt text is what the backend sees.  The trimmed transcript
    is included so callers can persist a structured record on the
    originator message's metadata for later auditability.
    """

    prompt: str
    transcript: tuple[AgentMessageRow, ...]


_SYSTEM_HEADER = (
    "You are an agent participating in a solet-mediated thread."
)


def assemble_prompt(
    *,
    thread: AgentThreadRow,
    history: Sequence[AgentMessageRow],
    current_request: MessageContent,
) -> AssembledPrompt:
    """Render the prompt the backend will receive.

    ``history`` is the full ordered message list available to the
    caller; only the most-recent bounded slice ends up in the prompt.
    ``current_request`` is the originator message that triggered this
    turn — the runner has already persisted it but does NOT include it
    in the rendered transcript section (it lives in ``Current request:``
    instead, to keep the prompt unambiguous about which message the
    agent is responding to).
    """
    transcript = _bounded_history(history, exclude_id=None)
    sections: list[str] = [
        "System:",
        _SYSTEM_HEADER,
        f"Thread id: {thread.id}",
        f"Originator: {thread.originator_type.value}",
        f"Working directory: {thread.working_directory or '(unspecified)'}",
        "",
        "Recent messages:",
    ]
    if transcript:
        sections.extend(_render_transcript(transcript))
    else:
        sections.append("(no prior messages)")
    sections.extend(
        [
            "",
            "Current request:",
            _flatten_content(current_request),
        ],
    )
    return AssembledPrompt(
        prompt="\n".join(sections),
        transcript=tuple(transcript),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _bounded_history(
    history: Sequence[AgentMessageRow], *, exclude_id: str | None,
) -> list[AgentMessageRow]:
    """Walk backward through ``history`` collecting up to the bounds.

    Returns the slice in chronological order (oldest first).
    """
    chronological = sorted(history, key=lambda m: m.cursor)
    chosen: list[AgentMessageRow] = []
    char_total = 0
    for message in reversed(chronological):
        if exclude_id is not None and message.id == exclude_id:
            continue
        size = _message_char_size(message)
        if (
            len(chosen) + 1 > MAX_TRANSCRIPT_MESSAGES
            or char_total + size > MAX_TRANSCRIPT_CHARS
        ):
            break
        chosen.append(message)
        char_total += size
    chosen.reverse()
    return chosen


def _render_transcript(messages: Iterable[AgentMessageRow]) -> list[str]:
    out: list[str] = []
    for message in messages:
        prefix = _role_prefix(message.role)
        text = _flatten_content(message.content)
        if not text.strip():
            text = "(empty)"
        out.append(f"{prefix} {text}")
    return out


def _role_prefix(role: MessageRole) -> str:
    if role is MessageRole.ORIGINATOR:
        return "[originator]"
    if role is MessageRole.AGENT:
        return "[agent]"
    return "[system]"


def _flatten_content(content: MessageContent) -> str:
    return "\n".join(part.text for part in content)


def _message_char_size(message: AgentMessageRow) -> int:
    return sum(len(p.text) for p in message.content)


__all__ = [
    "MAX_TRANSCRIPT_CHARS",
    "MAX_TRANSCRIPT_MESSAGES",
    "AssembledPrompt",
    "assemble_prompt",
]
