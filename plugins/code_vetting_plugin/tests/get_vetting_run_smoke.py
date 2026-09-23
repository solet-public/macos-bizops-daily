"""get_vetting_run_smoke.py — W3-C C3a: the run_id READ-VERB.

Pins the payload-by-run_id read seam the joseki card is gated on (the W3-C ruling Q3 content-carry
blocker — deterministic steps cannot pipe a prior step's runtime result, so the L2/L3 inference steps
read report + evidence by run_id from HERE):

  * READ: ``read_vetting_run`` returns the persisted row for a known run_id via the SYNC single-namespace
    ``query_state`` equality filter (own ``vetting_runs`` namespace, no join, no raw SQL — asserted on the
    recorded call shape), and None for an unknown run_id.
  * REPORT ROUND-TRIP: a run persisted with a ``report`` + ``dead_symbols`` + ``structural_metrics`` reads
    back with all of them intact at the joseki's field paths (report, dead_symbols.candidates[],
    structural_metrics.literals[]).
  * VERB ENVELOPE: get_vetting_run(known) → COMPLETED data envelope carrying the row; get_vetting_run(unknown)
    → typed ``run_not_found`` ERROR envelope (fail-loud, never an empty success); empty run_id → ``invalid_run_id``.

Hermetic: a fake StateServiceProtocol backs one (namespace, table) map; no live solet / DB. Run directly or
via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ananta.core.domain.enums import ActionStatus
from code_vetting_plugin.live_state import (
    VETTING_RUNS_NAMESPACE,
    VETTING_RUNS_TABLE,
    read_vetting_run,
)
from code_vetting_plugin.plugin import CodeVettingPlugin

_CHECKS_RUN: list[str] = []
_COMPLETED = ActionStatus.COMPLETED.value
_ERROR = ActionStatus.ERROR.value


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": _COMPLETED, "data": data, "actions": [], "error": None, "timestamp": ""}


class FakeStateService:
    """A minimal StateServiceProtocol stand-in: backs the vetting_runs table by run_id and records calls."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def seed(self, record: dict[str, Any]) -> None:
        self.rows[str(record["run_id"])] = record

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("query_state", namespace, filters))
        inner = filters.get("filters", {})
        run_id = str(inner.get("run_id", ""))
        matched = [self.rows[run_id]] if run_id in self.rows else []
        return _ok({"records": matched})


class _StubPlugin(CodeVettingPlugin):
    """CodeVettingPlugin with the state-service acquisition stubbed (no orchestrator wiring needed)."""

    def __init__(self, state_service: FakeStateService) -> None:
        super().__init__()
        self._stub_state = state_service

    def _state_service(self) -> Any:  # noqa: ANN401 — the fake stands in for StateServiceProtocol
        return self._stub_state


def _record(run_id: str, *, report: str) -> dict[str, Any]:
    """A persisted vetting_runs row shape (RunMetrics.to_dict-like) with the joseki's evidence payloads."""
    return {
        "run_id": run_id,
        "target": {"repo": "example", "ref": "HEAD", "scope": "self"},
        "started": "2026-07-21T00:00:00Z",
        "finished": "2026-07-21T00:01:00Z",
        "substrate": "local_inference",
        "counts_by_severity": {"high": 1},
        "counts_by_dimension": {"code_quality": 1},
        "coverage_gaps": [],
        "structural_metrics": {"literals": [{"value": "TODO", "count": 5}], "worst_offenders": []},
        "dead_symbols": {"total": 2, "candidates": [{"file": "a.py", "name": "dead_fn", "kind": "function", "confidence": 60}]},
        "report": report,
    }


def _check_read_and_roundtrip() -> None:
    state = FakeStateService()
    state.seed(_record("vr-known", report="# Vetting report\n\nHIGH x1"))
    row = read_vetting_run(state, "vr-known")
    _check("read_vetting_run returns the row for a known run_id", row is not None and row["run_id"] == "vr-known", str(row))
    assert row is not None
    _check("read: single-namespace query_state filter (own namespace, no raw SQL)", state.calls[-1] == ("query_state", VETTING_RUNS_NAMESPACE, {"table": VETTING_RUNS_TABLE, "filters": {"run_id": "vr-known"}}), str(state.calls[-1]))
    _check("read: report round-trips", row["report"] == "# Vetting report\n\nHIGH x1", str(row.get("report")))
    _check("read: candidate_dead_symbols at dead_symbols.candidates[]", row["dead_symbols"]["candidates"][0]["name"] == "dead_fn", str(row["dead_symbols"]))
    _check("read: literal_table at structural_metrics.literals[]", row["structural_metrics"]["literals"][0]["value"] == "TODO", str(row["structural_metrics"]))
    _check("read_vetting_run returns None for an unknown run_id", read_vetting_run(state, "vr-missing") is None, "")


def _check_verb_envelope() -> None:
    state = FakeStateService()
    state.seed(_record("vr-known", report="# report body"))
    plugin = _StubPlugin(state)

    found = plugin.get_vetting_run({"run_id": "vr-known"}, {})
    _check("verb: known run_id → COMPLETED envelope", found["action_status"] == _COMPLETED, str(found.get("action_status")))
    _check("verb: envelope data carries the row + report", found["data"]["run_id"] == "vr-known" and found["data"]["report"] == "# report body", str(found["data"].get("run_id")))

    missing = plugin.get_vetting_run({"run_id": "vr-nope"}, {})
    _check("verb: unknown run_id → ERROR envelope", missing["action_status"] == _ERROR, str(missing.get("action_status")))
    _check("verb: unknown run_id → typed run_not_found (fail-loud, not empty success)", missing["error"]["code"] == "run_not_found", str(missing.get("error")))

    empty = plugin.get_vetting_run({"run_id": "   "}, {})
    _check("verb: empty run_id → invalid_run_id refusal", empty["action_status"] == _ERROR and empty["error"]["code"] == "invalid_run_id", str(empty.get("error")))


def main() -> int:
    try:
        _check_read_and_roundtrip()
        _check_verb_envelope()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"get_vetting_run_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
