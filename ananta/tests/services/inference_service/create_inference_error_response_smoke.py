#!/usr/bin/env python3
"""Smoke: P1 Cycle A — ``create_inference_error_response`` IO-injection branches.

Per the v3 coding-agent inference interface design
(``workbench/2026-06-13_coding_agent_inference_interface_design_v3.md``)
Cycle A pins the existing semantic of the post_message injection inside
``create_inference_error_response`` BEFORE the Step 2 decoupling and
Cycle B / Cycle C vertex routing lands. The function is reached from
two production callers:

  1. ``ananta/src/ananta/services/inference_service/inference_transaction.py``
     line 101 — platform-owned wrapper path used when the binding routes
     inference through the typed ``InferenceProvider`` protocol. The
     wrapper resolves the IO process key via ``_resolve_io_process_key``
     and threads it as ``io_process_key``.
  2. ``plugins/default_inference_plugin/src/default_inference_plugin/plugin.py``
     line 1772 — legacy in-plugin path. The plugin resolves the IO process
     key via ``self._resolve_active_io_process_key(state)`` and threads
     it the same way.

Both callers pass the resolved key (or ``None``) verbatim into
``create_inference_error_response``. Smoking the FUNCTION at both
``io_process_key`` values therefore exercises the conditional injection
both callers depend on.

Two subsmokes:

  (a) IO-origin flow — ``io_process_key`` is a real process key.
      Expectation: the returned ``ActionResult.actions`` contains exactly
      one ``post_message`` action whose ``process_key`` matches the
      caller-supplied key, and whose ``arguments.message`` carries the
      error wording.

  (b) Non-IO-origin flow — ``io_process_key`` is ``None``.
      Expectation: the returned ``ActionResult.actions`` is the empty
      list, and no ``post_message`` action is emitted. This is the
      no-recipient branch that the action-queue-poller's
      ``_flow_has_no_vertex_binding`` predicate (renamed in this same
      cycle) is the gate for at the routing layer.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run with:

    .venv/bin/python3 ananta/tests/services/inference_service/create_inference_error_response_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.plugins.plugin_contracts import ActionStatus  # noqa: E402
from ananta.services.inference_service.transaction import (  # noqa: E402
    create_inference_error_response,
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


def _make_state() -> dict[str, Any]:
    return {
        "session_id": "sess-smoke-create-inference-error",
        "flow_id": "flow-smoke-create-inference-error",
    }


def _case_io_origin_injects_post_message() -> None:
    print(
        "\nCase (a): IO-origin flow — io_process_key present, "
        "post_message injected",
    )
    state = _make_state()
    io_process_key = "plugin::agent_messaging_plugin::post_message"

    result = create_inference_error_response(
        RuntimeError("upstream provider exploded"),
        "process_results",
        state,
        io_process_key,
    )

    _check(
        result.get("action_status") == ActionStatus.ERROR.value,
        "ActionResult['action_status'] == 'error'",
    )
    actions = result.get("actions") or []
    _check(
        len(actions) == 1,
        "ActionResult['actions'] has exactly 1 entry",
    )

    if not actions:
        return
    action = actions[0]

    _check(
        action.get("name") == "post_message",
        "Injected action.name == 'post_message'",
    )
    _check(
        action.get("process_key") == io_process_key,
        f"Injected action.process_key == {io_process_key!r}",
    )

    arguments = action.get("arguments")
    _check(
        isinstance(arguments, dict),
        "Injected action.arguments is a dict",
    )
    if isinstance(arguments, dict):
        message = arguments.get("message", "")
        _check(
            isinstance(message, str) and "upstream provider exploded" in message,
            "Injected action.arguments.message carries the underlying error wording",
        )

    _check(
        action.get("session_id") == state["session_id"],
        "Injected action.session_id matches state.session_id",
    )
    _check(
        action.get("flow_id") == state["flow_id"],
        "Injected action.flow_id matches state.flow_id",
    )


def _case_non_io_origin_skips_post_message() -> None:
    print(
        "\nCase (b): non-IO-origin flow — io_process_key is None, "
        "no post_message injection",
    )
    state = _make_state()

    result = create_inference_error_response(
        RuntimeError("upstream provider exploded"),
        "process_error",
        state,
        None,
    )

    _check(
        result.get("action_status") == ActionStatus.ERROR.value,
        "ActionResult['action_status'] == 'error' even with no IO recipient",
    )
    actions = result.get("actions") or []
    _check(
        len(actions) == 0,
        "ActionResult['actions'] is an empty list (no post_message injected)",
    )

    # The error payload is still present; only the IO-side post_message
    # is suppressed. Callers (action queue poller, agent_messaging
    # bridge consumer) read ``error`` directly when no actions exist.
    _check(
        isinstance(result.get("error"), dict),
        "ActionResult['error'] remains a populated dict for upstream inspection",
    )


def main() -> int:
    print("Smoke: create_inference_error_response IO-injection branches")
    _case_io_origin_injects_post_message()
    _case_non_io_origin_skips_post_message()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
