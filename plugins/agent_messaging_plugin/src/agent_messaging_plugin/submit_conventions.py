"""Declarative, harness-owned input and submission conventions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class InsertMode(StrEnum):
    LITERAL_KEYSTROKES = "literal_keystrokes"
    BRACKETED_PASTE = "bracketed_paste"
    JSON_MESSAGE = "json_message"


class SubmitForm(StrEnum):
    KEY_NAME = "key_name"
    CONTROL_CHARACTER = "control_character"
    MESSAGE_BOUNDARY = "message_boundary"


@dataclass(frozen=True, slots=True)
class SubmitConvention:
    """The one declared insert/submit convention for a harness."""

    insert_mode: InsertMode
    submit_form: SubmitForm
    submit_value: str | None
    settle_required: bool
    confirm_method: str


TMUX_CLAUDE_SUBMIT_CONVENTION = SubmitConvention(
    InsertMode.BRACKETED_PASTE, SubmitForm.KEY_NAME, "Enter", True, "capture-pane diff",
)
"""Bracketed paste, not literal keystrokes, since 2026-09-07.

The declared mode used to be ``LITERAL_KEYSTROKES`` and the channel used
``send-keys -l`` to match it. That mechanism was measured silently dropping
the HEAD of a large payload against a real Claude Code pane, so the channel
now loads the text into a private tmux buffer and pastes it with
``paste-buffer -p``. This declaration follows the mechanism rather than
leading it — see ``tmux_adapter._TmuxSendKeysDriverChannel._insert`` for the
measurement that forced both."""
TMUX_CODEX_SUBMIT_CONVENTION = SubmitConvention(
    InsertMode.LITERAL_KEYSTROKES, SubmitForm.KEY_NAME, "Enter", True, "pane diff, bounded retry",
)
ITERM2_SEAT_SUBMIT_CONVENTION = SubmitConvention(
    InsertMode.BRACKETED_PASTE, SubmitForm.CONTROL_CHARACTER, "\r", True, "pane diff",
)
HEADLESS_STREAM_JSON_SUBMIT_CONVENTION = SubmitConvention(
    InsertMode.JSON_MESSAGE, SubmitForm.MESSAGE_BOUNDARY, None, False, "stream ack",
)
