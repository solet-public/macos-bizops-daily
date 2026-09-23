"""vet_persist_smoke.py — W3-C persist micro-slice: the vet_codebase ``persist`` opt-in + sync write path.

The joseki's step-1 pin needs a PERSISTED run to read by run_id; the bare vet_codebase verb returned inline
and did not persist. This slice adds the opt-in. Pins:

  * SCHEMA DECLARATION (red-first for the create-schema fix): CodeVettingPlugin is a SchemaProvider and
    DECLARES the vetting_runs table via get_schema_definitions, so the platform creates it at BOOT
    (collect_schemas, before any write). The proving run caught the original defect — the plugin was NOT a
    SchemaProvider, so the live table was never created and the first live persist failed
    ("Table must be created via create_table() first"). Neither hermetic write path created it, so the fake
    now ENFORCES create-before-upsert (a fresh trail rejects the write — the live failure reproduced) so this
    class cannot pass hermetically again.
  * SYNC WRITE + PRUNE (``persist_run_sync``): over a CREATED trail, a run round-trips through read_vetting_run
    at the joseki field paths (report, structural_metrics.literals[], dead_symbols.candidates[]); the
    bounded-retention prune hard-deletes the oldest beyond the bound.
  * VERB GATE (red-first): ``_run(..., persist=False)`` writes NOTHING (bare callers byte-identical — the
    return payload keys are unchanged); ``_run(..., persist=True)`` writes exactly one row that round-trips.
  * PARAM WIRING (red-first): vet_codebase reads ``persist`` and threads it to _run — off → no write, on → write.

Hermetic: a fake StateServiceProtocol backs the trail with create-before-upsert semantics; a stub scan_fn
feeds canned L1 output (no real scanners). Run directly or via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
import tempfile
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import code_vetting_plugin.plugin as plugin_module
from ananta.core.plugins.protocols import SchemaProvider
from code_vetting_plugin.live_state import (
    VETTING_RUNS_NAMESPACE,
    VETTING_RUNS_TABLE,
    LiveStateError,
    read_vetting_run,
)
from code_vetting_plugin.metrics import persist_run_sync
from code_vetting_plugin.run_record import (
    AllowlistDelta,
    CoverageRecord,
    RunMetrics,
    RunTarget,
    build_run_metrics,
)
from code_vetting_plugin.runner import L1ReportData
from code_vetting_plugin.scanners.dead_code import DeadSymbol, DeadSymbolsReport

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None, "timestamp": ""}


def _uncreated_error(namespace: str, table: str) -> dict[str, Any]:
    return {"action_status": "error", "data": {}, "actions": [], "error": {"code": "state.upsert_failed", "message": f"Cannot generate ID for table '{namespace}__{table}': id_prefix not registered. Table must be created via create_table() first."}, "timestamp": ""}


class FakeStateService:
    """A StateServiceProtocol stand-in that ENFORCES create-before-upsert (mirrors the live constraint the
    proving run hit), backs the vetting_runs table, and records upsert/delete counts."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.upserts = 0
        self.deletes = 0
        self._created: set[tuple[str, str]] = set()

    def install_schema(self, namespace: str, table: str) -> None:
        """Simulate boot's collect_schemas → create-table for a declared table."""
        self._created.add((namespace, table))

    def _guard(self, namespace: str, table: str) -> dict[str, Any] | None:
        return None if (namespace, table) in self._created else _uncreated_error(namespace, table)

    def upsert_state(self, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        table = str(payload["table"])
        guard = self._guard(namespace, table)
        if guard is not None:
            return guard
        self.upserts += 1
        record = dict(payload["record"])
        self.rows[str(record["run_id"])] = record
        return _ok({})

    def query_ordered(self, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        guard = self._guard(namespace, str(payload["table"]))
        if guard is not None:
            return guard
        ordered = sorted(self.rows.values(), key=lambda r: (r["started"], r["run_id"]))
        return _ok({"records": ordered[: int(payload["limit"])]})

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        guard = self._guard(namespace, str(filters["table"]))
        if guard is not None:
            return guard
        run_id = str(filters.get("filters", {}).get("run_id", ""))
        return _ok({"records": [self.rows[run_id]] if run_id in self.rows else []})

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        guard = self._guard(namespace, str(query["table"]))
        if guard is not None:
            return guard
        run_id = str(query.get("filters", {}).get("run_id", ""))
        if run_id in self.rows:
            del self.rows[run_id]
            self.deletes += 1
        return _ok({"deleted": 1})


class _StubPlugin(plugin_module.CodeVettingPlugin):
    """CodeVettingPlugin with the state-service acquisition + worktree stubbed (no orchestrator/readiness)."""

    def __init__(self, state_service: FakeStateService, worktree: Path) -> None:
        super().__init__()
        self._stub_state = state_service
        self._worktree_root = worktree

    def _state_service(self) -> Any:  # noqa: ANN401 — the fake stands in for StateServiceProtocol
        return self._stub_state


def _installed_state() -> FakeStateService:
    """A fake with the vetting_runs table already CREATED (as boot's collect_schemas would)."""
    state = FakeStateService()
    state.install_schema(VETTING_RUNS_NAMESPACE, VETTING_RUNS_TABLE)
    return state


def _install_declared_schema(state: FakeStateService, plugin: plugin_module.CodeVettingPlugin) -> None:
    """Create every table the plugin DECLARES via get_schema_definitions — what the platform does at boot."""
    for schema in plugin.get_schema_definitions():
        tables = schema.tables.values() if isinstance(schema.tables, dict) else schema.tables
        for table in tables:
            state.install_schema(schema.namespace, table.table_name)


def _metrics(run_id: str, started: str, *, report: str) -> RunMetrics:
    return build_run_metrics(
        run_id=run_id,
        target=RunTarget(repo="example", ref="HEAD", scope="self"),
        started=started,
        finished="2026-07-21T00:01:00Z",
        substrate="heuristic",
        layers_run=[],
        findings=[],
        coverage=[],
        allowlist_delta=AllowlistDelta(totals={}),
        structural_metrics={"literals": [{"value": "TODO", "count": 5}]},
        dead_symbols={"candidates": [{"name": "dead_fn", "kind": "function"}]},
        report=report,
    )


def _check_schema_declaration() -> None:
    plugin = plugin_module.CodeVettingPlugin()
    _check("CodeVettingPlugin is a SchemaProvider (declares its owned tables at boot)", isinstance(plugin, SchemaProvider), "")
    declared = {(s.namespace, t.table_name) for s in plugin.get_schema_definitions() for t in (s.tables.values() if isinstance(s.tables, dict) else s.tables)}
    _check("plugin DECLARES the vetting_runs table (so collect_schemas creates it before any write)", (VETTING_RUNS_NAMESPACE, VETTING_RUNS_TABLE) in declared, str(declared))


def _check_create_before_upsert() -> None:
    fresh = FakeStateService()
    raised = False
    try:
        persist_run_sync(fresh, _metrics("vr-x", "2026-07-21T00:00:00Z", report="r"))
    except LiveStateError:
        raised = True
    _check("fresh trail (table NOT created) REJECTS the upsert — the live failure reproduced hermetically", raised and fresh.upserts == 0, str(fresh.upserts))
    installed = FakeStateService()
    _install_declared_schema(installed, plugin_module.CodeVettingPlugin())
    persist_run_sync(installed, _metrics("vr-y", "2026-07-21T00:00:00Z", report="r"))
    _check("installing the plugin's DECLARED schema (boot's job) lets the write through", installed.upserts == 1, str(installed.upserts))


def _check_sync_write_and_prune() -> None:
    state = _installed_state()
    persist_run_sync(state, _metrics("vr-1", "2026-07-21T00:00:00Z", report="# report one"))
    _check("persist_run_sync wrote one row", state.upserts == 1 and "vr-1" in state.rows, str(state.upserts))
    row = read_vetting_run(state, "vr-1")
    assert row is not None
    _check("sync write round-trips report", row["report"] == "# report one", str(row.get("report")))
    _check("sync write round-trips literal_table at structural_metrics.literals[]", row["structural_metrics"]["literals"][0]["value"] == "TODO", str(row["structural_metrics"]))
    _check("sync write round-trips candidate at dead_symbols.candidates[]", row["dead_symbols"]["candidates"][0]["name"] == "dead_fn", str(row["dead_symbols"]))
    persist_run_sync(state, _metrics("vr-2", "2026-07-21T00:00:01Z", report="r2"), retention=2)
    persist_run_sync(state, _metrics("vr-3", "2026-07-21T00:00:02Z", report="r3"), retention=2)
    _check("bounded-retention prune hard-deletes the oldest beyond the bound", "vr-1" not in state.rows and {"vr-2", "vr-3"} <= set(state.rows), str(sorted(state.rows)))


def _stub_scan(tree: object, run_id: str) -> tuple[list[Any], list[CoverageRecord], L1ReportData]:  # noqa: ARG001 — stub matches ScanFn
    dead = DeadSymbolsReport(tool="vulture", tool_version="2.16", total=1, by_kind={"function": 1}, candidates=(DeadSymbol(file="a.py", line=1, name="dead_fn", kind="function", confidence=60, dead_lines=3),))
    return [], [CoverageRecord(scanner="stub", ran=True, files_examined=1)], L1ReportData(structural_metrics=None, dead_symbols=dead)


def _foreign_fixture(base: Path) -> Path:
    root = base / "target"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    return root


def _check_run_gate(base: Path) -> None:
    state = _installed_state()
    plugin = _StubPlugin(state, base / "worktree")
    (base / "worktree").mkdir()
    fixture = _foreign_fixture(base)

    off = plugin._run(_stub_scan, scope="s", tag="l1", target_path=str(fixture), persist=False)  # noqa: SLF001 — the persist gate is the unit under test
    _check("persist=False writes NOTHING (bare caller byte-identical)", state.upserts == 0, str(state.upserts))

    on = plugin._run(_stub_scan, scope="s", tag="l1", target_path=str(fixture), persist=True)  # noqa: SLF001
    _check("persist=True writes exactly one row", state.upserts == 1, str(state.upserts))
    _check("persist does NOT change the payload key set (side-effect only)", set(off) == set(on), f"{set(off) ^ set(on)}")
    row = read_vetting_run(state, str(on["run_id"]))
    assert row is not None
    _check("persisted run round-trips: substrate=heuristic, L1 layer, report + candidates present", row["substrate"] == "heuristic" and row["layers_run"] == ["L1_deterministic"] and bool(row["report"]) and row["dead_symbols"]["candidates"][0]["name"] == "dead_fn", str({k: row[k] for k in ("substrate", "layers_run")}))


def _check_verb_param_wiring(base: Path) -> None:
    """vet_codebase must READ the persist param and thread it to _run (monkeypatch run_all → stub)."""
    original = plugin_module.run_all
    plugin_module.run_all = lambda tree, run_id, *, execute_target_toolchain=False: _stub_scan(tree, run_id)  # noqa: ARG005
    try:
        state = _installed_state()
        plugin = _StubPlugin(state, base / "worktree")
        (base / "worktree").mkdir()
        fixture = _foreign_fixture(base)
        plugin.vet_codebase({"target_path": str(fixture)}, {})
        _check("vet_codebase default (no persist) writes nothing", state.upserts == 0, str(state.upserts))
        plugin.vet_codebase({"target_path": str(fixture), "persist": True}, {})
        _check("vet_codebase persist=True threads through to a write", state.upserts == 1, str(state.upserts))
    finally:
        plugin_module.run_all = original


def main() -> int:
    try:
        _check_schema_declaration()
        _check_create_before_upsert()
        _check_sync_write_and_prune()
        with tempfile.TemporaryDirectory() as tmp:
            _check_run_gate(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_verb_param_wiring(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"vet_persist_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
