#!/usr/bin/env python3
"""D0.3/Option-A regression guard: AsyncJobManager's completion-handler firing.

Not the live proof of the mechanic (see
`workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md`
§6 — comfyui_image_generation_plugin's production history is that: every
generated image that lands in conversation is a live firing of this exact
code path). This smoke's job is narrower and complementary: pin the real
`AsyncJobManager.update_job` code path against fakes so a future regression
here reds a smoke instead of only degrading production silently.

Traces (file:line current as of 2026-08-09):
`ananta/src/ananta/core/state/async_job_manager.py:446` (`update_job`) ->
`:842` (`_handle_completion_actions`) -> `:827`
(`self._action_factory.submit_action_definition`, via `_submit_completion_action`).

Four cases, each naming its failing mutation:

  1. Happy path: terminal status + `completion_handlers` + `flow_id` in job
     metadata -> exactly one action submitted, addressed at the configured
     process key, carrying the job's flow_id. (Mutation: remove the
     `_action_factory.submit_action_definition` call from
     `_submit_completion_action` -> this case goes from PASS to FAIL.)
  2. No `completion_handlers` in metadata -> silent, legitimate no-op (no
     submission, no error — a job with no configured continuation is valid,
     e.g. a job created outside action context). (Mutation: make
     `_get_completion_handler` return a handler even with no
     `completion_handlers` key -> this case's "no action submitted" check
     goes from PASS to FAIL.)
  3. Non-terminal status transition (`"processing"`) -> no submission at
     all; completion routing is gated on terminal status in `update_job`
     itself, before `_handle_completion_actions` is ever called. (Mutation:
     widen `update_job`'s `if new_status in (...)` guard to include
     `"processing"` -> this case's "no action submitted" check goes from
     PASS to FAIL.)
  4. `completion_handlers` present but `flow_id` missing from metadata ->
     loud, structured failure (not a silent success and not a swallowed
     exception): `_handle_completion_actions`'s fail-fast `ValueError`
     propagates into `update_job`'s own `except Exception` handler and comes
     back as `action_status="error"` naming the missing field. (Mutation:
     make the fail-fast check `if not flow_id: raise ValueError(...)` a
     silent `return` instead -> this case's error-surfaced check goes from
     PASS to FAIL, since `update_job` would then report a false
     "completed".) Note (observed, not asserted further): the job's ledger
     row and result payload ARE written before this failure — they are
     written earlier in `update_job`, before the terminal-status branch runs
     — so a caller sees "the job's own status/result persisted" alongside
     "the continuation-routing call failed." That combination is real
     `update_job` behavior, not a smoke artifact.

Rail (coordinator seat, arm-6363cec06def7aa902c2b10dee4ccb3b, 2026-08-09): fakes must
track the real protocols they stand in for, so a signature drift reds this
smoke rather than quietly vacating it — `test_fakes_track_real_signatures`
below.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:

    .venv/bin/python3 ananta/tests/core/actions/async_job_manager_completion_handler_smoke.py
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.job_completion_reach import (  # noqa: E402
    COMPLETION_REACH_KEY,
    REACH_BRIDGE_DISPATCH_NO_RETURN_PATH,
    REACH_CHANNEL_FLOW,
)
from ananta.interfaces.state_service_protocol import StateServiceProtocol  # noqa: E402

_RESULT_HANDLER_KEY = "plugin::_smoke_only::noop_result"  # wint:negative-fixture
_ERROR_HANDLER_KEY = "plugin::_smoke_only::noop_error"  # wint:negative-fixture

_failures: list[str] = []


def _check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        _failures.append(message)


class _FakeActionFactory:
    """Stand-in tracking ActionFactory's real submit_action_definition surface."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def submit_action_definition(
        self, action_definition: dict[str, object], context: dict[str, object] | None = None
    ) -> str:
        self.calls.append(action_definition)
        return "ae-smoke-fake-action-id"


class _FakeFlowRuntimeGraph:
    """Stand-in for FlowRuntimeGraph — none of these test jobs carry a
    flow_token_id, so complete_token must never fire; tracked, not assumed."""

    def __init__(self) -> None:
        self.complete_token_calls: list[str] = []

    def complete_token(
        self, token_id: str, success: bool = True, result_summary: dict[str, object] | None = None
    ) -> None:
        self.complete_token_calls.append(token_id)


