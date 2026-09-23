#!/usr/bin/env python3
"""Unit smoke: memory_store reads are loud on error and opted into the row cap.

2026-08-15 deploy regression, caught live: the first green boot on the
read_state row cap (D1) died at identity verification. ``get_all_memories``
reads the ENTIRE ``memory`` table and filters by tag client-side (``tags`` is
a JSON-string column outside the equality filter grammar); the live memory
table exceeds the 10,000-row cap because knowledge-base articles are memories,
so the capped read refused — and the bare ``.get("data", {}).get("records",
[])`` chain swallowed that refusal into an empty list. Identity verification
then reported "no memories found" where the truth was "the read was refused".

Two properties pinned here, each mutation-checked at authoring time:

1. LOUD, NEVER EMPTY — an error-shaped ``read_state`` result raises
   ``FrameworkError`` from every memory_store read helper; it never renders as
   an empty result, because an error rendered empty is indistinguishable from
   data loss.
2. CONSENTED WHOLE-TABLE READS — the two deliberate whole-table readers
   (``get_all_memories``, ``get_all_memorizations``) pass ``unbounded=True``,
   the row-cap opt-in; the id-filtered single-row readers do NOT, so they keep
   the capped default.

PURE UNIT (no DB): a spy state service captures queries and returns canned
ActionResult shapes, including the exact ``query.unbounded_read_over_cap``
refusal shape the live incident produced.

Run::

    .venv/bin/python3 plugins/actr_memory_plugin/tests/actr_memory_read_discipline_smoke.py
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
# ruff: noqa: E402
from actr_memory_plugin import memory_store
from ananta.error_handling import FrameworkError
from ananta.services.state_service.read_bounds import MAX_READ_ROWS, resolve_read_limit

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


class _SpyStateService:
    """Returns a canned result and records every read_state query."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.queries: list[dict[str, Any]] = []

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        self.queries.append(query)
        return self.result


def _completed(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {"action_status": "completed", "data": {"records": records}}


# The exact refusal shape the postgres provider produced live on 2026-08-15.
_OVER_CAP_REFUSAL: dict[str, Any] = {
    "action_status": "error",
    "error": {
        "type": "plugin_error",
        "code": "query.unbounded_read_over_cap",
        "message": "read_state on table 'memory' exceeded the row cap",
    },
}


def main() -> int:
    print("=== actr_memory_read_discipline_smoke ===")

    # 1. The incident shape: an over-cap refusal must RAISE, never read as empty.
    spy = _SpyStateService(_OVER_CAP_REFUSAL)
    try:
        memory_store.get_all_memories(spy, tag="identity")
        _check(False, "over-cap refusal raises from get_all_memories (never an empty list)")
    except FrameworkError as exc:
        _check(
            "unbounded_read_over_cap" in str(exc.message),
            "over-cap refusal raises from get_all_memories (never an empty list)",
        )

    # 2. The whole-table readers consent to the cap with unbounded=True.
    spy = _SpyStateService(_completed([]))
    memory_store.get_all_memories(spy, status="active")
    _check(
        spy.queries[-1].get("unbounded") is True,
        "get_all_memories opts into the row cap (unbounded=True)",
    )
    memory_store.get_all_memories(spy)
    _check(
        spy.queries[-1].get("unbounded") is True,
        "get_all_memories opts in on the no-filter path too",
    )
    memory_store.get_all_memorizations(spy)
    _check(
        spy.queries[-1].get("unbounded") is True,
        "get_all_memorizations opts into the row cap (unbounded=True)",
    )

    # 3. Id-filtered single-row readers keep the capped default.
    spy = _SpyStateService(_completed([]))
    memory_store.get_memory(spy, "mem-1")
    _check(
        "unbounded" not in spy.queries[-1],
        "get_memory stays under the capped default (id-filtered)",
    )
    memory_store.get_memorization(spy, "mem-1")
    _check(
        "unbounded" not in spy.queries[-1],
        "get_memorization stays under the capped default (id-filtered)",
    )

    # 4. Errors raise from the single-row readers as well — an error is not
    #    "the memory does not exist".
    spy = _SpyStateService(_OVER_CAP_REFUSAL)
    try:
        memory_store.get_memory(spy, "mem-1")
        _check(False, "get_memory raises on an error result (error is not 'absent')")
    except FrameworkError:
        _check(True, "get_memory raises on an error result (error is not 'absent')")

    # 5. THE SEAM — the flag must be HONOURED, not merely passed. A knob the
    #    provider ignores is inert, and a smoke that only asserts memory_store
    #    sends `unbounded` would go green against a provider that caps anyway.
    #    So exercise the real resolver both ways, and the provider's own
    #    `fetch_limit or None` translation of its result.
    fetch_limit, overflow_is_error = resolve_read_limit(None, unbounded=True, table="memory")
    _check(
        (fetch_limit or None) is None and overflow_is_error is False,
        "resolve_read_limit(unbounded=True) yields a genuinely uncapped fetch",
    )
    capped_limit, capped_overflow = resolve_read_limit(None, unbounded=False, table="memory")
    _check(
        capped_limit == MAX_READ_ROWS + 1 and capped_overflow is True,
        "resolve_read_limit(unbounded=False) still fetches cap+1 to detect overflow",
    )
    # The live memory table measured 249,525 rows (42,500 active) on
    # 2026-08-15 — the whole-table readers are ~4x over the cap on the active
    # filter alone, so this opt-in is load-bearing, not theoretical.
    _check(
        MAX_READ_ROWS < 42_500,
        "the cap really is below the measured live active-memory row count",
    )

    # 6. Healthy path: tag filtering still works client-side on a completed read.
    rows = [
        {"id": "m1", "tags": '["identity"]', "content": "who I am"},
        {"id": "m2", "tags": '["other"]', "content": "unrelated"},
    ]
    spy = _SpyStateService(_completed(rows))
    got = memory_store.get_all_memories(spy, tag="identity")
    _check(
        [m["id"] for m in got] == ["m1"],
        "tag filter still selects exactly the tagged memory on a completed read",
    )

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
