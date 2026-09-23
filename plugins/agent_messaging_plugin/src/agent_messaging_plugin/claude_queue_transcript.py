"""Authoritative Claude Code queue-operation transcript reader."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

QUEUE_OPERATION_TYPE = "queue-operation"
QUEUE_OPERATIONS = frozenset({"enqueue", "dequeue", "remove"})


def transcript_confirms_input(path: Path, *, session_id: str, text: str) -> bool:
    """Return true only when a matching Claude queue operation proves input.

    Pane text is deliberately not consulted: visible composer text may be
    stranded terminal output rather than a queued Claude Code input.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != QUEUE_OPERATION_TYPE:
            continue
        if entry.get("operation") not in QUEUE_OPERATIONS:
            continue
        if entry.get("sessionId") == session_id and entry.get("content") == text:
            return True
    return False
