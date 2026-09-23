"""Claude Code `~/.claude/tasks/<cli-session-uuid>/<task-id>.json` parser (spec §17.3 M10).

Layout: one directory per Claude Code CLI session, one JSON file per task
within that session. Per-file shape (empirically confirmed against
operator's filesystem 2026-06-11):

::

    {
      "id": "<task id, opaque string>",
      "subject": "<one-line task title>",
      "description": "<longer task description>",
      "activeForm": "<spinner-text imperative>",
      "status": "pending" | "in_progress" | "completed",
      "blocks": [<task-id>, ...],
      "blockedBy": [<task-id>, ...]
    }

Each task file emits ONE ``RawSessionEvent`` carrying a SYSTEM payload.
The source plugin's ``normalize`` lifts this into a SYSTEM
``NormalizedSessionEvent`` with
``content_json={"subtype": "task_state", ...task fields}`` per the M6
hybrid extractor / subtype-lift pattern documented in
``knowledge_bases/ananta_platform/19_session_ledger_01_system_event_subtype_lift.md``.

Timestamp source: per-file ``mtime``. The JSON content carries no
timestamp field, so the file's last-modified time is the only signal
available. `event_at` is UTC-anchored.

Non-task files in a session directory (``.highwatermark``, ``.lock``,
editor temp files) are filtered out at the source-plugin layer via
``*.json`` glob, NOT here — the parser is glob-blind so smokes can feed
arbitrary paths.

Failure policy (KB "Critical Development Guidelines v2"): malformed JSON or shape mismatch
raises ``ValueError``. Missing required fields raise ``ValueError`` —
the importer wraps in a per-session catch (importer.py:228) so one bad
task file doesn't kill the whole batch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Payload key sentinel — the source plugin's ``normalize`` reads this to
# emit the canonical SYSTEM NormalizedSessionEvent. Single-shape parser;
# each task file becomes one SYSTEM event with subtype=task_state.
PAYLOAD_KIND_TASK = "claude_code_task"
SUBTYPE_TASK_STATE = "task_state"


@dataclass(frozen=True, slots=True)
class _ParsedTask:
    """A parsed task JSON, ready to become a ``RawSessionEvent`` payload."""

    task_id: str
    subject: str
    description: str
    active_form: str
    status: str
    blocks: tuple[str, ...]
    blocked_by: tuple[str, ...]
    event_at: datetime


def _coerce_str_list(value: object, field_name: str, file_path: Path) -> tuple[str, ...]:
    """Coerce a list-of-strings field. Empty list is acceptable.

    Coerces non-string entries (rare, but ``blockedBy`` may contain
    integer task ids on some authoring paths) via ``str()`` so the
    SYSTEM event's JSONB stays homogeneously typed.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(
            f"claude_code tasks: {file_path}: field {field_name!r} must be a list, "
            f"got {type(value).__name__}",
        )
    return tuple(str(item) for item in value)


def _load_task_dict(path: Path) -> dict[str, object]:
    """Read + decode + json.loads + dict-shape validate one task file."""
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"claude_code tasks: cannot read {path}: {exc}") from exc
    try:
        decoded = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"claude_code tasks: non-UTF8 content in {path}: {exc}"
        ) from exc
    try:
        obj = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"claude_code tasks: malformed JSON in {path}: {exc}"
        ) from exc
    if not isinstance(obj, dict):
        raise ValueError(
            f"claude_code tasks: {path}: top-level value is not a dict "
            f"({type(obj).__name__})",
        )
    return obj


def _require_str(
    obj: dict[str, object], field: str, path: Path, *, non_empty: bool = False
) -> str:
    value = obj.get(field)
    if not isinstance(value, str) or (non_empty and not value):
        suffix = "non-empty " if non_empty else ""
        raise ValueError(
            f"claude_code tasks: {path}: missing/invalid {suffix}{field!r} string field",
        )
    return value


def parse_task_file(path: Path) -> _ParsedTask:
    """Parse one ``<task-id>.json`` file into a ``_ParsedTask``.

    Raises ``ValueError`` on malformed JSON, non-dict top-level, missing
    required fields, or wrong field types.
    """
    obj = _load_task_dict(path)
    task_id = _require_str(obj, "id", path, non_empty=True)
    subject = _require_str(obj, "subject", path)
    description = _require_str(obj, "description", path)
    active_form = _require_str(obj, "activeForm", path)
    status = _require_str(obj, "status", path)
    blocks = _coerce_str_list(obj.get("blocks"), "blocks", path)
    blocked_by = _coerce_str_list(obj.get("blockedBy"), "blockedBy", path)
    # No JSON-side timestamp. Use file mtime, UTC-anchored.
    event_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return _ParsedTask(
        task_id=task_id,
        subject=subject,
        description=description,
        active_form=active_form,
        status=status,
        blocks=blocks,
        blocked_by=blocked_by,
        event_at=event_at,
    )


def to_payload(parsed: _ParsedTask) -> dict[str, object]:
    """Build the ``RawSessionEvent.payload`` dict from a parsed task.

    Schema-stable: every field always present so the source plugin's
    ``normalize`` is straight-line; lists are emitted as Python lists
    (the importer's JSONB serializer round-trips list → JSON array).
    """
    return {
        "kind": PAYLOAD_KIND_TASK,
        "task_id": parsed.task_id,
        "subject": parsed.subject,
        "description": parsed.description,
        "activeForm": parsed.active_form,
        "status": parsed.status,
        "blocks": list(parsed.blocks),
        "blockedBy": list(parsed.blocked_by),
    }


__all__ = [
    "PAYLOAD_KIND_TASK",
    "SUBTYPE_TASK_STATE",
    "parse_task_file",
    "to_payload",
]
