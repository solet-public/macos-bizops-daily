"""Free-function ``stop_self`` body for macos_self_deployment_plugin.

Slice 4.5 of
``workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md``.
The plugin class would push over the god-class non-process-LOC
threshold if it carried this body inline; the helper lives at module
scope and the plugin's ``stop_self`` method is a thin delegator.

See the ABC docstring at
:meth:`ananta.interfaces.self_deployment_service_interface.SelfDeploymentServiceInterface.stop_self`
for the full contract; this module just packages the macOS execution.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from ananta.interfaces.lifecycle_result_types import (
    StopSelfResult,
    StopSelfStatus,
)

from macos_self_deployment_plugin import drain_sentinel, stop_self_watchdog


def run(
    *,
    solet_name: str,
    reason: str,
    dry_run: bool,
    watchdog_spawner: stop_self_watchdog.WatchdogSpawner,
) -> StopSelfResult:
    """Write the drain sentinel + spawn the detached SIGTERM watchdog.

    Idempotent: returns ``StopSelfStatus.ALREADY_STOPPED`` when the
    sentinel is already on disk so a re-invocation by an operator who
    forgot to run ``./launch.py`` doesn't spawn a redundant watchdog
    that would SIGTERM whatever process happens to inherit our pid
    later.
    """
    timestamp = datetime.now(UTC).isoformat()
    sentinel = drain_sentinel.sentinel_path(solet_name)
    if dry_run:
        return StopSelfResult(
            status=StopSelfStatus.DRY_RUN,
            reason=reason,
            duration_seconds=0.0,
            stopped_at="",
            backend_action_id="",
            dry_run=True,
            message=(
                f"dry_run=True; would write {sentinel}, spawn detached "
                f"watchdog SIGTERM-ing pid {os.getpid()}"
            ),
        )
    if sentinel.exists():
        return StopSelfResult(
            status=StopSelfStatus.ALREADY_STOPPED,
            reason=reason,
            duration_seconds=0.0,
            stopped_at=timestamp,
            backend_action_id="",
            dry_run=False,
            message=(
                f"drain sentinel already at {sentinel}; "
                "no-op idempotent return"
            ),
        )
    drain_sentinel.write(solet_name)
    watchdog_pid = watchdog_spawner(os.getpid())
    return StopSelfResult(
        status=StopSelfStatus.SUCCESS,
        reason=reason,
        duration_seconds=0.0,
        stopped_at=timestamp,
        backend_action_id=str(watchdog_pid),
        dry_run=False,
        message=(
            f"sentinel at {sentinel}; detached watchdog pid {watchdog_pid} "
            f"will SIGTERM this process shortly"
        ),
    )
