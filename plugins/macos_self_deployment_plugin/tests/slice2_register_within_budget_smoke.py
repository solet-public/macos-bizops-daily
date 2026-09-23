"""Slice 2 + 2.5 smoke: unified transient-state budget + structured tokens.

Validates the spawn-path guarantee from the bridge-port-routing-and-
session-lifecycle design record (dev-checkout workbench — not part of
the shipped tree) §6 Slice 2 + Slice 2.5 (strict-I2 verdict):

* **Plugin-mediated bridge-port discovery.** ``_lookup_bridge_port`` walks
  ``orchestrator_ref.plugin_manager.plugins[agent_messaging_plugin]
  .bridge_port`` and returns the integer — proving the file-mediated
  detour through ``<name>-<color>.bridge.port`` is gone.

* **Successful registration within budget.** With a fake router that
  accepts ``register_color``, the heartbeat thread registers, transitions
  to steady-state heartbeat, and never invokes the self-SIGTERM helper.

* **Bind-wait expiry → SIGTERM with
  ``FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED`` (Slice 2.5).** When
  the cross-plugin port lookup never returns a port within the unified
  budget, the SIGTERM callback fires with the new structured token.
  Closes the strict-I2 gap where Slice 2 silently exited on bind-wait
  expiry rather than failing loud.

* **Register-budget expiry → SIGTERM with
  ``FAILED_REGISTRATION_BOUNDED_WINDOW_EXPIRED``.** When the port appears
  but the router rejects every register attempt, the SIGTERM callback
  fires with the original token before the unified budget elapses.

Smokes inject a short ``budget_seconds`` (1.5s) via
:meth:`MacosSelfDeploymentPlugin.set_budget_seconds_for_smoke` so
SIGTERM-path scenarios complete in ~2s instead of 30s. The SIGTERM
callback is patched via
:meth:`MacosSelfDeploymentPlugin.set_sigterm_callback_for_smoke` so the
test runner never actually receives SIGTERM.

No ``pytest``; runs directly via
``.venv/bin/python3 plugins/macos_self_deployment_plugin/tests/slice2_register_within_budget_smoke.py``
and exits 0 on success, 1 on any failure with stderr detail.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Plugin is editable-installed; deployment/ is a non-packaged source tree
# but this smoke does not need it (no router stand-up).
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from macos_self_deployment_plugin.constants import (  # noqa: E402
    AGENT_MESSAGING_PLUGIN_NAME,
    FAILED_REGISTRATION_BOUNDED_WINDOW_EXPIRED,
    FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED,
)
from macos_self_deployment_plugin.plugin import (  # noqa: E402
    MacosSelfDeploymentPlugin,
)
from macos_self_deployment_plugin.router_client import (  # noqa: E402
    RouterClientError,
)

# Smoke budget — long enough for register-success scenario to land
# one register call (~ a few hundred ms) and short enough that the
# SIGTERM-path scenarios complete in ~2s.
_SMOKE_BUDGET_SECONDS: float = 1.5
_SMOKE_SLACK_SECONDS: float = 2.0


class _FakeAcceptingClient:
    """RouterClient stub that accepts register + reports no active color.

    ``status`` returning ``active_color=None`` makes the heartbeat's
    ``_ensure_active_color`` call ``activate`` (also stubbed to succeed).
    """

    def __init__(self) -> None:
        self.register_calls: list[tuple[int, str, str]] = []
        self.heartbeat_calls: int = 0

    def register_color(
        self, port: int, color: str, instance_id: str,
        *, streamable_port: int | None = None,
    ) -> dict[str, Any]:
        del streamable_port
        self.register_calls.append((port, color, instance_id))
        return {"accepted": True}

    def status(self) -> dict[str, Any]:
        return {"active_color": None}

    def activate(self, color: str, instance_id: str) -> dict[str, Any]:
        del color, instance_id
        return {"activated": True}

    def heartbeat(self, instance_id: str) -> dict[str, Any]:
        del instance_id
        self.heartbeat_calls += 1
        return {"alive": True}


class _FakeFailingClient:
    """RouterClient stub that raises on every register attempt."""

    def __init__(self) -> None:
        self.register_attempts: int = 0

    def register_color(
        self, port: int, color: str, instance_id: str,
        *, streamable_port: int | None = None,
    ) -> dict[str, Any]:
        del port, color, instance_id, streamable_port
        self.register_attempts += 1
        raise RouterClientError("register_color", "router unavailable (smoke)")

    def status(self) -> dict[str, Any]:
        return {"active_color": None}

    def activate(self, color: str, instance_id: str) -> dict[str, Any]:
        del color, instance_id
        return {"activated": False}

    def heartbeat(self, instance_id: str) -> dict[str, Any]:
        del instance_id
        raise RouterClientError("heartbeat", "router unavailable (smoke)")


def _build_plugin(
    bridge_port: int | None,
) -> MacosSelfDeploymentPlugin:
    """Build a plugin wired with a fake orchestrator + agent-messaging-plugin.

    The fake agent-messaging-plugin exposes ``bridge_port`` as a plain
    attribute (``hasattr`` succeeds, ``getattr`` returns the int).
    ``bridge_port=None`` simulates the pre-``start_interface`` window;
    in scenario 4 the value is held at None for the entire run to model
    "start_interface never fires."
    """
    plugin = MacosSelfDeploymentPlugin()
    plugin._solet_name = "example"
    plugin._self_color = "blue"
    plugin._self_instance_id = "example-blue-smoke0001"
    fake_messaging = SimpleNamespace(bridge_port=bridge_port)
    plugin.orchestrator_ref = SimpleNamespace(  # type: ignore[assignment]
        plugin_manager=SimpleNamespace(
            plugins={AGENT_MESSAGING_PLUGIN_NAME: fake_messaging},
        ),
    )
    return plugin


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"  OK  {message}")


def _scenario_lookup_returns_bridge_port() -> None:
    print("Scenario 1: _lookup_bridge_port returns agent_messaging_plugin.bridge_port")
    plugin = _build_plugin(bridge_port=12345)
    _expect(
        plugin._lookup_bridge_port() == 12345,  # noqa: PLR2004
        "lookup returns the bound port held in-process",
    )

    plugin_none = _build_plugin(bridge_port=None)
    _expect(
        plugin_none._lookup_bridge_port() is None,
        "lookup returns None when bridge_port is unset (pre-start_interface)",
    )

    plugin_no_orch = MacosSelfDeploymentPlugin()
    _expect(
        plugin_no_orch._lookup_bridge_port() is None,
        "lookup returns None when orchestrator_ref is missing",
    )


def _scenario_successful_register_within_budget() -> None:
    print("Scenario 2: accepting router → register succeeds, no SIGTERM")
    plugin = _build_plugin(bridge_port=8123)
    client = _FakeAcceptingClient()
    plugin._router_client = client  # type: ignore[assignment]

    sigterm_event = threading.Event()
    captured_tokens: list[str] = []

    def fake_sigterm(token: str) -> None:
        captured_tokens.append(token)
        sigterm_event.set()

    plugin.set_sigterm_callback_for_smoke(fake_sigterm)
    plugin.set_budget_seconds_for_smoke(_SMOKE_BUDGET_SECONDS)

    plugin._spawn_heartbeat_thread()
    # Wait long enough for register + at least one heartbeat tick attempt.
    time.sleep(min(1.0, _SMOKE_BUDGET_SECONDS))
    plugin._heartbeat_stop.set()
    if plugin._heartbeat_thread is not None:
        plugin._heartbeat_thread.join(timeout=5.0)

    _expect(
        len(client.register_calls) >= 1,
        f"register_color was invoked (calls={len(client.register_calls)})",
    )
    _expect(
        client.register_calls[0] == (8123, "blue", "example-blue-smoke0001"),
        "register_color received the plugin-looked-up bound port + (color, id)",
    )
    _expect(
        not sigterm_event.is_set(),
        "SIGTERM callback NOT invoked on the success path",
    )
    _expect(
        captured_tokens == [],
        f"no structured token threaded (captured={captured_tokens!r})",
    )


def _scenario_register_budget_expiry() -> None:
    print(
        "Scenario 3: port available + failing router → "
        f"FAILED_REGISTRATION_BOUNDED_WINDOW_EXPIRED within "
        f"{_SMOKE_BUDGET_SECONDS}s + {_SMOKE_SLACK_SECONDS}s slack",
    )
    plugin = _build_plugin(bridge_port=8123)
    client = _FakeFailingClient()
    plugin._router_client = client  # type: ignore[assignment]

    sigterm_event = threading.Event()
    captured_tokens: list[str] = []

    def fake_sigterm(token: str) -> None:
        captured_tokens.append(token)
        sigterm_event.set()

    plugin.set_sigterm_callback_for_smoke(fake_sigterm)
    plugin.set_budget_seconds_for_smoke(_SMOKE_BUDGET_SECONDS)

    plugin._spawn_heartbeat_thread()
    fired = sigterm_event.wait(timeout=_SMOKE_BUDGET_SECONDS + _SMOKE_SLACK_SECONDS)
    plugin._heartbeat_stop.set()
    if plugin._heartbeat_thread is not None:
        plugin._heartbeat_thread.join(timeout=5.0)

    _expect(
        fired,
        f"SIGTERM callback fired within "
        f"{_SMOKE_BUDGET_SECONDS + _SMOKE_SLACK_SECONDS}s",
    )
    _expect(
        client.register_attempts >= 2,  # noqa: PLR2004
        f"router retried before SIGTERM (attempts={client.register_attempts})",
    )
    _expect(
        captured_tokens == [FAILED_REGISTRATION_BOUNDED_WINDOW_EXPIRED],
        f"token {FAILED_REGISTRATION_BOUNDED_WINDOW_EXPIRED!r} fired exactly once "
        f"(captured={captured_tokens!r})",
    )


def _scenario_bind_wait_expiry() -> None:
    print(
        "Scenario 4: port never appears → "
        f"FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED within "
        f"{_SMOKE_BUDGET_SECONDS}s + {_SMOKE_SLACK_SECONDS}s slack",
    )
    # bridge_port stays None for the entire run — models the case where
    # agent_messaging_plugin.start_interface never fires (or fires after
    # the budget). Strict-I2 verdict: this MUST SIGTERM, not silent-exit.
    plugin = _build_plugin(bridge_port=None)
    client = _FakeAcceptingClient()  # router is fine; never reached
    plugin._router_client = client  # type: ignore[assignment]

    sigterm_event = threading.Event()
    captured_tokens: list[str] = []

    def fake_sigterm(token: str) -> None:
        captured_tokens.append(token)
        sigterm_event.set()

    plugin.set_sigterm_callback_for_smoke(fake_sigterm)
    plugin.set_budget_seconds_for_smoke(_SMOKE_BUDGET_SECONDS)

    plugin._spawn_heartbeat_thread()
    fired = sigterm_event.wait(timeout=_SMOKE_BUDGET_SECONDS + _SMOKE_SLACK_SECONDS)
    plugin._heartbeat_stop.set()
    if plugin._heartbeat_thread is not None:
        plugin._heartbeat_thread.join(timeout=5.0)

    _expect(
        fired,
        f"SIGTERM callback fired within "
        f"{_SMOKE_BUDGET_SECONDS + _SMOKE_SLACK_SECONDS}s",
    )
    _expect(
        len(client.register_calls) == 0,
        "register_color was NEVER attempted (port never appeared)",
    )
    _expect(
        captured_tokens == [FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED],
        f"token {FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED!r} fired exactly once "
        f"(captured={captured_tokens!r})",
    )


def main() -> int:
    print(
        "Slice 2 + 2.5 smoke: unified transient-state budget + structured tokens\n",
    )
    _scenario_lookup_returns_bridge_port()
    print()
    _scenario_successful_register_within_budget()
    print()
    _scenario_register_budget_expiry()
    print()
    _scenario_bind_wait_expiry()
    print("\nAll scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
