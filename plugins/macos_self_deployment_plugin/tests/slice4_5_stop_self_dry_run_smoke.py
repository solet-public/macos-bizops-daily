"""Slice 4.5 smoke: macos stop_self dry_run path.

Pairs with ``slice4_5_stop_self_smoke.py``. Split into its own file so
the live + dry-run control flows live in separate functions and the
project's CC gate stays clean (mixing them stacked the branches over
threshold).

Validates:

* dry_run=True returns ``StopSelfStatus.DRY_RUN`` with the planning
  message.
* No sentinel is written.
* The watchdog spawner is NEVER invoked.

No ``pytest``; runs directly via
``.venv/bin/python3 plugins/macos_self_deployment_plugin/tests/slice4_5_stop_self_dry_run_smoke.py``
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


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"  OK  {message}")


def _scenario_dry_run_plans_without_mutating() -> None:
    print("Scenario 1: stop_self(dry_run=True) plans without writing/spawning")
    with tempfile.TemporaryDirectory(prefix="slice4_5_dryrun_smoke_") as tmp:
        prior_xdg = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = tmp
        try:
            plugin = MacosSelfDeploymentPlugin()
            plugin._solet_name = "smoketest"
            plugin._self_color = "blue"
            plugin._self_instance_id = "smoketest-blue-dryrun"
            sentinel = drain_sentinel.sentinel_path("smoketest")
            spawner_calls: list[int] = []

            def fake_spawner(target_pid: int) -> int:
                spawner_calls.append(target_pid)
                return 0

            plugin.set_watchdog_spawner_for_smoke(fake_spawner)

            result = plugin.stop_self(reason="dry-run-test", dry_run=True)

            _expect(
                result.status == StopSelfStatus.DRY_RUN,
                f"status=DRY_RUN (got {result.status})",
            )
            _expect(
                result.dry_run is True,
                f"dry_run=True (got {result.dry_run})",
            )
            _expect(
                result.reason == "dry-run-test",
                f"reason echoed (got {result.reason!r})",
            )
            _expect(
                not sentinel.exists(),
                f"sentinel NOT written on dry_run path (path={sentinel})",
            )
            _expect(
                spawner_calls == [],
                f"watchdog spawner NOT invoked on dry_run (calls={spawner_calls!r})",
            )
            _expect(
                result.backend_action_id == "",
                f"backend_action_id empty on dry_run (got {result.backend_action_id!r})",
            )
            _expect(
                result.duration_seconds == 0.0,
                f"duration_seconds=0.0 on dry_run (got {result.duration_seconds})",
            )
        finally:
            if prior_xdg is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = prior_xdg


def main() -> int:
    print("Slice 4.5 smoke: macos stop_self dry_run path\n")
    _scenario_dry_run_plans_without_mutating()
    print("\nAll scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
