#!/usr/bin/env python3
"""Unit smoke: ``knowledge:official`` chunks are EXCLUDED from consolidation.

Pins the KB-degradation fix in ``_filter_consolidation_candidates`` (backend.py).
KB chunks are stored as EPISODIC memories tagged ``knowledge:official`` (via
``memory_service.remember`` in default_knowledge_plugin/kb_indexing.py:388 —
``remember`` creates episodic memories). ``consolidate`` pulls
``memory_type="episodic", status="active"`` (backend.py:1529) and
``_filter_consolidation_candidates`` previously excluded ONLY
``CONSOLIDATION_EXCLUDED_TAGS = {tool_use, identity, conversation}`` — so aged,
decayed KB chunks (strength < -1.5 AND age > 7d) became consolidation
candidates, and ``_finalize_consolidated_memory`` then ``forget``s each source
(``status=archived`` + vector deleted), silently removing it from KB search.
The aggravator: KB search passes ``score_by_similarity=True``
(kb_search.py:215/226), which skips ``_record_retrieval_boost`` (backend.py:586),
so KB chunks are never reinforced and a never-retrieved chunk sits at strength
-10.0 from creation — crossing the 7-day age gate makes it an immediate
candidate. The fix adds ``knowledge:official`` to ``CONSOLIDATION_EXCLUDED_TAGS``.

This is a PURE UNIT (no DB): ``_filter_consolidation_candidates`` is pure
computation over memory dicts (``compute_strength`` + ``parse_created_at`` +
tag check), so the smoke calls the REAL method via ``object.__new__`` with no
backend state. A memory with no ``retrieval_times`` computes strength -10.0
(< the -1.5 threshold); ``created_at`` 30d ago clears the 7d age cutoff.

RED-GREEN: pre-fix the ``knowledge:official`` memory IS returned as a candidate
(test FAILS); post-fix it is excluded (test PASSES). The normal-weak-episodic
positive case proves the fix does NOT over-exclude (those still consolidate).

Run (no env gate — deterministic, no external deps)::

    .venv/bin/python3 plugins/actr_memory_plugin/tests/actr_memory_consolidation_kb_exclusion_smoke.py
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"),
)

# Pre-load config_manager (via importlib so the import sorter can't reorder it
# after the backend) to cache the deep plugin_contracts chain before
# ``ananta.utils`` initializes — avoids the utils↔config circular import when the
# backend is imported standalone.
importlib.import_module("ananta.core.config.config_manager")
from actr_memory_plugin.backend import (  # noqa: E402
    EPISODIC_CONSOLIDATION_THRESHOLD,
    ACTRMemoryBackend,
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


# A fixed "now"; every memory is created 30d ago (past the 7d cutoff) and carries
# NO retrieval_times, so compute_strength returns -10.0 (< the -1.5 threshold) —
# i.e. every fixture clears BOTH the strength and age gates, isolating the tag
# check as the only thing that decides candidacy.
_NOW = datetime(2026, 6, 21, 0, 0, 0, tzinfo=UTC)
_CREATED_AT = (_NOW - timedelta(days=30)).isoformat()
_CUTOFF = _NOW - timedelta(days=7)


def _memory(mid: str, *, tags: list[str], content: str) -> dict[str, Any]:
    # No 'retrieval_times' key → compute_strength → -10.0 (deeply below threshold).
    return {"id": mid, "content": content, "tags": tags, "created_at": _CREATED_AT}


def _filter(memories: list[dict[str, Any]]) -> set[str]:
    backend = object.__new__(ACTRMemoryBackend)  # method uses no instance state
    candidates = backend._filter_consolidation_candidates(
        memories, EPISODIC_CONSOLIDATION_THRESHOLD, _CUTOFF, _NOW
    )
    return {m["id"] for m in candidates}


def test_knowledge_official_excluded_from_consolidation() -> None:
    """A weak, aged knowledge:official episodic memory is NOT a consolidation
    candidate (the fix); a plain weak episodic one still IS (no over-exclusion);
    the pre-existing tool_use exclusion is intact."""
    memories = [
        _memory("m-kb", tags=["knowledge:official", "episodic"], content="Official KB chunk on X."),
        _memory("m-plain", tags=[], content="A weak episodic conversation snippet."),
        _memory("m-tool", tags=["tool_use"], content="A non-Tool-prefixed tool-use record."),
    ]
    candidate_ids = _filter(memories)

    _check(
        "m-plain" in candidate_ids,
        "plain weak/aged episodic memory IS a consolidation candidate (no over-exclusion)",
    )
    _check(
        "m-kb" not in candidate_ids,
        "knowledge:official chunk is EXCLUDED from consolidation candidates (THE FIX)",
    )
    _check(
        "m-tool" not in candidate_ids,
        "pre-existing tool_use exclusion still holds",
    )


def main() -> int:
    print("=== actr_memory_consolidation_kb_exclusion_smoke ===")
    test_knowledge_official_excluded_from_consolidation()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
