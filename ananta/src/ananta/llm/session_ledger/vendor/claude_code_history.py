"""Claude Code `~/.claude/history.jsonl` parser (spec §17.3 M7, architect v2 §6.1).

One file. One line per user prompt. No nested content blocks — every line
is one structural MESSAGE event with `role=USER`. The shape:

::

    {
      "display": "<the user's prompt as typed>",
      "pastedContents": <opaque>,
      "project": "<encoded cwd>",
      "sessionId": "<optional UUID>",
      "timestamp": <epoch milliseconds>
    }

External-session keying per architect v2 §6.1 (path (ii) hybrid):

* If ``sessionId`` is present and non-empty: ``external_session_id =
  sessionId``. ~93% of operator's 17,744-line history (empirical 2026-06-11).
* Else: ``external_session_id = f"history_orphan_{timestamp_ms}_{hash(project)}"``.
  ~7% are orphan-keyed; concentrated at the file head (pre-sessionId era).

NUL strip semantics: the repository's ``_strip_nuls`` helper handles this
at the TEXT-write seam — per the 2026-06-01 operator ruling captured in
``knowledge_bases/ananta_platform/19_session_ledger_02_nul_byte_sanitization_seam.md``
— so the parser leaves the raw ``display`` text alone.

Failure policy: per the KB "Critical Development Guidelines v2", malformed JSON or non-dict object
is a ``ValueError``. Lines missing ``timestamp`` or ``display`` cannot be
salvaged into a MESSAGE event and are skipped with a warning; raising
here would block ingest on a single bad line in a large append-only file.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from ananta.llm.session_ledger.types import RawSessionEvent

logger = logging.getLogger(__name__)

# Payload key sentinel — the source plugin's ``normalize`` reads this to
# emit the canonical NormalizedSessionEvent. Single-shape parser; no
# discriminator needed (every history.jsonl line is a USER MESSAGE).
PAYLOAD_KIND_MESSAGE = "history_message"


@dataclass(frozen=True, slots=True)
class _ParsedLine:
    """A parsed history.jsonl line, ready to become a ``RawSessionEvent``.

    ``byte_offset`` is the absolute byte position AFTER this line (its
    newline included) — the source plugin's cursor is the post-line offset
    of the last fully-drained line.
    """

    external_session_id: str
    display: str
    project: str | None
    event_at: datetime
    byte_offset: int


def _build_orphan_key(timestamp_ms: int, project: str | None) -> str:
    """Stable key for sessionId-less lines (path (ii) fallback)."""
    project_str = project or ""
    project_hash = hashlib.sha256(project_str.encode("utf-8")).hexdigest()[:16]
    return f"history_orphan_{timestamp_ms}_{project_hash}"


def _parse_one_line(
    decoded: str,
    line_end_offset: int,
) -> _ParsedLine | None:
    """Parse a single JSONL line; return None on a skip-worthy malformed row."""
    try:
        obj = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"claude_code history: malformed JSON at byte_offset={line_end_offset}: {exc}"
        ) from exc
    if not isinstance(obj, dict):
        raise ValueError(
            f"claude_code history: top-level value is not a dict at byte_offset="
            f"{line_end_offset}: {type(obj).__name__}",
        )
    display = obj.get("display")
    if not isinstance(display, str):
        logger.warning(
            "claude_code history: line at byte_offset=%d missing 'display' string; skip",
            line_end_offset,
        )
        return None
    timestamp = obj.get("timestamp")
    if not isinstance(timestamp, int | float):
        logger.warning(
            "claude_code history: line at byte_offset=%d missing numeric 'timestamp'; skip",
            line_end_offset,
        )
        return None
    timestamp_ms = int(timestamp)
    event_at = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    session_id = obj.get("sessionId")
    project = obj.get("project") if isinstance(obj.get("project"), str) else None
    if isinstance(session_id, str) and session_id:
        external_session_id = session_id
    else:
        external_session_id = _build_orphan_key(timestamp_ms, project)
    return _ParsedLine(
        external_session_id=external_session_id,
        display=display,
        project=project,
        event_at=event_at,
        byte_offset=line_end_offset,
    )


def parse_file_from_offset(
    file_handle: object,
    start_offset: int,
) -> Iterator[_ParsedLine]:
    """Yield ``_ParsedLine`` per fully-drained line from ``start_offset``.

    ``file_handle`` is an open BINARY file object; the caller has already
    ``seek``ed to ``start_offset``. Partial trailing line (no terminating
    newline) is left for the next poll pass and the iterator returns.
    """
    # The type-hint is intentionally ``object`` so the helper accepts both
    # real files and in-memory ``BytesIO`` from smokes without dragging in
    # the typing.IO protocol noise.
    offset = start_offset
    readline = file_handle.readline  # type: ignore[attr-defined]
    while True:
        line: bytes = readline()
        if not line:
            return
        if not line.endswith(b"\n"):
            return
        offset += len(line)
        try:
            decoded = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"claude_code history: non-UTF8 line at byte_offset={offset}: {exc}"
            ) from exc
        parsed = _parse_one_line(decoded, offset)
        if parsed is None:
            continue
        yield parsed


def to_raw_event(parsed: _ParsedLine) -> RawSessionEvent:
    """Convert a ``_ParsedLine`` into the importer's canonical RawSessionEvent.

    ``vendor_event_id`` is the post-line byte offset as a string — uniquely
    identifies this line within the file so the importer's
    ``find_event_id_by_vendor_id`` idempotency check works on re-poll.
    """
    return RawSessionEvent(
        external_session_id=parsed.external_session_id,
        payload={
            "kind": PAYLOAD_KIND_MESSAGE,
            "display": parsed.display,
            "project": parsed.project,
            "_byte_offset": parsed.byte_offset,
        },
        event_at=parsed.event_at,
        vendor_event_id=f"history_{parsed.byte_offset}",
        vendor_parent_event_id=None,
    )


__all__ = [
    "PAYLOAD_KIND_MESSAGE",
    "parse_file_from_offset",
    "to_raw_event",
]
