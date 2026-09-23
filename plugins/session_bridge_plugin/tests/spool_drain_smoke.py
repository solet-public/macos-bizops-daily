#!/usr/bin/env python3
"""Offline smoke for the session dispatch bridge's spool schema + drainer.

Run:

    .venv/bin/python3 plugins/session_bridge_plugin/tests/spool_drain_smoke.py

Covers the plugin's actual write-surface logic without touching the real
``~/.ananta`` spool home or any live memory_service: ``parse_spool_line``'s
well-formed/blank/torn/invalid cases (design §7), and ``SpoolDrainer.drain_once``
end-to-end against a tmp spool dir + an in-memory fake memory_service —
TaskCreated writes an audit record and an in-flight upsert, TaskCompleted
writes an audit record and clears the in-flight tag, the cursor advances past
drained files and stays put on an undrainable one (retry-from-file-one
semantics, design §3 D2.2). Exercises ``SpoolDrainer`` directly (not the
``SessionBridgePlugin`` wrapper) so no test touches the real
``~/.ananta`` spool home.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "session_bridge_plugin" / "src"))

from ananta.interfaces.memory_service_interface import MemoryServiceInterface  # noqa: E402
from session_bridge_plugin.drainer import SpoolDrainer  # noqa: E402
from session_bridge_plugin.spool_schema import (  # noqa: E402
    EVENT_TASK_COMPLETED,
    EVENT_TASK_CREATED,
    in_flight_tag,
    parse_spool_line,
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


class _FakeMemoryService:
    """Records every call; no embedding, no network, no real memory_service."""

    def __init__(self) -> None:
        self.remembered: list[dict[str, Any]] = []
        self.upserted: list[dict[str, Any]] = []
        self.deleted_tags: list[str] = []

    def remember(self, *, content: str, tags: list[str], session_id: str, embed: bool) -> None:
        self.remembered.append(
            {"content": content, "tags": tags, "session_id": session_id, "embed": embed},
        )

    def upsert_memory_by_tag(self, *, content: str, tag: str, session_id: str) -> None:
        self.upserted.append({"content": content, "tag": tag, "session_id": session_id})

    def delete_memories_by_tag(self, tag: str) -> None:
        self.deleted_tags.append(tag)


def _spool_line(event: str, session_id: str, task_id: str) -> str:
    import json

    return json.dumps(
        {
            "event": event,
            "session_id": session_id,
            "received_at": "2026-09-22T00:00:00+00:00",
            "agent": "claude_code",
            "source": "hook_bridge",
            "payload": {"task_id": task_id, "summary": f"{event} for {task_id}"},
        },
    )


def _spool_filename() -> str:
    """Matches the producer's fixed-width ``{time.time_ns()}-{uuid}.jsonl`` naming
    (spool_schema.py's docstring) so lexicographic order == chronological order."""
    return f"{time.time_ns()}-{uuid.uuid4().hex}.jsonl"


# ─── parse_spool_line ────────────────────────────────────────────────────────


def test_parse_spool_line_well_formed_defaults_agent_and_source() -> None:
    line = '{"event": "TaskCreated", "session_id": "s1", "received_at": "2026-09-22T00:00:00+00:00"}'
    record = parse_spool_line(line)
    _check(record is not None, "well-formed line without agent/source parses")
    if record is not None:
        _check(record.agent == "claude_code", f"agent defaults to claude_code (got {record.agent!r})")
        _check(record.source == "hook_bridge", f"source defaults to hook_bridge (got {record.source!r})")
        _check(record.payload == {}, f"missing payload defaults to {{}} (got {record.payload!r})")


def test_parse_spool_line_blank_returns_none() -> None:
    _check(parse_spool_line("") is None, "blank line -> None")
    _check(parse_spool_line("   \n") is None, "whitespace-only line -> None")


def test_parse_spool_line_torn_json_returns_none() -> None:
    _check(parse_spool_line('{"event": "TaskCreated", "sess') is None, "torn mid-append JSON -> None")


def test_parse_spool_line_non_dict_returns_none() -> None:
    _check(parse_spool_line("[1, 2, 3]") is None, "non-dict JSON -> None")


def test_parse_spool_line_missing_required_field_returns_none() -> None:
    line = '{"event": "TaskCreated", "received_at": "2026-09-22T00:00:00+00:00"}'
    _check(parse_spool_line(line) is None, "missing session_id -> None")


# ─── SpoolDrainer.drain_once ─────────────────────────────────────────────────


def test_drain_once_drains_created_then_completed_in_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spool_dir = Path(tmp) / "spool"
        spool_dir.mkdir()
        cursor_dir = spool_dir / "cursors"
        lock_path = spool_dir.parent / ".janitor.lock"

        created_name = _spool_filename()
        (spool_dir / created_name).write_text(
            _spool_line(EVENT_TASK_CREATED, "sess-1", "task-1") + "\n", encoding="utf-8",
        )
        time.sleep(0.001)  # guarantee a distinct, later time.time_ns() filename
        completed_name = _spool_filename()
        (spool_dir / completed_name).write_text(
            _spool_line(EVENT_TASK_COMPLETED, "sess-1", "task-1") + "\n", encoding="utf-8",
        )

        drainer = SpoolDrainer(
            drainer_id="test-solet",
            spool_dir=spool_dir,
            cursor_dir=cursor_dir,
            lock_path=lock_path,
            logger=logging.getLogger("spool_drain_smoke"),
        )
        fake = _FakeMemoryService()
        drained = drainer.drain_once(cast(MemoryServiceInterface, fake))

        _check(drained == 2, f"drain_once drains both files in one tick (got {drained})")
        _check(len(fake.remembered) == 2, f"remember called once per event (got {len(fake.remembered)})")
        _check(
            fake.remembered[0]["content"].startswith(EVENT_TASK_CREATED),
            "audit records land in file (chronological) order: created first",
        )
        _check(
            len(fake.upserted) == 1 and fake.upserted[0]["tag"] == in_flight_tag("sess-1"),
            "TaskCreated upserts the in-flight tag",
        )
        _check(
            fake.deleted_tags == [in_flight_tag("sess-1")],
            f"TaskCompleted deletes the in-flight tag (got {fake.deleted_tags})",
        )

        from session_bridge_plugin.cursor import read_cursor

        cursor = read_cursor(cursor_dir, "test-solet")
        _check(
            cursor is not None and cursor["position"] == completed_name,
            "cursor position advances to the newest drained filename",
        )


def test_drain_once_leaves_cursor_unadvanced_on_torn_file_then_retries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spool_dir = Path(tmp) / "spool"
        spool_dir.mkdir()
        cursor_dir = spool_dir / "cursors"
        lock_path = spool_dir.parent / ".janitor.lock"

        good_name = _spool_filename()
        (spool_dir / good_name).write_text(
            _spool_line(EVENT_TASK_CREATED, "sess-2", "task-2") + "\n", encoding="utf-8",
        )
        time.sleep(0.001)
        torn_name = _spool_filename()
        # A torn mid-append write: valid line followed by a truncated one.
        (spool_dir / torn_name).write_text(
            _spool_line(EVENT_TASK_CREATED, "sess-3", "task-3") + "\n" + '{"event": "TaskCrea',
            encoding="utf-8",
        )

        drainer = SpoolDrainer(
            drainer_id="test-solet-2",
            spool_dir=spool_dir,
            cursor_dir=cursor_dir,
            lock_path=lock_path,
            logger=logging.getLogger("spool_drain_smoke"),
        )
        fake = _FakeMemoryService()
        drained = drainer.drain_once(cast(MemoryServiceInterface, fake))

        _check(drained == 1, f"only the good file drains this tick (got {drained})")

        from session_bridge_plugin.cursor import read_cursor

        cursor = read_cursor(cursor_dir, "test-solet-2")
        _check(
            cursor is not None and cursor["position"] == good_name,
            "cursor stops at the last good file, not the torn one",
        )

        # Fix the torn file in place and re-drain: retry-from-file-one semantics.
        (spool_dir / torn_name).write_text(
            _spool_line(EVENT_TASK_CREATED, "sess-3", "task-3") + "\n", encoding="utf-8",
        )
        drained_again = drainer.drain_once(cast(MemoryServiceInterface, fake))
        _check(drained_again == 1, f"fixed file drains on the next tick (got {drained_again})")
        cursor_after = read_cursor(cursor_dir, "test-solet-2")
        _check(
            cursor_after is not None and cursor_after["position"] == torn_name,
            "cursor advances past the now-fixed file",
        )


def test_drain_once_empty_spool_is_a_clean_noop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spool_dir = Path(tmp) / "spool"
        spool_dir.mkdir()
        drainer = SpoolDrainer(
            drainer_id="test-solet-3",
            spool_dir=spool_dir,
            cursor_dir=spool_dir / "cursors",
            lock_path=spool_dir.parent / ".janitor.lock",
            logger=logging.getLogger("spool_drain_smoke"),
        )
        fake = _FakeMemoryService()
        _check(drainer.drain_once(cast(MemoryServiceInterface, fake)) == 0, "empty spool drains nothing")
        _check(fake.remembered == [], "no memory_service calls on an empty spool")


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    print("=== spool_drain_smoke (session_bridge_plugin) ===")
    test_parse_spool_line_well_formed_defaults_agent_and_source()
    test_parse_spool_line_blank_returns_none()
    test_parse_spool_line_torn_json_returns_none()
    test_parse_spool_line_non_dict_returns_none()
    test_parse_spool_line_missing_required_field_returns_none()
    test_drain_once_drains_created_then_completed_in_order()
    test_drain_once_leaves_cursor_unadvanced_on_torn_file_then_retries()
    test_drain_once_empty_spool_is_a_clean_noop()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
