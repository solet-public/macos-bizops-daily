"""M7 §1.5 — cursor advance + idempotent re-read smoke.

Sandbox: writes a tmp history.jsonl, exercises the plugin's
discover_sessions twice. First pass yields all sessions; second pass with
the advanced cursor yields nothing (idempotent re-read).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(_REPO_ROOT / "plugins" / "claude_code_history_session_source_plugin" / "src"),
)

from claude_code_history_session_source_plugin.plugin import (  # noqa: E402
    ClaudeCodeHistorySessionSourcePlugin,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _write_lines(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("ab") as f:
        for r in rows:
            f.write((json.dumps(r) + "\n").encode("utf-8"))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        history = Path(td) / "history.jsonl"
        # Initial three lines, two unique sessions.
        _write_lines(
            history,
            [
                {"display": "a1", "timestamp": 1_700_000_000_000, "sessionId": "uuid-a", "project": "/p"},
                {"display": "a2", "timestamp": 1_700_000_001_000, "sessionId": "uuid-a", "project": "/p"},
                {"display": "b1", "timestamp": 1_700_000_002_000, "sessionId": "uuid-b", "project": "/p"},
            ],
        )
        plugin = ClaudeCodeHistorySessionSourcePlugin()
        # P1.1.E: root is threaded per-call as root_uri, not read from config.
        root_uri = str(history)

        # Pass 1: cold start (cursor=None) — must yield 2 sessions.
        sessions_first = list(plugin.discover_sessions(root_uri, None))
        _assert(
            {s.external_session_id for s in sessions_first} == {"uuid-a", "uuid-b"},
            f"pass1 sessions: {[s.external_session_id for s in sessions_first]}",
        )
        events_a = list(plugin.read_events(root_uri, sessions_first[0], None))
        events_b = list(plugin.read_events(root_uri, sessions_first[1], None))
        total_first_pass = len(events_a) + len(events_b)
        _assert(total_first_pass == 3, f"expected 3 events across both sessions, got {total_first_pass}")
        cursor_after_first = plugin.session_discovery_cursor(root_uri, sessions_first[-1])
        _assert(
            "byte_offset" in cursor_after_first,
            f"cursor must carry byte_offset, got {cursor_after_first}",
        )
        offset = cursor_after_first["byte_offset"]
        _assert(isinstance(offset, int) and offset == history.stat().st_size,
                f"cursor offset {offset} should equal file size {history.stat().st_size}")

        # Pass 2: re-scan with advanced cursor — must yield nothing.
        sessions_second = list(plugin.discover_sessions(root_uri, cursor_after_first))
        _assert(
            len(sessions_second) == 0,
            f"pass2 (no new bytes) should yield nothing; got {len(sessions_second)} sessions",
        )

        # Pass 3: append one new line, re-scan from cursor — must yield the
        # new session only.
        _write_lines(
            history,
            [{"display": "c1", "timestamp": 1_700_000_003_000, "sessionId": "uuid-c", "project": "/p"}],
        )
        sessions_third = list(plugin.discover_sessions(root_uri, cursor_after_first))
        _assert(
            [s.external_session_id for s in sessions_third] == ["uuid-c"],
            f"pass3 should yield only uuid-c, got {[s.external_session_id for s in sessions_third]}",
        )
        events_c = list(plugin.read_events(root_uri, sessions_third[0], None))
        _assert(len(events_c) == 1, f"expected 1 event for uuid-c, got {len(events_c)}")
        _assert(events_c[0].payload["display"] == "c1", events_c[0].payload["display"])

    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
