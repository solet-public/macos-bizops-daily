"""Slice 4 smoke: drain sentinel held across SIGTERM + unregister.

Validates the crash-supervisor coordination invariant from the
bridge-port-routing-and-session-lifecycle design record (dev-checkout
workbench — not part of the shipped tree) §6 Slice 4 (Architect's
2026-06-06 verdict — Option A + single
cross-color sentinel):

* **Sentinel exists during the SIGTERM call.** The smoke patches
  ``MacosSelfDeploymentPlugin._signal_and_wait`` to record whether the
  sentinel file exists at the moment SIGTERM would be issued. This
  proves the ``drain_sentinel.held`` context manager wrote the file
  BEFORE the SIGTERM step ran — the launchd PathState predicate sees
  the sentinel in time to suppress respawn.

* **Sentinel removed after complete_swap returns.** The ``finally``
  branch of ``held`` guarantees cleanup even on success, so the
  LaunchAgent's respawn protection re-enables once the drain finishes.

* **Cleanup runs even on exception.** A separate scenario raises inside
  the ``with`` block and confirms the sentinel is still removed.

* **Path is single cross-color** (no ``-blue`` / ``-green`` suffix) —
  Architect's amendment to the original per-color dispatch framing.

The plugin's `_signal_and_wait` patch keeps the smoke from actually
killing any process; the real SIGTERM-on-real-pid flow is exercised by
``swap_round_trip_smoke.py``. This smoke focuses on sentinel
lifecycle correctness in isolation.

No ``pytest``; runs directly via
``.venv/bin/python3 plugins/macos_self_deployment_plugin/tests/slice4_drain_sentinel_smoke.py``
and exits 0 on success, 1 on any failure with stderr detail.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from macos_self_deployment_plugin import drain_sentinel, process_identity  # noqa: E402
from macos_self_deployment_plugin.pending_finisher import (  # noqa: E402
    PendingFinisher,
    pending_finisher_path,
    write_pending_finisher,
)
from macos_self_deployment_plugin.plugin import (  # noqa: E402
    MacosSelfDeploymentPlugin,
)


class _FakeUnregisterClient:
    """Minimal RouterClient stub for the unregister leg of complete_swap."""

    def __init__(self) -> None:
        self.unregister_calls: list[str] = []

    def unregister_color(self, instance_id: str) -> dict[str, Any]:
        self.unregister_calls.append(instance_id)
        return {"unregistered": True}


def _build_plugin() -> MacosSelfDeploymentPlugin:
    plugin = MacosSelfDeploymentPlugin()
    plugin._solet_name = "smoketest"
    plugin._self_color = "blue"
    plugin._self_instance_id = "smoketest-blue-test0001"
    plugin._router_client = _FakeUnregisterClient()  # type: ignore[assignment]
    return plugin


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"  OK  {message}")


def _scenario_sentinel_held_around_signal_and_unregister() -> None:
    print("Scenario 1: sentinel exists during SIGTERM + unregister, gone after")
    with tempfile.TemporaryDirectory(prefix="slice4_smoke_") as tmp:
        prior_xdg = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = tmp
        try:
            plugin = _build_plugin()
            sentinel = drain_sentinel.sentinel_path("smoketest")
            observations: dict[str, bool] = {}

            def fake_signal(pid: int, *, escalation: str) -> str:
                del pid, escalation
                observations["sentinel_during_signal"] = sentinel.exists()
                return "prior_terminated_cleanly"

            plugin._signal_and_wait = fake_signal  # type: ignore[method-assign]

            _expect(
                not sentinel.exists(),
                f"sentinel absent before complete_swap (path={sentinel})",
            )
            # complete_swap is record-driven (B2): seed the durable pending-finisher
            # record so it reaches the SIGTERM step (an absent record no-ops). Use
            # THIS live pid so the identity-token gate matches; ``_signal_and_wait``
            # is stubbed (fake_signal) so no real process is ever signalled.
            prior_pid = os.getpid()
            write_pending_finisher(
                pending_finisher_path(sentinel.parent, "smoketest"),
                PendingFinisher(
                    prior_pid=prior_pid, prior_instance_id="prior-blue-x",
                    prior_color="blue", candidate_release_id="rel-smoketest",
                    prior_start_token=process_identity.start_token(prior_pid),
                ),
            )
            result = plugin.complete_swap(
                prior_pid=prior_pid,
                prior_instance_id="prior-blue-x",
                prior_color="blue",
            )
            _expect(
                observations.get("sentinel_during_signal") is True,
                "sentinel existed at the moment SIGTERM step ran",
            )
            _expect(
                not sentinel.exists(),
                "sentinel removed after complete_swap returned",
            )
            _expect(
                result["status"] == "completed",
                f"complete_swap status='completed' (got {result['status']!r})",
            )
            unregister = plugin._router_client.unregister_calls  # type: ignore[attr-defined]
            _expect(
                unregister == ["prior-blue-x"],
                f"unregister ran after SIGTERM (calls={unregister!r})",
            )
        finally:
            if prior_xdg is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = prior_xdg


def _scenario_sentinel_removed_on_exception() -> None:
    print("Scenario 2: sentinel removed even when wrapped code raises")
    with tempfile.TemporaryDirectory(prefix="slice4_smoke_") as tmp:
        prior_xdg = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = tmp
        try:
            sentinel = drain_sentinel.sentinel_path("smoketest")
            try:
                with drain_sentinel.held("smoketest"):
                    _expect(sentinel.exists(), "sentinel present inside with-block")
                    msg = "synthetic drain failure"
                    raise RuntimeError(msg)
            except RuntimeError:
                pass
            _expect(
                not sentinel.exists(),
                "sentinel removed by finally even on RuntimeError",
            )
        finally:
            if prior_xdg is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = prior_xdg


def _scenario_sentinel_filename_is_cross_color() -> None:
    print("Scenario 3: sentinel filename matches single cross-color pattern")
    sentinel = drain_sentinel.sentinel_path("example")
    _expect(
        sentinel.name == "example.draining",
        f"sentinel basename is 'example.draining' (got {sentinel.name!r})",
    )
    _expect(
        "-blue" not in sentinel.name and "-green" not in sentinel.name,
        "sentinel filename carries NO color suffix (Architect's amendment)",
    )

    # Different solet names produce distinct sentinels — multi-solet
    # cohabitation on the same Mac doesn't collide.
    bayda_sentinel = drain_sentinel.sentinel_path("bayda")
    _expect(
        bayda_sentinel.name == "bayda.draining" and bayda_sentinel != sentinel,
        f"distinct solet -> distinct sentinel (example={sentinel.name}, "
        f"bayda={bayda_sentinel.name})",
    )


def main() -> int:
    print(
        "Slice 4 smoke: drain sentinel held across SIGTERM + unregister\n",
    )
    _scenario_sentinel_held_around_signal_and_unregister()
    print()
    _scenario_sentinel_removed_on_exception()
    print()
    _scenario_sentinel_filename_is_cross_color()
    print("\nAll scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
