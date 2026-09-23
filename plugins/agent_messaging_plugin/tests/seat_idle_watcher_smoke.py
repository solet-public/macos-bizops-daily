#!/usr/bin/env python3
"""Unit smoke for ``seat_idle_watcher.py`` (rotation-systematization P4(b),
P4(a) ratification 2026-08-07, six bound conditions). Offline, stub-driven --
no real iTerm2 connection, no real ``solet``/fleet call (matching this
repo's existing ``seat_rotation_helper_smoke.py`` convention).

Proves: idle-seconds computation is 0 whenever status != "idle" and never
negative; the poke/rotate/none precedence decision (P4(a).4, condition 4)
matches its ratified rules in every branch, INCLUDING the load-bearing one
-- pending mail defers rotate even past the rotate threshold, and poke wins
over rotate whenever both conditions could apply; poke cooldown boundaries
(condition 4's 30-min cooldown); the cross-check fails OPEN (trusts the
status file alone) when the transcript is unreadable but fails CLOSED
(refuses to trust idle) when the two signals actively disagree; the
ACT-time gate (condition 5) refuses on both a non-empty composer and a
never-stabilizing screen, and NEVER lets ``run_rotation`` fire when it
refuses; and the hard privacy rule (condition 2) -- the poke text sent is
EXACTLY the fixed constant, byte-for-byte, even when the underlying
``solet`` envelope carries a distinctive marker string in a message
body, proving the pending-count extraction never lets body content reach
the injected text.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/seat_idle_watcher_smoke.py
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import seat_idle_watcher as watcher  # noqa: E402

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


# ─── Stub iterm2-shaped fixtures (mirrors seat_rotation_helper_smoke.py) ─────


class _StubLine:
    def __init__(self, string: str) -> None:
        self.string = string


class _StubScreenContents:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    @property
    def number_of_lines(self) -> int:
        return len(self._lines)

    def line(self, index: int) -> _StubLine:
        return _StubLine(self._lines[index])


class _StubSession:
    def __init__(self, *, role: str, session_id: str, screen_lines_fn: Any) -> None:
        self.role = role
        self.session_id = session_id
        self._screen_lines_fn = screen_lines_fn
        self.get_screen_calls = 0

    async def async_get_variable(self, _name: str) -> str:
        return self.role

    async def async_get_screen_contents(self) -> _StubScreenContents:
        self.get_screen_calls += 1
        return _StubScreenContents(self._screen_lines_fn(self))


class _StubTab:
    def __init__(self, tab_id: int, sessions: list[_StubSession]) -> None:
        self.tab_id = tab_id
        self.sessions = sessions


class _StubWindow:
    def __init__(self, window_id: int, tabs: list[_StubTab]) -> None:
        self.window_id = window_id
        self.tabs = tabs


class _StubApp:
    def __init__(self, windows: list[_StubWindow]) -> None:
        self.windows = windows


class _StubIterm:
    def __init__(self, app: _StubApp | None) -> None:
        self._app = app

        class _Connection:
            @staticmethod
            async def async_create() -> object:
                return object()

        self.Connection = _Connection

    async def _async_get_app(self, _connection: object) -> _StubApp | None:
        return self._app


def _one_pane_app(session: _StubSession) -> _StubApp:
    return _StubApp([_StubWindow(1, [_StubTab(1, [session])])])


def _patched_iterm(app: _StubApp | None) -> Any:
    """Context-manager-free patch helper matching the sibling smoke's
    ``_run_with_stub`` shape, adapted for this module's own ``_iterm``."""
    stub = _StubIterm(app)
    original = watcher._iterm
    watcher._iterm = stub
    watcher._iterm.async_get_app = stub._async_get_app
    return original


def _restore_iterm(original: Any) -> None:
    watcher._iterm = original


# ─── resolve_seat_identity — the live-env-shape regression (fix loop #3) ─────


