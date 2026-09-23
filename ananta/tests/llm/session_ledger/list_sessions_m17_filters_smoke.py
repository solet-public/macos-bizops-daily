"""list_sessions junction-migration smoke (SQL-lockdown) + M17 filter coverage.

The pre-migration raw-SQL list_sessions (EXISTS-over-canonical-group source_kind
+ in-SQL windows/order) retired onto the Architect-ruled junction read-then-route
(`query_state(session_source_kind, {source_kind}) → canonical_ids →
query_state(session, {id: ANY}})`) + an UNCAPPED `query_state` session read whose
two two-sided `event_at` windows + `SessionsOrderBy` sort + limit apply in Python
(`select_sessions_page`). This smoke exercises that end-to-end through a
FILTER+WRITE-honoring shim (so the real `list_sessions` / `upsert_session`
junction-attach / `backfill_session_source_kinds` / `canonical_pointer_repair`
recompute run in CI) plus focused pure `select_sessions_page` assertions, and
keeps the two migration-unaffected M17 tests (search_sessions envelope filtering
+ register_source outcome).

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/list_sessions_m17_filters_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.read_support import (  # noqa: E402
    SessionWindow,
    select_sessions_page,
)
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_SESSION,
    TABLE_SESSION_SOURCE_KIND,
)
from ananta.llm.session_ledger.types import (  # noqa: E402
    IngestSourceKind,
    SessionsOrderBy,
    SourceVendor,
)

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


_CLOCK = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)


def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Faithful flat-grammar filter: scalar eq, list = ANY, is_null/is_not_null, range."""
    for col, cond in filters.items():
        val = row.get(col)
        if isinstance(cond, dict):
            op = cond.get("op")
            if op == "is_null" and val is not None:
                return False
            if op == "is_not_null" and val is None:
                return False
            if op in ("lt", "lte", "gt", "gte"):
                if val is None:
                    return False
                target = cond["value"]
                left = (
                    datetime.fromisoformat(str(val))
                    if isinstance(target, datetime)
                    else val
                )
                if op == "lte" and not left <= target:  # type: ignore[operator]
                    return False
                if op == "lt" and not left < target:  # type: ignore[operator]
                    return False
                if op == "gte" and not left >= target:  # type: ignore[operator]
                    return False
                if op == "gt" and not left > target:  # type: ignore[operator]
                    return False
        elif isinstance(cond, list):
            if val not in cond:
                return False
        elif val != cond:
            return False
    return True


