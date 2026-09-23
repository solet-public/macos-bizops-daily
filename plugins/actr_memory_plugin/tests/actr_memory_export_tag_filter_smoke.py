#!/usr/bin/env python3
"""Unit smoke: export_memories' tags filter uses ALL semantics + isolates origins.

Unified-memory-passthrough Slice 1(b). ``export_memories(tags=...)`` filters with
ALL semantics (a record must carry EVERY listed tag, matching recall's documented
tag filter), so a per-origin hydrate export cannot leak another agent's projection.
The load-bearing case: two origins seeded, ``tags=["agent_memory",
"agent_memory:origin:<X>"]`` exports ONLY origin X.

PURE UNIT (no DB): exercises the real ``filter_memories_by_all_tags`` helper that
``backend.export_memories`` applies before writing the snapshot file.

Run::

    .venv/bin/python3 plugins/actr_memory_plugin/tests/actr_memory_export_tag_filter_smoke.py
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))

importlib.import_module("ananta.core.config.config_manager")
from actr_memory_plugin.record_helpers import filter_memories_by_all_tags  # noqa: E402

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


_ORIGIN_A = "agent_memory:origin:claude_code.a"
_ORIGIN_B = "agent_memory:origin:codex"


def _fixtures() -> list[dict[str, Any]]:
    return [
        {"id": "a1", "tags": ["agent_memory", _ORIGIN_A, "agent_memory:slot:a:one"]},
        {"id": "a2", "tags": ["agent_memory", _ORIGIN_A, "agent_memory:slot:a:two"]},
        {"id": "b1", "tags": ["agent_memory", _ORIGIN_B, "agent_memory:slot:b:one"]},
        {"id": "plain", "tags": ["conversation"]},
        {"id": "notags", "tags": []},
    ]


def _ids(memories: list[dict[str, Any]]) -> set[str]:
    return {m["id"] for m in memories}


def test_all_semantics_and_origin_isolation() -> None:
    memories = _fixtures()

    umbrella_only = _ids(filter_memories_by_all_tags(memories, ["agent_memory"]))
    _check(
        umbrella_only == {"a1", "a2", "b1"},
        f"umbrella tag returns all agent_memory records across origins (got {sorted(umbrella_only)})",
    )

    origin_a = _ids(filter_memories_by_all_tags(memories, ["agent_memory", _ORIGIN_A]))
    _check(
        origin_a == {"a1", "a2"},
        f"ALL filter [agent_memory, origin:A] returns ONLY origin A (got {sorted(origin_a)}) — the leak guard",
    )

    origin_b = _ids(filter_memories_by_all_tags(memories, ["agent_memory", _ORIGIN_B]))
    _check(
        origin_b == {"b1"},
        f"origin B isolated from origin A (got {sorted(origin_b)})",
    )

    none_all = _ids(filter_memories_by_all_tags(memories, None))
    _check(none_all == _ids(memories), "tags=None returns every record unchanged")

    empty_all = _ids(filter_memories_by_all_tags(memories, []))
    _check(empty_all == _ids(memories), "tags=[] returns every record unchanged")

    absent = filter_memories_by_all_tags(memories, ["agent_memory", "agent_memory:origin:nobody"])
    _check(absent == [], "a tag no record fully carries returns empty (not a partial match)")

    # Subset must NOT satisfy an ALL filter: a record with the umbrella but not the
    # origin is excluded when both are required.
    subset = _ids(filter_memories_by_all_tags(
        [{"id": "u", "tags": ["agent_memory"]}], ["agent_memory", _ORIGIN_A]
    ))
    _check(subset == set(), "umbrella-only record excluded when origin tag also required (ALL, not ANY)")


def main() -> int:
    print("=== actr_memory_export_tag_filter_smoke ===")
    test_all_semantics_and_origin_isolation()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
