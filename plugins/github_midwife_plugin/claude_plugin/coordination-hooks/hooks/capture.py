#!/usr/bin/env python3
"""PostToolUse capture hook for memory-passthrough (Slice 2).

Fires after Write | Edit | MultiEdit. If the touched file is a per-fact memory
markdown file under this agent's memory dir, append one capture record to the
journal (with echo-break) and exit 0. Otherwise no-op. Does ONLY this local
append — no network, no MCP, no DB (a hook subprocess has no bridge; drain is
agent-mediated, design §4.4).

Stdin payload contract (Claude Code PostToolUse):
  {
    "session_id": "...", "transcript_path": "...", "cwd": "...",
    "hook_event_name": "PostToolUse",
    "tool_name": "Write" | "Edit" | "MultiEdit",
    "tool_input": { "file_path": "<abs path>", ... },   # file_path present for all three
    "tool_response": { ... }
  }
We read only ``tool_input.file_path``. Any parse failure, missing field, or
unexpected shape exits 0 silently — capture must never disrupt the tool that
already ran.

Exit code: always 0. Errors go to the passthrough error log, never to the model.

Stdlib-only by design — fires outside the venv.
"""


from __future__ import annotations

# INTERPRETER FLOOR. These hooks are Python 3.13 source and use datetime.UTC
# (3.11+). Claude Code launches them with a bare `python3`, which resolves from
# PATH -- on a stock macOS that is frequently the system 3.9, and the resulting
# ImportError traceback surfaced to the operator as a hook error on EVERY tool
# call. Measured 2026-08-20: 8 of 20 shipped hook modules failed to import.
#
# Placed AFTER `from __future__` (which must stay the first statement) and
# BEFORE the first 3.11+ import, because an ImportError at module level cannot
# be caught by anything inside this file. Exits 0 and SILENTLY: the shipped
# contract is that a session which cannot run these hooks gets zero output and
# zero errors, and a diagnostic here would reproduce the very symptom it fixes.
# A floor, not a compatibility shim -- nothing is emulated or back-ported.
import sys

if sys.version_info < (3, 11):  # noqa: UP036 -- see above; ruff assumes
    # the project's py313 target, but this file ships to an ADOPTER's machine
    # and is launched by whatever `python3` their PATH resolves.
    raise SystemExit(0)


import json
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ruff: noqa: E402
# pyright: reportMissingImports=false
import _journal

_TRACKED_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})


def _file_path_from_stdin(raw_text: str) -> str | None:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("tool_name") not in _TRACKED_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    return file_path if isinstance(file_path, str) and file_path else None


def _log_error(detail: str) -> None:
    try:
        log = _journal.state_dir() / ".log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(UTC).isoformat()} capture error: {detail}\n")
    except Exception:  # noqa: BLE001 — telemetry is strictly best-effort.
        pass


def main() -> None:
    try:
        file_path = _file_path_from_stdin(sys.stdin.read())
        if file_path is not None:
            _journal.append_capture(file_path)
    except Exception as exc:  # noqa: BLE001 — capture MUST never disrupt the session.
        _log_error(repr(exc))
    sys.exit(0)


if __name__ == "__main__":
    main()
