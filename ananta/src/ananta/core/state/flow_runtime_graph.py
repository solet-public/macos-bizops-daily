"""
Flow Runtime Graph (FRG) Manager.

Tracks outstanding work tokens per flow. Flow completes when token_count == 0.

Persistence goes through the StateManagementInterface high-level primitives
(write_state / update_state / query_state) — no raw SQL. Results follow the
ActionResult contract:
- write_state() returns {action_status, data: {result: {generated_id}}}
- query_state() returns {action_status, data: {records: [{col: value}, ...]}}
- update_state() returns {action_status, data: {result: {updated: <int>}}}
"""

import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.status import is_status_match
from ananta.error_handling import FrameworkError
from ananta.services.state_service.bounded_read import iter_table_rows

if TYPE_CHECKING:
    from ananta.interfaces.state_service_protocol import StateServiceProtocol

logger = logging.getLogger(__name__)


class TokenOwnerType(StrEnum):
    """Type of entity owning a token."""

    VERTEX = "vertex"
    PROCESS = "process"
    JOB = "job"


class TokenState(StrEnum):
    """Token lifecycle states."""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    WAITING_JOB = "waiting_job"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABORTED = "aborted"


TERMINAL_STATES = frozenset(
    {
        TokenState.COMPLETED,
        TokenState.FAILED,
        TokenState.CANCELLED,
        TokenState.ABORTED,
    }
)

# The token state-machine is closed (a CHECK constraint pins ``state`` to the
# seven TokenState values), so "non-terminal" is exactly the complement of the
# terminal set — letting an equality ``=ANY`` filter stand in for ``NOT IN``.
NON_TERMINAL_STATES = frozenset(TokenState) - TERMINAL_STATES

# Bound on the pending-token walk in ``get_pending_tokens``.
#
# NOT a claim that `flow_tokens` is small — it held 110,926 rows when this was
# written. It is a claim about the PREDICATE: non-terminal tokens for ONE flow.
# A flow whose outstanding work is in the tens of thousands is not a flow with a
# backlog, it is a flow that is broken, and this method exists to help a human
# look at that — so refusing beats returning a list nobody can read.
#
# Measured 2026-08-15 PDT / 2026-08-16 UTC: the worst live flow
# (`flow-ledger-periodic-poll`) held 25,339 rows total and 129 non-terminal.
# 10,000 is ~77x that non-terminal figure and still far below the whole table.
_PENDING_TOKEN_WALK_CEILING = 10_000
_PENDING_TOKEN_WALK_REASON = (
    "non-terminal tokens for a SINGLE flow (worst live flow measured 129 on "
    "2026-08-15 PDT / 2026-08-16 UTC); a flow with more outstanding tokens than "
    "this is malfunctioning rather than busy, and this debug view cannot help "
    "with it."
)


