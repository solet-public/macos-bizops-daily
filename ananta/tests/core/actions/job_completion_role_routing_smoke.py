#!/usr/bin/env python3
"""Lane W guard: a job completion is PUSHED to its durable role, replacing the
inference continuation, without ever costing the flow its token.

Companion to `async_job_manager_completion_handler_smoke.py` (which pins the
continuation path this one deliberately replaces) and to
`job_service_retrieval_smoke.py` (which pins the pull path this one supersedes
for addressable callers).

Why replace rather than duplicate (measured 2026-08-14, the ruling's basis): a
bridge-origin flow has no inference-vertex binding, so its continuation resolves
DEFAULT, forwards to the `sys:autonomic` frontier holder, and — when that slot is
vacant, as it was on the live deployment when this was written — DEFERS into the
shared durable no-loss queue. Duplicating would hand every routed completion to a
future `sys:autonomic` claimant's first drain as well, re-driving work whose owner
had already been told.

Cases, each naming the mutation that reds it:

  1. A flow stamped `completion_route_role` submits the DELIVERY verb and NOT
     the configured continuation, with the envelope carrying job_id, provider,
     status and payload. (Mutation: make `job_completion_route.resolve_route`
     return None unconditionally -> FAIL.)
  2. A flow with no routing stamp is untouched: the continuation fires and no
     delivery is submitted. (Mutation: make `read_completion_route_role` return
     a role when the key is absent -> FAIL. This is the negative control: it is
     what keeps coverage honestly partial, since a bare-shell CLI caller
     resolves to no role and MUST stay on the pull path.)
  3. RULED REQUIREMENT (2): an `error` completion routes exactly like a
     `completed` one, status carried in the envelope. (Mutation: restrict
     `resolve_route`'s status set to {"completed"} -> FAIL.)
  4. RULED REQUIREMENT (1): a routed completion still resolves its FRG token —
     routing changes who hears about a job, never whether its flow closes.
     (Mutation: move `_resolve_job_token` inside the `if not ...` block so the
     routed path skips it -> FAIL.)
  5. A delivery whose submission RAISES falls back to the continuation, and the
     pre-stamped unreached marker still stands — the push and the marker can
     never both be lost. (Mutation: let the exception propagate, or return True
     from the except branch -> FAIL.)
  6. A non-terminal transition routes nothing. HONEST SCOPE, stated because a
     check no single mutation can red is decoration: "processing" is blocked
     independently by THREE guards — `update_job`'s outer terminal tuple,
     `resolve_route`'s own status set, and
     `_handle_completion_actions`'s early return — so widening any ONE leaves
     this case GREEN. It was verified non-vacuous with a COMPOUND mutation
     (outer tuple AND inner set both widened to include "processing" -> FAIL).
     Treat it as defence-in-depth, not as a single-point regression guard.
  7. `cancelled` IS terminal, so it passes that outer guard and does reach the
     inner one — and must still not route, because it has no continuation to
     replace and pushing one would be new behaviour, not a replacement. This is
     the case that actually pins the inner guard. (Mutation: add "cancelled" to
     `resolve_route`'s status set -> FAIL.)

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:

    .venv/bin/python3 ananta/tests/core/actions/job_completion_role_routing_smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.job_completion_reach import (  # noqa: E402
    COMPLETION_REACH_KEY,
    REACH_BRIDGE_DISPATCH_NO_RETURN_PATH,
)
from ananta.core.state.job_completion_route import (  # noqa: E402
    COMPLETION_DELIVERY_PROCESS_KEY,
    COMPLETION_ROUTE_ROLE_KEY,
)

_RESULT_HANDLER_KEY = "plugin::_smoke_only::noop_result"  # wint:negative-fixture
_ERROR_HANDLER_KEY = "plugin::_smoke_only::noop_error"  # wint:negative-fixture
_ROLE = "lane-w-smoke-role"
_PROVIDER = "g_suite_plugin.sheets_create_from_files"
_TOKEN = "tok-smoke-1"

_failures: list[str] = []


def _check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        _failures.append(message)


class _FakeActionFactory:
    """Tracks ActionFactory.submit_action_definition; can be told to raise."""

    def __init__(self, *, raise_on_delivery: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self._raise_on_delivery = raise_on_delivery

    def submit_action_definition(
        self,
        action_definition: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> str:
        if (
            self._raise_on_delivery
            and action_definition.get("process_key") == COMPLETION_DELIVERY_PROCESS_KEY
        ):
            raise RuntimeError("smoke: delivery submission failed")
        self.calls.append(action_definition)
        return "ae-smoke-fake-action-id"

    def submitted(self, process_key: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c.get("process_key") == process_key]


class _FakeFlowRuntimeGraph:
    def __init__(self) -> None:
        self.complete_token_calls: list[str] = []

    def complete_token(
        self,
        token_id: str,
        success: bool = True,
        result_summary: dict[str, object] | None = None,
    ) -> None:
        self.complete_token_calls.append(token_id)


class _FakeStateService:
    """Job + flow reads, with the flow row carrying real `trigger_data` JSON.

    `trigger_data` is a TEXT column holding JSON (written via json.dumps by
    FlowManager), so the fake stores it as a STRING — a dict here would let a
    decode bug pass.
    """

    def __init__(self, *, route_role: str | None, flow_token_id: str | None = _TOKEN) -> None:
        self._route_role = route_role
        self._flow_token_id = flow_token_id
        self.update_state_calls: list[dict[str, object]] = []

    def _trigger_data(self) -> str:
        trigger: dict[str, object] = {"source_namespace": "agent_messaging_plugin"}
        if self._route_role is not None:
            trigger[COMPLETION_ROUTE_ROLE_KEY] = self._route_role
        return json.dumps(trigger)

    def read_state(self, namespace: str, query: dict[str, object]) -> dict[str, object]:
        table = query.get("table")
        filters = query.get("filters")
        row_id = filters.get("id") if isinstance(filters, dict) else None
        if table == "job_payload":
            return {"action_status": "completed", "data": {"records": []}}
        if table == "flows":
            return {
                "action_status": "completed",
                "data": {
                    "records": [
                        {
                            "id": row_id,
                            "trigger_type": "bridge_process_call",
                            "trigger_data": self._trigger_data(),
                        }
                    ]
                },
            }
        if table == "job":
            return {
                "action_status": "completed",
                "data": {
                    "records": [
                        {
                            "id": row_id,
                            "metadata": _metadata(),
                            "provider_name": _PROVIDER,
                            "flow_token_id": self._flow_token_id,
                        }
                    ]
                },
            }
        raise AssertionError(f"unexpected read_state table: {table!r}")

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
        self.update_state_calls.append(updates)
        return {"action_status": "completed"}


def _metadata() -> dict[str, object]:
    return {
        "flow_id": "flow-smoke-1",
        "session_id": "session-smoke-1",
        "completion_handlers": {
            "result": {"process_key": _RESULT_HANDLER_KEY, "template": {}, "notes": "smoke"},
            "error": {"process_key": _ERROR_HANDLER_KEY, "template": {}, "notes": "smoke"},
        },
    }


def _make_manager(
    *,
    route_role: str | None,
    raise_on_delivery: bool = False,
    flow_token_id: str | None = _TOKEN,
) -> tuple[AsyncJobManager, _FakeStateService, _FakeActionFactory, _FakeFlowRuntimeGraph]:
    state = _FakeStateService(route_role=route_role, flow_token_id=flow_token_id)
    factory = _FakeActionFactory(raise_on_delivery=raise_on_delivery)
    frg = _FakeFlowRuntimeGraph()
    manager = AsyncJobManager(state_service=state, flow_runtime_graph=frg)  # type: ignore[arg-type]
    manager.set_action_factory(factory)  # type: ignore[arg-type]
    return manager, state, factory, frg


def _stamped_reach(state: _FakeStateService) -> object:
    """The last completion_reach value written, or None."""
    for updates in reversed(state.update_state_calls):
        raw = updates.get("metadata")
        if isinstance(raw, str):
            decoded = json.loads(raw)
            if isinstance(decoded, dict) and COMPLETION_REACH_KEY in decoded:
                return decoded[COMPLETION_REACH_KEY]
    return None


def test_routed_flow_replaces_the_continuation() -> None:
    """Case 1: a routing role submits the delivery verb INSTEAD of the continuation."""
    print("\ntest_routed_flow_replaces_the_continuation")
    manager, _state, factory, _frg = _make_manager(route_role=_ROLE)
    manager.update_job("job-1", {"status": "completed", "result": {"url": "https://x"}})

    delivered = factory.submitted(COMPLETION_DELIVERY_PROCESS_KEY)
    _check(len(delivered) == 1, "exactly one delivery action is submitted")
    _check(
        not factory.submitted(_RESULT_HANDLER_KEY),
        "the configured continuation is NOT also submitted (replace, not duplicate)",
    )
    args = delivered[0].get("arguments") if delivered else None
    args = args if isinstance(args, dict) else {}
    _check(args.get("name") == _ROLE, "the delivery is addressed to the stamped role")
    _check(args.get("job_id") == "job-1", "the envelope carries the job_id")
    _check(args.get("provider_name") == _PROVIDER, "the envelope carries the provider")
    _check(args.get("status") == "completed", "the envelope carries the terminal status")
    _check(
        args.get("payload") == {"url": "https://x"},
        "the envelope carries the payload, so the recipient needs no second lookup",
    )


def test_unrouted_flow_stays_on_the_pull_path() -> None:
    """Case 2 (negative control): no stamp -> continuation fires, no delivery."""
    print("\ntest_unrouted_flow_stays_on_the_pull_path")
    manager, state, factory, _frg = _make_manager(route_role=None)
    manager.update_job("job-2", {"status": "completed", "result": {"url": "https://x"}})

    _check(
        not factory.submitted(COMPLETION_DELIVERY_PROCESS_KEY),
        "an unaddressable caller gets NO push (coverage stays honestly partial)",
    )
    _check(
        len(factory.submitted(_RESULT_HANDLER_KEY)) == 1,
        "the existing continuation still fires unchanged",
    )
    _check(
        _stamped_reach(state) == REACH_BRIDGE_DISPATCH_NO_RETURN_PATH,
        "and the row keeps the unreached stamp so the drain still finds it",
    )


def test_error_completions_route_like_results() -> None:
    """Case 3 (ruled requirement 2): failures reach the owner too."""
    print("\ntest_error_completions_route_like_results")
    manager, _state, factory, _frg = _make_manager(route_role=_ROLE)
    manager.update_job("job-3", {"status": "error", "error": {"message": "boom"}})

    delivered = factory.submitted(COMPLETION_DELIVERY_PROCESS_KEY)
    _check(len(delivered) == 1, "an error completion is delivered to the role")
    args = delivered[0].get("arguments") if delivered else None
    args = args if isinstance(args, dict) else {}
    _check(args.get("status") == "error", "the envelope names the error status")
    _check(
        args.get("payload") == {"message": "boom"},
        "the error payload travels with it, not just the status",
    )
    _check(
        not factory.submitted(_ERROR_HANDLER_KEY),
        "the error continuation is replaced, matching the result path",
    )


def test_routed_completion_still_resolves_its_flow_token() -> None:
    """Case 4 (ruled requirement 1): the flow must still close."""
    print("\ntest_routed_completion_still_resolves_its_flow_token")
    manager, _state, factory, frg = _make_manager(route_role=_ROLE)
    manager.update_job("job-4", {"status": "completed", "result": {"ok": True}})

    _check(
        len(factory.submitted(COMPLETION_DELIVERY_PROCESS_KEY)) == 1,
        "precondition: this completion really did take the routed path",
    )
    _check(
        frg.complete_token_calls == [_TOKEN],
        "the FRG token is resolved exactly once on the ROUTED path",
    )


def test_failed_delivery_falls_back_loudly() -> None:
    """Case 5: never lose both the push and the marker."""
    print("\ntest_failed_delivery_falls_back_loudly")
    manager, state, factory, frg = _make_manager(route_role=_ROLE, raise_on_delivery=True)
    result = manager.update_job("job-5", {"status": "completed", "result": {"ok": True}})

    _check(
        result.get("action_status") == "completed",
        "a failed delivery does not fail the job update itself",
    )
    _check(
        len(factory.submitted(_RESULT_HANDLER_KEY)) == 1,
        "the continuation is restored as the fallback rather than dropped",
    )
    _check(
        _stamped_reach(state) == REACH_BRIDGE_DISPATCH_NO_RETURN_PATH,
        "the unreached marker still stands, so the drain still lists the job",
    )
    _check(
        frg.complete_token_calls == [_TOKEN],
        "and the flow token is still resolved on the fallback path",
    )


def test_non_terminal_status_never_routes() -> None:
    """Case 6: gated by update_job's OUTER terminal guard (see module docstring)."""
    print("\ntest_non_terminal_status_never_routes")
    manager, _state, factory, _frg = _make_manager(route_role=_ROLE)
    manager.update_job("job-6", {"status": "processing"})

    _check(
        not factory.calls,
        "a non-terminal transition submits nothing at all",
    )


def test_cancelled_is_terminal_but_never_routes() -> None:
    """Case 7: pins the INNER guard — cancelled reaches it and must not route."""
    print("\ntest_cancelled_is_terminal_but_never_routes")
    manager, _state, factory, frg = _make_manager(route_role=_ROLE)
    manager.update_job("job-7", {"status": "cancelled"})

    _check(
        not factory.submitted(COMPLETION_DELIVERY_PROCESS_KEY),
        "a cancelled job is NOT pushed (there is no continuation to replace)",
    )
    _check(
        frg.complete_token_calls == [_TOKEN],
        "but a cancelled job still resolves its flow token, as it always did",
    )


def main() -> int:
    print("Lane W: job completion role routing")
    test_routed_flow_replaces_the_continuation()
    test_unrouted_flow_stays_on_the_pull_path()
    test_error_completions_route_like_results()
    test_routed_completion_still_resolves_its_flow_token()
    test_failed_delivery_falls_back_loudly()
    test_non_terminal_status_never_routes()
    test_cancelled_is_terminal_but_never_routes()

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