def test_resolve_seat_identity_tolerates_absent_agent_instance_id() -> None:
    """RED-FIRST regression against the exact live-fire failure (fix loop
    #3, 2026-08-07): the REAL seat's process env carries AGENT_SESSION_ID
    but NOT AGENT_INSTANCE_ID (measured, disclosed 00:39Z). The 42
    pre-existing assertions all supplied fixtures where BOTH vars are
    present, so none of them could see this gap -- this fixture matches the
    live env's ACTUAL shape, not the idealized one."""
    with tempfile.TemporaryDirectory() as tmp:
        session_file = Path(tmp) / "39546.json"
        session_file.write_text(
            '{"name": "Coordinator-Main", "sessionId": "cs-real", "status": "idle", "statusUpdatedAt": 123456.0}',
        )
        original_find = watcher.find_session_file
        original_env = watcher.read_process_env_var
        watcher.find_session_file = lambda _role_tag: session_file  # type: ignore[assignment]

        def _fake_env(_pid: int, name: str) -> str | None:
            return "ases-real" if name == watcher._AGENT_SESSION_ID_ENV else None  # noqa: SLF001

        watcher.read_process_env_var = _fake_env  # type: ignore[assignment]
        try:
            identity = watcher.resolve_seat_identity("Coordinator-Main")
        finally:
            watcher.find_session_file = original_find
            watcher.read_process_env_var = original_env
    _check(identity is not None, "resolve_seat_identity succeeds when AGENT_INSTANCE_ID is absent (the real seat's env shape)")
    _check(identity is not None and identity.agent_session_id == "ases-real", "agent_session_id is still correctly resolved")
    _check(identity is not None and identity.agent_instance_id is None, "agent_instance_id is None, never fabricated")


def test_resolve_seat_identity_still_requires_agent_session_id() -> None:
    """The other half of the same boundary: AGENT_SESSION_ID is the one
    hard requirement (peer_inbox is keyed on it) -- its absence must still
    fail resolution, never silently proceed with a missing/fabricated
    session id."""
    with tempfile.TemporaryDirectory() as tmp:
        session_file = Path(tmp) / "39546.json"
        session_file.write_text(
            '{"name": "Coordinator-Main", "sessionId": "cs-real", "status": "idle", "statusUpdatedAt": 123456.0}',
        )
        original_find = watcher.find_session_file
        original_env = watcher.read_process_env_var
        watcher.find_session_file = lambda _role_tag: session_file  # type: ignore[assignment]
        watcher.read_process_env_var = lambda _pid, _name: None  # type: ignore[assignment]
        try:
            identity = watcher.resolve_seat_identity("Coordinator-Main")
        finally:
            watcher.find_session_file = original_find
            watcher.read_process_env_var = original_env
    _check(identity is None, "resolve_seat_identity still refuses when AGENT_SESSION_ID itself is absent")


def test_state_path_uses_explicit_placeholder_when_agent_instance_id_is_none() -> None:
    path_with_none = watcher._state_path(Path("/tmp/marker"), None, "cs-real")  # noqa: SLF001
    path_with_value = watcher._state_path(Path("/tmp/marker"), "agi-x", "cs-real")  # noqa: SLF001
    _check(
        watcher._NO_AGENT_INSTANCE_ID_PLACEHOLDER in path_with_none.name,  # noqa: SLF001
        "the state-file name carries the explicit, honestly-labeled placeholder when agent_instance_id is None",
    )
    _check(path_with_none != path_with_value, "a None agent_instance_id never collides with a real one's state file")


# ─── compute_idle_seconds ────────────────────────────────────────────────────


def _identity(*, status: str, status_updated_at_ms: float) -> watcher.SeatIdentity:
    return watcher.SeatIdentity(
        pid=1, claude_session_id="cs1", agent_session_id="ases-x",
        agent_instance_id="agi-x", status=status,
        status_updated_at_ms=status_updated_at_ms, session_label="Coordinator-Main",
    )


def test_compute_idle_seconds_zero_when_not_idle() -> None:
    identity = _identity(status="working", status_updated_at_ms=0.0)
    _check(
        watcher.compute_idle_seconds(identity, now_ms=999_999.0) == 0.0,
        "compute_idle_seconds is 0.0 whenever status != 'idle'",
    )


def test_compute_idle_seconds_positive_when_idle() -> None:
    identity = _identity(status="idle", status_updated_at_ms=1_000.0)
    _check(
        watcher.compute_idle_seconds(identity, now_ms=1_000.0 + 90_000.0) == 90.0,
        "compute_idle_seconds returns the correct elapsed seconds when idle",
    )