class _FakeStateService:
    """Stand-in tracking StateServiceProtocol's real read/write/update surface.

    Table-keyed on the same literal table names AsyncJobManager itself uses
    (`self._table` == "job", plus the hardcoded "job_payload" sequence/write
    calls) — an unexpected table name is a smoke-authoring bug, not a case
    to swallow, so it raises rather than returning an empty result.
    """

    def __init__(
        self,
        metadata: dict[str, object] | None,
        trigger_type: str | None = "operator_message",
    ) -> None:
        self._metadata = metadata
        self._trigger_type = trigger_type
        self.update_state_calls: list[dict[str, object]] = []
        self.write_state_calls: list[dict[str, object]] = []

    def read_state(self, namespace: str, query: dict[str, object]) -> dict[str, object]:
        table = query.get("table")
        if table == "job_payload":
            return {"action_status": "completed", "data": {"records": []}}
        if table == "flows":
            # Carried since _record_completion_reach (2026-08-14) classifies a
            # job's origin from its flow's trigger_type. trigger_type=None
            # stands for an unreadable flow row — the case that must leave the
            # stamp ABSENT rather than guessing a value.
            if self._trigger_type is None:
                return {"action_status": "completed", "data": {"records": []}}
            filters = query.get("filters")
            flow_id = filters.get("id") if isinstance(filters, dict) else None
            return {
                "action_status": "completed",
                "data": {
                    "records": [{"id": flow_id, "trigger_type": self._trigger_type}]
                },
            }
        if table == "job":
            if self._metadata is None:
                return {"action_status": "completed", "data": {"records": []}}
            filters = query.get("filters")
            job_id = filters.get("id") if isinstance(filters, dict) else None
            return {
                "action_status": "completed",
                "data": {"records": [{"id": job_id, "metadata": self._metadata}]},
            }
        raise AssertionError(f"unexpected read_state table: {table!r}")

    def write_state(
        self,
        namespace: str,
        data: dict[str, object],
        calling_service: str | None = None,
        calling_namespace: str | None = None,
    ) -> dict[str, object]:
        self.write_state_calls.append(data)
        return {"action_status": "completed"}

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> dict[str, object]:
        self.update_state_calls.append(updates)
        return {"action_status": "completed"}


def _completion_handlers_metadata(
    *, flow_id: str | None = "flow-smoke-1", session_id: str = "session-smoke-1"
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "session_id": session_id,
        "completion_handlers": {
            "result": {"process_key": _RESULT_HANDLER_KEY, "template": {}, "notes": "smoke"},
            "error": {"process_key": _ERROR_HANDLER_KEY, "template": {}, "notes": "smoke"},
        },
    }
    if flow_id is not None:
        metadata["flow_id"] = flow_id
    return metadata


def _make_manager(
    metadata: dict[str, object] | None,
    trigger_type: str | None = "operator_message",
) -> tuple[AsyncJobManager, _FakeStateService, _FakeActionFactory, _FakeFlowRuntimeGraph]:
    state = _FakeStateService(metadata=metadata, trigger_type=trigger_type)
    factory = _FakeActionFactory()
    frg = _FakeFlowRuntimeGraph()
    manager = AsyncJobManager(state_service=state, flow_runtime_graph=frg)  # type: ignore[arg-type]
    manager.set_action_factory(factory)  # type: ignore[arg-type]
    return manager, state, factory, frg


def test_happy_path_fires_completion_handler() -> None:
    """Case 1: terminal status + completion_handlers + flow_id -> submission fires."""
    manager, _state, factory, frg = _make_manager(_completion_handlers_metadata())
    result = manager.update_job("job-smoke-1", {"status": "completed", "result": {"ok": True}})
    _check(result.get("action_status") == "completed", "update_job itself reports completed")
    _check(len(factory.calls) == 1, "exactly one action submitted on terminal completion")
    if factory.calls:
        submitted = factory.calls[0]
        _check(
            submitted.get("process_key") == _RESULT_HANDLER_KEY,
            "submitted action targets the configured result completion_handler",
        )
        _check(
            submitted.get("flow_id") == "flow-smoke-1",
            "submitted action carries the job's flow_id",
        )
    _check(
        len(frg.complete_token_calls) == 0,
        "FlowRuntimeGraph.complete_token not called (job carries no flow_token_id)",
    )


