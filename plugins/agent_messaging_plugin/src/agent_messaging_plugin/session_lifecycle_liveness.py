"""Liveness-report mechanics kept separate from lifecycle verb dispatch.

This module owns the two distinct facts introduced by D-4.2 through D-5.3:
an explicit self-report changes the reporting deadline and status provenance;
a passive heartbeat records only liveness telemetry and any failures it carried
forward.  Imports from ``session_lifecycle_verbs`` are deliberately deferred
inside the call path: that module owns the public error type and deadline
writer, while this module prevents the public verb registry from becoming the
complexity sink for liveness mechanics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ananta.interfaces.state_management_interface import StateManagementInterface
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed

from agent_messaging_plugin.schema import (
    LIFECYCLE_OVERDUE,
    TABLE_MANAGED_SESSION,
)
from agent_messaging_plugin.session_lifecycle_store import (
    SessionNotFoundError,
    StaleLifecycleStateError,
    read_managed_session,
    transition_lifecycle_state,
)


def report_alive(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    status: str,
    directed_by: str,
    status_note: str = "",
    heartbeat_failures_since_last: int = 0,
    heartbeat_failure_first_at: str | None = None,
    heartbeat_failure_last_reason: str = "",
) -> dict[str, Any]:
    """Accept an explicit status report or record a passive heartbeat."""
    if status == "heartbeat":
        return _report_passive_alive(
            state,
            agent_instance_id=agent_instance_id,
            failures_since_last=heartbeat_failures_since_last,
            failure_first_at=heartbeat_failure_first_at,
            failure_last_reason=heartbeat_failure_last_reason,
        )
    from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: PLC0415
        _REPORT_ALIVE_EDGE,
        VerbError,
    )
    to_state = _REPORT_ALIVE_EDGE.get(status)
    if to_state is None:
        raise VerbError(
            "unknown_status",
            f"report_alive status must be one of working|idle, got {status!r}.",
        )
    return _report_explicit_alive(
        state,
        agent_instance_id=agent_instance_id,
        to_state=to_state,
        directed_by=directed_by,
        status_note=status_note,
    )


def _report_passive_alive(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    failures_since_last: int,
    failure_first_at: str | None,
    failure_last_reason: str,
) -> dict[str, Any]:
    from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: PLC0415
        VerbError,
        _refuse_report_alive_on_ineligible_state,
    )
    try:
        row = read_managed_session(state, agent_instance_id)
    except SessionNotFoundError as exc:
        raise VerbError("session_not_found", str(exc)) from exc
    current = str(row.get("lifecycle_state") or "")
    _refuse_report_alive_on_ineligible_state(current)
    require_completed(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": agent_instance_id}},
            {
                "last_heartbeat_at": datetime.now(UTC).isoformat(),
                "heartbeat_failures_since_last": failures_since_last,
                # A healthy heartbeat clears a previous failure episode with
                # the column's typed NULL. Omitting the field would preserve a
                # stale failure timestamp and manufacture a frozen episode.
                "heartbeat_failure_first_at": failure_first_at,
                "heartbeat_failure_last_reason": failure_last_reason,
            },
        ),
        "record passive heartbeat",
    )
    return {"lifecycle_state": current, "recovered": False}


def _report_explicit_alive(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    to_state: str,
    directed_by: str,
    status_note: str,
) -> dict[str, Any]:
    from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: PLC0415
        REPORT_BY_SOURCE_EXPLICIT_SELF_REPORT,
        VerbError,
        _rearm_report_by,
        _refuse_report_alive_on_ineligible_state,
        _row_report_by_seconds,
        _write_explicit_status_source,
    )
    for attempt in range(2):
        try:
            row = read_managed_session(state, agent_instance_id)
        except SessionNotFoundError as exc:
            raise VerbError("session_not_found", str(exc)) from exc
        current = str(row.get("lifecycle_state") or "")
        _refuse_report_alive_on_ineligible_state(current)
        report_by_seconds = _row_report_by_seconds(row)
        if current == to_state:
            _rearm_report_by(
                state,
                agent_instance_id,
                report_by_seconds=report_by_seconds,
                source=REPORT_BY_SOURCE_EXPLICIT_SELF_REPORT,
            )
            _write_explicit_status_source(state, agent_instance_id)
            return {"lifecycle_state": current, "recovered": False}
        try:
            transition_lifecycle_state(
                state,
                agent_instance_id=agent_instance_id,
                from_state=current,
                to_state=to_state,
                directed_by=directed_by,
                reason=status_note or "report_alive",
            )
            _rearm_report_by(
                state,
                agent_instance_id,
                report_by_seconds=report_by_seconds,
                source=REPORT_BY_SOURCE_EXPLICIT_SELF_REPORT,
            )
            _write_explicit_status_source(state, agent_instance_id)
            return {"lifecycle_state": to_state, "recovered": current == LIFECYCLE_OVERDUE}
        except StaleLifecycleStateError as exc:
            if attempt == 1:
                raise VerbError("stale_lifecycle_state", str(exc)) from exc
    raise VerbError("stale_lifecycle_state", "report_alive lost the race twice.")