def test_compute_idle_seconds_never_negative() -> None:
    identity = _identity(status="idle", status_updated_at_ms=50_000.0)
    _check(
        watcher.compute_idle_seconds(identity, now_ms=1_000.0) == 0.0,
        "compute_idle_seconds clamps to 0.0 rather than going negative "
        "(now_ms before status_updated_at_ms -- a clock-skew edge case)",
    )


# ─── project_dir_slug_for / transcript_path_for ──────────────────────────────


def test_project_dir_slug_matches_measured_convention() -> None:
    _check(
        watcher.project_dir_slug_for(Path("/Users/alice/Workspace/solet")) == "-Users-alice-Workspace-solet",
        "project_dir_slug_for matches the live-measured Claude Code transcript-dir convention",
    )


def test_transcript_path_for_builds_expected_path() -> None:
    expected = Path.home() / ".claude" / "projects" / "-Users-alice-Workspace-solet" / "abc123.jsonl"
    _check(
        watcher.transcript_path_for("-Users-alice-Workspace-solet", "abc123") == expected,
        "transcript_path_for builds the expected transcript path",
    )


# ─── cross_check_idle ─────────────────────────────────────────────────────────


def test_cross_check_idle_agrees_within_tolerance() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "t.jsonl"
        transcript.write_text("{}")
        mtime = transcript.stat().st_mtime
        identity = _identity(status="idle", status_updated_at_ms=mtime * 1000.0)
        _check(
            watcher.cross_check_idle(identity, transcript),
            "cross_check_idle agrees when transcript mtime matches statusUpdatedAt",
        )


def test_cross_check_idle_disagrees_when_transcript_is_newer() -> None:
    """The ONLY real contradiction: the transcript was written AFTER the
    status file claimed idle, i.e. the session kept working while calling
    itself idle."""
    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "t.jsonl"
        transcript.write_text("{}")
        mtime = transcript.stat().st_mtime
        identity = _identity(status="idle", status_updated_at_ms=(mtime - 3600) * 1000.0)
        _check(
            watcher.cross_check_idle(identity, transcript) is False,
            "cross_check_idle fails CLOSED (returns False) when the transcript "
            "is NEWER than the idle stamp beyond tolerance -- never trusts an "
            "idle claim the session's own writes contradict",
        )


def test_cross_check_idle_accepts_transcript_older_than_stamp() -> None:
    """The bounded-waiter case, and the reason this guard is one-sided: the
    coordination Stop hook holds the turn boundary for a bounded interval, so
    idle is stamped minutes after the turn's last transcript write. A quieter-
    than-claimed transcript corroborates idleness; the old symmetric abs()
    form rejected it and skipped every genuinely idle tick."""
    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "t.jsonl"
        transcript.write_text("{}")
        mtime = transcript.stat().st_mtime
        identity = _identity(status="idle", status_updated_at_ms=(mtime + 2400) * 1000.0)
        _check(
            watcher.cross_check_idle(identity, transcript),
            "cross_check_idle AGREES when the transcript is older than the idle "
            "stamp (bounded wake_waiter): quiet-for-longer corroborates idle",
        )


def test_cross_check_idle_missing_transcript_fails_open() -> None:
    identity = _identity(status="idle", status_updated_at_ms=0.0)
    _check(
        watcher.cross_check_idle(identity, Path("/nonexistent/does/not/exist.jsonl")),
        "cross_check_idle fails OPEN (trusts the status file alone) when the "
        "transcript can't be read -- nothing to disagree with",
    )


# ─── decide_action — the P4(a).4 precedence, verbatim ────────────────────────


def test_decide_action_poke_when_pending_and_idle_past_poke_threshold() -> None:
    decision = watcher.decide_action(idle_seconds=400, pending_count=2, poke_on_cooldown=False)
    _check(decision.action == watcher.ACTION_POKE, "poke fires: pending + idle past poke threshold")


def test_decide_action_rotate_when_idle_past_rotate_threshold_and_nothing_pending() -> None:
    decision = watcher.decide_action(idle_seconds=2200, pending_count=0, poke_on_cooldown=False)
    _check(decision.action == watcher.ACTION_ROTATE, "rotate fires: idle past rotate threshold, nothing pending")


