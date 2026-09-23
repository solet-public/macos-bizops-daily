"""State-interface persistence for the two fleet-steward check phases.

These are append-only operational measurements, not a cache and not a
project-solet work-register projection.  The records deliberately live in the
agent-messaging plugin's deployment namespace: they describe one deployment's
liveness and its own stewardship decisions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_records

from .schema import TABLE_FLEET_LIVENESS_RUN, TABLE_FLEET_PROGRESS_RUN

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

MAX_FLEET_CHECK_RUNS = 64
"""Largest page a steward may request from either recent-run surface."""


def append_fleet_liveness_run(
    state: StateManagementInterface,
    *,
    observed_at: str,
    checked: dict[str, Any],
    stuck: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    outcome: str,
    role_inbox_drain: dict[str, Any],
    sleep_check: dict[str, Any],
    escalations: list[dict[str, Any]],
) -> None:
    """Append one complete Phase-A pass; callers never overwrite history."""
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_FLEET_LIVENESS_RUN,
                "record": {
                    "observed_at": observed_at,
                    "checked": checked,
                    "stuck": stuck,
                    "actions": actions,
                    "outcome": outcome,
                    "role_inbox_drain": role_inbox_drain,
                    "sleep_check": sleep_check,
                    "escalations": escalations,
                },
            },
        ),
        "append fleet_liveness_run",
    )


def append_fleet_progress_run(
    state: StateManagementInterface,
    *,
    reviewed_at: str,
    workstream_id: str,
    objective_citation: str,
    metrics: dict[str, Any],
    delta: dict[str, Any],
    assessment: str,
    recommendation: str,
    independent_critique: dict[str, Any],
    escalations: list[dict[str, Any]],
    phase_a_run_id: str | None,
) -> None:
    """Append one Phase-B assessment for one workstream in a review cycle."""
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_FLEET_PROGRESS_RUN,
                "record": {
                    "reviewed_at": reviewed_at,
                    "workstream_id": workstream_id,
                    "objective_citation": objective_citation,
                    "metrics": metrics,
                    "delta": delta,
                    "assessment": assessment,
                    "recommendation": recommendation,
                    "independent_critique": independent_critique,
                    "escalations": escalations,
                    "phase_a_run_id": phase_a_run_id,
                },
            },
        ),
        "append fleet_progress_run",
    )


def read_fleet_liveness_runs(
    state: StateManagementInterface,
    *,
    limit: int,
    after: list[str] | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Return a newest-first page and whether a follow-up page exists."""
    return _read_page(
        state,
        table=TABLE_FLEET_LIVENESS_RUN,
        timestamp_column="observed_at",
        filters={},
        limit=limit,
        after=after,
    )


def read_fleet_progress_runs(
    state: StateManagementInterface,
    *,
    workstream_id: str | None,
    limit: int,
    after: list[str] | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Return newest-first Phase-B records, optionally for one workstream."""
    filters: dict[str, Any] = {}
    if workstream_id is not None:
        filters["workstream_id"] = workstream_id
    return _read_page(
        state,
        table=TABLE_FLEET_PROGRESS_RUN,
        timestamp_column="reviewed_at",
        filters=filters,
        limit=limit,
        after=after,
    )


def _read_page(
    state: StateManagementInterface,
    *,
    table: str,
    timestamp_column: str,
    filters: dict[str, Any],
    limit: int,
    after: list[str] | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Read one bounded, tie-safe newest-first page through the state API."""
    capped = max(1, min(limit, MAX_FLEET_CHECK_RUNS))
    query: dict[str, Any] = {
        "table": table,
        "filters": filters,
        "order_by": [[timestamp_column, "desc"], ["id", "desc"]],
        "limit": capped + 1,
    }
    if after is not None:
        query["after"] = after
    rows = [dict(row) for row in require_records(
        state.query_ordered(AGENT_ROLE_BINDING_NAMESPACE, query),
    )]
    return rows[:capped], len(rows) > capped


__all__ = [
    "MAX_FLEET_CHECK_RUNS",
    "append_fleet_liveness_run",
    "append_fleet_progress_run",
    "read_fleet_liveness_runs",
    "read_fleet_progress_runs",
]
