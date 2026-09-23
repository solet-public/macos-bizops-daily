"""One-time, conservative staging of pre-dispatch legacy session leaks.

This is intentionally separate from the completed-idle sweep: legacy rows
predate managed dispatch and therefore have no dispatch id from which a normal
completion predicate could be derived.  It never retires a host.  A qualifying
row is merely CAS-parked and returned to the seat with its evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.services.state_service.bounded_read import iter_table_rows

from .schema import LIFECYCLE_IDLE, LIFECYCLE_LIVE, LIFECYCLE_PARKED, TABLE_SESSION_DEPENDENCY
from .session_lifecycle_store import (
    StaleLifecycleStateError,
    read_managed_sessions_page,
    transition_lifecycle_state,
)
from .session_lifecycle_verbs import session_status

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ananta.interfaces.state_management_interface import StateManagementInterface

_PAGE_SIZE = 100
_WHO_HAS_WORK_LIMIT = 500
_PROJECT_SOLET_CLI_ENV = "PROJECT_SOLET_CLI"
_ACTIVE_UNIT_STATES = frozenset({"dispatched", "working", "authorized"})


class OwnershipReadError(RuntimeError):
    """The work-register ownership read was unavailable or not complete."""


def _project_solet_cli() -> str:
    """Resolve the explicit register CLI without falling back to a shell PATH."""
    configured = os.environ.get(_PROJECT_SOLET_CLI_ENV)
    if not configured:
        raise OwnershipReadError(
            f"{_PROJECT_SOLET_CLI_ENV} is not set; this module refuses to guess "
            "a developer-specific default path for the project-solet CLI."
        )
    return configured


def _invoke_who_has_work(
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    """Run who-has-work and decode its JSON payload, refusing any failure."""
    argv = [_project_solet_cli(), "db", "unit", "who-has-work", "--limit", str(_WHO_HAS_WORK_LIMIT)]
    try:
        completed = run(argv, capture_output=True, check=False, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OwnershipReadError(f"who-has-work command failed: {exc}") from exc
    if completed.returncode != 0:
        raise OwnershipReadError(
            f"who-has-work exited {completed.returncode}: {completed.stderr.strip()}",
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OwnershipReadError("who-has-work returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise OwnershipReadError("who-has-work payload was not an object")
    return payload


def _require_complete_page(payload: dict[str, Any]) -> list[Any]:
    """Validate the payload is a full, untruncated who-has-work page and return its rows."""
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise OwnershipReadError("who-has-work payload omitted rows")
    if payload.get("effective_limit") != _WHO_HAS_WORK_LIMIT:
        raise OwnershipReadError("who-has-work did not honor the complete-page limit")
    if payload.get("returned_count") != len(rows):
        raise OwnershipReadError("who-has-work returned_count disagrees with rows")
    total_count = payload.get("total_count")
    if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
        raise OwnershipReadError("who-has-work returned invalid total_count")
    if payload.get("truncated") is not False or total_count != len(rows):
        raise OwnershipReadError("who-has-work page was incomplete")
    return rows


def _row_actor(row: Any) -> str | None:
    """Validate one who-has-work row and return its actor, or None if unowned."""
    if not isinstance(row, dict):
        raise OwnershipReadError("who-has-work row was not an object")
    state = row.get("state")
    if state not in _ACTIVE_UNIT_STATES:
        raise OwnershipReadError(f"who-has-work returned invalid active state: {state!r}")
    actor = row.get("actor")
    if actor is None:
        return None
    if not isinstance(actor, str) or not actor:
        raise OwnershipReadError("who-has-work row returned invalid actor")
    return actor


def _active_work_actors(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> set[str]:
    """Read every active unit owner, refusing an incomplete register page.

    ``who-has-work`` has a bounded page rather than a cursor.  Asking for its
    largest page and requiring ``truncated=false`` makes the bound explicit:
    an ownership read can never quietly become an empty owner set.
    """
    rows = _require_complete_page(_invoke_who_has_work(run))
    actors: set[str] = set()
    for row in rows:
        actor = _row_actor(row)
        if actor is not None:
            actors.add(actor)
    return actors


def _legacy_candidate(row: dict[str, Any]) -> bool:
    """Recognize only the dispatch-less, unscoped registration-era shape."""
    fields = ("dispatch_id", "lane_id", "role_name", "brief_ref")
    return not any(str(row.get(field) or "") for field in fields)


def _armed_edges(state: StateManagementInterface) -> set[str]:
    """Return every instance id named by an un-fired dependency edge."""
    blocked: set[str] = set()
    for edge in iter_table_rows(
        state,
        namespace=AGENT_ROLE_BINDING_NAMESPACE,
        table=TABLE_SESSION_DEPENDENCY,
        filters={"fired_at": {"op": "is_null"}},
        ceiling=1_000,
        reason="legacy backfill refuses sessions with armed dependency edges",
    ):
        for field in ("waiter_instance_id", "condition_ref"):
            value = str(edge.get(field) or "")
            if value.startswith("agi-"):
                blocked.add(value)
    return blocked


def _pages(
    state: StateManagementInterface,
    lifecycle_state: str,
) -> Iterator[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Yield rows with the cursor that produced them, never a whole-ledger claim."""
    after: list[str] | None = None
    while True:
        rows, has_more = read_managed_sessions_page(
            state,
            {"lifecycle_state": lifecycle_state},
            limit=_PAGE_SIZE,
            after=after,
        )
        for row in rows:
            yield row, {"created_at": row.get("created_at"), "id": row.get("id")}
        if not has_more or not rows:
            return
        after = [str(rows[-1].get("created_at") or ""), str(rows[-1].get("id") or "")]


