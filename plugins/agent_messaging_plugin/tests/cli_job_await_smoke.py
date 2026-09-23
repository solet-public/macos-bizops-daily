#!/usr/bin/env python3
"""Regression guard: `solet call` awaits a born-async job's SECOND hop.

A born-async verb answers in milliseconds with ``{job_id, status: queued}`` —
a handle, not an outcome. ``call_and_wait`` waits only for that DISPATCH, so
before this change an interactive ``solet call`` printed the handle and left
the caller to go redeem the result by hand. That is the exact shape of the
2026-08-14 incident (a finished spreadsheet reachable only by ad-hoc polling).

Cases, each naming the mutation that reds it:

  1. The queued envelope is recognized in BOTH measured shapes: a plugin
     verb's ``result.data`` (g_suite's ``_success``) and a service verb's
     ``result.data.result`` (JobService's KEY_DATA/KEY_RESULT). There is no
     universal ``data`` key, so both are checked explicitly. (Mutation: check
     only one nesting -> PASS to FAIL.)
  2. A payload carrying ``job_id`` WITHOUT ``status == "queued"`` is not
     treated as a fresh dispatch — otherwise ``get_job``'s own answer would
     start an await loop on a job that already finished. (Mutation: drop the
     status condition -> PASS to FAIL.)
  3. An unrecognized envelope yields None, so the CLI falls through to its
     normal behavior instead of acting on a guess. (Mutation: return the first
     job_id found anywhere -> PASS to FAIL.)
  4. ``await_job`` polls until the job is terminal, then returns the job
     record. (Mutation: return on the first poll -> PASS to FAIL.)
  5. ``await_job`` raises BridgeResultTimeoutError when the job is still
     non-terminal at the deadline — the job keeps running; only the waiting
     stops. (Mutation: return the non-terminal record instead -> PASS to FAIL.)
  6. ``_run(propagate_timeout=True)`` lets that timeout REACH the caller.
     Without it ``_run`` converts it to SystemExit and the CLI's "here is how
     to redeem the result later" hint becomes unreachable code. (Mutation:
     drop the propagate_timeout branch -> PASS to FAIL.)

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/cli_job_await_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.local_cli import cli as cli_module  # noqa: E402

# HERMETICITY: _run() resolves the live bridge URL and BridgeClient.__enter__
# POSTs a real /open BEFORE the callable ever runs, so on a machine with no
# running solet (a born clone — the born-clone gate runs this register) every
# _run leg died at CONNECTION_ERROR without reaching the fake callables below.
# The legs test _run's failure MAPPING, not the transport, and every fake
# ignores its client argument — so both seams are stubbed: a dummy URL and a
# no-op client. Found by the born-clone publication gate, 2026-08-14 — at
# origin the ambient running solet masked the coupling.
cli_module.resolve_base_url = lambda: "http://127.0.0.1:9"  # type: ignore[assignment]


class _StubBridgeClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None: ...
    def __enter__(self) -> _StubBridgeClient:
        return self
    def __exit__(self, *_exc: object) -> None: ...


cli_module.BridgeClient = _StubBridgeClient  # type: ignore[assignment]
from agent_messaging_plugin.local_cli.client import (  # noqa: E402
    BridgeCallError,
    BridgeResultTimeoutError,
    queued_job_id,
)

_failures: list[str] = []


def _check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        _failures.append(message)


def _plugin_shape(job_id: str, status: str = "queued") -> dict[str, Any]:
    """A plugin verb's envelope: the return sits directly under result.data."""
    return {"status": "completed", "result": {"data": {"job_id": job_id, "status": status}}}


def _service_shape(job_id: str, status: str = "queued") -> dict[str, Any]:
    """A service-interface verb's envelope: one level deeper."""
    return {
        "status": "completed",
        "result": {"data": {"result": {"job_id": job_id, "status": status}}},
    }


def test_both_measured_shapes_are_recognized() -> None:
    """Case 1: no universal data key — both nestings are checked."""
    _check(queued_job_id(_plugin_shape("job-a")) == "job-a",
           "a plugin verb's result.data queued envelope is recognized")
    _check(queued_job_id(_service_shape("job-b")) == "job-b",
           "a service verb's result.data.result queued envelope is recognized")


def test_non_queued_job_id_is_not_a_dispatch() -> None:
    """Case 2: job_id alone is not a fresh dispatch."""
    _check(queued_job_id(_plugin_shape("job-c", status="completed")) is None,
           "a job_id with status=completed is NOT treated as a new dispatch")
    _check(queued_job_id({"status": "completed",
                          "result": {"data": {"job_id": "job-d"}}}) is None,
           "a job_id with no status at all is not treated as a new dispatch")


