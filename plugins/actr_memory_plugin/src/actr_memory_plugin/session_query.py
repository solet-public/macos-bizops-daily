"""Session query helpers for ACT-R memory plugin.

All reads/aggregates over ``core__memory_events`` go through the state
interface (``query_ordered`` / ``query_state`` / transaction-scoped
aggregates) per the SQL-lockdown mandate — never raw ``execute_sql``.
See ``ananta_platform/16_store_abstraction_STATE_INTERFACE_FILTER_GRAMMAR.md``.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.timestamps import to_naive_utc
from ananta.error_handling import FrameworkError

from .record_helpers import parse_metadata

logger = logging.getLogger(__name__)


def build_memory_query(
    session_id: str | None,
    max_events: int,
    max_age_hours: int | None,
    namespace_filter: str | None,
) -> dict[str, Any]:
    """Build the ``query_ordered`` ``data`` dict for recent-memory retrieval.

    Memories are global - ``session_id`` is an optional filter. The original
    raw-SQL path applied no ``is_deleted`` filter, so ``include_deleted=True``
    preserves that semantics exactly (``core__memory_events`` is hard-deleted,
    so no soft-deleted rows accumulate either way). ``timestamp`` uses the
    half-open range op with a naive-UTC value (filter values are bound raw).
    ``order_by`` carries an ``id`` tie-break so the composite key is a total
    order (the migrated-away SQL ordered on ``timestamp`` alone).
    """
    filters: dict[str, Any] = {}
    if session_id:
        filters["session_id"] = session_id
    if namespace_filter:
        filters["source_namespace"] = namespace_filter
    if max_age_hours is not None:
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        filters["timestamp"] = {"op": "gt", "value": to_naive_utc(cutoff)}

    return {
        "table": "memory_events",
        "filters": filters,
        "order_by": [["timestamp", "desc"], ["id", "desc"]],
        "limit": max_events,
        "unbounded": max_events > 100,
        "include_deleted": True,
    }


def convert_memory_record(record: dict[str, Any]) -> dict[str, Any]:
    """Project a raw memory_events dict row, parsing the ``metadata`` JSON.

    The state interface (``query_ordered``) returns dict rows; ``metadata`` is a
    JSON-string column, so it is parsed here — the equivalence fix vs the
    migrated-away raw SQL, whose list-shaped DB-API rows were parsed positionally.
    """
    return {
        "id": record.get("id"),
        "session_id": record.get("session_id"),
        "source_namespace": record.get("source_namespace"),
        "event_type": record.get("event_type"),
        "content": record.get("content"),
        "metadata": parse_metadata(record.get("metadata"), record.get("id")),
        "timestamp": record.get("timestamp"),
    }


def query_event_summary(state_service: Any, session_id: str) -> dict[str, Any]:
    """Query total count and timestamp range for session events. Fails fast.

    Uses the transaction-scoped aggregate primitives (``count`` / ``min_value``
    / ``max_value``) — the sanctioned interface verbs for COUNT/MIN/MAX. No
    ``is_deleted`` filter is applied, matching the original raw-SQL semantics
    (the aggregates do not auto-exclude soft-deleted rows; ``memory_events`` is
    hard-deleted regardless). An empty session yields ``count=0`` and
    ``min``/``max`` ``None``.
    """
    spec: dict[str, Any] = {
        "table": "memory_events",
        "filters": {"session_id": session_id},
    }
    ts_spec: dict[str, Any] = {
        "table": "memory_events",
        "column": "timestamp",
        "filters": {"session_id": session_id},
    }

    with state_service.transactional() as txn:
        total = txn.count("core", spec)
        oldest = txn.min_value("core", ts_spec)
        newest = txn.max_value("core", ts_spec)

    return {
        "total_events": total,
        "oldest_event": oldest,
        "newest_event": newest,
    }


def query_namespace_breakdown(
    state_service: Any, session_id: str
) -> dict[str, dict[str, int]]:
    """Query per-namespace event counts. Fails fast.

    The state interface has no GROUP-BY primitive, so this reads the session's
    events (single-namespace, uncapped ``query_state``) and counts per
    ``source_namespace`` in Python — the canonical "restructure to a
    single-namespace shape" remedy. Note: this loads every event row for the
    session, replacing a server-side GROUP BY with a client-side fold.
    """
    result = state_service.query_state(
        namespace="core",
        filters={"table": "memory_events", "filters": {"session_id": session_id}},
    )

    if result.get("action_status") != ActionStatus.COMPLETED.value:
        raise FrameworkError(
            message=f"Failed to query namespace breakdown: {result.get('error')}",
            error_code="memory.query_failed",
        )

    by_namespace: dict[str, dict[str, int]] = {}
    for record in result.get("data", {}).get("records", []):
        namespace = record.get("source_namespace") if isinstance(record, dict) else None
        if namespace:
            by_namespace.setdefault(namespace, {"events": 0})["events"] += 1

    return by_namespace
