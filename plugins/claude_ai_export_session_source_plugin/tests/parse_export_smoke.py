"""M9 §5 — claude_ai_export vendor parse smoke.

Verifies:
- 3 fixture conversations parse into 3 ExternalSessionRef + N events.
- sender → role mapping (human → USER, assistant → ASSISTANT).
- parent_message_uuid handling: ROOT_SENTINEL → None; non-sentinel UUID → str.
- Empirically against fixture derived from operator's actual export shape:
  at least ONE message has parent_message_uuid == ROOT_SENTINEL AND the
  parser maps it to vendor_parent_event_id=None.
- summary_text_seed lifts from conv.summary when non-empty; None otherwise.
"""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.types import MessageRole  # noqa: E402
from ananta.llm.session_ledger.vendor import claude_ai_export as v  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _build_fixture_zip(td: Path) -> Path:
    """Build a 3-conversation fixture ZIP mirroring operator's actual schema."""
    convs = [
        {
            "uuid": "conv-001",
            "name": "First conversation",
            "summary": "Short summary",
            "account": {"uuid": "acct-x"},
            "created_at": "2025-11-01T00:00:00Z",
            "updated_at": "2025-11-01T00:05:00Z",
            "chat_messages": [
                {
                    "uuid": "msg-001-A",
                    "sender": "human",
                    "text": "short",
                    "content": "User question",
                    "created_at": "2025-11-01T00:00:00Z",
                    "updated_at": "2025-11-01T00:00:00Z",
                    "attachments": [],
                    "files": [],
                    "parent_message_uuid": v.ROOT_SENTINEL,
                },
                {
                    "uuid": "msg-001-B",
                    "sender": "assistant",
                    "text": "",
                    "content": [
                        {"type": "text", "text": "Assistant reply"},
                    ],
                    "created_at": "2025-11-01T00:01:00Z",
                    "updated_at": "2025-11-01T00:01:00Z",
                    "attachments": [],
                    "files": [],
                    "parent_message_uuid": "msg-001-A",
                },
            ],
        },
        {
            "uuid": "conv-002",
            "name": "Second",
            "summary": "",
            "account": {"uuid": "acct-x"},
            "created_at": "2025-12-01T00:00:00Z",
            "updated_at": "2025-12-01T00:01:00Z",
            "chat_messages": [
                {
                    "uuid": "msg-002-A",
                    "sender": "human",
                    "text": "x",
                    "content": "Only message",
                    "created_at": "2025-12-01T00:00:00Z",
                    "updated_at": "2025-12-01T00:00:00Z",
                    "attachments": [],
                    "files": [],
                    "parent_message_uuid": v.ROOT_SENTINEL,
                },
            ],
        },
        {
            "uuid": "conv-003",
            "name": "Third",
            "summary": "Summary 3",
            "account": {"uuid": "acct-x"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "chat_messages": [
                {
                    "uuid": "msg-003-A",
                    "sender": "human",
                    "text": "hi",
                    "content": "Q3",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "attachments": [],
                    "files": [],
                    "parent_message_uuid": v.ROOT_SENTINEL,
                },
            ],
        },
    ]
    zp = td / "fixture.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr(v.CONVERSATIONS_ENTRY, json.dumps(convs))
    return zp


def test_parse_three_conversations() -> None:
    with tempfile.TemporaryDirectory() as td:
        zp = _build_fixture_zip(Path(td))
        payloads = list(v.parse_export_zip(zp))
        _assert(len(payloads) == 3, f"expected 3 conversations; got {len(payloads)}")
        ids = [p.session_ref.external_session_id for p in payloads]
        _assert(ids == ["conv-001", "conv-002", "conv-003"], f"ids: {ids}")
        # summary_text_seed: lifted when non-empty, None when empty
        _assert(payloads[0].summary_seed == "Short summary", str(payloads[0].summary_seed))
        _assert(payloads[1].summary_seed is None, str(payloads[1].summary_seed))
        _assert(payloads[2].summary_seed == "Summary 3", str(payloads[2].summary_seed))
        # session_ref.vendor_session_label = conv.name
        _assert(
            payloads[0].session_ref.vendor_session_label == "First conversation",
            payloads[0].session_ref.vendor_session_label or "",
        )
        # first_seen_at = parsed created_at, UTC-aware
        _assert(
            payloads[0].session_ref.first_seen_at == datetime(2025, 11, 1, tzinfo=UTC),
            f"first_seen_at: {payloads[0].session_ref.first_seen_at}",
        )
        # conv-001 has 2 messages
        _assert(len(payloads[0].messages) == 2, f"conv-001 msg count: {len(payloads[0].messages)}")


def test_root_sentinel_maps_to_none() -> None:
    with tempfile.TemporaryDirectory() as td:
        zp = _build_fixture_zip(Path(td))
        payloads = list(v.parse_export_zip(zp))
        # First message of conv-001 has parent_message_uuid = ROOT_SENTINEL
        root_msg = payloads[0].messages[0]
        _assert(
            root_msg.parent_uuid is None,
            f"ROOT_SENTINEL must map to None; got {root_msg.parent_uuid!r}",
        )
        # Second message has parent_message_uuid = "msg-001-A"
        child_msg = payloads[0].messages[1]
        _assert(
            child_msg.parent_uuid == "msg-001-A",
            f"non-sentinel parent must be preserved; got {child_msg.parent_uuid!r}",
        )


def test_sender_to_role_mapping() -> None:
    with tempfile.TemporaryDirectory() as td:
        zp = _build_fixture_zip(Path(td))
        payloads = list(v.parse_export_zip(zp))
        first_msg = payloads[0].messages[0]
        second_msg = payloads[0].messages[1]
        normalized_first = v.to_normalized_event(first_msg, payloads[0].session_ref.external_session_id)
        normalized_second = v.to_normalized_event(second_msg, payloads[0].session_ref.external_session_id)
        _assert(normalized_first.role is MessageRole.USER, f"human → USER; got {normalized_first.role}")
        _assert(
            normalized_second.role is MessageRole.ASSISTANT,
            f"assistant → ASSISTANT; got {normalized_second.role}",
        )


def test_to_raw_event_carries_threading() -> None:
    with tempfile.TemporaryDirectory() as td:
        zp = _build_fixture_zip(Path(td))
        payloads = list(v.parse_export_zip(zp))
        first_msg = payloads[0].messages[0]
        second_msg = payloads[0].messages[1]
        raw_first = v.to_raw_event(first_msg, payloads[0].session_ref.external_session_id)
        raw_second = v.to_raw_event(second_msg, payloads[0].session_ref.external_session_id)
        _assert(raw_first.vendor_event_id == "msg-001-A", raw_first.vendor_event_id or "")
        _assert(raw_first.vendor_parent_event_id is None, str(raw_first.vendor_parent_event_id))
        _assert(raw_second.vendor_parent_event_id == "msg-001-A", str(raw_second.vendor_parent_event_id))


def test_bad_zip_raises_value_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad.zip"
        bad.write_bytes(b"not a zip")
        try:
            list(v.parse_export_zip(bad))
        except ValueError as exc:
            _assert("not a valid ZIP" in str(exc), f"unexpected err: {exc}")
            return
        raise AssertionError("malformed ZIP must raise ValueError")


def main() -> int:
    tests = [
        test_parse_three_conversations,
        test_root_sentinel_maps_to_none,
        test_sender_to_role_mapping,
        test_to_raw_event_carries_threading,
        test_bad_zip_raises_value_error,
    ]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