def test_missing_completion_handlers_is_silent_noop() -> None:
    """Case 2: no completion_handlers in metadata -> legitimate silent no-op."""
    metadata: dict[str, object] = {"session_id": "s", "flow_id": "f"}  # no completion_handlers
    manager, _state, factory, _frg = _make_manager(metadata)
    result = manager.update_job("job-smoke-2", {"status": "completed", "result": {"ok": True}})
    _check(
        result.get("action_status") == "completed",
        "update_job still reports completed (the ledger write itself succeeds)",
    )
    _check(
        len(factory.calls) == 0,
        "no action submitted when metadata carries no completion_handlers",
    )


def test_non_terminal_status_does_not_fire() -> None:
    """Case 3: non-terminal status transition -> completion routing never runs."""
    manager, _state, factory, _frg = _make_manager(_completion_handlers_metadata())
    result = manager.update_job("job-smoke-3", {"status": "processing", "progress_percent": 50})
    _check(
        result.get("action_status") == "completed",
        "update_job reports completed for the ledger write itself",
    )
    _check(
        len(factory.calls) == 0,
        "no action submitted on a non-terminal ('processing') status transition",
    )


def test_missing_flow_id_fails_loud_not_silent() -> None:
    """Case 4: completion_handlers present, flow_id missing -> loud structured failure."""
    manager, _state, factory, _frg = _make_manager(
        _completion_handlers_metadata(flow_id=None)
    )
    result = manager.update_job("job-smoke-4", {"status": "completed", "result": {"ok": True}})
    _check(
        result.get("action_status") == "error",
        "update_job surfaces a loud error, not a silent success, when flow_id is missing",
    )
    error = result.get("error")
    error_message = str(error.get("message", "")) if isinstance(error, dict) else ""
    _check(
        "flow_id" in error_message,
        f"the surfaced error names the missing flow_id, not a generic swallow (got: {error_message!r})",
    )
    _check(
        len(factory.calls) == 0,
        "no action submitted when the fail-fast flow_id check trips",
    )


def _stamped_reach(state: _FakeStateService) -> object | None:
    """The completion_reach value written to the job's metadata, if any."""
    for updates in state.update_state_calls:
        raw = updates.get("metadata")
        if not isinstance(raw, str):
            continue
        decoded = json.loads(raw)
        if isinstance(decoded, dict) and COMPLETION_REACH_KEY in decoded:
            return decoded[COMPLETION_REACH_KEY]
    return None


def test_bridge_dispatch_is_stamped_unreached() -> None:
    """Case 5: a job dispatched by a bridge is stamped as having no return path.

    Measured 2026-08-14: a completion never returns over the dispatching
    bridge — the continuation posts into an IO channel a bridge flow does not
    have — so the job row is the only place its result can be found. The
    stamp is what makes that row findable.

    (Mutation: drop the `self._record_completion_reach(job_id)` call from
    `update_job`'s terminal branch -> this case goes from PASS to FAIL.)
    """
    manager, state, _factory, _frg = _make_manager(
        _completion_handlers_metadata(), trigger_type="bridge_process_call"
    )
    manager.update_job("job-smoke-5", {"status": "completed", "result": {"ok": True}})
    _check(
        _stamped_reach(state) == REACH_BRIDGE_DISPATCH_NO_RETURN_PATH,
        "a bridge_process_call flow stamps completion_reach="
        f"{REACH_BRIDGE_DISPATCH_NO_RETURN_PATH}",
    )


def test_channel_flow_is_not_stamped_unreached() -> None:
    """Case 6: a job from a channel flow is stamped as reachable, not unreached.

    The negative control for case 5: without it, a stamp hardcoded to the
    unreached value would pass case 5 while making the drain verb list every
    finished job in the system.

    (Mutation: make `_record_completion_reach` always stamp
    REACH_BRIDGE_DISPATCH_NO_RETURN_PATH -> this case goes from PASS to FAIL.)
    """
    manager, state, _factory, _frg = _make_manager(
        _completion_handlers_metadata(), trigger_type="operator_message"
    )
    manager.update_job("job-smoke-6", {"status": "completed", "result": {"ok": True}})
    _check(
        _stamped_reach(state) == REACH_CHANNEL_FLOW,
        f"a non-bridge flow stamps completion_reach={REACH_CHANNEL_FLOW}",
    )


