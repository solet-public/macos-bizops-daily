#!/usr/bin/env python3
"""Decision-logic smoke for the Option-B colour-agnostic supervisor.

The supervisor's entire value is crash-supervision that survives blue-green
cutovers. The discriminating test is NOT "more cutovers" (during a cutover the
router always shows a fresh active colour, so the supervisor correctly no-ops
— it is never exercised): it is a deliberate loss of the active colour, after
which the supervisor must spawn a replacement from ``current``. This smoke
drives :meth:`Supervisor.tick` against fully-faked seams (no real sockets,
processes, or sleeps) and asserts the :class:`TickOutcome` for every branch:

* cold-start: router has no active colour → SPAWNED;
* a spawn in flight → PENDING (never double-spawn);
* the spawn becomes active → HEALTHY (pending cleared, backoff reset);
* steady state + a blue→green cutover → HEALTHY throughout (purely additive);
* the active colour is lost (crash; router GC clears it) → SPAWNED (the
  respawn that closes the supervision gap);
* router unreachable → ROUTER_DOWN (wait, don't blind-spawn zombies);
* ``stop_self`` drain sentinel set → DRAINING (respawn suppressed);
* a boot that dies without activating → BACKOFF until the wait elapses, then
  SPAWNED (crash-loop guard);
* a spawn seam that RAISES (missing ``current`` interpreter / ``Popen`` failure)
  → SPAWN_FAILED then BACKOFF, not a tight per-poll retry (crash-loop guard
  covers the raise path too);
* exited children are reaped (no zombie accumulation).

Standalone — not pytest. Run with::

    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/supervisor_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from macos_self_deployment_plugin.supervisor import (  # noqa: E402
    Supervisor,
    SupervisorSeams,
    TickOutcome,
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


class _FakePopen:
    """Minimal Popen stand-in: a pid + a controllable ``poll()`` disposition."""

    _seq = 1000

    def __init__(self, *, exited: bool = False) -> None:
        _FakePopen._seq += 1
        self.pid = _FakePopen._seq
        self._code: int | None = 0 if exited else None

    def set_exited(self, code: int = 0) -> None:
        self._code = code

    def poll(self) -> int | None:
        return self._code


class _Harness:
    """Controllable seams + a Supervisor wired to them."""

    def __init__(self, *, backoff_base: float = 5.0) -> None:
        self.active_instance_id: str | None = None
        self.router_reachable = True
        self.draining = False
        self.clock = 1_000_000.0
        self.spawn_calls = 0
        self.spawned: list[_FakePopen] = []
        self.next_spawn_exited = False
        self.spawn_raises = False
        self.supervisor = Supervisor(
            solet_name="supsmoke",
            app_home=Path("/nonexistent/profile"),
            releases_root=Path("/nonexistent/releases"),
            seams=SupervisorSeams(
                router_status=self._router_status,
                spawn=self._spawn,
                is_draining=lambda: self.draining,
                sleep=lambda _s: None,
                monotonic=lambda: self.clock,
            ),
            backoff_base_seconds=backoff_base,
        )

    def _router_status(self) -> dict[str, Any] | None:
        """Mirror the live-verified router ``status`` wire shape.

        Top-level keys + the binding-derived ``active_color`` (non-None iff a
        live active binding exists) match the real ``_dispatch_status`` dict
        confirmed against the running router. ``active_color`` is what the
        supervisor keys on, so it must be present and binding-derived here.
        """
        if not self.router_reachable:
            return None
        active_color: str | None = None
        colors: list[dict[str, Any]] = []
        if self.active_instance_id is not None:
            active_color = "green" if "green" in self.active_instance_id else "blue"
            colors.append(
                {
                    "instance_id": self.active_instance_id,
                    "color": active_color,
                    "port": 60000,
                    "status": "active",
                    "last_heartbeat": self.clock,
                }
            )
        return {
            "router_started_at": self.clock,
            "active_color": active_color,
            "active_instance_id": self.active_instance_id,
            "colors": colors,
            "drain_entries": [],
        }

    def _spawn(self) -> _FakePopen:
        self.spawn_calls += 1
        if self.spawn_raises:
            # Model a broken ``current`` interpreter / a ``Popen`` failure: the
            # spawn seam itself raises before returning a process.
            raise OSError("simulated spawn seam failure (missing interpreter)")
        proc = _FakePopen(exited=self.next_spawn_exited)
        self.spawned.append(proc)
        return proc

    def tick(self) -> TickOutcome:
        return self.supervisor.tick()

    # Convenience mutators that model the router's observable transitions.
    def go_active(self, instance_id: str = "example-blue-cold") -> None:
        self.active_instance_id = instance_id

    def lose_active(self) -> None:
        """Model the router's ``_heartbeat_gc`` clearing a dead active binding."""
        self.active_instance_id = None


