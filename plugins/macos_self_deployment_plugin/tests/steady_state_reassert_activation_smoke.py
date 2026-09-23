"""Steady-state activation re-assert smoke.

A plain platform restart can wedge the router at ``no_active_color``
forever: the new instance registers while the outgoing instance is still
active (the one-shot cold-start ``_ensure_active_color`` probe sees a live
color and correctly declines), then the router's heartbeat GC drops the
outgoing instance and clears the active binding — and, pre-fix, no later
path ever re-checked. The fix makes every HEALTHY steady-state heartbeat
tick re-run ``_ensure_active_color``, so the wedge heals within one
heartbeat interval while the same probe still refuses to steal from a live
color.

Two scenarios drive the REAL ``_run_steady_state_heartbeat`` loop:

1. **Wedge heals.** Heartbeat healthy, router status shows
   ``active_color=None`` → the loop activates self on its first tick.
2. **No stealing.** Heartbeat healthy, another color is active → the loop
   never calls activate, tick after tick.

Run directly:

    .venv/bin/python3 \\
        plugins/macos_self_deployment_plugin/tests/steady_state_reassert_activation_smoke.py
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from macos_self_deployment_plugin.heartbeat_lifecycle import (  # noqa: E402
    _run_steady_state_heartbeat,
)
from macos_self_deployment_plugin.router_client import RouterClient  # noqa: E402

_passed = 0
_failed: list[str] = []

_SELF_IID = "example-blue-29ce7ff6"
_JOIN_TIMEOUT_SECONDS = 5.0


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _WedgedRouter:
    """Healthy heartbeats, but nobody active — the Part 15 wedge."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop_event = stop_event
        self.activate_calls: list[tuple[str, str]] = []

    def heartbeat(self, _instance_id: str) -> dict[str, Any]:
        return {"alive": True}

    def status(self) -> dict[str, Any]:
        return {"active_color": None, "colors": []}

    def activate(self, color: str, instance_id: str) -> dict[str, Any]:
        self.activate_calls.append((color, instance_id))
        self._stop_event.set()
        return {"activated": True}


class _OtherColorActiveRouter:
    """Healthy heartbeats with green active — self (blue) must not steal."""

    def __init__(self, stop_event: threading.Event, *, ticks: int) -> None:
        self._stop_event = stop_event
        self._remaining = ticks
        self.activate_calls: list[tuple[str, str]] = []
        self.status_calls = 0

    def heartbeat(self, _instance_id: str) -> dict[str, Any]:
        return {"alive": True}

    def status(self) -> dict[str, Any]:
        self.status_calls += 1
        self._remaining -= 1
        if self._remaining <= 0:
            self._stop_event.set()
        return {"active_color": "green", "colors": []}

    def activate(self, color: str, instance_id: str) -> dict[str, Any]:
        self.activate_calls.append((color, instance_id))
        return {"activated": True}


def _run_loop(client: object, stop_event: threading.Event) -> None:
    logger = logging.getLogger("steady_state_reassert_smoke")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    thread = threading.Thread(
        target=lambda: _run_steady_state_heartbeat(
            client=cast("RouterClient", client),
            port=0,
            self_color="blue",
            self_instance_id=_SELF_IID,
            stop_event=stop_event,
            pending_finisher_file=None,
            current_release_lookup=None,
            logger=logger,
            set_color_active=lambda _active: None,
        ),
        name="reassert-smoke",
        daemon=True,
    )
    thread.start()
    thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
    _check(not thread.is_alive(), "loop exited within the join budget")


def test_healthy_tick_heals_no_active_color_wedge() -> None:
    print("scenario 1: healthy heartbeat + active_color=None -> self-activates")
    stop_event = threading.Event()
    client = _WedgedRouter(stop_event)
    _run_loop(client, stop_event)
    _check(
        client.activate_calls == [("blue", _SELF_IID)],
        f"activate called exactly once with self identity: {client.activate_calls}",
    )


def test_healthy_tick_never_steals_from_live_color() -> None:
    print("scenario 2: healthy heartbeat + green active -> blue never activates")
    stop_event = threading.Event()
    client = _OtherColorActiveRouter(stop_event, ticks=1)
    _run_loop(client, stop_event)
    _check(client.status_calls >= 1, "status probed on the healthy tick")
    _check(
        client.activate_calls == [],
        f"activate never called while another color is active: {client.activate_calls}",
    )


def main() -> int:
    print("=== macos_self_deployment_plugin steady-state re-assert (Part 15) ===")
    test_healthy_tick_heals_no_active_color_wedge()
    test_healthy_tick_never_steals_from_live_color()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
