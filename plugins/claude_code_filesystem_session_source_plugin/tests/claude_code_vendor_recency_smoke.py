#!/usr/bin/env python3
"""Regression smoke locking the 2026-05-31 claude_code vendor-drift fix.

Watchdog tag: ``dispatch:in_flight:claude_code_timestamp_vendor_fix``.

Symptom (pre-fix, from ``profile/data/logs/2026-05-31_profile.log``):

::

    WARNING - ledger poll: skipping session 471a05a5-d18c-4dc0-87c3-...
      (claude_code_local) on ValueError:
      claude_code: line missing non-empty string 'timestamp'

Three live sessions (``471a05a5...``, ``95072955...``, ``d6add7e5...``)
all had the same failure. Inspection of the real ``~/.claude/projects/``
jsonl files showed 4 session-metadata line types were NOT in
:data:`_SKIP_LINE_TYPES`:

* ``custom-title``   — operator-set custom title (``/rename`` slash command)
* ``agent-name``     — agent role label (e.g. ``"Claude-A"``)
* ``bridge-session`` — bridge-session id pinning
* ``ai-title``       — model-generated summary title (broader scan only)

None carry a ``timestamp`` field; they're session-config records, not
events. Pre-fix, parsing fell through ``_LINE_TYPE_USER`` / ``_LINE_TYPE_
ASSISTANT`` to :meth:`_LineContext.from_data` which called
:func:`_parse_timestamp` and raised ``ValueError`` on the missing field.
The per-session try/except in :class:`SessionLedgerImporter` swallowed
the error, the discovery cursor advanced regardless (``last_session_ref``
is set BEFORE the try block), and three same-day sessions silently
orphaned. Same vendor-drift bug class as the codex
``compacted`` / ``web_search_call`` fix that landed earlier today.

This smoke pins:

1. Every one of the 4 new skipped types parses cleanly when present in a
   real-shaped Claude-Code jsonl line.
2. A realistic ``~/.claude/projects/`` line sequence (mirroring the head
   of ``471a05a5-d18c-4dc0-87c3-e96a02b852ff.jsonl``) parses without
   raising; the conversational events that follow are yielded.
3. The pre-existing skipped types (``permission-mode``,
   ``file-history-snapshot``, ``last-prompt``, ``attachment``,
   ``summary``) keep their behavior — guarding against an accidental
   skip-list trim.

Run::

    .venv/bin/python3 plugins/claude_code_filesystem_session_source_plugin/tests/claude_code_vendor_recency_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.vendor import claude_code  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# ───── Fixture line builders (real Claude Code shapes) ──────────────────────


_SESSION_ID = "471a05a5-d18c-4dc0-87c3-e96a02b852ff"


_NEWLY_SKIPPED_LINES: dict[str, dict[str, object]] = {
    "custom-title": {
        "type": "custom-title",
        "customTitle": "Claude-A",
        "sessionId": _SESSION_ID,
    },
    "agent-name": {
        "type": "agent-name",
        "agentName": "Claude-A",
        "sessionId": _SESSION_ID,
    },
    "bridge-session": {
        "type": "bridge-session",
        "sessionId": _SESSION_ID,
        "bridgeSessionId": "cse_0178ZLiM8pUT8jEVyStUqFFm",
        "lastSequenceNum": 0,
    },
    "ai-title": {
        "type": "ai-title",
        "aiTitle": "Vendor recency fix planning",
        "sessionId": _SESSION_ID,
    },
}


_PREEXISTING_SKIPPED_LINES: dict[str, dict[str, object]] = {
    "permission-mode": {
        "type": "permission-mode",
        "permissionMode": "bypassPermissions",
        "sessionId": _SESSION_ID,
    },
    "file-history-snapshot": {
        "type": "file-history-snapshot",
        "messageId": "msg_demo",
        "snapshot": [],
        "isSnapshotUpdate": False,
    },
    "last-prompt": {
        "type": "last-prompt",
        "leafUuid": "5a84b6b2-8b8f-407b-8fb1-b8ba874db567",
        "sessionId": _SESSION_ID,
    },
    "attachment": {
        "type": "attachment",
        "attachmentId": "att-demo",
    },
    "summary": {
        "type": "summary",
        "summary": "Old session summary",
    },
}


def _user_message_line(uuid: str, parent_uuid: str | None = None) -> dict[str, object]:
    line: dict[str, object] = {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-05-31T16:00:00.000Z",
        "cwd": "/Users/alice/Workspace/example",
        "gitBranch": "master",
        "sessionId": _SESSION_ID,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "hello after metadata"}],
        },
    }
    if parent_uuid is not None:
        line["parentUuid"] = parent_uuid
    return line


# ───── (1) Each newly-skipped type parses to ZERO events, no ValueError ─────


def test_each_newly_skipped_type_yields_no_events() -> None:
    for type_name, data in _NEWLY_SKIPPED_LINES.items():
        raised: ValueError | None = None
        events: list[object] = []
        try:
            events = list(claude_code.parse_line_data(dict(data)))
        except ValueError as exc:
            raised = exc
        _check(
            raised is None and events == [],
            f"line type {type_name!r} parses to 0 events without raising "
            f"(raised={raised!r}, events={len(events)})",
        )


# ───── (2) Pre-existing skipped types keep their behavior ───────────────────


def test_preexisting_skipped_types_unchanged() -> None:
    for type_name, data in _PREEXISTING_SKIPPED_LINES.items():
        raised: ValueError | None = None
        events: list[object] = []
        try:
            events = list(claude_code.parse_line_data(dict(data)))
        except ValueError as exc:
            raised = exc
        _check(
            raised is None and events == [],
            f"pre-existing skipped type {type_name!r} still yields 0 events "
            f"without raising (raised={raised!r}, events={len(events)})",
        )


# ───── (3) Realistic 471a05a5 head sequence parses cleanly ──────────────────


def test_realistic_471a05a5_head_sequence_parses_cleanly() -> None:
    """Mirrors the first 9 lines of
    ``~/.claude/projects/-Users-alice-Workspace-example/471a05a5-d18c-4dc0-87c3-e96a02b852ff.jsonl``:

    last-prompt, custom-title, agent-name, permission-mode, bridge-session,
    user message, assistant message, last-prompt, file-history-snapshot.

    Pre-fix, the SECOND line (``custom-title``) raised because it wasn't
    in ``_SKIP_LINE_TYPES`` and the parser fell through to
    ``_parse_timestamp``. The whole session was orphaned; the importer's
    per-session try/except silently swallowed and advanced the cursor.

    Post-fix: every line parses; the 2 conversational lines yield events.
    """
    fixture_lines: list[dict[str, object]] = [
        dict(_PREEXISTING_SKIPPED_LINES["last-prompt"]),
        dict(_NEWLY_SKIPPED_LINES["custom-title"]),
        dict(_NEWLY_SKIPPED_LINES["agent-name"]),
        dict(_PREEXISTING_SKIPPED_LINES["permission-mode"]),
        dict(_NEWLY_SKIPPED_LINES["bridge-session"]),
        _user_message_line("evt-aaaa"),
        {
            "type": "assistant",
            "uuid": "evt-bbbb",
            "parentUuid": "evt-aaaa",
            "timestamp": "2026-05-31T16:00:05.000Z",
            "cwd": "/Users/alice/Workspace/example",
            "gitBranch": "master",
            "sessionId": _SESSION_ID,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hi back."}],
            },
        },
        dict(_PREEXISTING_SKIPPED_LINES["last-prompt"]),
        dict(_PREEXISTING_SKIPPED_LINES["file-history-snapshot"]),
    ]
    yielded_events: list[object] = []
    raised: ValueError | None = None
    try:
        for line_data in fixture_lines:
            yielded_events.extend(claude_code.parse_line_data(line_data))
    except ValueError as exc:
        raised = exc
    _check(
        raised is None,
        f"realistic 471a05a5 head sequence does not raise "
        f"(raised={raised!r})",
    )
    _check(
        len(yielded_events) == 2,
        f"exactly 2 conversational events yielded "
        f"(user + assistant message); got {len(yielded_events)}",
    )


# ───── (4) Existing-skip set still rejects unknown types ────────────────────


def test_unknown_type_still_raises() -> None:
    """Vendor stays strict on GENUINELY-NEW EVENT-SHAPE lines, while tolerating
    non-event-shape metadata drift. `parse_line_data` (see its 2026 comment
    block) scopes the fast-fail to unknown ``type``s that DO carry the
    event-shape signal (a ``message`` dict with a ``role``) — those raise so a
    real new event kind is caught loudly; an unknown ``type`` WITHOUT that
    signal is deliberately skip-with-debug-logged to keep ingest alive across
    vendor drift. This asserts BOTH halves so the contract can't silently
    collapse either way.

    Both fixtures carry a ``timestamp`` so the parser gets PAST
    :func:`_parse_timestamp` and reaches the unknown-``type`` branch.
    """
    # (a) event-shape unknown -> RAISES (the retained fast-fail safety net).
    event_shape_unknown: dict[str, object] = {
        "type": "future-unknown-shape-2027",
        "sessionId": _SESSION_ID,
        "timestamp": "2027-01-15T00:00:00.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "x"}]},
    }
    raised: ValueError | None = None
    try:
        list(claude_code.parse_line_data(event_shape_unknown))
    except ValueError as exc:
        raised = exc
    _check(
        raised is not None and "unrecognized event-shape line 'type'" in str(raised),
        f"unknown EVENT-SHAPE type raises the vendor-contract-change ValueError "
        f"(raised={raised!r})",
    )
    # (b) non-event-shape unknown -> TOLERATED (0 events, no raise) for drift-resilience.
    tol_events: list[object] = []
    tol_raised: ValueError | None = None
    try:
        tol_events = list(claude_code.parse_line_data({
            "type": "future-unknown-shape-2027",
            "sessionId": _SESSION_ID,
            "timestamp": "2027-01-15T00:00:00.000Z",
        }))
    except ValueError as exc:
        tol_raised = exc
    _check(
        tol_raised is None and tol_events == [],
        f"unknown NON-event-shape type is tolerated (0 events, no raise) "
        f"(raised={tol_raised!r}, events={len(tol_events)})",
    )


# ───── Driver ───────────────────────────────────────────────────────────────


def main() -> int:
    print("=== claude_code_vendor_recency_smoke ===")
    test_each_newly_skipped_type_yields_no_events()
    test_preexisting_skipped_types_unchanged()
    test_realistic_471a05a5_head_sequence_parses_cleanly()
    test_unknown_type_still_raises()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
