"""live_state.py — the LIVE ``StateWriter`` binding for the vetting_runs trail (B2).

The metrics writer (``metrics.py``) targets the async ``StateWriter`` Protocol;
Wave-1 runs it against ``samples.InMemoryStateWriter``. :class:`LiveStateWriter`
is the production binding: it wraps the platform ``StateServiceProtocol`` (which
is SYNC and returns an ``ActionResult``) and satisfies the async Protocol by
dispatching each sync call to a worker thread (``asyncio.to_thread``) and
unwrapping the ``ActionResult`` fail-loud.

State discipline (RB-STATE / RB-NAMESPACE): it touches ONLY the plugin's own
``vetting_runs`` namespace, through the sanctioned state primitives
(``upsert_state`` / ``query_ordered`` / ``delete_records``) — no raw SQL, no
``execute_sql``, no foreign namespace, no join. The retention prune HARD-deletes
(``soft_delete=False``) so an expired metrics row is gone, not tombstoned into
its own unbounded trail (the metrics trail must not become its own leak, F1 §3).

The ``vetting_runs`` schema is declared here as a :class:`SchemaDefinition`
(declarative, never hand-DDL) and installed through the plugin's
``get_schema_definitions`` lifecycle (W3-C wires the live construction + install).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from ananta.core.domain.enums import ActionStatus
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import ColumnDefinition, SchemaDefinition, TableSchema

# The plugin's OWN dedicated namespace + table (metrics.py writes here). Table
# materializes as ``vetting_runs__vetting_runs`` under the state interface.
VETTING_RUNS_NAMESPACE = "vetting_runs"
VETTING_RUNS_TABLE = "vetting_runs"
_VETTING_RUNS_ID_PREFIX = "vr"

# The delete-count key varies across storage backends; the retention prune deletes
# one row per call and does NOT rely on the count, so this is best-effort only.
_DELETE_COUNT_KEYS: tuple[str, ...] = ("deleted", "rows_affected", "records_deleted", "count")


class LiveStateError(RuntimeError):
    """A state-service call for the vetting_runs trail did not complete (fail-loud)."""


def get_vetting_runs_schema() -> SchemaDefinition:
    """Declarative ``vetting_runs`` schema (F1 §3) — one metrics row per run.

    ``run_id`` is the unique upsert key; ``started`` is the retention ordering key;
    the nested aggregates ride JSON columns; ``survival_rate`` is a nullable REAL;
    ``substrate`` records which inference engine reviewed/refuted
    (heuristic / local_inference / subscription).
    """
    columns = {
        "run_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True, unique=True, description="Unique run id — the upsert conflict key."),
        "target": ColumnDefinition(
            type=ColumnType.JSON,
            not_null=True,
            description=(
                "{repo, ref, scope} — what was examined; foreign runs may also "
                "carry the bounded source_role_partition and technology_fingerprint "
                "(scoped evidence plus capability-routing needs) in this existing JSON column."
            ),
        ),
        "started": ColumnDefinition(type=ColumnType.TEXT, not_null=True, description="ISO-8601 UTC run start (retention ordering key)."),
        "finished": ColumnDefinition(type=ColumnType.TEXT, description="ISO-8601 UTC run finish."),
        "substrate": ColumnDefinition(type=ColumnType.TEXT, not_null=True, description="Which inference engine reviewed/refuted: heuristic | local_inference | subscription."),
        "layers_run": ColumnDefinition(type=ColumnType.JSON, description="Layers executed this run (L1/L2/L3)."),
        "files_examined": ColumnDefinition(type=ColumnType.JSON, description="Per-scanner coverage evidence."),
        "counts_by_severity": ColumnDefinition(type=ColumnType.JSON, description="Severity histogram."),
        "counts_by_dimension": ColumnDefinition(type=ColumnType.JSON, description="Dimension histogram."),
        "survival_rate": ColumnDefinition(type=ColumnType.REAL, description="L2->L3 precision proxy; NULL when nothing was verified."),
        "coverage_gaps": ColumnDefinition(type=ColumnType.JSON, description="Scanners that could not run, each with its reason."),
        "allowlist_delta": ColumnDefinition(type=ColumnType.JSON, description="Tracked-debt burn-down snapshot."),
        "structural_metrics": ColumnDefinition(type=ColumnType.JSON, description="R8-1: per-run structural-metrics distribution + aggregates + worst-offenders (lizard); NULL when not run."),
        "dead_symbols": ColumnDefinition(type=ColumnType.JSON, description="R9-A: per-run candidate-dead-symbols list (vulture 60%-class; L2 targeting evidence); NULL when not run."),
        "report": ColumnDefinition(type=ColumnType.TEXT, description="W3C-C3a: the severity-ranked markdown report text — the get_vetting_run read-verb serves it by run_id so the joseki's L2/L3 steps read it without carrying a prior step's runtime result; NULL for a metrics-only subset run."),
    }
    return SchemaDefinition(
        namespace=VETTING_RUNS_NAMESPACE,
        tables={
            VETTING_RUNS_TABLE: TableSchema(
                table_name=VETTING_RUNS_TABLE, columns=columns, id_prefix=_VETTING_RUNS_ID_PREFIX,
            ),
        },
    )


def _unwrap(result: Mapping[str, object], *, op: str) -> Mapping[str, object]:
    """Return the ``data`` payload of a COMPLETED ActionResult, else raise fail-loud."""
    if result.get("action_status") != ActionStatus.COMPLETED.value:
        raise LiveStateError(f"vetting_runs {op} did not complete: {result.get('error')!r}")
    data = result.get("data")
    return data if isinstance(data, Mapping) else {}


@dataclass(frozen=True, slots=True)
class LiveStateWriter:
    """Production ``StateWriter`` — an async-over-sync bridge onto ``StateServiceProtocol``.

    Own ``vetting_runs`` namespace only; sanctioned state primitives only. Each
    method hands the sync state call to a worker thread and unwraps the
    ``ActionResult``, so the async ``metrics.MetricsWriter`` runs unchanged against
    a synchronous state service.
    """

    state_service: StateServiceProtocol

    async def upsert_state(
        self, *, namespace: str, data: Mapping[str, object], conflict_columns: Sequence[str],
    ) -> None:
        result = await asyncio.to_thread(
            self.state_service.upsert_state,
            namespace,
            {"table": VETTING_RUNS_TABLE, "record": dict(data), "conflict_columns": list(conflict_columns)},
        )
        _unwrap(result, op="upsert_state")

    async def query_ordered(
        self, *, namespace: str, order_by: Sequence[tuple[str, str]], limit: int,
    ) -> list[Mapping[str, object]]:
        result = await asyncio.to_thread(
            self.state_service.query_ordered,
            namespace,
            {
                "table": VETTING_RUNS_TABLE,
                "filters": {},
                "order_by": [[column, direction] for column, direction in order_by],
                "limit": limit,
            },
        )
        records = _unwrap(result, op="query_ordered").get("records", [])
        return list(cast("Sequence[Mapping[str, object]]", records))

    async def delete_records(
        self, *, namespace: str, filters: Mapping[str, object],
    ) -> int:
        result = await asyncio.to_thread(
            self.state_service.delete_records,
            namespace,
            {"table": VETTING_RUNS_TABLE, "filters": dict(filters), "soft_delete": False},
        )
        payload = _unwrap(result, op="delete_records")
        for key in _DELETE_COUNT_KEYS:
            value = payload.get(key)
            if isinstance(value, int):
                return value
        return 0


def read_vetting_run(state_service: StateServiceProtocol, run_id: str) -> Mapping[str, object] | None:
    """Read ONE persisted ``vetting_runs`` row by ``run_id`` — the ``get_vetting_run`` read-verb's lookup.

    Uses the SYNC ``query_state`` single-namespace equality filter DIRECTLY (the read-verb is synchronous,
    so it does not go through the async ``LiveStateWriter`` adapter): sanctioned state primitive, own
    ``vetting_runs`` namespace, no join, no raw SQL. Returns the row (``run_id`` is UNIQUE) or None when absent.
    """
    result = state_service.query_state(
        VETTING_RUNS_NAMESPACE,
        {"table": VETTING_RUNS_TABLE, "filters": {"run_id": run_id}},
    )
    records = _unwrap(result, op="query_state").get("records", [])
    rows = cast("list[Mapping[str, object]]", records) if isinstance(records, list) else []
    return rows[0] if rows else None


def read_recent_runs(state_service: StateServiceProtocol, limit: int) -> list[Mapping[str, object]]:
    """The most-recent ``vetting_runs`` rows, NEWEST first — the scheduled self-vet's regression compare reads
    the prior row here BEFORE it persists the new one. SYNC single-namespace ``query_ordered`` (own namespace,
    composite tie-safe order, no raw SQL); an empty trail returns ``[]``.
    """
    result = state_service.query_ordered(
        VETTING_RUNS_NAMESPACE,
        {"table": VETTING_RUNS_TABLE, "filters": {}, "order_by": [["started", "desc"], ["run_id", "desc"]], "limit": limit},
    )
    records = _unwrap(result, op="query_ordered").get("records", [])
    return cast("list[Mapping[str, object]]", records) if isinstance(records, list) else []
