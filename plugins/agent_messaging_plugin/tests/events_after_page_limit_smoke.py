#!/usr/bin/env python3
"""iss_c93a9b2f: bound the per-call burst size of ``events_after``.

Run with:

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/events_after_page_limit_smoke.py

Before this fix, ``BridgeSessionState.events_after`` returned every pending
event for a bridge in one call -- the only bound was the overall queue-full
cap (``max_pending_events``). A role holder that fell behind could have its
entire backlog (up to that cap) delivered to the MCP client in one
uninterrupted burst, which contributed to killing a session under real fleet
load. This smoke pins: (1) a burst larger than the page limit is delivered
across multiple calls, oldest-first, never fewer than the limit at a time
while more remain; (2) nothing is lost or reordered across pages; (3) the
existing ``acked``/cursor-advance contract (unaffected rows, ``after``
semantics) still holds; (4) ``limit=None`` restores the old unbounded
behavior for any caller that still needs it.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_messaging_plugin.models import EVENTS_AFTER_PAGE_LIMIT, BridgeSessionState

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


def _bridge() -> BridgeSessionState:
    return BridgeSessionState(bridge_id="agc-page-limit-test", session_id="ses-page-limit-test")


def test_burst_larger_than_limit_pages_across_calls() -> None:
    """Drive ``events_after`` directly, advancing ``after`` to the last
    returned cursor each call (this method's own contract: acked = cursor <=
    after). The forwarder's HTTP-route caller layers a separate one-event
    "-1" re-confirm overlap on top of this for at-least-once safety across
    reconnects (see http_routes.py) -- that belongs to that layer's own
    tests, not this one, so it is deliberately not reproduced here.
    """
    bridge = _bridge()
    burst_size = EVENTS_AFTER_PAGE_LIMIT * 2 + 7  # deliberately not a clean multiple
    for i in range(burst_size):
        bridge.append_event("post_message", f"msg-{i}")

    seen_content: list[str] = []
    after = -1
    pages = 0
    # Bounded loop: real usage re-polls; a correct implementation drains in
    # ceil(burst_size / limit) calls, never fewer, never hanging forever.
    for _ in range(burst_size + 5):
        acked, pending = bridge.events_after(after, limit=EVENTS_AFTER_PAGE_LIMIT)
        del acked
        if not pending:
            break
        pages += 1
        _check(
            len(pending) <= EVENTS_AFTER_PAGE_LIMIT,
            f"page {pages} carries at most {EVENTS_AFTER_PAGE_LIMIT} events "
            f"(got {len(pending)})",
        )
        seen_content.extend(e.content for e in pending)
        after = pending[-1].cursor

    expected = [f"msg-{i}" for i in range(burst_size)]
    _check(
        seen_content == expected,
        "every event delivered exactly once, in order, across all pages",
    )
    _check(
        pages >= 2,
        f"a burst of {burst_size} with page limit {EVENTS_AFTER_PAGE_LIMIT} "
        f"took multiple calls (got {pages})",
    )


def test_small_page_returned_whole_no_extra_pages() -> None:
    bridge = _bridge()
    for i in range(3):
        bridge.append_event("post_message", f"small-{i}")
    acked, pending = bridge.events_after(-1, limit=EVENTS_AFTER_PAGE_LIMIT)
    _check(acked == [], "no acked rows on a first call with after=-1")
    _check(
        [e.content for e in pending] == ["small-0", "small-1", "small-2"],
        "a page under the limit is returned whole, in order",
    )


def test_limit_none_restores_unbounded_behavior() -> None:
    bridge = _bridge()
    burst_size = EVENTS_AFTER_PAGE_LIMIT + 10
    for i in range(burst_size):
        bridge.append_event("post_message", f"unbounded-{i}")
    _, pending = bridge.events_after(-1, limit=None)
    _check(
        len(pending) == burst_size,
        f"limit=None returns the full backlog in one call ({len(pending)}/{burst_size})",
    )


def test_default_limit_applies_without_explicit_argument() -> None:
    """Callers that don't pass ``limit`` (none currently do) still get the bound."""
    bridge = _bridge()
    burst_size = EVENTS_AFTER_PAGE_LIMIT + 5
    for i in range(burst_size):
        bridge.append_event("post_message", f"default-{i}")
    _, pending = bridge.events_after(-1)
    _check(
        len(pending) == EVENTS_AFTER_PAGE_LIMIT,
        "omitting limit uses EVENTS_AFTER_PAGE_LIMIT, not unbounded",
    )


def test_acked_and_unreturned_pending_both_stay_correct() -> None:
    """Rows past the page limit remain legitimately un-acked, not dropped."""
    bridge = _bridge()
    for i in range(EVENTS_AFTER_PAGE_LIMIT + 3):
        bridge.append_event("post_message", f"ov-{i}")
    _, first_page = bridge.events_after(-1, limit=EVENTS_AFTER_PAGE_LIMIT)
    _check(len(first_page) == EVENTS_AFTER_PAGE_LIMIT, "first page hits the limit exactly")
    # The 3 overflow rows must still be present un-acked (not silently dropped).
    _check(
        bridge.pending_event_count() == EVENTS_AFTER_PAGE_LIMIT + 3,
        "rows past the page limit stay in pending_events (nothing dropped)",
    )
    next_after = first_page[-1].cursor
    acked_second, second_page = bridge.events_after(
        next_after, limit=EVENTS_AFTER_PAGE_LIMIT,
    )
    _check(
        len(second_page) == 3,
        f"second call returns exactly the 3 overflow rows (got {len(second_page)})",
    )
    _check(
        len(acked_second) == EVENTS_AFTER_PAGE_LIMIT,
        "second call acks (drains) every row already confirmed by page 1",
    )


def main() -> int:
    print("=== events_after page-limit smoke (iss_c93a9b2f) ===")
    test_burst_larger_than_limit_pages_across_calls()
    test_small_page_returned_whole_no_extra_pages()
    test_limit_none_restores_unbounded_behavior()
    test_default_limit_applies_without_explicit_argument()
    test_acked_and_unreturned_pending_both_stay_correct()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
