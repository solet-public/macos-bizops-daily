"""GAU-15 item 4 — state-layer primitives for the gauge tamper canary.

Two tables, each answering one binding constraint:

* `gauge_canary_registry` — constraint (c): the canary's provenance mark lives
  at the STORE PLANE, in its own table, so operational consumers can filter
  synthetic sessions out of fleet decisions. Never a column on the gauge row,
  because the detector reads that row and constraint (b) binds harder.
* `gauge_canary_tamper` — constraint (d): every arrest is audited with who
  ordered it, the bounded window, and the alarm it expected, so that any alarm
  is mechanically attributable to a scheduled tamper or to a real fault.

Nothing here is reachable from the detection path. The sweep's gauge legs read
`managed_session` and `session_context_status`; neither of those reads touches
either table, so a canary is an ordinary session to the detector — which is the
whole reason the canary can test it at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_records

from .schema import TABLE_GAUGE_CANARY_REGISTRY, TABLE_GAUGE_CANARY_TAMPER

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

MAX_TAMPER_ROWS = 64
"""Hard ceiling on one tamper-log read, stated rather than silent.

Below the state layer's own 100-row `query_ordered` cap, which refuses a larger
page without explicit `unbounded=True` consent — the same ceiling that sized
the notice record's retention.
"""

_COL_AGENT_INSTANCE_ID = "agent_instance_id"
_COL_ARREST_FROM = "arrest_from"
_COL_RETIRED_AT = "retired_at"


class CanaryError(Exception):
    """A canary operation was refused. Carries a code for the verb layer."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def register_canary(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    purpose: str,
    registered_by: str,
) -> dict[str, Any]:
    """Mark one identity as a canary, or raise if it already is.

    Refusing a duplicate rather than upserting is deliberate: a second
    registration under a different `registered_by` would silently rewrite who
    is answerable for a synthetic identity in the fleet, and provenance that
    can be overwritten without trace is not provenance.
    """
    if read_canary(state, agent_instance_id) is not None:
        raise CanaryError(
            "already_registered",
            f"{agent_instance_id!r} is already a registered canary; retire it "
            "before re-registering, so the change of ownership is on the record.",
        )
    record = {
        _COL_AGENT_INSTANCE_ID: agent_instance_id,
        "purpose": purpose,
        "registered_at": datetime.now(UTC).isoformat(),
        "registered_by": registered_by,
        _COL_RETIRED_AT: None,
    }
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_GAUGE_CANARY_REGISTRY, "record": record},
        ),
        "register gauge canary",
    )
    return record


def read_canary(
    state: StateManagementInterface, agent_instance_id: str,
) -> dict[str, Any] | None:
    """This identity's registry row, or ``None`` if it is not a canary.

    Returns retired rows too. A caller asking "was this a canary" while reading
    old alarms needs the retired ones; a caller asking "is it active now" reads
    `retired_at`, which is why that distinction is a column rather than a
    deletion.
    """
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_GAUGE_CANARY_REGISTRY,
                "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id},
                "order_by": [["registered_at", "desc"], ["id", "desc"]],
                "limit": 1,
            },
        ),
    )
    return rows[0] if rows else None


def is_active_canary(state: StateManagementInterface, agent_instance_id: str) -> bool:
    """Whether this identity is a canary that has NOT been retired."""
    row = read_canary(state, agent_instance_id)
    return row is not None and not row.get(_COL_RETIRED_AT)


def list_canaries(
    state: StateManagementInterface, *, include_retired: bool = False,
) -> list[dict[str, Any]]:
    """Every registered canary — the join operational consumers make.

    ``include_retired`` defaults to False because the operational question
    ("which identities should I exclude from fleet decisions right now") is
    about active ones, while the forensic question is explicit.
    """
    filters: dict[str, Any] = {}
    if not include_retired:
        filters[_COL_RETIRED_AT] = {"op": "is_null"}
    return require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_GAUGE_CANARY_REGISTRY,
                "filters": filters,
                "order_by": [["registered_at", "desc"], ["id", "desc"]],
                "limit": MAX_TAMPER_ROWS,
            },
        ),
    )


def retire_canary(
    state: StateManagementInterface, *, agent_instance_id: str,
) -> None:
    """Stand a canary down without forgetting it ever existed."""
    if read_canary(state, agent_instance_id) is None:
        raise CanaryError(
            "not_a_canary", f"{agent_instance_id!r} is not a registered canary.",
        )
    require_completed(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_GAUGE_CANARY_REGISTRY,
                "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id},
            },
            {_COL_RETIRED_AT: datetime.now(UTC).isoformat()},
        ),
        "retire gauge canary",
    )


def record_tamper(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    directed_by: str,
    arrest_from: str,
    arrest_until: str,
    expected_notice_type: str,
    reason: str,
) -> dict[str, Any]:
    """Log one audited arrest window. RAISES rather than degrading.

    Unlike the notice record — which is best-effort bookkeeping riding an
    operational loop — this write IS the act. An arrest whose audit row failed
    to land would produce alarms nobody can attribute, which is precisely the
    failure constraint (d) exists to prevent, so a failure here must stop the
    tamper rather than proceed unlogged.
    """
    record = {
        _COL_AGENT_INSTANCE_ID: agent_instance_id,
        "directed_by": directed_by,
        _COL_ARREST_FROM: arrest_from,
        "arrest_until": arrest_until,
        "expected_notice_type": expected_notice_type,
        "reason": reason,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_GAUGE_CANARY_TAMPER, "record": record},
        ),
        "record gauge canary tamper",
    )
    return record


def read_tamper_windows(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    since: str | None = None,
    limit: int = MAX_TAMPER_ROWS,
) -> tuple[list[dict[str, Any]], bool]:
    """``(windows_newest_first, truncated)`` for this canary."""
    capped = max(1, min(limit, MAX_TAMPER_ROWS))
    filters: dict[str, Any] = {_COL_AGENT_INSTANCE_ID: agent_instance_id}
    if since is not None:
        filters[_COL_ARREST_FROM] = {"op": "gte", "value": since}
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_GAUGE_CANARY_TAMPER,
                "filters": filters,
                "order_by": [[_COL_ARREST_FROM, "desc"], ["id", "desc"]],
                "limit": capped + 1,
            },
        ),
    )
    return rows[:capped], len(rows) > capped


__all__ = [
    "MAX_TAMPER_ROWS",
    "CanaryError",
    "is_active_canary",
    "list_canaries",
    "read_canary",
    "read_tamper_windows",
    "record_tamper",
    "register_canary",
    "retire_canary",
]
