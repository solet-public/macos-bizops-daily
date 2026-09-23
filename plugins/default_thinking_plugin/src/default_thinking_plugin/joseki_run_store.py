"""Run-row store for platform-driven joseki runs (the ``run_joseki`` driver).

One thin row per run in ``thinking_joseki_run`` (declared in ``schema.py``):
run-level status + pointers ONLY. Per-step state lives in the instantiated
joseki-scoped WBS document — the pull-engine durable substrate — so a driver
crash re-derives everything from the WBS and the run row never duplicates
step truth. Design: ``workbench/2026-07-05_run_joseki_driver_design_spec.md``
§3 (v3, review-CLEAR).

Storage discipline mirrors :mod:`default_thinking_plugin.authored_lifecycle`:
``StateManagementInterface`` primitives only — ``write_state`` for creation,
``read_state`` for reads, and predicated ``update_state`` compare-and-set
(rows-affected IS the guard). Never raw SQL; never a foreign namespace.

RACE SEMANTICS — the one deliberate divergence from the lifecycle module:
the lifecycle raises on a lost CAS (a manual transition racing is an error a
caller must see). Run-status transitions are DIFFERENT: the live event path
and the reconciler sweep race BY DESIGN (spec §4.3; the INF-02
``_lost_stamp_verdict`` posture), so :meth:`JosekiRunStore.cas_status` reports
won/lost and NEVER raises on a lost race — the loser re-reads and defers.
Absent rows still fail loud everywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from ananta.error_handling import FrameworkError

from default_thinking_plugin.constants import (
    JOSEKI_RUN_STATUS_AWAITING_USER,
    JOSEKI_RUN_STATUS_COMPLETED,
    JOSEKI_RUN_STATUS_FAILED,
    JOSEKI_RUN_STATUS_RUNNING,
    ErrorCode,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_RUN_TABLE = "thinking_joseki_run"

# Terminal statuses never transition again; the guard set for callers.
RUN_TERMINAL_STATUSES = frozenset(
    {JOSEKI_RUN_STATUS_COMPLETED, JOSEKI_RUN_STATUS_FAILED},
)
_ALL_STATUSES = frozenset(
    {
        JOSEKI_RUN_STATUS_RUNNING,
        JOSEKI_RUN_STATUS_AWAITING_USER,
        JOSEKI_RUN_STATUS_COMPLETED,
        JOSEKI_RUN_STATUS_FAILED,
    },
)


class RunStateStore(Protocol):
    """State access for run rows — create, read, and predicated CAS."""

    def write_state(
        self, namespace: str, data: dict[str, object],
    ) -> dict[str, Any]: ...

    def read_state(
        self, namespace: str, query: dict[str, object],
    ) -> dict[str, Any]: ...

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
    ) -> dict[str, Any]: ...


class JosekiRunStore:
    """CRUD + CAS over the ``thinking_joseki_run`` row.

    Collaborators injected; no service resolution inside (offline-testable
    against the in-memory state double, same as the lifecycle engine).
    """

    def __init__(self, *, state_store: RunStateStore, namespace: str) -> None:
        self._state = state_store
        self._namespace = namespace

    # -- creation ------------------------------------------------------------

    def create(
        self,
        *,
        joseki_key: str,
        wbs_id: str,
        session_id: str,
        flow_id: str,
        requester: str,
        label: str,
    ) -> str:
        """Insert the run row (status='running') and return its run id."""
        result = self._state.write_state(
            namespace=self._namespace,
            data={
                "table": _RUN_TABLE,
                "record": {
                    "joseki_key": joseki_key,
                    "wbs_id": wbs_id,
                    "session_id": session_id,
                    "flow_id": flow_id,
                    "status": JOSEKI_RUN_STATUS_RUNNING,
                    "failure_detail": "",
                    "requester": requester,
                    "label": label,
                    "attempts": 0,
                },
            },
        )
        run_id = _generated_id(result)
        if not run_id:
            raise FrameworkError(
                message=(
                    f"joseki run row insert for {joseki_key!r} returned no "
                    f"generated id — state write failed"
                ),
                error_code=ErrorCode.JOSEKI_RUN_WRITE_FAILED,
            )
        return run_id

    # -- reads ---------------------------------------------------------------

    def get(self, *, run_id: str) -> dict[str, Any] | None:
        """The run row by id, or ``None`` when absent."""
        result = self._state.read_state(
            namespace=self._namespace,
            query={
                "table": _RUN_TABLE,
                "filters": {"id": run_id, "is_deleted": 0},
                "limit": 1,
            },
        )
        records = result.get("data", {}).get("records") or []
        return records[0] if records else None

    def require(self, *, run_id: str) -> dict[str, Any]:
        """The run row by id; absent rows fail loud."""
        row = self.get(run_id=run_id)
        if row is None:
            raise FrameworkError(
                message=f"joseki run {run_id!r} does not exist",
                error_code=ErrorCode.JOSEKI_RUN_NOT_FOUND,
            )
        return row

    def list_runs(
        self,
        *,
        status: str | None = None,
        joseki_key: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Bounded run listing, newest rows first by platform ordering."""
        if status is not None and status not in _ALL_STATUSES:
            raise FrameworkError(
                message=(
                    f"unknown run status filter {status!r} — one of "
                    f"{sorted(_ALL_STATUSES)}"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        filters: dict[str, object] = {"is_deleted": 0}
        if status is not None:
            filters["status"] = status
        if joseki_key is not None:
            filters["joseki_key"] = joseki_key
        result = self._state.read_state(
            namespace=self._namespace,
            query={
                "table": _RUN_TABLE,
                "filters": filters,
                "limit": limit,
            },
        )
        records = result.get("data", {}).get("records") or []
        return list(records)

    # -- predicated transitions ----------------------------------------------

    def cas_status(
        self,
        *,
        run_id: str,
        from_status: str,
        to_status: str,
        extra_updates: Mapping[str, object] | None = None,
    ) -> bool:
        """Predicated status transition; ``True`` iff THIS caller won.

        A lost race (rows-affected == 0 with the row present) is a benign
        ``False`` — spec §4.3: the live event path and the reconciler race by
        design and the loser defers. An ABSENT row fails loud: losing to a
        deletion is not a race, it is corruption.
        """
        updates: dict[str, object] = {"status": to_status}
        if extra_updates:
            updates.update(extra_updates)
        affected = self._update(
            {"id": run_id, "status": from_status, "is_deleted": 0},
            updates,
        )
        if affected > 0:
            return True
        self.require(run_id=run_id)
        return False

    def cas_increment_attempts(self, *, run_id: str, prior_attempts: int) -> bool:
        """Attempt-counter increment, predicated on the prior value."""
        affected = self._update(
            {
                "id": run_id,
                "status": JOSEKI_RUN_STATUS_RUNNING,
                "attempts": prior_attempts,
                "is_deleted": 0,
            },
            {"attempts": prior_attempts + 1},
        )
        if affected > 0:
            return True
        self.require(run_id=run_id)
        return False

    def record_current_step(self, *, run_id: str, step_number: int) -> bool:
        """Stamp the run's PROGRESS CURSOR and reset the stall counter.

        ``current_step`` carries the progress cursor (completed action count
        observed at the last reconcile; 0 at kickoff). Observed progress
        RESETS ``attempts`` — the stall counter counts CONSECUTIVE
        no-progress sweeps, never cumulative between-step windows (Rev-A
        build delta-2 N). Predicated on ``running``.
        """
        affected = self._update(
            {
                "id": run_id,
                "status": JOSEKI_RUN_STATUS_RUNNING,
                "is_deleted": 0,
            },
            {"current_step": step_number, "attempts": 0},
        )
        if affected > 0:
            return True
        self.require(run_id=run_id)
        return False

    # -- plumbing --------------------------------------------------------------

    def _update(
        self, filters: dict[str, object], updates: dict[str, object],
    ) -> int:
        result = self._state.update_state(
            namespace=self._namespace,
            query={"table": _RUN_TABLE, "filters": filters},
            updates=updates,
        )
        return _rows_affected(result)


def _rows_affected(result: dict[str, Any]) -> int:
    """Extract ``data.result.updated`` from an update envelope."""
    data = result.get("data")
    if not isinstance(data, dict):
        return 0
    inner = data.get("result")
    if not isinstance(inner, dict):
        return 0
    updated = inner.get("updated", 0)
    return int(updated) if isinstance(updated, int) else 0


def _generated_id(result: dict[str, Any]) -> str:
    """Extract the platform-assigned row id from a write envelope."""
    data = result.get("data")
    if not isinstance(data, dict):
        return ""
    inner = data.get("result")
    if isinstance(inner, dict):
        generated = inner.get("generated_id") or inner.get("id")
        if isinstance(generated, str):
            return generated
    generated = data.get("generated_id")
    return generated if isinstance(generated, str) else ""