def test_decide_action_none_below_both_thresholds() -> None:
    decision = watcher.decide_action(idle_seconds=100, pending_count=0, poke_on_cooldown=False)
    _check(decision.action == watcher.ACTION_NONE, "no action below both thresholds")


def test_decide_action_pending_defers_rotate_even_past_rotate_threshold_when_poke_on_cooldown() -> None:
    """The single most load-bearing precedence assertion (P4(a).4, verbatim:
    "rotate only fires when idle alone crosses its threshold with NOTHING
    pending to poke about first"). Idle is well past the rotate threshold,
    but mail is still pending and poke is on cooldown -- must NOT rotate."""
    decision = watcher.decide_action(idle_seconds=5000, pending_count=1, poke_on_cooldown=True)
    _check(
        decision.action == watcher.ACTION_NONE,
        "pending mail defers rotate even far past the rotate threshold, when poke is on cooldown",
    )


def test_decide_action_poke_wins_over_rotate_when_both_thresholds_crossed() -> None:
    """The other half of the same precedence rule: when BOTH conditions
    could apply (idle past rotate threshold too) and poke is NOT on
    cooldown, poke -- not rotate -- fires."""
    decision = watcher.decide_action(idle_seconds=5000, pending_count=1, poke_on_cooldown=False)
    _check(
        decision.action == watcher.ACTION_POKE,
        "poke wins over rotate when both conditions could apply and poke is not on cooldown",
    )


def test_decide_action_exact_boundary_poke_threshold_is_due() -> None:
    decision = watcher.decide_action(
        idle_seconds=300, pending_count=1, poke_on_cooldown=False,
        idle_poke_threshold_seconds=300, idle_rotate_threshold_seconds=2100,
    )
    _check(decision.action == watcher.ACTION_POKE, "exactly at the poke threshold -> due (kills a strict > mutation)")


def test_decide_action_exact_boundary_rotate_threshold_is_due() -> None:
    decision = watcher.decide_action(
        idle_seconds=2100, pending_count=0, poke_on_cooldown=False,
        idle_poke_threshold_seconds=300, idle_rotate_threshold_seconds=2100,
    )
    _check(decision.action == watcher.ACTION_ROTATE, "exactly at the rotate threshold -> due (kills a strict > mutation)")


# ─── is_poke_on_cooldown ──────────────────────────────────────────────────────


def test_is_poke_on_cooldown_none_is_false() -> None:
    _check(
        watcher.is_poke_on_cooldown(None, now=1000.0) is False,
        "never poked before -> not on cooldown",
    )


def test_is_poke_on_cooldown_recent_is_true() -> None:
    _check(
        watcher.is_poke_on_cooldown(1000.0, now=1100.0, cooldown_seconds=1800) is True,
        "poked 100s ago with a 1800s cooldown -> still on cooldown",
    )


def test_is_poke_on_cooldown_past_window_is_false() -> None:
    _check(
        watcher.is_poke_on_cooldown(1000.0, now=1000.0 + 1801.0, cooldown_seconds=1800) is False,
        "poked longer ago than the cooldown window -> no longer on cooldown",
    )


def test_is_poke_on_cooldown_exact_boundary_is_false() -> None:
    _check(
        watcher.is_poke_on_cooldown(1000.0, now=1000.0 + 1800.0, cooldown_seconds=1800) is False,
        "exactly at the cooldown boundary -> no longer on cooldown (strict <, kills an off-by-one)",
    )


# ─── resolve_pending_count — structural privacy guarantee ────────────────────


def test_resolve_pending_count_extracts_only_the_length() -> None:
    original = watcher._solet_inbox
    marker = "SECRET_INBOX_BODY_MARKER_ALPHA"
    try:
        watcher._solet_inbox = lambda: {  # type: ignore[assignment]
            "complete": True,
            "messages": [{"message": {"content": [{"text": marker}]}}],
            "role_count": 2,
        }
        count = watcher.resolve_pending_count("ases-x")
        _check(count == 2, "resolve_pending_count returns the merged role count")
        _check(isinstance(count, int), "resolve_pending_count's return type is a bare int")
    finally:
        watcher._solet_inbox = original


def test_resolve_pending_count_none_on_call_failure() -> None:
    original = watcher._solet_inbox
    try:
        watcher._solet_inbox = lambda: None  # type: ignore[assignment]
        _check(
            watcher.resolve_pending_count("ases-x") is None,
            "resolve_pending_count returns None on a failed solet call, never a guess",
        )
    finally:
        watcher._solet_inbox = original


