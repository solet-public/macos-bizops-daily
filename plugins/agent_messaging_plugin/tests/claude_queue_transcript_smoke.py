#!/usr/bin/env python3
"""Queue-operation entries, not pane text, prove Claude Code input uptake."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
from agent_messaging_plugin.claude_queue_transcript import transcript_confirms_input  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "session.jsonl"
        path.write_text(
            '{"type":"queue-operation","operation":"enqueue","sessionId":"cc0","content":"accepted"}\n'
            '{"type":"queue-operation","operation":"remove","sessionId":"cc0","content":"accepted"}\n'
            '{"type":"assistant","content":"stranded-visible-text"}\n', encoding="utf-8",
        )
        assert transcript_confirms_input(path, session_id="cc0", text="accepted")
        assert not transcript_confirms_input(path, session_id="cc0", text="stranded-visible-text")
    print("2 passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