def test_unreadable_flow_leaves_stamp_absent() -> None:
    """Case 7: an unreadable flow row leaves the stamp ABSENT, never guessed.

    Absent means unmeasured. Writing a default here would let the drain verb
    report a measurement nobody took.

    (Mutation: make `_record_completion_reach` fall back to
    REACH_CHANNEL_FLOW when `_read_flow_trigger_type` returns None -> this
    case goes from PASS to FAIL.)
    """
    manager, state, _factory, _frg = _make_manager(
        _completion_handlers_metadata(), trigger_type=None
    )
    result = manager.update_job("job-smoke-7", {"status": "completed", "result": {"ok": True}})
    _check(
        _stamped_reach(state) is None,
        "an unreadable flow row writes no completion_reach value at all",
    )
    _check(
        result.get("action_status") == "completed",
        "a stamp that cannot be written does not fail the completion it describes",
    )


def test_stamp_survives_a_raising_completion_handler() -> None:
    """Case 8: the stamp lands even when the continuation then fails loudly.

    This is the doctrine's second ledger gap (a terminal row whose handler
    raised, so nothing was submitted and no token resolved — a row that does
    not LOOK stuck). Stamping BEFORE `_handle_completion_actions` is what
    keeps such a row findable.

    The fixture raises INSIDE the handler while flow_id is present: a
    malformed process_key trips `_parse_process_key`'s ValueError during
    `_build_action_definition`. Forcing the raise by removing flow_id instead
    would prove nothing — with no flow to classify, the stamp is legitimately
    skipped, so that fixture measures the wrong thing.

    (Mutation: move the `record_completion_reach(...)` call to AFTER the
    `_handle_completion_actions` call -> this case goes from PASS to FAIL.)
    """
    metadata = _completion_handlers_metadata()
    handlers = metadata["completion_handlers"]
    assert isinstance(handlers, dict)
    result_handler = handlers["result"]
    assert isinstance(result_handler, dict)
    result_handler["process_key"] = "not_a_three_part_key"  # wint:negative-fixture
    manager, state, factory, _frg = _make_manager(
        metadata, trigger_type="bridge_process_call"
    )
    result = manager.update_job("job-smoke-8", {"status": "completed", "result": {"ok": True}})
    _check(
        result.get("action_status") == "error",
        "the malformed-process_key failure surfaces loudly, not as a false completion",
    )
    _check(
        len(factory.calls) == 0,
        "no continuation was submitted for this job",
    )
    _check(
        _stamped_reach(state) == REACH_BRIDGE_DISPATCH_NO_RETURN_PATH,
        "the job is still stamped, so a row whose continuation never ran stays findable",
    )


def test_fakes_track_real_signatures() -> None:
    """Rail (coordinator seat, arm-6363cec0...): protocol drift must red this smoke."""
    real_submit = list(inspect.signature(ActionFactory.submit_action_definition).parameters)
    fake_submit = list(inspect.signature(_FakeActionFactory.submit_action_definition).parameters)
    _check(
        real_submit == fake_submit,
        f"_FakeActionFactory.submit_action_definition params {fake_submit} match "
        f"the real ActionFactory's {real_submit}",
    )

    for method_name in ("read_state", "write_state", "update_state"):
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
    print("D0.3/Option-A AsyncJobManager completion-handler smoke")
    print("\nCase 1: happy path fires the configured completion handler")
    test_happy_path_fires_completion_handler()
    print("\nCase 2: no completion_handlers -> silent, legitimate no-op")
    test_missing_completion_handlers_is_silent_noop()
    print("\nCase 3: non-terminal status -> completion routing never runs")
    test_non_terminal_status_does_not_fire()
    print("\nCase 4: missing flow_id -> loud structured failure, not swallowed")
    test_missing_flow_id_fails_loud_not_silent()
    print("\nCase 5: a bridge dispatch is stamped as having no return path")
    test_bridge_dispatch_is_stamped_unreached()
    print("\nCase 6: a channel flow is stamped reachable (negative control)")
    test_channel_flow_is_not_stamped_unreached()
    print("\nCase 7: an unreadable flow leaves the stamp absent, never guessed")
    test_unreadable_flow_leaves_stamp_absent()
    print("\nCase 8: the stamp lands even when the continuation then fails")
    test_stamp_survives_a_raising_completion_handler()
    print("\nRail: fakes track the real protocol/implementation signatures")
    test_fakes_track_real_signatures()

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} check(s) failed")
        for message in _failures:
            print(f"  - {message}")
        return 1
    print("PASS: AsyncJobManager completion-handler firing behaves as traced in D0.3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
