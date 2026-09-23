"""Canonical spool-line schema for the session dispatch bridge (W4).

Single source of truth for the backend-neutral spool-line contract (design §7):
the producer (``.claude/hooks/dispatch_bridge.py``) writes this shape and the
in-process drainer reads it. The producer cannot import this module — it runs
out of process, outside the venv — so it re-implements the same contract by
hand; the two are kept in sync by contract, not import. A future Codex producer
(design M4) writes the identical shape with ``agent="codex"`` and plugs into the
same drainer unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypedDict

SCHEMA_VERSION = 1

# Glob matching one spool file (one append-only JSON-lines file per hook fire).
SPOOL_GLOB = "*.jsonl"

# Provenance values — the drainer turns these into ``agent:<v>`` / ``source:<v>`` tags.
AGENT_CLAUDE_CODE = "claude_code"
SOURCE_HOOK_BRIDGE = "hook_bridge"

# Hook event kinds handled in M1 (design §4).
EVENT_TASK_CREATED = "TaskCreated"
EVENT_TASK_COMPLETED = "TaskCompleted"

# Spool-record field keys.
FIELD_EVENT = "event"
FIELD_SESSION_ID = "session_id"
FIELD_RECEIVED_AT = "received_at"
FIELD_AGENT = "agent"
FIELD_SOURCE = "source"
FIELD_PAYLOAD = "payload"

# Tag grammar (design §5) — the existing ``<namespace>:<...>`` colon convention.
_TAG_EVENT_PREFIX = "dispatch:event:"
_TAG_IN_FLIGHT_PREFIX = "dispatch:in_flight:"
_TAG_AGENT_PREFIX = "agent:"
_TAG_SOURCE_PREFIX = "source:"

# Ordered best-effort candidate keys for surfacing a task id / summary from the
# raw payload (OQ-2.1). The full payload is preserved verbatim in the audit
# memory regardless, so this extraction is convenience only — never load-bearing.
_TASK_ID_KEYS = ("task_id", "taskId", "id", "agent_id")
_SUMMARY_KEYS = (
    "description",
    "summary",
    "prompt",
    "subject",
    "result",
    "result_summary",
    "status",
)

UNKNOWN = "unknown"


def event_tag(session_id: str) -> str:
    """Append-only lifecycle audit tag for a session."""
    return f"{_TAG_EVENT_PREFIX}{session_id}"


def in_flight_tag(session_id: str) -> str:
    """Replaceable in-flight status tag for a session."""
    return f"{_TAG_IN_FLIGHT_PREFIX}{session_id}"


def agent_tag(agent: str) -> str:
    """Provenance tag identifying the producing agent backend."""
    return f"{_TAG_AGENT_PREFIX}{agent}"


def source_tag(source: str) -> str:
    """Provenance tag identifying the ingest source."""
    return f"{_TAG_SOURCE_PREFIX}{source}"


def _first_str(payload: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return UNKNOWN


@dataclass(frozen=True)
class SpoolRecord:
    """One parsed, well-formed dispatch spool line."""

    event: str
    session_id: str
    received_at: str
    agent: str
    source: str
    payload: dict[str, object]

    @property
    def task_id(self) -> str:
        """Best-effort task identifier from the raw payload (raw is preserved)."""
        return _first_str(self.payload, _TASK_ID_KEYS)

    @property
    def summary(self) -> str:
        """Best-effort one-line summary from the raw payload (raw is preserved)."""
        return _first_str(self.payload, _SUMMARY_KEYS)

    @property
    def payload_json(self) -> str:
        """The full raw payload, compacted — the ground-truth audit body."""
        return json.dumps(self.payload, ensure_ascii=False, sort_keys=True)


def parse_spool_line(line: str) -> SpoolRecord | None:
    """Parse one spool line; return None for a blank / torn / invalid line.

    A None return means "leave the file for retry" — e.g. a torn mid-append write
    that will be complete on the next drain tick. A returned record is well-formed
    and drainable.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed: object = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    event = parsed.get(FIELD_EVENT)
    session_id = parsed.get(FIELD_SESSION_ID)
    received_at = parsed.get(FIELD_RECEIVED_AT)
    if not (isinstance(event, str) and isinstance(session_id, str) and isinstance(received_at, str)):
        return None
    agent = parsed.get(FIELD_AGENT)
    source = parsed.get(FIELD_SOURCE)
    payload = parsed.get(FIELD_PAYLOAD)
    return SpoolRecord(
        event=event,
        session_id=session_id,
        received_at=received_at,
        agent=agent if isinstance(agent, str) else AGENT_CLAUDE_CODE,
        source=source if isinstance(source, str) else SOURCE_HOOK_BRIDGE,
        payload=payload if isinstance(payload, dict) else {},
    )


# ---------------------------------------------------------------------------
# Cursor + gap-marker schemas (M1.5: cursor + janitor machinery, design §3 D2.2)
# ---------------------------------------------------------------------------
#
# Centralised here so the schema-owning module is the single source of truth for
# every on-disk shape the plugin reads/writes (spool line, cursor, gap marker).
# ``cursor.py`` and ``janitor.py`` consume these; nothing outside the plugin
# touches them.

# Cursor-file extensions / suffixes.
CURSOR_SUFFIX = ".cursor"
GAP_MARKER_PREFIX = ".gap-marker."
GAP_MARKER_SUFFIX = ".json"

# Cursor field keys.
CURSOR_FIELD_VERSION = "version"
CURSOR_FIELD_DRAINER_ID = "drainer_id"
CURSOR_FIELD_POSITION = "position"
CURSOR_FIELD_HEARTBEAT = "heartbeat"
CURSOR_FIELD_RETIRED = "retired"

# Gap-marker field keys.
GAP_FIELD_ADVANCED_AT = "advanced_at"
GAP_FIELD_PRIOR_MIN_CURSOR = "prior_min_cursor"
GAP_FIELD_FILES_DROPPED = "files_dropped"
GAP_FIELD_DRAINERS_FORCE_ADVANCED = "drainers_force_advanced"


class CursorState(TypedDict):
    """One drainer's persisted read position (design §3 D2.2).

    ``version`` is a per-file monotonically-increasing write counter (NOT a schema
    version): a janitor reading concurrently with a drainer write sees a coherent
    older-or-newer generation, never a partial one (the atomic temp+rename in
    ``cursor.write_cursor`` is what actually guarantees no torn read; the counter
    is the generation marker on top of it). ``position`` is the *filename* of the
    last fully-drained spool file (empty string == nothing drained yet); the
    producer's ``{time.time_ns()}-{uuid}.jsonl`` names are fixed-width so
    lexicographic filename order == chronological order. ``heartbeat`` is refreshed
    on EVERY drain tick whether or not ``position`` advanced — so a stuck-but-alive
    drainer (writes failing, position frozen) still proves liveness and holds the
    janitor watermark, while a genuinely dead drainer goes stale and is retired.
    This cursor-internal heartbeat deliberately supersedes the design diagram's
    separate ``drainers/<id>.live`` files (resolved at M1.5a: one file, one write).
    ``retired`` is set true by the janitor when the heartbeat goes stale; a
    returning drainer clears it on its next tick.
    """

    version: int
    drainer_id: str
    position: str
    heartbeat: str
    retired: bool


class GapMarker(TypedDict):
    """Audit record written when the janitor force-advances at the retention
    ceiling (design §3 D2.2 — bounded disk wins, the gap is recorded never
    silently swallowed). Gap markers accumulate and are never auto-deleted.
    """

    advanced_at: str
    prior_min_cursor: str
    files_dropped: int
    drainers_force_advanced: list[str]