class FlowRuntimeGraph:
    """
    Manages FRG tokens for flow completion tracking.

    Thread-safe via database transactions.
    All methods fail-fast on errors - no fallbacks, no silent failures.
    """

    def __init__(self, state_service: "StateServiceProtocol") -> None:
        self._state_service = state_service
        self._completion_callbacks: list[Callable[[str], None]] = []

    def register_completion_callback(
        self, callback: Callable[[str], None]
    ) -> None:
        """Register a callback to be invoked when a flow completes.

        Args:
            callback: Function taking flow_id, called on completion (success or failure)
        """
        self._completion_callbacks.append(callback)

    # =========================================================================
    # Token CRUD Operations
    # =========================================================================

    def create_token(
        self,
        flow_id: str,
        owner_type: TokenOwnerType,
        owner_ref: str,
        process_key: str | None = None,
        parent_token_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> str:
        """
        Create a new token for outstanding work.

        Returns the generated token ID.
        Raises FrameworkError immediately on failure (fail-fast).
        """
        token_data: dict[str, object] = {
            "core__flows_id": flow_id,
            "flow_id_trace": flow_id,
            "owner_type": owner_type.value,
            "owner_ref": owner_ref,
            "state": TokenState.PENDING.value,
            "parent_token_id": parent_token_id,
            "process_key": process_key,
            "metadata": json.dumps(metadata or {}),
        }

        result = self._state_service.write_state(
            namespace="core",
            data={"table": "flow_tokens", "record": token_data},
        )

        # Validate response using actual ActionResult contract
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            raise FrameworkError(
                message=f"Failed to create FRG token for flow {flow_id}",
                error_code="frg.token_creation_failed",
                details={"result": dict(result), "flow_id": flow_id},
            )

        token_id = self._extract_generated_id(result)
        if not token_id:
            raise FrameworkError(
                message="StateService did not return generated_id for FRG token",
                error_code="frg.missing_generated_id",
                details={"result": dict(result), "flow_id": flow_id},
            )

        return token_id

    def update_token_state(
        self,
        token_id: str,
        new_state: TokenState,
        result_summary: Mapping[str, object] | None = None,
    ) -> None:
        """
        Transition token to new state.

        If new_state is terminal, also sets completed_at.
        Raises on failure or if the token row is missing (affected != 1).
        ``updated_at`` is omitted — a universal BEFORE-UPDATE trigger maintains it.
        """
        completed_at = (
            datetime.now(UTC).isoformat() if new_state in TERMINAL_STATES else None
        )
        result_json = json.dumps(result_summary) if result_summary else "{}"

        result = self._state_service.update_state(
            namespace="core",
            query={"table": "flow_tokens", "filters": {"id": token_id}},
            updates={
                "state": new_state.value,
                "completed_at": completed_at,
                "result_summary": result_json,
            },
        )

        affected = self._affected(
            result,
            f"Failed to update FRG token {token_id} to state {new_state.value}",
            "frg.token_update_failed",
            {"token_id": token_id},
        )
        # Identity update on the primary key: exactly one row must change. A
        # 0-count means the token is missing (flow_tokens is never deleted, so
        # this is a hard invariant, not a benign CAS miss) — fail loudly.
        if affected != 1:
            raise FrameworkError(
                message=(
                    f"FRG token {token_id} update to {new_state.value} affected "
                    f"{affected} rows (expected exactly 1 — token missing)"
                ),
                error_code="frg.token_update_missing",
                details={"token_id": token_id, "updated": affected},
            )

    def complete_token(
        self,
        token_id: str,
        success: bool = True,
        result_summary: Mapping[str, object] | None = None,
    ) -> None:
        """
        Mark token as completed or failed.

        After completion, checks if flow should complete.
        """
        new_state = TokenState.COMPLETED if success else TokenState.FAILED
        self.update_token_state(token_id, new_state, result_summary)

        # Get flow_id and check for completion
        flow_id = self._get_flow_id_for_token(token_id)
        if flow_id:
            self._check_flow_completion(flow_id)

    # =========================================================================
    # Query Operations
    # =========================================================================

    def get_pending_token_count(self, flow_id: str) -> int:
        """Count non-terminal tokens for a flow — a scalar aggregate, no rows.

        THE READ IS GONE, not bounded (2026-08-15 PDT / 2026-08-16 UTC). This
        used to fetch every matching row and return ``len(rows)``, which is the
        most expensive possible way to ask "how many" — and once the default row
        bound dropped to 100 it stopped working at all. Measured on the release
        that first ran with that bound: ``flow-ledger-periodic-poll`` held 25,339
        rows, **129 of them non-terminal**, so the read was refused and every
        completion for that flow failed with "Failed to query pending token
        count".

        That mattered far more than one flow, because this is reached from
        ``complete_token`` -> ``_check_flow_completion`` — **on every action
        completion**. A bound here would have been correct and still wrong: the
        caller never wanted the rows, it wanted the number, and ``count`` runs
        the aggregate inside the owner plugin and ships a scalar. It is outside
        the row cap entirely, so this site cannot regress at any table size.

        No ``is_deleted`` handling is added or removed: ``count`` applies no
        automatic exclusion, and neither did the ``query_state`` it replaces.
        """
        result = self._state_service.count(
            "core",
            {
                "table": "flow_tokens",
                "filters": {
                    "flow_id_trace": flow_id,
                    "state": [s.value for s in NON_TERMINAL_STATES],
                },
            },
        )
        return self._query_scalar(
            result,
            f"Failed to query pending token count for flow {flow_id}",
            {"flow_id": flow_id},
        )

    def get_pending_tokens(self, flow_id: str) -> list[dict[str, object]]:
        """Get all pending tokens for a flow (for debugging/observability).

        PAGINATED (2026-08-15 PDT / 2026-08-16 UTC). This genuinely needs the
        rows — unlike :meth:`get_pending_token_count` above, which wanted only
        the number and now asks for only the number — so the repair is a walk
        rather than a deletion.

        The docstring this replaces argued the opposite, and the argument was
        correct when it was written: "uses uncapped ``query_state`` rather than
        ``query_ordered`` — the latter silently caps at 100 rows, which would
        under-report a flow with many outstanding tokens". Against the old
        10,000-row default that reasoning held. It is now exactly inverted: the
        default bound is 100, so the UNCAPPED read is the one that gets refused,
        and a page cap only truncates if you stop after one page. ``iter_table_rows``
        pages until a short page proves the end.

        The Python sort is gone with it. Its key was ``(created_at, id)``, which
        is precisely the helper's own cursor — so the ordering now comes from the
        read rather than from re-sorting a fully materialised result, and the
        ``id`` tie-break that made equal-``created_at`` ordering deterministic is
        preserved by the cursor's second column.
        """
        ordered = list(
            iter_table_rows(
                self._state_service,
                namespace="core",
                table="flow_tokens",
                filters={
                    "flow_id_trace": flow_id,
                    "state": [s.value for s in NON_TERMINAL_STATES],
                },
                ceiling=_PENDING_TOKEN_WALK_CEILING,
                reason=_PENDING_TOKEN_WALK_REASON,
                include_deleted=True,
            ),
        )
        return [
            {
                "id": row["id"],
                "owner_type": row["owner_type"],
                "owner_ref": row["owner_ref"],
                "state": row["state"],
                "process_key": row["process_key"],
                "created_at": row["created_at"],
            }
            for row in ordered
        ]

    def get_token_for_action(self, action_id: str) -> str | None:
        """Look up token ID for an action.

        Returns None when the action has no row or a null ``flow_token_id``. A
        non-completed / malformed query envelope RAISES (fail-fast) — a database
        error must never be coerced to a benign "no token".
        """
        result = self._state_service.query_state(
            "core",
            {"table": "action_events", "filters": {"id": action_id}},
        )
        rows = self._query_rows(
            result,
            f"Failed to look up token for action {action_id}",
            {"action_id": action_id},
        )
        if rows and rows[0].get("flow_token_id"):
            return str(rows[0]["flow_token_id"])
        return None

    # =========================================================================
    # Flow Completion
    # =========================================================================

    def _check_flow_completion(self, flow_id: str) -> None:
        """
        Check if flow should complete (zero pending tokens).

        This is the SINGLE SOURCE OF TRUTH for flow completion.
        """
        pending_count = self.get_pending_token_count(flow_id)
        logger.debug(
            "FRG_COMPLETION_CHECK: flow_id=%s, pending_tokens=%d",
            flow_id, pending_count
        )

        if pending_count > 0:
            return

        # Zero tokens - complete the flow
        logger.debug("FRG: Flow %s has zero pending tokens - completing", flow_id)

        # Genuine compare-and-set: only the transition out of 'active' wins. A
        # 0-row result is a LEGITIMATE miss (the flow already left 'active' via
        # another path), NOT an error — so the envelope is validated for
        # fail-fast but the callbacks are NOT gated on the affected count.
        result = self._state_service.update_state(
            namespace="core",
            query={"table": "flows", "filters": {"id": flow_id, "status": "active"}},
            updates={
                "status": "completed",
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )
        self._affected(
            result,
            f"Failed to complete flow {flow_id}",
            "frg.flow_completion_failed",
            {"flow_id": flow_id},
        )

        logger.debug("FRG: Flow %s completed", flow_id)

        # Invoke completion callbacks UNCONDITIONALLY after the envelope check.
        # Gating on the CAS win would UNDER-fire when the flow reached terminal
        # via another path; the callbacks are idempotent, so a double-fire is
        # harmless but a missed fire would leak pending attachments.
        self._invoke_completion_callbacks(flow_id)

    def _invoke_completion_callbacks(self, flow_id: str) -> None:
        """Invoke all registered completion callbacks for a flow."""
        for callback in self._completion_callbacks:
            try:
                callback(flow_id)
            except Exception:
                logger.exception(
                    "FRG: Completion callback failed for flow %s", flow_id
                )

    # =========================================================================
    # Helper Methods - StateService Result Extraction
    # =========================================================================

    def _extract_generated_id(self, result: object) -> str | None:
        """
        Extract generated_id from write_state() result.

        ActionResult structure: {action_status, data: {result: {generated_id}}}
        """
        if not isinstance(result, dict):
            return None

        data = result.get("data")
        if not isinstance(data, dict):
            return None

        result_obj = data.get("result")
        if isinstance(result_obj, dict):
            generated_id = result_obj.get("generated_id")
            if isinstance(generated_id, str):
                return generated_id

        return None

    def _query_scalar(
        self, result: object, message: str, details: dict[str, object]
    ) -> int:
        """Extract the integer from a ``count`` result, fail-fast.

        The scalar lands at ``data.result.value`` — NESTED, not flat. Checked
        rather than assumed: reading ``data.value`` would yield ``None`` on a
        perfectly healthy response and silently report every flow as having zero
        pending tokens, which would mark live flows complete. A wrong count here
        is worse than an error, so every departure from the expected shape
        raises rather than defaulting.
        """
        if not isinstance(result, dict):
            raise FrameworkError(
                message=message,
                error_code="frg.count_failed",
                details={**details, "result": repr(result)},
            )
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            raise FrameworkError(
                message=message,
                error_code="frg.count_failed",
                details={**details, "result": dict(result)},
            )
        data = result.get("data")
        inner = data.get("result") if isinstance(data, dict) else None
        value = inner.get("value") if isinstance(inner, dict) else None
        if not isinstance(value, int) or isinstance(value, bool):
            raise FrameworkError(
                message=f"{message} (malformed count envelope)",
                error_code="frg.count_failed",
                details={**details, "result": dict(result)},
            )
        return value

    def _query_rows(
        self, result: object, message: str, details: dict[str, object]
    ) -> list[dict[str, object]]:
        """Extract dict records from a ``query_state`` result, fail-fast.

        The state interface returns records as a list of dicts. A non-completed
        or structurally malformed envelope RAISES ``FrameworkError`` — a database
        error must never coerce to an empty list and read as 'no rows found'.
        """
        if not isinstance(result, dict):
            raise FrameworkError(
                message=message,
                error_code="frg.query_failed",
                details={**details, "result": repr(result)},
            )
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            raise FrameworkError(
                message=message,
                error_code="frg.query_failed",
                details={**details, "result": dict(result)},
            )
        data = result.get("data")
        if not isinstance(data, dict):
            raise FrameworkError(
                message=f"{message} (malformed data envelope)",
                error_code="frg.query_failed",
                details={**details, "data": repr(data)},
            )
        records = data.get("records")
        if not isinstance(records, list):
            raise FrameworkError(
                message=f"{message} (malformed records)",
                error_code="frg.query_failed",
                details={**details, "records": repr(records)},
            )
        for record in records:
            if not isinstance(record, dict):
                raise FrameworkError(
                    message=f"{message} (non-dict record)",
                    error_code="frg.query_failed",
                    details={**details, "record": repr(record)},
                )
        return records

    def _affected(
        self,
        result: object,
        message: str,
        error_code: str,
        details: dict[str, object],
    ) -> int:
        """Extract the rows-affected count from an ``update_state`` result.

        The compare-and-set signal lives at ``data.result.updated``. A
        non-completed envelope, or a missing / non-int (or bool) count, is
        malformed and RAISES — it must not coerce to 0 and read as a legitimate
        CAS miss.
        """
        if not isinstance(result, dict):
            raise FrameworkError(
                message=message,
                error_code=error_code,
                details={**details, "result": repr(result)},
            )
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            raise FrameworkError(
                message=message,
                error_code=error_code,
                details={**details, "result": dict(result)},
            )
        data = result.get("data")
        if not isinstance(data, dict):
            raise FrameworkError(
                message=f"{message} (malformed data envelope)",
                error_code=error_code,
                details={**details, "data": repr(data)},
            )
        inner = data.get("result")
        if not isinstance(inner, dict):
            raise FrameworkError(
                message=f"{message} (malformed result)",
                error_code=error_code,
                details={**details, "result_obj": repr(inner)},
            )
        updated = inner.get("updated")
        if isinstance(updated, bool) or not isinstance(updated, int):
            raise FrameworkError(
                message=f"{message} (non-int affected count)",
                error_code=error_code,
                details={**details, "updated": repr(updated)},
            )
        return updated

    def _get_flow_id_for_token(self, token_id: str) -> str | None:
        """Look up flow_id for a token.

        Returns None when the token has no row or a null ``flow_id_trace``. A
        non-completed / malformed query envelope RAISES (fail-fast).
        """
        result = self._state_service.query_state(
            "core",
            {"table": "flow_tokens", "filters": {"id": token_id}},
        )
        rows = self._query_rows(
            result,
            f"Failed to look up flow for token {token_id}",
            {"token_id": token_id},
        )
        if rows and rows[0].get("flow_id_trace"):
            return str(rows[0]["flow_id_trace"])
        return None
