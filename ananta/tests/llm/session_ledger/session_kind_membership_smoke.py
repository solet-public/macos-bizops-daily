#!/usr/bin/env python3
"""Offline smoke for ``list_sessions(source_kind=…)`` — wave 2b (no pytest).

Read-cap sweep wave 2b, 2026-08-16 (lane-ak). The ``source_kind`` path was over
the 100-row cap **twice**, and the live refusal on the serving release named the
FIRST of the two:

    code: query.unbounded_read_over_cap
    read_state on table 'session_source_kind' returned more than the 100-row cap
    details: {namespace: session_ledger, table: session_source_kind, cap_rows: 100}

1. ``_junction_canonical_ids`` read ``__session_source_kind`` whole —
   **4,005 rows for ``claude_code_local``**, 8,055 in the table.
2. Its output then became ``id = ANY(those 4,005)`` on ``__session``, so the
   second read was over the cap because the first one was.

Now: the junction PAGES, and every ``= ANY`` read is CHUNKED at the cap.

WHY THIS FILE EXISTS WHEN list_sessions_m17_filters_smoke ALREADY PASSES
========================================================================
It passes, and it cannot see either repair. Its fixtures hold a handful of
junction rows, so:

* the junction walk completes in **one short page** — a walk that stopped after
  page one would pass identically; and
* the membership list never exceeds the chunk size — an **unchunked** ``= ANY``
  would pass identically too.

Third time this sweep has hit the same shape: **a green from a fixture that
cannot reach the boundary is evidence about a different program.** So this
fixture is built to cross both boundaries, and asserts that it does.

THE FAKE REFUSES WHAT THE REAL PROVIDER REFUSES — THAT IS THE WHOLE MECHANISM
=============================================================================
``_FakeState.query_state`` raises when a ``col = ANY(values)`` filter carries
more than ``MAX_READ_ROWS`` values, exactly as the live provider does. Without
that, chunking is untestable offline: an unchunked read would quietly succeed
against a permissive fake and the smoke would certify the bug it was written to
catch. The refusal is the test.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/session_kind_membership_smoke.py
"""

from __future__ import annotations

import sys
from functools import partial
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.base import walk_table  # noqa: E402
from ananta.llm.session_ledger.read_support import (  # noqa: E402
    _read_full_group_membership,
)
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_SESSION,
    TABLE_SESSION_SOURCE_KIND,
)
from ananta.llm.session_ledger.types import IngestSourceKind  # noqa: E402
from ananta.services.state_service.read_bounds import MAX_READ_ROWS  # noqa: E402

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


#: Above MAX_READ_ROWS (100) so both boundaries are crossed, and below
#: ``list_sessions``' own limit clamp of 200 so a complete canonical-only result
#: is not truncated by the clamp. Getting this wrong cost a debugging pass: the
#: first fixture used 250 and the verb correctly returned 200, which looked
#: exactly like a lost chunk.
_GROUP_SIZE = 120
_KIND = IngestSourceKind.CLAUDE_CODE_LOCAL
_OTHER_KIND = IngestSourceKind.CODEX_LOCAL


class _CapRefusedError(RuntimeError):
    """What the live provider raises for an over-cap read."""


def _spec_matches(cell: object, spec: object) -> bool:
    """One filter spec. Fails loud on an op the code under test should not emit."""
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
    return all(_spec_matches(row.get(key), spec) for key, spec in filters.items())


def _key(row: dict[str, Any], columns: list[str]) -> tuple[str, ...]:
    return tuple(str(row.get(c, "")) for c in columns)


def _ordered_page(
    rows: list[dict[str, Any]],
    filters: dict[str, Any],
    columns: list[str],
    after: object,
) -> list[dict[str, Any]]:
    """Live rows matching ``filters``, ordered, and seeked past ``after``.

    ``is_deleted = 0`` is applied unconditionally — ``query_ordered``'s default,
    and what replaced the explicit filter these call sites used to pass.
    """
    matched = [
        dict(r) for r in rows if int(r.get("is_deleted", 0)) == 0 and _matches(r, filters)
    ]
    matched.sort(key=lambda r: _key(r, columns))
    if after is not None:
        cursor = tuple(str(v) for v in cast("list[Any]", after))
        matched = [r for r in matched if _key(r, columns) > cursor]
    return matched


