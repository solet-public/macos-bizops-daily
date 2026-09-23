"""metrics.py — persists one ``vetting_runs`` row per run via the state interface.

The metrics writer touches its OWN ``vetting_runs`` namespace only, through the
platform state interface — no raw SQL, no ``execute_sql``, no foreign namespace
(RB-STATE / RB-NAMESPACE). It writes with idempotent ``upsert_state`` (keyed on
``run_id``) and then prunes to a bounded retention so the metrics trail cannot
become its own unbounded leak (F1 §3, design-brief §3.5).

The :class:`StateWriter` Protocol is the narrow seam onto the state interface.
Wave 2 binds it to ``service_interface::state_service::*`` (in-process via
``orchestrator.get_service`` or the MCP ``process_call`` path); Wave 1 uses an
in-memory implementation (``samples.InMemoryStateWriter``) for the dogfood run.
If a needed read verb is ever missing from the interface, that is flagged to the
coordinator — never worked around by reaching into a table.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from ananta.interfaces.state_service_protocol import StateServiceProtocol

from .live_state import VETTING_RUNS_NAMESPACE, VETTING_RUNS_TABLE, _unwrap  # noqa: PLC2701 — the sync state plumbing persist_run_sync shares with read_vetting_run (own namespace, no raw SQL)
from .run_record import RunMetrics

_RUN_ID_COLUMN = "run_id"
_STARTED_COLUMN = "started"
_DEFAULT_RETENTION = 50
# query_ordered's hard limit (state filter grammar, Gap-C). Retention must stay
# below it so a single prune sees enough of the oldest rows to trim the excess.
_RETENTION_SCAN_LIMIT = 100
_ORDER_OLDEST_FIRST: tuple[tuple[str, str], ...] = ((_STARTED_COLUMN, "asc"), (_RUN_ID_COLUMN, "asc"))


class StateWriter(Protocol):
    """The subset of the platform state interface the metrics writer needs.

    Every call targets the writer's own ``vetting_runs`` namespace. The methods
    mirror the sanctioned state-interface primitives (``upsert_state``,
    ``query_ordered``, ``delete_records``); no method expresses a raw query, a
    join, or a foreign namespace.
    """

    async def upsert_state(
        self,
        *,
        namespace: str,
        data: Mapping[str, object],
        conflict_columns: Sequence[str],
    ) -> None:
        """Insert or update one row, idempotent on ``conflict_columns``."""
        ...

    async def query_ordered(
        self,
        *,
        namespace: str,
        order_by: Sequence[tuple[str, str]],
        limit: int,
    ) -> list[Mapping[str, object]]:
        """Ordered, bounded read of the own namespace (retention scan)."""
        ...

    async def delete_records(
        self,
        *,
        namespace: str,
        filters: Mapping[str, object],
    ) -> int:
        """Delete rows matching ``filters``; returns the rows-affected count."""
        ...


@dataclass(frozen=True, slots=True)
class MetricsWriter:
    """Writes and bounds the ``vetting_runs`` metrics trail (F1 §3)."""

    state: StateWriter
    retention: int = _DEFAULT_RETENTION

    def __post_init__(self) -> None:
        if not 1 <= self.retention < _RETENTION_SCAN_LIMIT:
            raise ValueError(f"retention must be in [1, {_RETENTION_SCAN_LIMIT - 1}], got {self.retention}")

    async def persist(self, metrics: RunMetrics) -> None:
        """Upsert one run row, then prune the trail to the retention bound."""
        await self.state.upsert_state(
            namespace=VETTING_RUNS_NAMESPACE,
            data=metrics.to_dict(),
            conflict_columns=(_RUN_ID_COLUMN,),
        )
        await self._prune()

    async def _prune(self) -> None:
        """Delete the oldest rows beyond the retention bound (own namespace only)."""
        oldest_first = await self.state.query_ordered(
            namespace=VETTING_RUNS_NAMESPACE,
            order_by=_ORDER_OLDEST_FIRST,
            limit=_RETENTION_SCAN_LIMIT,
        )
        for run_id in _prune_targets(oldest_first, self.retention):
            await self.state.delete_records(
                namespace=VETTING_RUNS_NAMESPACE,
                filters={_RUN_ID_COLUMN: run_id},
            )


def _prune_targets(oldest_first: Sequence[Mapping[str, object]], retention: int) -> list[object]:
    """The ``run_id`` values to hard-delete to bound the trail to ``retention`` (oldest-first excess).

    The pure retention DECISION, shared by the async ``MetricsWriter._prune`` and the sync
    ``persist_run_sync`` so the bound lives in ONE place (the two callers differ only in async-vs-sync I/O).
    """
    excess = len(oldest_first) - retention
    return [row[_RUN_ID_COLUMN] for row in oldest_first[:excess]] if excess > 0 else []


def persist_run_sync(state_service: StateServiceProtocol, metrics: RunMetrics, *, retention: int = _DEFAULT_RETENTION) -> None:
    """SYNCHRONOUS persist + retention-prune for the vet_codebase ``persist`` opt-in.

    The mirror of :meth:`MetricsWriter.persist` over the raw sync ``StateServiceProtocol`` — the verb is
    synchronous and runs in the executor thread where ``state_service`` is directly callable, so no async
    machinery is needed. Own ``vetting_runs`` namespace only, sanctioned primitives only (upsert_state +
    query_ordered + hard-delete delete_records), NO raw SQL; shares the retention decision (``_prune_targets``)
    with the async writer so the bound cannot drift.
    """
    if not 1 <= retention < _RETENTION_SCAN_LIMIT:
        raise ValueError(f"retention must be in [1, {_RETENTION_SCAN_LIMIT - 1}], got {retention}")
    _unwrap(
        state_service.upsert_state(
            VETTING_RUNS_NAMESPACE,
            {"table": VETTING_RUNS_TABLE, "record": metrics.to_dict(), "conflict_columns": [_RUN_ID_COLUMN]},
        ),
        op="upsert_state",
    )
    scan = _unwrap(
        state_service.query_ordered(
            VETTING_RUNS_NAMESPACE,
            {"table": VETTING_RUNS_TABLE, "filters": {}, "order_by": [list(pair) for pair in _ORDER_OLDEST_FIRST], "limit": _RETENTION_SCAN_LIMIT},
        ),
        op="query_ordered",
    ).get("records", [])
    oldest_first = scan if isinstance(scan, list) else []
    for run_id in _prune_targets(oldest_first, retention):
        state_service.delete_records(
            VETTING_RUNS_NAMESPACE,
            {"table": VETTING_RUNS_TABLE, "filters": {_RUN_ID_COLUMN: run_id}, "soft_delete": False},
        )