# ─── _confirm_pane_ready_for_action — condition 5's ACT-time gate ────────────


def test_act_time_gate_refuses_when_composer_not_cleared() -> None:
    """settle_timeout_seconds=3.0 gives real margin over the 3-sample x
    0.5s-poll-interval floor (~1.0s) so this never races the timeout --
    the screen IS stable here (constant lines), only the composer content
    is the thing under test."""
    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines_fn=lambda _s: ["❯\xa0/still-typing"])
    original = _patched_iterm(_one_pane_app(session))
    try:
        ready, reason = asyncio.run(
            watcher._confirm_pane_ready_for_action("Coordinator-Main", "❯", settle_timeout_seconds=3.0),
        )
    finally:
        _restore_iterm(original)
    _check(ready is False, "ACT-time gate refuses when the composer is not confirmed empty")
    _check("composer" in reason, "refusal reason names the composer check")


def test_act_time_gate_succeeds_when_stable_and_cleared() -> None:
    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines_fn=lambda _s: ["❯"])
    original = _patched_iterm(_one_pane_app(session))
    try:
        ready, _reason = asyncio.run(
            watcher._confirm_pane_ready_for_action("Coordinator-Main", "❯", settle_timeout_seconds=3.0),
        )
    finally:
        _restore_iterm(original)
    _check(ready is True, "ACT-time gate confirms ready on a stable, positively-cleared composer")


def test_act_time_gate_refuses_when_screen_never_stabilizes() -> None:
    counter = {"n": 0}

    def _never_stable(_session: _StubSession) -> list[str]:
        counter["n"] += 1
        return [f"❯ changing-{counter['n']}"]

    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines_fn=_never_stable)
    original = _patched_iterm(_one_pane_app(session))
    try:
        ready, reason = asyncio.run(
            watcher._confirm_pane_ready_for_action(
                "Coordinator-Main", "❯", settle_timeout_seconds=0.3,
            ),
        )
    finally:
        _restore_iterm(original)
    _check(ready is False, "ACT-time gate refuses when the screen never stabilizes (fail-closed timeout)")
    _check("stable" in reason, "refusal reason names the stability check")


# ─── run_tick — integration of the whole decision + gate + act sequence ─────


def _patch(obj: Any, name: str, value: Any) -> Any:
    original = getattr(obj, name)
    setattr(obj, name, value)
    return original


def _run_tick_with_stubs(
    *, identity: watcher.SeatIdentity | None, pending_count: int | None,
    gate_result: tuple[bool, str], record_calls: list[tuple[str, str, dict[str, Any]]],
    marker_dir: Path, now: float,
) -> dict[str, Any]:
    originals: dict[str, Any] = {}
    originals["resolve_seat_identity"] = _patch(watcher, "resolve_seat_identity", lambda _role: identity)
    originals["resolve_pending_count"] = _patch(watcher, "resolve_pending_count", lambda _sid: pending_count)
    originals["cross_check_idle"] = _patch(watcher, "cross_check_idle", lambda *_a, **_k: True)

    async def _fake_gate(_role: str, _sig: str, *, settle_timeout_seconds: float) -> tuple[bool, str]:
        return gate_result

    originals["_confirm_pane_ready_for_action"] = _patch(watcher, "_confirm_pane_ready_for_action", _fake_gate)

    async def _fake_run_rotation(role_tag: str, text: str, **kwargs: Any) -> dict[str, Any]:
        record_calls.append((role_tag, text, kwargs))
        return {"status": "completed"}

    originals["run_rotation"] = _patch(watcher._rotation_helper, "run_rotation", _fake_run_rotation)
    originals["time_time"] = _patch(watcher.time, "time", lambda: now)
    try:
        return asyncio.run(watcher.run_tick(
            role_tag="Coordinator-Main", cleared_signature="❯",
            pickup_prompt_path=marker_dir / "pickup.txt",
            poke_message=watcher.DEFAULT_POKE_MESSAGE,
            marker_dir=marker_dir, project_dir=Path("/nonexistent-project-dir"),
        ))
    finally:
        _patch(watcher, "resolve_seat_identity", originals["resolve_seat_identity"])
        _patch(watcher, "resolve_pending_count", originals["resolve_pending_count"])
        _patch(watcher, "cross_check_idle", originals["cross_check_idle"])
        _patch(watcher, "_confirm_pane_ready_for_action", originals["_confirm_pane_ready_for_action"])
        _patch(watcher._rotation_helper, "run_rotation", originals["run_rotation"])
        _patch(watcher.time, "time", originals["time_time"])


