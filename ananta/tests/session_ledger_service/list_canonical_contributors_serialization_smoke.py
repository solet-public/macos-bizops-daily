#!/usr/bin/env python3
"""list_canonical_contributors serializer smoke (2026-08-16, lane-fix-small-defects).

``SessionLedgerService.list_canonical_contributors`` (the EDGE-registered
service wrapper) used to pass the repository's contributor rows straight
through. The repository DELIBERATELY returns naive ``datetime`` objects for
``first_event_at`` / ``last_event_at`` (the pre-migration ``_fetch_all``
return-type contract — see
``ananta/tests/llm/session_ledger/list_canonical_contributors_migration_smoke.py::
test_datetime_return_type_parsed_back``, which this smoke does NOT touch or
weaken), but a raw ``datetime`` has no serialization step of its own before
hitting the EDGE process's JSON envelope — measured live (lane-ak,
2026-08-16): "raw datetime hits the JSON serializer".

This smoke proves the SERVICE-layer fix: ``_naive_utc_to_iso`` converts each
contributor's two timestamp fields to an explicit-offset ISO string
(``+00:00``, never a bare naive isoformat — the defect class named in this
repair's dispatch memo) at the wrapper seam, while the repository layer
underneath keeps returning naive datetimes unchanged.

Run::

    .venv/bin/python3 \\
      ananta/tests/session_ledger_service/list_canonical_contributors_serialization_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.services.session_ledger_service.service import SessionLedgerService  # noqa: E402

_passed = 0
_failed: list[str] = []


def _expect(condition: bool, message: str) -> None:
    global _passed  # noqa: PLW0603
    if condition:
        _passed += 1
        print(f"PASS: {message}")
    else:
        _failed.append(message)
        print(f"FAIL: {message}", file=sys.stderr)


def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    for col, cond in filters.items():
        val = row.get(col)
        if isinstance(cond, dict):
            op = cond.get("op")
            if op == "is_null" and val is not None:
                return False
            if op == "is_not_null" and val is None:
                return False
        elif isinstance(cond, list):
            if val not in cond:
                return False
        elif val != cond:
            return False
    return True


def _serialize(value: Any) -> Any:
    """Mirror the real query_state path: naive-UTC datetime cell -> offset-less ISO."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat()
    return value


class _Shim:
    """Filter-honoring query_state stand-in, matching the migration smoke's own shim."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._t = tables

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        flt = cast("dict[str, Any]", query.get("filters") or {})
        rows = [
            {k: _serialize(v) for k, v in r.items()}
            for r in self._t.get(str(query["table"]), [])
            if _matches(r, flt)
        ]
        return {
            "action_status": "completed",
            "data": {"records": rows},
            "actions": [],
            "error": None,
        }


_NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)
_PLANTED_TS = datetime(2026, 6, 14, 10, 0, 0, tzinfo=UTC)


def _session_row() -> dict[str, Any]:
    return {
        "id": "les_canonical",
        "source_id": "src_local",
        "external_session_id": "ext_T1",
        "vendor": "claude_code",
        "vendor_session_label": "les_canonical",
        "project_path": "/p",
        "first_event_at": _PLANTED_TS,
        "last_event_at": _PLANTED_TS,
        "event_count": 3,
        "canonical_external_session_id": None,
        "is_deleted": 0,
    }


def _source_row() -> dict[str, Any]:
    return {
        "id": "src_local",
        "source_kind": "claude_code_local",
        "root_uri": "/r/src_local",
        "enabled": True,
        "is_deleted": 0,
    }


def _build_service() -> SessionLedgerService:
    tables = {"session": [_session_row()], "source": [_source_row()]}
    repository = SessionLedgerRepository(
        state_service=cast("Any", _Shim(tables)), clock=lambda: _NOW,
    )
    svc = SessionLedgerService.__new__(SessionLedgerService)
    svc._repository = repository  # type: ignore[attr-defined]  # noqa: SLF001
    return svc


def test_service_wrapper_returns_explicit_offset_iso_strings() -> None:
    svc = _build_service()
    result = svc.list_canonical_contributors(session_id="les_canonical")
    contributors = cast("list[dict[str, Any]]", result["contributors"])
    _expect(len(contributors) == 1, "one contributor projected from the fixture")
    row = contributors[0]
    first = row["first_event_at"]
    last = row["last_event_at"]
    _expect(isinstance(first, str), f"first_event_at is a str at the service seam (got {type(first).__name__})")
    _expect(isinstance(last, str), f"last_event_at is a str at the service seam (got {type(last).__name__})")
    _expect(
        isinstance(first, str) and (first.endswith("+00:00") or first.endswith("Z")),
        f"first_event_at carries an explicit UTC offset, not a bare naive isoformat (got {first!r})",
    )
    _expect(
        isinstance(first, str) and datetime.fromisoformat(first) == _PLANTED_TS,
        "first_event_at round-trips to the planted instant",
    )
    _expect(
        isinstance(last, str) and datetime.fromisoformat(last) == _PLANTED_TS,
        "last_event_at round-trips to the planted instant",
    )


def test_repository_layer_still_returns_naive_datetime_unchanged() -> None:
    """The repository's own contract (Architect's catch) must stay intact —
    this smoke's fix lives at the service seam, not the repository."""
    tables = {"session": [_session_row()], "source": [_source_row()]}
    repository = SessionLedgerRepository(
        state_service=cast("Any", _Shim(tables)), clock=lambda: _NOW,
    )
    raw = repository.list_canonical_contributors(session_id="les_canonical")
    raw_contributors = cast("list[dict[str, Any]]", raw["contributors"])
    raw_first = raw_contributors[0]["first_event_at"]
    _expect(
        isinstance(raw_first, datetime) and raw_first.tzinfo is None,
        f"repository layer still returns a NAIVE datetime, untouched by this repair (got {raw_first!r})",
    )


def main() -> int:
    test_service_wrapper_returns_explicit_offset_iso_strings()
    test_repository_layer_still_returns_naive_datetime_unchanged()
    if _failed:
        print(f"\n{len(_failed)} FAILURE(S) of {_passed + len(_failed)} checks", file=sys.stderr)
        return 1
    print(f"\nlist_canonical_contributors_serialization_smoke OK: {_passed} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
