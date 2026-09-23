"""In-process liveness signal for the action dispatch path (D5).

During the 2026-08-15 outage ``solet health`` reported ``healthy`` for the
entire 3h20m freeze, and every observer on the fleet was misled by it. (That
history is about the observers, not about deploy gating: this checkout's
blue-green swap paths — local router and AWS ALB/ECS alike — never consult
this endpoint's body during warmup, only the unrelated ``readiness_probe``
closure's 200-vs-503 split; see the GAU-10 investigation notes (dev-checkout
workbench record, not part of the shipped tree). Do not read the paragraph
below as a description of current deploy wiring.)

Lane AA established the subtlety that decides this module's design: **health
was not lying.** It was truthfully measuring a genuinely-alive half of a
half-dead process. A worker thread held the GIL inside the C JSON parser, so
everything under ``ananta.core`` starved while the ``agent_messaging_plugin``
bridge surfaces kept answering normally. The fix is therefore to measure the
half that DIED, not to distrust the probe.

## Why this is in-process and touches no database

The obvious implementation — have the health endpoint query the newest
completed action — is wrong, and dangerously so. That query needs the GIL to
run, so under exactly the condition it exists to detect it would not return an
unhealthy answer; it would HANG. A silent green is bad, but a probe that hangs
takes the load balancer's target group down with it and turns a degraded
platform into an unreachable one. So this module holds plain attributes, is
written only by the poller, read only by the probe, takes no lock, performs no
I/O, and cannot block.

Because the poller and the bridge's uvicorn share one process, a poller that is
starved of the GIL simply stops updating these values while the probe keeps
serving — the age grows, which is precisely the signal that was missing.

## The alarm is STALE POLL AGE ALONE (GAU-10, 2026-08-18)

``poll_age_seconds`` on its own is the whole signal. It does not false-alarm
on an idle platform because :meth:`ActionPathLiveness.record_poll_cycle` is
called at the end of **every** poll cycle, including empty ones (see its own
docstring and ``ActionQueuePoller._poll_once``) — an idle-but-alive poller
keeps age fresh forever, so ``age > threshold`` already means "the poller
stopped completing cycles," not "the platform is quiet."

This module used to gate the alarm on ``queued_depth > 0`` as well, reasoning
that depth alone false-greens on a freeze that begins at an idle moment. That
reasoning is true but pointed the fix the wrong way: ``last_observed_queue_depth``
is written only by the same poller whose death the alarm exists to detect, so a
freeze that begins at depth 0 pinned depth at 0 forever and the conjunction
could **never** fire — exactly the failure GAU-10 measured live (stale age with
an empty queue, reported not-stalled). Depth is still published in
:meth:`ActionPathLiveness.snapshot` as corroborating context, but it no longer
gates the verdict.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

# A poll cycle that has not completed for this long means the dispatch path
# is not running. Generous relative to the default 1 s poll interval: the
# point is to catch a total freeze in minutes rather than hours, not to flag
# a slow cycle.
DEFAULT_STALL_THRESHOLD_SECONDS = 120.0


@dataclass
class ActionPathLiveness:
    """Mutable liveness counters for one process's action dispatch path.

    Written only by :class:`ActionQueuePoller`; read only by health probes.
    Plain attribute assignment under the GIL is sufficient — no lock is taken
    anywhere in this class, deliberately, so a reader can never block behind a
    writer that is itself starved.
    """

    #: ``time.monotonic()`` at the end of the last completed poll cycle.
    #: Monotonic rather than wall-clock so a clock adjustment cannot fabricate
    #: or mask a stall. ``None`` until the first cycle completes.
    last_poll_monotonic: float | None = None

    #: Wall-clock UTC of the last completed poll cycle, for human readers.
    last_poll_at_utc: str | None = None

    #: Rows observed ``queued`` on the last cycle — the poller already computes
    #: this to do its own work, so publishing it costs nothing.
    last_observed_queue_depth: int = 0

    #: Actions dispatched since process start. A monotonically increasing
    #: counter distinguishes "quiet" from "never worked" on a fresh boot.
    total_dispatched: int = 0

    #: ``time.monotonic()`` at construction, so age is well-defined before the
    #: first poll completes rather than being unknowable.
    started_monotonic: float = field(default_factory=time.monotonic)

    def record_poll_cycle(self, *, queue_depth: int, dispatched: int) -> None:
        """Stamp the completion of one poll cycle.

        Called at the END of every cycle INCLUDING an empty one. An idle
        platform must keep its poll age fresh — if only non-empty cycles
        stamped it, a quiet night would be indistinguishable from a freeze and
        this module would reproduce the ambiguity it exists to remove.
        """
        self.last_poll_monotonic = time.monotonic()
        self.last_poll_at_utc = datetime.now(UTC).isoformat()
        self.last_observed_queue_depth = queue_depth
        self.total_dispatched += dispatched

    def poll_age_seconds(self) -> float:
        """Seconds since the last completed poll cycle.

        Before the first cycle completes this reports the age since process
        start, so a poller that never started at all is visible rather than
        reading as ``None`` and being skipped by a naive threshold check.
        """
        reference = (
            self.last_poll_monotonic
            if self.last_poll_monotonic is not None
            else self.started_monotonic
        )
        return max(0.0, time.monotonic() - reference)

    def stalled(
        self, *, threshold_seconds: float = DEFAULT_STALL_THRESHOLD_SECONDS
    ) -> bool:
        """True when the dispatch path has gone stale.

        Stale poll age alone is diagnostic — see the module docstring
        (GAU-10). ``record_poll_cycle`` stamps every cycle including empty
        ones, so an idle-but-alive poller keeps age fresh; age past the
        threshold already means the poller stopped completing cycles.
        ``last_observed_queue_depth`` does not gate this: it is written only
        by the poller whose death this method exists to detect, so a freeze
        that begins at depth 0 would pin depth at 0 forever and could never
        be used as a gate without silencing exactly the freeze it is meant to
        catch.
        """
        return self.poll_age_seconds() > threshold_seconds

    def snapshot(
        self, *, threshold_seconds: float = DEFAULT_STALL_THRESHOLD_SECONDS
    ) -> dict[str, object]:
        """Render the liveness view for a health probe.

        Ships the derived ``action_path_stalled`` verdict alongside the raw
        numbers so a consumer does not have to re-derive the stale-age check
        (and get it wrong) to know whether the action path is alive.
        """
        return {
            "action_path_stalled": self.stalled(threshold_seconds=threshold_seconds),
            "poll_age_seconds": round(self.poll_age_seconds(), 3),
            "queued_depth": self.last_observed_queue_depth,
            "last_poll_at_utc": self.last_poll_at_utc,
            "total_dispatched": self.total_dispatched,
            "stall_threshold_seconds": threshold_seconds,
        }


#: Process-wide singleton. The poller and the health probe live in the same
#: process (that co-location is what makes a GIL-starved poller observable from
#: a still-serving HTTP surface), so a module-level instance is the whole
#: wiring — no registry, no injection, nothing that could itself fail closed.
ACTION_PATH_LIVENESS = ActionPathLiveness()


__all__ = [
    "ACTION_PATH_LIVENESS",
    "DEFAULT_STALL_THRESHOLD_SECONDS",
    "ActionPathLiveness",
]
