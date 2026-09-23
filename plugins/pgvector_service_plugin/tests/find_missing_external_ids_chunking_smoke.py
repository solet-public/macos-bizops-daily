#!/usr/bin/env python3
"""Smoke test: `find_missing_external_ids` chunks its membership read (no pytest).

D9, proven live 2026-08-15. The membership read filters
`external_id = ANY(candidates)`, so its result size is bounded by the CALLER's
list length — and the caller is the orphan reconcile, which passes every active
memory id (42,500 on the deployment where this fired). One read of that shape
exceeds the `read_state` row cap, is refused, and killed a green colour's boot
at `reindex_orphaned_memories` — the second boot failure of that deploy.

The fix chunks the candidate list so each read is bounded BY CONSTRUCTION: a
chunk can match at most its own length, so no `unbounded` opt-in is needed and
the bound holds no matter how large the caller's list grows.

Verified here:

1. A candidate list well over the cap issues MULTIPLE reads, and EVERY read
   stays within the cap — the property the boot failure violated.
2. Each read carries an explicit `limit` equal to its own chunk length.
3. The missing set is computed correctly ACROSS chunk boundaries (a chunking
   bug that dropped or duplicated a chunk would corrupt this), and input order
   is preserved.
4. A short list still issues exactly one read — chunking adds no round trips
   to the common case.
5. A non-completed read still raises rather than being swallowed.

PURE UNIT: a spy state_service records every query. No DB, no pool, no network.

Run:
    .venv/bin/python3 plugins/pgvector_service_plugin/tests/find_missing_external_ids_chunking_smoke.py

Project policy: no pytest. Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "pgvector_service_plugin" / "src"))

from ananta.services.state_service.read_bounds import MAX_READ_ROWS  # noqa: E402
from pgvector_service_plugin.postgres_backend.vector.provider import (  # noqa: E402
    PGVectorProvider,
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


class _SpyStateService:
    """Records every read_state query; reports a chosen subset as present."""

    def __init__(self, present_ids: set[str]) -> None:
        self.present_ids = present_ids
        self.queries: list[dict[str, Any]] = []

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        self.queries.append(query)
        requested = query.get("filters", {}).get("external_id", [])
        records = [{"external_id": eid} for eid in requested if eid in self.present_ids]
        return {"action_status": "completed", "data": {"records": records}}


class _ErrorStateService:
    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        return {
            "action_status": "error",
            "error": {"code": "query.unbounded_read_over_cap", "message": "refused"},
        }


def _provider(state_service: Any) -> PGVectorProvider:
    """A provider with its state_service bound and no pool ever built.

    `find_missing_external_ids` goes through `read_state` exclusively, so the
    connection pool must never be touched — building one here would make the
    smoke depend on a live database and quietly stop being a unit test.
    """
    provider = PGVectorProvider.__new__(PGVectorProvider)
    provider._state_service = state_service  # pyright: ignore[reportPrivateUsage]
    return provider


def _case_over_cap_list_is_chunked() -> None:
    """Cases 1-3: many bounded reads, every candidate read once, correct union."""
    total = MAX_READ_ROWS * 2 + 137  # spans three chunks, last one partial
    candidates = [f"mem-{i}" for i in range(total)]
    # Present: every third id, spread across ALL chunks so a dropped chunk
    # changes the answer rather than going unnoticed.
    present = {f"mem-{i}" for i in range(0, total, 3)}
    spy = _SpyStateService(present)

    result = _provider(spy).find_missing_external_ids(
        namespace="actr_memory_plugin", candidate_external_ids=candidates
    )

    _check(len(spy.queries) > 1, f"an over-cap list issues MULTIPLE reads ({len(spy.queries)})")
    sizes = [len(q["filters"]["external_id"]) for q in spy.queries]
    _check(
        all(size <= MAX_READ_ROWS for size in sizes),
        f"EVERY read stays within the {MAX_READ_ROWS}-row cap (max chunk {max(sizes)})",
    )
    _check(sum(sizes) == total, "every candidate is read exactly once (no chunk dropped)")
    _check(
        all(q.get("limit") == len(q["filters"]["external_id"]) for q in spy.queries),
        "each read declares an explicit limit equal to its chunk length",
    )

    expected_missing = [eid for eid in candidates if eid not in present]
    _check(
        result["missing"] == expected_missing,
        "the missing set is correct ACROSS chunk boundaries, in input order",
    )


def _case_small_inputs_are_not_made_worse() -> None:
    """Case 4: chunking adds no round trips to the common case."""
    spy_small = _SpyStateService(set())
    _provider(spy_small).find_missing_external_ids(
        namespace="actr_memory_plugin", candidate_external_ids=["a", "b", "c"]
    )
    _check(len(spy_small.queries) == 1, "a short list still issues exactly ONE read")

    spy_empty = _SpyStateService(set())
    empty = _provider(spy_empty).find_missing_external_ids(
        namespace="actr_memory_plugin", candidate_external_ids=[]
    )
    _check(
        empty["missing"] == [] and not spy_empty.queries,
        "an empty candidate list short-circuits with no read at all",
    )


def _case_refused_read_raises() -> None:
    """Case 5: a refused read is never swallowed into "missing"."""
    label = "a non-completed read RAISES (never swallowed as 'all present')"
    try:
        _provider(_ErrorStateService()).find_missing_external_ids(
            namespace="actr_memory_plugin", candidate_external_ids=["a"]
        )
    except RuntimeError as exc:
        _check("StateService read failed" in str(exc), label)
    else:
        _check(False, label)


def main() -> int:
    print("=== find_missing_external_ids_chunking_smoke ===")

    _case_over_cap_list_is_chunked()
    _case_small_inputs_are_not_made_worse()
    _case_refused_read_raises()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
