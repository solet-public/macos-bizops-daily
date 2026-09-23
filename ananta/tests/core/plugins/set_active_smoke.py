"""Slice D smoke — LifecycleManaged.set_active + ActionQueuePoller is_active_color.

Per L3 plan §3.2 (`workbench/2026-06-01_local_blue_green_L3_implementation_plan.md`):

  Two LifecycleManaged plugin instances in a test harness. Call set_active(False)
  on one; verify its background loop quiesces. Call set_active(True); verify it
  resumes. PLUS: tick action_queue_poller against orchestrator.is_active_color =
  False; verify no work claimed. Set True; verify work proceeds.

Standalone — no pytest, no platform startup. Two test surfaces:

  case_plugin_layer:   two fake plugins that inherit ServicePlugin get the
                       default no-op set_active; one overrides to gate its tick
                       counter. Verify counter behavior across
                       set_active(False/True).
  case_aqp_layer:      instantiate the real ActionQueuePoller with stub deps,
                       bind a getter, verify _poll_once is called/not-called per
                       the getter's return value.

Run: .venv/bin/python3 ananta/tests/core/plugins/set_active_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.domain.types import ActionResult  # noqa: E402
from ananta.core.plugins.plugin_base import ServicePlugin  # noqa: E402
from ananta.core.plugins.protocols import LifecycleManaged  # noqa: E402


def _stamp(label: str, ok: bool, detail: str = "") -> bool:
    sym = "PASS" if ok else "FAIL"
    print(f"  [{sym}] {label}" + (f" — {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------------------
# Case 1: plugin layer — set_active on ServicePlugin subclasses
# ---------------------------------------------------------------------------


def _completed_result() -> ActionResult:
    return ActionResult(
        action_status="completed",
        data={},
        actions=[],
        error=None,
        timestamp="1970-01-01T00:00:00+00:00",
    )


class _DefaultNoOpPlugin(ServicePlugin):
    """Inherits ServicePlugin's default no-op set_active. Mimics a service
    plugin with no background work to gate — set_active is a silent no-op."""

    async def start_services(self) -> ActionResult:
        return _completed_result()

    async def stop_services(self) -> ActionResult:
        return _completed_result()


class _GatedTickerPlugin(ServicePlugin):
    """Overrides set_active to gate a per-tick counter. Mimics a real plugin
    with a background loop (drainer, scheduler, message handler)."""

    def __init__(self) -> None:
        super().__init__()
        self._active: bool = True
        self.tick_count: int = 0

    def set_active(self, active: bool) -> None:
        self._active = active

    def tick(self) -> None:
        if not self._active:
            return
        self.tick_count += 1

    async def start_services(self) -> ActionResult:
        return _completed_result()

    async def stop_services(self) -> ActionResult:
        return _completed_result()


def case_plugin_layer() -> bool:
    print("\n[case_plugin_layer] set_active gates background work; default is no-op")
    default_plugin = _DefaultNoOpPlugin()
    gated_plugin = _GatedTickerPlugin()

    # Both should be runtime_checkable as LifecycleManaged (ServicePlugin's
    # default set_active + the override both satisfy the structural Protocol).
    ok_proto_default = isinstance(default_plugin, LifecycleManaged)  # pyright: ignore[reportUnnecessaryIsInstance]
    ok_proto_gated = isinstance(gated_plugin, LifecycleManaged)  # pyright: ignore[reportUnnecessaryIsInstance]
    _stamp("ServicePlugin satisfies LifecycleManaged Protocol (set_active present)", ok_proto_default)
    _stamp("Override plugin satisfies LifecycleManaged Protocol", ok_proto_gated)

    # Default no-op accepts the call and does nothing observable.
    default_plugin.set_active(False)
    default_plugin.set_active(True)
    _stamp("Default no-op set_active accepts False+True without raising", True)

    # Gated plugin: tick 3 times while active, set inactive, tick 3 more,
    # set active, tick 3 more. Expect counter = 3, 3, 6.
    for _ in range(3):
        gated_plugin.tick()
    after_active_burst = gated_plugin.tick_count

    gated_plugin.set_active(False)
    for _ in range(3):
        gated_plugin.tick()
    after_inactive_burst = gated_plugin.tick_count

    gated_plugin.set_active(True)
    for _ in range(3):
        gated_plugin.tick()
    after_resume_burst = gated_plugin.tick_count

    ok_active = after_active_burst == 3
    ok_quiesce = after_inactive_burst == 3  # unchanged during inactive burst
    ok_resume = after_resume_burst == 6
    _stamp(f"3 ticks while active → counter=3 (got {after_active_burst})", ok_active)
    _stamp(f"3 ticks while inactive → counter unchanged at 3 (got {after_inactive_burst})", ok_quiesce)
    _stamp(f"3 ticks after resume → counter=6 (got {after_resume_burst})", ok_resume)

    # Idempotency: calling set_active(False) twice is a no-op on second call.
    gated_plugin.set_active(False)
    gated_plugin.set_active(False)
    _stamp("set_active(False) twice in a row is idempotent (no raise)", True)

    return (
        ok_proto_default and ok_proto_gated
        and ok_active and ok_quiesce and ok_resume
    )


# ---------------------------------------------------------------------------
# Case 2: ActionQueuePoller layer — gating via is_active_color_getter
# ---------------------------------------------------------------------------


class _StubStateService:
    """Minimal StateService stub for ActionQueuePoller construction."""

    def __init__(self) -> None:
        self.write_state_calls: int = 0

    def write_state(self, **_kwargs: Any) -> dict[str, object]:
        self.write_state_calls += 1
        return {"action_status": "completed"}


def case_aqp_layer() -> bool:
    print("\n[case_aqp_layer] ActionQueuePoller skips _poll_once when getter returns False")

    from ananta.core.actions.action_queue_poller import ActionQueuePoller

    # Construct AQP with minimal stubs; the blob_storage_service check
    # requires a non-None value, so pass an object().
    poller = ActionQueuePoller(
        state_service=_StubStateService(),  # type: ignore[arg-type]
        action_processor=object(),  # type: ignore[arg-type]
        flow_runtime_graph=_StubFRG(),  # type: ignore[arg-type]
        action_factory=object(),  # type: ignore[arg-type]
        blob_storage_service=object(),  # type: ignore[arg-type]
        poll_interval=0.02,
    )

    # Substitute _poll_once with a counter; the loop should call this every
    # tick the getter returns True and skip when it returns False.
    poll_once_calls = [0]

    async def _fake_poll_once() -> None:
        poll_once_calls[0] += 1

    poller._poll_once = _fake_poll_once  # type: ignore[method-assign]

    # No getter bound → always-active default; expect _poll_once to fire.
    async def _drive_default() -> None:
        poller.running = True
        task = asyncio.create_task(poller._poll_loop())
        await asyncio.sleep(0.08)
        poller.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive_default())
    default_calls = poll_once_calls[0]
    ok_default = default_calls >= 2
    _stamp(
        f"no getter bound → always-active; _poll_once fires (calls={default_calls})",
        ok_default,
    )

    # Bind getter; toggle False; expect _poll_once to NOT fire on subsequent
    # ticks (small grace window for the in-flight tick to complete).
    poll_once_calls[0] = 0
    is_active = [True]
    poller.set_is_active_color_getter(lambda: is_active[0])

    async def _drive_toggle() -> tuple[int, int, int]:
        poller.running = True
        task = asyncio.create_task(poller._poll_loop())
        await asyncio.sleep(0.06)  # active phase
        active_calls = poll_once_calls[0]
        is_active[0] = False
        poll_once_calls[0] = 0
        await asyncio.sleep(0.10)  # inactive phase
        inactive_calls = poll_once_calls[0]
        is_active[0] = True
        poll_once_calls[0] = 0
        await asyncio.sleep(0.06)  # resumed
        resumed_calls = poll_once_calls[0]
        poller.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return active_calls, inactive_calls, resumed_calls

    active_calls, inactive_calls, resumed_calls = asyncio.run(_drive_toggle())
    ok_active_phase = active_calls >= 1
    ok_quiesce_phase = inactive_calls == 0
    ok_resume_phase = resumed_calls >= 1
    _stamp(f"active-phase _poll_once fires (calls={active_calls})", ok_active_phase)
    _stamp(f"inactive-phase _poll_once SKIPPED (calls={inactive_calls})", ok_quiesce_phase)
    _stamp(f"resumed-phase _poll_once fires (calls={resumed_calls})", ok_resume_phase)

    return ok_default and ok_active_phase and ok_quiesce_phase and ok_resume_phase


class _StubFRG:
    """Stub FlowRuntimeGraph satisfying ActionQueuePoller's callback-registration."""

    def register_completion_callback(self, _cb: Any) -> None:
        pass


def main() -> int:
    print(f"set_active_smoke: starting at {time.time():.3f}")
    results = [
        ("plugin_layer", case_plugin_layer()),
        ("aqp_layer", case_aqp_layer()),
    ]
    print("\nsummary")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        _stamp(name, ok)
    print(f"\n{passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