def test_run_tick_skips_when_identity_unresolvable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        calls: list[tuple[str, str, dict[str, Any]]] = []
        result = _run_tick_with_stubs(
            identity=None, pending_count=0, gate_result=(True, "ok"),
            record_calls=calls, marker_dir=Path(tmp), now=1000.0,
        )
    _check(result["tick_status"] == "skipped", "run_tick skips cleanly when identity can't be resolved")
    _check(calls == [], "no injection attempted when identity is unresolvable")


def test_run_tick_no_action_below_thresholds() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        identity = _identity(status="idle", status_updated_at_ms=(1000.0 - 50) * 1000.0)
        calls: list[tuple[str, str, dict[str, Any]]] = []
        result = _run_tick_with_stubs(
            identity=identity, pending_count=0, gate_result=(True, "ok"),
            record_calls=calls, marker_dir=Path(tmp), now=1000.0,
        )
    _check(result["tick_status"] == "no_action", "run_tick takes no action below both thresholds")
    _check(calls == [], "no injection attempted below thresholds")


def test_run_tick_refuses_when_act_time_gate_fails_and_never_injects() -> None:
    """Condition 5, proven end-to-end: even when the decision would be
    ACT_POKE, a failed ACT-time gate must mean run_rotation is NEVER
    called."""
    with tempfile.TemporaryDirectory() as tmp:
        identity = _identity(status="idle", status_updated_at_ms=(1000.0 - 400) * 1000.0)
        calls: list[tuple[str, str, dict[str, Any]]] = []
        result = _run_tick_with_stubs(
            identity=identity, pending_count=2, gate_result=(False, "composer not confirmed empty"),
            record_calls=calls, marker_dir=Path(tmp), now=1000.0,
        )
    _check(result["tick_status"] == "refused", "run_tick reports 'refused' when the ACT-time gate fails")
    _check(calls == [], "run_rotation is NEVER invoked when the ACT-time gate refuses")


def test_run_tick_pokes_with_the_fixed_constant_and_records_cooldown_state() -> None:
    """The condition-2 privacy smoke: the text actually handed to
    run_rotation is EXACTLY the fixed constant, even though the pending
    count came from a stubbed solet response -- proving the poke path
    never threads inbox content into the injected text. Also proves the
    cooldown state persists: a second tick immediately after must NOT
    poke again."""
    with tempfile.TemporaryDirectory() as tmp:
        identity = _identity(status="idle", status_updated_at_ms=(1000.0 - 400) * 1000.0)
        calls: list[tuple[str, str, dict[str, Any]]] = []
        result = _run_tick_with_stubs(
            identity=identity, pending_count=2, gate_result=(True, "ok"),
            record_calls=calls, marker_dir=Path(tmp), now=1000.0,
        )
        _check(result["tick_status"] == "acted", "first tick acts (pokes)")
        _check(len(calls) == 1, "exactly one run_rotation call on the poking tick")
        _check(
            calls[0][1] == watcher.DEFAULT_POKE_MESSAGE,
            "the injected text is EXACTLY the fixed poke constant, byte-for-byte "
            "-- never built from or containing any inbox-derived content",
        )
        _check(calls[0][2].get("inject_only") is True, "poke uses inject_only mode -- never /clear")

        # Second tick, same "now" (well within the 1800s cooldown) -- must NOT poke again.
        result2 = _run_tick_with_stubs(
            identity=identity, pending_count=2, gate_result=(True, "ok"),
            record_calls=calls, marker_dir=Path(tmp), now=1005.0,
        )
    _check(result2["tick_status"] == "no_action", "second tick within cooldown takes no action")
    _check(len(calls) == 1, "cooldown state persisted across ticks -- no second poke")


