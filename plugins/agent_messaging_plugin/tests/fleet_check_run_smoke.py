#!/usr/bin/env python3
"""Smoke the state-backed replacement for the two fleet markdown logs.

The fake records exact state-interface payloads and performs the ordered
cursor page used in production, so a green run proves the structured fields,
workstream filter, newest-first ordering, and continuation cursor rather than
only a happy-path wrapper call.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_messaging_plugin.fleet_check_run_verbs import (  # noqa: E402
    recent_fleet_liveness_runs,
    recent_fleet_progress_runs,
    record_fleet_liveness_run,
    record_fleet_progress_run,
)
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_FLEET_LIVENESS_RUN,
    TABLE_FLEET_PROGRESS_RUN,
    get_session_lifecycle_schema_definition,
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


class _State:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.writes: list[dict[str, Any]] = []

    def write_state(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        table = str(payload["table"])
        row = dict(payload["record"])
        row["id"] = f"row-{len(self.tables.get(table, [])) + 1}"
        self.tables.setdefault(table, []).append(row)
        self.writes.append({"table": table, "record": row})
        return {"action_status": "completed", "data": {}}

    def query_ordered(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        rows = list(self.tables.get(str(payload["table"]), []))
        filters = payload.get("filters", {})
        rows = [
            row for row in rows
            if all(row.get(str(column)) == value for column, value in filters.items())
        ]
        order = payload["order_by"]
        timestamp = str(order[0][0])
        rows.sort(key=lambda row: (str(row[timestamp]), str(row["id"])), reverse=True)
        after = payload.get("after")
        if isinstance(after, list):
            cursor = (str(after[0]), str(after[1]))
            rows = [row for row in rows if (str(row[timestamp]), str(row["id"])) < cursor]
        return {"action_status": "completed", "data": {"records": rows[:int(payload["limit"])]}}


def _liveness(state: _State, observed_at: str, outcome: str = "clear") -> None:
    result = record_fleet_liveness_run(
        state, observed_at=observed_at, checked={"fleet_status": "read"}, stuck=[], actions=[],
        outcome=outcome, role_inbox_drain={"status": "clear"}, sleep_check={"status": "clear"},
        escalations=[],
    )
    _check(result == {"status": "recorded"}, "Phase-A append returns recorded")


def test_liveness_page_and_cursor() -> None:
    state = _State()
    _liveness(state, "2026-09-08T10:00:00+00:00")
    _liveness(state, "2026-09-08T10:01:00+00:00", "recovered lane")
    page = recent_fleet_liveness_runs(state, limit=1)
    _check(page["entries"][0]["outcome"] == "recovered lane", "newest Phase-A run is first")
    _check(page["truncated"] is True and page["next_cursor"] is not None, "Phase-A page exposes continuation")
    cursor = page["next_cursor"]
    assert isinstance(cursor, dict)
    older = recent_fleet_liveness_runs(
        state, limit=1, after_observed_at=str(cursor["timestamp"]), after_id=str(cursor["id"]),
    )
    _check(older["entries"][0]["outcome"] == "clear", "Phase-A cursor reaches the older row without repeat")
    recorded = state.writes[0]["record"]
    _check(recorded["role_inbox_drain"] == {"status": "clear"} and recorded["sleep_check"] == {"status": "clear"}, "clear-path drain and sleep evidence are persisted")


def test_progress_filter_and_phase_a_reference() -> None:
    state = _State()
    for workstream_id, reviewed_at in (("WS1", "2026-09-08T10:00:00+00:00"), ("WS2", "2026-09-08T10:01:00+00:00"), ("WS1", "2026-09-08T10:02:00+00:00")):
        result = record_fleet_progress_run(
            state, reviewed_at=reviewed_at, workstream_id=workstream_id, objective_citation="rul_test",
            metrics={"resolved": 1}, delta={"resolved": 1}, assessment="measured", recommendation="continue",
            independent_critique={"status": "not_due"}, escalations=[], phase_a_run_id="flr-1",
        )
        _check(result == {"status": "recorded"}, "Phase-B append returns recorded")
    page = recent_fleet_progress_runs(state, workstream_id="WS1", limit=64)
    _check(page["returned"] == 2 and all(row["workstream_id"] == "WS1" for row in page["entries"]), "workstream filter excludes other streams")
    _check(page["entries"][0]["phase_a_run_id"] == "flr-1", "Phase-B preserves cited Phase-A run id")


def test_invalid_payload_is_refused() -> None:
    try:
        record_fleet_liveness_run(
            _State(), observed_at="", checked={}, stuck=[], actions=[], outcome="clear",
            role_inbox_drain={}, sleep_check={}, escalations=[],
        )
        _check(False, "empty timestamp raises VerbError")
    except VerbError as exc:
        _check(exc.code == "missing_argument", "empty timestamp fails loud with missing_argument")
    try:
        recent_fleet_progress_runs(_State(), workstream_id="WS1", after_reviewed_at="2026-09-08T10:00:00+00:00")
        _check(False, "partial cursor raises VerbError")
    except VerbError as exc:
        _check(exc.code == "invalid_cursor", "partial cursor fails loud rather than skipping rows")


def test_plugin_transport() -> None:
    state = _State()
    plugin = object.__new__(AgentMessagingPlugin)
    plugin._get_state_service = lambda: state  # type: ignore[method-assign]
    recorded = plugin.record_fleet_liveness_run({"parameters": {
        "observed_at": "2026-09-08T10:00:00+00:00", "checked": {"fleet_status": "read"}, "stuck": [],
        "actions": [], "outcome": "clear", "role_inbox_drain": {"status": "clear"},
        "sleep_check": {"status": "clear"}, "escalations": [],
    }}, {})
    _check(recorded.get("action_status") == "completed", "plugin exposes Phase-A write verb")
    queried = plugin.recent_fleet_liveness_runs({"parameters": {"limit": 1}}, {})
    _check(queried.get("action_status") == "completed" and queried["data"]["returned"] == 1, "plugin exposes Phase-A recent query verb")


def test_schema_registration() -> None:
    tables = get_session_lifecycle_schema_definition().tables
    _check(
        TABLE_FLEET_LIVENESS_RUN in tables and TABLE_FLEET_PROGRESS_RUN in tables,
        "schema definition installs both deployment-native fleet-run tables",
    )


def main() -> int:
    test_liveness_page_and_cursor()
    test_progress_filter_and_phase_a_reference()
    test_invalid_payload_is_refused()
    test_plugin_transport()
    test_schema_registration()
    print(f"fleet_check_run_smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
