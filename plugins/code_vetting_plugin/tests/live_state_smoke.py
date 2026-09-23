"""live_state_smoke.py — the B2 LIVE StateWriter adapter, driven against a FAKE
sync state_service (no real DB, no live platform).

Pins the async-over-sync bridge + the state-interface arg mapping the live
``vetting_runs`` trail depends on:
  * upsert_state  -> state_service.upsert_state(ns, {table, record, conflict_columns})
  * query_ordered -> state_service.query_ordered(ns, {table, filters, order_by, limit}); rows at data.records
  * delete_records-> state_service.delete_records(ns, {table, filters, soft_delete: FALSE})  (hard-delete prune)
  * ActionResult unwrap: a non-COMPLETED result raises LiveStateError (fail-loud, never silent)
  * MetricsWriter(state=LiveStateWriter(...)) runs UNCHANGED and enforces retention end-to-end
  * the `substrate` provenance field round-trips into the stored row
  * the declarative vetting_runs SchemaDefinition shape (run_id unique key, substrate col, survival_rate REAL)
  * NO raw SQL / execute_sql / foreign namespace — only the 3 sanctioned verbs are ever called.

Run directly: ``.venv/bin/python3 plugins/code_vetting_plugin/tests/live_state_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import asyncio
import sys
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ananta.core.domain.enums import ActionStatus
from ananta.types.column_types import ColumnType
from code_vetting_plugin.live_state import (
    VETTING_RUNS_NAMESPACE,
    VETTING_RUNS_TABLE,
    LiveStateError,
    LiveStateWriter,
    get_vetting_runs_schema,
)
from code_vetting_plugin.metrics import MetricsWriter
from code_vetting_plugin.run_record import AllowlistDelta, RunTarget, build_run_metrics

_CHECKS_RUN: list[str] = []
_COMPLETED = ActionStatus.COMPLETED.value


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _ok(data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"action_status": _COMPLETED, "data": data or {}, "actions": [], "error": None, "timestamp": ""}


class FakeStateService:
    """A synchronous, ActionResult-returning stand-in for the state service.

    Backs one table by (namespace, table) -> {run_id: record}; records every call
    so the smoke can assert the exact arg mapping. Only the 3 sanctioned verbs the
    adapter uses are implemented — any OTHER verb (execute_sql, write_state, a
    foreign namespace) would be an AttributeError, so a leak is structurally caught.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_next_upsert = False

    def upsert_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("upsert_state", namespace, data))
        if self.fail_next_upsert:
            return {"action_status": "error", "data": {}, "actions": [], "error": {"code": "boom"}, "timestamp": ""}
        table = str(data["table"])
        record = dict(data["record"])
        key = str(record[str(data["conflict_columns"][0])])
        self.rows.setdefault((namespace, table), {})[key] = record
        return _ok({"upserted": 1})

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("query_ordered", namespace, data))
        table = str(data["table"])
        stored = list(self.rows.get((namespace, table), {}).values())
        order_by = data["order_by"]
        column, direction = order_by[0][0], order_by[0][1]
        stored.sort(key=lambda row: str(row.get(column, "")), reverse=direction == "desc")
        return _ok({"records": stored[: int(data["limit"])]})

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("delete_records", namespace, query))
        table = str(query["table"])
        bucket = self.rows.get((namespace, table), {})
        filters = query["filters"]
        doomed = [k for k, row in bucket.items() if all(row.get(c) == v for c, v in filters.items())]
        for k in doomed:
            del bucket[k]
        return _ok({"deleted": len(doomed)})


def _metrics(run_id: str, started: str, substrate: str = "subscription") -> Any:
    return build_run_metrics(
        run_id=run_id,
        target=RunTarget(repo="example", ref="deadbeef", scope="b2 smoke"),
        started=started,
        finished=started,
        substrate=substrate,
        layers_run=[],
        findings=[],
        coverage=[],
        allowlist_delta=AllowlistDelta(totals={}),
    )