def test_run_tick_rotates_when_nothing_pending_past_rotate_threshold() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pickup_path = Path(tmp) / "pickup.txt"
        pickup_path.write_text("pickup content")
        identity = _identity(status="idle", status_updated_at_ms=(1000.0 - 2200) * 1000.0)
        calls: list[tuple[str, str, dict[str, Any]]] = []
        originals = {}
        originals["resolve_seat_identity"] = _patch(watcher, "resolve_seat_identity", lambda _role: identity)
        originals["resolve_pending_count"] = _patch(watcher, "resolve_pending_count", lambda _sid: 0)
        originals["cross_check_idle"] = _patch(watcher, "cross_check_idle", lambda *_a, **_k: True)

        async def _fake_gate(_role: str, _sig: str, *, settle_timeout_seconds: float) -> tuple[bool, str]:
            return True, "ok"

        originals["_confirm_pane_ready_for_action"] = _patch(watcher, "_confirm_pane_ready_for_action", _fake_gate)

        async def _fake_run_rotation(role_tag: str, text: str, **kwargs: Any) -> dict[str, Any]:
            calls.append((role_tag, text, kwargs))
            return {"status": "completed"}

        originals["run_rotation"] = _patch(watcher._rotation_helper, "run_rotation", _fake_run_rotation)
        originals["time_time"] = _patch(watcher.time, "time", lambda: 1000.0)
        try:
            result = asyncio.run(watcher.run_tick(
                role_tag="Coordinator-Main", cleared_signature="❯",
                pickup_prompt_path=pickup_path, poke_message=watcher.DEFAULT_POKE_MESSAGE,
                marker_dir=Path(tmp), project_dir=Path("/nonexistent-project-dir"),
            ))
        finally:
            _patch(watcher, "resolve_seat_identity", originals["resolve_seat_identity"])
            _patch(watcher, "resolve_pending_count", originals["resolve_pending_count"])
            _patch(watcher, "cross_check_idle", originals["cross_check_idle"])
            _patch(watcher, "_confirm_pane_ready_for_action", originals["_confirm_pane_ready_for_action"])
            _patch(watcher._rotation_helper, "run_rotation", originals["run_rotation"])
            _patch(watcher.time, "time", originals["time_time"])
    _check(result["tick_status"] == "acted" and result["action"] == watcher.ACTION_ROTATE, "run_tick rotates when nothing is pending and idle is past the rotate threshold")
    _check(len(calls) == 1 and calls[0][1] == "pickup content", "rotate sends the pickup file's own content")
    _check("inject_only" not in calls[0][2] or calls[0][2].get("inject_only") is not True, "rotate does NOT use inject_only -- it sends the full /clear sequence")


def _import_with_iterm2_blocked(module_name: str) -> Any:
    """Import ``module_name`` fresh on a machine that has NO ``iterm2`` module.

    Mirrors seat_rotation_helper_smoke's harness: a meta-path finder refusing
    exactly ``iterm2`` reproduces a headless adopter box ON a machine that has
    the bindings installed. That input class is why every dev-machine gate was
    structurally blind to this -- the module is present here, so a hard import
    is invisibly satisfied.
    """

    class _BlockIterm2:
        def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
            if fullname == "iterm2" or fullname.startswith("iterm2."):
                raise ModuleNotFoundError("No module named 'iterm2'", name="iterm2")
            return None

    blocker = _BlockIterm2()
    evicted = {
        name: mod
        for name, mod in sys.modules.items()
        if name == module_name or name == "iterm2" or name.startswith("iterm2.")
    }
    for name in evicted:
        del sys.modules[name]
    sys.meta_path.insert(0, blocker)  # pyright: ignore[reportArgumentType]
    try:
        return importlib.import_module(module_name)
    finally:
        sys.meta_path.remove(blocker)  # pyright: ignore[reportArgumentType]
        sys.modules.pop(module_name, None)
        sys.modules.update(evicted)


