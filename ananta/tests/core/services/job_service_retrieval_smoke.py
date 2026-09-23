#!/usr/bin/env python3
"""Regression guard: JobService retrieval returns the OUTCOME, not just a status.

The defect this pins (measured 2026-08-14 against the live solet): the
``core__job`` ledger row carries NO ``result`` and NO ``error`` column —
payloads are written to the separate ``core__job_payload`` table by
``AsyncJobManager._write_payload``. ``get_latest_job``'s shipped guidance
nonetheless instructed readers to "surface the result field's payload", so a
session following it to the letter got a status line and no result: the same
dead end the guidance was written to fix. ``JobService`` now attaches the
stored payloads under exactly those two documented names.

Also pins the two retrieval verbs added alongside it — ``get_job``
(fetch-by-id) and ``list_unreached_job_completions`` (the drain for
completions that had no channel to arrive on).

Cases, each naming the mutation that reds it:

  1. get_job attaches the stored result payload. (Mutation: return
     ``rows[0]`` instead of ``self._attach_payloads(rows[0])`` from
     ``_query_job_by_id`` -> PASS to FAIL.)
  2. get_latest_job attaches it too — the verb whose own KB JSON has been
     promising the field. (Mutation: same removal in ``_query_latest_job``
     -> PASS to FAIL.)
  3. The NEWEST payload wins when a job wrote several. (Mutation: take
     ``rows[0]`` instead of the ``max`` by sequence in ``_latest_payload``
     -> PASS to FAIL.)
  4. A blank job_id fails LOUD rather than answering ``job: None`` — "no id
     supplied" must not be spelled like "no such job". (Mutation: delete the
     ``ValueError`` guard in ``_query_job_by_id`` -> PASS to FAIL.)
  5. An unknown job_id is a SUCCESSFUL call carrying ``job: None`` — an
     unknown id is not an error. (Mutation: raise instead of returning None
     -> PASS to FAIL.)
  6. list_unreached_job_completions returns ONLY jobs stamped
     ``bridge_dispatch_no_return_path``; an unstamped job is never listed,
     because absent means unmeasured. (Mutation: drop the
     ``COMPLETION_REACH_KEY`` predicate from ``_query_unreached_completions``
     -> PASS to FAIL.)
  7. An out-of-range limit is refused, not silently clamped. (Mutation:
     replace the range check with a clamp -> PASS to FAIL.)

Rail: the fake tracks StateServiceProtocol's real signatures, so protocol
drift reds this smoke instead of quietly vacating it.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:

    .venv/bin/python3 ananta/tests/core/services/job_service_retrieval_smoke.py
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.state.job_completion_reach import (  # noqa: E402
    COMPLETION_REACH_KEY,
    REACH_BRIDGE_DISPATCH_NO_RETURN_PATH,
    REACH_CHANNEL_FLOW,
)
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
    """Stand-in tracking StateServiceProtocol's real read/write/update surface.

    Serves ``job`` and ``job_payload`` rows from in-memory lists, applying the
    same equality / ``= ANY`` filter semantics the real grammar compiles (a
    list value means IN-list) — a fake that ignored filters would let a
    filter-shaped bug pass.
    """

    def __init__(
        self,
        jobs: list[dict[str, Any]] | None = None,
        payloads: list[dict[str, Any]] | None = None,
    ) -> None:
        self.jobs = jobs or []
        self.payloads = payloads or []

    @staticmethod
    def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
        for column, expected in filters.items():
            actual = row.get(column)
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def _rows_for(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        table = query.get("table")
        source = {"job": self.jobs, "job_payload": self.payloads}.get(str(table))
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
        return {"action_status": "completed"}


def _job(
    job_id: str,
    *,
    created_at: str = "2026-08-14T00:00:00",
    status: str = "completed",
    reach: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, object] = {"flow_id": "flow-1", "session_id": "ses-1"}
    if reach is not None:
        metadata[COMPLETION_REACH_KEY] = reach
    return {
        "id": job_id,
        "provider_name": "g_suite_plugin.sheets_create_from_files",
        "status": status,
        "created_at": created_at,
        "metadata": json.dumps(metadata),
    }


def _payload(job_id: str, payload_type: str, data: dict[str, Any], sequence: int) -> dict[str, Any]:
    return {
        "id": f"jpl-{job_id}-{payload_type}-{sequence}",
        "job_id": job_id,
        "payload_type": payload_type,
        "payload_data": json.dumps(data),
        "sequence": sequence,
    }


_SHEET_RESULT = {"spreadsheet_url": "https://example.invalid/s/1", "row_count": 42}


def _service(
    jobs: list[dict[str, Any]], payloads: list[dict[str, Any]] | None = None
) -> JobService:
    state = _FakeStateService(jobs=jobs, payloads=payloads)
    return JobService(state_service=state)  # type: ignore[arg-type]


def _returned_job(envelope: dict[str, Any]) -> dict[str, Any] | None:
    data = envelope.get("data")
    assert isinstance(data, dict)
    result = data.get("result")
    assert isinstance(result, dict)
    job = result.get("job")
    return job if isinstance(job, dict) else None


def test_get_job_attaches_result_payload() -> None:
    """Case 1: the fetched job carries the outcome, not just a status."""
    svc = _service([_job("job-1")], [_payload("job-1", "result", _SHEET_RESULT, 0)])
    job = _returned_job(svc.get_job("job-1"))
    _check(job is not None, "get_job returns the job row")
    _check(
        job is not None and job.get("result") == _SHEET_RESULT,
        "get_job attaches the stored result payload under 'result'",
    )
    _check(
        job is not None and job.get("error") is None,
        "a job with no error payload carries error=None, not a missing key",
    )


def test_get_latest_job_attaches_result_payload() -> None:
    """Case 2: the verb whose shipped guidance promises the field honors it."""
    svc = _service([_job("job-1")], [_payload("job-1", "result", _SHEET_RESULT, 0)])
    job = _returned_job(svc.get_latest_job())
    _check(
        job is not None and job.get("result") == _SHEET_RESULT,
        "get_latest_job attaches the stored result payload its KB JSON documents",
    )


def test_newest_payload_wins() -> None:
    """Case 3: a job that wrote several payloads reports the newest."""
    svc = _service(
        [_job("job-1")],
        [
            _payload("job-1", "result", {"stale": True}, 0),
            _payload("job-1", "result", _SHEET_RESULT, 3),
            _payload("job-1", "result", {"middle": True}, 1),
        ],
    )
    job = _returned_job(svc.get_job("job-1"))
    _check(
        job is not None and job.get("result") == _SHEET_RESULT,
        "the highest-sequence payload wins, not the first row the store returned",
    )


def test_blank_job_id_fails_loud() -> None:
    """Case 4: 'no id supplied' is not spelled like 'no such job'."""
    svc = _service([_job("job-1")])
    envelope = svc.get_job("   ")
    _check(
        envelope.get("action_status") == "error",
        "a blank job_id returns an error envelope, not a successful empty answer",
    )
    data = envelope.get("data")
    message = ""
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        message = str(data["result"].get("error", ""))
    _check("job_id" in message, f"the error names job_id (got: {message!r})")


def test_unknown_job_id_is_a_successful_empty_answer() -> None:
    """Case 5: an unknown id is not an error."""
    svc = _service([_job("job-1")])
    envelope = svc.get_job("job-does-not-exist")
    _check(
        envelope.get("action_status") == "completed",
        "an unknown job_id is a successful call",
    )
    _check(_returned_job(envelope) is None, "an unknown job_id answers job: None")


def test_unreached_lists_only_stamped_jobs() -> None:
    """Case 6: only measured-unreached jobs are listed."""
    svc = _service(
        [
            _job("job-bridge", created_at="2026-08-14T00:00:03",
                 reach=REACH_BRIDGE_DISPATCH_NO_RETURN_PATH),
            _job("job-channel", created_at="2026-08-14T00:00:02", reach=REACH_CHANNEL_FLOW),
            _job("job-unstamped", created_at="2026-08-14T00:00:01"),
            _job("job-running", created_at="2026-08-14T00:00:04", status="processing",
                 reach=REACH_BRIDGE_DISPATCH_NO_RETURN_PATH),
        ],
        [_payload("job-bridge", "result", _SHEET_RESULT, 0)],
    )
    envelope = svc.list_unreached_job_completions()
    data = envelope.get("data")
    assert isinstance(data, dict)
    result = data.get("result")
    assert isinstance(result, dict)
    jobs = result.get("jobs")
    ids = [j["id"] for j in jobs] if isinstance(jobs, list) else []
    _check(ids == ["job-bridge"], f"only the stamped terminal job is listed (got {ids})")
    _check(result.get("count") == 1, "count matches the returned page")
    _check(
        isinstance(jobs, list)
        and bool(jobs)
        and jobs[0].get("result") == _SHEET_RESULT,
        "each listed job carries the payload nobody received",
    )


def test_out_of_range_limit_is_refused() -> None:
    """Case 7: a limit outside the bound is refused, never silently clamped."""
    svc = _service([_job("job-1", reach=REACH_BRIDGE_DISPATCH_NO_RETURN_PATH)])
    for bad_limit in (0, 101):
        envelope = svc.list_unreached_job_completions(limit=bad_limit)
        _check(
            envelope.get("action_status") == "error",
            f"limit={bad_limit} is refused with an error envelope",
        )


def test_fake_tracks_real_signatures() -> None:
    """Rail: protocol drift must red this smoke, not silently vacate it."""
    for method_name in ("read_state", "write_state", "update_state", "query_state"):
        real_params = list(
            inspect.signature(getattr(StateServiceProtocol, method_name)).parameters
        )
        fake_params = list(inspect.signature(getattr(_FakeStateService, method_name)).parameters)
        _check(
            real_params == fake_params,
            f"_FakeStateService.{method_name} params {fake_params} match "
            f"StateServiceProtocol's {real_params}",
        )


def main() -> int:
    print("JobService retrieval smoke — payload attach, fetch-by-id, unreached drain")
    print("\nCase 1: get_job attaches the stored result payload")
    test_get_job_attaches_result_payload()
    print("\nCase 2: get_latest_job attaches it too")
    test_get_latest_job_attaches_result_payload()
    print("\nCase 3: the newest payload wins")
    test_newest_payload_wins()
    print("\nCase 4: a blank job_id fails loud")
    test_blank_job_id_fails_loud()
    print("\nCase 5: an unknown job_id is a successful empty answer")
    test_unknown_job_id_is_a_successful_empty_answer()
    print("\nCase 6: the drain lists only measured-unreached jobs")
    test_unreached_lists_only_stamped_jobs()
    print("\nCase 7: an out-of-range limit is refused")
    test_out_of_range_limit_is_refused()
    print("\nRail: the fake tracks the real protocol signatures")
    test_fake_tracks_real_signatures()

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} check(s) failed")
        for message in _failures:
            print(f"  - {message}")
        return 1
    print("PASS: job retrieval returns the outcome, by id, and drains what nobody received")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
