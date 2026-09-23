#!/usr/bin/env python3
"""W3 — dispatch refuses to claim `queued_watcher` against a silent bridge.

`append_event` succeeds against ANY non-closed bridge session, and a
SIGKILLed watcher leaves its server-side session standing. So the send is
labelled `queued_watcher` — "a queue accepted it" — while nothing will ever poll
that queue, for up to the full 3_600s idle sweep. The label is not merely
unhelpful, it is false.

The SAFETY half is the priority half. A gate that over-fires would stop
delivering to healthy watchers, which is a worse regression than §34.1 ever
was — so every "stale is refused" case here is paired with a "fresh still
delivers" control, and the knob itself is exercised with a NON-DEFAULT value so
a wiring regression cannot hide behind the default.

Project policy: stdlib-only, no pytest. Run with::

    python3 plugins/agent_messaging_plugin/tests/watcher_liveness_gate_smoke.py
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_messaging_plugin.bridge_sessions import (  # noqa: E402
    DEFAULT_BINDING_LIVENESS_WINDOW_S,
    BridgeSessionManager,
)
from agent_messaging_plugin.models import (  # noqa: E402
    WATCH_AGENT_INSTANCE_PREFIX,
    BridgeBinding,
)
from agent_messaging_plugin.peer_dispatch import binding_is_live  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str, detail: str = "") -> bool:
    global _passed
    if condition:
        _passed += 1
        return True
    _failed.append(f"{label}: {detail}" if detail else label)
    print(f"  FAIL  {_failed[-1]}")
    return False


def _Binding(bridge_id: str, *, is_watcher: bool) -> BridgeBinding:  # noqa: N802
    """A REAL BridgeBinding — `is_watcher` is derived from the instance-id
    prefix, so a hand-rolled stub could assert liveness against a shape the
    registry never produces."""
    instance = (
        f"{WATCH_AGENT_INSTANCE_PREFIX}testdigest" if is_watcher else "agi-mcp-test"
    )
    return BridgeBinding(
        bridge_id=bridge_id,
        agent_id="claude_code",
        agent_instance_id=instance,
        session_label="Watcher-Test",
        parent_pid=None,
    )


def _manager(window_s: int | None = None) -> BridgeSessionManager:
    kwargs: dict[str, Any] = {
        "session_id_factory": lambda _n: "ases-test",
        "idle_timeout_s": 3600,
        "max_pending_events": 200,
        "long_poll_timeout_s": 25,
    }
    if window_s is not None:
        kwargs["binding_liveness_window_s"] = window_s
    return BridgeSessionManager(**kwargs)


def _aged_bridge(
    manager: BridgeSessionManager,
    seconds_ago: float,
    *,
    now: datetime | None = None,
) -> str:
    bridge = manager.open("testhome")
    bridge.last_seen_at = (
        (now or datetime.now(UTC)) - timedelta(seconds=seconds_ago)
    ).isoformat()
    return bridge.bridge_id


def case_fresh_watcher_is_live() -> None:
    """THE SAFETY CONTROL: a healthy watcher must still read as live.

    Without this, a gate that refused every watcher would satisfy every
    staleness assertion below while silently ending delivery to working
    sessions.
    """
    manager = _manager()
    for age in (0.0, 5.0, 30.0, float(DEFAULT_BINDING_LIVENESS_WINDOW_S - 1)):
        binding = _Binding(_aged_bridge(manager, age), is_watcher=True)
        _check(
            binding_is_live(
                bridge_manager=manager,
                binding=binding,
                window_seconds=DEFAULT_BINDING_LIVENESS_WINDOW_S,
            ),
            f"a watcher polled {age}s ago is LIVE",
            "the gate would have refused a healthy watcher",
        )


def case_stale_watcher_is_not_live() -> None:
    """The feature: past the window, the binding reads dead."""
    manager = _manager()
    for age in (
        float(DEFAULT_BINDING_LIVENESS_WINDOW_S + 1),
        300.0,
        3600.0,
    ):
        binding = _Binding(_aged_bridge(manager, age), is_watcher=True)
        _check(
            not binding_is_live(
                bridge_manager=manager,
                binding=binding,
                window_seconds=DEFAULT_BINDING_LIVENESS_WINDOW_S,
            ),
            f"a watcher last polled {age}s ago is NOT live",
        )


def case_boundary_is_inclusive() -> None:
    """Exactly at the window is still live — pins the comparison operator.

    Without both sides, `>` and `>=` are indistinguishable and the window's
    stated meaning drifts by one second per refactor.
    """
    manager = _manager()
    # `now` is pinned rather than read from the wall clock: with a live clock
    # the "exactly at the window" case is decided by however many microseconds
    # the call took, which makes the boundary assertion a coin flip.
    pinned = datetime.now(UTC)
    at = _Binding(_aged_bridge(manager, 10.0, now=pinned), is_watcher=True)
    _check(
        binding_is_live(
            bridge_manager=manager, binding=at, window_seconds=10, now=pinned,
        ),
        "age exactly == window is LIVE (inclusive)",
    )
    past = _Binding(_aged_bridge(manager, 10.5, now=pinned), is_watcher=True)
    _check(
        not binding_is_live(
            bridge_manager=manager, binding=past, window_seconds=10, now=pinned,
        ),
        "age just past the window is NOT live",
    )


def case_missing_and_closed_bridges_are_not_live() -> None:
    """A bridge that is gone or closed cannot be polled by anyone."""
    manager = _manager()
    _check(
        not binding_is_live(
            bridge_manager=manager,
            binding=_Binding("agc-does-not-exist", is_watcher=True),
            window_seconds=DEFAULT_BINDING_LIVENESS_WINDOW_S,
        ),
        "a missing bridge is NOT live",
    )
    bridge_id = _aged_bridge(manager, 0.0)
    bridge = manager.get(bridge_id)
    assert bridge is not None
    bridge.closed = True
    _check(
        not binding_is_live(
            bridge_manager=manager,
            binding=_Binding(bridge_id, is_watcher=True),
            window_seconds=DEFAULT_BINDING_LIVENESS_WINDOW_S,
        ),
        "a closed-but-present bridge is NOT live",
    )


def case_unparseable_timestamp_is_not_live() -> None:
    """Liveness that cannot be established is not liveness.

    Failing OPEN here would tell a sender a bridge is reachable on the strength
    of a corrupt field — the one direction that reintroduces the false
    `queued_watcher` this gate exists to remove.
    """
    manager = _manager()
    bridge_id = _aged_bridge(manager, 0.0)
    bridge = manager.get(bridge_id)
    assert bridge is not None
    bridge.last_seen_at = "not-a-timestamp"
    _check(
        not binding_is_live(
            bridge_manager=manager,
            binding=_Binding(bridge_id, is_watcher=True),
            window_seconds=DEFAULT_BINDING_LIVENESS_WINDOW_S,
        ),
        "an unparseable last_seen_at is NOT live (fails closed)",
    )


def case_configured_window_is_honoured() -> None:
    """THE KNOB'S OWN TEST — a NON-DEFAULT window must change the verdict.

    Deliberately uses a value the default cannot produce: at 300s a bridge aged
    200s is live, while under the 90s default it would be dead. If the wiring
    from config to the manager is ever dropped, the manager falls back to 90 and
    this case goes red — which is the whole point of not asserting against the
    default.
    """
    wide = _manager(300)
    aged_200 = _Binding(_aged_bridge(wide, 200.0), is_watcher=True)
    _check(
        wide.binding_liveness_window_s == 300,
        "manager carries the configured window",
        f"got {wide.binding_liveness_window_s}",
    )
    _check(
        binding_is_live(
            bridge_manager=wide,
            binding=aged_200,
            window_seconds=wide.binding_liveness_window_s,
        ),
        "under a 300s window, a 200s-idle bridge is LIVE",
        "the configured window was not honoured",
    )

    narrow = _manager(30)
    aged_60 = _Binding(_aged_bridge(narrow, 60.0), is_watcher=True)
    _check(
        not binding_is_live(
            bridge_manager=narrow,
            binding=aged_60,
            window_seconds=narrow.binding_liveness_window_s,
        ),
        "under a 30s window, a 60s-idle bridge is NOT live",
    )
    # Both directions differ from the 90s default, so neither verdict could be
    # produced by an unwired manager silently using the default.
    _check(
        DEFAULT_BINDING_LIVENESS_WINDOW_S not in (300, 30),
        "the fixture's windows are genuinely non-default",
    )


def case_non_watcher_bindings_are_out_of_scope() -> None:
    """The dispatch gate is watcher-scoped; MCP bindings are untouched here.

    `binding_is_live` itself is transport-neutral (WS-2e §4.3.2 reuses it), so
    what is asserted is that it answers about the BRIDGE and leaves the
    is_watcher decision to the caller — which is why a stale non-watcher
    binding still reports not-live but is never gated by dispatch.
    """
    manager = _manager()
    stale = _Binding(_aged_bridge(manager, 500.0), is_watcher=False)
    _check(
        not binding_is_live(
            bridge_manager=manager,
            binding=stale,
            window_seconds=DEFAULT_BINDING_LIVENESS_WINDOW_S,
        ),
        "liveness is a property of the BRIDGE, not of is_watcher",
    )


def main() -> int:
    print("agent_messaging — watcher liveness gate (WS-2a W3)")
    print("=" * 68)
    for case in (
        case_fresh_watcher_is_live,
        case_stale_watcher_is_not_live,
        case_boundary_is_inclusive,
        case_missing_and_closed_bridges_are_not_live,
        case_unparseable_timestamp_is_not_live,
        case_configured_window_is_honoured,
        case_non_watcher_bindings_are_out_of_scope,
    ):
        case()
    print("-" * 68)
    if _failed:
        print(f"{_passed} passed, {len(_failed)} FAILED")
        return 1
    print(f"{_passed} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