def test_module_imports_and_discloses_when_iterm2_bindings_are_absent() -> None:
    """The module must IMPORT with no ``iterm2``, and disclose at the ACT gate.

    ``iterm2`` is undeclared by this plugin and arrives only with
    iterm2_coding_agent_management_plugin, which the shipped macos-bizops profile
    deliberately excludes -- so a module-scope hard import made the whole
    watcher, cooldown/threshold logic included, unimportable on an adopter box.
    """
    reloaded = _import_with_iterm2_blocked("agent_messaging_plugin.seat_idle_watcher")
    _check(
        reloaded._iterm is None,  # noqa: SLF001
        "seat_idle_watcher imports with no iterm2 module and records the absence as None",
    )
    ok, reason = asyncio.run(
        reloaded._confirm_pane_ready_for_action(  # noqa: SLF001
            "Coordinator-Main", "\u276f", settle_timeout_seconds=1.0,
        )
    )
    _check(not ok, f"the ACT gate REFUSES when the bindings are absent (got ok={ok!r})")
    _check(
        "iterm2" in reason.lower(),
        f"the refusal DISCLOSES the missing bindings by name (got {reason!r})",
    )


def test_solet_inbox_fails_closed_with_named_absence_when_cli_is_not_on_path() -> None:
    """A missing bridge must never surface as an unclassified subprocess failure."""
    original_shutil = getattr(watcher, "shutil", None)
    original_log = watcher._log
    original_run = watcher.subprocess.run
    logs: list[str] = []
    watcher.shutil = SimpleNamespace(which=lambda _name: None)  # type: ignore[attr-defined]
    watcher._log = logs.append

    def _unexpected_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("missing bridge guard invoked subprocess.run")

    watcher.subprocess.run = _unexpected_run
    try:
        result = watcher._solet_inbox()
    finally:
        watcher._log = original_log
        watcher.subprocess.run = original_run
        if original_shutil is None:
            del watcher.shutil
        else:
            watcher.shutil = original_shutil
    _check(result is None, "missing solet-bridge fails closed without invoking a subprocess")
    _check(
        logs == ["solet-bridge is unavailable on PATH; refusing the seat-idle inbox read"],
        f"missing solet-bridge produces the named condition (got {logs!r})",
    )


def main() -> int:
    print("=== seat_idle_watcher smoke ===")
    test_resolve_seat_identity_tolerates_absent_agent_instance_id()
    test_resolve_seat_identity_still_requires_agent_session_id()
    test_state_path_uses_explicit_placeholder_when_agent_instance_id_is_none()
    test_compute_idle_seconds_zero_when_not_idle()
    test_compute_idle_seconds_positive_when_idle()
    test_compute_idle_seconds_never_negative()
    test_project_dir_slug_matches_measured_convention()
    test_transcript_path_for_builds_expected_path()
    test_cross_check_idle_agrees_within_tolerance()
    test_cross_check_idle_disagrees_when_transcript_is_newer()
    test_cross_check_idle_accepts_transcript_older_than_stamp()
    test_cross_check_idle_missing_transcript_fails_open()
    test_decide_action_poke_when_pending_and_idle_past_poke_threshold()
    test_decide_action_rotate_when_idle_past_rotate_threshold_and_nothing_pending()
    test_decide_action_none_below_both_thresholds()
    test_decide_action_pending_defers_rotate_even_past_rotate_threshold_when_poke_on_cooldown()
    test_decide_action_poke_wins_over_rotate_when_both_thresholds_crossed()
    test_decide_action_exact_boundary_poke_threshold_is_due()
    test_decide_action_exact_boundary_rotate_threshold_is_due()
    test_is_poke_on_cooldown_none_is_false()
    test_is_poke_on_cooldown_recent_is_true()
    test_is_poke_on_cooldown_past_window_is_false()
    test_is_poke_on_cooldown_exact_boundary_is_false()
    test_resolve_pending_count_extracts_only_the_length()
    test_resolve_pending_count_none_on_call_failure()
    test_act_time_gate_refuses_when_composer_not_cleared()
    test_act_time_gate_succeeds_when_stable_and_cleared()
    test_act_time_gate_refuses_when_screen_never_stabilizes()
    test_run_tick_skips_when_identity_unresolvable()
    test_run_tick_no_action_below_thresholds()
    test_run_tick_refuses_when_act_time_gate_fails_and_never_injects()
    test_run_tick_pokes_with_the_fixed_constant_and_records_cooldown_state()
    test_run_tick_rotates_when_nothing_pending_past_rotate_threshold()
    test_module_imports_and_discloses_when_iterm2_bindings_are_absent()
    test_solet_inbox_fails_closed_with_named_absence_when_cli_is_not_on_path()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
