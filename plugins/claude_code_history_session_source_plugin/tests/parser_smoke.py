"""M7 §1.5 — vendor parser smoke for claude_code_history.

Verifies the line-shape parser:
- sessionId-present lines key by sessionId
- sessionId-absent lines key by the orphan hash
- timestamp is divided by 1000 → datetime
- bad shapes (missing display / non-numeric timestamp) WARN-and-skip
- malformed JSON raises ValueError (KB "Critical Development Guidelines v2")
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.vendor import claude_code_history as v  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _enc(rows: list[dict[str, object]]) -> bytes:
    return ("\n".join(json.dumps(r) for r in rows) + "\n").encode("utf-8")


def test_sessionid_lines_key_by_sessionid() -> None:
    payload = _enc(
        [
            {"display": "hello", "timestamp": 1_700_000_000_000, "sessionId": "uuid-aaa", "project": "/p"},
            {"display": "world", "timestamp": 1_700_000_001_000, "sessionId": "uuid-aaa", "project": "/p"},
            {"display": "other", "timestamp": 1_700_000_002_000, "sessionId": "uuid-bbb", "project": "/p"},
        ]
    )
    parsed = list(v.parse_file_from_offset(io.BytesIO(payload), 0))
    _assert(len(parsed) == 3, f"expected 3 parsed lines, got {len(parsed)}")
    _assert(parsed[0].external_session_id == "uuid-aaa", parsed[0].external_session_id)
    _assert(parsed[1].external_session_id == "uuid-aaa", parsed[1].external_session_id)
    _assert(parsed[2].external_session_id == "uuid-bbb", parsed[2].external_session_id)
    _assert(parsed[0].display == "hello", parsed[0].display)
    _assert(
        parsed[0].event_at == datetime.fromtimestamp(1_700_000_000, tz=UTC),
        f"event_at mismatch: {parsed[0].event_at}",
    )


def test_orphan_key_stable_and_correct() -> None:
    project = "/Users/alice/proj"
    payload = _enc(
        [{"display": "hello", "timestamp": 1_700_000_000_000, "project": project}]
    )
    parsed = list(v.parse_file_from_offset(io.BytesIO(payload), 0))
    _assert(len(parsed) == 1, f"got {len(parsed)}")
    expected_hash = hashlib.sha256(project.encode("utf-8")).hexdigest()[:16]
    expected_key = f"history_orphan_1700000000000_{expected_hash}"
    _assert(
        parsed[0].external_session_id == expected_key,
        f"orphan key mismatch: expected {expected_key!r}, got {parsed[0].external_session_id!r}",
    )


def test_byte_offset_advances() -> None:
    payload = _enc(
        [
            {"display": "a", "timestamp": 1_700_000_000_000, "sessionId": "s1", "project": "/p"},
            {"display": "b", "timestamp": 1_700_000_001_000, "sessionId": "s1", "project": "/p"},
        ]
    )
    handle = io.BytesIO(payload)
    handle.seek(0)
    parsed = list(v.parse_file_from_offset(handle, 0))
    _assert(parsed[0].byte_offset < parsed[1].byte_offset, "byte_offset must advance line-by-line")
    _assert(parsed[1].byte_offset == len(payload), "final byte_offset must equal full payload length")


def test_partial_trailing_line_left_for_next_poll() -> None:
    payload_full = _enc(
        [{"display": "complete", "timestamp": 1_700_000_000_000, "sessionId": "s1", "project": "/p"}]
    )
    # Append a partial line (no trailing newline) — parser must STOP there
    partial = b'{"display":"partial","timestamp":1700000001000,"sessionId":"s1","project":"/p"}'
    handle = io.BytesIO(payload_full + partial)
    parsed = list(v.parse_file_from_offset(handle, 0))
    _assert(len(parsed) == 1, f"expected 1 complete line, got {len(parsed)} (partial leaked)")
    _assert(parsed[0].external_session_id == "s1", parsed[0].external_session_id)
    _assert(
        parsed[0].byte_offset == len(payload_full),
        f"byte_offset should stop at end of complete line, got {parsed[0].byte_offset}",
    )


def test_missing_display_or_timestamp_skips() -> None:
    payload = _enc(
        [
            {"display": "ok", "timestamp": 1_700_000_000_000, "sessionId": "s1", "project": "/p"},
            {"timestamp": 1_700_000_001_000, "sessionId": "s2", "project": "/p"},  # missing display
            {"display": "ok2", "sessionId": "s3", "project": "/p"},  # missing timestamp
            {"display": "ok3", "timestamp": 1_700_000_003_000, "sessionId": "s4", "project": "/p"},
        ]
    )
    parsed = list(v.parse_file_from_offset(io.BytesIO(payload), 0))
    _assert(len(parsed) == 2, f"expected 2 (bad lines skipped), got {len(parsed)}")
    _assert([p.external_session_id for p in parsed] == ["s1", "s4"], "wrong survivors")


def test_malformed_json_raises_value_error() -> None:
    payload = b'{"display":"good","timestamp":1700000000000,"sessionId":"s1","project":"/p"}\n{garbage\n'
    try:
        list(v.parse_file_from_offset(io.BytesIO(payload), 0))
    except ValueError as e:
        _assert("malformed JSON" in str(e), f"unexpected ValueError text: {e}")
        return
    raise AssertionError("malformed JSON should raise ValueError")


def test_to_raw_event_shape() -> None:
    payload = _enc(
        [{"display": "x", "timestamp": 1_700_000_000_000, "sessionId": "s1", "project": "/p"}]
    )
    parsed = next(v.parse_file_from_offset(io.BytesIO(payload), 0))
    raw = v.to_raw_event(parsed)
    _assert(raw.external_session_id == "s1", raw.external_session_id)
    _assert(raw.vendor_event_id == f"history_{parsed.byte_offset}", raw.vendor_event_id)
    _assert(raw.vendor_parent_event_id is None, "parent must be None")
    _assert(raw.payload["kind"] == v.PAYLOAD_KIND_MESSAGE, raw.payload["kind"])
    _assert(raw.payload["display"] == "x", raw.payload["display"])
    _assert(raw.payload["project"] == "/p", raw.payload["project"])
    _assert(raw.payload["_byte_offset"] == parsed.byte_offset, "byte_offset propagation")


def main() -> int:
    tests = [
        test_sessionid_lines_key_by_sessionid,
        test_orphan_key_stable_and_correct,
        test_byte_offset_advances,
        test_partial_trailing_line_left_for_next_poll,
        test_missing_display_or_timestamp_skips,
        test_malformed_json_raises_value_error,
        test_to_raw_event_shape,
    ]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
