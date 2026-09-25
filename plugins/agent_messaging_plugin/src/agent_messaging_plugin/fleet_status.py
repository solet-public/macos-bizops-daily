"""Read-only, bounded fleet-status projection over the existing deployment ledger."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.schema import (
    COL_CONSUMED,
    COL_ESCALATED,
    TABLE_AGENT_ROLE_MESSAGE,
)
from ananta.llm.agent_messaging.state_results import require_records
from ananta.services.state_service.bounded_read import iter_table_rows

from .context_status_verbs import session_context_status
from .gauge_series import classify_gauge_series
from .schema import (
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_SPAWNING,
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_DEPENDENCY,
)
from .session_context_status_store import read_session_context_status_history
from .session_sweep import last_report_alive

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface


FLEET_STATUS_SCOPE_LANES: Final = "lanes"
FLEET_STATUS_SCOPE_ALL: Final = "all"
FLEET_STATUS_SCOPES: Final = frozenset({FLEET_STATUS_SCOPE_LANES, FLEET_STATUS_SCOPE_ALL})
FLEET_STATUS_MAX_SESSIONS: Final = 250
FLEET_STATUS_MAX_OWED_MESSAGES: Final = 100
# These are complete-set reads, not display windows: fleet_status must classify
# every active session and every armed dependency before applying its separate
# 250-session rendering bound. State-interface counts measured 2026-09-15:
# 1,017 active managed sessions, 999 active sessions without report_by, and
# one armed dependency. 10,000 leaves about 10x headroom for the two current
# managed-session populations and lets dependencies grow with the active fleet
# without borrowing the sibling full-ledger walk's 1,000,000-row assumption.
_ACTIVE_SESSION_READ_CEILING: Final = 10_000
_NO_CONTRACT_SESSION_READ_CEILING: Final = 10_000
_ARMED_DEPENDENCY_READ_CEILING: Final = 10_000
_ACTIVE_LIFECYCLE_STATES: Final = (
    LIFECYCLE_SPAWNING,
    LIFECYCLE_LIVE,
    LIFECYCLE_IDLE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
)


class FleetStatusError(ValueError):
    """A stable caller-visible fleet-status contract violation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def fleet_status(
    state: StateManagementInterface,
    *,
    scope: object = FLEET_STATUS_SCOPE_LANES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return one bounded, read-only classification of the active fleet.

    The no-contract population is deliberately read with the state interface's
    explicit ``is_null`` grammar.  It is inventory, not a report-or-die lane,
    and must never be silently folded into ``STALLED``.
    """
    selected_scope = _validated_scope(scope)
    observed_at = now or datetime.now(UTC)
    rows = _active_sessions(state)
    no_contract_rows = _no_contract_sessions(state)
    dependencies = _armed_dependencies(state)
    classifications = _classifications(rows, dependencies, observed_at)
    scoped_rows = _scoped_rows(rows, selected_scope)
    rendered_sessions = [
        _session_view(row, classifications[str(row.get("agent_instance_id") or "")], state, observed_at)
        for row in scoped_rows[:FLEET_STATUS_MAX_SESSIONS]
    ]
    owed_messages = _owed_role_messages(state)
    class_counts = Counter(item["classification"] for item in classifications.values())
    for classification in ("working", "STALLED", "no-contract", "retiring", "idle", "holding"):
        class_counts.setdefault(classification, 0)
    return {
        "as_of": observed_at.isoformat(),
        "scope": selected_scope,
        "class_counts": dict(sorted(class_counts.items())),
        "sessions": rendered_sessions,
        "sessions_truncated": max(0, len(scoped_rows) - len(rendered_sessions)),
        "unregistered": _unregistered_summary(no_contract_rows),
        "owed_messages": owed_messages[:FLEET_STATUS_MAX_OWED_MESSAGES],
        # This is an overflow INDICATOR (0 or 1), not an exact backlog count.
        # _owed_role_messages reads one extra row to establish whether more
        # messages exist without consenting to a full-table walk.
        "owed_messages_truncated": max(0, len(owed_messages) - FLEET_STATUS_MAX_OWED_MESSAGES),
        # Measurement: direct wake persistence/replay is code-retired; only
        # role envelopes are authoritative enough for this v1 owed set.
        "legacy_direct_delivery_unknown": True,
    }


def _validated_scope(scope: object) -> str:
    if isinstance(scope, str) and scope in FLEET_STATUS_SCOPES:
        return scope
    raise FleetStatusError(
        "invalid_scope",
        "fleet_status scope must be 'lanes' or 'all'.",
    )


def _active_sessions(state: StateManagementInterface) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in iter_table_rows(
            state,
            namespace=AGENT_ROLE_BINDING_NAMESPACE,
            table=TABLE_MANAGED_SESSION,
            filters={"lifecycle_state": list(_ACTIVE_LIFECYCLE_STATES)},
            ceiling=_ACTIVE_SESSION_READ_CEILING,
            reason=(
                "fleet_status reads the complete active managed-session population "
                "(1,017 rows measured 2026-09-15; 10,000-row ceiling leaves about 10x headroom)"
            ),
        )
    ]


def _no_contract_sessions(state: StateManagementInterface) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in iter_table_rows(
            state,
            namespace=AGENT_ROLE_BINDING_NAMESPACE,
            table=TABLE_MANAGED_SESSION,
            filters={
                "lifecycle_state": list(_ACTIVE_LIFECYCLE_STATES),
                "report_by": {"op": "is_null"},
            },
            ceiling=_NO_CONTRACT_SESSION_READ_CEILING,
            reason=(
                "fleet_status reads the complete contract-less active managed-session population "
                "(999 rows measured 2026-09-15; 10,000-row ceiling leaves about 10x headroom)"
            ),
        )
    ]


def _armed_dependencies(state: StateManagementInterface) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in iter_table_rows(
        state,
        namespace=AGENT_ROLE_BINDING_NAMESPACE,
        table=TABLE_SESSION_DEPENDENCY,
        filters={"fired_at": {"op": "is_null"}},
        ceiling=_ARMED_DEPENDENCY_READ_CEILING,
        reason=(
            "fleet_status reads the complete armed-dependency set "
            "(1 row measured 2026-09-15; 10,000 rows accommodate dependency growth with the active fleet)"
        ),
    ):
        waiter = str(row.get("waiter_instance_id") or "")
        if waiter:
            grouped[waiter].append(
                {
                    "id": str(row.get("id") or ""),
                    "kind": str(row.get("condition_kind") or ""),
                    "ref": str(row.get("condition_ref") or ""),
                },
            )
    return dict(grouped)


def _classifications(
    rows: list[dict[str, Any]],
    dependencies: dict[str, list[dict[str, str]]],
    now: datetime,
) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("agent_instance_id") or ""): _classify(row, dependencies, now)
        for row in rows
    }


def _classify(
    row: dict[str, Any],
    dependencies: dict[str, list[dict[str, str]]],
    now: datetime,
) -> dict[str, Any]:
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if row.get("report_by") is None:
        return {"classification": "no-contract", "reason": "report_by is null", "holds": []}
    if str(row.get("worktree_disposition") or ""):
        return {"classification": "retiring", "reason": "worktree disposition is recorded", "holds": []}
    holds = dependencies.get(agent_instance_id, [])
    if holds:
        return {"classification": "holding", "reason": "armed session dependency", "holds": holds}
    lifecycle_state = str(row.get("lifecycle_state") or "")
    if lifecycle_state in {LIFECYCLE_IDLE, LIFECYCLE_PARKED}:
        return {"classification": "idle", "reason": f"lifecycle_state is {lifecycle_state}", "holds": []}
    report_by = _timestamp(row.get("report_by"))
    if report_by is not None and report_by <= now:
        return {"classification": "STALLED", "reason": "report_by is past due", "holds": []}
    return {"classification": "working", "reason": "report_by is still in the future", "holds": []}


def _scoped_rows(rows: list[dict[str, Any]], scope: str) -> list[dict[str, Any]]:
    selected = rows if scope == FLEET_STATUS_SCOPE_ALL else [
        row for row in rows if row.get("report_by") is not None
    ]
    return sorted(selected, key=lambda row: (str(row.get("lane_id") or ""), str(row.get("id") or "")))


def _session_view(
    row: dict[str, Any],
    classification: dict[str, Any],
    state: StateManagementInterface,
    now: datetime,
) -> dict[str, Any]:
    agent_instance_id = str(row.get("agent_instance_id") or "")
    return {
        "agent_instance_id": agent_instance_id,
        "lane_id": row.get("lane_id"),
        "role_name": row.get("role_name"),
        "model": row.get("model"),
        "effort": row.get("effort"),
        "dispatch_kind": row.get("dispatch_kind"),
        "host": row.get("host"),
        "ledger_state": row.get("lifecycle_state"),
        "classification": classification["classification"],
        "classification_reason": classification["reason"],
        "report_by": _string_or_none(row.get("report_by")),
        "holds": classification["holds"],
        "gauge": _gauge_view(state, agent_instance_id, row, now),
    }


def _gauge_view(
    state: StateManagementInterface,
    agent_instance_id: str,
    lifecycle_row: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    status = session_context_status(state, agent_instance_id=agent_instance_id)
    if status.get("resolved") is not True:
        return {"status": "no gauge"}
    history, _truncated = read_session_context_status_history(state, agent_instance_id, limit=1)
    newest = _timestamp(history[0].get("recorded_at")) if history else None
    series_state, series_reason = classify_gauge_series(
        newest_recorded_at=newest,
        last_alive=last_report_alive(lifecycle_row),
        lifecycle_readable=True,
        now=now,
    )
    return {
        "status": "reported",
        "current_tokens": status["current_tokens"],
        "ceiling": status["ceiling"],
        "fraction": status["fraction"],
        "rotation_due": status["rotation_due"],
        "measured_at": status["measured_at"],
        "series_state": series_state,
        "series_reason": series_reason,
    }


def _owed_role_messages(state: StateManagementInterface) -> list[dict[str, Any]]:
    rows = require_records(
        state.query_ordered(
            "core",
            {
                "table": TABLE_AGENT_ROLE_MESSAGE,
                "filters": {"important": True, COL_CONSUMED: False, COL_ESCALATED: False},
                # Owed messages are FIFO: the longest-waiting message is first,
                # matching the authoritative role-delivery drain contract.
                "order_by": [["created_at", "asc"], ["id", "asc"]],
                "limit": FLEET_STATUS_MAX_OWED_MESSAGES + 1,
                # The state-interface default cap is 100.  This declared,
                # fixed one-row over-read is bounded and establishes overflow.
                "unbounded": True,
            },
        ),
    )
    return [_owed_message_view(row) for row in rows]


def _owed_message_view(row: dict[str, object]) -> dict[str, Any]:
    return {
        "message_id": row.get("message_id"),
        "thread_id": row.get("thread_id"),
        "recipient_kind": row.get("recipient_kind"),
        "recipient_key": row.get("recipient_key"),
        "sender_instance_id": row.get("sender_agent_instance_id"),
        "sender_session_label": row.get("sender_session_label"),
        "created_at": _string_or_none(row.get("created_at")),
        "emit_count": row.get("emit_count"),
        "escalated": row.get("escalated"),
        "escalation_reason": row.get("escalation_reason"),
    }


def _unregistered_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    timestamps = sorted(
        value for row in rows if (value := _string_or_none(row.get("created_at"))) is not None
    )
    return {
        "count": len(rows),
        "oldest_created_at": timestamps[0] if timestamps else None,
        "newest_created_at": timestamps[-1] if timestamps else None,
    }


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _string_or_none(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) and value else None


__all__ = [
    "FLEET_STATUS_SCOPE_ALL",
    "FLEET_STATUS_SCOPE_LANES",
    "FleetStatusError",
    "fleet_status",
]
