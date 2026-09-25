#!/usr/bin/env python3
"""Transport-shim smoke: the D1 L1 verbs as actually reached through
``AgentMessagingPlugin`` methods (not just the pure functions in
``session_lifecycle_verbs.py``, which ``session_lifecycle_verbs_smoke.py``
already covers). Verifies the ``params``/``state`` extraction, the
``VerbError`` -> ``_failure_result`` mapping, and that ``call_context`` flows
into ``directed_by`` on the session_transition audit trail.

Builds a BARE plugin instance (``object.__new__`` — the full
``AgentMessagingPlugin.__init__`` wants an orchestrator/config this smoke has
no business standing up) and monkeypatches ``_get_state_service`` to hand
back a real ``RealShapeState`` — the same technique other unit smokes in
this suite use to reach an EDGE method's body directly. This does NOT
exercise the platform's process registry, `process_search`, or a live
`process_call` (no unresolved `<<FIELD>>` check) — that layer needs a
running solet and a restart to pick up the new processes; NOT verified
here, and this slice's commit request must say so explicitly.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/session_lifecycle_transport_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.core.services.call_context import CallContext  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402

import agent_messaging_plugin.session_lifecycle_verbs as lifecycle_verbs  # noqa: E402
from agent_messaging_plugin.model_capability_store import load_seed_table, seed_catalog  # noqa: E402
from agent_messaging_plugin.model_capability_verbs import select_dispatch_tier  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_SESSION_TRANSITION,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
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


def _bare_plugin(state: StateManagementInterface) -> AgentMessagingPlugin:
    plugin = object.__new__(AgentMessagingPlugin)
    plugin._get_state_service = lambda: state  # type: ignore[method-assign]
    return plugin


def _params(**kwargs: object) -> dict[str, Any]:
    return {"parameters": kwargs}


def _state_with_context() -> dict[str, Any]:
    return {"call_context": CallContext.for_operator()}


def _error_code(result: dict[str, Any]) -> str | None:
    error = result.get("error")
    return error.get("code") if isinstance(error, dict) else None


def _selection_params(state: StateManagementInterface) -> dict[str, object]:
    """Seed a fresh accepted catalog and produce the receipt public spawn requires."""
    seed_catalog(state, seed=load_seed_table())
    measured_at = datetime.now(UTC).isoformat()
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        row.update(
            acceptance=CELL_ACCEPTANCE_ACCEPTED,
            accepted_at=measured_at,
            measured_at=measured_at,
            last_refresh_run_id="session-lifecycle-transport-fixture",
        )
    receipt = select_dispatch_tier(
        state,
        {"required_score": 38, "billing_objective": "metered_usd", "dispatch_kind": "infrastructure"},
    )["selection_receipt"]
    selected = receipt["selected"]
    return {
        "difficulty_score": receipt["required_score"],
        "selection_receipt": receipt,
        "agent_runtime": selected["runtime"],
        "model": selected["model"],
        "effort": selected["effort"],
    }


def test_phase_selector_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    _selection_params(state)
    plugin = _bare_plugin(state)
    selected = plugin.select_dispatch_tier(
        _params(required_score=50, dispatch_kind="test", scope_tags=["state_schema"],
                cost_tolerance=0),
        _state_with_context(),
    )
    cell = selected.get("data", {}).get("selected", {})
    _check(
        selected.get("action_status") == "completed"
        and (cell.get("runtime"), cell.get("model"), cell.get("effort"))
        == ("codex", "gpt-6-astra", "medium"),
        "public selector accepts test and selects Astra medium at score 50",
    )
    unknown = plugin.select_dispatch_tier(
        _params(required_score=50, dispatch_kind="unknown-phase"),
        _state_with_context(),
    )
    _check(
        unknown.get("action_status") == "completed"
        and unknown.get("data", {}).get("selected", {}).get("capability_score", 0) >= 50,
        "public selector accepts open nonblank phase provenance",
    )
    blank = plugin.select_dispatch_tier(
        _params(required_score=50, dispatch_kind=" "),
        _state_with_context(),
    )
    _check(
        blank.get("action_status") == "failed"
        and _error_code(blank) == "dispatch_kind_required",
        "public selector rejects blank phase provenance",
    )


def test_spawn_session_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    plugin = _bare_plugin(state)
    result = plugin.spawn_session(
        _params(
            role_class="bogus", lane_id="lane-1", brief_ref="b", work_class="read_only",
            budget_line="b1", dispatch_kind="infrastructure",
        ),
        _state_with_context(),
    )
    _check(
        result.get("action_status") == "failed" and _error_code(result) == "unknown_role_class",
        f"spawn_session transport maps VerbError to a failure result with the "
        f"right code (got {result!r})",
    )

    selector_params = _selection_params(state)
    ok = plugin.spawn_session(
        _params(
            role_class="ephemeral", lane_id="lane-1", brief_ref="b", work_class="read_only",
            budget_line="b1", host="operator", dispatch_kind="infrastructure",
            **selector_params,
        ),
        _state_with_context(),
    )
    _check(
        ok.get("action_status") == "failed" and _error_code(ok) == "host_cannot_spawn",
        "a valid-but-doomed spawn (operator host) surfaces host_cannot_spawn "
        "through the transport, not an unhandled exception",
    )


def test_list_and_status_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-z", lane_id="lane-z", brief_ref="", work_class="read_only",
            budget_line="b1", host="operator",
        ),
    )
    plugin = _bare_plugin(state)
    listed = plugin.list_sessions(_params(lane_id="lane-z"), {})
    _check(
        listed.get("action_status") == "completed"
        and len(listed["data"]["sessions"]) == 1,
        f"list_sessions transport returns the envelope-wrapped sessions list (got {listed!r})",
    )

    unfiltered = plugin.list_sessions(_params(), {})
    _check(
        unfiltered.get("action_status") == "failed"
        and _error_code(unfiltered) == "filter_required",
        "list_sessions transport maps an omitted filter to filter_required",
    )

    status = plugin.session_status(_params(agent_instance_id="agi-z"), {})
    _check(
        status.get("action_status") == "completed"
        and status["data"]["agent_instance_id"] == "agi-z",
        "session_status transport returns the ledger row",
    )

    missing = plugin.session_status(_params(agent_instance_id="agi-none"), {})
    _check(
        missing.get("action_status") == "failed" and _error_code(missing) == "session_not_found",
        "session_status(unknown) transport surfaces session_not_found",
    )


def test_terminate_retire_directed_by_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-y", lane_id="lane-y", brief_ref="", work_class="read_only",
            budget_line="b1", host="operator",
        ),
    )
    plugin = _bare_plugin(state)
    terminated = plugin.terminate_session(
        _params(agent_instance_id="agi-y"), _state_with_context(),
    )
    _check(
        terminated.get("action_status") == "completed"
        and terminated["data"]["already_terminal"] is False,
        "terminate_session transport lands the transition",
    )

    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_SESSION_TRANSITION, "filters": {"agent_instance_id": "agi-y"}},
    )
    rows = require_records(result)
    _check(
        len(rows) == 1 and rows[0]["directed_by"] == "operator",
        f"call_context flows through format_directed_by into the audit "
        f"trail's directed_by column (got {rows[0].get('directed_by') if rows else None!r})",
    )

    retired = plugin.retire_session(_params(agent_instance_id="agi-y"), _state_with_context())
    _check(
        retired.get("action_status") == "completed" and retired["data"]["already_retired"] is False,
        "retire_session transport lands terminated -> retired",
    )


def test_report_alive_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-w", lane_id="lane-w", brief_ref="", work_class="read_only",
            budget_line="b1", host="operator",
        ),
    )
    plugin = _bare_plugin(state)
    bad_status = plugin.report_alive(
        _params(agent_instance_id="agi-w", status="bogus"), _state_with_context(),
    )
    _check(
        bad_status.get("action_status") == "failed" and _error_code(bad_status) == "unknown_status",
        "report_alive transport surfaces unknown_status through the failure envelope",
    )
    before = read_managed_session(state, "agi-w").get("report_by")
    working = plugin.report_alive(
        _params(agent_instance_id="agi-w", status="working"), _state_with_context(),
    )
    after = read_managed_session(state, "agi-w")
    _check(
        working.get("action_status") == "completed"
        and working["data"]["lifecycle_state"] == "live",
        "the plugin report_alive process path promotes a spawned row to live",
    )
    _check(
        after["report_by"] != before,
        "the plugin report_alive process path re-arms report_by",
    )
    first_heartbeat = plugin.report_alive(
        _params(agent_instance_id="agi-w", status="heartbeat"), _state_with_context(),
    )
    after_first_heartbeat = read_managed_session(state, "agi-w")
    _check(
        first_heartbeat.get("action_status") == "completed"
        and after_first_heartbeat["last_heartbeat_at"] is not None,
        "a healthy heartbeat writes a distinct liveness reading through the transport path",
    )
    _check(
        after_first_heartbeat["heartbeat_failure_first_at"] is None,
        "a healthy heartbeat writes typed NULL for its absent failure timestamp",
    )
    failing_heartbeat = plugin.report_alive(
        _params(
            agent_instance_id="agi-w",
            status="heartbeat",
            heartbeat_failures_since_last=1,
            heartbeat_failure_first_at="2026-09-04T00:00:00+00:00",
            heartbeat_failure_last_reason="bridge timeout",
        ),
        _state_with_context(),
    )
    after_failing_heartbeat = read_managed_session(state, "agi-w")
    _check(
        failing_heartbeat.get("action_status") == "completed"
        and after_failing_heartbeat["heartbeat_failure_first_at"] == "2026-09-04T00:00:00+00:00",
        "a failing heartbeat records its failure episode before the healthy recovery",
    )
    recovered_heartbeat = plugin.report_alive(
        _params(agent_instance_id="agi-w", status="heartbeat"), _state_with_context(),
    )
    after_recovery = read_managed_session(state, "agi-w")
    _check(
        recovered_heartbeat.get("action_status") == "completed"
        and after_recovery["last_heartbeat_at"] != after_failing_heartbeat["last_heartbeat_at"],
        "a healthy recovery advances the heartbeat row rather than reusing the failing value",
    )
    _check(
        after_recovery["heartbeat_failure_first_at"] is None
        and after_recovery["heartbeat_failure_last_reason"] == "",
        "a healthy recovery clears the frozen failure episode with typed NULL",
    )
    before_empty_attempt = dict(after_recovery)
    empty_timestamp = plugin.report_alive(
        _params(
            agent_instance_id="agi-w",
            status="heartbeat",
            heartbeat_failure_first_at="",
        ),
        _state_with_context(),
    )
    _check(
        empty_timestamp.get("action_status") == "failed"
        and _error_code(empty_timestamp) == "invalid_heartbeat_failure_first_at",
        "an explicit empty timestamp fails loud instead of returning a false success",
    )
    _check(
        read_managed_session(state, "agi-w")["last_heartbeat_at"]
        == before_empty_attempt["last_heartbeat_at"],
        "the rejected empty timestamp does not advance the heartbeat row",
    )
    state.fail_next("update")
    rejected_write = plugin.report_alive(
        _params(agent_instance_id="agi-w", status="heartbeat"), _state_with_context(),
    )
    _check(
        rejected_write.get("action_status") == "failed"
        and _error_code(rejected_write) == "heartbeat_write_failed"
        and "injected update failure" in str(rejected_write.get("error")),
        "a state-layer write rejection reaches the caller as a failed report_alive result",
    )


def main() -> int:
    fixture_temp = tempfile.TemporaryDirectory()
    fixture_root = Path(fixture_temp.name)
    original_provision = lifecycle_verbs._provision_spawn_worktree  # noqa: SLF001
    original_retire = lifecycle_verbs._retire_lane_worktree  # noqa: SLF001
    original_remove = lifecycle_verbs.remove_lane_worktree  # noqa: SLF001
    provisioning_calls: list[tuple[str, str, lifecycle_verbs.LaneWorktree]] = []
    retirement_calls: list[dict[str, object]] = []

    def fixture_provision(
        state: StateManagementInterface, *, role_name: str, agent_instance_id: str, repository_root: str,
    ) -> lifecycle_verbs.LaneWorktree:
        del state, repository_root
        worktree = lifecycle_verbs.LaneWorktree(
            repo_root=fixture_root, root=fixture_root, path=fixture_root,
            branch=f"fixture/{role_name}/{agent_instance_id}",
        )
        provisioning_calls.append((role_name, agent_instance_id, worktree))
        return worktree

    def fixture_retire(row: dict[str, object]) -> None:
        retirement_calls.append(row)

    lifecycle_verbs._provision_spawn_worktree = fixture_provision  # type: ignore[assignment]  # noqa: SLF001
    lifecycle_verbs._retire_lane_worktree = fixture_retire  # type: ignore[assignment]  # noqa: SLF001
    lifecycle_verbs.remove_lane_worktree = lambda worktree: None  # type: ignore[assignment]  # noqa: SLF001
    try:
        test_spawn_session_transport()
        test_phase_selector_transport()
        test_list_and_status_transport()
        test_terminate_retire_directed_by_transport()
        test_report_alive_transport()
        _check(
            len(provisioning_calls) == 1
            and provisioning_calls[0][0] == "lane-1"
            and provisioning_calls[0][2].repo_root == fixture_root
            and provisioning_calls[0][2].path == fixture_root
            and provisioning_calls[0][2].branch
            == f"fixture/lane-1/{provisioning_calls[0][1]}",
            "transport spawn invokes provisioning with the lane-derived branch",
        )
        _check(
            len(retirement_calls) == 1
            and retirement_calls[0].get("agent_instance_id") == "agi-y",
            "transport retirement invokes teardown for the retiring session",
        )
    finally:
        lifecycle_verbs._provision_spawn_worktree = original_provision  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs._retire_lane_worktree = original_retire  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs.remove_lane_worktree = original_remove  # type: ignore[assignment]  # noqa: SLF001
        fixture_temp.cleanup()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