class _FakeState:
    """State stand-in that ENFORCES the row cap, the point of the whole file."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._t = tables
        self.query_state_calls = 0
        self.query_ordered_calls = 0
        self.max_values_seen = 0

    def _rows(self, table: str) -> list[dict[str, Any]]:
        return self._t.setdefault(table, [])

    @staticmethod
    def _env(data: dict[str, Any]) -> dict[str, Any]:
        return {"action_status": "completed", "data": data, "actions": [], "error": None}

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        self.query_state_calls += 1
        filters = cast("dict[str, Any]", query.get("filters") or {})
        for spec in filters.values():
            if isinstance(spec, (list, tuple)):
                self.max_values_seen = max(self.max_values_seen, len(spec))
                if len(spec) > MAX_READ_ROWS:
                    raise _CapRefusedError(
                        f"query.unbounded_read_over_cap: `= ANY` filter carried "
                        f"{len(spec)} values against the {MAX_READ_ROWS}-row cap. "
                        f"An unchunked membership read is bounded by the CALLER's "
                        f"list, not by the query."
                    )
        rows = [dict(r) for r in self._rows(str(query["table"])) if _matches(r, filters)]
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
        if limit > MAX_READ_ROWS:
            raise _CapRefusedError(
                f"query_ordered limit {limit} over the {MAX_READ_ROWS} ceiling"
            )
        filters = cast("dict[str, Any]", query.get("filters") or {})
        columns = [pair[0] for pair in cast("list[list[str]]", query["order_by"])]
        rows = _ordered_page(
            self._rows(str(query["table"])), filters, columns, query.get("after"),
        )
        return self._env({"records": rows[:limit]})


def _fixture() -> dict[str, list[dict[str, Any]]]:
    """One source_kind group of ``_GROUP_SIZE`` canonicals, each with a sibling.

    Sizes chosen so BOTH repairs are exercised: the junction holds more rows than
    one page, and the canonical-id list is longer than one chunk.
    """
    junction: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    for i in range(_GROUP_SIZE):
        canon = f"canon-{i:04d}"
        junction.append(
            {
                "id": f"j-{i:04d}",
                "canonical_session_id": canon,
                "source_kind": _KIND.value,
                "is_deleted": 0,
                "created_at": f"2026-08-16T00:00:{i % 60:02d}.{i:06d}",
            }
        )
        sessions.append(_session(sid=canon, ext=f"ext-{i:04d}", canonical=None))
        # the sibling shares external_session_id — reachable ONLY via the
        # external-id membership read, never via `id = ANY(canonical_ids)`
        sessions.append(
            _session(sid=f"sib-{i:04d}", ext=f"ext-{i:04d}", canonical=f"ext-{i:04d}")
        )
    # a second kind that must never leak into the first kind's results
    junction.append(
        {
            "id": "j-other",
            "canonical_session_id": "canon-other",
            "source_kind": _OTHER_KIND.value,
            "is_deleted": 0,
            "created_at": "2026-08-16T00:01:00.000000",
        }
    )
    sessions.append(_session(sid="canon-other", ext="ext-other", canonical=None))
    return {TABLE_SESSION_SOURCE_KIND: junction, TABLE_SESSION: sessions}


def _session(*, sid: str, ext: str, canonical: str | None) -> dict[str, Any]:
    return {
        "id": sid,
        "source_id": "src-A",
        "external_session_id": ext,
        "vendor": "claude_code",
        "vendor_session_label": sid,
        "project_path": "/p",
        "first_event_at": "2026-06-01T00:00:00",
        "last_event_at": "2026-06-10T00:00:00",
        "event_count": 1,
        "canonical_external_session_id": canonical,
        "is_deleted": 0,
        "created_at": f"2026-08-16T00:00:00.{abs(hash(sid)) % 1000000:06d}",
    }


def _repo(state: _FakeState) -> SessionLedgerRepository:
    return SessionLedgerRepository(state_service=cast("Any", state))


# ---------------------------------------------------------------------------
# The fixture must be able to fail before any green means anything
# ---------------------------------------------------------------------------


def test_fixture_crosses_both_boundaries() -> None:
    tables = _fixture()
    junction_for_kind = [
        r for r in tables[TABLE_SESSION_SOURCE_KIND] if r["source_kind"] == _KIND.value
    ]
    _check(
        len(junction_for_kind) > MAX_READ_ROWS,
        f"junction holds {len(junction_for_kind)} rows for {_KIND.value} > cap "
        f"{MAX_READ_ROWS} — the walk MUST page (a smaller fixture tests nothing)",
    )
    _check(
        len(junction_for_kind) > MAX_READ_ROWS,
        f"canonical-id list is {len(junction_for_kind)} > chunk size {MAX_READ_ROWS} "
        f"— the membership read MUST chunk",
    )


# ---------------------------------------------------------------------------
# The repairs
# ---------------------------------------------------------------------------


def test_source_kind_route_survives_the_cap() -> None:
    state = _FakeState(_fixture())
    rows = _repo(state).list_sessions(limit=_GROUP_SIZE, source_kind=_KIND)
    ids = {str(r["id"]) for r in rows}
    _check(
        len(rows) == _GROUP_SIZE,
        f"canonical-only route returned all {_GROUP_SIZE} canonicals (got {len(rows)}) "
        f"— a walk that stopped at page 1 would return {MAX_READ_ROWS}",
    )
    _check("canon-other" not in ids, "the other source_kind's session did not leak in")
    _check(
        state.max_values_seen <= MAX_READ_ROWS,
        f"no `= ANY` read carried more than {MAX_READ_ROWS} values "
        f"(largest seen: {state.max_values_seen}) — chunking held",
    )


def test_include_siblings_returns_full_groups_across_chunks() -> None:
    """The FULL group — canonical + sibling — survives both chunk boundaries.

    Asserted against ``_read_full_group_membership`` directly rather than through
    ``list_sessions``, because the verb clamps ``limit`` to 200 and the group here
    is 240 rows. Going through the verb would test the clamp, not the repair, and
    a truncated-by-clamp result is indistinguishable from a lost chunk — which is
    exactly the confusion the first version of this file walked into.

    The siblings are the load-bearing half: they share ``external_session_id``
    with their canonical and are reachable ONLY through the second membership
    read, so their presence is what proves that read is complete. That read is
    also the one that must PAGE rather than merely chunk, because
    ``external_session_id`` is non-unique by design.
    """
    state = _FakeState(_fixture())
    repo = _repo(state)
    canonical_ids = [f"canon-{i:04d}" for i in range(_GROUP_SIZE)]
    rows = _read_full_group_membership(
        repo._query_membership_chunked,  # noqa: SLF001
        partial(walk_table, state),
        canonical_ids=canonical_ids,
        vendor=None,
        project_path=None,
    )
    ids = {str(r["id"]) for r in rows}
    canon = {f"canon-{i:04d}" for i in range(_GROUP_SIZE)}
    sibs = {f"sib-{i:04d}" for i in range(_GROUP_SIZE)}
    _check(canon <= ids, f"all {_GROUP_SIZE} canonicals present ({len(canon & ids)})")
    _check(
        sibs <= ids,
        f"all {_GROUP_SIZE} siblings present ({len(sibs & ids)}) — reachable only "
        f"via the external-id read, so this proves the PAGED membership read is "
        f"complete where input-chunking alone was not",
    )
    _check(
        "canon-other" not in ids,
        "the other source_kind's session did not leak into the group",
    )
    _check(
        state.max_values_seen <= MAX_READ_ROWS,
        f"no `= ANY` carried more than {MAX_READ_ROWS} values "
        f"(largest: {state.max_values_seen})",
    )


def test_empty_junction_short_circuits() -> None:
    state = _FakeState(_fixture())
    rows = _repo(state).list_sessions(limit=10, source_kind=IngestSourceKind.CHATGPT_EXPORT)
    _check(rows == [], "a source_kind with no junction rows short-circuits to []")


def main() -> int:
    print("=== session_kind_membership_smoke ===")
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
