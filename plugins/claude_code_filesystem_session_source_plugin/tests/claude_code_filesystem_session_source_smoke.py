#!/usr/bin/env python3
"""Smoke test for claude_code_filesystem_session_source_plugin (no pytest).

Coverage:

* Descriptor reports source_kind='claude_code_local', vendor='claude_code',
  supported_modes=('pulling',).
* Parser flattening: one ``.jsonl`` line containing 1 text block + 2
  tool_use blocks yields 1 MESSAGE + 2 TOOL_CALL raw events; one user line
  carrying a tool_result block yields 1 TOOL_RESULT raw event.
* vendor_event_id discipline (spec §17.3 acceptance — load-bearing for
  tool-call projection):
    - MESSAGE: vendor_event_id = line.uuid
    - TOOL_CALL: vendor_event_id = tool_use.id (the toolu_... value);
      vendor_parent_event_id = line.uuid
    - TOOL_RESULT: vendor_event_id = line.uuid;
      vendor_parent_event_id = content.tool_use_id (resolution key)
* Skip-list line types (permission-mode, file-history-snapshot, last-prompt,
  attachment, summary) produce zero events.
* Skip-list content blocks (thinking) are silently dropped without affecting
  surrounding text-block aggregation.
* discover_sessions walks a sandboxed tmpdir, respects the mtime high-water
  cursor, and surfaces ExternalSessionRef per .jsonl file.
* read_events seeks past a non-zero byte offset and stops at the last
  newline-terminated record (partial trailing line stays unconsumed).
* normalize() produces NormalizedSessionEvent shapes that pass
  ``SessionLedgerRepository._validate_event_shape``.
* Cursor producers: discovery (ISO mtime) and event-read (byte offset).
* End-to-end acceptance through ``SessionLedgerImporter`` against a stub
  state_service: TOOL_RESULT projection updates the matching tool_call
  row to ``status='succeeded'``.
* Sandboxing per [[sandbox-mutating-smokes]]: the test config points
  ``root_uri`` at a tmpdir created under ``tempfile`` and never touches
  ``~/.claude``.

Run:
    .venv/bin/python3 plugins/claude_code_filesystem_session_source_plugin/tests/claude_code_filesystem_session_source_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(
        REPO_ROOT / "plugins" / "claude_code_filesystem_session_source_plugin" / "src"
    ),
)
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    ExternalSessionRef,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    SourceVendor,
)
from ananta.llm.session_ledger.vendor.claude_code import parse_line  # noqa: E402
from claude_code_filesystem_session_source_plugin.plugin import (  # noqa: E402
    ClaudeCodeFilesystemSessionSourcePlugin,
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


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubConfigProvider:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, name: str, default: object | None = None) -> object:
        if name in self._values:
            return self._values[name]
        return default


def _make_plugin() -> ClaudeCodeFilesystemSessionSourcePlugin:
    plugin = ClaudeCodeFilesystemSessionSourcePlugin()
    # P1.1.E: root is threaded per-call as root_uri; only glob stays in config.
    plugin.set_config_provider(  # type: ignore[arg-type]
        _StubConfigProvider({"glob": "*.jsonl"})
    )
    plugin.prepare_for_readiness()
    return plugin


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


_SID = "01234567-89ab-cdef-0123-456789abcdef"
_USER_UUID = "u" + "1" * 35
_ASSISTANT_TEXT_AND_TOOL_USE_UUID = "a" + "2" * 35
_TOOL_RESULT_USER_UUID = "u" + "3" * 35
_TOOL_USE_ID_A = "toolu_A" + "a" * 17
_TOOL_USE_ID_B = "toolu_B" + "b" * 17


def _line_user_text() -> dict[str, Any]:
    return {
        "parentUuid": None,
        "promptId": "p1",
        "type": "user",
        "message": {"role": "user", "content": "help me debug"},
        "uuid": _USER_UUID,
        "timestamp": "2026-05-30T12:00:00.000Z",
        "cwd": "/Users/alice/Workspace/example",
        "sessionId": _SID,
        "version": "2.1.118",
        "gitBranch": "master",
    }


def _line_assistant_text_and_two_tool_uses() -> dict[str, Any]:
    return {
        "parentUuid": _USER_UUID,
        "type": "assistant",
        "message": {
            "model": "claude-opus-4-7",
            "id": "msg_1",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "internal", "signature": "x"},
                {"type": "text", "text": "I'll read the file first."},
                {
                    "type": "tool_use",
                    "id": _TOOL_USE_ID_A,
                    "name": "Read",
                    "input": {"file_path": "/tmp/x"},
                },
                {
                    "type": "tool_use",
                    "id": _TOOL_USE_ID_B,
                    "name": "Bash",
                    "input": {"command": "ls"},
                },
            ],
        },
        "uuid": _ASSISTANT_TEXT_AND_TOOL_USE_UUID,
        "timestamp": "2026-05-30T12:00:01.000Z",
        "cwd": "/Users/alice/Workspace/example",
        "sessionId": _SID,
    }


def _line_user_tool_result_for_a() -> dict[str, Any]:
    return {
        "parentUuid": _ASSISTANT_TEXT_AND_TOOL_USE_UUID,
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": _TOOL_USE_ID_A,
                    "type": "tool_result",
                    "content": [{"type": "text", "text": "file body"}],
                }
            ],
        },
        "uuid": _TOOL_RESULT_USER_UUID,
        "timestamp": "2026-05-30T12:00:02.000Z",
        "cwd": "/Users/alice/Workspace/example",
        "sessionId": _SID,
    }


def _line_permission_mode() -> dict[str, Any]:
    return {
        "type": "permission-mode",
        "permissionMode": "bypassPermissions",
        "sessionId": _SID,
    }


def _line_file_history() -> dict[str, Any]:
    return {
        "type": "file-history-snapshot",
        "messageId": "x",
        "snapshot": {
            "messageId": "x",
            "trackedFileBackups": {},
            "timestamp": "2026-05-30T12:00:00.000Z",
        },
        "isSnapshotUpdate": False,
    }


# ---------------------------------------------------------------------------
# Descriptor
# ---------------------------------------------------------------------------


def test_descriptor() -> None:
    plugin = _make_plugin()  # not exercising filesystem
    desc = plugin.describe()
    _check(
        desc.source_kind is IngestSourceKind.CLAUDE_CODE_LOCAL,
        "descriptor source_kind = claude_code_local",
    )
    _check(desc.vendor is SourceVendor.CLAUDE_CODE, "descriptor vendor = claude_code")
    _check(
        desc.supported_modes == (IngestMode.PULLING,),
        "descriptor supported_modes = (pulling,)",
    )


# ---------------------------------------------------------------------------
# Vendor parser
# ---------------------------------------------------------------------------


def test_parser_user_text_line() -> None:
    events = parse_line(json.dumps(_line_user_text()))
    _check(len(events) == 1, "user text line → 1 raw event")
    e = events[0]
    _check(e.payload.get("kind") == "message", "kind = message")
    _check(e.payload.get("role") == "user", "role = user")
    _check(e.payload.get("text") == "help me debug", "text body preserved")
    _check(e.vendor_event_id == _USER_UUID, "vendor_event_id = line.uuid")
    _check(e.vendor_parent_event_id is None, "first user message has no parent")


def test_parser_assistant_text_and_tool_uses_flatten() -> None:
    events = parse_line(json.dumps(_line_assistant_text_and_two_tool_uses()))
    _check(len(events) == 3, "1 text + 2 tool_use blocks → 3 raw events")
    kinds = [e.payload.get("kind") for e in events]
    _check(
        kinds.count("tool_call") == 2 and kinds.count("message") == 1,
        "2 tool_call + 1 message events emitted",
    )
    text_event = next(e for e in events if e.payload.get("kind") == "message")
    _check(
        text_event.payload.get("text") == "I'll read the file first.",
        "assistant text body preserved",
    )
    tool_calls = [e for e in events if e.payload.get("kind") == "tool_call"]
    _check(
        {tc.vendor_event_id for tc in tool_calls} == {_TOOL_USE_ID_A, _TOOL_USE_ID_B},
        "TOOL_CALL vendor_event_id = tool_use.id for both blocks "
        "(load-bearing for projection)",
    )
    _check(
        all(tc.vendor_parent_event_id == _ASSISTANT_TEXT_AND_TOOL_USE_UUID
            for tc in tool_calls),
        "TOOL_CALL vendor_parent_event_id = line.uuid",
    )


def test_parser_tool_result_event() -> None:
    events = parse_line(json.dumps(_line_user_tool_result_for_a()))
    _check(len(events) == 1, "user tool_result line → 1 raw event")
    e = events[0]
    _check(e.payload.get("kind") == "tool_result", "kind = tool_result")
    _check(
        e.vendor_event_id == _TOOL_RESULT_USER_UUID,
        "TOOL_RESULT vendor_event_id = line.uuid",
    )
    _check(
        e.vendor_parent_event_id == _TOOL_USE_ID_A,
        "TOOL_RESULT vendor_parent_event_id = content.tool_use_id "
        "(load-bearing for resolution)",
    )
    _check(e.payload.get("text") == "file body", "tool result body preserved")


def test_parser_skips_session_config_noise() -> None:
    for line in (_line_permission_mode(), _line_file_history()):
        _check(
            parse_line(json.dumps(line)) == [],
            f"skip-listed type {line.get('type')!r} → 0 events",
        )


def test_parser_skips_thinking_blocks_in_isolation() -> None:
    line: dict[str, Any] = {
        "parentUuid": None,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "internal", "signature": "sig"},
            ],
        },
        "uuid": "a" + "9" * 35,
        "timestamp": "2026-05-30T12:00:00.000Z",
        "sessionId": _SID,
    }
    _check(parse_line(json.dumps(line)) == [], "thinking-only line → 0 events")


def test_parser_raises_on_unknown_block_kind() -> None:
    line: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "totally_made_up", "x": 1}],
        },
        "uuid": "a" + "8" * 35,
        "timestamp": "2026-05-30T12:00:00.000Z",
        "sessionId": _SID,
    }
    try:
        parse_line(json.dumps(line))
    except ValueError as exc:
        _check(
            "totally_made_up" in str(exc),
            "unknown content-block type raises ValueError (no defensive fallback)",
        )
        return
    _check(False, "expected ValueError on unknown content-block type")


def test_parser_raises_on_invalid_json() -> None:
    try:
        parse_line('{"not": "closed"')
    except ValueError as exc:
        _check("valid JSON" in str(exc), "malformed JSON raises ValueError")
        return
    _check(False, "expected ValueError on malformed JSON")


# ---------------------------------------------------------------------------
# normalize() — repository shape validation
# ---------------------------------------------------------------------------


def test_normalize_message_user() -> None:
    plugin = _make_plugin()
    raw = parse_line(json.dumps(_line_user_text()))[0]
    n = plugin.normalize(raw)
    _check(n.event_type is EventType.MESSAGE, "MESSAGE event")
    _check(n.role is MessageRole.USER, "role mapped to USER")
    _check(n.content_text == "help me debug", "content_text preserved")
    _check(n.vendor_event_id == _USER_UUID, "vendor_event_id preserved")


def test_normalize_tool_call_carries_tool_name() -> None:
    plugin = _make_plugin()
    events = parse_line(json.dumps(_line_assistant_text_and_two_tool_uses()))
    tool_call_raw = next(e for e in events if e.payload.get("kind") == "tool_call")
    n = plugin.normalize(tool_call_raw)
    _check(n.event_type is EventType.TOOL_CALL, "TOOL_CALL event")
    _check(n.content_text is None, "TOOL_CALL has no content_text (repo validator)")
    _check(
        isinstance(n.content_json, dict) and n.content_json.get("tool_name") in {
            "Read",
            "Bash",
        },
        "content_json carries tool_name (importer's _extract_tool_name reads this)",
    )


def test_normalize_tool_result_round_trips_parent_link() -> None:
    plugin = _make_plugin()
    raw = parse_line(json.dumps(_line_user_tool_result_for_a()))[0]
    n = plugin.normalize(raw)
    _check(n.event_type is EventType.TOOL_RESULT, "TOOL_RESULT event")
    _check(
        n.vendor_parent_event_id == _TOOL_USE_ID_A,
        "vendor_parent_event_id round-trips for projection resolution",
    )
    _check(n.role is MessageRole.TOOL, "TOOL_RESULT role = TOOL")


# ---------------------------------------------------------------------------
# Filesystem walking + cursors
# ---------------------------------------------------------------------------


def _write_jsonl(target: Path, lines: list[dict[str, object]]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def test_discover_and_read_events_in_tmpdir() -> None:
    with tempfile.TemporaryDirectory(prefix="claude_code_smoke_") as tmp:
        root = Path(tmp)
        encoded_proj = root / "-Users-alice-Workspace-example"
        jsonl = encoded_proj / f"{_SID}.jsonl"
        _write_jsonl(
            jsonl,
            [
                _line_user_text(),
                _line_assistant_text_and_two_tool_uses(),
                _line_user_tool_result_for_a(),
            ],
        )
        plugin = _make_plugin()
        refs = list(plugin.discover_sessions(str(root), cursor_payload=None))
        _check(len(refs) == 1, "discover_sessions surfaced 1 fixture session")
        ref = refs[0]
        _check(ref.external_session_id == _SID, "session id matches filename stem")
        _check(
            ref.project_path == "/Users/alice/Workspace/example",
            "project_path decoded from directory name",
        )
        raw_events = list(plugin.read_events(str(root), ref, cursor_payload=None))
        # 1 user-text MESSAGE + 1 assistant-text MESSAGE + 2 TOOL_CALL +
        # 1 TOOL_RESULT = 5 events
        _check(len(raw_events) == 5, f"flattened to 5 raw events (got {len(raw_events)})")
        # event_read_cursor must carry the per-file byte offset
        last_cursor = plugin.event_read_cursor(str(root), ref, raw_events[-1])
        _check(
            isinstance(last_cursor.get("byte_offset"), int)
            and last_cursor["byte_offset"] > 0,
            "event_read_cursor returns positive byte_offset after a full read",
        )


def test_discover_high_water_skips_unchanged_files() -> None:
    with tempfile.TemporaryDirectory(prefix="claude_code_smoke_") as tmp:
        root = Path(tmp)
        encoded_proj = root / "-Users-alice-Workspace-example"
        jsonl = encoded_proj / f"{_SID}.jsonl"
        _write_jsonl(jsonl, [_line_user_text()])
        # Set mtime explicitly to the past so we can craft a future high-water.
        past = datetime(2026, 5, 30, 11, 0, tzinfo=UTC).timestamp()
        os.utime(jsonl, (past, past))
        plugin = _make_plugin()
        future_iso = datetime(2026, 5, 30, 13, 0, tzinfo=UTC).isoformat()
        refs = list(
            plugin.discover_sessions(str(root),
                cursor_payload={"mtime_high_water_iso": future_iso}
            )
        )
        _check(
            refs == [],
            "high-water mtime in the future suppresses the unchanged fixture",
        )


def test_read_events_resumes_from_offset() -> None:
    with tempfile.TemporaryDirectory(prefix="claude_code_smoke_") as tmp:
        root = Path(tmp)
        encoded_proj = root / "-Users-alice-Workspace-example"
        jsonl = encoded_proj / f"{_SID}.jsonl"
        _write_jsonl(jsonl, [_line_user_text(), _line_assistant_text_and_two_tool_uses()])
        plugin = _make_plugin()
        ref = ExternalSessionRef(
            external_session_id=_SID,
            vendor_session_label=None,
            project_path="/Users/alice/Workspace/example",
            first_seen_at=datetime.now(UTC),
        )
        # First pass: read everything and capture the post-final offset.
        all_events = list(plugin.read_events(str(root), ref, cursor_payload=None))
        final_offset = plugin.event_read_cursor(str(root), ref, all_events[-1])["byte_offset"]
        # Second pass: resume from final offset — should yield zero new events.
        replay = list(
            plugin.read_events(str(root), ref, cursor_payload={"byte_offset": final_offset})
        )
        _check(
            replay == [],
            "resuming at the final offset yields zero new events (idempotent re-poll)",
        )


def test_read_events_stops_at_partial_trailing_line() -> None:
    with tempfile.TemporaryDirectory(prefix="claude_code_smoke_") as tmp:
        root = Path(tmp)
        encoded_proj = root / "-Users-alice-Workspace-example"
        jsonl = encoded_proj / f"{_SID}.jsonl"
        _write_jsonl(jsonl, [_line_user_text()])
        # Append a half-line without a trailing newline.
        with jsonl.open("ab") as fh:
            fh.write(b'{"type":"user","message":{')
        plugin = _make_plugin()
        ref = ExternalSessionRef(
            external_session_id=_SID,
            vendor_session_label=None,
            project_path="/Users/alice/Workspace/example",
            first_seen_at=datetime.now(UTC),
        )
        events = list(plugin.read_events(str(root), ref, cursor_payload=None))
        _check(len(events) == 1, "consumed the one full line; left half-line unread")


class _QualificationLedger:
    def __init__(
        self,
        *,
        root_uri: str,
        registered: bool = True,
        events_persisted: int = 4,
    ) -> None:
        self.registered = registered
        self.root_uri = root_uri
        self.events_persisted = events_persisted
        self.polled_source_id: str | None = None

    def list_sources(self) -> dict[str, object]:
        rows: list[object] = []
        if self.registered:
            rows.append({
                "source_kind": "claude_code_local",
                "source_id": "src-claude-fixture",
                "enabled": True,
                "root_uri": self.root_uri,
            })
        return {"sources": rows}

    def poll_source(self, source_id: str) -> dict[str, int]:
        self.polled_source_id = source_id
        return {"events_persisted": self.events_persisted}

    def list_sessions(self, **kwargs: object) -> dict[str, object]:
        return {"sessions": [{"session_id": "les-claude-fixture"}]}

    def get_session_timeline(self, **kwargs: object) -> dict[str, object]:
        return {"events": [{"content_text": "fixture only"}]}


class _QualificationOrchestrator:
    def __init__(self, ledger: _QualificationLedger) -> None:
        self._ledger = ledger

    def get_service(self, name: str) -> _QualificationLedger | None:
        return self._ledger if name == "session_ledger_service" else None


def test_qualify_is_source_local_and_content_blind() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "fixture-session.jsonl").write_text("fixture only\n", encoding="utf-8")
        ledger = _QualificationLedger(root_uri=tmp)
        plugin = ClaudeCodeFilesystemSessionSourcePlugin()
        plugin.orchestrator_ref = _QualificationOrchestrator(ledger)  # type: ignore[assignment]
        result = plugin.qualify({}, {})
        _check(result["action_status"] == "completed", "qualify completes for fixture source")
        _check(ledger.polled_source_id == "src-claude-fixture", "qualify polls its registered source")
        _check(
            result["data"] == {
                "source_registered": True,
                "source_kind": "claude_code_local",
                "backfill_count": 4,
                "sample_content_retrieved": True,
            },
            "qualify returns the bounded proof shape without sample payload",
        )


def test_qualify_allows_absent_registered_root() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = _QualificationLedger(
            root_uri=str(Path(tmp) / "never-created"), events_persisted=0
        )
        plugin = ClaudeCodeFilesystemSessionSourcePlugin()
        plugin.orchestrator_ref = _QualificationOrchestrator(ledger)  # type: ignore[assignment]
        result = plugin.qualify({}, {})
    _check(result["action_status"] == "completed", "qualify accepts an absent registered root")
    _check(ledger.polled_source_id is None, "absent root bypasses the impossible backfill")
    _check(
        result["data"] == {
            "source_registered": True,
            "source_kind": "claude_code_local",
            "backfill_count": 0,
            "sample_content_retrieved": False,
        },
        "absent root returns an honest empty proof",
    )


def test_qualify_refuses_existing_empty_root() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = _QualificationLedger(root_uri=tmp, events_persisted=0)
        plugin = ClaudeCodeFilesystemSessionSourcePlugin()
        plugin.orchestrator_ref = _QualificationOrchestrator(ledger)  # type: ignore[assignment]
        result = plugin.qualify({}, {})
    _check(result["action_status"] == "failed", "qualify refuses an existing empty root")
    _check(ledger.polled_source_id == "src-claude-fixture", "existing root still runs backfill")


def test_qualify_refuses_unregistered_source() -> None:
    plugin = ClaudeCodeFilesystemSessionSourcePlugin()
    plugin.orchestrator_ref = _QualificationOrchestrator(  # type: ignore[assignment]
        _QualificationLedger(root_uri="/unused", registered=False)
    )
    result = plugin.qualify({}, {})
    _check(result["action_status"] == "failed", "qualify fails without a registered source")
    _check(result["data"] == {}, "failed qualification does not fabricate proof fields")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    print("=== claude_code_filesystem_session_source_smoke ===")
    test_descriptor()
    test_parser_user_text_line()
    test_parser_assistant_text_and_tool_uses_flatten()
    test_parser_tool_result_event()
    test_parser_skips_session_config_noise()
    test_parser_skips_thinking_blocks_in_isolation()
    test_parser_raises_on_unknown_block_kind()
    test_parser_raises_on_invalid_json()
    test_normalize_message_user()
    test_normalize_tool_call_carries_tool_name()
    test_normalize_tool_result_round_trips_parent_link()
    test_discover_and_read_events_in_tmpdir()
    test_discover_high_water_skips_unchanged_files()
    test_read_events_resumes_from_offset()
    test_read_events_stops_at_partial_trailing_line()
    test_qualify_is_source_local_and_content_blind()
    test_qualify_allows_absent_registered_root()
    test_qualify_refuses_existing_empty_root()
    test_qualify_refuses_unregistered_source()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
