#!/usr/bin/env python3
"""Regression guard: the two async-job failure modes a monitor must distinguish.

Slice 2 of the durable-job-completion work. Both verbs exist because a job can
go wrong in two ways that need OPPOSITE handling, and one mechanism cannot
serve both:

  * NEVER FINISHED — a worker died mid-run, the row sits in ``processing``
    forever, the flow never closes. ``sweep_stale_jobs`` terminates it through
    ``AsyncJobManager.update_job``, so the error continuation fires and the FRG
    token resolves.
  * FINISHED, CONTINUATION FAILED — ``update_job`` writes the terminal status
    BEFORE the completion handler runs and before the token resolves, so a
    raising handler leaves a row reading ``completed`` while nothing was
    submitted and the token stayed open. That row does not look stuck, so the
    sweep cannot find it. ``detect_unresolved_completion_tokens`` reports it and
    changes NOTHING — resolving here would fabricate a completion.

Cases, each naming the mutation that reds it:

  1. The sweep terminates only rows past the window, through the manager's own
     update path. (Mutation: call ``_update_ledger``-style direct write, or
     drop the ``updated_at`` cutoff filter -> PASS to FAIL.)
  2. The sweep refuses ``queued`` rows — a queued job is a backlog, not a
     death. (Mutation: add ``queued`` to the sweepable statuses -> PASS to FAIL.)
  3. The recorded status_reason NAMES the sweep, so a swept job is never
     readable as a worker-reported failure. (Mutation: drop the prefix from the
     reason string -> PASS to FAIL.)
  4. The sweep REFUSES to run with no job manager rather than writing a
     terminal status directly. (Mutation: fall back to a direct state write
     -> PASS to FAIL.)
  5. max_age_minutes is required and validated; limit is bounded. (Mutation:
     default the window or clamp the limit -> PASS to FAIL.)
  6. The detector reports a terminal row whose token is non-terminal. (Mutation:
     drop the ``_token_is_unresolved`` predicate -> PASS to FAIL.)
  7. The detector does NOT report a terminal row whose token resolved, and does
     NOT report one whose token row is missing (absent evidence is not evidence
     of the defect). (Mutation: treat a missing token row as unresolved ->
     PASS to FAIL.)
  8. The detector mutates nothing. (Mutation: have it resolve tokens -> PASS to
     FAIL.)

Rail: the fakes track StateServiceProtocol's and AsyncJobManager's real
signatures, so drift reds this smoke rather than vacating it.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:

    .venv/bin/python3 ananta/tests/core/services/job_service_lifecycle_smoke.py
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.interfaces.state_service_protocol import StateServiceProtocol  # noqa: E402
from ananta.services.job_service.service import JobService  # noqa: E402

_failures: list[str] = []


def _check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        _failures.append(message)


def _ok(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {"action_status": "completed", "data": {"records": records}, "actions": []}


class _FakeStateService:
    """Serves job / job_payload / flow_tokens rows with real filter semantics.

    Implements the subset of the filter grammar these verbs actually use —
    scalar equality, list (``= ANY``), and the ``{"op": "lt"}`` range — because
    a fake that ignored the cutoff would let a sweep-everything bug pass.
    """

    def __init__(
        self,
        jobs: list[dict[str, Any]] | None = None,
        tokens: list[dict[str, Any]] | None = None,
    ) -> None:
        self.jobs = jobs or []
        self.tokens = tokens or []
        self.payloads: list[dict[str, Any]] = []
        self.update_state_calls: list[dict[str, Any]] = []

    @staticmethod
    def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
        for column, expected in filters.items():
            actual = row.get(column)
            if isinstance(expected, dict) and expected.get("op") == "lt":
                if actual is None or not actual < expected["value"]:
                    return False
            elif isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def _rows_for(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        table = str(query.get("table"))
        source = {
            "job": self.jobs,
            "job_payload": self.payloads,
            "flow_tokens": self.tokens,
        }.get(table)
        if source is None:
            raise AssertionError(f"unexpected table: {table!r}")
        filters = query.get("filters") or {}
        if not isinstance(filters, dict):
            filters = {}
        return [row for row in source if self._matches(row, filters)]

    def read_state(self, namespace: str, query: dict[str, object]) -> dict[str, object]:
        return _ok(self._rows_for(dict(query)))

    def query_state(self, namespace: str, filters: dict[str, object]) -> dict[str, object]:
        return _ok(self._rows_for(dict(filters)))

    def write_state(
        self,
        namespace: str,
        data: dict[str, object],
        calling_service: str | None = None,
        calling_namespace: str | None = None,
    ) -> dict[str, object]:
        return {"action_status": "completed"}

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> dict[str, object]:
        self.update_state_calls.append({"query": dict(query), "updates": dict(updates)})
        filters = query.get("filters") or {}
        if isinstance(filters, dict):
            for row in self._rows_for({"table": query.get("table"), "filters": filters}):
                row.update(updates)
        return {"action_status": "completed"}


class _RecordingJobManager:
    """AsyncJobManager stand-in recording terminal transitions."""

    def __init__(self, state: _FakeStateService) -> None:
        self._state = state
        self.update_job_calls: list[tuple[str, dict[str, object]]] = []

    def update_job(self, job_id: str, updates: dict[str, object]) -> dict[str, object]:
        self.update_job_calls.append((job_id, dict(updates)))
        self._state.update_state(
            "core", {"table": "job", "filters": {"id": job_id}}, dict(updates)
        )
        return {"action_status": "completed", "data": {"job_id": job_id, "updated": True}}


def _stamp(minutes_ago: int) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes_ago)


def _job(
    job_id: str,
    *,
    status: str = "processing",
    updated_minutes_ago: int = 60,
    provider: str = "g_suite_plugin.sheets_create_from_files",
    token: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": job_id,
        "provider_name": provider,
        "status": status,
        "created_at": _stamp(updated_minutes_ago + 5),
        "updated_at": _stamp(updated_minutes_ago),
        "metadata": json.dumps({"flow_id": "flow-1"}),
    }
    if token is not None:
        row["flow_token_id"] = token
    return row


def _result(envelope: dict[str, Any], key: str) -> Any:
    data = envelope.get("data")
    assert isinstance(data, dict)
    result = data.get("result")
    assert isinstance(result, dict)
    return result.get(key)


def _service(
    jobs: list[dict[str, Any]],
    tokens: list[dict[str, Any]] | None = None,
    *,
    with_manager: bool = True,
) -> tuple[JobService, _FakeStateService, _RecordingJobManager | None]:
    state = _FakeStateService(jobs=jobs, tokens=tokens)
    manager = _RecordingJobManager(state) if with_manager else None
    svc = JobService(state_service=state, async_job_manager=manager)  # type: ignore[arg-type]
    return svc, state, manager


def test_sweep_terminates_only_stale_processing_rows() -> None:
    """Case 1: past the window is swept; inside the window is left alone."""
    svc, _state, manager = _service(
        [
            _job("job-dead", updated_minutes_ago=90),
            _job("job-live", updated_minutes_ago=2),
        ]
    )
    envelope = svc.sweep_stale_jobs(max_age_minutes=30)
    swept = _result(envelope, "swept")
    ids = [s["job_id"] for s in swept] if isinstance(swept, list) else []
    _check(ids == ["job-dead"], f"only the stale processing job is swept (got {ids})")
    assert manager is not None
    _check(
        [c[0] for c in manager.update_job_calls] == ["job-dead"],
        "the sweep goes through AsyncJobManager.update_job, not a direct ledger write",
    )
    _check(
        bool(manager.update_job_calls)
        and manager.update_job_calls[0][1].get("status") == "error",
        "the swept job is transitioned to a terminal error status",
    )


def test_sweep_ignores_queued_rows() -> None:
    """Case 2: a queued job is a backlog, not a death."""
    svc, _state, manager = _service([_job("job-queued", status="queued",
                                          updated_minutes_ago=999)])
    envelope = svc.sweep_stale_jobs(max_age_minutes=1)
    _check(_result(envelope, "count") == 0, "an ancient QUEUED job is never swept")
    assert manager is not None
    _check(not manager.update_job_calls, "no terminal transition was attempted for it")


def test_swept_reason_names_the_sweep() -> None:
    """Case 3: a swept job must not read as a worker-reported failure."""
    svc, _state, manager = _service([_job("job-dead", updated_minutes_ago=90)])
    svc.sweep_stale_jobs(max_age_minutes=30)
    assert manager is not None
    reason = str(manager.update_job_calls[0][1].get("status_reason", ""))
    _check(
        reason.startswith("swept_stale"),
        f"the recorded reason marks it as swept, not worker-reported (got {reason!r})",
    )
    _check(
        "NOT reported by the worker" in reason,
        "the reason says outright that the worker did not report this failure",
    )


def test_sweep_refuses_without_a_job_manager() -> None:
    """Case 4: refuse rather than write a terminal status behind the manager."""
    svc, state, _ = _service([_job("job-dead", updated_minutes_ago=90)],
                             with_manager=False)
    envelope = svc.sweep_stale_jobs(max_age_minutes=30)
    _check(
        envelope.get("action_status") == "error",
        "the sweep returns an error envelope when no job manager is wired",
    )
    _check(
        not state.update_state_calls,
        "and it writes NOTHING — no direct terminal write behind the manager's back",
    )


def test_sweep_validates_its_bounds() -> None:
    """Case 5: the window is required and the page is bounded."""
    svc, _state, _m = _service([_job("job-dead", updated_minutes_ago=90)])
    for bad in (0, -5):
        _check(
            svc.sweep_stale_jobs(max_age_minutes=bad).get("action_status") == "error",
            f"max_age_minutes={bad} is refused",
        )
    _check(
        svc.sweep_stale_jobs(max_age_minutes=30, limit=101).get("action_status") == "error",
        "limit=101 is refused rather than clamped",
    )


def test_detector_reports_terminal_row_with_open_token() -> None:
    """Case 6: the row that does not look stuck is found."""
    svc, _state, _m = _service(
        [_job("job-ghost", status="completed", token="tok-open")],
        [{"id": "tok-open", "state": "waiting_job"}],
    )
    envelope = svc.detect_unresolved_completion_tokens()
    jobs = _result(envelope, "jobs")
    ids = [j["id"] for j in jobs] if isinstance(jobs, list) else []
    _check(ids == ["job-ghost"], f"the completed-looking, token-open job is reported (got {ids})")


def test_detector_excludes_resolved_and_missing_tokens() -> None:
    """Case 7: resolved tokens and ABSENT token rows are both excluded."""
    svc, _state, _m = _service(
        [
            _job("job-fine", status="completed", token="tok-done"),
            _job("job-no-token-row", status="completed", token="tok-missing"),
        ],
        [{"id": "tok-done", "state": "completed"}],
    )
    envelope = svc.detect_unresolved_completion_tokens()
    _check(
        _result(envelope, "count") == 0,
        "a resolved token is not reported, and a MISSING token row is not either "
        "(absent evidence is not evidence of the defect)",
    )


def test_detector_mutates_nothing() -> None:
    """Case 8: read-only — it must never fabricate a completion."""
    svc, state, manager = _service(
        [_job("job-ghost", status="completed", token="tok-open")],
        [{"id": "tok-open", "state": "waiting_job"}],
    )
    svc.detect_unresolved_completion_tokens()
    _check(not state.update_state_calls, "the detector performed no state writes")
    assert manager is not None
    _check(not manager.update_job_calls, "the detector performed no job transitions")


def test_fakes_track_real_signatures() -> None:
    """Rail: protocol/implementation drift must red this smoke."""
    for method_name in ("read_state", "write_state", "update_state", "query_state"):
        real_params = list(
            inspect.signature(getattr(StateServiceProtocol, method_name)).parameters
        )
        fake_params = list(inspect.signature(getattr(_FakeStateService, method_name)).parameters)
        _check(
            real_params == fake_params,
            f"_FakeStateService.{method_name} params match StateServiceProtocol's",
        )
    real_update = list(inspect.signature(AsyncJobManager.update_job).parameters)
    fake_update = list(inspect.signature(_RecordingJobManager.update_job).parameters)
    _check(
        real_update == fake_update,
        f"_RecordingJobManager.update_job params {fake_update} match "
        f"AsyncJobManager's {real_update}",
    )


def main() -> int:
    print("JobService lifecycle smoke — staleness sweep + unresolved-token detector")
    print("\nCase 1: the sweep terminates only stale processing rows")
    test_sweep_terminates_only_stale_processing_rows()
    print("\nCase 2: the sweep ignores queued rows")
    test_sweep_ignores_queued_rows()
    print("\nCase 3: the swept reason names the sweep")
    test_swept_reason_names_the_sweep()
    print("\nCase 4: the sweep refuses without a job manager")
    test_sweep_refuses_without_a_job_manager()
    print("\nCase 5: the sweep validates its bounds")
    test_sweep_validates_its_bounds()
    print("\nCase 6: the detector reports a terminal row with an open token")
    test_detector_reports_terminal_row_with_open_token()
    print("\nCase 7: the detector excludes resolved AND missing tokens")
    test_detector_excludes_resolved_and_missing_tokens()
    print("\nCase 8: the detector mutates nothing")
    test_detector_mutates_nothing()
    print("\nRail: fakes track the real signatures")
    test_fakes_track_real_signatures()

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} check(s) failed")
        for message in _failures:
            print(f"  - {message}")
        return 1
    print("PASS: the two failure modes stay distinguishable, and only one of them mutates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
