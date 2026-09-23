#!/usr/bin/env python3
"""The success path clears a stale error AND announces the double execution.

Adopter issue #9 (solet-public/macos-bizops#9): a result envelope came back
carrying a success payload and an ``unsupported_on_host`` error at the same
time. Both halves were real. They were from two different executions of one
action row, merged at read time:

  * ``error_message`` is ONE mutable column on the ``action_events`` row;
  * ``result`` is the LATEST of many ``action_results`` rows for that action.

``_update_action_status_to_failed`` wrote both ``status`` and ``error_message``;
``_update_action_status_to_completed`` wrote only ``status``. So a row that
failed and then succeeded ended ``status=completed`` still carrying the failed
attempt's error, plus a second result row — and ``PlatformSurface.process_result``
paired the stale error with the fresh success.

**This smoke asserts BOTH halves of the fix, because the first half alone is a
regression.** Clearing the column silently would make the envelope clean and
the double execution invisible — deleting the only surviving evidence that an
action row ever executes twice. So the clear must announce itself: a WARNING
carrying the action_id and both timestamps, plus a counter on ``get_metrics()``.

Nothing here stubs the code under test. The two shipped status writers, the
shipped result store, and the shipped envelope reader all run; only the state
service is replaced, by an in-memory pair of tables that returns
envelope-faithful read/query/update/write results.

Mutations this suite is calibrated against (each names the check it reddens):

  A. drop ``"error_message": None`` from the completed update
     -> "the rendered envelope no longer pairs a stale error with the success"
     -> "the completed status write clears error_message EXPLICITLY"
  B. clear the column but delete the ``_report_double_execution`` call (the
     trap: symptom closed, instrument deleted)
     -> "clearing a stale error announces a DOUBLE-EXECUTION"
     -> "the announcement carries the action_id and BOTH timestamps"
     -> "the telemetry counter counts the detection"
  C. announce/increment unconditionally instead of only when something was
     cleared
     -> "a clean completion stays silent (the instrument must not cry wolf)"
     -> "a clean completion leaves the counter alone"
  D. swallow an unreadable pre-clear read instead of announcing it
     -> "an unreadable row is announced as a DETECTOR BLIND SPOT"

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        ananta/tests/core/actions/completion_clears_stale_error_smoke.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.actions.action_queue_poller import ActionQueuePoller  # noqa: E402

from agent_messaging_plugin.platform_surface import PlatformSurface  # noqa: E402

_POLLER_LOGGER = "ananta.core.actions.action_queue_poller"
_ACTION_ID = "ae-issue9"
_PROCESS_KEY = "plugin::agent_messaging_plugin::deliver_result"
_STALE_ERROR = (
    "{'type': 'agent_messaging_error', 'code': 'unsupported_on_host', "
    "'message': 'host_ref not recognised by this process'}"
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeStateService:
    """In-memory ``core.action_events`` + ``core.action_results``.

    Envelope-faithful on purpose: the poller and the envelope reader both
    unwrap ``{"action_status": "completed", "data": {"records": [...]}}``, and
    a fake that returned bare lists would let a reader-side regression pass.
    """

    def __init__(self, *, error_message: str | None = None) -> None:
        self.events: dict[str, dict[str, Any]] = {
            _ACTION_ID: {
                "id": _ACTION_ID,
                "process_key": _PROCESS_KEY,
                "core__flows_id": "flow-issue9",
                "status": "processing",
                "error_message": error_message,
            },
        }
        self.results: list[dict[str, Any]] = []
        self.update_payloads: list[dict[str, Any]] = []
        self.read_envelope_status = "completed"
        self._clock = 0

    @staticmethod
    def _ok(payload: dict[str, Any]) -> dict[str, Any]:
        return {"action_status": "completed", "data": payload, "error": None}

    @staticmethod
    def _match(row: dict[str, Any], filters: dict[str, Any]) -> bool:
        return all(row.get(key) == value for key, value in filters.items())

    def _rows(self, table: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        if table == "action_events":
            source: list[dict[str, Any]] = list(self.events.values())
        elif table == "action_results":
            source = self.results
        else:  # fast failure — an unmodelled table is a broken fixture
            raise AssertionError(f"fixture does not model table {table!r}")
        return [dict(row) for row in source if self._match(row, filters)]

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        _ = namespace
        rows = self._rows(filters["table"], filters.get("filters", {}))
        return {
            "action_status": self.read_envelope_status,
            "data": {"records": rows, "count": len(rows)},
            "error": None,
        }

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        _ = namespace
        rows = self._rows(query["table"], query.get("filters", {}))
        return self._ok({"records": rows, "count": len(rows)})

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        _ = namespace
        self.update_payloads.append(dict(updates))
        updated = 0
        for row in self.events.values():
            if self._match(row, query["filters"]):
                row.update(updates)
                updated += 1
        return self._ok({"result": {"updated": updated}})

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        _ = namespace
        self._clock += 1
        record = dict(data["record"])
        # Monotonic and comparable as a string — the newest-row pick is a
        # lexicographic max over created_at in both the poller and the reader.
        record["created_at"] = f"2026-08-15T09:41:07.{self._clock:06d}"
        self.results.append(record)
        return self._ok({"id": f"ar-{self._clock}"})


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def warnings(self) -> list[str]:
        return [
            r.getMessage() for r in self.records if r.levelno >= logging.WARNING
        ]


def _poller(state: _FakeStateService) -> ActionQueuePoller:
    """A real poller with only its state service replaced."""
    poller = object.__new__(ActionQueuePoller)
    poller.state_service = state
    poller.total_double_executions_detected = 0
    poller.total_actions_processed = 0
    poller.total_poll_cycles = 0
    poller.running = False
    poller.last_poll_time = None
    poller.poll_interval = 1.0
    poller.max_actions_per_poll = 10
    return poller


def _envelope(state: _FakeStateService) -> dict[str, Any]:
    """Render the real #9 envelope through the shipped reader."""
    surface = object.__new__(PlatformSurface)
    surface._state_service = state
    return surface.process_result(_ACTION_ID)