def test_cold_start_and_pending_and_activate() -> None:
    h = _Harness()
    # Cold start: router has no active colour → spawn.
    _check(h.tick() is TickOutcome.SPAWNED, "cold-start with no active colour → SPAWNED")
    _check(h.spawn_calls == 1, "cold-start spawned exactly one child")
    # The spawn is booting (poll None) and not yet active → no double-spawn.
    _check(h.tick() is TickOutcome.PENDING, "spawn in flight → PENDING")
    _check(h.spawn_calls == 1, "PENDING did not spawn a second child")
    # The spawn registers + self-activates (router now names it active).
    h.go_active("example-blue-cold")
    _check(h.tick() is TickOutcome.HEALTHY, "spawn became active → HEALTHY")
    _check(h.spawn_calls == 1, "HEALTHY did not spawn")


def test_steady_state_and_cutover_are_noops() -> None:
    h = _Harness()
    h.go_active("example-blue-1")
    _check(h.tick() is TickOutcome.HEALTHY, "steady-state active colour → HEALTHY")
    # A blue→green cutover: the active instance flips, but is never None.
    h.go_active("example-green-2")
    _check(h.tick() is TickOutcome.HEALTHY, "cutover (active flips, never None) → HEALTHY")
    h.go_active("example-blue-3")
    _check(h.tick() is TickOutcome.HEALTHY, "second cutover → HEALTHY")
    _check(h.spawn_calls == 0, "no spawn across steady-state + two cutovers (purely additive)")


def test_crash_respawn() -> None:
    """The discriminating test: lose the active colour → supervisor respawns."""
    h = _Harness()
    h.go_active("example-green-live")
    _check(h.tick() is TickOutcome.HEALTHY, "active colour healthy")
    # kill -9 the active colour; the router's hb-gc clears active_instance_id.
    h.lose_active()
    _check(h.tick() is TickOutcome.SPAWNED, "active colour lost (crash) → SPAWNED replacement")
    _check(h.spawn_calls == 1, "crash respawn spawned exactly one replacement")
    # Replacement is booting → PENDING, not a second respawn.
    _check(h.tick() is TickOutcome.PENDING, "replacement booting → PENDING")
    _check(h.spawn_calls == 1, "no second respawn while the first boots")


def test_router_down_waits() -> None:
    h = _Harness()
    h.router_reachable = False
    _check(h.tick() is TickOutcome.ROUTER_DOWN, "router unreachable → ROUTER_DOWN")
    _check(h.spawn_calls == 0, "router-down did NOT blind-spawn a zombie")


def test_draining_suppresses_respawn() -> None:
    h = _Harness()
    h.draining = True  # operator stop_self persistent sentinel
    _check(h.tick() is TickOutcome.DRAINING, "no active + draining → DRAINING (suppressed)")
    _check(h.spawn_calls == 0, "draining suppressed the spawn (stop-and-stay-stopped)")
    # Clearing the sentinel lets the next poll respawn — a clean resume.
    h.draining = False
    _check(h.tick() is TickOutcome.SPAWNED, "sentinel cleared → SPAWNED (resume)")


