#!/usr/bin/env python3
"""Smoke coverage for the LIVE ``session_context_status_history`` platform
process threading ``peer_registry`` through to the GAU-18 idle/absent split
(GAU-18 rider).

THE DEFECT this closes: ``plugin.py``'s ``session_context_status_history``
call site never passed ``peer_registry`` to
``gauge_series.session_context_status_history``, so the idle/absent split —
built, tested, and landed at ``76d3387f5`` — was UNREACHABLE through the
live verb. ``gauge_series_smoke.py``'s own tests call the pure classifier
and the bare module function directly, handing a registry in by hand;
neither goes through ``AgentMessagingPlugin.session_context_status_history``,
the platform process an actual caller invokes, so neither could have caught
the missing wire. This file drives the plugin's real verb method instead.

★ THE DISCRIMINATOR. A stalled series with NO live watcher-presence binding
must read ABSENT through the verb. If the call site drops ``peer_registry``
(the exact GAU-18 rider regression), ``watcher_present`` stays ``None``
regardless of what the registry actually holds, and the read collapses back
to the old caveat-free IDLE — silently, since IDLE is still a valid state,
just the wrong one.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/session_context_status_history_process_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
)
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.gauge_series import (  # noqa: E402
    GAUGE_SERIES_STALL_S,
    SERIES_ABSENT,
    SERIES_IDLE,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_CONTEXT_STATUS_HISTORY,
    get_peer_binding_schema,
)

_passed = 0
_failed: list[str] = []

LEDGER_ID = "agi-73ba7ce552285765b4716a1059326da0"
SESSION_ID = "ases-agi-73ba7ce552285765b4716a1059326da0"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _OrchStub:
    """Orchestrator stub: only ``state_service`` resolves, exactly what
    ``AgentMessagingPlugin._get_state_service`` reads."""

    def __init__(self, state: Any) -> None:
        self._state = state

    def get_service(self, name: str) -> Any:
        return self._state if name == "state_service" else None


def _stalled_history_row(state: Any, *, recorded_at: str, tokens: int = 10) -> None:
    """A single history row backdated straight into the store, bypassing the
    real-clock-stamping write path. ``_append_history`` always stamps
    ``recorded_at`` from ``datetime.now(UTC)``, and waiting out
    ``GAUGE_SERIES_STALL_S`` (900s) for real is not a smoke's job — the same
    reasoning ``gauge_series_smoke.py``'s own ``_lifecycle_row`` fixture
    applies to ``report_by``."""
    state.write_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_CONTEXT_STATUS_HISTORY,
            "record": {
                "agent_instance_id": LEDGER_ID,
                "claude_session_id": "c-1",
                "agent_session_id": SESSION_ID,
                "model": "claude-opus-5",
                "current_tokens": tokens,
                "ceiling": 1_000_000,
                "measured_at": recorded_at,
                "recorded_at": recorded_at,
            },
        },
    )


def _lifecycle_row(state: Any, *, last_alive: datetime, window_s: int = 5400) -> None:
    """A ``managed_session`` row whose derived last ``report_alive`` is
    ``last_alive`` and whose ``agent_session_id`` is the watcher-presence
    join key — ``_lifecycle_tick`` reads both."""
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "record": {
                "agent_instance_id": LEDGER_ID,
                "agent_session_id": SESSION_ID,
                "lifecycle_state": "live",
                "report_by_seconds": window_s,
                "report_by": (last_alive + timedelta(seconds=window_s)).isoformat(),
                "report_by_source": "explicit_self_report",
                "is_deleted": 0,
            },
            "conflict_columns": ["agent_instance_id"],
        },
    )


def _registry(*, present: bool) -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
    )
    registry = PeerRegistry(bindings_store=store)
    if present:
        registry.register(
            BridgeBinding(
                bridge_id="agc-watch",
                agent_id="claude_code",
                agent_instance_id="agi-watch-anything",
                session_label="watcher",
                parent_pid=1,
                agent_session_id=SESSION_ID,
            ),
        )
    return registry


def _plugin(*, state: Any, registry: PeerRegistry) -> AgentMessagingPlugin:
    plugin = AgentMessagingPlugin()
    plugin.orchestrator_ref = _OrchStub(state)  # type: ignore[attr-defined]
    plugin._peer_registry = registry  # noqa: SLF001
    return plugin


def _stalled_fixture(*, watcher_present: bool) -> AgentMessagingPlugin:
    state = RealShapeState()
    stalled = datetime.now(UTC) - timedelta(seconds=GAUGE_SERIES_STALL_S + 120)
    _stalled_history_row(state, recorded_at=stalled.isoformat())
    _lifecycle_row(state, last_alive=stalled)
    return _plugin(state=state, registry=_registry(present=watcher_present))


def _call(plugin: AgentMessagingPlugin) -> dict[str, Any]:
    return plugin.session_context_status_history(
        {"parameters": {"agent_instance_id": LEDGER_ID}}, {},
    )


# ---------------------------------------------------------------------------
# The verb, not the classifier — this is the whole point of the file.
# ---------------------------------------------------------------------------


def test_verb_reads_absent_when_no_watcher_binding_exists() -> None:
    """★ THE DISCRIMINATOR. CATCHES: ``plugin.py``'s call site dropping
    ``peer_registry`` — the exact GAU-18 rider regression. Pre-fix this reads
    IDLE (``watcher_present`` silently ``None``) even though the registry
    handed to the plugin carries no binding for this session at all."""
    result = _call(_stalled_fixture(watcher_present=False))
    _check(result["action_status"] == "completed", "the call itself succeeds")
    data = result["data"]
    _check(
        data["series_state"] == SERIES_ABSENT,
        f"a stalled series with NO watcher binding reads ABSENT through the "
        f"live verb (got {data.get('series_state')!r}) — proves peer_registry "
        "reached the classifier, not just that the classifier can do this "
        "when called directly",
    )


def test_verb_reads_idle_with_caveat_when_watcher_binding_is_live() -> None:
    """The other half of the split, same call path: a LIVE binding reads
    IDLE, not ABSENT, and the reason line still carries the mandatory
    two-situations caveat (coordinating-seat ruling, 2026-08-20)."""
    result = _call(_stalled_fixture(watcher_present=True))
    data = result["data"]
    _check(data["series_state"] == SERIES_IDLE, "a live watcher binding reads IDLE, not ABSENT")
    _check(
        "STILL COVERS TWO SITUATIONS" in data["series_state_reason"],
        "the mandatory caveat survives through the live verb, not just the "
        "pure classifier",
    )


def main() -> int:
    tests = (
        test_verb_reads_absent_when_no_watcher_binding_exists,
        test_verb_reads_idle_with_caveat_when_watcher_binding_is_live,
    )
    print("=== session_context_status_history platform-process smoke (GAU-18 rider) ===")
    for test in tests:
        test()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
