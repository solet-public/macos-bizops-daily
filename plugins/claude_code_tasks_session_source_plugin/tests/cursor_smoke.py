"""M10 §1.3 — discovery + per-session cursor advance + idempotent re-read.

Three-phase fixture per [[sandbox-mutating-smokes]]:
1. Cold start (cursor=None): both sessions discovered, all task files emitted.
2. Re-poll with advanced mtime cursor: no new sessions emitted (idempotent).
3. Add a new task file: that session re-emerges; task_index cursor resumes
   past the previously drained tasks; emits only the new file.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(_REPO_ROOT / "plugins" / "claude_code_tasks_session_source_plugin" / "src"),
)

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


def _make_task(path: Path, task_id: str, mtime: float | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "id": task_id,
                "subject": f"subj {task_id}",
                "description": f"desc {task_id}",
                "activeForm": f"doing {task_id}",
                "status": "pending",
                "blocks": [],
                "blockedBy": [],
            }
        )
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sess_a = root / "uuid-a"
        sess_a.mkdir()
        _make_task(sess_a / "1.json", "ta1", mtime=1_700_000_000.0)
        _make_task(sess_a / "2.json", "ta2", mtime=1_700_000_010.0)
        sess_b = root / "uuid-b"
        sess_b.mkdir()
        _make_task(sess_b / "1.json", "tb1", mtime=1_700_000_020.0)
        # Drop in noise files that *.json glob must skip
        (sess_a / ".highwatermark").write_text("noise")
        (sess_a / ".lock").write_text("noise")

        plugin = ClaudeCodeTasksSessionSourcePlugin()
        root_uri = str(root)
        plugin.config_provider = _StubProvider({"glob": "*.json"})

        # Pass 1: cold start
        sessions = list(plugin.discover_sessions(root_uri, None))
        ids = {s.external_session_id for s in sessions}
        _assert(ids == {"uuid-a", "uuid-b"}, f"pass1 sessions: {ids}")
        last_ref = None
        for ref in sessions:
            events = list(plugin.read_events(root_uri, ref, None))
            expected = 2 if ref.external_session_id == "uuid-a" else 1
            _assert(
                len(events) == expected,
                f"{ref.external_session_id} pass1 events: got {len(events)} want {expected}",
            )
            last_ref = ref

        cursor = plugin.session_discovery_cursor(root_uri, last_ref)
        _assert(
            "mtime_high_water_iso" in cursor,
            f"discovery cursor missing mtime field: {cursor}",
        )

        # The per-session cursor depends on which subdir is "last seen". Build
        # one per session manually using the last raw event.
        per_session_cursors: dict[str, dict[str, object]] = {}
        for ref in sessions:
            events = list(plugin.read_events(root_uri, ref, None))
            per_session_cursors[ref.external_session_id] = plugin.event_read_cursor(root_uri,
                ref, events[-1] if events else None
            )

        # Pass 2: cold-start the discovery cursor to the MAX mtime across both subdirs,
        # then verify no sessions are yielded.
        from datetime import UTC, datetime

        max_mtime = datetime.fromtimestamp(1_700_000_020.0, tz=UTC).isoformat()
        sessions2 = list(plugin.discover_sessions(root_uri, {"mtime_high_water_iso": max_mtime}))
        _assert(
            len(sessions2) == 0,
            f"pass2 (cursor at max mtime) should yield nothing; got {len(sessions2)}",
        )

        # Pass 3: drop a new task file into uuid-a with newer mtime → resurface
        # uuid-a only; read_events with task_index=2 cursor emits ONLY the new file
        time.sleep(0.01)
        _make_task(sess_a / "3.json", "ta3", mtime=1_700_000_100.0)
        sessions3 = list(plugin.discover_sessions(root_uri, {"mtime_high_water_iso": max_mtime}))
        _assert(
            [s.external_session_id for s in sessions3] == ["uuid-a"],
            f"pass3 should yield only uuid-a; got {[s.external_session_id for s in sessions3]}",
        )
        events3 = list(plugin.read_events(root_uri, sessions3[0], per_session_cursors["uuid-a"]))
        _assert(
            len(events3) == 1,
            f"pass3 task_index cursor should skip already-drained; got {len(events3)} events",
        )
        _assert(events3[0].payload["task_id"] == "ta3", events3[0].payload["task_id"])

    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
