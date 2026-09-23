#!/usr/bin/env python3
"""Surface 2 smoke — `source_namespace` canonical `.get` consumer pattern.

Background: 2026-06-17 G5.6 Surface 2 fix. The two
`_resolve_io_process_key` consumers of `FlowService.get_flow_input(...)` —
`ananta.core.plugins.plugin_base.PluginBase._resolve_io_process_key` and
`ananta.services.inference_service.inference_transaction._resolve_io_process_key`
— previously bracket-accessed the inner `source_namespace` key. That raised
an opaque `KeyError` when the consumed result lacked the key (cron-fired
flows that are terminal/headless do not carry an originating IO plugin
namespace; `get_flow_input`'s not-found and exception return shapes also
omit the key). The canonical pattern is `.get("source_namespace", "")` so
the downstream `if not source_namespace:` guard fires the canonical
`FrameworkError` / `RuntimeError` with the design-doc-pointed message
instead of an opaque KeyError before the guard runs.

This smoke positively asserts the canonical consumer pattern at both
sites and locks the contract in place against regression.

Cases:
  A. `inference_transaction._resolve_io_process_key` raises FrameworkError
     (NOT KeyError) when the flow_service result inner lacks
     `source_namespace` (cron-fired shape).
  B. PluginBase `_resolve_io_process_key` raises RuntimeError (NOT
     KeyError) on the same cron-fired shape.
  C. `inference_transaction._resolve_io_process_key` returns the correct
     `plugin::<ns>::post_message` key when `source_namespace` is present
     (positive-control happy path).
  D. PluginBase `_resolve_io_process_key` returns the correct
     `plugin::<ns>::post_message` key on the same happy-path shape.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run from repo root:
    .venv/bin/python3 ananta/src/ananta/services/inference_service/tests/source_namespace_canonical_get_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.constants import CONTEXT_KEY_FLOW_ID  # noqa: E402
from ananta.core.plugins.plugin_base import PluginBase  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.services.inference_service.inference_transaction import (  # noqa: E402
    _resolve_io_process_key as inference_resolve_io_process_key,
)


def _cron_shape_result() -> dict[str, Any]:
    """Shape returned by `FlowService.get_flow_input` for a cron-fired flow.

    Cron-fired flows are system-owned, terminal/headless: trigger_data has
    no originating IO plugin so `source_namespace` is absent from the
    inner result. Modelled on `flow_service/service.py:214-228` minus the
    `source_namespace` key.
    """
    return {
        "action_status": "completed",
        "data": {
            "result": {
                "original_input": "",
                "flow_id": "flow-cron-test",
                "kind": "system_owned_periodic_cron",
            }
        },
        "actions": [],
    }


def _happy_shape_result(source_namespace: str) -> dict[str, Any]:
    """Shape returned by `FlowService.get_flow_input` for an IO-originated flow."""
    return {
        "action_status": "completed",
        "data": {
            "result": {
                "original_input": "ping",
                "flow_id": "flow-happy-test",
                "source_namespace": source_namespace,
                "source": "test",
                "sender_name": "tester",
                "session_id": "sess-1",
                "kind": "",
            }
        },
        "actions": [],
    }


class _StubFlowService:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def get_flow_input(self, flow_id: str) -> dict[str, Any]:
        del flow_id  # unused — single-shot stub
        return self._payload


class _StubOrchestrator:
    def __init__(self, flow_service: _StubFlowService) -> None:
        self._flow_service = flow_service

    def get_service(self, name: str) -> _StubFlowService | None:
        if name == "flow_service":
            return self._flow_service
        return None


class _StubPluginBase(PluginBase):
    """Concrete PluginBase subclass exposing the protected resolver for the smoke."""

    def get_actions(self) -> list[Any]:
        return []

    def resolve(self, state: dict[str, object]) -> str:
        return self._resolve_io_process_key(state)


def _scenario_inference_cron_shape_raises_framework_error() -> None:
    print("A. inference_transaction cron shape -> FrameworkError (not KeyError)")
    orch = _StubOrchestrator(_StubFlowService(_cron_shape_result()))
    state = {CONTEXT_KEY_FLOW_ID: "flow-cron-test"}
    try:
        inference_resolve_io_process_key(orch, state)
    except FrameworkError as e:
        msg = str(e)
        assert "system_owned_periodic_cron" in msg or "source_namespace" in msg, (
            f"FrameworkError message lacks expected anchor: {msg!r}"
        )
        print(f"  OK  FrameworkError raised: {msg[:80]}...")
        return
    except KeyError as e:  # noqa: BLE001 — exact-class catch on purpose
        raise AssertionError(
            f"KeyError leaked from inference_transaction._resolve_io_process_key "
            f"on cron-shape; canonical .get(...) pattern not applied: {e!r}"
        ) from None
    raise AssertionError("expected FrameworkError on cron-shape, got no exception")


def _scenario_plugin_base_cron_shape_raises_runtime_error() -> None:
    print("B. PluginBase cron shape -> RuntimeError (not KeyError)")
    flow_service = _StubFlowService(_cron_shape_result())
    orch = _StubOrchestrator(flow_service)
    plugin = _StubPluginBase()
    plugin.orchestrator_ref = orch  # type: ignore[assignment]
    state: dict[str, object] = {CONTEXT_KEY_FLOW_ID: "flow-cron-test"}
    try:
        plugin.resolve(state)
    except RuntimeError as e:
        msg = str(e)
        assert "source_namespace" in msg, (
            f"RuntimeError message lacks 'source_namespace' anchor: {msg!r}"
        )
        print(f"  OK  RuntimeError raised: {msg[:80]}")
        return
    except KeyError as e:  # noqa: BLE001
        raise AssertionError(
            f"KeyError leaked from PluginBase._resolve_io_process_key on "
            f"cron-shape; canonical .get(...) pattern not applied: {e!r}"
        ) from None
    raise AssertionError("expected RuntimeError on cron-shape, got no exception")


def _scenario_inference_happy_path() -> None:
    print("C. inference_transaction happy path -> plugin::<ns>::post_message")
    orch = _StubOrchestrator(_StubFlowService(_happy_shape_result("agent_messaging_plugin")))
    state = {CONTEXT_KEY_FLOW_ID: "flow-happy-test"}
    key = inference_resolve_io_process_key(orch, state)
    assert key == "plugin::agent_messaging_plugin::post_message", (
        f"unexpected resolved key: {key!r}"
    )
    print(f"  OK  resolved: {key}")


def _scenario_plugin_base_happy_path() -> None:
    print("D. PluginBase happy path -> plugin::<ns>::post_message")
    orch = _StubOrchestrator(_StubFlowService(_happy_shape_result("agent_messaging_plugin")))
    plugin = _StubPluginBase()
    plugin.orchestrator_ref = orch  # type: ignore[assignment]
    state: dict[str, object] = {CONTEXT_KEY_FLOW_ID: "flow-happy-test"}
    key = plugin.resolve(state)
    assert key == "plugin::agent_messaging_plugin::post_message", (
        f"unexpected resolved key: {key!r}"
    )
    print(f"  OK  resolved: {key}")


def main() -> int:
    scenarios = [
        _scenario_inference_cron_shape_raises_framework_error,
        _scenario_plugin_base_cron_shape_raises_runtime_error,
        _scenario_inference_happy_path,
        _scenario_plugin_base_happy_path,
    ]
    for scenario in scenarios:
        try:
            scenario()
        except AssertionError as e:
            print(f"FAIL: {e}")
            return 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL (unexpected exception in scenario): {type(e).__name__}: {e}")
            return 1
    print("PASS: all 4 scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