def _run_failed_then_succeeded() -> tuple[
    _FakeStateService, ActionQueuePoller, _LogCapture, dict[str, Any]
]:
    """Replay #9: attempt 1 fails, attempt 2 succeeds, on ONE action row.

    Both attempts go through the shipped writers, in the shipped order
    (``_mark_action_completed`` stamps status before it stores its result row,
    which is why the detector can still see the earlier attempt's row).
    """
    state = _FakeStateService()
    poller = _poller(state)
    capture = _LogCapture()
    logger = logging.getLogger(_POLLER_LOGGER)
    logger.addHandler(capture)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        # Attempt 1 — the execution whose process did not hold the host_ref.
        poller._update_action_status_to_failed(_ACTION_ID, _STALE_ERROR)
        poller._store_action_result(
            _ACTION_ID,
            {"action_status": "failed", "error": {"code": "unsupported_on_host"}},
            _PROCESS_KEY,
        )
        # Attempt 2 — the execution that actually did the work.
        poller._update_action_status_to_completed(_ACTION_ID)
        poller._store_action_result(
            _ACTION_ID,
            {"success": True, "action_status": "completed", "data": {"sent": True}},
            _PROCESS_KEY,
        )
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
    return state, poller, capture, _envelope(state)


def test_the_split_envelope_is_gone() -> None:
    """★ The adopter-visible defect: one envelope, one execution's outcome."""
    state, _poller_obj, _capture, envelope = _run_failed_then_succeeded()

    _check(
        len(state.results) == 2,
        "fixture precondition: the action row owns TWO result rows "
        f"(got {len(state.results)}) — without this the merge cannot occur",
    )
    _check(
        envelope.get("status") == "completed",
        f"the envelope reports the successful outcome (got {envelope.get('status')!r})",
    )
    _check(
        envelope.get("error_message") is None,
        "the rendered envelope no longer pairs a stale error with the success "
        f"(got {envelope.get('error_message')!r})",
    )
    result = envelope.get("result")
    _check(
        isinstance(result, dict) and result.get("success") is True,
        f"the success payload still reaches the caller (got {result!r})",
    )


def test_the_completed_write_clears_the_column_explicitly() -> None:
    """The clear must live in THIS writer — the asymmetry is the bug."""
    state, _poller_obj, _capture, _envelope_out = _run_failed_then_succeeded()
    completed_writes = [
        payload
        for payload in state.update_payloads
        if payload.get("status") == "completed"
    ]
    _check(
        len(completed_writes) == 1,
        f"exactly one completed status write was issued (got {len(completed_writes)})",
    )
    payload = completed_writes[0] if completed_writes else {}
    _check(
        "error_message" in payload and payload["error_message"] is None,
        "the completed status write clears error_message EXPLICITLY "
        f"(payload was {payload!r})",
    )