class _Shim:
    """Filter+write-honoring StateManagementInterface stand-in over in-memory tables."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._t = tables

    def _rows(self, table: str) -> list[dict[str, Any]]:
        return self._t.setdefault(table, [])

    def _env(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"action_status": "completed", "data": data, "actions": [], "error": None}

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        flt = cast("dict[str, Any]", query.get("filters") or {})
        rows = [dict(r) for r in self._rows(str(query["table"])) if _matches(r, flt)]
        return self._env({"records": rows})

    def query_ordered(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        flt = cast("dict[str, Any]", query.get("filters") or {})
        rows = [
            dict(r)
            for r in self._rows(str(query["table"]))
            if int(r.get("is_deleted", 0)) == 0 and _matches(r, flt)
        ]
        for col, direction in reversed(cast("list[list[str]]", query["order_by"])):
            rows.sort(key=lambda r: str(r.get(col, "")), reverse=(direction == "desc"))
        return self._env({"records": rows[: int(query["limit"])]})

    def upsert_state(
        self, namespace: str, query: dict[str, Any], *args: Any, **kwargs: Any,
    ) -> dict[str, Any]:
        # do_nothing on the conflict_columns (the junction UNIQUE).
        record = cast("dict[str, Any]", query["record"])
        cols = cast("list[str]", query["conflict_columns"])
        table = str(query["table"])
        for existing in self._rows(table):
            if all(existing.get(c) == record.get(c) for c in cols):
                return self._env({"result": {"inserted": False}})
        self._rows(table).append(dict(record))
        return self._env({"result": {"inserted": True}})

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any],
    ) -> dict[str, Any]:
        flt = cast("dict[str, Any]", query.get("filters") or {})
        updated = 0
        for row in self._rows(str(query["table"])):
            if _matches(row, flt):
                row.update(updates)
                updated += 1
        return self._env({"result": {"updated": updated}})

    def write_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        record = cast("dict[str, Any]", query["record"])
        self._rows(str(query["table"])).append(dict(record))
        return self._env({"result": {"generated_id": str(record.get("id", ""))}})

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        flt = cast("dict[str, Any]", query.get("filters") or {})
        table = str(query["table"])
        keep = [r for r in self._rows(table) if not _matches(r, flt)]
        deleted = len(self._rows(table)) - len(keep)
        self._t[table] = keep
        return self._env({"result": {"deleted": deleted}})


def _repo(tables: dict[str, list[dict[str, Any]]]) -> SessionLedgerRepository:
    return SessionLedgerRepository(state_service=cast("Any", _Shim(tables)), clock=lambda: _CLOCK)


def _session(
    *, sid: str, vendor: str = "codex", source_id: str = "src-A",
    ext: str = "ext-1", canonical: str | None = None,
    last_event_at: str = "2026-06-10T00:00:00", first_event_at: str = "2026-06-01T00:00:00",
    project_path: str = "/p", is_deleted: int = 0,
) -> dict[str, Any]:
    return {
        "id": sid, "source_id": source_id, "external_session_id": ext, "vendor": vendor,
        "vendor_session_label": sid, "project_path": project_path,
        "first_event_at": first_event_at, "last_event_at": last_event_at,
        "event_count": 1, "canonical_external_session_id": canonical, "is_deleted": is_deleted,
    }


# ── pure select_sessions_page ────────────────────────────────────────────────

def test_pure_windows_boundary_and_projection() -> None:
    rows = [
        _session(sid="a", last_event_at="2026-06-10T00:00:00", first_event_at="2026-06-01T00:00:00"),
        _session(sid="b", last_event_at="2026-06-20T00:00:00", first_event_at="2026-06-02T00:00:00"),
        _session(sid="c", last_event_at="2026-06-05T00:00:00", first_event_at="2026-06-01T00:00:00"),  # last < since
        _session(sid="d", last_event_at="2026-06-15T00:00:00", first_event_at="2026-05-01T00:00:00"),  # first < first_since
    ]
    out = select_sessions_page(
        rows,
        window=SessionWindow(
            since=datetime(2026, 6, 8), until=datetime(2026, 6, 21),
            first_event_since=datetime(2026, 5, 15), first_event_until=None,
        ),
        order_by=SessionsOrderBy.LAST_EVENT_AT_DESC, limit=50,
    )
    _check(
        [str(r["id"]) for r in out] == ["b", "a"],
        "pure: both two-sided windows applied (c dropped by since, d by first_event_since); DESC",
    )
    _check(
        set(out[0]) == {"id", "source_id", "external_session_id", "vendor",
                        "vendor_session_label", "project_path", "first_event_at",
                        "last_event_at", "event_count", "canonical_external_session_id"},
        "pure: projected to the 10-col envelope",
    )


def test_pure_order_variants_and_limit() -> None:
    rows = [
        _session(sid="old_last", last_event_at="2026-06-01T00:00:00", first_event_at="2026-06-09T00:00:00"),
        _session(sid="new_last", last_event_at="2026-06-09T00:00:00", first_event_at="2026-06-01T00:00:00"),
    ]
    win = SessionWindow(since=None, until=None, first_event_since=None, first_event_until=None)
    desc = select_sessions_page(rows, window=win, order_by=SessionsOrderBy.LAST_EVENT_AT_DESC, limit=50)
    asc = select_sessions_page(rows, window=win, order_by=SessionsOrderBy.FIRST_EVENT_AT_ASC, limit=50)
    capped = select_sessions_page(rows, window=win, order_by=SessionsOrderBy.LAST_EVENT_AT_DESC, limit=1)
    _check([str(r["id"]) for r in desc] == ["new_last", "old_last"], "pure: LAST_EVENT_AT_DESC orders by last_event_at desc")
    _check([str(r["id"]) for r in asc] == ["new_last", "old_last"], "pure: FIRST_EVENT_AT_ASC orders by first_event_at asc")
    _check([str(r["id"]) for r in capped] == ["new_last"], "pure: limit truncates after sort")


# ── real list_sessions via the honoring shim ─────────────────────────────────

def test_real_canonical_only_default_and_include_siblings() -> None:
    tables = {TABLE_SESSION: [
        _session(sid="canon", canonical=None),
        _session(sid="sib", canonical="ext-1"),
    ]}
    default = _repo(tables).list_sessions(limit=50)
    forensic = _repo(tables).list_sessions(limit=50, include_siblings=True)
    _check([str(r["id"]) for r in default] == ["canon"], "real: default returns canonical only")
    _check(
        sorted(str(r["id"]) for r in forensic) == ["canon", "sib"],
        "real: include_siblings returns canonical + sibling",
    )


def test_real_vendor_and_window_filtering() -> None:
    tables = {TABLE_SESSION: [
        _session(sid="cx", vendor="codex", last_event_at="2026-06-10T00:00:00"),
        _session(sid="cc", vendor="claude_code", last_event_at="2026-06-10T00:00:00"),
        _session(sid="cx_old", vendor="codex", last_event_at="2026-05-01T00:00:00"),  # before since
    ]}
    rows = _repo(tables).list_sessions(
        limit=50, vendor=SourceVendor.CODEX, since=datetime(2026, 6, 1, tzinfo=UTC),
    )
    _check(
        [str(r["id"]) for r in rows] == ["cx"],
        "real: vendor equality + last_event_at since window (cc dropped by vendor, cx_old by since)",
    )


def test_real_source_kind_junction_route() -> None:
    tables = {
        TABLE_SESSION: [_session(sid="canon", canonical=None), _session(sid="other", ext="ext-2", canonical=None)],
        TABLE_SESSION_SOURCE_KIND: [
            {"canonical_session_id": "canon", "source_kind": "codex_history", "is_deleted": 0},
        ],
    }
    rows = _repo(tables).list_sessions(limit=50, source_kind=IngestSourceKind.CODEX_HISTORY)
    _check(
        [str(r["id"]) for r in rows] == ["canon"],
        "real: source_kind routes via junction → only the canonical whose group has the kind",
    )
    empty = _repo(tables).list_sessions(limit=50, source_kind=IngestSourceKind.CHATGPT_EXPORT)
    _check(empty == [], "real: source_kind with no junction match short-circuits to []")


def test_real_source_kind_with_include_siblings_returns_full_group() -> None:
    """include_siblings=True + source_kind=K returns the group's FULL membership.

    The DISCRIMINATOR for the relayed-fix key-space bug (Reviewer-C MAJOR): the
    canonical + sibling have DISTINCT session ids but SHARE external_session_id,
    and the junction is keyed on the canonical's SESSION id. A direct
    ``id = ANY(canonical_ids)`` read (the bug, and the relayed fix's no-op) returns
    only the canonical; the faithful expansion via the shared external_session_id
    returns canonical + sibling. A cross-vendor row sharing the same
    external_session_id but in a non-kind-K group is dropped by the (vendor, ext)
    pairs filter (the byte-faithful backstop for the EXISTS vendor+ext predicate).
    """
    tables = {
        TABLE_SESSION: [
            _session(sid="canon", ext="ext-1", canonical=None),
            _session(sid="sib", ext="ext-1", canonical="ext-1"),
            _session(sid="xv", vendor="claude_code", ext="ext-1", canonical=None),
        ],
        TABLE_SESSION_SOURCE_KIND: [
            {"canonical_session_id": "canon", "source_kind": "codex_history", "is_deleted": 0},
        ],
    }
    rows = _repo(tables).list_sessions(
        limit=50, source_kind=IngestSourceKind.CODEX_HISTORY, include_siblings=True,
    )
    ids = sorted(str(r["id"]) for r in rows)
    _check(
        ids == ["canon", "sib"],
        "real: include_siblings + source_kind returns the FULL group (canonical + "
        "sibling via shared external_session_id), NOT canonical-only — "
        f"discriminates the key-space bug (got {ids})",
    )
    _check(
        "xv" not in ids,
        "real: a cross-vendor row sharing external_session_id but in a non-kind-K "
        "group is dropped by the (vendor, ext) pairs filter",
    )


# ── real ingest-attach junction maintenance ──────────────────────────────────

def test_real_upsert_session_attaches_junction() -> None:
    tables: dict[str, list[dict[str, Any]]] = {}
    repo = _repo(tables)
    sid = repo.upsert_session(
        source_id="src-A", external_session_id="ext-1", vendor=SourceVendor.CODEX,
        source_kind=IngestSourceKind.CODEX_LOCAL, vendor_session_label=None,
        project_path=None, first_event_at=_CLOCK, last_event_at=_CLOCK,
    )
    junction = tables.get(TABLE_SESSION_SOURCE_KIND, [])
    _check(
        len(junction) == 1
        and junction[0]["canonical_session_id"] == sid
        and junction[0]["source_kind"] == IngestSourceKind.CODEX_LOCAL.value,
        "real: upsert_session (new canonical) attaches (canonical_id, source_kind) to the junction",
    )


# ── real backfill ────────────────────────────────────────────────────────────

def test_real_backfill_populates_and_idempotent() -> None:
    tables = {
        "source": [
            {"id": "src-A", "source_kind": "codex_local", "is_deleted": 0},
            {"id": "src-B", "source_kind": "codex_history", "is_deleted": 0},
        ],
        TABLE_SESSION: [
            _session(sid="canon", source_id="src-A", ext="ext-1", canonical=None),
            _session(sid="sib", source_id="src-B", ext="ext-1", canonical="ext-1"),  # same group
        ],
        TABLE_SESSION_SOURCE_KIND: [],
    }
    repo = _repo(tables)
    first = repo.backfill_session_source_kinds()
    pairs = {(r["canonical_session_id"], r["source_kind"]) for r in tables[TABLE_SESSION_SOURCE_KIND]}
    _check(
        pairs == {("canon", "codex_local"), ("canon", "codex_history")},
        "real backfill: both group members' kinds recorded under the canonical id",
    )
    _check(first["junction_rows_written"] == 2, "real backfill: reports 2 new pairs")
    second = repo.backfill_session_source_kinds()
    _check(second["junction_rows_written"] == 0, "real backfill: idempotent (re-run writes 0)")


# ── real canonical_pointer_repair junction recompute ─────────────────────────

def test_real_recompute_moves_demoted_kinds_to_survivor() -> None:
    tables = {
        TABLE_SESSION: [
            _session(sid="c_old", ext="ext-1", canonical=None, last_event_at="2026-06-01T00:00:00"),
            _session(sid="c_new", ext="ext-1", canonical=None, last_event_at="2026-06-02T00:00:00"),
        ],
        TABLE_SESSION_SOURCE_KIND: [
            {"canonical_session_id": "c_old", "source_kind": "codex_local", "is_deleted": 0},
            {"canonical_session_id": "c_new", "source_kind": "codex_history", "is_deleted": 0},
        ],
    }
    # c_old/c_new are BOTH canonical for ext-1 (the duplicate anomaly). created_at
    # decides the survivor; give c_old the older created_at.
    tables[TABLE_SESSION][0]["created_at"] = "2026-06-01T00:00:00"
    tables[TABLE_SESSION][1]["created_at"] = "2026-06-02T00:00:00"
    repo = _repo(tables)
    demoted = repo.lift_canonical_pointer_for_duplicate_sessions()
    survivor_pairs = {
        r["source_kind"] for r in tables[TABLE_SESSION_SOURCE_KIND]
        if r["canonical_session_id"] == "c_old"
    }
    stale = [r for r in tables[TABLE_SESSION_SOURCE_KIND] if r["canonical_session_id"] == "c_new"]
    _check(demoted >= 1, "real recompute: the duplicate canonical was demoted")
    _check(
        survivor_pairs == {"codex_local", "codex_history"},
        "real recompute: demoted c_new's kind merged under survivor c_old",
    )
    _check(stale == [], "real recompute: stale (c_new, *) junction rows hard-deleted")


# ── migration-unaffected M17 tests (kept) ────────────────────────────────────

def test_search_sessions_filter_envelopes() -> None:
    from ananta.llm.session_ledger.summarization import SearchResultEnvelope  # noqa: PLC0415
    from ananta.services.session_ledger_service.service import _filter_envelopes  # noqa: PLC0415

    def _env(sid: str, vendor: SourceVendor, sk: IngestSourceKind, last: datetime) -> SearchResultEnvelope:
        return SearchResultEnvelope(
            session_id=sid, chunk_index=0, summary_text="", score=0.0,
            session={"vendor": vendor.value, "source_kind": sk.value, "last_event_at": last},
        )

    e1 = _env("s1", SourceVendor.CODEX, IngestSourceKind.CODEX_STATE, datetime(2026, 6, 10, tzinfo=UTC))
    e2 = _env("s2", SourceVendor.CLAUDE_CODE, IngestSourceKind.CLAUDE_CODE_LOCAL, datetime(2026, 6, 11, tzinfo=UTC))
    e3 = _env("s3", SourceVendor.CODEX, IngestSourceKind.CODEX_HISTORY, datetime(2026, 5, 1, tzinfo=UTC))
    _check(
        [e.session_id for e in _filter_envelopes([e1, e2, e3], vendor=SourceVendor.CODEX, source_kind=None, since=None)] == ["s1", "s3"],
        "search_sessions: vendor filter",
    )
    _check(
        [e.session_id for e in _filter_envelopes([e1, e2, e3], vendor=None, source_kind=IngestSourceKind.CODEX_STATE, since=None)] == ["s1"],
        "search_sessions: source_kind filter",
    )
    _check(
        [e.session_id for e in _filter_envelopes([e1, e2, e3], vendor=None, source_kind=None, since=datetime(2026, 6, 1, tzinfo=UTC))] == ["s1", "s2"],
        "search_sessions: since filter",
    )


def test_register_source_returns_outcome_not_action() -> None:
    """The insert path reports ``outcome=registered``; the idempotent hit reports
    ``outcome=existed``; neither uses the ``action`` key.

    REWRITTEN 2026-08-16 (lane-ak, coordinator ruling). The original asserted
    this by ``inspect.getsource(SessionLedgerService._register_source_internal)``
    and grepping for the literal strings. That check had been FAILING on master
    for the ``registered`` half — **and the behaviour had never regressed.** A
    refactor moved the insert-path return one call deeper, into
    ``duplicate_source_repair.insert_source_or_absorb_race`` (line 77), where the
    grep could not see it. The failure was a refactor artifact, not a defect.

    A behavioural property cannot be verified by scanning source text: the check
    breaks on refactors that preserve behaviour, and would equally PASS on a
    function that merely mentioned the string in a comment. The intent is
    preserved and the method replaced — this drives the real seam against the
    fixture store and asserts the RETURNED envelopes.

    The ``action`` key matters because the platform's result-contract validator
    treats a top-level ``action`` as ``action_status``; ``existed``/``registered``
    are not valid ActionStatus values and would trip
    ``result_status_not_completed``.
    """
    from ananta.services.session_ledger_service.service import SessionLedgerService  # noqa: PLC0415

    class _ServiceStub:
        """Carries only what ``_register_source_internal`` touches: ``_repository``."""

        def __init__(self, repository: SessionLedgerRepository) -> None:
            self._repository = repository

    tables: dict[str, list[dict[str, Any]]] = {}
    stub = _ServiceStub(_repo(tables))
    args = {"source_kind": IngestSourceKind.CODEX_LOCAL.value, "root_uri": "file:///tmp/rs-probe/"}

    first = SessionLedgerService._register_source_internal(cast("Any", stub), **args)
    second = SessionLedgerService._register_source_internal(cast("Any", stub), **args)

    _check(
        first.get("outcome") == "registered",
        f"register_source INSERT path returns outcome=registered (got {first.get('outcome')!r})",
    )
    _check(
        second.get("outcome") == "existed",
        f"register_source IDEMPOTENT hit returns outcome=existed (got {second.get('outcome')!r})",
    )
    _check(
        first.get("source_id") and first.get("source_id") == second.get("source_id"),
        "both calls name the SAME source_id — idempotent on (source_kind, root_uri)",
    )
    _check(
        "action" not in first and "action" not in second,
        "neither envelope uses the 'action' key (it would be read as action_status)",
    )


def main() -> int:
    print("=== list_sessions_m17_filters smoke ===")
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
