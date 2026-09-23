"""Slice 4.5 smoke: macos stop_self drain sentinel + detached watchdog.

Validates the contract from the bridge-port-routing-and-session-
lifecycle design record (dev-checkout workbench — not part of the
shipped tree) §6 Slice 4.5 (operator-scoped stop_self verb on
SelfDeploymentServiceInterface, paired with the cloud sibling):

* **Live path:** writes the cross-color drain sentinel at
  ``~/.ananta/runtime/<solet>.draining`` and invokes the watchdog
  spawner with the current pid. The spawner is patched via
  ``set_watchdog_spawner_for_smoke`` so the smoke records the call
  without actually killing the test runner. The sentinel PERSISTS
  beyond the verb's return (distinct from drain_sentinel.held() in
  Slice 4 which auto-cleans on context exit).

* **Idempotent ALREADY_STOPPED:** a second stop_self call against the
  same solet name finds the sentinel already on disk and returns
  ALREADY_STOPPED without re-invoking the spawner.

* **Return envelope:** StopSelfResult fields populated as the dispatch
  spec describes — status, reason echo, backend_action_id carrying the
  watchdog pid as a string, stopped_at non-empty.

Pairs with ``slice4_5_stop_self_dry_run_smoke.py`` which covers the
dry-run path separately to keep both functions A/B per the project
coherence gates.

No ``pytest``; runs directly via
``.venv/bin/python3 plugins/macos_self_deployment_plugin/tests/slice4_5_stop_self_smoke.py``
and exits 0 on success, 1 on any failure with stderr detail.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ananta.interfaces.lifecycle_result_types import StopSelfStatus  # noqa: E402
from macos_self_deployment_plugin import drain_sentinel  # noqa: E402
from macos_self_deployment_plugin.plugin import (  # noqa: E402
    MacosSelfDeploymentPlugin,
)


def _build_plugin() -> MacosSelfDeploymentPlugin:
    plugin = MacosSelfDeploymentPlugin()
    plugin._solet_name = "smoketest"
    plugin._self_color = "blue"
    plugin._self_instance_id = "smoketest-blue-test0001"
    return plugin


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"  OK  {message}")


def _scenario_live_path_writes_sentinel_and_spawns_watchdog() -> None:
    print(
        "Scenario 1: stop_self writes sentinel (persists) + invokes watchdog spawner",
    )
    with tempfile.TemporaryDirectory(prefix="slice4_5_smoke_") as tmp:
        prior_xdg = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = tmp
        try:
            plugin = _build_plugin()
            sentinel = drain_sentinel.sentinel_path("smoketest")
            recorded_calls: list[int] = []

            def fake_spawner(target_pid: int) -> int:
                recorded_calls.append(target_pid)
                return 99999  # synthetic watchdog pid

            plugin.set_watchdog_spawner_for_smoke(fake_spawner)

            _expect(
                not sentinel.exists(),
                f"sentinel absent before stop_self (path={sentinel})",
            )
            result = plugin.stop_self(reason="smoke-test-stop")

            _expect(
                sentinel.exists(),
                "sentinel persists after stop_self returns (no auto-cleanup)",
            )
            _expect(
                recorded_calls == [os.getpid()],
                f"watchdog spawner invoked with current pid (calls={recorded_calls!r})",
            )
            _expect(
                result.status == StopSelfStatus.SUCCESS,
                f"status=SUCCESS (got {result.status})",
            )
            _expect(
                result.reason == "smoke-test-stop",
                f"reason echoed (got {result.reason!r})",
            )
            _expect(
                result.backend_action_id == "99999",
                f"backend_action_id carries watchdog pid as str (got {result.backend_action_id!r})",
            )
            _expect(
                result.stopped_at != "",
                "stopped_at populated (non-empty ISO timestamp)",
            )
            _expect(
                not result.dry_run,
                f"dry_run=False (got {result.dry_run})",
            )
        finally:
            if prior_xdg is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = prior_xdg


def _scenario_idempotent_already_stopped() -> None:
    print(
        "Scenario 2: stop_self called twice -> second call returns ALREADY_STOPPED",
    )
    with tempfile.TemporaryDirectory(prefix="slice4_5_smoke_") as tmp:
        prior_xdg = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = tmp
        try:
            plugin = _build_plugin()
            recorded_calls: list[int] = []
            plugin.set_watchdog_spawner_for_smoke(
                lambda target_pid: (recorded_calls.append(target_pid), 88888)[1],
            )

            first = plugin.stop_self(reason="first")
            _expect(
                first.status == StopSelfStatus.SUCCESS,
                f"first stop_self returns SUCCESS (got {first.status})",
            )
            _expect(
                len(recorded_calls) == 1,
                f"watchdog spawned exactly once (calls={len(recorded_calls)})",
            )

            second = plugin.stop_self(reason="second-idempotent")
            _expect(
                second.status == StopSelfStatus.ALREADY_STOPPED,
                f"second stop_self returns ALREADY_STOPPED (got {second.status})",
            )
            _expect(
                second.reason == "second-idempotent",
                "second call's reason echoed verbatim",
            )
            _expect(
                len(recorded_calls) == 1,
                f"watchdog NOT re-spawned on idempotent re-invocation "
                f"(calls={len(recorded_calls)})",
            )
        finally:
            if prior_xdg is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = prior_xdg


def main() -> int:
    print("Slice 4.5 smoke: macos stop_self drain sentinel + detached watchdog\n")
    _scenario_live_path_writes_sentinel_and_spawns_watchdog()
    print()
    _scenario_idempotent_already_stopped()
    print("\nAll scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
