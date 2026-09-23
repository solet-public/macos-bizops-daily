"""Work-product register persistence via the state-management interface.

Stores the serialized ``WorkProductRegister`` JSON in the
``thinking_wbs.work_products_data`` column, keyed by WBS ID, through the
``read_state`` / ``update_state`` primitives (no raw SQL).
The ``wbs_run_id`` for ``WorkProductStoreLike`` is the WBS record ID
(``wbs-`` prefix) which is durable across flow boundaries.
"""

from __future__ import annotations

import logging
from typing import Final, Protocol, cast

from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.types import ActionResult

logger = logging.getLogger(__name__)

THINKING_NAMESPACE: Final = "default_thinking_plugin"
THINKING_WBS_TABLE: Final = "thinking_wbs"
WORK_PRODUCTS_COLUMN: Final = "work_products_data"


class StateServiceProtocol(Protocol):
    """Narrow protocol for the state service operations needed."""

    def read_state(self, namespace: str, query: dict[str, object]) -> ActionResult: ...
    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> ActionResult: ...


def _assert_completed(result: ActionResult, operation: str, wbs_run_id: str) -> None:
    if result.get("action_status") == ActionStatus.COMPLETED.value:
        return
    raise RuntimeError(
        f"Work-product register {operation} failed for WBS {wbs_run_id}: "
        f"{result.get('error')}"
    )


def _records_from_result(
    result: ActionResult, operation: str, wbs_run_id: str
) -> list[object]:
    _assert_completed(result, operation, wbs_run_id)
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Work-product register {operation} returned malformed data for WBS "
            f"{wbs_run_id}: {data!r}"
        )
    records = data.get("records")
    if not isinstance(records, list):
        raise RuntimeError(
            f"Work-product register {operation} returned malformed records for "
            f"WBS {wbs_run_id}: {records!r}"
        )
    return records


def _affected_count(result: ActionResult, operation: str, wbs_run_id: str) -> int:
    """Extract the rows-affected count from an ``update_state`` result.

    The compare-and-set signal lives at ``data.result.updated``. A missing /
    non-int (or bool) count is a malformed envelope and RAISES — it must not
    coerce to 0 and read as a legitimate miss.
    """
    _assert_completed(result, operation, wbs_run_id)
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Work-product register {operation} returned malformed data for WBS "
            f"{wbs_run_id}: {data!r}"
        )
    inner = data.get("result")
    if not isinstance(inner, dict):
        raise RuntimeError(
            f"Work-product register {operation} returned malformed result for WBS "
            f"{wbs_run_id}: {inner!r}"
        )
    updated = inner.get("updated")
    if isinstance(updated, bool) or not isinstance(updated, int):
        raise RuntimeError(
            f"Work-product register {operation} returned a non-int affected count "
            f"for WBS {wbs_run_id}: {updated!r}"
        )
    return updated


class WorkProductStoreAdapter:
    """Implements ``WorkProductStoreLike`` using the thinking_wbs table.

    When ``work_product_run_id`` is set, all register load/save
    operations use it as the storage key instead of the per-fragment
    ``wbs_run_id``. This allows multiple joseki WBS fragments to
    share a single work-product register across one execution run.
    """

    def __init__(
        self,
        state_service: StateServiceProtocol,
        work_product_run_id: str | None = None,
    ) -> None:
        self._state_service = state_service
        self._work_product_run_id = work_product_run_id

    def _resolve_storage_key(self, wbs_run_id: str) -> str:
        """Return the shared run ID if set, otherwise the fragment's own ID."""
        return self._work_product_run_id or wbs_run_id

    def load_register(self, wbs_run_id: str) -> str | None:
        """Load a serialized register for a WBS run.

        Args:
            wbs_run_id: The WBS record ID (e.g. ``wbs-abc123``).
                When ``work_product_run_id`` was set on the adapter,
                that shared key is used instead.

        Returns:
            Serialized JSON string, or None if no register exists.
        """
        key = self._resolve_storage_key(wbs_run_id)
        result = self._state_service.read_state(
            THINKING_NAMESPACE,
            {
                "table": THINKING_WBS_TABLE,
                "filters": {"id": key, "is_deleted": 0},
                "limit": 1,
            },
        )
        rows = _records_from_result(result, "load", key)
        if not rows:
            raise RuntimeError(f"WORK_PRODUCTS: WBS record not found: {wbs_run_id}")
        row = rows[0]
        if not isinstance(row, dict):
            raise RuntimeError(
                f"WORK_PRODUCTS: WBS row has invalid type for {wbs_run_id}: "
                f"{type(row).__name__}"
            )
        data = cast("dict[str, object]", row).get(WORK_PRODUCTS_COLUMN)
        if data is None:
            return None
        if not isinstance(data, str):
            raise RuntimeError(
                f"WORK_PRODUCTS: {WORK_PRODUCTS_COLUMN} has invalid type for "
                f"{wbs_run_id}: {type(data).__name__}"
            )
        logger.info("WORK_PRODUCTS: Loaded register for %s (%d chars)", wbs_run_id, len(data))
        return data

    def save_register(self, wbs_run_id: str, data: str) -> None:
        """Save a serialized register for a WBS run.

        Args:
            wbs_run_id: The WBS record ID.
                When ``work_product_run_id`` was set on the adapter,
                that shared key is used instead.
            data: Serialized JSON string from ``WorkProductRegister.serialize()``.
        """
        key = self._resolve_storage_key(wbs_run_id)
        result = self._state_service.update_state(
            THINKING_NAMESPACE,
            {"table": THINKING_WBS_TABLE, "filters": {"id": key}},
            {WORK_PRODUCTS_COLUMN: data},
        )
        # FAIL-FAST: the update MUST touch exactly the one WBS row. A 0-count is a
        # missing/deleted row silently no-op'd; a >1 count is a key collision —
        # neither may log success.
        updated = _affected_count(result, "save", key)
        if updated != 1:
            raise RuntimeError(
                f"Work-product register save for WBS {key} affected {updated} rows "
                "(expected exactly 1 — the WBS row is missing or deleted)"
            )
        logger.info("WORK_PRODUCTS: Saved register for %s (%d chars)", key, len(data))
