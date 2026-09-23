"""M9 §5 — probe (c) attachment representation outcome smoke.

Documents the M9 probe-(c) finding: 27/609 messages in operator's actual
2026-06-11 export had non-empty attachments; 30/609 had non-empty files.
ZIP contains ONLY metadata (file_uuid + file_name) — no actual file bytes.

M9 defers __attachment event emission behind EMIT_ATTACHMENT_EVENTS flag
(default False). This smoke verifies the flag default + the
_AttachmentMeta extraction shape so a future M-section can flip it on.
"""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.vendor import claude_ai_export as v  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_emit_attachment_events_flag_default_false() -> None:
    _assert(
        v.EMIT_ATTACHMENT_EVENTS is False,
        f"M9 ships with EMIT_ATTACHMENT_EVENTS=False (defer); got {v.EMIT_ATTACHMENT_EVENTS!r}",
    )


def test_attachment_metadata_extracted_when_non_empty() -> None:
    """Even with EMIT_ATTACHMENT_EVENTS=False, metadata is parsed into _AttachmentMeta tuples."""
    with tempfile.TemporaryDirectory() as td:
        convs = [
            {
                "uuid": "conv-1",
                "name": "With attachment",
                "summary": "",
                "account": {},
                "created_at": "2025-11-01T00:00:00Z",
                "updated_at": "2025-11-01T00:00:00Z",
                "chat_messages": [
                    {
                        "uuid": "msg-1",
                        "sender": "human",
                        "text": "see file",
                        "content": "Please review the attached file",
                        "created_at": "2025-11-01T00:00:00Z",
                        "updated_at": "2025-11-01T00:00:00Z",
                        "attachments": [],
                        "files": [
                            {
                                "file_uuid": "41b94b3c-7b06-4e9b-8460-b71752d46a91",
                                "file_name": "screenshot.png",
                            }
                        ],
                        "parent_message_uuid": v.ROOT_SENTINEL,
                    }
                ],
            }
        ]
        zp = Path(td) / "fix.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr(v.CONVERSATIONS_ENTRY, json.dumps(convs))
        payloads = list(v.parse_export_zip(zp))
        msg = payloads[0].messages[0]
        _assert(
            len(msg.attachment_metas) == 1,
            f"one file metadata expected; got {len(msg.attachment_metas)}",
        )
        meta = msg.attachment_metas[0]
        _assert(meta.kind == "file", f"kind: {meta.kind}")
        _assert(
            meta.file_uuid == "41b94b3c-7b06-4e9b-8460-b71752d46a91",
            f"file_uuid: {meta.file_uuid}",
        )
        _assert(meta.file_name == "screenshot.png", f"file_name: {meta.file_name}")


def test_attachment_count_propagates_to_raw_payload() -> None:
    """to_raw_event captures attachment count so the importer / future operator can see it."""
    with tempfile.TemporaryDirectory() as td:
        convs = [
            {
                "uuid": "conv-1",
                "name": "n",
                "summary": "",
                "account": {},
                "created_at": "2025-11-01T00:00:00Z",
                "updated_at": "2025-11-01T00:00:00Z",
                "chat_messages": [
                    {
                        "uuid": "msg-1",
                        "sender": "human",
                        "text": "",
                        "content": "Q",
                        "created_at": "2025-11-01T00:00:00Z",
                        "updated_at": "2025-11-01T00:00:00Z",
                        "attachments": [{"file_uuid": "a1", "file_name": "a.txt"}],
                        "files": [{"file_uuid": "f1", "file_name": "f.png"}],
                        "parent_message_uuid": v.ROOT_SENTINEL,
                    }
                ],
            }
        ]
        zp = Path(td) / "fix.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr(v.CONVERSATIONS_ENTRY, json.dumps(convs))
        payloads = list(v.parse_export_zip(zp))
        msg = payloads[0].messages[0]
        raw = v.to_raw_event(msg, "conv-1")
        _assert(
            raw.payload["attachment_count"] == 2,
            f"attachment_count must include both attachments + files; got {raw.payload['attachment_count']}",
        )


def main() -> int:
    tests = [
        test_emit_attachment_events_flag_default_false,
        test_attachment_metadata_extracted_when_non_empty,
        test_attachment_count_propagates_to_raw_payload,
    ]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
