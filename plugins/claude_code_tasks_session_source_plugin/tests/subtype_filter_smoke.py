"""M10 §1.3 — JSONB subtype-filter smoke.

The whole point of the M6 hybrid-extractor subtype-lift pattern is that
downstream SQL can filter `content_json::jsonb->>'subtype' = 'task_state'`
to find M10 events without re-parsing text. This smoke verifies the
NormalizedSessionEvent's content_json has the exact shape the SQL filter
expects, by checking via a Python-side json.dumps round-trip that mimics
the repository's serialization seam.
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
    str(_REPO_ROOT / "plugins" / "claude_code_tasks_session_source_plugin" / "src"),
)

from ananta.llm.session_ledger.types import EventType  # noqa: E402
from ananta.llm.session_ledger.vendor.claude_code_tasks import (  # noqa: E402
    SUBTYPE_TASK_STATE,
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


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subdir = root / "uuid-a"
        subdir.mkdir()
        (subdir / "1.json").write_text(
            json.dumps(
                {
                    "id": "task-001",
                    "subject": "Run subtype filter smoke",
                    "description": "Verify content_json shape for JSONB filtering",
                    "activeForm": "Verifying",
                    "status": "completed",
                    "blocks": [],
                    "blockedBy": ["task-000"],
                }
            )
        )

        plugin = ClaudeCodeTasksSessionSourcePlugin()
        root_uri = str(root)
        plugin.config_provider = _StubProvider({"glob": "*.json"})

        sessions = list(plugin.discover_sessions(root_uri, None))
        events = list(plugin.read_events(root_uri, sessions[0], None))
        normalized = plugin.normalize(events[0])

        # The repository serializes content_json via json.dumps(sort_keys=True).
        # Round-trip through that lens to mimic the storage seam.
        _assert(normalized.content_json is not None, "content_json must not be None")
        roundtripped = json.loads(json.dumps(normalized.content_json, sort_keys=True))

        # `subtype` is the load-bearing field for the SQL filter.
        _assert(
            roundtripped["subtype"] == SUBTYPE_TASK_STATE,
            f"subtype mismatch: {roundtripped['subtype']!r}",
        )
        _assert(
            roundtripped["subtype"] == "task_state",
            f"canonical literal mismatch: {roundtripped['subtype']!r}",
        )
        # Verify the other queryable axes survive serialization.
        _assert(roundtripped["id"] == "task-001", roundtripped["id"])
        _assert(roundtripped["status"] == "completed", roundtripped["status"])
        _assert(roundtripped["blockedBy"] == ["task-000"], str(roundtripped["blockedBy"]))
        # event_type is SYSTEM so the repository routes through _validate_system_event
        _assert(
            normalized.event_type is EventType.SYSTEM,
            f"event_type must be SYSTEM, got {normalized.event_type}",
        )

    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
