#!/usr/bin/env python3
"""Red-to-green smoke for the bounded fleet identity qualifier.

The old qualifier stopped after spawning and returned three false lifecycle
facts.  These checks require the positive ordered path and require a
non-watcher delivery to remain red while still retiring the synthetic worker.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import agent_messaging_plugin.plugin as plugin_module  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.role_claim import RoleClaimSuccess  # noqa: E402

_passed = 0
_failed: list[str] = []
_INSTANCE_ID = "agi-qualify-smoke"
_SESSION_ID = f"ases-{_INSTANCE_ID}"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _Registry:
    def resolve_by_agent_session_id(self, session_id: str) -> BridgeBinding | None:
        if session_id != _SESSION_ID:
            return None
        return BridgeBinding(
            bridge_id="agc-qualify-smoke", agent_id="qualification",
            agent_instance_id=_INSTANCE_ID, session_label="qualify-fleet-worker",
            parent_pid=None, agent_session_id=_SESSION_ID,
        )


class _FakePlugin:
    _qualify_fleet_worker = AgentMessagingPlugin._qualify_fleet_worker
    _wait_for_qualification_binding = AgentMessagingPlugin._wait_for_qualification_binding
    _qualification_action_error = staticmethod(AgentMessagingPlugin._qualification_action_error)
    _retire_qualification_worker = staticmethod(AgentMessagingPlugin._retire_qualification_worker)

    def __init__(self, delivery: str) -> None:
        self._peer_registry = _Registry()
        self._bridge_manager = object()
        self.delivery = delivery
        self.releases = 0

    def _get_state_service(self) -> object:
        return object()

    def _handover_service(self) -> None:
        return None

    def peer_send_by_name(
        self, params: dict[str, object], state: dict[str, object],
    ) -> dict[str, object]:
        del params, state
        return {
            "action_status": "completed",
            "data": {"resolved_agent_instance_id": _INSTANCE_ID, "delivery": self.delivery},
        }

    def peer_release_role(
        self, params: dict[str, object], state: dict[str, object],
    ) -> dict[str, object]:
        del params, state
        self.releases += 1
        return {"action_status": "completed", "data": {}}


def _run(delivery: str) -> tuple[dict[str, Any], _FakePlugin, list[str]]:
    fake = _FakePlugin(delivery)
    retired: list[str] = []
    original_spawn = plugin_module.lifecycle_spawn_session
    original_claim = plugin_module.claim_role_for_session
    original_retire = plugin_module.lifecycle_retire_session
    plugin_module.lifecycle_spawn_session = (  # type: ignore[assignment]
        lambda state, request: {"agent_instance_id": _INSTANCE_ID}
    )
    plugin_module.claim_role_for_session = lambda **kwargs: RoleClaimSuccess(  # type: ignore[assignment]
        action="claimed", name="qualify-fleet-worker", agent_instance_id=_INSTANCE_ID,
        agent_session_id=_SESSION_ID,
    )
    plugin_module.lifecycle_retire_session = lambda state, *, agent_instance_id, directed_by: (  # type: ignore[assignment]
        retired.append(agent_instance_id) or {"already_retired": False, "dependencies_fired": 0}
    )
    try:
        result = AgentMessagingPlugin.qualify_fleet(fake, {}, {})
    finally:
        plugin_module.lifecycle_spawn_session = original_spawn
        plugin_module.claim_role_for_session = original_claim
        plugin_module.lifecycle_retire_session = original_retire
    return result, fake, retired


def main() -> int:
    green, fake, retired = _run("queued_watcher")
    data = green.get("data", {})
    _check(
        green.get("action_status") == "completed"
        and data.get("spawned") is True
        and data.get("role_claimed") is True
        and data.get("addressed_delivery_confirmed") is True
        and data.get("retired") is True,
        "success requires spawn, role claim, watcher-addressed delivery, and retirement",
    )
    _check(
        fake.releases == 1 and retired == [_INSTANCE_ID],
        "success releases the role and retires the exact worker",
    )

    red, failed_fake, failed_retired = _run("queued_notification")
    error = red.get("error")
    _check(
        red.get("action_status") == "failed"
        and isinstance(error, dict)
        and error.get("code") == "qualification_delivery_unobserved",
        "a non-watcher queued result is not over-credited as delivery observation",
    )
    _check(
        failed_fake.releases == 1 and failed_retired == [_INSTANCE_ID],
        "failed delivery still releases and retires the synthetic worker",
    )
    print(f"\nPASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
