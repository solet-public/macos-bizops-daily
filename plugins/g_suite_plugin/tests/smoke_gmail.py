#!/usr/bin/env python3
"""Gmail action smoke tests for g_suite_plugin (no pytest, no live Google).

Exercises the pure gmail_actions functions against a faked Gmail service and a
faked attachment loader — no network, no credentials. Red-first: each check
asserts real parsing/MIME behavior, so a regression in gmail_actions fails here.

Exercises:
  1. list_messages — row shape + count
  2. list_messages — max clamp to the 100 cap
  3. get_message  — header extraction, plain-text body decode, attachment meta
  4. get_message  — missing id raises ValueError (gsuite.invalid_params path)
  5. send_message — text-only MIME round-trips through the base64url raw
  6. send_message — blob-backed attachment lands in the MIME

Run:
    .venv/bin/python3 plugins/g_suite_plugin/tests/smoke_gmail.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))

from g_suite_plugin.gmail_actions import (  # noqa: E402
    OutgoingAttachment,
    get_message,
    list_messages,
    send_message,
)

_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _fake_gmail(execute_value: dict[str, Any]) -> MagicMock:
    """A MagicMock whose messages().<verb>().execute() returns execute_value."""
    gmail = MagicMock()
    messages = gmail.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = execute_value
    messages.get.return_value.execute.return_value = execute_value
    messages.send.return_value.execute.return_value = {"id": "sent1", "threadId": "t9"}
    return gmail


def test_list_messages_shape() -> None:
    gmail = _fake_gmail({"messages": [{"id": "a", "threadId": "t1"}, {"id": "b", "threadId": "t2"}]})
    result = list_messages(gmail, {"query": "is:unread"})
    _assert("list count is 2", result["count"] == 2)
    _assert("list rows carry id+thread_id", result["messages"][0] == {"id": "a", "thread_id": "t1"})


def test_list_messages_default_is_500() -> None:
    """Business-data limits (2026-08-02): default raised to Gmail's own real
    single-call maximum (500, Reviewer-D's census) -- operator-kept explicitly."""
    gmail = _fake_gmail({"messages": []})
    list_messages(gmail, {})
    kwargs = gmail.users.return_value.messages.return_value.list.call_args.kwargs
    _assert("no max -> default 500", kwargs.get("maxResults") == 500)


def test_list_messages_clamp() -> None:
    """No override mechanism exists on this verb (§5.3 disclosure-only shape --
    the default already sits at Gmail's real per-call ceiling, nothing for an
    override to raise to without pageToken pagination, which this verb does not
    build): a caller-requested max above 500 clamps DOWN to 500, it does not
    fail loud and it cannot be raised by any parameter."""
    gmail = _fake_gmail({"messages": []})
    list_messages(gmail, {"max": 5000})
    kwargs = gmail.users.return_value.messages.return_value.list.call_args.kwargs
    _assert("max clamped to the 500 ceiling", kwargs.get("maxResults") == 500)


def test_list_messages_max_below_default_honored() -> None:
    gmail = _fake_gmail({"messages": []})
    list_messages(gmail, {"max": 10})
    kwargs = gmail.users.return_value.messages.return_value.list.call_args.kwargs
    _assert("a smaller explicit max is honored", kwargs.get("maxResults") == 10)


def test_get_message_parse() -> None:
    payload = {
        "headers": [
            {"name": "From", "value": "alice@example.com"},
            {"name": "Subject", "value": "Quarterly"},
            {"name": "X-Ignored", "value": "nope"},
        ],
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64url("hello body")}},
            {
                "mimeType": "application/pdf",
                "filename": "report.pdf",
                "body": {"attachmentId": "att1", "size": 1234},
            },
        ],
    }
    gmail = _fake_gmail({"id": "m1", "threadId": "t1", "snippet": "hello", "payload": payload})
    result = get_message(gmail, {"id": "m1"})
    _assert("body decoded", result["body_text"] == "hello body")
    _assert("from header extracted", result["headers"].get("from") == "alice@example.com")
    _assert("non-wanted header dropped", "x-ignored" not in result["headers"])
    _assert("attachment metadata present", result["attachments"][0]["name"] == "report.pdf")
    _assert("attachment id captured", result["attachments"][0]["attachment_id"] == "att1")


def test_get_message_requires_id() -> None:
    gmail = _fake_gmail({})
    raised = False
    try:
        get_message(gmail, {})
    except ValueError:
        raised = True
    _assert("missing id raises ValueError", raised)


def test_send_text_only() -> None:
    gmail = _fake_gmail({})
    result = send_message(
        gmail, {"to": "bob@example.com", "subject": "Hi", "body": "the body"}, _unused_loader
    )
    _assert("send returns id", result["id"] == "sent1")
    raw = gmail.users.return_value.messages.return_value.send.call_args.kwargs["body"]["raw"]
    decoded = base64.urlsafe_b64decode(raw)
    _assert("MIME has recipient", b"bob@example.com" in decoded)
    _assert("MIME has subject", b"Hi" in decoded)
    _assert("MIME has body", b"the body" in decoded)


def test_send_with_attachment() -> None:
    gmail = _fake_gmail({})

    def loader(blob_id: str) -> OutgoingAttachment:
        _assert("loader receives blob id", blob_id == "blob-42")
        return OutgoingAttachment(filename="data.csv", mime_type="text/csv", content=b"x,y\n1,2\n")

    send_message(gmail, {"to": "c@example.com", "attachments": ["blob-42"]}, loader)
    raw = gmail.users.return_value.messages.return_value.send.call_args.kwargs["body"]["raw"]
    decoded = base64.urlsafe_b64decode(raw)
    _assert("attachment filename in MIME", b"data.csv" in decoded)


def _unused_loader(blob_id: str) -> OutgoingAttachment:  # pragma: no cover - not called
    raise AssertionError("attachment loader should not be called for a text-only send")


def main() -> int:
    print("\ng_suite_plugin Gmail smoke tests")
    print("=" * 40)
    test_list_messages_shape()
    test_list_messages_default_is_500()
    test_list_messages_clamp()
    test_list_messages_max_below_default_honored()
    test_get_message_parse()
    test_get_message_requires_id()
    test_send_text_only()
    test_send_with_attachment()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All Gmail smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