def test_backoff_after_failed_boot() -> None:
    h = _Harness(backoff_base=5.0)
    # The spawned child dies immediately without ever becoming active.
    h.next_spawn_exited = True
    _check(h.tick() is TickOutcome.SPAWNED, "first boot attempt → SPAWNED")
    # The pending child has already exited (poll != None); still no active
    # colour; backoff has NOT elapsed (clock unchanged) → BACKOFF, not respawn.
    _check(h.tick() is TickOutcome.BACKOFF, "failed boot, backoff not elapsed → BACKOFF")
    _check(h.spawn_calls == 1, "BACKOFF held off the immediate respawn")
    # Advance the clock past the (base) backoff → retry.
    h.clock += 6.0
    _check(h.tick() is TickOutcome.SPAWNED, "after backoff elapses → SPAWNED retry")
    _check(h.spawn_calls == 2, "exactly one retry after backoff")


def test_spawn_exception_backoff() -> None:
    """A RAISING spawn seam must arm the backoff, not retry every poll (MINOR-1).

    Before the fix the boot-failure counter only incremented AFTER ``spawn()``
    returned, so a seam that raised (missing ``current`` interpreter, ``Popen``
    failure) escaped to ``run_forever`` and the next poll re-attempted
    immediately — a tight crash-loop at poll cadence. The fix stamps the
    attempt + counts it before the seam call and returns SPAWN_FAILED.
    """
    h = _Harness(backoff_base=5.0)
    h.spawn_raises = True
    # Cold start: no active colour → attempt a spawn, but the seam raises.
    _check(h.tick() is TickOutcome.SPAWN_FAILED, "spawn seam raises → SPAWN_FAILED (no crash)")
    _check(h.spawn_calls == 1, "one spawn attempt was made")
    _check(
        h.supervisor._boot_failures == 1,  # noqa: SLF001 — smoke inspects internal counter
        "failed spawn was counted as a boot failure (arms backoff)",
    )
    # Clock unchanged → backoff has NOT elapsed → the next poll must NOT
    # re-attempt (the bug was an immediate retry every poll).
    _check(
        h.tick() is TickOutcome.BACKOFF,
        "failed spawn → BACKOFF on next tick (no immediate retry)",
    )
    _check(h.spawn_calls == 1, "BACKOFF held off the immediate re-attempt")
    # After the backoff window elapses, it retries; the seam now works.
    h.clock += 6.0
    h.spawn_raises = False
    _check(h.tick() is TickOutcome.SPAWNED, "after backoff elapses → SPAWNED retry")
    _check(h.spawn_calls == 2, "exactly one retry after the backoff window")


def test_children_reaped() -> None:
    h = _Harness()
    # Three cold-start spawns, each dies before activating; each tick reaps the
    # prior dead pending and (after backoff) spawns afresh.
    h.next_spawn_exited = True
    h.tick()  # spawn #1 (already-exited)
    h.clock += 100.0
    h.tick()  # reaps #1, spawn #2
    h.clock += 100.0
    h.tick()  # reaps #2, spawn #3
    # Only the latest (still "pending", though exited) remains tracked after a
    # final reaping tick; the earlier exited children are gone (no zombies).
    h.clock += 100.0
    h.tick()
    tracked = len(h.supervisor._children)  # noqa: SLF001 — smoke inspects internal
    _check(
        tracked <= 1,
        f"exited children reaped — at most the latest remains tracked (got {tracked})",
    )


def main() -> int:
    print("=== supervisor_smoke (Option-B crash-supervision decision logic) ===")
    test_cold_start_and_pending_and_activate()
    test_steady_state_and_cutover_are_noops()
    test_crash_respawn()
    test_router_down_waits()
    test_draining_suppresses_respawn()
    test_backoff_after_failed_boot()
    test_spawn_exception_backoff()
    test_children_reaped()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
