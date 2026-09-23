#!/usr/bin/env python3
"""Unit smoke for v10 Control #5 drain-row serialization (no DB, no live solet).

Pins the server-side ``/peer/drain`` projection — in particular the IMPORTANT-
marker-strip PARITY: ``persist_role_message`` stores the ORIGINAL content (the
marker still embedded), while the live wake path delivers marker-stripped
``delivered_prose``. The repair drain MUST deliver byte-identical prose, so
``_serialize_role_drain_row`` joins the stored parts and strips the marker the
same way the live path does. A regression here would surface a stray
"IMPORTANT" prefix only on re-delivered (drained) messages.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_drain_serialization_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.http_routes import (  # noqa: E402
    _role_drain_prose,
    _serialize_role_drain_row,
)

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


def _parts(*texts: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": t} for t in texts]


def test_marker_stripped_to_match_live() -> None:
    # The live path matches IMPORTANT_MARKER_RE (^\s*IMPORTANT[:\s]\s*) and
    # slices from match.end(); the drain must produce the identical prose.
    _check(
        _role_drain_prose(_parts("IMPORTANT: ping the architect")) == "ping the architect",
        "marker 'IMPORTANT: ' stripped (colon form)",
    )
    _check(
        _role_drain_prose(_parts("IMPORTANT ping")) == "ping",
        "marker 'IMPORTANT ' stripped (whitespace form)",
    )


def test_no_marker_passthrough() -> None:
    _check(
        _role_drain_prose(_parts("just prose, no marker")) == "just prose, no marker",
        "no marker → prose unchanged",
    )


def test_multipart_join() -> None:
    _check(
        _role_drain_prose(_parts("IMPORTANT: line one", "line two")) == "line one\nline two",
        "multi-part content joined with newline, marker stripped from the head",
    )


def test_non_list_content_empty() -> None:
    _check(_role_drain_prose(None) == "", "non-list content → empty prose (no crash)")
    _check(_role_drain_prose("raw string") == "", "string content → empty prose (not a parts list)")


def test_serialize_full_shape() -> None:
    row = {
        "external_id": "role:Architect:arm-7",
        "recipient_key": "Architect",
        "message_id": "arm-7",
        "sender_agent_id": "claude_code",
        "sender_agent_instance_id": "agi-sender",
        "sender_session_label": "Coordinator",
        "thread_id": "role:Architect",
        "important": True,
        "content": _parts("IMPORTANT: the payload"),
    }
    out = _serialize_role_drain_row(row)
    _check(out["external_id"] == "role:Architect:arm-7", "serialize carries external_id")
    _check(out["recipient_key"] == "Architect", "serialize carries recipient_key")
    _check(out["content"] == "the payload", "serialize content is marker-stripped prose")
    _check(out["important"] is True, "serialize carries important flag")
    _check(
        out["sender_agent_instance_id"] == "agi-sender",
        "serialize carries sender provenance for the targeted-reply meta",
    )


def main() -> int:
    print("=== v10 Control #5 drain-row serialization + marker-strip parity smoke ===")
    test_marker_stripped_to_match_live()
    test_no_marker_passthrough()
    test_multipart_join()
    test_non_list_content_empty()
    test_serialize_full_shape()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
