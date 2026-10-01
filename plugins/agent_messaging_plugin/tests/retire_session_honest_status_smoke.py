#!/usr/bin/env python3
"""``retire_session`` / ``terminate_session`` say what they did NOT do (iss_7ee6fb98).

A lane launched by hand registers as ``host='operator'``: the operator driver
cannot stop a process it did not spawn, so ``terminate_session`` lands the
ledger transition and ``retire_session`` returned ``completed`` with no sign
that the process was still running and still listed by ``peer_list`` (20 lanes,
2026-09-30). The refusal to kill a process the ledger never spawned is
deliberate and unchanged; the defect was the silence. This smoke pins the
result: ``host_action`` (``terminated`` / ``none_available`` /
``not_attempted``) with the driver's own remedy, and, on the plugin verb, a
``not_done`` list plus the live peer registration. Real ledger fake, real
operator driver, a recording fake driver for the host that does terminate.
Hermetic.
"""

from __future__ import annotations

import sys
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
from ananta.services.store import Store, open_store  # noqa: E402

import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    VerbError,
    retire_session,
    terminate_session,
)

_passed = 0
_failed: list[str] = []
_KILLING_HOST = "smoke-killing-host"
_WHO = "operator:none"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


class _KillingDriver:
    """A host driver that really ends its host: records what it was asked to stop."""

    def __init__(self) -> None:
        self.terminated: list[str] = []

    def spawn(self, spec: object) -> str:
        del spec
        return "fake-ref"

    def alive(self, host_ref: str) -> bool:
        return host_ref not in self.terminated

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del grace_seconds
        self.terminated.append(host_ref)

    def driver_channel(self, host_ref: str) -> None:
        del host_ref

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _live_row(state: StateManagementInterface, instance: str, host: str) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=instance, lane_id=f"lane-{instance}", brief_ref="",
            work_class="read_only", budget_line="b1", host=host,
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id=instance, from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by=_WHO,
    )
    # No derivable lane worktree: adjudication is a no-op, as for the lanes in the report.
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": instance}},
        {"role_name": "", "local_name": ""},
    )


def test_operator_host_retire_reports_the_host_was_not_stopped() -> None:
    state = _state()
    _live_row(state, "agi-op", "operator")
    result = retire_session(state, agent_instance_id="agi-op", directed_by=_WHO)
    _check(result["already_retired"] is False, "the ledger transition still lands for an operator-hosted row")
    _check(read_managed_session(state, "agi-op")["lifecycle_state"] == LIFECYCLE_RETIRED, "the row is retired")
    _check(result.get("host") == "operator", "the result names the row's host")
    _check(result.get("host_action") == "none_available", "an operator-hosted retire reports host_action=none_available, not silence")
    _check("stop the process manually" in str(result.get("host_remedy")), "the driver's own remedy text is carried")


def test_terminating_host_reports_terminated() -> None:
    state = _state()
    driver = _KillingDriver()
    session_hosts._REGISTRY[_KILLING_HOST] = driver  # noqa: SLF001 -- test-only monkeypatch
    try:
        _live_row(state, "agi-kill", _KILLING_HOST)
        result = retire_session(state, agent_instance_id="agi-kill", directed_by=_WHO)
    finally:
        session_hosts._REGISTRY.pop(_KILLING_HOST, None)  # noqa: SLF001
    _check(result.get("host_action") == "terminated", "a driver that ended its host reports host_action=terminated")
    _check(result.get("host_remedy") is None, "a terminated host carries no remedy")
    _check(len(driver.terminated) == 1, "the driver was asked to terminate exactly once")


def test_repeat_and_redrive_do_not_claim_a_host_action() -> None:
    state = _state()
    _live_row(state, "agi-rep", "operator")
    first_terminate = terminate_session(state, agent_instance_id="agi-rep", directed_by=_WHO)
    _check(first_terminate.get("host_action") == "none_available", "terminate_session reports the same host_action")
    _check(first_terminate.get("host") == "operator", "terminate_session names the host")
    repeat_terminate = terminate_session(state, agent_instance_id="agi-rep", directed_by=_WHO)
    _check(
        repeat_terminate["already_terminal"] is True and repeat_terminate.get("host_action") == "not_attempted",
        "a repeat terminate_session did no host action and says so",
    )
    redrive = retire_session(state, agent_instance_id="agi-rep", directed_by=_WHO)
    _check(
        redrive["already_retired"] is False and redrive.get("host_action") == "not_attempted",
        "retiring an already-terminated row (re-drive) reports it did not attempt the host",
    )
    again = retire_session(state, agent_instance_id="agi-rep", directed_by=_WHO)
    _check(
        again["already_retired"] is True and again.get("host_action") == "not_attempted",
        "retiring an already-retired row reports it did not attempt the host",
    )
    _check(read_managed_session(state, "agi-rep")["lifecycle_state"] == LIFECYCLE_RETIRED, "the redrive still ends retired")


def test_unsupported_host_still_refuses() -> None:
    state = _state()
    _live_row(state, "agi-unk", "no-such-host")
    try:
        retire_session(state, agent_instance_id="agi-unk", directed_by=_WHO)
    except VerbError as exc:
        _check(exc.code == "unsupported_on_host", "a host with no driver still refuses loudly")
    else:
        _check(False, "a host with no driver still refuses loudly")
    _check(read_managed_session(state, "agi-unk")["lifecycle_state"] != LIFECYCLE_TERMINATED, "a refused retire leaves the ledger untouched")


