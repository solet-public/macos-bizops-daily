"""Public contracts for durable fleet-liveness and progress-review records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .fleet_check_run_store import (
    MAX_FLEET_CHECK_RUNS,
    append_fleet_liveness_run,
    append_fleet_progress_run,
    read_fleet_liveness_runs,
    read_fleet_progress_runs,
)
from .session_lifecycle_verbs import VerbError

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface


def record_fleet_liveness_run(
    state: StateManagementInterface,
    *,
    observed_at: str,
    checked: object,
    stuck: object,
    actions: object,
    outcome: str,
    role_inbox_drain: object,
    sleep_check: object,
    escalations: object,
) -> dict[str, Any]:
    """Record one complete Phase-A pass, including the clear-path evidence."""
    _require_text("observed_at", observed_at)
    _require_text("outcome", outcome)
    append_fleet_liveness_run(
        state,
        observed_at=observed_at,
        checked=_require_object("checked", checked),
        stuck=_require_object_list("stuck", stuck),
        actions=_require_object_list("actions", actions),
        outcome=outcome,
        role_inbox_drain=_require_object("role_inbox_drain", role_inbox_drain),
        sleep_check=_require_object("sleep_check", sleep_check),
        escalations=_require_object_list("escalations", escalations),
    )
    return {"status": "recorded"}


def record_fleet_progress_run(
    state: StateManagementInterface,
    *,
    reviewed_at: str,
    workstream_id: str,
    objective_citation: str,
    metrics: object,
    delta: object,
    assessment: str,
    recommendation: str,
    independent_critique: object,
    escalations: object,
    phase_a_run_id: str | None = None,
) -> dict[str, Any]:
    """Record one Phase-B assessment, queryable by its stable workstream id."""
    for name, value in {
        "reviewed_at": reviewed_at,
        "workstream_id": workstream_id,
        "objective_citation": objective_citation,
        "assessment": assessment,
        "recommendation": recommendation,
    }.items():
        _require_text(name, value)
    append_fleet_progress_run(
        state,
        reviewed_at=reviewed_at,
        workstream_id=workstream_id,
        objective_citation=objective_citation,
        metrics=_require_object("metrics", metrics),
        delta=_require_object("delta", delta),
        assessment=assessment,
        recommendation=recommendation,
        independent_critique=_require_object("independent_critique", independent_critique),
        escalations=_require_object_list("escalations", escalations),
        phase_a_run_id=phase_a_run_id,
    )
    return {"status": "recorded"}


def recent_fleet_liveness_runs(
    state: StateManagementInterface,
    *,
    limit: object = MAX_FLEET_CHECK_RUNS,
    after_observed_at: str | None = None,
    after_id: str | None = None,
) -> dict[str, Any]:
    """Read a newest-first, cursor-paginated Phase-A telemetry page."""
    rows, truncated = read_fleet_liveness_runs(
        state,
        limit=_validated_limit(limit),
        after=_validated_cursor(after_observed_at, after_id),
    )
    return _page(rows, truncated=truncated, timestamp_column="observed_at")


def recent_fleet_progress_runs(
    state: StateManagementInterface,
    *,
    workstream_id: str | None = None,
    limit: object = MAX_FLEET_CHECK_RUNS,
    after_reviewed_at: str | None = None,
    after_id: str | None = None,
) -> dict[str, Any]:
    """Read a newest-first, cursor-paginated Phase-B page, optionally by stream."""
    if workstream_id is not None and not workstream_id.strip():
        raise VerbError("missing_argument", "workstream_id was supplied but empty; omit it to read all streams.")
    rows, truncated = read_fleet_progress_runs(
        state,
        workstream_id=workstream_id,
        limit=_validated_limit(limit),
        after=_validated_cursor(after_reviewed_at, after_id),
    )
    result = _page(rows, truncated=truncated, timestamp_column="reviewed_at")
    result["workstream_id"] = workstream_id
    return result


def _require_text(name: str, value: str) -> None:
    if not value.strip():
        raise VerbError("missing_argument", f"{name} must be a non-empty string.")


def _require_object(name: str, value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    raise VerbError("invalid_argument", f"{name} must be an object, not {type(value).__name__}.")


def _require_object_list(name: str, value: object) -> list[dict[str, Any]]:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return [dict(item) for item in value]
    raise VerbError("invalid_argument", f"{name} must be a list of objects.")


def _validated_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_FLEET_CHECK_RUNS:
        raise VerbError("invalid_limit", f"limit must be an integer from 1 through {MAX_FLEET_CHECK_RUNS}.")
    return value


def _validated_cursor(timestamp: str | None, row_id: str | None) -> list[str] | None:
    if timestamp is None and row_id is None:
        return None
    if not isinstance(timestamp, str) or not timestamp.strip() or not isinstance(row_id, str) or not row_id.strip():
        raise VerbError("invalid_cursor", "cursor requires both a non-empty timestamp and row id.")
    return [timestamp, row_id]


def _page(rows: list[dict[str, Any]], *, truncated: bool, timestamp_column: str) -> dict[str, Any]:
    next_cursor = None
    if truncated and rows:
        last = rows[-1]
        next_cursor = {"timestamp": last.get(timestamp_column), "id": last.get("id")}
    return {
        "entries": rows,
        "returned": len(rows),
        "truncated": truncated,
        "next_cursor": next_cursor,
    }


__all__ = [
    "record_fleet_liveness_run",
    "record_fleet_progress_run",
    "recent_fleet_liveness_runs",
    "recent_fleet_progress_runs",
]