def test_clearing_announces_a_double_execution() -> None:
    """★ The instrument. A silent clear closes #9 and hides its cause."""
    _state, _poller_obj, capture, _envelope_out = _run_failed_then_succeeded()
    announcements = [
        message
        for message in capture.warnings()
        if "DOUBLE-EXECUTION DETECTED" in message
    ]
    _check(
        len(announcements) == 1,
        "clearing a stale error announces a DOUBLE-EXECUTION at WARNING "
        f"(got {len(announcements)} such warnings)",
    )
    message = announcements[0] if announcements else ""
    _check(
        _ACTION_ID in message,
        "the announcement names the action row it happened on",
    )


def test_the_announcement_carries_both_timestamps() -> None:
    """Both halves of the race, or the signal cannot be measured against."""
    state, _poller_obj, capture, _envelope_out = _run_failed_then_succeeded()
    message = next(
        (m for m in capture.warnings() if "DOUBLE-EXECUTION DETECTED" in m), ""
    )
    failed_attempt_created_at = state.results[0]["created_at"] if state.results else ""
    _check(
        f"previous_attempt_result_at={failed_attempt_created_at}" in message,
        "the announcement carries the action_id and BOTH timestamps — earlier "
        f"attempt at {failed_attempt_created_at!r} (message was {message!r})",
    )
    _check(
        "this_completion_at=2" in message,
        "…and the moment this execution completed",
    )
    _check(
        "unsupported_on_host" in message,
        "the cleared error text is preserved in the log — the row no longer "
        "holds it, so the announcement is now the only copy",
    )


def test_the_counter_counts_the_detection() -> None:
    """A counter on the poller's existing metrics surface."""
    _state, poller, _capture, _envelope_out = _run_failed_then_succeeded()
    _check(
        poller.total_double_executions_detected == 1,
        "the telemetry counter counts the detection "
        f"(got {poller.total_double_executions_detected})",
    )
    _check(
        poller.get_metrics().get("total_double_executions_detected") == 1,
        "get_metrics() exposes the counter to anything polling the poller",
    )


def test_a_clean_completion_stays_silent() -> None:
    """★ The control. An instrument that fires on every success is noise.

    This is what separates "clear only when there IS something to clear" from
    "announce every completion" — and it is the check that a mutation making
    the announcement unconditional reddens.
    """
    state = _FakeStateService()  # no prior error: a first-and-only execution
    poller = _poller(state)
    capture = _LogCapture()
    logger = logging.getLogger(_POLLER_LOGGER)
    logger.addHandler(capture)
    try:
        poller._update_action_status_to_completed(_ACTION_ID)
    finally:
        logger.removeHandler(capture)

    _check(
        not [m for m in capture.warnings() if "DOUBLE-EXECUTION" in m],
        "a clean completion stays silent (the instrument must not cry wolf) — "
        f"warnings were {capture.warnings()!r}",
    )
    _check(
        poller.total_double_executions_detected == 0,
        "a clean completion leaves the counter alone "
        f"(got {poller.total_double_executions_detected})",
    )
    _check(
        state.update_payloads
        and state.update_payloads[-1].get("error_message", "missing") is None,
        "…but the column is still cleared unconditionally — the clear is the "
        "fix, the announcement is only the instrument",
    )
    _check(
        state.events[_ACTION_ID]["status"] == "completed",
        "control: the completion itself still lands",
    )


def test_an_unreadable_row_is_announced_not_swallowed() -> None:
    """No silent fallback: a deaf detector must not look like a quiet platform."""
    state = _FakeStateService(error_message=_STALE_ERROR)
    state.read_envelope_status = "failed"  # the pre-clear read does not come back
    poller = _poller(state)
    capture = _LogCapture()
    logger = logging.getLogger(_POLLER_LOGGER)
    logger.addHandler(capture)
    try:
        poller._update_action_status_to_completed(_ACTION_ID)
    finally:
        logger.removeHandler(capture)

    _check(
        [m for m in capture.warnings() if "DETECTOR BLIND" in m],
        "an unreadable row is announced as a DETECTOR BLIND SPOT "
        f"(warnings were {capture.warnings()!r})",
    )
    _check(
        state.events[_ACTION_ID]["error_message"] is None,
        "control: the completion and the clear still happen — a blind detector "
        "must not block the drain loop",
    )


def main() -> None:
    print("the success path clears a stale error AND announces the double execution")
    for name, obj in sorted(globals().items()):
        if name.startswith("test_") and callable(obj):
            print(f"\n{name}")
            obj()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
