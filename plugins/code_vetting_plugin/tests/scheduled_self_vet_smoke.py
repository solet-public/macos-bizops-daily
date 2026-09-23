"""scheduled_self_vet_smoke.py — W3C-3: the trigger_scheduled_self_vet EDGE_SINK heartbeat.

The program's last slice: a DAILY L1-only self-vet fired on a single-slot background executor, persisting a
vetting_runs row per run (the R9-3 trend baseline), with a regression-only queued memory note. Pins:

  * SINGLE-SLOT LEASE (the slot IS the lease): SingleSlotVetExecutor.submit returns True (started) for a free
    slot and False (already_running) while a run is in-flight; the slot frees when the run finishes.
  * FAST-RETURN + R6 BOUNDARY: the verb only SUBMITS + returns a receipt — it touches NO state/memory (those
    are on the background job). Proven with a stub executor that never runs the work + state/memory that RAISE
    if touched.
  * EDGE_SINK REGISTRATION: the verb is processor_policy_category=EDGE_SINK and is NOT in
    get_edge_process_definitions (an EDGE_SINK there → edge_process_mismatch FATAL at boot).
  * REGRESSION PREDICATE: is_regression is True iff blocker OR high worsens vs the prior run (medium/low churn
    silent).
  * BACKGROUND PERSIST + NOTIFY: the background body persists a row (over a boot-created trail) and queues a
    regression memory note (tag code_vetting:self_vet_regression) ONLY on a worse run; a clean run is silent.

Hermetic: fake StateServiceProtocol (create-before-upsert) + fake memory + a stub scan; no real scanners, no
live solet. Run directly or via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import code_vetting_plugin.plugin as plugin_module
from ananta.core.domain.enums import ProcessorPolicyCategory
from code_vetting_plugin.live_state import VETTING_RUNS_NAMESPACE, VETTING_RUNS_TABLE
from code_vetting_plugin.models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
)
from code_vetting_plugin.run_context import (
    _ALLOWLIST_GATES,  # noqa: PLC2701 — the self-vet reads these allowlist files; the fixture must carry them
)
from code_vetting_plugin.runner import L1ReportData
from code_vetting_plugin.scheduled_vet import (
    REGRESSION_MEMORY_TAG,
    SingleSlotVetExecutor,
    is_regression,
)

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None, "timestamp": ""}


class FakeStateService:
    """Create-before-upsert state fake backing the vetting_runs trail (mirrors vet_persist_smoke)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self._created: set[tuple[str, str]] = set()

    def install_schema(self, namespace: str, table: str) -> None:
        self._created.add((namespace, table))

    def _guard(self, namespace: str, table: str) -> dict[str, Any] | None:
        return None if (namespace, table) in self._created else {"action_status": "error", "data": {}, "actions": [], "error": {"code": "state.upsert_failed"}, "timestamp": ""}

    def upsert_state(self, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        guard = self._guard(namespace, str(payload["table"]))
        if guard is not None:
            return guard
        record = dict(payload["record"])
        self.rows[str(record["run_id"])] = record
        return _ok({})

    def query_ordered(self, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        guard = self._guard(namespace, str(payload["table"]))
        if guard is not None:
            return guard
        newest_first = sorted(self.rows.values(), key=lambda r: (r["started"], r["run_id"]), reverse=True)
        return _ok({"records": newest_first[: int(payload["limit"])]})

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        guard = self._guard(namespace, str(filters["table"]))
        if guard is not None:
            return guard
        run_id = str(filters.get("filters", {}).get("run_id", ""))
        return _ok({"records": [self.rows[run_id]] if run_id in self.rows else []})

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        run_id = str(query.get("filters", {}).get("run_id", ""))
        self.rows.pop(run_id, None)
        return _ok({"deleted": 1})


class FakeMemoryService:
    """Records remember() calls so the regression-note queue is assertable."""

    def __init__(self) -> None:
        self.remembered: list[tuple[str, list[str]]] = []

    def remember(self, content: str, tags: list[str] | None = None, **_: object) -> dict[str, Any]:
        self.remembered.append((content, tags or []))
        return {"stored": True}


class _RaisingService:
    """Any attribute access raises — proves the verb path never touches state/memory."""

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        raise AssertionError(f"the trigger verb must not touch the service (accessed {name!r})")


class _StubExecutor:
    """Records submit() without running the work; returns a scripted started/busy result."""

    def __init__(self, *, started: bool) -> None:
        self._started = started
        self.submitted = 0

    def submit(self, work: Any) -> bool:  # noqa: ANN401 — Callable, unused (never run)
        self.submitted += 1
        return self._started


class _StubPlugin(plugin_module.CodeVettingPlugin):
    def __init__(self, state: Any, memory: Any, worktree: Path) -> None:  # noqa: ANN401
        super().__init__()
        self._stub_state = state
        self._stub_memory = memory
        self._worktree_root = worktree

    def _state_service(self) -> Any:  # noqa: ANN401
        return self._stub_state

    def _memory_service(self) -> Any:  # noqa: ANN401
        return self._stub_memory


def _high_finding(run_id: str) -> Finding:
    return Finding.build(
        run_id=run_id, layer=Layer.L1_DETERMINISTIC, dimension=Dimension.SECURITY, severity=Severity.HIGH,
        file="a.py", line=1, constraint_violated="stub:high", evidence="e", provenance=Provenance(source="smoke"),
        context_profile=ContextProfile.PRODUCTION,
    )


def _installed_state() -> FakeStateService:
    state = FakeStateService()
    state.install_schema(VETTING_RUNS_NAMESPACE, VETTING_RUNS_TABLE)
    return state


def _self_vet_fixture(base: Path) -> Path:
    """A tiny tree posing as the self-vet worktree — carries an empty quality_gates/ allowlist set so the
    SELF-vet's allowlist_totals(root) resolves (the real worktree always has these)."""
    root = base / "target"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    gates = root / "quality_gates"
    gates.mkdir()
    for filename in _ALLOWLIST_GATES.values():
        (gates / filename).write_text("", encoding="utf-8")
    return root


def _check_single_slot_lease() -> None:
    executor = SingleSlotVetExecutor(name="smoke-vet")
    running = threading.Event()
    release = threading.Event()

    def blocking_work() -> None:
        running.set()
        release.wait(timeout=5)

    _check("first submit → started (slot free)", executor.submit(blocking_work) is True, "")
    _check("work reached the daemon thread", running.wait(timeout=5), "")
    _check("second submit WHILE in-flight → already_running (slot IS the lease)", executor.submit(lambda: None) is False, "")
    release.set()
    freed = False
    for _ in range(50):
        if executor.submit(lambda: None) is True:
            freed = True
            break
        time.sleep(0.05)
    _check("slot frees after the run finishes → a later submit starts again", freed, "")


def _check_regression_predicate() -> None:
    _check("regression: high worsens (1 vs 0) → True", is_regression({"high": 1}, {"high": 0}) is True, "")
    _check("regression: blocker worsens (1 vs 0) → True", is_regression({"blocker": 1}, {"blocker": 0}) is True, "")
    _check("regression: equal counts → False", is_regression({"high": 3, "blocker": 1}, {"high": 3, "blocker": 1}) is False, "")
    _check("regression: improved (high 0 vs 2) → False", is_regression({"high": 0}, {"high": 2}) is False, "")
    _check("regression: medium/low churn worse but blocker/high equal → SILENT (False)", is_regression({"high": 1, "medium": 9}, {"high": 1, "medium": 0}) is False, "")


def _check_verb_fast_return_and_boundary(base: Path) -> None:
    plugin = _StubPlugin(_RaisingService(), _RaisingService(), base / "wt")
    (base / "wt").mkdir()
    plugin._vet_executor = _StubExecutor(started=True)  # noqa: SLF001 — inject a non-running executor
    started = plugin.trigger_scheduled_self_vet({}, {})
    _check("verb returns COMPLETED", started["action_status"] == "completed", str(started.get("action_status")))
    _check("verb: free slot → 'started' receipt WITHOUT touching state/memory (R6/boundary)", started["data"]["self_vet"] == "started", str(started["data"]))
    plugin._vet_executor = _StubExecutor(started=False)  # noqa: SLF001
    busy = plugin.trigger_scheduled_self_vet({}, {})
    _check("verb: busy slot → 'already_running' receipt", busy["data"]["self_vet"] == "already_running", str(busy["data"]))


def _check_edge_sink_registration() -> None:
    plugin = plugin_module.CodeVettingPlugin()
    _check("trigger_scheduled_self_vet is NOT in get_edge_process_definitions (EDGE_SINK, not EDGE)", "trigger_scheduled_self_vet" not in plugin.get_edge_process_definitions(), str(sorted(plugin.get_edge_process_definitions())))
    meta = plugin_module.CodeVettingPlugin.trigger_scheduled_self_vet._platform_process_metadata  # noqa: SLF001 — pin the decorated category
    category = meta.processor_policy_category
    _check("verb declares processor_policy_category=EDGE_SINK", category in (ProcessorPolicyCategory.EDGE_SINK, ProcessorPolicyCategory.EDGE_SINK.value), str(category))


def _run_background(plugin: _StubPlugin, base: Path, findings: list[Finding]) -> dict[str, Any]:
    fixture = _self_vet_fixture(base)
    original = plugin_module.run_all
    plugin_module.run_all = lambda tree, run_id, *, execute_target_toolchain=False: (findings, [], L1ReportData())  # noqa: ARG005
    try:
        # point the self-vet at the fixture (foreign) so the scan is tiny; _run persists on persist=True
        plugin._worktree_root = fixture  # noqa: SLF001 — self-vet anchors on the worktree
        plugin._run_scheduled_self_vet()  # noqa: SLF001 — the background body is the unit under test
    finally:
        plugin_module.run_all = original
    return {}


def _check_background_persist_and_notify(base: Path) -> None:
    # Regression case: prior run has 0 high; the new run finds 1 high → a regression note is queued.
    state = _installed_state()
    state.rows["vr-prior"] = {"run_id": "vr-prior", "started": "2026-07-20T00:00:00Z", "counts_by_severity": {"high": 0}}
    memory = FakeMemoryService()
    plugin = _StubPlugin(state, memory, base / "wt1")
    (base / "wt1").mkdir()
    _run_background(plugin, base / "reg", [_high_finding("vr-new")])
    persisted = [r for k, r in state.rows.items() if k != "vr-prior"]
    _check("background body PERSISTED a scheduled run (substrate=heuristic)", len(persisted) == 1 and persisted[0]["substrate"] == "heuristic", str(persisted))
    _check("regression (high 1 vs prior 0) → a queued memory note with the regression tag", len(memory.remembered) == 1 and memory.remembered[0][1] == [REGRESSION_MEMORY_TAG], str(memory.remembered))
    _check("regression note names the run + blocker/high delta", "high 1" in memory.remembered[0][0] and "prior blocker 0 / high 0" in memory.remembered[0][0], memory.remembered[0][0])

    # Clean case: prior run has 5 high; the new run finds 1 high → NOT worse → silent.
    state2 = _installed_state()
    state2.rows["vr-prior"] = {"run_id": "vr-prior", "started": "2026-07-20T00:00:00Z", "counts_by_severity": {"high": 5}}
    memory2 = FakeMemoryService()
    plugin2 = _StubPlugin(state2, memory2, base / "wt2")
    (base / "wt2").mkdir()
    _run_background(plugin2, base / "clean", [_high_finding("vr-new2")])
    _check("clean/improved run (high 1 vs prior 5) → SILENT (no memory note)", memory2.remembered == [], str(memory2.remembered))


def main() -> int:
    try:
        _check_single_slot_lease()
        _check_regression_predicate()
        _check_edge_sink_registration()
        with tempfile.TemporaryDirectory() as tmp:
            _check_verb_fast_return_and_boundary(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_background_persist_and_notify(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"scheduled_self_vet_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