async def _check_arg_mapping(fake: FakeStateService) -> None:
    writer = LiveStateWriter(state_service=fake)  # type: ignore[arg-type]
    await writer.upsert_state(
        namespace=VETTING_RUNS_NAMESPACE, data=_metrics("vr-1", "2026-07-20T00:00:00Z").to_dict(),
        conflict_columns=("run_id",),
    )
    _, ns, payload = fake.calls[-1]
    _check("upsert targets the vetting_runs namespace + table", ns == VETTING_RUNS_NAMESPACE and payload["table"] == VETTING_RUNS_TABLE, str(payload))
    _check("upsert passes conflict_columns=[run_id]", payload["conflict_columns"] == ["run_id"], str(payload["conflict_columns"]))
    _check("upsert record carries the substrate field", payload["record"]["substrate"] == "subscription", str(payload["record"].get("substrate")))

    rows = await writer.query_ordered(
        namespace=VETTING_RUNS_NAMESPACE, order_by=(("started", "asc"), ("run_id", "asc")), limit=100,
    )
    _, ns, payload = fake.calls[-1]
    _check("query_ordered maps order_by to [[col, dir]] pairs", payload["order_by"] == [["started", "asc"], ["run_id", "asc"]], str(payload["order_by"]))
    _check("query_ordered passes the limit", payload["limit"] == 100, str(payload["limit"]))
    _check("query_ordered returns data.records", len(rows) == 1 and rows[0]["run_id"] == "vr-1", str(rows))

    n = await writer.delete_records(namespace=VETTING_RUNS_NAMESPACE, filters={"run_id": "vr-1"})
    _, ns, payload = fake.calls[-1]
    _check("delete HARD-deletes (soft_delete=False)", payload["soft_delete"] is False, str(payload))
    _check("delete returns the rows-affected count", n == 1, str(n))


async def _check_metrics_writer_end_to_end() -> None:
    fake = FakeStateService()
    writer = MetricsWriter(state=LiveStateWriter(state_service=fake), retention=3)  # type: ignore[arg-type]
    for i in range(5):
        await writer.persist(_metrics(f"vr-{i}", f"2026-07-20T00:0{i}:00Z"))
    kept = fake.rows.get((VETTING_RUNS_NAMESPACE, VETTING_RUNS_TABLE), {})
    _check("retention prune (via the live adapter) kept exactly `retention` rows", len(kept) == 3, f"kept {len(kept)}")
    _check("retention kept the NEWEST runs (oldest pruned)", set(kept) == {"vr-2", "vr-3", "vr-4"}, str(sorted(kept)))
    _check("the substrate field round-trips into the stored row", all(r["substrate"] == "subscription" for r in kept.values()), str(kept))
    verbs = {verb for verb, _, _ in fake.calls}
    _check("ONLY the 3 sanctioned verbs were called (no raw SQL / foreign ns)", verbs <= {"upsert_state", "query_ordered", "delete_records"}, str(verbs))


async def _check_fail_loud() -> None:
    fake = FakeStateService()
    fake.fail_next_upsert = True
    writer = LiveStateWriter(state_service=fake)  # type: ignore[arg-type]
    raised = False
    try:
        await writer.upsert_state(namespace=VETTING_RUNS_NAMESPACE, data={"run_id": "vr-x"}, conflict_columns=("run_id",))
    except LiveStateError:
        raised = True
    _check("a non-COMPLETED ActionResult raises LiveStateError (fail-loud, never silent)", raised)


def _check_schema() -> None:
    schema = get_vetting_runs_schema()
    _check("schema namespace is vetting_runs", schema.namespace == VETTING_RUNS_NAMESPACE, schema.namespace)
    _check("schema declares the vetting_runs table", VETTING_RUNS_TABLE in schema.tables, str(schema.tables.keys()))
    cols = schema.tables[VETTING_RUNS_TABLE].columns
    _check("run_id is the unique key", cols["run_id"].unique is True, str(cols["run_id"]))
    _check("substrate is a declared column", "substrate" in cols and cols["substrate"].type is ColumnType.TEXT, str(cols.get("substrate")))
    _check("survival_rate is a nullable REAL", cols["survival_rate"].type is ColumnType.REAL and not cols["survival_rate"].not_null, str(cols["survival_rate"]))
    _check("nested aggregates ride JSON columns", cols["target"].type is ColumnType.JSON and cols["counts_by_severity"].type is ColumnType.JSON, "")


def main() -> int:
    try:
        asyncio.run(_check_arg_mapping(FakeStateService()))
        asyncio.run(_check_metrics_writer_end_to_end())
        asyncio.run(_check_fail_loud())
        _check_schema()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"live_state_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
