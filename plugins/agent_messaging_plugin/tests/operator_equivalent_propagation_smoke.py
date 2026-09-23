#!/usr/bin/env python3
"""operator_equivalent propagation smoke (B1 Finding-B, no pytest, offline).

Pins the operator-access-preservation chain: a VERIFIED ``operator_equivalent``
OAuth client keeps OPERATOR authority once the streamable no-auth flag flips.

Chain under test (all real code, no mocks of the platform):
  vault ``is_operator_equivalent(client_id)``  [injected here]
    -> PlatformSurface._resolve_operator_equivalent(bridge)   (degrade-safe)
    -> PlatformSurface._build_process_call_trigger_data(..., operator_equivalent=)
       stamps ``authenticated_principal.operator_equivalent``
    -> ActionProcessor._build_call_context(action)            (master consumer)
       returns CallContext.for_operator_equivalent (is_operator_principal).

RED-FIRST: remove the ``"operator_equivalent": operator_equivalent`` stamp in
_build_process_call_trigger_data (or the _resolve/ wiring) and the operator case
resolves to for_external (non-operator) -> the for_operator_equivalent assertion
fails. The default (non-operator) client resolves to for_external either way.

Run from repo root:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/operator_equivalent_propagation_smoke.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.actions.action_processor import (  # noqa: E402
    ActionProcessor,
    QueuedActionProtocol,
)
from ananta.core.services.call_context import CallContext  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.models import BridgeSessionState  # noqa: E402
from agent_messaging_plugin.platform_surface import PlatformSurface  # noqa: E402

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


def _bridge(*, client_id: str, agent_instance_id: str) -> BridgeSessionState:
    # Real BridgeSessionState (only bridge_id/session_id are required; the rest
    # default) — no duck-typing, so the read paths under test are type-faithful.
    return BridgeSessionState(
        bridge_id="agc-test",
        session_id="sess-test",
        agent_instance_id=agent_instance_id,
        client_id=client_id,
    )


def _surface(check: Callable[[str], bool] | None) -> PlatformSurface:
    ps = PlatformSurface(
        action_factory=None,
        flow_manager=None,
        compilation_context_builder=None,
        bridge_manager=cast(BridgeSessionManager, None),
    )
    if check is not None:
        ps.set_operator_equivalent_check(check)
    return ps


class _FakeActionProcessor:
    """Minimal self for the REAL ActionProcessor._build_call_context.

    That method reads only ``_get_flow_trigger_data`` + the action's
    ``source_plugin``/``flow_id`` — no other ActionProcessor state.
    """

    def __init__(self, trigger_data: dict[str, object]) -> None:
        self._trigger_data = trigger_data

    def _get_flow_trigger_data(self, _flow_id: str) -> dict[str, object]:
        return self._trigger_data


_ACTION = SimpleNamespace(source_plugin=None, flow_id="flow-test")


def _resolve_context(trigger_data: dict[str, object]) -> CallContext:
    fake = _FakeActionProcessor(trigger_data)
    return ActionProcessor._build_call_context(
        cast(ActionProcessor, fake), cast(QueuedActionProtocol, _ACTION),
    )


def _test_resolver_degrade_safe() -> None:
    print("Case 1: _resolve_operator_equivalent is degrade-safe")
    oauth_bridge = _bridge(client_id="client-op", agent_instance_id="agi-oauth-client-op")
    _check(_surface(None)._resolve_operator_equivalent(oauth_bridge) is False,
           "1a: unset callback -> False (safe default)")
    _check(_surface(lambda cid: cid == "client-op")._resolve_operator_equivalent(oauth_bridge) is True,
           "1b: wired callback, matching client_id -> True")
    stdio_bridge = _bridge(client_id="", agent_instance_id="agi-real-stdio")
    _check(_surface(lambda _cid: True)._resolve_operator_equivalent(stdio_bridge) is False,
           "1c: empty client_id (stdio/no-auth) -> False even if callback would say True")

    def _throws(_cid: str) -> bool:
        raise RuntimeError("vault registry unavailable")

    _check(_surface(_throws)._resolve_operator_equivalent(oauth_bridge) is False,
           "1d: throwing callback -> degrades to False (never breaks dispatch)")


def _test_operator_equivalent_chain() -> None:
    print("\nCase 2: operator_equivalent client -> for_operator_equivalent (is_operator_like)")
    oauth_bridge = _bridge(client_id="client-op", agent_instance_id="agi-oauth-client-op")
    ps = _surface(lambda cid: cid == "client-op")
    is_op = ps._resolve_operator_equivalent(oauth_bridge)
    trigger = PlatformSurface._build_process_call_trigger_data(
        bridge=oauth_bridge, process_key="k", reason="r",
        inference_vertex_role="", operator_equivalent=is_op,
    )
    principal = trigger.get("authenticated_principal")
    _check(isinstance(principal, dict) and principal.get("operator_equivalent") is True,
           "2a: authenticated_principal stamped operator_equivalent=True")
    ctx = _resolve_context(trigger)
    _check(ctx.principal_kind == "operator_equivalent" and ctx.is_operator_principal,
           "2b: _build_call_context -> for_operator_equivalent (is_operator_principal True)")
    _check(ctx.principal_id == "client-op",
           "2c: principal_id carries the client_id (audit)")


def _test_default_client_is_external() -> None:
    print("\nCase 3: default (non-operator_equivalent) client -> for_external (non-operator)")
    oauth_bridge = _bridge(client_id="client-plain", agent_instance_id="agi-oauth-client-plain")
    ps = _surface(lambda cid: cid == "client-op")  # this client is NOT operator_equivalent
    is_op = ps._resolve_operator_equivalent(oauth_bridge)
    trigger = PlatformSurface._build_process_call_trigger_data(
        bridge=oauth_bridge, process_key="k", reason="r",
        inference_vertex_role="", operator_equivalent=is_op,
    )
    principal = trigger.get("authenticated_principal")
    _check(isinstance(principal, dict) and principal.get("operator_equivalent") is False,
           "3a: authenticated_principal stamped operator_equivalent=False")
    ctx = _resolve_context(trigger)
    _check(ctx.principal_kind == "external" and not ctx.is_operator_principal,
           "3b: _build_call_context -> for_external (NOT operator) — default preserved")


def _test_stdio_bridge_unaffected() -> None:
    print("\nCase 4: stdio bridge (empty client_id) carries NO authenticated_principal")
    stdio_bridge = _bridge(client_id="", agent_instance_id="agi-real-stdio")
    ps = _surface(lambda _cid: True)
    is_op = ps._resolve_operator_equivalent(stdio_bridge)
    trigger = PlatformSurface._build_process_call_trigger_data(
        bridge=stdio_bridge, process_key="k", reason="r",
        inference_vertex_role="", operator_equivalent=is_op,
    )
    _check("authenticated_principal" not in trigger,
           "4a: stdio bridge -> no authenticated_principal (operator-direct path unchanged)")
    ctx = _resolve_context(trigger)
    _check(ctx.principal_kind == "operator",
           "4b: _build_call_context -> for_operator (stdio operator-direct, unaffected)")


def main() -> int:
    print("operator_equivalent propagation smoke (B1 Finding-B)")
    _test_resolver_degrade_safe()
    _test_operator_equivalent_chain()
    _test_default_client_is_external()
    _test_stdio_bridge_unaffected()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
