#!/usr/bin/env python3
"""Smoke the one-call, no-class-guess role provisioning path.

The positive leg provisions ``Git-Controller`` from no role row and proves
that the managed worker is named for the mutation gate while its first turn
instructs the worker itself to claim.  The red leg restores the former caller
mistake (``principal`` on a fresh Git-Controller) and proves it still reaches
the intended ``role_not_legislated`` refusal.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from _recorded_lane_worktree_fixture import RecordedLaneWorktreeFixture  # noqa: E402
from _register_unit_double import RegisterUnitDouble  # noqa: E402
from ananta.core.services.call_context import CallContext  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_INSTANCE_ID,
    COL_AGENT_SESSION_ID,
    COL_CLAIM_EPOCH,
    COL_HOLDER_IDENTITY,
    COL_HOLDER_KIND,
    COL_ROLE,
    HOLDER_KIND_SESSION,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)

import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
from agent_messaging_plugin.model_capability_store import load_seed_table, seed_catalog  # noqa: E402
from agent_messaging_plugin.model_capability_verbs import select_dispatch_tier  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    TABLE_MODEL_CAPABILITY_CELL,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    SpawnSessionRequest,
    VerbError,
    spawn_session,
)

_HOST = "role-provisioning-fixture-host"
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


class _Channel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class _Driver:
    def __init__(self) -> None:
        self.channel = _Channel()
        self.spawn_calls = 0

    def spawn(self, spec: object) -> str:
        del spec
        self.spawn_calls += 1
        return "role-provisioning-fixture-ref"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> _Channel:
        del host_ref
        return self.channel

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


class _Orchestrator:
    def __init__(self, state: object) -> None:
        self._state = state

    def get_service(self, name: str) -> object:
        if name == "state_service":
            return self._state
        raise KeyError(name)


class _Registry:
    def agent_session_id_for_instance(self, agent_instance_id: str) -> str:
        return {
            "agi-coordinator": "ases-coordinator",
            "agi-existing-controller": "ases-existing-controller",
        }.get(agent_instance_id, "")


def _state() -> Any:
    state = RealShapeState()
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "record": {
                "external_id": role_binding_external_id("Coordinator-Main"),
                COL_ROLE: "Coordinator-Main",
                COL_HOLDER_KIND: HOLDER_KIND_SESSION,
                COL_AGENT_INSTANCE_ID: "agi-coordinator",
                COL_AGENT_SESSION_ID: "ases-coordinator",
                COL_HOLDER_IDENTITY: {"agent_id": "codex", "session_label": "Coordinator-Main"},
                COL_CLAIM_EPOCH: 1,
            },
            "conflict_columns": ["external_id"],
        },
    )
    now = datetime.now(UTC).isoformat()
    seed_catalog(state, seed=load_seed_table())
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        row.update(
            acceptance=CELL_ACCEPTANCE_ACCEPTED,
            accepted_at=now,
            measured_at=now,
            last_refresh_run_id="role-provisioning-fixture",
        )
    return state


def _raw(tmp: Path, state: Any) -> dict[str, object]:
    brief = tmp / "brief.md"
    brief.write_text("role provisioning fixture\n", encoding="utf-8")
    now = datetime.now(UTC)
    selected = select_dispatch_tier(
        state,
        {"required_score": 38, "billing_objective": "metered_usd", "dispatch_kind": "infrastructure"},
    )
    receipt = selected["selection_receipt"]
    chosen = receipt["selected"]
    return {
        "role_name": "Git-Controller",
        "lane_id": "role-provisioning-fixture",
        "brief_ref": str(brief),
        "brief_sha256": hashlib.sha256(brief.read_bytes()).hexdigest(),
        "expected_path": str(tmp / "report.md"),
        "completion_contract": {
            "evidence_obligations": [{"id": "focused", "allowed_statuses": ["pass"]}],
            "allowed_verdicts": ["BLOCKED"],
        },
        "work_class": "production_mutation",
        "budget_line": "role-provisioning-fixture-budget",
        "model": chosen["model"],
        "dispatch_kind": "infrastructure",
        "difficulty_score": receipt["required_score"],
        "selection_receipt": receipt,
        "effort": chosen["effort"],
        "agent_runtime": "codex",
        "allowed_hosts": [_HOST],
        "host": _HOST,
        "spawned_by_role": "Coordinator-Main",
        "visibility": "headless",
        "report_by_seconds": 900,

        "allowed_tools": [],
        "permission_mode": "bypassPermissions",
        "transport": "mcp",
        "allow_askuserquestion": False,
        "degraded_hooks_acknowledged": False,
        "uptake_due_at": (now + timedelta(minutes=2)).isoformat(),
        "report_by": (now + timedelta(minutes=15)).isoformat(),
        "watchdog_due_at": (now + timedelta(minutes=3)).isoformat(),

    }


def _authenticated_state() -> dict[str, object]:
    return {
        "call_context": CallContext.for_operator(),
        "authenticated_principal": {
            "client_id": "fixture-client",
            "agent_id": "codex",
            "agent_instance_id": "agi-coordinator",
            "bridge_id": "agc-coordinator",
            "session_id": "ases-coordinator",
        }
    }


def _bind_existing_controller(state: Any) -> None:
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "record": {
                "external_id": role_binding_external_id("Git-Controller"),
                COL_ROLE: "Git-Controller",
                COL_HOLDER_KIND: HOLDER_KIND_SESSION,
                COL_AGENT_INSTANCE_ID: "agi-existing-controller",
                COL_AGENT_SESSION_ID: "ases-existing-controller",
                COL_HOLDER_IDENTITY: {
                    "agent_id": "codex",
                    "session_label": "Git-Controller",
                },
                COL_CLAIM_EPOCH: 1,
            },
            "conflict_columns": ["external_id"],
        },
    )


def _assert_existing_controller_is_reused(
    *,
    plugin: AgentMessagingPlugin,
    state: Any,
    tmp: Path,
    fixture: RecordedLaneWorktreeFixture,
    driver: _Driver,
) -> None:
    _bind_existing_controller(state)
    existing_result = plugin.provision_role_session(
        {"parameters": _raw(tmp, state)}, _authenticated_state(),
    )
    existing_data = cast(dict[str, Any], existing_result.get("data", {}))
    existing_holder = cast(dict[str, Any], existing_data.get("existing_holder", {}))
    _check(
        existing_result.get("action_status") == "completed"
        and existing_data.get("provisioning_action") == "existing_live_holder"
        and existing_holder.get("agent_instance_id") == "agi-existing-controller",
        "a live Git-Controller is returned rather than provisioned again",
    )
    _check(
        len(fixture.provisioning_calls) == 1 and driver.spawn_calls == 1,
        "RED mutation: a second provision request never creates a second controller",
    )


def test_provisioning_and_red_mutation() -> None:
    state = _state()
    driver = _Driver()
    registry_key = ("codex", _HOST)
    session_hosts._REGISTRY[registry_key] = driver  # noqa: SLF001
    previous_home = os.environ.get("APP_HOME")
    previous_name = os.environ.get("SOLET_NAME")
    try:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            # The register Unit is resolved from the lane root (APP_HOME's
            # parent), so the fixture names its own temp checkout, and the
            # register is a double that can never reach the live psolet.
            lane_root = tmp / "repo"
            (lane_root / ".git").mkdir(parents=True)
            os.environ["APP_HOME"] = str(lane_root / "profile")
            os.environ["SOLET_NAME"] = "fixture"
            plugin = AgentMessagingPlugin()
            plugin.orchestrator_ref = cast(Any, _Orchestrator(state))
            plugin._peer_registry = cast(Any, _Registry())  # noqa: SLF001
            register = RegisterUnitDouble()
            plugin._register_unit_client = lambda: register  # type: ignore[method-assign]  # noqa: SLF001
            with RecordedLaneWorktreeFixture(tmp) as fixture:
                result = plugin.provision_role_session(
                    {"parameters": _raw(lane_root, state)}, _authenticated_state(),
                )
                data = cast(dict[str, Any], result.get("data", {}))
                attempt = cast(dict[str, Any], data.get("attempt", {}))
                _check(
                    result.get("action_status") == "completed"
                    and data.get("resolved_role_class") == "project",
                    "one call resolves a fresh Git-Controller to project instead of principal",
                )
                _check(
                    attempt.get("lifecycle_state") == "spawning"
                    and attempt.get("first_turn_delivered") is True,
                    "the project-class controller worker is actually spawned through managed dispatch",
                )
                _check(
                    fixture.provisioning_calls
                    and fixture.provisioning_calls[0].role_name == "Git-Controller",
                    "the spawned lane worktree is recorded under the requested controller role",
                )
                _check(
                    bool(driver.channel.sent)
                    and "CLAIM YOUR ROLE BINDING FIRST" in driver.channel.sent[0]
                    and "Git-Controller" in driver.channel.sent[0],
                    "the first turn directs the worker itself to claim; the spawner never claims it",
                )
                gate = cast(dict[str, Any], data.get("git_controller_gate", {}))
                _check(
                    gate.get("status") == "not_armed" and len(gate.get("launchers", [])) == 2,
                    "the result reports an unarmed launcher gate without modifying launchers",
                )

                _assert_existing_controller_is_reused(
                    plugin=plugin,
                    state=state,
                    tmp=lane_root,
                    fixture=fixture,
                    driver=driver,
                )

            red_code = ""
            try:
                spawn_session(
                    state,
                    SpawnSessionRequest(
                        role_class="principal",
                        lane_id="red-principal-git-controller",
                        brief_ref="red-mutation",
                        work_class="production_mutation",
                        budget_line="red-mutation",
                        role_name="Git-Controller-Red",
                        host=_HOST,
                        dispatch_kind="infrastructure",
                    ),
                )
            except VerbError as exc:
                red_code = exc.code
            _check(
                red_code == "role_not_legislated",
                "RED mutation: restoring the caller's principal choice reproduces role_not_legislated",
            )
    finally:
        if previous_home is None:
            os.environ.pop("APP_HOME", None)
        else:
            os.environ["APP_HOME"] = previous_home
        if previous_name is None:
            os.environ.pop("SOLET_NAME", None)
        else:
            os.environ["SOLET_NAME"] = previous_name
        session_hosts._REGISTRY.pop(registry_key, None)  # noqa: SLF001


def main() -> int:
    test_provisioning_and_red_mutation()
    print(f"role provisioning smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
