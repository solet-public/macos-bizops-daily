#!/usr/bin/env python3
"""Unit smoke for ``session_claude_mapping_store.py`` (T1 usage-capture
lane, the 2026-08-05 usage-capture ruling), against
``RealShapeState`` (real provider ActionResult envelopes).

Proves the idempotent-upsert contract the ruling requires: re-ingesting the
SAME (agent_instance_id, claude_session_id, captured_at) triple — exactly
what happens if the ingestion verb crashes after a durable write but before
deleting the spool file, and re-processes the same file next run — updates
the existing row rather than duplicating it; a genuinely different firing
(different captured_at) inserts a new row alongside it (the ONE-TO-MANY
rotation history, not a single mutable slot).

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/session_claude_mapping_store_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402

from agent_messaging_plugin.schema import (  # noqa: E402
    CAPTURE_SOURCE_HOOK_CLEAR,
    CAPTURE_SOURCE_HOOK_STARTUP,
    CAPTURE_SOURCE_INIT_EVENT,
)
from agent_messaging_plugin.session_claude_mapping_store import (  # noqa: E402
    list_session_claude_mappings,
    upsert_session_claude_mapping,
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


def test_fresh_upsert_round_trips() -> None:
    state = RealShapeState()
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-smoke-1",
        claude_session_id="cs-aaa",
        captured_at="2026-08-05T16:00:00+00:00",
        capture_source=CAPTURE_SOURCE_HOOK_STARTUP,
    )
    rows = list_session_claude_mappings(state, "agi-smoke-1")
    _check(len(rows) == 1, "a fresh upsert round-trips as exactly one row")
    _check(
        rows and rows[0]["claude_session_id"] == "cs-aaa",
        "the round-tripped row carries the claude_session_id verbatim",
    )
    _check(
        rows and rows[0]["capture_source"] == CAPTURE_SOURCE_HOOK_STARTUP,
        "the round-tripped row carries capture_source verbatim",
    )


def test_reingest_same_triple_updates_not_duplicates() -> None:
    """The crash-before-delete case: re-ingesting the same spool file (same
    agent_instance_id/claude_session_id/captured_at) must UPDATE the
    existing row, never insert a second one."""
    state = RealShapeState()
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-smoke-2",
        claude_session_id="cs-bbb",
        captured_at="2026-08-05T16:05:00+00:00",
        capture_source=CAPTURE_SOURCE_HOOK_STARTUP,
    )
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-smoke-2",
        claude_session_id="cs-bbb",
        captured_at="2026-08-05T16:05:00+00:00",
        capture_source=CAPTURE_SOURCE_INIT_EVENT,  # simulates a re-ingest with a different source
    )
    rows = list_session_claude_mappings(state, "agi-smoke-2")
    _check(
        len(rows) == 1,
        "re-ingesting the identical (instance, session, captured_at) triple "
        "stays exactly one row -- idempotent, never duplicated",
    )
    _check(
        rows and rows[0]["capture_source"] == CAPTURE_SOURCE_INIT_EVENT,
        "the re-ingest UPDATED the existing row's capture_source (real upsert, not a no-op)",
    )


def test_different_firing_inserts_a_second_row() -> None:
    """A genuinely different firing (rotation: /clear mints a new
    claude_session_id, or simply a later captured_at) is a SEPARATE row --
    the ONE-TO-MANY rotation history, never a single mutable slot."""
    state = RealShapeState()
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-smoke-3",
        claude_session_id="cs-ccc",
        captured_at="2026-08-05T16:10:00+00:00",
        capture_source=CAPTURE_SOURCE_HOOK_STARTUP,
    )
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-smoke-3",
        claude_session_id="cs-ddd",  # rotated after a /clear
        captured_at="2026-08-05T16:20:00+00:00",
        capture_source=CAPTURE_SOURCE_HOOK_CLEAR,
    )
    rows = list_session_claude_mappings(state, "agi-smoke-3")
    _check(
        len(rows) == 2,
        "a rotated claude_session_id (post-/clear) is a SECOND row, not an "
        "overwrite -- the ONE-TO-MANY rotation history survives",
    )
    sources = {r["capture_source"] for r in rows}
    _check(
        sources == {CAPTURE_SOURCE_HOOK_STARTUP, CAPTURE_SOURCE_HOOK_CLEAR},
        "both firings' capture_source values are preserved, distinctly",
    )


def test_list_filters_by_instance() -> None:
    state = RealShapeState()
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-smoke-4a", claude_session_id="cs-a",
        captured_at="2026-08-05T16:30:00+00:00", capture_source=CAPTURE_SOURCE_HOOK_STARTUP,
    )
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-smoke-4b", claude_session_id="cs-b",
        captured_at="2026-08-05T16:31:00+00:00", capture_source=CAPTURE_SOURCE_HOOK_STARTUP,
    )
    rows = list_session_claude_mappings(state, "agi-smoke-4a")
    _check(
        len(rows) == 1 and rows[0]["agent_instance_id"] == "agi-smoke-4a",
        "list_session_claude_mappings returns only the named instance's rows, "
        "never a sibling worker's",
    )


def main() -> int:
    test_fresh_upsert_round_trips()
    test_reingest_same_triple_updates_not_duplicates()
    test_different_firing_inserts_a_second_row()
    test_list_filters_by_instance()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
