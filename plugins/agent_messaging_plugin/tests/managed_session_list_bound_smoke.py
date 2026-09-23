#!/usr/bin/env python3
"""Offline smoke for the filter-required, hard-bounded fleet list (no pytest).

Read-cap sweep, 2026-08-16 (lane-ak). ``plugin::agent_messaging_plugin::list_sessions``
called with NO filters was **measured refusing on the serving release**:

    code: query.unbounded_read_over_cap
    read_state on table 'managed_session' returned more than the 100-row cap
    details: {namespace: agent_messaging_plugin, table: managed_session, cap_rows: 100}

106 live rows against a 100-row cap. The fleet ledger is append-mostly and
nothing prunes it, so it crossed the bound by **accumulating history** rather
than by anything going wrong. The repair now makes that dangerous default
impossible: ``list_sessions`` requires a meaningful fleet filter, refuses when
match ``limit + 1`` exists, and reserves the complete paged walk for a named,
reason-bearing internal helper.

WHAT THIS FILE PINS THAT A SMALLER FIXTURE CANNOT
==================================================
Two properties, neither visible below the page boundary:

1. **Cursor continuation past 250 rows.** The fixture holds more than 250 rows
   at a provider page size of 100, so a public roster can prove it neither
   refuses nor silently truncates an operator-scale backlog.
2. **The soft-delete override survives.** The old code seeded ``{is_deleted: 0}``
   and let a caller's ``filters`` OVERWRITE it, so ``is_deleted: 1`` returned
   soft-deleted rows. ``iter_table_rows`` expresses that as ``include_deleted``,
   so the request had to be translated rather than dropped. No in-repo caller
   uses it today, but ``list_sessions`` forwards arbitrary filters — so it is
   reachable, and silently changing it would be a behaviour change smuggled
   inside a bound fix. Both directions are asserted here.

THE FAKE ENFORCES THE CAP
=========================
``_FakeState.query_state`` refuses an unlimited read returning more than
``MAX_READ_ROWS`` rows, exactly as the live provider does. Against a permissive
fake the ORIGINAL unbounded read passes this file unchanged and the smoke
certifies the bug it was written to catch. Earlier in this sweep a strict fake
of this kind rejected a repair of mine that a permissive one would have greened.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/managed_session_list_bound_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.services.state_service.read_bounds import MAX_READ_ROWS  # noqa: E402

from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionFilterRequiredError,
    iter_managed_sessions_unbounded,
    list_managed_sessions,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    VerbError,
    list_sessions,
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


#: Comfortably over both MAX_READ_ROWS (100) and list_sessions' former 250-row
#: refusal so the cursor walk must cross multiple provider pages and public
#: pages.  The live table was 106 when the original provider bound appeared;
#: the operator-host backlog now exceeds 250.
_LIVE_ROWS = 301
_DELETED_ROWS = 30


class _CapRefusedError(RuntimeError):
    """What the live provider raises for an over-cap read."""


def _spec_matches(cell: object, spec: object) -> bool:
    if isinstance(spec, dict):
        op = spec.get("op")
        if op == "is_null":
            return cell is None
        if op == "is_not_null":
            return cell is not None
        raise AssertionError(f"fake does not implement op {op!r}")
    if isinstance(spec, (list, tuple)):
        return cell in spec
    return cell == spec


def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    return all(_spec_matches(row.get(k), v) for k, v in filters.items())


def _key(row: dict[str, Any], columns: list[str]) -> tuple[str, ...]:
    return tuple(str(row.get(c, "")) for c in columns)


def _ordered_page(
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any],
    columns: list[str],
    include_deleted: bool,
    after: object,
) -> list[dict[str, Any]]:
    """Live rows matching ``filters``, ordered, and seeked past ``after``.

    ``include_deleted`` is honoured rather than assumed: it is the predicate that
    REPLACED this call site's explicit ``{is_deleted: 0}`` filter, and the
    soft-delete override test depends on the fake distinguishing the two.
    """
    matched = [
        dict(r)
        for r in rows
        if (include_deleted or int(r.get("is_deleted", 0)) == 0) and _matches(r, filters)
    ]
    matched.sort(key=lambda r: _key(r, columns))
    if after is None:
        return matched
    cursor = tuple(str(v) for v in cast("list[Any]", after))
    return [r for r in matched if _key(r, columns) > cursor]


class _FakeState:
    """State stand-in that ENFORCES the row cap — the point of the file."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.query_state_calls = 0
        self.query_ordered_calls = 0

    @staticmethod
    def _env(data: dict[str, Any]) -> dict[str, Any]:
        return {"action_status": "completed", "data": data, "actions": [], "error": None}

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        self.query_state_calls += 1
        filters = cast("dict[str, Any]", query.get("filters") or {})
        rows = [dict(r) for r in self._rows if _matches(r, filters)]
        if not query.get("unbounded") and "limit" not in query and len(rows) > MAX_READ_ROWS:
            raise _CapRefusedError(
                f"query.unbounded_read_over_cap: {str(query['table'])!r} returned "
                f"{len(rows)} rows against the {MAX_READ_ROWS}-row cap."
            )
        return self._env({"records": rows})

    def query_ordered(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        self.query_ordered_calls += 1
        limit = int(query["limit"])
        if limit > MAX_READ_ROWS and not query.get("unbounded"):
            raise _CapRefusedError(f"limit {limit} over the {MAX_READ_ROWS} ceiling")
        rows = _ordered_page(
            self._rows,
            filters=cast("dict[str, Any]", query.get("filters") or {}),
            columns=[pair[0] for pair in cast("list[list[str]]", query["order_by"])],
            include_deleted=bool(query.get("include_deleted")),
            after=query.get("after"),
        )
        return self._env({"records": rows[:limit]})


def _row(idx: int, *, is_deleted: int = 0, lane: str = "lane-a") -> dict[str, Any]:
    return {
        "id": f"ms-{idx:04d}",
        "agent_instance_id": f"agi-{idx:04d}",
        "agent_session_id": f"ases-agi-{idx:04d}",
        "lane_id": lane,
        "lifecycle_state": "live" if idx % 3 else "retired",
        "host": "tmux",
        "is_deleted": is_deleted,
        "created_at": f"2026-08-16T00:00:00.{idx:06d}",
    }


def _state() -> _FakeState:
    rows = [_row(i) for i in range(_LIVE_ROWS)]
    rows += [_row(_LIVE_ROWS + i, is_deleted=1) for i in range(_DELETED_ROWS)]
    return _FakeState(rows)


def test_fixture_crosses_the_page_boundary() -> None:
    _check(
        _LIVE_ROWS > 2 * MAX_READ_ROWS,
        f"fixture has {_LIVE_ROWS} live rows vs page size {MAX_READ_ROWS} — the "
        f"walk MUST page more than once (the live table was 106, a near-miss)",
    )


def test_deliberate_unbounded_helper_returns_every_live_row() -> None:
    """The named escape hatch remains complete past page one."""
    state = _state()
    rows = list(
        iter_managed_sessions_unbounded(
            state,  # type: ignore[arg-type]
            reason="smoke proves the explicit full-ledger helper stays complete",
        )
    )
    _check(
        len(rows) == _LIVE_ROWS,
        f"unfiltered fleet list returned all {_LIVE_ROWS} live rows (got "
        f"{len(rows)}) — a page-1-only walk returns {MAX_READ_ROWS}",
    )
    _check(
        len({str(r["id"]) for r in rows}) == len(rows),
        "no duplicate rows across page boundaries",
    )
    _check(
        all(int(cast("int", r["is_deleted"])) == 0 for r in rows),
        f"the {_DELETED_ROWS} soft-deleted rows are excluded by the "
        f"include_deleted default, which replaced the explicit is_deleted filter",
    )
    _check(state.query_state_calls == 0, "no unbounded query_state read remains")


def test_store_refuses_an_implicit_full_walk() -> None:
    refused = False
    try:
        list_managed_sessions(_state())  # type: ignore[arg-type]
    except ManagedSessionFilterRequiredError:
        refused = True
    _check(refused, "list_managed_sessions without filters refuses by default")


def test_list_sessions_requires_a_filter() -> None:
    """RED MUTATION: remove the verb filter guard and this test must fail."""
    for filters in (None, {}, {"is_deleted": 0}):
        code = None
        message = ""
        try:
            list_sessions(_state(), filters)  # type: ignore[arg-type]
        except VerbError as exc:
            code = exc.code
            message = exc.message
        _check(
            code == "filter_required"
            and "live_only" in message
            and "lane_id" in message
            and "\n" not in message,
            f"list_sessions({filters!r}) -> one-line filter_required with accepted keys",
        )


def test_list_sessions_pages_past_former_hard_limit() -> None:
    """A >250-row filtered roster is enumerated, not refused or truncated."""
    state = _state()
    ids: list[str] = []
    cursor: dict[str, str] | None = None
    pages = 0
    while True:
        page = list_sessions(
            state,
            {"lane_id": "lane-a"},
            limit=50,  # type: ignore[arg-type]
            after_created_at=(cursor or {}).get("created_at"),
            after_id=(cursor or {}).get("id"),
        )
        pages += 1
        rows = page["sessions"]
        ids.extend(str(row["id"]) for row in rows)
        _check(page["returned"] == len(rows), "each page reports its exact returned count")
        if not page["truncated"]:
            _check(page["next_cursor"] is None, "the final page has no continuation cursor")
            break
        next_cursor = page["next_cursor"]
        _check(
            isinstance(next_cursor, dict)
            and isinstance(next_cursor.get("created_at"), str)
            and isinstance(next_cursor.get("id"), str),
            "every non-final page supplies both immutable cursor components",
        )
        cursor = cast("dict[str, str]", next_cursor)
    _check(
        pages > 5 and len(ids) == _LIVE_ROWS and len(set(ids)) == _LIVE_ROWS,
        "a filtered roster above the former 250-row ceiling drains completely without duplicates",
    )


def test_list_sessions_returns_exact_under_limit_rows() -> None:
    state = _state()
    expected_ids = [f"ms-{idx:04d}" for idx in range(_LIVE_ROWS) if idx % 3 == 0]
    rows = list_sessions(
        state, {"lifecycle_state": "retired"}, limit=150,  # type: ignore[arg-type]
    )["sessions"]
    _check(
        [row["id"] for row in rows] == expected_ids,
        "a filtered match set under limit returns the exact rows",
    )


def test_list_sessions_live_only_excludes_terminal_rows() -> None:
    rows = list_sessions(
        _state(), live_only=True, limit=250,  # type: ignore[arg-type]
    )["sessions"]
    _check(
        len(rows) == sum(1 for idx in range(_LIVE_ROWS) if idx % 3 != 0)
        and {str(row["lifecycle_state"]) for row in rows} == {"live"},
        "live_only uses one IN filter and returns non-terminal rows only",
    )


def test_caller_filters_are_pushed_down() -> None:
    state = _state()
    rows = list_managed_sessions(state, {"lifecycle_state": "retired"})  # type: ignore[arg-type]
    expected = sum(1 for i in range(_LIVE_ROWS) if i % 3 == 0)
    _check(
        len(rows) == expected and all(r["lifecycle_state"] == "retired" for r in rows),
        f"equality filter applied on EVERY page, not just the first "
        f"(got {len(rows)}, expected {expected})",
    )


def test_soft_delete_override_is_preserved() -> None:
    """``is_deleted: 1`` still returns soft-deleted rows, as it did before.

    The old code seeded ``{is_deleted: 0}`` and let ``filters`` overwrite it.
    The paged read expresses the same thing through ``include_deleted``, so the
    request is TRANSLATED rather than dropped. Without this test the translation
    could be quietly removed and nothing in the repo would notice — no in-repo
    caller uses it, but ``list_sessions`` forwards arbitrary filters.
    """
    state = _state()
    rows = list_managed_sessions(state, {"is_deleted": 1})  # type: ignore[arg-type]
    _check(
        len(rows) == _DELETED_ROWS,
        f"is_deleted=1 returns the {_DELETED_ROWS} soft-deleted rows "
        f"(got {len(rows)}) — the override survived the rewrite",
    )
    _check(
        all(int(cast("int", r["is_deleted"])) == 1 for r in rows),
        "and returns ONLY soft-deleted rows, not a union",
    )


def test_explicit_is_deleted_zero_matches_the_default() -> None:
    state = _state()
    explicit = list_managed_sessions(state, {"is_deleted": 0})  # type: ignore[arg-type]
    default = list(
        iter_managed_sessions_unbounded(
            state,  # type: ignore[arg-type]
            reason="compare the deliberate default soft-delete predicate",
        )
    )
    _check(
        {str(r["id"]) for r in explicit} == {str(r["id"]) for r in default},
        "an explicit is_deleted=0 is identical to the default — the two "
        "predicates agree, which is what makes dropping the explicit one safe",
    )


def main() -> int:
    print("=== managed_session_list_bound_smoke ===")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
