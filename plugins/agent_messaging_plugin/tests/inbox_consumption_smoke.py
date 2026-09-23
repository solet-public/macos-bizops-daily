#!/usr/bin/env python3
"""Unit smoke for ``inbox_consumption_store.py`` / ``inbox_consumption_verbs.py``
(CDX-06 part C, the honesty field).

Proves, against a recording state double (not an envelope fake — records are
stored verbatim, so a field the verb forgets to pass is simply absent on
read):
  - a report round-trips through the store and reads back
  - `resolved=False` (never a raised error, never a defaulted True) is the
    shape for a session that has never reported — the entire point of this
    table
  - the unresolved shape carries the SAME keys as the resolved one, so a
    caller cannot KeyError its way through a legitimate `resolved: false`
  - `pending_found_at`/`pending_reason` are genuinely optional and round-trip
    as None when omitted, never coerced to a default
  - `pending_reason` without `pending_found_at` is refused (`missing_argument`)
  - an unknown `reporter_surface` is refused (`unknown_reporter_surface`),
    same closed vocabulary as `report_context_status`
  - a later report OVERWRITES the row (latest-state, not a history table)

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/inbox_consumption_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_messaging_plugin.inbox_consumption_verbs import (  # noqa: E402
    report_inbox_consumption,
    session_inbox_consumption_status,
)
from agent_messaging_plugin.session_lifecycle_verbs import VerbError  # noqa: E402

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


class _RecordingState:
    """Stores what it is given and hands it back verbatim -- a field the
    verb forgets to pass is simply absent on read, not silently defaulted."""

    def __init__(self) -> None:
        self.rows_by_table: dict[str, dict[str, dict[str, Any]]] = {}

    def upsert_state(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = dict(payload["record"])
        table = str(payload["table"])
        self.rows_by_table.setdefault(table, {})[record["agent_instance_id"]] = record
        return {"action_status": "completed", "data": {}}

    def query_state(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        table = str(payload["table"])
        filters = payload["filters"]
        rows = self.rows_by_table.get(table, {}).values()
        matches = [row for row in rows if all(row.get(column, 0 if column == "is_deleted" else None) == value for column, value in filters.items())]
        return {"action_status": "completed", "data": {"records": matches}}

    def add_peer_binding(self, *, agent_instance_id: str, agent_session_id: str) -> None:
        self.rows_by_table.setdefault("peer_binding", {})[agent_instance_id] = {"agent_instance_id": agent_instance_id, "agent_session_id": agent_session_id, "is_deleted": 0}


def _report(state: _RecordingState, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "agent_instance_id": "agi-test",
        "runtime": "codex",
        "checked_at": "2026-08-24T18:00:00+00:00",
    }
    kwargs.update(overrides)
    return report_inbox_consumption(state, **kwargs)


def test_report_round_trips() -> None:
    state = _RecordingState()
    _report(state)
    out = session_inbox_consumption_status(state, agent_instance_id="agi-test")
    _check(out["resolved"] is True, "a reported check reads back as resolved")
    _check(out["runtime"] == "codex", "runtime survives the round trip")
    _check(out["checked_at"] == "2026-08-24T18:00:00+00:00", "checked_at survives the round trip")
    _check(
        out["pending_found_at"] is None,
        "no pending_found_at was reported -> None, not fabricated",
    )
    _check(
        out["pending_reason"] is None,
        "no pending_reason was reported -> None, not fabricated",
    )


def test_unresolved_shape_matches_resolved_keys() -> None:
    """A caller must never KeyError its way through a legitimate resolved:false."""
    state = _RecordingState()
    unresolved_out = session_inbox_consumption_status(state, agent_instance_id="never-reported")
    _report(state, agent_instance_id="agi-other")
    resolved_out = session_inbox_consumption_status(state, agent_instance_id="agi-other")
    _check(
        set(unresolved_out.keys()) == set(resolved_out.keys()),
        "the unresolved shape carries the exact same keys as the resolved one",
    )


def test_unresolved_is_never_an_error_and_never_defaults_true() -> None:
    state = _RecordingState()
    out = session_inbox_consumption_status(state, agent_instance_id="agi-never-seen")
    _check(out["resolved"] is False, "a session with no report reads back resolved=False")
    _check(
        isinstance(out.get("resolution_error"), str) and out["resolution_error"],
        "resolved=False carries an honest resolution_error, not a silent gap",
    )
    _check(out["checked_at"] is None, "unresolved checked_at is None, never an estimated timestamp")


def test_pending_fields_round_trip_when_present() -> None:
    state = _RecordingState()
    _report(
        state,
        agent_instance_id="agi-pending",
        pending_found_at="2026-08-24T18:05:00+00:00",
        pending_reason="unread peer mail",
        reporter_surface="vendored",
        agent_session_id="ases-agi-pending",
    )
    out = session_inbox_consumption_status(state, agent_instance_id="agi-pending")
    _check(out["pending_found_at"] == "2026-08-24T18:05:00+00:00", "pending_found_at round-trips")
    _check(out["pending_reason"] == "unread peer mail", "pending_reason round-trips")
    _check(out["reporter_surface"] == "vendored", "reporter_surface round-trips")
    _check(out["agent_session_id"] == "ases-agi-pending", "agent_session_id round-trips")


def test_pending_reason_without_pending_found_at_is_refused() -> None:
    state = _RecordingState()
    try:
        _report(state, agent_instance_id="agi-bad", pending_reason="orphaned reason")
        _check(False, "pending_reason without pending_found_at raises VerbError")
    except VerbError as exc:
        _check(exc.code == "missing_argument", "the refusal code is missing_argument")


def test_unknown_reporter_surface_is_refused() -> None:
    state = _RecordingState()
    try:
        _report(state, agent_instance_id="agi-bad2", reporter_surface="made_up_surface")
        _check(False, "an invented reporter_surface raises VerbError")
    except VerbError as exc:
        _check(
            exc.code == "unknown_reporter_surface",
            "the refusal code is unknown_reporter_surface",
        )


def test_empty_agent_instance_id_is_refused_on_report_and_read() -> None:
    state = _RecordingState()
    try:
        _report(state, agent_instance_id="   ")
        _check(False, "report_inbox_consumption refuses a blank agent_instance_id")
    except VerbError as exc:
        _check(exc.code == "missing_argument", "report refusal code is missing_argument")
    try:
        session_inbox_consumption_status(state, agent_instance_id="")
        _check(False, "session_inbox_consumption_status refuses a blank agent_instance_id")
    except VerbError as exc:
        _check(exc.code == "missing_argument", "read refusal code is missing_argument")


def test_later_report_overwrites_not_accumulates() -> None:
    """Latest-state table, not a history: the second report for the same
    session REPLACES the first, it does not create a second row."""
    state = _RecordingState()
    _report(state, agent_instance_id="agi-latest", checked_at="2026-08-24T18:00:00+00:00")
    _report(
        state,
        agent_instance_id="agi-latest",
        checked_at="2026-08-24T18:10:00+00:00",
        pending_found_at="2026-08-24T18:10:00+00:00",
        pending_reason="second check found mail",
    )
    out = session_inbox_consumption_status(state, agent_instance_id="agi-latest")
    _check(
        out["checked_at"] == "2026-08-24T18:10:00+00:00",
        "the row reflects the SECOND (latest) check",
    )
    _check(
        out["pending_reason"] == "second check found mail",
        "the second check's pending fields survive",
    )
    _check(
        len(state.rows_by_table["inbox_consumption_status"]) == 1,
        "exactly one row exists for the session -- overwrite, not accumulation",
    )


def test_watcher_identity_resolves_report_via_agent_session_id() -> None:
    """Killing mutation: remove the GAU-07 join."""
    state = _RecordingState()
    _report(state, agent_instance_id="agi-ledger-reporter", agent_session_id="ases-stable-session")
    state.add_peer_binding(agent_instance_id="agi-watch-one-way-digest", agent_session_id="ases-stable-session")
    out = session_inbox_consumption_status(state, agent_instance_id="agi-watch-one-way-digest")
    _check(out["resolved"] is True, "a watcher-held identity resolves its ledger-keyed inbox report through GAU-07")


def main() -> int:
    test_report_round_trips()
    test_unresolved_shape_matches_resolved_keys()
    test_unresolved_is_never_an_error_and_never_defaults_true()
    test_pending_fields_round_trip_when_present()
    test_pending_reason_without_pending_found_at_is_refused()
    test_unknown_reporter_surface_is_refused()
    test_empty_agent_instance_id_is_refused_on_report_and_read()
    test_later_report_overwrites_not_accumulates()
    test_watcher_identity_resolves_report_via_agent_session_id()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
