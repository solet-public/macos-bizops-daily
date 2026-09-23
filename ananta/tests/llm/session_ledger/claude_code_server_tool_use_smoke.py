#!/usr/bin/env python3
"""Bug H (2026-06-13): claude_code vendor parser must recognize
``server_tool_use`` + ``<tool>_tool_result`` content blocks.

Pre-fix, encountering an ``advisor`` / ``web_search`` (or any other
server-side built-in tool) block raised
``ValueError("claude_code: unrecognized content block type ...")``,
which the importer's per-session catch absorbed — silently truncating
the affected session's ingest at the first such block. Fidelity gap,
not coverage gap; the affected sessions persisted FEWER events than
their source JSONLs contain.

Run:

    .venv/bin/python3 ananta/tests/llm/session_ledger/claude_code_server_tool_use_smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.vendor import claude_code as vendor  # noqa: E402

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


def _line_user_text() -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": "u-1",
            "sessionId": "s-bug-h",
            "timestamp": "2026-06-13T00:00:00.000Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                ],
            },
        },
    )


def _line_assistant_server_tool_use() -> str:
    # Exact shape observed in
# ~/.claude/projects/-Users-alice-Workspace-example/079c0eb5-*.jsonl
    return json.dumps(
        {
            "type": "assistant",
            "uuid": "u-2",
            "sessionId": "s-bug-h",
            "timestamp": "2026-06-13T00:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "server_tool_use",
                        "id": "srvtoolu_advisor_1",
                        "name": "advisor",
                        "input": {},
                    },
                ],
            },
        },
    )


def _line_user_named_tool_result() -> str:
    # Exact shape observed alongside the server_tool_use sample.
    return json.dumps(
        {
            "type": "user",
            "uuid": "u-3",
            "sessionId": "s-bug-h",
            "timestamp": "2026-06-13T00:00:02.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "advisor_tool_result",
                        "tool_use_id": "srvtoolu_advisor_1",
                        "content": {
                            "type": "advisor_result",
                            "text": "advisor verdict here",
                        },
                    },
                ],
            },
        },
    )


def _line_assistant_legacy_tool_use() -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": "u-4",
            "sessionId": "s-bug-h",
            "timestamp": "2026-06-13T00:00:03.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_legacy_1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/x"},
                    },
                ],
            },
        },
    )


def _line_user_legacy_tool_result() -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": "u-5",
            "sessionId": "s-bug-h",
            "timestamp": "2026-06-13T00:00:04.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_legacy_1",
                        "content": "legacy result",
                    },
                ],
            },
        },
    )


# ─── Tests ─────────────────────────────────────────────────────────────


def test_server_tool_use_block_yields_one_event() -> None:
    events = vendor.parse_line(_line_assistant_server_tool_use())
    _check(
        len(events) == 1,
        f"server_tool_use block yields exactly one event "
        f"(got {len(events)})",
    )
    if events:
        evt = events[0]
        _check(
            evt.payload.get("tool_use_id") == "srvtoolu_advisor_1",
            f"tool_use_id propagated (got "
            f"{evt.payload.get('tool_use_id')!r})",
        )
        _check(
            evt.payload.get("tool_name") == "advisor",
            f"tool_name propagated from block.name (got "
            f"{evt.payload.get('tool_name')!r})",
        )
        _check(
            evt.vendor_event_id == "srvtoolu_advisor_1",
            "vendor_event_id keyed by tool-use id (idempotency)",
        )


def test_named_tool_result_block_yields_one_event() -> None:
    events = vendor.parse_line(_line_user_named_tool_result())
    _check(
        len(events) == 1,
        f"advisor_tool_result block yields exactly one event "
        f"(got {len(events)})",
    )
    if events:
        evt = events[0]
        _check(
            evt.payload.get("tool_use_id") == "srvtoolu_advisor_1",
            "tool_use_id propagated from result block to event payload",
        )
        _check(
            evt.payload.get("text") == "advisor verdict here",
            f"dict-shaped content.text flattened into payload.text "
            f"(got {evt.payload.get('text')!r})",
        )


def test_legacy_tool_use_still_works() -> None:
    """Regression guard: existing tool_use blocks must NOT break."""
    events = vendor.parse_line(_line_assistant_legacy_tool_use())
    _check(
        len(events) == 1,
        f"legacy tool_use block still yields one event "
        f"(got {len(events)})",
    )
    if events:
        _check(
            events[0].payload.get("tool_name") == "Read",
            "legacy tool_name preserved",
        )


def test_legacy_tool_result_still_works() -> None:
    """Regression guard: existing tool_result blocks must NOT break."""
    events = vendor.parse_line(_line_user_legacy_tool_result())
    _check(
        len(events) == 1,
        f"legacy tool_result block still yields one event "
        f"(got {len(events)})",
    )
    if events:
        _check(
            events[0].payload.get("text") == "legacy result",
            "legacy str-shaped content preserved",
        )


def test_fixture_session_event_count_matches_jsonl_line_count() -> None:
    """Headline acceptance: count(events) == count(JSONL lines) for a
    fixture session that mixes legacy + server-side tool blocks.

    Pre-fix this would have been (5 lines parsed) but the 2nd + 3rd
    lines would have raised, the importer's per-session catch would have
    aborted the session walk, and ZERO events would have landed for the
    session. Post-fix all 5 lines yield exactly one event each."""
    fixture = [
        _line_user_text(),                     # 1 text event
        _line_assistant_server_tool_use(),     # 1 server_tool_use event
        _line_user_named_tool_result(),        # 1 advisor_tool_result event
        _line_assistant_legacy_tool_use(),     # 1 legacy tool_use event
        _line_user_legacy_tool_result(),       # 1 legacy tool_result event
    ]
    total = 0
    parse_errors = 0
    for line in fixture:
        try:
            evts = vendor.parse_line(line)
            total += len(evts)
        except ValueError:
            parse_errors += 1

    _check(
        parse_errors == 0,
        f"every fixture line parses without ValueError "
        f"(got {parse_errors} errors)",
    )
    _check(
        total == len(fixture),
        f"event count == JSONL line count "
        f"(got {total} events from {len(fixture)} lines)",
    )


# ─── Driver ─────────────────────────────────────────────────────────────


def main() -> int:
    print("ananta/tests/llm/session_ledger/claude_code_server_tool_use_smoke.py")
    test_server_tool_use_block_yields_one_event()
    test_named_tool_result_block_yields_one_event()
    test_legacy_tool_use_still_works()
    test_legacy_tool_result_still_works()
    test_fixture_session_event_count_matches_jsonl_line_count()
    print()
    print(f"passed: {_passed}")
    if _failed:
        print(f"failed: {len(_failed)}")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
