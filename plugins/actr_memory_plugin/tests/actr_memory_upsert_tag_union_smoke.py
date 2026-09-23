#!/usr/bin/env python3
"""Unit smoke: upsert_memory_by_tag unions provenance tags + reconciles slot dups.

Unified-memory-passthrough Slice 1(c). The slot ``tag`` stays the replace key,
and an optional ``tags`` list is unioned onto it (slot first, deduped) so the
umbrella/origin tags land literally on the stored record — without which the
consolidation/purge protection (which is exact-membership) would never apply.
Duplicate-slot reconciliation falls out of the store-new-then-delete-old ordering:
ALL prior records on the slot are deleted, so a slot that accumulated duplicates
(a prior failed old-vector delete) collapses to the single new record — newest
wins, stale repaired and reported as ``duplicates_reconciled``.

Exercises the REAL ``backend.upsert_memory_by_tag``. ``remember`` is overridden to
capture the tags it receives (avoids faking the embedding pipeline); the fake
state/vector services record deletes. No DB, no live dispatch.

Run::

    .venv/bin/python3 plugins/actr_memory_plugin/tests/actr_memory_upsert_tag_union_smoke.py
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
from actr_memory_plugin.backend import ACTRMemoryBackend  # noqa: E402

_passed = 0
_failed: list[str] = []

_SLOT = "agent_memory:slot:claude_code.a:feedback_keypairs"
_UMBRELLA = "agent_memory"
_ORIGIN = "agent_memory:origin:claude_code.a"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeVector:
    def delete_by_external_ids(self, namespace: str, external_ids: list[str]) -> dict[str, Any]:
        del namespace, external_ids
        return {"action_status": "completed"}


class _FakeState:
    """Returns seeded slot rows for find_memories_by_tag; records deletes."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = [dict(r) for r in rows]
        self.deleted_ids: list[str] = []

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace, query
        # `action_status` is part of the real read envelope and this fake's own
        # `delete_records` below already returns it; the read half omitting it
        # was an unfaithful fake, surfaced when memory_store began requiring a
        # COMPLETED status so an error can never read as an empty result.
        return {
            "action_status": "completed",
            "data": {"records": [dict(r) for r in self._rows]},
        }

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        mid = query.get("filters", {}).get("id")
        if mid is not None:
            self.deleted_ids.append(str(mid))
        return {"action_status": "completed"}


def _make_backend(prior_ids: list[str]) -> tuple[ACTRMemoryBackend, dict[str, Any], _FakeState]:
    rows = [{"id": pid, "tags": [_SLOT]} for pid in prior_ids]
    fake_state = _FakeState(rows)
    backend = object.__new__(ACTRMemoryBackend)
    backend.state_service = fake_state
    backend.vector_service = _FakeVector()

    captured: dict[str, Any] = {}

    def _fake_remember(
        content: str,
        tags: list[str] | None = None,
        source_file: str | None = None,
        session_id: str | None = None,
        embed: bool = True,
    ) -> dict[str, Any]:
        del source_file, embed
        captured["content"] = content
        captured["tags"] = list(tags) if tags else []
        captured["session_id"] = session_id
        return {"memory_id": "new-record-1"}

    backend.remember = _fake_remember  # type: ignore[method-assign]
    return backend, captured, fake_state


def test_tag_union_slot_first_deduped() -> None:
    backend, captured, _ = _make_backend(prior_ids=[])
    result = backend.upsert_memory_by_tag(
        content="canonical text", tag=_SLOT, tags=[_UMBRELLA, _ORIGIN]
    )

    _check(
        captured["tags"] == [_SLOT, _UMBRELLA, _ORIGIN],
        f"record carries [slot, umbrella, origin] with slot first (got {captured['tags']})",
    )
    _check(result["data"]["memory_id"] == "new-record-1", "returns the new memory id")


def test_tag_union_dedupes_slot_when_passed_again() -> None:
    backend, captured, _ = _make_backend(prior_ids=[])
    backend.upsert_memory_by_tag(
        content="x", tag=_SLOT, tags=[_UMBRELLA, _SLOT, _ORIGIN, _UMBRELLA]
    )
    _check(
        captured["tags"] == [_SLOT, _UMBRELLA, _ORIGIN],
        f"duplicate slot/umbrella tags are collapsed, order preserved (got {captured['tags']})",
    )


def test_no_tags_stores_slot_only() -> None:
    backend, captured, _ = _make_backend(prior_ids=[])
    backend.upsert_memory_by_tag(content="x", tag=_SLOT)
    _check(captured["tags"] == [_SLOT], f"tags=None stores just the slot tag (got {captured['tags']})")


def test_duplicate_slot_reconciled() -> None:
    """Two pre-existing records on the slot (a prior failed delete) collapse to the
    single new record; the extras are reported as duplicates_reconciled."""
    backend, _, fake_state = _make_backend(prior_ids=["old-1", "old-2"])
    result = backend.upsert_memory_by_tag(content="new", tag=_SLOT, tags=[_UMBRELLA])
    data = result["data"]

    _check(set(fake_state.deleted_ids) == {"old-1", "old-2"}, "both stale slot records deleted")
    _check(data["deleted_count"] == 2, f"deleted_count == 2 (got {data['deleted_count']})")
    _check(
        data["duplicates_reconciled"] == 1,
        f"duplicates_reconciled == 1 (surplus beyond the one canonical prior) (got {data['duplicates_reconciled']})",
    )


def test_healthy_slot_no_reconcile() -> None:
    backend, _, _ = _make_backend(prior_ids=["old-1"])
    data = backend.upsert_memory_by_tag(content="new", tag=_SLOT)["data"]
    _check(data["deleted_count"] == 1, f"single prior deleted (got {data['deleted_count']})")
    _check(
        data["duplicates_reconciled"] == 0,
        f"healthy slot reports no reconciliation (got {data['duplicates_reconciled']})",
    )


def main() -> int:
    print("=== actr_memory_upsert_tag_union_smoke ===")
    test_tag_union_slot_first_deduped()
    test_tag_union_dedupes_slot_when_passed_again()
    test_no_tags_stores_slot_only()
    test_duplicate_slot_reconciled()
    test_healthy_slot_no_reconcile()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
