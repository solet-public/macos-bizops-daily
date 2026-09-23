"""CDX-06 part C (2026-08-24) — `report_inbox_consumption` /
`session_inbox_consumption_status`, the honesty field: "a session that has
not actually consumed its inbox must be distinguishable from one that has;
no surface may report consumed-by-assumption" (ruling 4,
`2026-08-24_operator_rulings_test_seed_dependencies_codex_deafness.md`).

Same shape as `context_status_verbs.py`'s `report_context_status` /
`session_context_status` pair — a plain state upsert fed by a caller that
already did its own work client-side, and a trivial read that returns
`resolved=False` (never a raised error, never a defaulted `True`) when
nothing has ever been reported. Deliberately NOT added to that module: see
`schema.py`'s `TABLE_INBOX_CONSUMPTION_STATUS` comment for why this is a
sibling table, not new columns on `session_context_status`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .context_status_verbs import REPORTER_SURFACES
from .inbox_consumption_store import (
    AmbiguousInboxConsumptionAgentSessionIdError,
    read_inbox_consumption_status,
    read_inbox_consumption_status_by_agent_session_id,
    upsert_inbox_consumption_status,
)
from .session_context_status_store import read_agent_session_id_for_binding
from .session_lifecycle_verbs import VerbError

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface


def _require_report_identity(
    *, agent_instance_id: str, runtime: str, checked_at: str,
) -> None:
    required = {
        "agent_instance_id": agent_instance_id,
        "runtime": runtime,
        "checked_at": checked_at,
    }
    missing = sorted(name for name, value in required.items() if not value.strip())
    if missing:
        raise VerbError(
            "missing_argument",
            "report_inbox_consumption requires a non-empty " + ", ".join(missing) + ".",
        )


def _require_reporter_surface(reporter_surface: str | None) -> None:
    if reporter_surface is None or reporter_surface in REPORTER_SURFACES:
        return
    raise VerbError(
        "unknown_reporter_surface",
        f"report_inbox_consumption got reporter_surface={reporter_surface!r}, which is not "
        f"one of {sorted(REPORTER_SURFACES)}. Report 'unknown' when the hook cannot classify "
        "its own location — an invented surface silently poisons attribution.",
    )


def _require_pending_pair(
    *, pending_found_at: str | None, pending_reason: str | None,
) -> None:
    """`pending_reason` describes a specific `pending_found_at` event; a
    reason with no timestamp it belongs to is not a fact this table can
    represent, so it is refused rather than silently stored orphaned."""
    if pending_reason is not None and not pending_found_at:
        raise VerbError(
            "missing_argument",
            "report_inbox_consumption got pending_reason without pending_found_at — "
            "a reason must be attached to the check that found it.",
        )


def report_inbox_consumption(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    runtime: str,
    checked_at: str,
    pending_found_at: str | None = None,
    pending_reason: str | None = None,
    reporter_surface: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Overwrite the caller's own latest inbox-consumption-check row.

    `checked_at` is required and is the entire honesty mechanism: it is
    stamped on EVERY call, whether or not the check found anything pending,
    so a fresh `checked_at` proves the consumer hook executed and an absent
    row (never a stale or synthesized one) proves it never has. Callers must
    never skip this call on the "nothing to report" path — that path is
    exactly what the field exists to distinguish from "never ran".

    `pending_found_at`/`pending_reason` are optional and describe the most
    recent time the check found a pending delivery and forced a Stop-hook
    block/continue; omit both when this check found nothing. `reporter_surface`
    is optional and validated against the same closed vocabulary
    `report_context_status` uses (checkout/plugin_cache/vendored/release/unknown);
    an unrecognised value fails loud rather than silently poisoning
    attribution. `agent_session_id` is optional and captured for the
    GAU-07-style routing join used by the read side to resolve a watcher-held
    session.

    Errors: `missing_argument` (agent_instance_id/runtime/checked_at empty,
    or `pending_reason` given without `pending_found_at`), `unknown_reporter_surface`.
    """
    _require_report_identity(
        agent_instance_id=agent_instance_id, runtime=runtime, checked_at=checked_at,
    )
    _require_reporter_surface(reporter_surface)
    _require_pending_pair(pending_found_at=pending_found_at, pending_reason=pending_reason)
    upsert_inbox_consumption_status(
        state,
        agent_instance_id=agent_instance_id,
        runtime=runtime,
        checked_at=checked_at,
        pending_found_at=pending_found_at,
        pending_reason=pending_reason,
        reporter_surface=reporter_surface,
        agent_session_id=agent_session_id,
    )
    return {"status": "recorded"}


def _unresolved(
    agent_instance_id: str, *, agent_session_id: str,
) -> dict[str, Any]:
    if agent_session_id:
        resolution_error = (
            "no inbox-consumption report could be resolved for this identity: "
            "the direct instance-id lookup and the ledger/watch-id join both found no row"
        )
    else:
        resolution_error = (
            "no inbox-consumption report is directly recorded for this instance, and no "
            "peer binding supplied an agent_session_id for a ledger/watch-id lookup"
        )
    return {
        "resolved": False,
        "resolution_error": resolution_error,
        "agent_instance_id": agent_instance_id,
        "runtime": None,
        "checked_at": None,
        "pending_found_at": None,
        "pending_reason": None,
        "reporter_surface": None,
        "agent_session_id": None,
    }


def session_inbox_consumption_status(
    state: StateManagementInterface, *, agent_instance_id: str,
) -> dict[str, Any]:
    """Read the cached row for `agent_instance_id`. `resolved=False` (never
    a raised `VerbError`, never a defaulted `True`) is the expected, stable
    shape when this session's consumer hook has never reported — a fresh
    session, a `host=operator` seat, or a runtime that uses its own separate
    honest mechanism (Claude's `wake_waiter.py` + `solet-bridge wake`, which this
    table does not cover). Callers must treat `resolved=False` as a loud,
    honest "not known to have consumed", never estimate consumption in its
    place — the exact "no surface may report consumed-by-assumption"
    requirement this verb exists to satisfy.

    The direct lookup runs first. When it misses, the GAU-07 ledger/watch-id
    join reads the stable `agent_session_id` from this instance's peer binding
    and finds the report stored under that stable identity. The watch id is a
    one-way digest, so the join is always through stored data and never by
    reconstructing an id from its string shape.
    """
    if not agent_instance_id.strip():
        raise VerbError(
            "missing_argument",
            "session_inbox_consumption_status requires a non-empty agent_instance_id.",
        )
    row = read_inbox_consumption_status(state, agent_instance_id)
    agent_session_id = ""
    if row is None:
        agent_session_id = read_agent_session_id_for_binding(state, agent_instance_id)
        try:
            row = read_inbox_consumption_status_by_agent_session_id(state, agent_session_id)
        except AmbiguousInboxConsumptionAgentSessionIdError as exc:
            raise VerbError("ambiguous_agent_session_id", str(exc)) from exc
    if row is None:
        return _unresolved(agent_instance_id, agent_session_id=agent_session_id)
    return {
        "resolved": True,
        "resolution_error": None,
        "agent_instance_id": agent_instance_id,
        "runtime": str(row.get("runtime") or ""),
        "checked_at": str(row.get("checked_at") or ""),
        "pending_found_at": row.get("pending_found_at"),
        "pending_reason": row.get("pending_reason"),
        "reporter_surface": row.get("reporter_surface"),
        "agent_session_id": row.get("agent_session_id"),
    }


__all__ = [
    "report_inbox_consumption",
    "session_inbox_consumption_status",
]
