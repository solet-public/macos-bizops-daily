"""Cold-start auto-activate smoke for macos_self_deployment_plugin.

Verifies the gap closure committed in this slice: on first boot, the
heartbeat loop's ``_ensure_active_color`` helper detects that the router
has no active color and calls ``activate`` against self. On warm boot
(an active color already exists), the helper is a no-op and never
displaces the live color.

Three test paths cover the contract:

1. **Cold start, no active color.** Stub ``RouterClient`` so ``status()``
   returns ``{"active_color": None}`` and ``activate`` returns
   ``{"activated": True}``. Assert ``_ensure_active_color`` calls
   ``activate`` exactly once with ``(self_color, self_instance_id)`` and
   returns True.

2. **Warm boot, blue already active.** Stub ``status()`` to return
   ``{"active_color": "blue", ...}``. Assert ``activate`` is never
   called and the helper returns True (no-op).

3. **Activate refused.** Stub ``status()`` to return ``active_color=None``
   and ``activate`` to return ``{"activated": False, "reason": "..."}``.
   Assert ``_ensure_active_color`` returns False and logs the refusal.

No ``pytest``. Run directly via:

    .venv/bin/python3 \\
        plugins/macos_self_deployment_plugin/tests/cold_start_auto_activate_smoke.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from macos_self_deployment_plugin.heartbeat_lifecycle import (  # noqa: E402
    _ensure_active_color,
)
from macos_self_deployment_plugin.plugin import (  # noqa: E402
    MacosSelfDeploymentPlugin,
)
from macos_self_deployment_plugin.router_client import (  # noqa: E402
    RouterClient,
    RouterClientError,
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


class _StubRouterClient(RouterClient):
    """Inherits the real interface; overrides only the four verbs touched.

    Counts calls so the smoke can assert exact-call-count semantics
    rather than "at least once."
    """

    def __init__(
        self,
        status_response: dict[str, Any] | Exception,
        activate_response: dict[str, Any] | Exception,
    ) -> None:
        # socket_path is never used — every RouterClient verb is overridden
        # below. A non-/tmp sentinel keeps the smoke off /tmp per the operator
        # no-/tmp rule (scratch lives under ~/.ananta/).
        super().__init__(
            socket_path=Path.home() / ".ananta" / "_smoke_scratch" / "never-used.sock"
        )
        self._status_response = status_response
        self._activate_response = activate_response
        self.status_call_count = 0
        self.activate_call_count = 0
        self.activate_args: tuple[str, str] | None = None

    def status(self) -> dict[str, Any]:
        self.status_call_count += 1
        if isinstance(self._status_response, Exception):
            raise self._status_response
        return self._status_response

    def activate(self, color: str, instance_id: str) -> dict[str, Any]:
        self.activate_call_count += 1
        self.activate_args = (color, instance_id)
        if isinstance(self._activate_response, Exception):
            raise self._activate_response
        return self._activate_response


def _make_plugin(color: str = "blue", instance_id: str = "example-blue-test") -> (
    MacosSelfDeploymentPlugin
):
    """Build a plugin instance with the minimum state the helper reads.

    The plugin's full lifecycle (start_services / prepare_for_readiness)
    sets up a lot of machinery (action factory, orchestrator, heartbeat
    thread) the helper doesn't use. Setting the two private attributes
    directly keeps the smoke focused.
    """
    plugin = MacosSelfDeploymentPlugin()
    plugin._self_color = color  # noqa: SLF001
    plugin._self_instance_id = instance_id  # noqa: SLF001
    return plugin


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_cold_start_activates_self() -> None:
    plugin = _make_plugin(color="blue", instance_id="example-blue-cold")
    client = _StubRouterClient(
        status_response={"active_color": None, "colors": []},
        activate_response={"activated": True},
    )
    result = _ensure_active_color(
        client=client,
        self_color=plugin._self_color,  # noqa: SLF001
        self_instance_id=plugin._self_instance_id,  # noqa: SLF001
        logger=plugin.logger,
    )
    _check(result is True, "cold start helper returns True")
    _check(client.status_call_count == 1, "status() called exactly once")
    _check(client.activate_call_count == 1, "activate() called exactly once")
    _check(
        client.activate_args == ("blue", "example-blue-cold"),
        "activate args = (self_color, self_instance_id)",
    )


def test_warm_start_skips_activate() -> None:
    plugin = _make_plugin(color="green", instance_id="example-green-warm")
    client = _StubRouterClient(
        status_response={"active_color": "blue", "colors": []},
        activate_response={"activated": True},
    )
    result = _ensure_active_color(
        client=client,
        self_color=plugin._self_color,  # noqa: SLF001
        self_instance_id=plugin._self_instance_id,  # noqa: SLF001
        logger=plugin.logger,
    )
    _check(result is True, "warm start helper returns True (no-op)")
    _check(client.status_call_count == 1, "status() called exactly once")
    _check(
        client.activate_call_count == 0,
        "activate() never called when a color is already active",
    )


def test_activate_refusal_returns_false() -> None:
    plugin = _make_plugin(color="blue", instance_id="example-blue-refused")
    client = _StubRouterClient(
        status_response={"active_color": None, "colors": []},
        activate_response={"activated": False, "reason": "instance_not_registered"},
    )
    handler = logging.Handler()
    handler.records = []  # type: ignore[attr-defined]
    handler.emit = lambda record: handler.records.append(record)  # type: ignore[attr-defined,method-assign]
    plugin.logger.addHandler(handler)
    try:
        result = _ensure_active_color(
        client=client,
        self_color=plugin._self_color,  # noqa: SLF001
        self_instance_id=plugin._self_instance_id,  # noqa: SLF001
        logger=plugin.logger,
    )
    finally:
        plugin.logger.removeHandler(handler)
    _check(result is False, "activate refusal returns False")
    _check(
        any("refused auto-activate" in r.getMessage() for r in handler.records),  # type: ignore[attr-defined]
        "refusal logged with router-refused warning",
    )


def test_status_failure_returns_false() -> None:
    plugin = _make_plugin(color="blue", instance_id="example-blue-status-fail")
    client = _StubRouterClient(
        status_response=RouterClientError("status", "connect refused"),
        activate_response={"activated": True},
    )
    result = _ensure_active_color(
        client=client,
        self_color=plugin._self_color,  # noqa: SLF001
        self_instance_id=plugin._self_instance_id,  # noqa: SLF001
        logger=plugin.logger,
    )
    _check(result is False, "status RPC failure returns False")
    _check(
        client.activate_call_count == 0,
        "activate never attempted when status fails",
    )


def test_activate_rpc_error_returns_false() -> None:
    plugin = _make_plugin(color="blue", instance_id="example-blue-activate-fail")
    client = _StubRouterClient(
        status_response={"active_color": None, "colors": []},
        activate_response=RouterClientError("activate", "broken pipe"),
    )
    result = _ensure_active_color(
        client=client,
        self_color=plugin._self_color,  # noqa: SLF001
        self_instance_id=plugin._self_instance_id,  # noqa: SLF001
        logger=plugin.logger,
    )
    _check(result is False, "activate RPC failure returns False")
    _check(client.activate_call_count == 1, "activate attempted exactly once")


# ─── Driver ─────────────────────────────────────────────────────────────────


def main() -> int:
    print("=== macos_self_deployment_plugin cold-start auto-activate ===")
    test_cold_start_activates_self()
    test_warm_start_skips_activate()
    test_activate_refusal_returns_false()
    test_status_failure_returns_false()
    test_activate_rpc_error_returns_false()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
