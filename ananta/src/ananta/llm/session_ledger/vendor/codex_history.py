"""Codex `~/.codex/history.jsonl` parser (spec §17.3 M8, architect v2 §6.1).

One file. One line per user prompt. The shape:

::

    {
      "session_id": "<UUID>",
      "ts": <epoch seconds>,
      "text": "<the prompt as typed>"
    }

Probed empirically 2026-06-11 PT against operator's history.jsonl:

* 3,130 lines total.
* ``session_id`` is present on 100% of lines (path (i) clean per P1 §A.2.3).
* ``ts`` is **epoch SECONDS** (NOT milliseconds) — verified by sample
  lines whose ts → fromtimestamp(ts) yields 2025-2026 dates while
  fromtimestamp(ts/1000) yields 1970. The brief §2.3 ``event_at=line.ts/1000``
  was wrong; the parser uses ``ts`` directly. Flagged in completion report.

External-session keying per architect v2 §6.1 path (i): ``external_session_id
= session_id`` always; no orphan fallback needed.

NUL strip semantics: the repository's ``_strip_nuls`` helper handles this at
the TEXT-write seam (KB ``19_session_ledger_02_nul_byte_sanitization_seam.md``),
so the parser leaves raw ``text`` alone.

Failure policy: per the KB "Critical Development Guidelines v2", malformed JSON or non-dict object
is a ``ValueError``. Lines missing ``text`` or ``ts`` are skipped with a
warning; raising blocks ingest on a single bad line in a 3K-row file.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from ananta.llm.session_ledger.types import RawSessionEvent

logger = logging.getLogger(__name__)

PAYLOAD_KIND_MESSAGE = "history_message"


@dataclass(frozen=True, slots=True)
class _ParsedLine:
    """A parsed history.jsonl line, ready to become a ``RawSessionEvent``.

    ``byte_offset`` is the absolute byte position AFTER this line (its
    newline included) — the source plugin's cursor is the post-line
    offset of the last fully-drained line.
    """

    external_session_id: str
    text: str
    event_at: datetime
    byte_offset: int


def _parse_one_line(
    decoded: str,
    line_end_offset: int,
) -> _ParsedLine | None:
    """Parse one JSONL line; return None on a skip-worthy malformed row."""
    try:
        obj = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"codex history: malformed JSON at byte_offset={line_end_offset}: {exc}",
        ) from exc
    if not isinstance(obj, dict):
        raise ValueError(
            f"codex history: top-level value is not a dict at byte_offset="
            f"{line_end_offset}: {type(obj).__name__}",
        )
    session_id = obj.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        logger.warning(
            "codex history: line at byte_offset=%d missing 'session_id' string; skip",
            line_end_offset,
        )
        return None
    text = obj.get("text")
    if not isinstance(text, str):
        logger.warning(
            "codex history: line at byte_offset=%d missing 'text' string; skip",
            line_end_offset,
        )
        return None
    ts = obj.get("ts")
    if not isinstance(ts, int | float):
        logger.warning(
            "codex history: line at byte_offset=%d missing numeric 'ts'; skip",
            line_end_offset,
        )
        return None
    event_at = datetime.fromtimestamp(int(ts), tz=UTC)
    return _ParsedLine(
        external_session_id=session_id,
        text=text,
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
                f"codex history: non-UTF8 line at byte_offset={offset}: {exc}",
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
            "text": parsed.text,
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
