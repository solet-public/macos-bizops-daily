#!/usr/bin/env python3
"""Vendor-drift-tolerance smoke for the claude_code session-source parser.

Anchored to the 2026-06-12 ingest-stall incidents:

- Sub-2.5: Claude Code added the timestamp-absent metadata line type ``mode``
  (key: ``mode``, sessionId-only, no ``timestamp``). The parser was too
  strict — aborted the whole session on first ``mode`` line.
- Task #14: Claude Code emits timestamp-present marker lines of type
  ``queue-operation`` (912 occurrences across today's session corpus).
  These have a ``timestamp`` but lack the event-shape ``message`` dict;
  the parser's old "unrecognized line 'type'" raise burned every session.

The structural fix unifies both surfaces: any line type not in the known
event set ({system, user, assistant}) is tolerated unless it carries the
event-shape signal (a ``message`` dict with a ``role`` field). Unknown
event-shape lines still RAISE per [[fast-fail-development-strategy]] —
those are genuine vendor contract changes worth catching loudly.

This smoke pins the invariants going forward:

1. The 2026-06-12 ``mode`` (timestamp-absent) line type yields zero events.
2. The 2026-06-12 ``queue-operation`` (timestamp-present, no event-shape)
   line type yields zero events.
3. An UNKNOWN timestamp-absent line type (forward-compat for the next
   metadata-only vendor change) is tolerated.
4. An UNKNOWN timestamp-PRESENT line type WITHOUT the event-shape signal
   is tolerated (forward-compat for the next marker-line vendor change,
   matches Task #14's queue-operation shape).
5. An UNKNOWN line type WITH the event-shape signal (``message`` dict
   with ``role``) still RAISES — genuine contract change worth catching.
6. Pre-existing canonical skip-list entries still skip cleanly.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/claude_code_vendor_drift_tolerance_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.vendor.claude_code import (  # noqa: E402
    _SKIP_LINE_TYPES,
    parse_line_data,
)


def _assert(cond: object, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_mode_line_yields_zero_events() -> None:
    """The 2026-06-12 ``mode`` line type must skip cleanly (no exception)."""
    _assert(
        "mode" in _SKIP_LINE_TYPES,
        "regression: 'mode' must be in _SKIP_LINE_TYPES; was the canonical "
        "entry removed?",
    )
    line = {
        "type": "mode",
        "mode": "default",
        "sessionId": "test-session-abc",
    }
    events = list(parse_line_data(line))
    _assert(
        events == [],
        f"expected zero events for 'mode' metadata line; got {events!r}",
    )


def test_queue_operation_line_yields_zero_events() -> None:
    """The 2026-06-12 Task #14 ``queue-operation`` line type must skip cleanly.

    HAS a ``timestamp`` field but no event-shape ``message`` dict; the
    structural tolerance + explicit allowlist both keep ingest alive.
    """
    _assert(
        "queue-operation" in _SKIP_LINE_TYPES,
        "regression: 'queue-operation' must be in _SKIP_LINE_TYPES; was the "
        "canonical entry removed?",
    )
    line = {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": "2026-06-12T16:00:00Z",
        "sessionId": "test-session-abc",
        "content": "some payload",
    }
    events = list(parse_line_data(line))
    _assert(
        events == [],
        f"expected zero events for 'queue-operation' marker line; got {events!r}",
    )


def test_unknown_timestamp_absent_line_is_tolerated() -> None:
    """Forward-compat: an UNKNOWN timestamp-absent line type must NOT raise.

    Plants a synthetic line type Claude Code might add in the future without
    a `timestamp` field. The parser should treat it as metadata + skip with
    a debug log, NOT raise — that keeps ingest alive across vendor drift.
    """
    line = {
        "type": "hypothetical-future-metadata-2027-01-01",
        "sessionId": "test-session-xyz",
        "futureField": "anything",
    }
    # MUST NOT raise.
    events = list(parse_line_data(line))
    _assert(
        events == [],
        "expected zero events for unknown timestamp-absent metadata line; "
        f"got {events!r}",
    )


def test_unknown_timestamp_present_no_event_shape_is_tolerated() -> None:
    """Forward-compat: an UNKNOWN timestamp-present line type WITHOUT event-shape tolerated.

    Mirrors the Task #14 ``queue-operation`` pattern: timestamp-present
    marker line that doesn't carry the event-shape ``message`` dict signal.
    The structural tolerance branch must NOT raise on this shape — it
    keeps ingest alive across future vendor marker-line additions.
    """
    line = {
        "type": "hypothetical-future-marker-2027-01-01",
        "sessionId": "test-session-xyz",
        "timestamp": "2027-01-01T00:00:00Z",
        "operation": "something-new",
        "content": "marker payload",
    }
    # MUST NOT raise.
    events = list(parse_line_data(line))
    _assert(
        events == [],
        "expected zero events for unknown timestamp-present marker line "
        f"(no event-shape); got {events!r}",
    )


def test_unknown_line_with_event_shape_still_raises() -> None:
    """Unknown EVENT-shaped lines must still raise (genuine contract change).

    Event-shape = ``message`` dict with ``role`` field. An unknown line type
    that DOES carry this signal is a new event KIND we don't understand —
    raise loudly per [[fast-fail-development-strategy]] rather than silently
    swallow real events.
    """
    line = {
        "type": "hypothetical-future-event-2027-01-01",
        "sessionId": "test-session-xyz",
        "timestamp": "2027-01-01T00:00:00Z",
        "uuid": "evt-uuid-1",
        "parentUuid": None,
        "isSidechain": False,
        # The event-shape signal that gates the fast-fail safety net.
        "message": {"role": "user", "content": "anything"},
    }
    raised = False
    try:
        list(parse_line_data(line))
    except ValueError as exc:
        raised = True
        msg = str(exc)
        _assert(
            "unrecognized event-shape line 'type'" in msg
            or "hypothetical-future-event-2027-01-01" in msg,
            f"expected ValueError citing unrecognized event-shape type; got msg: {msg!r}",
        )
    _assert(
        raised,
        "expected ValueError on unknown event-shape line type; "
        "parser silently accepted it (which would swallow real events)",
    )


def test_existing_metadata_types_still_skipped() -> None:
    """Regression: the pre-existing skip-list entries must still skip cleanly."""
    pre_existing = [
        "permission-mode",
        "file-history-snapshot",
        "last-prompt",
        "custom-title",
        "agent-name",
        "bridge-session",
        "ai-title",
        "attachment",
        "summary",
    ]
    for t in pre_existing:
        line = {"type": t, "sessionId": "test"}
        events = list(parse_line_data(line))
        _assert(
            events == [],
            f"regression: pre-existing skip-type {t!r} no longer skips cleanly; "
            f"got {events!r}",
        )


def main() -> int:
    tests = [
        test_mode_line_yields_zero_events,
        test_queue_operation_line_yields_zero_events,
        test_unknown_timestamp_absent_line_is_tolerated,
        test_unknown_timestamp_present_no_event_shape_is_tolerated,
        test_unknown_line_with_event_shape_still_raises,
        test_existing_metadata_types_still_skipped,
    ]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