def _ineligibility_reason(
    state: StateManagementInterface,
    row: dict[str, Any],
    agent_instance_id: str,
    armed: set[str],
    active_work_actors: set[str],
) -> str | None:
    """Return why this row must not be staged, or None if it is eligible."""
    if not agent_instance_id or not _legacy_candidate(row):
        return "not_legacy_unscoped"
    if agent_instance_id in armed:
        return "armed_dependency"
    if agent_instance_id in active_work_actors:
        return "active_unit_assignment"
    liveness = str(session_status(state, agent_instance_id).get("host_liveness") or "unknown")
    return None if liveness == "dead" else f"host_{liveness}"


def _try_park(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    lifecycle_state: str,
    directed_by: str,
) -> bool:
    """Attempt the CAS park transition; return whether it landed."""
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=lifecycle_state,
            to_state=LIFECYCLE_PARKED,
            directed_by=directed_by,
            reason=(
                "legacy_unsupervised_backfill host_liveness=dead "
                "dependency=none seat_retirement_required"
            ),
        )
    except StaleLifecycleStateError:
        return False
    return True


def stage_legacy_unsupervised_sessions(
    state: StateManagementInterface,
    *,
    directed_by: str,
) -> dict[str, list[dict[str, Any]]]:
    """CAS-park only dead, dependency-free legacy rows for seat disposition."""
    staged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    armed = _armed_edges(state)
    active_work_actors = _active_work_actors()
    for lifecycle_state in (LIFECYCLE_LIVE, LIFECYCLE_IDLE):
        for row, cursor in _pages(state, lifecycle_state):
            agent_instance_id = str(row.get("agent_instance_id") or "")
            record = {"agent_instance_id": agent_instance_id, "cursor": cursor}
            reason = _ineligibility_reason(state, row, agent_instance_id, armed, active_work_actors)
            if reason is not None:
                skipped.append({**record, "reason": reason})
                continue
            if not _try_park(
                state,
                agent_instance_id=agent_instance_id,
                lifecycle_state=lifecycle_state,
                directed_by=directed_by,
            ):
                skipped.append({**record, "reason": "lifecycle_race"})
                continue
            staged.append(
                {**record, "reason": "host_dead_dependency_free", "seat_retirement_required": True}
            )
    return {"staged": staged, "skipped": skipped}


__all__ = ["stage_legacy_unsupervised_sessions"]
