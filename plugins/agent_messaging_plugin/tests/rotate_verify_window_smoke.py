#!/usr/bin/env python3
"""Unit smoke for rotate_session's verify-window false-negative fix
(2026-08-10). Live-measured defect: a real, HEALTHY production rotation
(gsuite-async, job-2ns5on395r9xz) reported ``status=error
code=verify_timeout`` because the new ``claude_session_id`` landed ~6.7s
after the OLD 60s window's deadline had already given up — a false negative
any job-status-driven automation would be misled by (the plausibility-fence-
below-the-plausible-range class).

Covers ``AgentMessagingPlugin._wait_for_new_claude_session``'s new
post-deadline final re-check (split out as
``_check_for_new_claude_session``): a claude_session_id that appears only
AFTER the poll loop's own deadline must still be observed and returned,
never silently reported as a timeout when the id in fact arrived late but
real.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/rotate_verify_window_smoke.py
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import agent_messaging_plugin.plugin as plugin_module  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

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


def _plugin() -> AgentMessagingPlugin:
    plugin = AgentMessagingPlugin.__new__(AgentMessagingPlugin)
    plugin._choreography_stop_event = threading.Event()  # noqa: SLF001 -- test setup
    return plugin


def _with_fake_mappings(
    fake: Callable[[Any, str], list[dict[str, str]]],
    run: Callable[[], list[str]],
) -> list[str]:
    """Monkeypatch the shared ``lifecycle_list_session_claude_mappings``
    seam for the duration of ``run()``, always restoring it -- mirrors this
    package's existing test-only monkeypatch convention
    (``session_hosts._REGISTRY[...] = fake_driver`` in
    ``session_lifecycle_verbs_smoke.py``), applied to a plain module-level
    import instead of a registry dict."""
    original = plugin_module.lifecycle_list_session_claude_mappings
    plugin_module.lifecycle_list_session_claude_mappings = fake
    try:
        return run()
    finally:
        plugin_module.lifecycle_list_session_claude_mappings = original


def test_wait_for_new_claude_session_finds_id_during_the_poll_loop() -> None:
    """Baseline regression: the ordinary in-window case still works after
    the final-recheck addition."""
    plugin = _plugin()

    def fake(state_service: Any, agent_instance_id: str) -> list[dict[str, str]]:
        del state_service, agent_instance_id
        return [{"claude_session_id": "new-id-during-loop"}]

    result = _with_fake_mappings(
        fake,
        lambda: plugin._wait_for_new_claude_session(  # noqa: SLF001 -- testing directly
            object(), "agi-x", set(), 5.0, 0.01,
        ),
    )
    _check(
        result == ["new-id-during-loop"],
        "an id present on the very first poll is returned immediately, "
        "well within the window",
    )


def test_wait_for_new_claude_session_final_recheck_catches_a_late_arrival() -> None:
    """The exact fix under test: max_wait_seconds<=0 means the poll loop's
    own body never runs at all (the deadline is already past before the
    first iteration check) -- so if the id is still observed, it can ONLY
    have come from the post-loop final re-check, never from the loop.
    Mirrors the live-measured shape: the loop's window expired, but the id
    was real and arrived moments later."""
    plugin = _plugin()

    def fake(state_service: Any, agent_instance_id: str) -> list[dict[str, str]]:
        del state_service, agent_instance_id
        return [{"claude_session_id": "new-id-arrived-late"}]

    result = _with_fake_mappings(
        fake,
        lambda: plugin._wait_for_new_claude_session(  # noqa: SLF001 -- testing directly
            object(), "agi-x", set(), 0.0, 0.01,
        ),
    )
    _check(
        result == ["new-id-arrived-late"],
        "a claude_session_id observed only by the post-deadline final "
        "re-check (the loop body never ran) is still returned -- a healthy "
        "rotation is never reported as verify_timeout just because the id "
        "landed a moment after the window",
    )


def test_wait_for_new_claude_session_returns_empty_when_truly_absent() -> None:
    """The negative control: if the id genuinely never appears (not even on
    the final re-check), the function still returns empty -- the fix adds
    ONE extra chance, not an unconditional success."""
    plugin = _plugin()

    def fake(state_service: Any, agent_instance_id: str) -> list[dict[str, str]]:
        del state_service, agent_instance_id
        return []

    result = _with_fake_mappings(
        fake,
        lambda: plugin._wait_for_new_claude_session(  # noqa: SLF001 -- testing directly
            object(), "agi-x", set(), 0.0, 0.01,
        ),
    )
    _check(
        result == [],
        "a genuinely-absent id still resolves to empty, even with the "
        "final re-check in place -- verify_timeout is still a real, "
        "reachable outcome for an actually-stuck rotation",
    )


def test_rotate_verify_window_raised_with_measured_rationale() -> None:
    _check(
        AgentMessagingPlugin._ROTATE_VERIFY_MAX_WAIT_SECONDS == 300.0,  # noqa: SLF001
        "_ROTATE_VERIFY_MAX_WAIT_SECONDS raised to 300.0s (from the old "
        "60.0s that measurably false-negatived a healthy production "
        "rotation) -- named constant, not a magic number at the call site",
    )


def main() -> int:
    test_wait_for_new_claude_session_finds_id_during_the_poll_loop()
    test_wait_for_new_claude_session_final_recheck_catches_a_late_arrival()
    test_wait_for_new_claude_session_returns_empty_when_truly_absent()
    test_rotate_verify_window_raised_with_measured_rationale()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
