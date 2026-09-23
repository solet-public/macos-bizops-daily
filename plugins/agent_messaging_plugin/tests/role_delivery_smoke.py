#!/usr/bin/env python3
"""Unit smoke for v10 Control #5 client side — OwedDeliveryCoordinator (no DB).

Drives the extracted bridge-side role-delivery coordinator against a fake
transport (no httpx, no running solet) to prove the at-least-once contract:

  * **settle** emits a role message at most once and ALWAYS confirms delivery
    (POST /peer/delivered) — even when emission is dedup-suppressed (M7);
  * **dedup** across the live path and the repair drain: the same
    ``external_id`` is emitted once but confirmed every encounter;
  * **repair pass** re-queries oldest-first until the drain page is empty, and
    a never-draining page is bounded by the loud ``MAX_PASSES`` guard (no hang);
  * **role_delivery_keys** recognises a role delivery by its Control #5 meta and
    fails closed (``None``) on a non-role / malformed event;
  * **drain-row → event** carries the marker-stripped prose + the role meta keys
    a live event carries, so a drained message is indistinguishable downstream;
  * **lifecycle**: start launches the loop, stop cancels it cleanly.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_delivery_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.mcp_bridge import owed_delivery  # noqa: E402
from agent_messaging_plugin.mcp_bridge.owed_delivery import (  # noqa: E402
    EVENT_PEER_MESSAGE,
    OwedDeliveryCoordinator,
    _drain_row_to_event,
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


class _FakeTransport:
    """Records emits + flips; serves pre-seeded drain pages in order."""

    def __init__(self, *, drain_pages: list[list[dict[str, Any]]] | None = None) -> None:
        self._drain_pages = list(drain_pages or [])
        self.emitted: list[dict[str, Any]] = []
        self.flipped: list[tuple[str, str]] = []
        self.drain_calls = 0
        self.fail_emits = 0  # number of upcoming emit_event calls to raise on
        self._running = True
        self._ready = True

    @property
    def bridge_ready(self) -> bool:
        return self._ready

    @property
    def running(self) -> bool:
        return self._running

    async def emit_event(self, event: dict[str, Any]) -> None:
        if self.fail_emits > 0:
            self.fail_emits -= 1
            msg = "injected emit failure"
            raise RuntimeError(msg)
        self.emitted.append(event)

    async def drain_page(self, limit: int) -> dict[str, Any]:
        self.drain_calls += 1
        page = self._drain_pages.pop(0) if self._drain_pages else []
        return {"undelivered": page, "re_emit_cap": 3}

    async def flip_delivered(self, *, external_id: str, recipient_key: str) -> None:
        self.flipped.append((external_id, recipient_key))


class _GatedTransport:
    """Transport whose emit BLOCKS on a gate until released — lets a test hold
    one emit in-flight while a second concurrent settle runs (the BLOCKER-2-deep
    interleaving)."""

    def __init__(self) -> None:
        self.emits = 0
        self.flips = 0
        self.fail = False
        self._gate = asyncio.Event()
        self._running = True
        self._ready = True

    @property
    def bridge_ready(self) -> bool:
        return self._ready

    @property
    def running(self) -> bool:
        return self._running

    def release(self) -> None:
        self._gate.set()

    async def emit_event(self, event: dict[str, Any]) -> None:
        await self._gate.wait()
        self.emits += 1
        if self.fail:
            self.fail = False
            msg = "gated emit failure"
            raise RuntimeError(msg)

    async def drain_page(self, limit: int) -> dict[str, Any]:
        return {"undelivered": [], "re_emit_cap": 3}

    async def flip_delivered(self, *, external_id: str, recipient_key: str) -> None:
        self.flips += 1


def _role_event(external_id: str, recipient_key: str = "Architect") -> dict[str, Any]:
    return {
        "event_type": EVENT_PEER_MESSAGE,
        "content": "hello",
        "cursor": 7,
        "meta": {
            "recipient_kind": "role",
            "recipient_key": recipient_key,
            "delivery_external_id": external_id,
        },
    }


def _drain_row(external_id: str, recipient_key: str = "Architect") -> dict[str, Any]:
    return {
        "external_id": external_id,
        "recipient_key": recipient_key,
        "message_id": external_id.split(":")[-1],
        "sender_agent_id": "claude_code",
        "sender_agent_instance_id": "agi-sender",
        "sender_session_label": "Coordinator",
        "thread_id": f"role:{recipient_key}",
        "important": True,
        "content": "ping from coordinator",
    }


def test_settle_live_emits_once_and_flips() -> None:
    transport = _FakeTransport()
    coord = OwedDeliveryCoordinator(transport)
    event = _role_event("role:Architect:arm-1")
    asyncio.run(
        coord.settle_live(
            event=event, external_id="role:Architect:arm-1", recipient_key="Architect",
        ),
    )
    _check(len(transport.emitted) == 1, "settle_live → emitted once")
    _check(
        transport.flipped == [("role:Architect:arm-1", "Architect")],
        "settle_live → confirmed delivered once",
    )


def test_dedup_suppresses_second_emit_but_still_flips() -> None:
    transport = _FakeTransport()
    coord = OwedDeliveryCoordinator(transport)

    async def _run() -> None:
        await coord.settle_live(
            event=_role_event("role:Architect:arm-2"),
            external_id="role:Architect:arm-2",
            recipient_key="Architect",
        )
        # Same external_id again (e.g. a drain race after a live emit).
        await coord.settle_live(
            event=_role_event("role:Architect:arm-2"),
            external_id="role:Architect:arm-2",
            recipient_key="Architect",
        )

    asyncio.run(_run())
    _check(len(transport.emitted) == 1, "M7: duplicate external_id emitted only once")
    _check(len(transport.flipped) == 2, "M7: dedup-suppressed encounter STILL re-confirms")


def test_emit_failure_rolls_back_dedup() -> None:
    # Codex BLOCKER-2: an emit exception must ROLL BACK the dedup claim so a
    # retry re-emits — otherwise the failed external_id stays "emitted" and is
    # never delivered (the flip also never fires). FAILS on the pre-fix code
    # (which claimed before the await + never rolled back → retry suppressed).
    transport = _FakeTransport()
    transport.fail_emits = 1  # the first emit raises
    coord = OwedDeliveryCoordinator(transport)

    async def _run() -> None:
        try:
            await coord.settle_live(
                event=_role_event("role:Architect:arm-9"),
                external_id="role:Architect:arm-9",
                recipient_key="Architect",
            )
        except RuntimeError:
            pass  # the injected emit failure propagates (flip NOT issued)
        # Retry — the claim was rolled back, so this emit actually delivers.
        await coord.settle_live(
            event=_role_event("role:Architect:arm-9"),
            external_id="role:Architect:arm-9",
            recipient_key="Architect",
        )

    asyncio.run(_run())
    _check(len(transport.emitted) == 1, "emit-failure rollback → retry RE-EMITS (not suppressed)")
    _check(
        transport.flipped == [("role:Architect:arm-9", "Architect")],
        "no flip on the failed emit; the successful retry confirms exactly once",
    )


def test_concurrent_no_flip_on_inflight_or_failed_emit() -> None:
    # BLOCKER-2-deep: a second concurrent settle for the same id must NOT flip
    # delivered on the basis of the first caller's IN-FLIGHT (then FAILED) emit.
    # On the pre-fix set-based code, B saw the id "claimed" and flipped while A's
    # emit was still pending → this asserts ZERO flips while pending (FAILS-on-old).
    transport = _GatedTransport()
    coord = OwedDeliveryCoordinator(transport)
    eid = "role:Architect:arm-c1"

    async def _run() -> tuple[list[object], int, int]:
        ev = _role_event(eid)
        a = asyncio.ensure_future(coord.settle_live(event=ev, external_id=eid, recipient_key="R"))
        await asyncio.sleep(0)  # A starts the shared emit task (blocked on the gate)
        b = asyncio.ensure_future(coord.settle_live(event=ev, external_id=eid, recipient_key="R"))
        await asyncio.sleep(0)  # B suspends awaiting the SAME in-flight emit task
        flips_pending = transport.flips
        transport.fail = True  # the shared emit will raise
        transport.release()
        results = await asyncio.gather(a, b, return_exceptions=True)
        return results, flips_pending, transport.flips

    results, flips_pending, flips_after = asyncio.run(_run())
    _check(flips_pending == 0, "concurrent: ZERO flips while the shared emit is in-flight")
    _check(
        all(isinstance(r, RuntimeError) for r in results),
        "concurrent: BOTH settles propagate the failed emit (no flip, no swallow)",
    )
    _check(flips_after == 0, "concurrent: ZERO flips after the failed emit (delivered STAYS false)")


def test_concurrent_retry_redelivers_after_failure() -> None:
    transport = _GatedTransport()
    coord = OwedDeliveryCoordinator(transport)
    eid = "role:Architect:arm-c2"

    async def _run() -> tuple[int, int]:
        ev = _role_event(eid)
        a = asyncio.ensure_future(coord.settle_live(event=ev, external_id=eid, recipient_key="R"))
        await asyncio.sleep(0)
        b = asyncio.ensure_future(coord.settle_live(event=ev, external_id=eid, recipient_key="R"))
        await asyncio.sleep(0)
        transport.fail = True
        transport.release()
        await asyncio.gather(a, b, return_exceptions=True)
        # The failed task was popped → a fresh settle re-emits + flips (re-delivery).
        await coord.settle_live(event=ev, external_id=eid, recipient_key="R")
        return transport.emits, transport.flips

    emits, flips = asyncio.run(_run())
    _check(emits == 2, "concurrent retry: failed emit + successful retry both ran (re-delivered)")
    _check(flips == 1, "concurrent retry: flip ONLY on the successful retry, not the failed in-flight")


def test_concurrent_success_single_emit_both_flip() -> None:
    transport = _GatedTransport()
    coord = OwedDeliveryCoordinator(transport)
    eid = "role:Architect:arm-c3"

    async def _run() -> tuple[int, int, int]:
        ev = _role_event(eid)
        a = asyncio.ensure_future(coord.settle_live(event=ev, external_id=eid, recipient_key="R"))
        await asyncio.sleep(0)
        b = asyncio.ensure_future(coord.settle_live(event=ev, external_id=eid, recipient_key="R"))
        await asyncio.sleep(0)
        flips_pending = transport.flips
        transport.release()  # the shared emit SUCCEEDS
        await asyncio.gather(a, b)
        return flips_pending, transport.emits, transport.flips

    flips_pending, emits, flips = asyncio.run(_run())
    _check(flips_pending == 0, "concurrent success: ZERO flips while the shared emit is in-flight")
    _check(emits == 1, "concurrent success: single-flight — exactly ONE emit for two settles")
    _check(flips == 2, "concurrent success: both flip, but only AFTER the shared emit completes (M7)")


def test_concurrent_cancel_one_waiter_isolated() -> None:
    # MINOR-1 (shield): cancelling ONE waiter must NOT cancel the shared emit
    # the OTHER waiters depend on. FAILS on the un-shielded code (A's cancel
    # cancels the shared task → B also fails); PASSES with asyncio.shield.
    transport = _GatedTransport()
    coord = OwedDeliveryCoordinator(transport)
    eid = "role:Architect:arm-c4"

    async def _run() -> tuple[bool, int, int]:
        ev = _role_event(eid)
        a = asyncio.ensure_future(coord.settle_live(event=ev, external_id=eid, recipient_key="R"))
        await asyncio.sleep(0)  # A creates + awaits the shared (gated) emit task
        b = asyncio.ensure_future(coord.settle_live(event=ev, external_id=eid, recipient_key="R"))
        await asyncio.sleep(0)  # B awaits the SAME task (shielded)
        a.cancel()
        try:
            await a
        except asyncio.CancelledError:
            pass
        a_cancelled = a.cancelled()
        transport.release()  # the shared emit now SUCCEEDS
        await b  # B must still complete — A's cancel did not cancel the shared emit
        return a_cancelled, transport.emits, transport.flips

    a_cancelled, emits, flips = asyncio.run(_run())
    _check(a_cancelled, "cancelling one waiter cancels only that await")
    _check(emits == 1, "shield: the shared emit still ran exactly once (not cancelled by A)")
    _check(flips == 1, "shield: the surviving waiter B still gets the result + flips")


def test_repair_pass_requeries_until_empty() -> None:
    transport = _FakeTransport(
        drain_pages=[
            [_drain_row("role:Architect:arm-a"), _drain_row("role:Architect:arm-b")],
            [_drain_row("role:Architect:arm-c")],
            [],
        ],
    )
    coord = OwedDeliveryCoordinator(transport)
    asyncio.run(coord._repair_pass())  # noqa: SLF001 — unit-driving the pass directly
    _check(len(transport.emitted) == 3, "repair pass emits every owed row across pages")
    _check(len(transport.flipped) == 3, "repair pass confirms every owed row")
    _check(transport.drain_calls == 3, "repair pass re-queries until the page is empty")


def test_repair_pass_dedups_against_live() -> None:
    transport = _FakeTransport(drain_pages=[[_drain_row("role:Architect:arm-x")], []])
    coord = OwedDeliveryCoordinator(transport)

    async def _run() -> None:
        await coord.settle_live(
            event=_role_event("role:Architect:arm-x"),
            external_id="role:Architect:arm-x",
            recipient_key="Architect",
        )
        await coord._repair_pass()  # noqa: SLF001

    asyncio.run(_run())
    _check(len(transport.emitted) == 1, "live-emitted row not re-emitted by the drain")
    _check(len(transport.flipped) == 2, "drain still re-confirms the already-emitted row")


def test_repair_pass_max_passes_guard() -> None:
    # A page that never empties (the fake never drops rows) must NOT hang — the
    # MAX_PASSES guard bounds it. Patch the constant small to keep the test fast.
    original = owed_delivery.REPAIR_DRAIN_MAX_PASSES
    owed_delivery.REPAIR_DRAIN_MAX_PASSES = 3
    try:
        transport = _FakeTransport(
            drain_pages=[[_drain_row(f"role:Architect:arm-{i}")] for i in range(100)],
        )
        coord = OwedDeliveryCoordinator(transport)
        asyncio.run(coord._repair_pass())  # noqa: SLF001
        _check(transport.drain_calls == 3, "MAX_PASSES guard bounds re-queries (no hang)")
    finally:
        owed_delivery.REPAIR_DRAIN_MAX_PASSES = original


def test_role_delivery_keys() -> None:
    keys = OwedDeliveryCoordinator.role_delivery_keys(_role_event("role:Architect:arm-9"))
    _check(keys == ("role:Architect:arm-9", "Architect"), "role event → (external_id, key)")
    _check(
        OwedDeliveryCoordinator.role_delivery_keys({"event_type": "post_message"}) is None,
        "non-role event (no meta) → None",
    )
    _check(
        OwedDeliveryCoordinator.role_delivery_keys(
            {"meta": {"recipient_kind": "instance"}},
        )
        is None,
        "instance-kind event → None",
    )
    _check(
        OwedDeliveryCoordinator.role_delivery_keys(
            {"meta": {"recipient_kind": "role", "recipient_key": "Architect"}},
        )
        is None,
        "role event missing delivery_external_id → None (fail closed)",
    )
    _check(
        OwedDeliveryCoordinator.role_delivery_keys(
            {
                "meta": {
                    "recipient_kind": "role",
                    "recipient_key": "",
                    "delivery_external_id": "role::arm",
                },
            },
        )
        is None,
        "role event with empty recipient_key → None (fail closed)",
    )


def test_drain_row_to_event_shape() -> None:
    event = _drain_row_to_event(
        _drain_row("role:Architect:arm-7"),
        external_id="role:Architect:arm-7",
        recipient_key="Architect",
    )
    _check(event["event_type"] == EVENT_PEER_MESSAGE, "drain event_type == peer_message")
    _check(event["content"] == "ping from coordinator", "drain event carries the prose")
    _check("cursor" not in event, "drain event has NO cursor (not part of /events stream)")
    meta = event["meta"]
    _check(
        meta["recipient_kind"] == "role"
        and meta["recipient_key"] == "Architect"
        and meta["delivery_external_id"] == "role:Architect:arm-7",
        "drain event meta carries the Control #5 role keys",
    )
    _check(
        meta["from_agent_id"] == "claude_code" and meta["important"] is True,
        "drain event meta carries sender provenance + important",
    )


def test_deliver_drain_row_missing_keys_skipped() -> None:
    transport = _FakeTransport()
    coord = OwedDeliveryCoordinator(transport)
    asyncio.run(coord._deliver_drain_row({"message_id": "arm-bad"}, cap=3))  # noqa: SLF001
    _check(
        not transport.emitted and not transport.flipped,
        "drain row missing external_id/recipient_key → skipped (no emit/flip)",
    )


def test_lifecycle_start_stop() -> None:
    transport = _FakeTransport()
    coord = OwedDeliveryCoordinator(transport)

    async def _run() -> bool:
        coord.start()
        await asyncio.sleep(0)  # let the loop schedule
        await coord.stop()
        return coord._repair_task is None  # noqa: SLF001

    _check(asyncio.run(_run()), "start launches the loop; stop cancels it cleanly")
    # stop is also safe with no task running.
    asyncio.run(OwedDeliveryCoordinator(_FakeTransport()).stop())
    _check(True, "stop is a no-op when no loop is running")


def main() -> int:
    print("=== v10 Control #5 client OwedDeliveryCoordinator smoke ===")
    test_settle_live_emits_once_and_flips()
    test_dedup_suppresses_second_emit_but_still_flips()
    test_emit_failure_rolls_back_dedup()
    test_concurrent_no_flip_on_inflight_or_failed_emit()
    test_concurrent_retry_redelivers_after_failure()
    test_concurrent_success_single_emit_both_flip()
    test_concurrent_cancel_one_waiter_isolated()
    test_repair_pass_requeries_until_empty()
    test_repair_pass_dedups_against_live()
    test_repair_pass_max_passes_guard()
    test_role_delivery_keys()
    test_drain_row_to_event_shape()
    test_deliver_drain_row_missing_keys_skipped()
    test_lifecycle_start_stop()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