def _registry_with(instance: str) -> PeerRegistry:
    store: Store = open_store(get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory")
    registry = PeerRegistry(bindings_store=store)
    registry.register(
        BridgeBinding(
            bridge_id="agc-live", agent_id="claude_code", agent_instance_id=instance,
            session_label="Lane", parent_pid=63626, agent_session_id="ases-live",
        ),
    )
    return registry


def _plugin(state: StateManagementInterface, registry: PeerRegistry | None) -> AgentMessagingPlugin:
    plugin = object.__new__(AgentMessagingPlugin)
    plugin._get_state_service = lambda: state  # type: ignore[method-assign]
    plugin._peer_registry = registry  # noqa: SLF001
    return plugin


def _retire_through_plugin(plugin: AgentMessagingPlugin, instance: str) -> dict[str, Any]:
    result = plugin.retire_session(
        {"parameters": {"agent_instance_id": instance}}, {"call_context": CallContext.for_operator()},
    )
    assert result["action_status"] == "completed", result
    return cast("dict[str, Any]", result["data"])


def test_plugin_verb_lists_what_was_not_done() -> None:
    state = _state()
    _live_row(state, "agi-reg", "operator")
    data = _retire_through_plugin(_plugin(state, _registry_with("agi-reg")), "agi-reg")
    not_done = data.get("not_done")
    _check(isinstance(not_done, list) and len(not_done) == 2, "an operator-hosted lane still registered reports both gaps")
    text = " | ".join(not_done) if isinstance(not_done, list) else ""
    _check("not stopped" in text and "stop the process manually" in text, "the host gap names the manual remedy")
    _check("peer_list" in text and "63626" in text, "the registration gap names the still-listed bridge pid")
    _check(
        data.get("peer_registration") == {"bridge_id": "agc-live", "parent_pid": 63626},
        "the live registration is returned as structured data",
    )


def test_plugin_verb_retire_after_an_earlier_teardown_lists_nothing_it_left_undone() -> None:
    """F2: a lane torn down by an earlier terminate (or the sweep) is not a false open item."""
    state = _state()
    session_hosts._REGISTRY[_KILLING_HOST] = _KillingDriver()  # noqa: SLF001 -- test-only monkeypatch
    try:
        _live_row(state, "agi-earlier", _KILLING_HOST)
        earlier = terminate_session(state, agent_instance_id="agi-earlier", directed_by=_WHO)
        data = _retire_through_plugin(_plugin(state, _registry_with("agi-someone-else")), "agi-earlier")
    finally:
        session_hosts._REGISTRY.pop(_KILLING_HOST, None)  # noqa: SLF001
    _check(earlier["host_action"] == "terminated", "setup: the earlier terminate really ended the host")
    _check(data["host_action"] == "not_attempted", "retire after a terminate reports it attempted no host action")
    _check(data["not_done"] == [], "retire after a terminate lists nothing: this call left nothing undone")
    again = _retire_through_plugin(_plugin(state, _registry_with("agi-someone-else")), "agi-earlier")
    _check(
        again["already_retired"] is True and again["not_done"] == [],
        "retiring an already-retired, fully torn-down lane lists nothing",
    )


def test_plugin_verb_still_reports_a_registration_left_after_an_earlier_terminate() -> None:
    """The registration check is a measured fact of its own: it still reports after a terminate."""
    state = _state()
    _live_row(state, "agi-op-earlier", "operator")
    terminate_session(state, agent_instance_id="agi-op-earlier", directed_by=_WHO)
    data = _retire_through_plugin(_plugin(state, _registry_with("agi-op-earlier")), "agi-op-earlier")
    _check(
        len(data["not_done"]) == 1 and "still registered in peer_list" in data["not_done"][0],
        "a lane still registered after an earlier terminate reports exactly that registration, nothing about the host call",
    )


def test_plugin_verb_clean_teardown_has_nothing_left() -> None:
    state = _state()
    session_hosts._REGISTRY[_KILLING_HOST] = _KillingDriver()  # noqa: SLF001
    try:
        _live_row(state, "agi-clean", _KILLING_HOST)
        data = _retire_through_plugin(_plugin(state, _registry_with("agi-someone-else")), "agi-clean")
    finally:
        session_hosts._REGISTRY.pop(_KILLING_HOST, None)  # noqa: SLF001
    _check(data.get("not_done") == [], "a terminated host with no registration left reports not_done=[]")
    _check(data.get("peer_registration") is None, "no registration is reported when none is bound")


def test_plugin_verb_without_a_registry_says_it_did_not_look() -> None:
    state = _state()
    session_hosts._REGISTRY[_KILLING_HOST] = _KillingDriver()  # noqa: SLF001
    try:
        _live_row(state, "agi-noreg", _KILLING_HOST)
        data = _retire_through_plugin(_plugin(state, None), "agi-noreg")
    finally:
        session_hosts._REGISTRY.pop(_KILLING_HOST, None)  # noqa: SLF001
    _check(
        isinstance(data.get("not_done"), list) and any("registration was not checked" in g for g in data["not_done"]),
        "with no peer registry the verb says it did not check the registration, rather than claiming none",
    )


def main() -> None:
    print("=== retire_session honest status smoke ===")
    test_operator_host_retire_reports_the_host_was_not_stopped()
    test_terminating_host_reports_terminated()
    test_repeat_and_redrive_do_not_claim_a_host_action()
    test_unsupported_host_still_refuses()
    test_plugin_verb_lists_what_was_not_done()
    test_plugin_verb_retire_after_an_earlier_teardown_lists_nothing_it_left_undone()
    test_plugin_verb_still_reports_a_registration_left_after_an_earlier_terminate()
    test_plugin_verb_clean_teardown_has_nothing_left()
    test_plugin_verb_without_a_registry_says_it_did_not_look()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
