#!/usr/bin/env python3
"""Regression tests for address-book idempotent-upsert + hard-delete (2026-07-17).

Locks the redesign that replaced create-only register + soft delete:

* register on a NEW name -> INSERT path (address row + entry rows written; no delete).
* register on an EXISTING (live) name -> REPLACE-in-place path: old memory
  archived, entries hard-swapped (delete_records soft_delete=False), row
  metadata + memory_id relinked via update_state, SAME surrogate id kept,
  NO second address INSERT. (Idempotent write — no name_exists error.)
* delete -> HARD delete: delete_records with soft_delete=False on BOTH
  address_entry and address (no tombstone left to occupy the name / orphan
  entries), old memory archived.

Fakes are in-memory; address_ops is pure-functional over the injected
state/memory services (no DB / filesystem side effects).

Run:
    .venv/bin/python3 plugins/default_address_book_plugin/tests/test_upsert_and_hard_delete.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1] / "src"))
sys.path.insert(0, str(_HERE.parents[3] / "ananta" / "src"))

from default_address_book_plugin.address_ops import (  # noqa: E402
    delete_impl,
    register_impl,
)

_LOGGER = logging.getLogger("test_upsert_and_hard_delete")
_ENTRIES = [
    {"field_type": "city", "description": "Municipality", "value": "Langford"},
]

_passed = 0
_failed: list[str] = []


def _check(cond: object, label: str) -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeState:
    """Records write/delete/update calls; read_state returns a preset row set."""

    def __init__(self, existing_row: dict[str, Any] | None) -> None:
        self._existing = existing_row
        self.writes: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        rows = [self._existing] if self._existing is not None else []
        return {"data": {"records": rows}}

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        self.writes.append(data)
        return {"data": {"result": {"generated_id": "adr-new-inserted"}}}

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        self.deletes.append(query)
        return {"data": {"result": {"deleted": 1}}}

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        self.updates.append({"query": query, "updates": updates})
        return {"data": {"result": {"updated": 1}}}


class _FakeMemory:
    def __init__(self) -> None:
        self.forgotten: list[str] = []

    def remember(self, content: str, tags: list[str]) -> dict[str, Any]:
        return {"memory_id": "mem-new", "message": "ok"}

    def forget(self, memory_id: str) -> dict[str, Any]:
        self.forgotten.append(memory_id)
        return {"data": {}}


def _writes_to(state: _FakeState, table: str) -> list[dict[str, Any]]:
    return [w for w in state.writes if w.get("table") == table]


def _deletes_to(state: _FakeState, table: str) -> list[dict[str, Any]]:
    return [d for d in state.deletes if d.get("table") == table]


def register_new_inserts() -> None:
    print("register_new_inserts:")
    state: Any = _FakeState(existing_row=None)
    mem = _FakeMemory()
    res = register_impl(
        state, "default_address_book_plugin", mem, True, _LOGGER,
        "op_home", "postal", "desc", _ENTRIES, ["operator"],
    )
    _check(res.get("data", {}).get("address_id") == "adr-new-inserted", "new name -> INSERT returns generated id")
    _check(res.get("data", {}).get("memory_id") == "mem-new", "new name -> memory_id linked")
    _check(len(_writes_to(state, "address")) == 1, "new name -> exactly one address row written")
    _check(len(_writes_to(state, "address_entry")) == 1, "new name -> entry rows written")
    _check(len(state.deletes) == 0, "new name -> no deletes")


def register_existing_replaces_in_place() -> None:
    print("register_existing_replaces_in_place:")
    existing = {"id": "adr-existing", "name": "op_home", "memory_id": "mem-old"}
    state: Any = _FakeState(existing_row=existing)
    mem = _FakeMemory()
    res = register_impl(
        state, "default_address_book_plugin", mem, True, _LOGGER,
        "op_home", "postal", "desc2", _ENTRIES, ["operator"],
    )
    _check(res.get("data", {}).get("address_id") == "adr-existing", "existing name -> SAME surrogate id kept")
    _check(res.get("data", {}).get("memory_id") == "mem-new", "existing name -> memory re-ingested + relinked")
    _check("mem-old" in mem.forgotten, "existing name -> old memory archived")
    _check(len(_writes_to(state, "address")) == 0, "existing name -> NO second address INSERT")
    entry_deletes = _deletes_to(state, "address_entry")
    _check(len(entry_deletes) == 1, "existing name -> old entries deleted")
    _check(entry_deletes and entry_deletes[0].get("soft_delete") is False, "existing name -> entry delete is HARD (soft_delete=False)")
    _check(len(_writes_to(state, "address_entry")) == 1, "existing name -> new entries inserted")
    _check(
        any(u["updates"].get("memory_id") == "mem-new" for u in state.updates),
        "existing name -> update_state relinks new memory_id",
    )


def delete_is_hard() -> None:
    print("delete_is_hard:")
    existing = {"id": "adr-del", "name": "op_home", "memory_id": "mem-del"}
    state: Any = _FakeState(existing_row=existing)
    mem = _FakeMemory()
    res = delete_impl(state, "default_address_book_plugin", mem, _LOGGER, "op_home")
    _check(res.get("data", {}).get("message") == "Address deleted", "delete returns deleted message")
    _check("mem-del" in mem.forgotten, "delete archives the memory")
    addr_deletes = _deletes_to(state, "address")
    entry_deletes = _deletes_to(state, "address_entry")
    _check(len(addr_deletes) == 1 and addr_deletes[0].get("soft_delete") is False, "delete address row is HARD")
    _check(len(entry_deletes) == 1 and entry_deletes[0].get("soft_delete") is False, "delete address_entry rows is HARD")


def main() -> int:
    register_new_inserts()
    register_existing_replaces_in_place()
    delete_is_hard()
    print()
    print(f"  passed: {_passed}")
    print(f"  failed: {len(_failed)}")
    for label in _failed:
        print(f"    - {label}")
    return 0 if not _failed else 1


if __name__ == "__main__":
    sys.exit(main())
