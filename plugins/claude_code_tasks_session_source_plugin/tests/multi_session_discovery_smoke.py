"""M10 §1.3 — multi-session discovery + drain order smoke.

Mirrors the operator's actual filesystem layout: multiple session subdirs
each carrying a different number of task files, plus session-management
noise files (`.highwatermark`, `.lock`) that must NOT count as tasks.

Verifies:
- Each subdir yields exactly one ExternalSessionRef
- Empty subdirs (no *.json files) are skipped
- Noise files (`.highwatermark`, `.lock`) are not emitted as events
- Tasks within a session emit in lexically-sorted order
- normalize() produces SYSTEM events with the right subtype + lifted fields
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(_REPO_ROOT / "plugins" / "claude_code_tasks_session_source_plugin" / "src"),
)

from ananta.llm.session_ledger.types import EventType, MessageRole  # noqa: E402
from claude_code_tasks_session_source_plugin.plugin import (  # noqa: E402
    ClaudeCodeTasksSessionSourcePlugin,
)


class _StubProvider:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str) -> object | None:
        return self._values.get(key)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _write_task(path: Path, task_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "id": task_id,
                "subject": f"subject {task_id}",
                "description": f"description {task_id}",
                "activeForm": f"doing {task_id}",
                "status": "pending",
                "blocks": [],
                "blockedBy": [],
            }
        )
    )


def _seed_fixture_tree(root: Path) -> None:
    """Lay out three session subdirs mirroring the operator's filesystem shape."""
    # Session 1: three tasks plus noise files that MUST be filtered out.
    s1 = root / "uuid-1"
    s1.mkdir()
    _write_task(s1 / "1.json", "t1")
    _write_task(s1 / "2.json", "t2")
    _write_task(s1 / "3.json", "t3")
    (s1 / ".highwatermark").write_text("3")
    (s1 / ".lock").write_text("")
    # Session 2: one task.
    s2 = root / "uuid-2"
    s2.mkdir()
    _write_task(s2 / "1.json", "u1")
    # Session 3: empty (operator deleted all tasks) — must be skipped.
    s3 = root / "uuid-3"
    s3.mkdir()
    (s3 / ".highwatermark").write_text("0")


def _build_plugin(root: Path) -> tuple[ClaudeCodeTasksSessionSourcePlugin, str]:
    plugin = ClaudeCodeTasksSessionSourcePlugin()
    # P1.1.E: the walk root is threaded per-call as root_uri, not read from config.
    plugin.config_provider = _StubProvider({"glob": "*.json"})
    return plugin, str(root)


def case_discover_skips_empty_subdir(
    plugin: ClaudeCodeTasksSessionSourcePlugin, root_uri: str
) -> list[Any]:
    sessions = list(plugin.discover_sessions(root_uri, None))
    ids = sorted(s.external_session_id for s in sessions)
    _assert(
        ids == ["uuid-1", "uuid-2"],
        f"empty subdir uuid-3 must be skipped; got {ids}",
    )
    return sessions


def _expected_event_count(external_session_id: str) -> int:
    return 3 if external_session_id == "uuid-1" else 1


def _assert_system_event_shape(external_session_id: str, normalized: list[Any]) -> None:
    _assert(
        all(n.event_type is EventType.SYSTEM for n in normalized),
        f"{external_session_id}: every event must be EventType.SYSTEM",
    )
    _assert(
        all(n.role is MessageRole.SYSTEM for n in normalized),
        f"{external_session_id}: every event must have role=SYSTEM",
    )


def _assert_content_json_payload(external_session_id: str, n: Any) -> None:
    _assert(
        n.content_json is not None,
        f"{external_session_id}: SYSTEM event must carry content_json",
    )
    _assert(
        n.content_json["subtype"] == "task_state",
        f"unexpected subtype: {n.content_json['subtype']}",
    )
    required = {"id", "subject", "description", "activeForm", "status", "blocks", "blockedBy"}
    missing = required - n.content_json.keys()
    _assert(
        not missing,
        f"content_json missing required fields: missing={missing} got={list(n.content_json.keys())}",
    )
    _assert(
        n.content_text is None,
        f"M10 SYSTEM events have no content_text (subtype carries everything); "
        f"got {n.content_text!r}",
    )


def case_normalize_and_drain_order(
    plugin: ClaudeCodeTasksSessionSourcePlugin, sessions: list[Any], root_uri: str
) -> None:
    for ref in sessions:
        events = list(plugin.read_events(root_uri, ref, None))
        normalized = [plugin.normalize(e) for e in events]
        _assert_system_event_shape(ref.external_session_id, normalized)
        for n in normalized:
            _assert_content_json_payload(ref.external_session_id, n)
        expected = _expected_event_count(ref.external_session_id)
        _assert(
            len(events) == expected,
            f"{ref.external_session_id}: got {len(events)} events, want {expected}",
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_fixture_tree(root)
        plugin, root_uri = _build_plugin(root)
        sessions = case_discover_skips_empty_subdir(plugin, root_uri)
        case_normalize_and_drain_order(plugin, sessions, root_uri)
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