def test_unrecognized_envelope_yields_none() -> None:
    """Case 3: fall through rather than guess."""
    for payload in (
        {},
        {"status": "completed"},
        {"status": "completed", "result": {}},
        {"status": "completed", "result": {"data": {"jobs": [{"job_id": "x"}]}}},
        {"result": {"data": {"job_id": 42, "status": "queued"}}},
    ):
        _check(queued_job_id(payload) is None,
               f"unrecognized envelope yields None: {payload}")


class _FakeClient:
    """BridgeClient stand-in serving a scripted sequence of get_job answers."""

    def __init__(self, statuses: list[str]) -> None:
        self._statuses = list(statuses)
        self.calls = 0

    def call_and_wait(
        self,
        process_key: str,
        arguments: dict[str, Any],
        *,
        reason: str | None = None,
        poll_timeout_s: float = 0.0,
    ) -> dict[str, Any]:
        self.calls += 1
        status = self._statuses.pop(0) if self._statuses else "processing"
        return {
            "status": "completed",
            "result": {
                "data": {
                    "result": {
                        "job": {
                            "id": arguments["job_id"],
                            "status": status,
                            "result": {"spreadsheet_url": "https://example.invalid/s/1"},
                        }
                    }
                }
            },
        }


def _await(client: _FakeClient, **kwargs: Any) -> dict[str, Any]:
    """Drive the REAL await_job body against the fake client."""
    from agent_messaging_plugin.local_cli.client import BridgeClient

    return BridgeClient.await_job(client, "job-1", **kwargs)  # type: ignore[arg-type]


def test_await_polls_until_terminal() -> None:
    """Case 4: keep polling while the job is still working."""
    client = _FakeClient(["queued", "processing", "completed"])
    job = _await(client, job_timeout_s=30.0)
    _check(client.calls == 3, f"polled until the job went terminal (calls={client.calls})")
    _check(job.get("status") == "completed", "returned the terminal job record")
    _check(
        isinstance(job.get("result"), dict) and "spreadsheet_url" in job["result"],
        "the returned record carries the job's attached result payload",
    )


def test_await_times_out_without_losing_the_job() -> None:
    """Case 5: the deadline stops the WAITING, not the job."""
    client = _FakeClient(["processing"] * 50)
    timed_out = False
    try:
        _await(client, job_timeout_s=0.0)
    except BridgeResultTimeoutError as exc:
        timed_out = True
        _check("job-1" in str(exc), "the timeout names the job id so it can be redeemed")
    _check(timed_out, "a still-running job at the deadline raises BridgeResultTimeoutError")


def test_run_can_propagate_the_timeout() -> None:
    """Case 6: without this, the CLI's redeem-later hint is unreachable code."""
    def _raise(_client: Any) -> dict[str, Any]:
        raise BridgeResultTimeoutError("job job-1 still 'processing' after 1s")

    propagated = False
    try:
        cli_module._run(_raise, propagate_timeout=True)  # noqa: SLF001
    except BridgeResultTimeoutError:
        propagated = True
    except SystemExit:
        propagated = False
    _check(propagated, "_run(propagate_timeout=True) re-raises instead of dying")

    died = False
    try:
        cli_module._run(_raise)  # noqa: SLF001
    except BridgeResultTimeoutError:
        died = False
    except SystemExit:
        died = True
    _check(died, "the default still dies with a mapped exit code (no behavior change)")


def test_other_errors_still_map_normally() -> None:
    """propagate_timeout must not widen into an escape hatch for every error."""
    def _raise(_client: Any) -> dict[str, Any]:
        raise BridgeCallError("boom")

    mapped = False
    try:
        cli_module._run(_raise, propagate_timeout=True)  # noqa: SLF001
    except SystemExit:
        mapped = True
    except BridgeCallError:
        mapped = False
    _check(mapped, "a non-timeout error still maps to a die/exit even with the flag on")


def main() -> int:
    print("CLI job-await smoke — recognizing the handle and waiting for the outcome")
    print("\nCase 1: both measured queued-envelope shapes")
    test_both_measured_shapes_are_recognized()
    print("\nCase 2: a job_id without queued status is not a dispatch")
    test_non_queued_job_id_is_not_a_dispatch()
    print("\nCase 3: unrecognized envelopes fall through")
    test_unrecognized_envelope_yields_none()
    print("\nCase 4: await polls until terminal")
    test_await_polls_until_terminal()
    print("\nCase 5: the deadline stops the waiting, not the job")
    test_await_times_out_without_losing_the_job()
    print("\nCase 6: _run can propagate the timeout to the caller")
    test_run_can_propagate_the_timeout()
    print("\nGuard: other errors still map normally")
    test_other_errors_still_map_normally()

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} check(s) failed")
        for message in _failures:
            print(f"  - {message}")
        return 1
    print("PASS: a queued dispatch is recognized, awaited, and never silently guessed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
