#!/usr/bin/env python3
"""Unit smoke: ``_real_state_fake.RealShapeState``'s own filter-matching
semantics (2026-08-04, fix slice following acceptance Test C).

``_match``/``_match_one`` is shared, load-bearing test infrastructure used
by 10+ smoke files across this plugin. Before this fix it equality-matched
a bare ``None`` filter value against a ``None`` cell — the real postgres
provider does not: a bare ``None`` compiles to SQL ``col = NULL``, always
UNKNOWN/false, matching ZERO rows (``provider.py::_build_filter_clauses``;
the documented NULL form is ``{"op": "is_null"}``). That divergence hid a
real production bug (session_dependency rows armed with ``fired_at=NULL``
were never found by the sweep's own bare-``None`` filter) behind a false
green for as long as this fixture has existed. This smoke pins the
CORRECTED semantics directly so a future regression here is caught
immediately, not silently, the way the original bug was.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/real_state_fake_filter_semantics_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402

_NAMESPACE = "test_ns"
_TABLE = "test_table"

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


def test_bare_none_filter_never_matches_a_null_cell() -> None:
    """The exact bug class Test C caught: a row with a genuinely NULL cell
    must NOT be found by a bare ``None`` filter value — mirrors the real
    provider's ``col = NULL`` (always false), not Python's ``None == None``.
    Named failing mutation: reverting ``_match_one``'s ``if want is None:
    return False`` to ``cell == want`` reds this exact leg."""
    state = RealShapeState()
    state.write_state(_NAMESPACE, {"table": _TABLE, "record": {"marker": "row-with-null", "fired_at": None}})
    result = state.query_state(_NAMESPACE, {"table": _TABLE, "filters": {"fired_at": None}})
    records = result["data"]["records"]
    _check(records == [], f"a bare None filter matches ZERO rows, even a genuinely NULL cell (got {records!r})")


def test_is_null_op_matches_a_null_cell() -> None:
    """The documented, correct way to filter for NULL. Named failing
    mutation: removing the ``op == "is_null"`` branch (or mapping it to
    equality) reds this leg."""
    state = RealShapeState()
    state.write_state(_NAMESPACE, {"table": _TABLE, "record": {"marker": "row-with-null", "fired_at": None}})
    state.write_state(_NAMESPACE, {"table": _TABLE, "record": {"marker": "row-with-value", "fired_at": "2026-01-01T00:00:00+00:00"}})
    result = state.query_state(_NAMESPACE, {"table": _TABLE, "filters": {"fired_at": {"op": "is_null"}}})
    records = result["data"]["records"]
    _check(
        len(records) == 1 and records[0]["marker"] == "row-with-null",
        f"{{'op': 'is_null'}} matches exactly the NULL-celled row (got {records!r})",
    )


def test_is_not_null_op_excludes_a_null_cell() -> None:
    state = RealShapeState()
    state.write_state(_NAMESPACE, {"table": _TABLE, "record": {"marker": "row-with-null", "fired_at": None}})
    state.write_state(_NAMESPACE, {"table": _TABLE, "record": {"marker": "row-with-value", "fired_at": "2026-01-01T00:00:00+00:00"}})
    result = state.query_state(_NAMESPACE, {"table": _TABLE, "filters": {"fired_at": {"op": "is_not_null"}}})
    records = result["data"]["records"]
    _check(
        len(records) == 1 and records[0]["marker"] == "row-with-value",
        f"{{'op': 'is_not_null'}} matches exactly the non-NULL-celled row (got {records!r})",
    )


def test_scalar_equality_unaffected() -> None:
    """The fix must not touch ordinary scalar matching — regression guard."""
    state = RealShapeState()
    state.write_state(_NAMESPACE, {"table": _TABLE, "record": {"marker": "m1", "status": "armed"}})
    state.write_state(_NAMESPACE, {"table": _TABLE, "record": {"marker": "m2", "status": "fired"}})
    result = state.query_state(_NAMESPACE, {"table": _TABLE, "filters": {"status": "armed"}})
    records = result["data"]["records"]
    _check(
        len(records) == 1 and records[0]["marker"] == "m1",
        f"ordinary scalar equality is unaffected by the None/is_null fix (got {records!r})",
    )


def test_id_stamped_on_insert_supports_a_later_is_null_guarded_update() -> None:
    """End-to-end regression guard for the compound bug: arm (insert,
    fired_at=NULL) -> query by is_null -> predicated update keyed on the
    read-back id, guarded by is_null again -- the exact shape
    _fire_armed_dependencies/_fire_session_terminal_dependencies use."""
    state = RealShapeState()
    state.write_state(_NAMESPACE, {"table": _TABLE, "record": {"marker": "edge-1", "fired_at": None}})
    found = state.query_state(_NAMESPACE, {"table": _TABLE, "filters": {"fired_at": {"op": "is_null"}}})
    records = found["data"]["records"]
    _check(len(records) == 1, "the armed row is found via is_null")
    edge_id = records[0].get("id")
    _check(bool(edge_id), f"the found row carries a stamped id (got {edge_id!r})")
    updated = state.update_state(
        _NAMESPACE,
        {"table": _TABLE, "filters": {"id": edge_id, "fired_at": {"op": "is_null"}}},
        {"fired_at": "2026-01-01T00:00:00+00:00"},
    )
    _check(
        updated["data"]["result"]["updated"] == 1,
        f"the predicated update, guarded by is_null and keyed on the read-back id, succeeds (got {updated})",
    )
    refound = state.query_state(_NAMESPACE, {"table": _TABLE, "filters": {"fired_at": {"op": "is_null"}}})
    _check(
        refound["data"]["records"] == [],
        "a second is_null query finds nothing -- the row is no longer armed (re-run-safe)",
    )


def main() -> int:
    print("=== RealShapeState filter-semantics smoke (None/is_null fix, 2026-08-04) ===")
    test_bare_none_filter_never_matches_a_null_cell()
    test_is_null_op_matches_a_null_cell()
    test_is_not_null_op_excludes_a_null_cell()
    test_scalar_equality_unaffected()
    test_id_stamped_on_insert_supports_a_later_is_null_guarded_update()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
