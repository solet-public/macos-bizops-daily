#!/usr/bin/env python3
"""vendor.codex_cloud parser smoke — Turn → RawSessionEvent transform table.

Covers the cases listed in design v3 §7.4:

* (a) ``current_user_turn.input_items[type='message']`` → role=USER MESSAGE.
* (b) ``current_assistant_turn.output_items[type='message']`` → role=
      ASSISTANT MESSAGE.
* (c) ``output_items[type='output_diff']`` → TOOL_RESULT subtype=output_diff.
* (d) ``output_items[type='pr']`` → TOOL_RESULT subtype=pr.
* (e) ``output_items[type='reasoning']`` → ASSISTANT MESSAGE subtype=reasoning.
* (f) ``worklog.messages[author=assistant]`` → ASSISTANT MESSAGE subtype=
      worklog; non-assistant authors skipped.
* (g) ``turn.error`` → SYSTEM event with subtype=error + code + message.
* (h) ``sibling_attempts[].attempts[].diff`` + ``messages`` → TOOL_RESULT +
      ASSISTANT MESSAGE entries with ``attempt_placement`` stashed in
      payload.
* (i) Unrecognized ``output_items[].type`` → ValueError.
* (j) Malformed envelope (missing external_session_id, missing task_details,
      bad JSON) → ValueError.
* (k) ``normalize_raw`` round-trips every emitted kind to the expected
      NormalizedSessionEvent shape.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    MessageRole,
    RawSessionEvent,
)
from ananta.llm.session_ledger.vendor import codex_cloud  # noqa: E402

_TASK_ID = "task_abcdef0123456789"
_TASK_EVENT_AT = "2026-06-14T22:33:44+00:00"

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


def _envelope(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "external_session_id": _TASK_ID,
        "task_event_at": _TASK_EVENT_AT,
        "task_details": {},
    }
    base.update(overrides)
    return base


def _serialize(env: dict[str, Any]) -> str:
    return json.dumps(env, ensure_ascii=False)


def _parse(env: dict[str, Any]) -> list[RawSessionEvent]:
    return list(codex_cloud.parse_chunk(_serialize(env)))


# ─── (a) user message ─────────────────────────────────────────────────────


def test_a_user_message_from_input_items() -> None:
    env = _envelope(
        task_details={
            "current_user_turn": {
                "id": "turn_u1",
                "input_items": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"content_type": "text", "text": "hello cloud codex"},
                        ],
                    },
                ],
            },
        },
    )
    events = _parse(env)
    _check(
        len(events) == 1,
        "(a) input_items[type=message] yields exactly one event",
    )
    event = events[0]
    _check(event.external_session_id == _TASK_ID, "(a) external_session_id stamped")
    _check(
        event.payload.get("text") == "hello cloud codex",
        "(a) text stamped on payload",
    )
    normalized = codex_cloud.normalize_raw(event)
    _check(
        normalized.role is MessageRole.USER,
        "(a) normalize_raw → role=USER",
    )
    _check(
        normalized.event_type is EventType.MESSAGE,
        "(a) normalize_raw → event_type=MESSAGE",
    )
    _check(
        normalized.content_text == "hello cloud codex",
        "(a) normalize_raw → content_text",
    )


# ─── (b) assistant message ────────────────────────────────────────────────


def test_b_assistant_message_from_output_items() -> None:
    env = _envelope(
        task_details={
            "current_assistant_turn": {
                "id": "turn_a1",
                "output_items": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"content_type": "text", "text": "ack"}],
                    },
                ],
            },
        },
    )
    events = _parse(env)
    _check(len(events) == 1, "(b) one event for one assistant message")
    normalized = codex_cloud.normalize_raw(events[0])
    _check(
        normalized.role is MessageRole.ASSISTANT,
        "(b) normalize_raw → role=ASSISTANT",
    )
    _check(
        normalized.event_type is EventType.MESSAGE,
        "(b) normalize_raw → event_type=MESSAGE",
    )


# ─── (c) output_diff → TOOL_RESULT ────────────────────────────────────────


def test_c_output_diff_emits_tool_result() -> None:
    diff_text = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    env = _envelope(
        task_details={
            "current_assistant_turn": {
                "id": "turn_a2",
                "output_items": [{"type": "output_diff", "diff": diff_text}],
            },
        },
    )
    events = _parse(env)
    _check(len(events) == 1, "(c) one event for one output_diff")
    normalized = codex_cloud.normalize_raw(events[0])
    _check(
        normalized.event_type is EventType.TOOL_RESULT,
        "(c) normalize_raw → event_type=TOOL_RESULT",
    )
    _check(
        normalized.content_text == diff_text,
        "(c) normalize_raw → diff content_text",
    )
    subtype = (normalized.content_json or {}).get("subtype")
    _check(subtype == "output_diff", "(c) subtype=output_diff stamped on content_json")


# ─── (d) pr → TOOL_RESULT ─────────────────────────────────────────────────


def test_d_pr_emits_tool_result() -> None:
    diff_text = "patch body"
    env = _envelope(
        task_details={
            "current_diff_task_turn": {
                "id": "turn_d1",
                "output_items": [
                    {"type": "pr", "output_diff": {"diff": diff_text}},
                ],
            },
        },
    )
    events = _parse(env)
    _check(len(events) == 1, "(d) one event for one pr item")
    normalized = codex_cloud.normalize_raw(events[0])
    _check(
        normalized.event_type is EventType.TOOL_RESULT,
        "(d) normalize_raw → event_type=TOOL_RESULT",
    )
    subtype = (normalized.content_json or {}).get("subtype")
    _check(subtype == "pr", "(d) subtype=pr stamped on content_json")


# ─── (e) reasoning → ASSISTANT MESSAGE ────────────────────────────────────


def test_e_reasoning_emits_assistant_message_with_subtype() -> None:
    env = _envelope(
        task_details={
            "current_assistant_turn": {
                "id": "turn_a3",
                "output_items": [
                    {
                        "type": "reasoning",
                        "content": [{"content_type": "text", "text": "thinking..."}],
                    },
                ],
            },
        },
    )
    events = _parse(env)
    _check(len(events) == 1, "(e) one event for one reasoning item")
    normalized = codex_cloud.normalize_raw(events[0])
    _check(
        normalized.role is MessageRole.ASSISTANT,
        "(e) normalize_raw → role=ASSISTANT",
    )
    _check(
        normalized.event_type is EventType.MESSAGE,
        "(e) normalize_raw → event_type=MESSAGE",
    )
    subtype = (normalized.content_json or {}).get("subtype")
    _check(subtype == "reasoning", "(e) subtype=reasoning stamped on content_json")


# ─── (f) worklog assistant messages ───────────────────────────────────────


def test_f_worklog_assistant_messages_emit_assistant_message() -> None:
    env = _envelope(
        task_details={
            "current_assistant_turn": {
                "id": "turn_w1",
                "worklog": {
                    "messages": [
                        {
                            "author": {"role": "assistant"},
                            "content": {
                                "parts": [
                                    {"content_type": "text", "text": "step 1 done"},
                                ],
                            },
                        },
                        {
                            "author": {"role": "user"},
                            "content": {
                                "parts": [
                                    {"content_type": "text", "text": "ignored"},
                                ],
                            },
                        },
                    ],
                },
            },
        },
    )
    events = _parse(env)
    _check(
        len(events) == 1,
        "(f) one event for one assistant worklog message (user skipped)",
    )
    normalized = codex_cloud.normalize_raw(events[0])
    _check(
        normalized.role is MessageRole.ASSISTANT,
        "(f) normalize_raw → role=ASSISTANT",
    )
    subtype = (normalized.content_json or {}).get("subtype")
    _check(subtype == "worklog", "(f) subtype=worklog stamped on content_json")


# ─── (g) turn.error → SYSTEM ──────────────────────────────────────────────


def test_g_turn_error_emits_system_event() -> None:
    env = _envelope(
        task_details={
            "current_assistant_turn": {
                "id": "turn_e1",
                "error": {"code": "rate_limit_exceeded", "message": "too fast"},
            },
        },
    )
    events = _parse(env)
    _check(len(events) == 1, "(g) one event for one turn error")
    normalized = codex_cloud.normalize_raw(events[0])
    _check(
        normalized.event_type is EventType.SYSTEM,
        "(g) normalize_raw → event_type=SYSTEM",
    )
    content = normalized.content_json or {}
    _check(content.get("subtype") == "error", "(g) subtype=error")
    _check(content.get("code") == "rate_limit_exceeded", "(g) code stamped")
    _check(content.get("message") == "too fast", "(g) message stamped")


# ─── (h) sibling_attempts → diff + messages with attempt_placement ────────


def test_h_sibling_attempts_with_attempt_placement() -> None:
    env = _envelope(
        task_details={"current_assistant_turn": {"id": "turn_h1"}},
        sibling_attempts=[
            {
                "turn_id": "turn_h1",
                "attempts": [
                    {
                        "turn_id": "turn_h1::alt1",
                        "attempt_placement": 1,
                        "diff": "alt-diff",
                        "messages": ["sibling text one", "sibling text two"],
                    },
                ],
            },
        ],
    )
    events = _parse(env)
    _check(
        len(events) == 3,
        "(h) sibling attempt yields 1 diff + 2 messages = 3 events",
    )
    placements = [
        evt.payload.get("_codex_cloud_attempt_placement") for evt in events
    ]
    _check(
        all(p == 1 for p in placements),
        "(h) attempt_placement=1 stamped on every sibling event",
    )


# ─── (i) Unrecognized output_item type → ValueError ───────────────────────


def test_i_unrecognized_output_item_type_raises() -> None:
    env = _envelope(
        task_details={
            "current_assistant_turn": {
                "id": "turn_i1",
                "output_items": [{"type": "bogus_phantom_type"}],
            },
        },
    )
    raised = False
    try:
        _parse(env)
    except ValueError:
        raised = True
    _check(raised, "(i) unrecognized output_item.type → ValueError")


# ─── (j) Malformed envelope → ValueError ──────────────────────────────────


def test_j_malformed_envelope_variants_raise() -> None:
    cases = [
        ("invalid JSON literal", "{not json"),
        ("not a JSON object", json.dumps([])),
        (
            "missing external_session_id",
            json.dumps({"task_event_at": _TASK_EVENT_AT, "task_details": {}}),
        ),
        (
            "missing task_event_at",
            json.dumps({"external_session_id": _TASK_ID, "task_details": {}}),
        ),
        (
            "missing task_details",
            json.dumps(
                {
                    "external_session_id": _TASK_ID,
                    "task_event_at": _TASK_EVENT_AT,
                },
            ),
        ),
        (
            "bad event_at format",
            json.dumps(
                {
                    "external_session_id": _TASK_ID,
                    "task_event_at": "not-a-date",
                    "task_details": {},
                },
            ),
        ),
    ]
    for label, chunk in cases:
        raised = False
        try:
            list(codex_cloud.parse_chunk(chunk))
        except ValueError:
            raised = True
        _check(raised, f"(j) malformed envelope ({label}) → ValueError")


# ─── (k) Event ordering + vendor_event_id uniqueness ──────────────────────


def test_k_event_ordering_and_vendor_event_id_uniqueness() -> None:
    env = _envelope(
        task_details={
            "current_user_turn": {
                "id": "tu",
                "input_items": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"content_type": "text", "text": "u"}],
                    },
                ],
            },
            "current_assistant_turn": {
                "id": "ta",
                "output_items": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"content_type": "text", "text": "a"}],
                    },
                    {"type": "output_diff", "diff": "d"},
                ],
            },
        },
    )
    events = _parse(env)
    _check(
        len(events) == 3,
        "(k) user message + assistant message + diff yields 3 events",
    )
    ids = [e.vendor_event_id for e in events]
    _check(len(set(ids)) == 3, "(k) vendor_event_id is unique across emitted events")


# ─── (l) Event-at parsing ─────────────────────────────────────────────────


def test_l_event_at_matches_envelope() -> None:
    env = _envelope(
        task_details={
            "current_user_turn": {
                "id": "tu",
                "input_items": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"content_type": "text", "text": "u"}],
                    },
                ],
            },
        },
    )
    events = _parse(env)
    expected = datetime(2026, 6, 14, 22, 33, 44, tzinfo=UTC)
    _check(
        events[0].event_at == expected,
        "(l) event_at matches envelope task_event_at",
    )


# ─── main ───────────────────────────────────────────────────────────────────


def main() -> int:
    print("=== codex_cloud_parse_smoke (12 cases a–l) ===")
    test_a_user_message_from_input_items()
    test_b_assistant_message_from_output_items()
    test_c_output_diff_emits_tool_result()
    test_d_pr_emits_tool_result()
    test_e_reasoning_emits_assistant_message_with_subtype()
    test_f_worklog_assistant_messages_emit_assistant_message()
    test_g_turn_error_emits_system_event()
    test_h_sibling_attempts_with_attempt_placement()
    test_i_unrecognized_output_item_type_raises()
    test_j_malformed_envelope_variants_raise()
    test_k_event_ordering_and_vendor_event_id_uniqueness()
    test_l_event_at_matches_envelope()
    print(
        f"\n{_passed} passed, {len(_failed)} failed"
        + ("" if not _failed else "\n  failures:\n    " + "\n    ".join(_failed)),
    )
    return 0 if not _failed else 1


if __name__ == "__main__":
    sys.exit(main())
