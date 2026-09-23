#!/usr/bin/env python3
"""Unit smoke for ``seat_rotation_helper.py`` (rotation-systematization P2
slice A, ratified 2026-08-07) -- the pane-resolution 0/1/N gate, the
poll-until-stable settle detector, and the full send-sequence's ordering
and fail-closed-abort contract, all offline (a stub ``iterm2``-shaped
Session/App/Connection -- no real iTerm2 connection, matching this repo's
existing ``iterm2_plugin_smoke.py`` convention of stub-driven structural
proof; end-to-end proof against a REAL disposable pane is this lane's
separate live-measurement leg, not this file's job).

Proves: zero/one/N pane matches resolve or refuse correctly, and N>1 never
silently picks a winner (the exact dict-collapse defect this lane found in
the sibling plugin's ``list()``/``_terminate_by_snapshot``,
`iterm2_coding_agent_management_plugin/plugin.py:328,547-548` -- this
module's own resolution path must never regress to that shape); NUL-bytes
are stripped before any cleared-state string match; the settle poll resets
its consecutive-stable counter on any non-matching sample (kills an
early-settle mutation) and returns after exactly N consecutive stable
samples (kills an off-by-one); a timeout with the signature never observed
raises ``SettleTimeoutError`` (fail-closed, never silently returns); the
full ``run_rotation`` happy path sends EXACTLY the two-call
text-then-separate-CR shape for both the clear and the pickup (the direct
regression test for the brief's core `\\n`-does-not-submit defect); a
pane-ambiguity refusal or a settle timeout NEVER reaches the pickup-send
step (the ordering contract that prevents injecting the pickup prompt into
a still-populated or not-yet-cleared composer); and a send-primitive
failure mid-sequence raises ``HelperStepError`` naming the exact failing
step, with no step past it ever attempted.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/seat_rotation_helper_smoke.py
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import seat_rotation_helper as helper  # noqa: E402

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


# ─── Stub iterm2-shaped fixtures (no real API connection) ───────────────────


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
    def __init__(
        self, *, role: str, session_id: str, screen_lines: list[str] | None = None,
        fail_on: set[str] | None = None, screen_lines_fn: Any = None,
    ) -> None:
        self.role = role
        self.session_id = session_id
        self.sent: list[str] = []
        self._screen_lines = screen_lines if screen_lines is not None else ["cleared-signature"]
        self._fail_on = fail_on or set()
        # Dynamic content override -- called with THIS session on every poll,
        # so a fixture can vary the returned screen by get_screen_calls
        # and/or by what's in self.sent so far (e.g. "still populated until
        # after the pickup CR was sent, then confirmed"). None means "use
        # the static _screen_lines every time" (existing behavior).
        self._screen_lines_fn = screen_lines_fn
        self.get_screen_calls = 0

    async def async_get_variable(self, _name: str) -> str:
        return self.role

    async def async_send_text(self, text: str) -> None:
        if text in self._fail_on:
            raise RuntimeError(f"send failed for {text!r}")
        self.sent.append(text)

    async def async_get_screen_contents(self) -> _StubScreenContents:
        self.get_screen_calls += 1
        if self._screen_lines_fn is not None:
            return _StubScreenContents(self._screen_lines_fn(self))
        return _StubScreenContents(self._screen_lines)


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
    """Stand-in for the ``iterm2`` module surface ``seat_rotation_helper``
    calls through ``helper._iterm`` -- patched in per test."""

    def __init__(self, app: _StubApp | None) -> None:
        self._app = app

        class _Connection:
            @staticmethod
            async def async_create() -> object:
                return object()

        self.Connection = _Connection

    async def _async_get_app(self, _connection: object) -> _StubApp | None:
        return self._app


def _one_pane_app(session: _StubSession, *, tab_id: int = 1, window_id: int = 1) -> _StubApp:
    return _StubApp([_StubWindow(window_id, [_StubTab(tab_id, [session])])])


async def _run_with_stub(app: _StubApp | None, coro_factory: Any) -> Any:
    stub = _StubIterm(app)
    original = helper._iterm
    helper._iterm = stub
    helper._iterm.async_get_app = stub._async_get_app
    try:
        return await coro_factory()
    finally:
        helper._iterm = original


# ─── Calibration regression guard (live leg #1, findings file §4) ───────────


def test_default_settle_timeout_has_real_margin_over_the_measured_seat_floor() -> None:
    """RED-FIRST regression against the exact live leg #1 failure: the
    original 15.0s default was margined against the P2 DISPOSABLE-RIG floor
    (~0.3s, bare boot) rather than the real seat's measured floor (~14-15s,
    SessionStart hooks + MCP bridge respawn + full TUI redraw) -- consuming
    the entire margin and stranding the seat dark, needing an operator
    keystroke. Guards the recalibrated value structurally so a future edit
    can't silently regress it back below the measured floor's margin."""
    _check(
        helper.DEFAULT_SETTLE_TIMEOUT_SECONDS >= 90.0,
        f"DEFAULT_SETTLE_TIMEOUT_SECONDS ({helper.DEFAULT_SETTLE_TIMEOUT_SECONDS}) carries "
        "real margin (>=90s) over the live-measured ~14-15s real-seat settle floor",
    )


# ─── resolve_pane_matches / resolve_single_pane — the 0/1/N gate ────────────


def test_resolve_zero_matches_refuses() -> None:
    rows = [helper.RoleRow(role="Other", session_id="s1", tab_id=1, window_id=1)]
    try:
        helper.resolve_single_pane(rows, "Coordinator-Main")
        _check(False, "zero matches raises PaneResolutionError")
    except helper.PaneResolutionError as exc:
        _check(exc.code == helper.CODE_NO_PANE_MATCH, "zero matches -> code=no_pane_match")
        _check(exc.candidates == [], "zero matches -> empty candidates")


def test_resolve_one_match_resolves() -> None:
    rows = [
        helper.RoleRow(role="Other", session_id="s1", tab_id=1, window_id=1),
        helper.RoleRow(role="Coordinator-Main", session_id="s2", tab_id=2, window_id=1),
    ]
    match = helper.resolve_single_pane(rows, "Coordinator-Main")
    _check(match.session_id == "s2", "one match resolves to the correct session_id")


def test_resolve_two_matches_refuses_with_both_candidates() -> None:
    """Direct regression test for the dict-collapse defect this lane found
    in the sibling plugin (`{t.role: t for t in tabs_list}` silently keeps
    whichever row iterates last on N>1). This module's resolution must
    never do that -- it must refuse and name BOTH candidates."""
    rows = [
        helper.RoleRow(role="Coordinator-Main", session_id="s1", tab_id=1, window_id=1),
        helper.RoleRow(role="Coordinator-Main", session_id="s2", tab_id=2, window_id=1),
    ]
    try:
        helper.resolve_single_pane(rows, "Coordinator-Main")
        _check(False, "two matches raises PaneResolutionError (no silent winner)")
    except helper.PaneResolutionError as exc:
        _check(
            exc.code == helper.CODE_AMBIGUOUS_PANE_MATCH,
            "two matches -> code=ambiguous_pane_match",
        )
        _check(
            set(exc.candidates) == {"s1", "s2"},
            "two matches -> candidates carry BOTH session_ids verbatim",
        )


def test_resolve_pane_matches_ignores_other_roles() -> None:
    rows = [
        helper.RoleRow(role="Other", session_id="s1", tab_id=1, window_id=1),
        helper.RoleRow(role="Coordinator-Main", session_id="s2", tab_id=2, window_id=1),
    ]
    matches = helper.resolve_pane_matches(rows, "Coordinator-Main")
    _check(len(matches) == 1 and matches[0].session_id == "s2", "filter ignores non-matching roles")


# ─── clean_screen_text / is_cleared_state ────────────────────────────────


def test_clean_screen_text_strips_nul_bytes() -> None:
    _check(
        helper.clean_screen_text("a\x00b\x00c") == "abc",
        "clean_screen_text strips every NUL byte",
    )


def test_is_cleared_state_requires_signature_present() -> None:
    _check(
        helper.is_cleared_state(["some\x00 line", "ready>"], "ready>") is True,
        "is_cleared_state True when signature present after NUL-strip",
    )
    _check(
        helper.is_cleared_state(["not it", "still not it"], "ready>") is False,
        "is_cleared_state False when signature absent from every line",
    )


def test_is_cleared_state_needs_the_stripped_form_to_match() -> None:
    """A signature that only matches once NULs are stripped must still be
    found -- kills a mutation that matches raw (unstripped) screen text."""
    _check(
        helper.is_cleared_state(["re\x00ady>"], "ready>") is True,
        "is_cleared_state matches across a NUL that would otherwise split the signature",
    )


def test_is_cleared_state_rejects_a_line_where_signature_is_only_a_prefix() -> None:
    """RED-FIRST, direct regression for the P3 runbook's found-and-fixed
    defect: real captured screen data shows the empty-composer row
    ("❯\xa0", the prompt glyph + trailing NBSP) strips to the bare glyph
    "❯", but a STILL-POPULATED composer row ("❯\xa0/clear") has
    that same glyph as its PREFIX. A substring-containment check (the
    original, shipped-then-fixed implementation) would match BOTH -- exactly
    the negative-control state this helper exists to never act on. Exact
    match (post NUL-strip + edge-whitespace-strip) must reject the
    populated row while still accepting the empty one."""
    populated_row = "❯\xa0/clear"
    empty_row = "❯\xa0"
    signature = "❯"
    _check(
        helper.is_cleared_state([populated_row], signature) is False,
        "a composer row where the signature is only a PREFIX (still populated) is REJECTED",
    )
    _check(
        helper.is_cleared_state([empty_row], signature) is True,
        "a composer row that reduces to EXACTLY the signature (genuinely empty) is accepted",
    )


# ─── wait_for_settle — poll-until-stable ─────────────────────────────────


def test_wait_for_settle_returns_after_n_consecutive_stable_samples() -> None:
    class _FakeSession:
        def __init__(self) -> None:
            self.calls = 0

        async def async_get_screen_contents(self) -> _StubScreenContents:
            self.calls += 1
            return _StubScreenContents(["ready>"])

    session = _FakeSession()
    diagnostics = asyncio.run(helper.wait_for_settle(
        session, cleared_signature="ready>", stable_samples_required=3,
        poll_interval_seconds=0.0, timeout_seconds=5.0,
        now_fn=_counting_clock(), sleep_fn=_noop_sleep,
    ))
    _check(
        diagnostics.samples_taken == 3,
        "settles after exactly 3 consecutive stable samples, not more",
    )
    _check(diagnostics.streak_reset_count == 0, "no resets when every sample matches from the start")
    _check(
        diagnostics.current_streak_first_match_sample_index == 1,
        "the winning streak's first match is sample 1 -- it never reset",
    )


def test_wait_for_settle_resets_counter_on_non_matching_sample() -> None:
    """A sample that stops matching must reset the consecutive-stable
    counter to 0 -- kills a mutation that never resets it (which would
    settle on ANY 3 matching samples total, even non-consecutive ones)."""
    lines_sequence = [["ready>"], ["ready>"], ["mid-redraw garbage"], ["ready>"], ["ready>"], ["ready>"]]

    class _SequencedSession:
        def __init__(self) -> None:
            self._iter = iter(lines_sequence)

        async def async_get_screen_contents(self) -> _StubScreenContents:
            return _StubScreenContents(next(self._iter))

    session = _SequencedSession()
    diagnostics = asyncio.run(helper.wait_for_settle(
        session, cleared_signature="ready>", stable_samples_required=3,
        poll_interval_seconds=0.0, timeout_seconds=5.0,
        now_fn=_counting_clock(), sleep_fn=_noop_sleep,
    ))
    _check(
        diagnostics.samples_taken == 6,
        "a non-matching sample resets the streak; settle needs all 6 polls, not 5",
    )
    _check(diagnostics.streak_reset_count == 1, "exactly one reset -- the single mid-redraw sample")
    _check(
        diagnostics.current_streak_first_match_sample_index == 4,
        "the WINNING streak started at sample 4 (right after the reset), not sample 1",
    )


def test_wait_for_settle_times_out_when_signature_never_seen() -> None:
    class _NeverSession:
        async def async_get_screen_contents(self) -> _StubScreenContents:
            return _StubScreenContents(["still typing..."])

    clock = _counting_clock(step=1.0)
    try:
        asyncio.run(helper.wait_for_settle(
            _NeverSession(), cleared_signature="ready>", stable_samples_required=2,
            poll_interval_seconds=0.0, timeout_seconds=3.0,
            now_fn=clock, sleep_fn=_noop_sleep,
        ))
        _check(False, "never-matching screen raises SettleTimeoutError")
    except helper.SettleTimeoutError as exc:
        _check(True, "never-matching screen raises SettleTimeoutError")
        _check(exc.diagnostics.samples_taken > 0, "the exception carries diagnostics with samples_taken > 0")
        _check(
            exc.diagnostics.streak_reset_count == 0,
            "no reset ever fires when the signature is NEVER seen even once "
            "(nothing to reset -- kills a mutation that counts every non-match as a reset)",
        )
        _check(
            exc.diagnostics.current_streak_first_match_sample_index is None,
            "no streak ever started, so its first-match index is None, not a stale value",
        )


def _counting_clock(step: float = 0.01) -> Any:
    state = {"t": 0.0}

    def _clock() -> float:
        state["t"] += step
        return state["t"]

    return _clock


async def _noop_sleep(_seconds: float) -> None:
    return None


# ─── run_rotation — full sequence, ordering, and fail-closed-abort ───────


def test_run_rotation_zero_matches_never_sends_anything() -> None:
    session = _StubSession(role="Other", session_id="s1")
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "pickup text", cleared_signature="ready>",
            poll_interval_seconds=0.0, settle_timeout_seconds=1.0,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(result["status"] == "refused" and result["code"] == helper.CODE_NO_PANE_MATCH,
           "zero matches -> refused/no_pane_match envelope")
    _check(session.sent == [], "zero matches -> no send_text call ever issued")


def test_run_rotation_ambiguous_matches_never_sends_anything() -> None:
    s1 = _StubSession(role="Coordinator-Main", session_id="s1")
    s2 = _StubSession(role="Coordinator-Main", session_id="s2")
    app = _StubApp([_StubWindow(1, [_StubTab(1, [s1, s2])])])

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "pickup text", cleared_signature="ready>",
            poll_interval_seconds=0.0, settle_timeout_seconds=1.0,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(
        result["status"] == "refused" and result["code"] == helper.CODE_AMBIGUOUS_PANE_MATCH,
        "N=2 matches -> refused/ambiguous_pane_match envelope",
    )
    _check(set(result["candidates"]) == {"s1", "s2"}, "ambiguous refusal names both candidates")
    _check(s1.sent == [] and s2.sent == [], "N=2 matches -> neither pane receives any send_text")


def test_run_rotation_happy_path_sends_exact_two_call_shape_twice() -> None:
    """The direct regression test for the brief's core defect: `/clear` and
    the pickup prompt must EACH be one send_text call with no trailing
    newline, followed by SEPARATE send_text("\\r") call(s) -- never combined
    into one `...text\\n` call. The clear leg is the five-send shape: the
    space + kill-line preamble (2026-08-28 ghost-text matrix -- a leading
    flush-Enter SUBMITS a stranded remote-control draft, so it is banned),
    then /clear, CR, menu-confirm CR (2026-08-27 joseki: a single CR after
    a slash command was measured leaving it accepted-but-unsubmitted)."""
    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines=["ready>"])
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "resume the work", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=2.0,
            send_gap_seconds=0.0,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(result["status"] == "completed", "happy path completes")
    _check(
        session.sent == [" ", "\x15", "/clear", "\r", "\r", "resume the work", "\r"],
        f"exact 7-call ordered send sequence, no embedded newlines (got {session.sent!r})",
    )
    _check(
        "settle_diagnostics" in result and result["settle_diagnostics"]["samples_taken"] == 1,
        "the COMPLETED envelope carries settle_diagnostics (live leg #1's own named gap: "
        "a green result with no settle-timing detail can't localize how long settle took)",
    )


def test_run_rotation_model_none_is_byte_identical_to_the_existing_sequence() -> None:
    """The additive model parameter must not alter the established default.

    This is deliberately separate from the general happy-path assertion: it
    makes ``model=None`` itself a permanent byte-for-byte contract rather
    than merely an implicit default of a test written before model support.
    """
    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines=["ready>"])
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "resume the work", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=2.0,
            send_gap_seconds=0.0, model=None,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(result["status"] == "completed", "model=None happy path completes")
    _check(
        session.sent == [" ", "\x15", "/clear", "\r", "\r", "resume the work", "\r"],
        f"model=None preserves the exact pre-model send sequence (got {session.sent!r})",
    )


def test_run_rotation_model_leg_follows_clear_settle_before_pickup() -> None:
    """A requested model uses the proven five-send slash-command shape only
    after the clear settle check, then itself settles before pickup injection."""
    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines=["ready>"])
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "resume the work", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=2.0,
            send_gap_seconds=0.0, model="claude-sonnet-5",
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(result["status"] == "completed", "model-leg happy path completes")
    _check(
        session.sent == [
            " ", "\x15", "/clear", "\r", "\r",
            " ", "\x15", "/model claude-sonnet-5", "\r", "\r",
            "resume the work", "\r",
        ],
        f"model leg is clear-settle then five sends then model-settle then pickup (got {session.sent!r})",
    )
    _check("model_settle_diagnostics" in result, "completed model rotation reports its second settle")


def test_run_rotation_model_send_failure_aborts_before_model_cr_or_pickup() -> None:
    """The model leg uses the same fail-closed primitive-failure contract as
    clear: a failed command text never reaches either CR or the pickup."""
    session = _StubSession(
        role="Coordinator-Main", session_id="s1", screen_lines=["ready>"],
        fail_on={"/model claude-sonnet-5"},
    )
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "pickup text", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=2.0,
            send_gap_seconds=0.0, model="claude-sonnet-5",
        )

    try:
        asyncio.run(_run_with_stub(app, _go))
        _check(False, "a failing /model send raises HelperStepError")
    except helper.HelperStepError as exc:
        _check(
            exc.step == helper.STEP_SEND_MODEL_TEXT,
            f"failing /model send names the model-text step (got {exc.step!r})",
        )
    _check(
        session.sent == [" ", "\x15", "/clear", "\r", "\r", " ", "\x15"],
        f"model send failure reaches neither model CR nor pickup (got {session.sent!r})",
    )


def test_run_rotation_settle_timeout_never_sends_pickup() -> None:
    """Ordering safety contract: a settle timeout must abort BEFORE the
    pickup prompt is ever sent -- injecting the pickup into a not-yet-
    cleared composer would be worse than the original defect."""
    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines=["still mid-clear..."])
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "pickup text", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=0.05,
            send_gap_seconds=0.0,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(
        result["status"] == "refused" and result["code"] == helper.CODE_SETTLE_TIMEOUT,
        "settle timeout -> refused/settle_timeout envelope",
    )
    _check(
        session.sent == [" ", "\x15", "/clear", "\r", "\r"],
        f"settle timeout sent only the clear leg, never the pickup (got {session.sent!r})",
    )
    _check(
        "settle_diagnostics" in result and result["settle_diagnostics"]["samples_taken"] > 0,
        "the REFUSED envelope ALSO carries settle_diagnostics -- live leg #1's exact gap "
        "('the log records no per-sample timing, so flapping ... cannot be excluded from "
        "this envelope alone') was on the refused path specifically",
    )


def test_run_rotation_send_failure_on_clear_text_aborts_before_cr() -> None:
    session = _StubSession(role="Coordinator-Main", session_id="s1", fail_on={"/clear"})
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "pickup text", cleared_signature="ready>",
            poll_interval_seconds=0.0, settle_timeout_seconds=1.0,
            send_gap_seconds=0.0,
        )

    try:
        asyncio.run(_run_with_stub(app, _go))
        _check(False, "a failing /clear send raises HelperStepError")
    except helper.HelperStepError as exc:
        _check(
            exc.step == helper.STEP_SEND_CLEAR_TEXT,
            f"failing /clear send names step=send_clear_text (got {exc.step!r})",
        )
    _check(
        session.sent == [" ", "\x15"],
        f"a failing clear-text send happens after only the space+kill-line preamble and "
        f"never reaches the submitting CRs (got {session.sent!r})",
    )


def test_run_rotation_send_failure_on_pickup_text_names_that_step() -> None:
    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines=["ready>"], fail_on={"pickup text"})
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "pickup text", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=2.0,
            send_gap_seconds=0.0,
        )

    try:
        asyncio.run(_run_with_stub(app, _go))
        _check(False, "a failing pickup-text send raises HelperStepError")
    except helper.HelperStepError as exc:
        _check(
            exc.step == helper.STEP_SEND_PICKUP_TEXT,
            f"failing pickup-text send names step=send_pickup_text (got {exc.step!r})",
        )
    _check(
        session.sent == [" ", "\x15", "/clear", "\r", "\r"],
        f"failing pickup-text send happens only after the clear leg, never a trailing CR (got {session.sent!r})",
    )


def test_run_rotation_send_failure_on_final_cr_names_pickup_cr_step() -> None:
    """The final "\\r" call (pickup CR) is byte-identical to the clear leg's
    two CRs (submit, menu-confirm) -- fail only on the THIRD occurrence of
    "\\r" (an occurrence-counting stub) so the failure lands specifically on
    the pickup CR step, not any clear-leg CR."""

    class _ThirdCrFailsSession(_StubSession):
        def __init__(self) -> None:
            super().__init__(role="Coordinator-Main", session_id="s1", screen_lines=["ready>"])
            self._cr_calls = 0

        async def async_send_text(self, text: str) -> None:
            if text == "\r":
                self._cr_calls += 1
                if self._cr_calls == 3:
                    raise RuntimeError("send failed for third \\r")
            self.sent.append(text)

    session = _ThirdCrFailsSession()
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "pickup text", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=2.0,
            send_gap_seconds=0.0,
        )

    try:
        asyncio.run(_run_with_stub(app, _go))
        _check(False, "a failing final CR send raises HelperStepError")
    except helper.HelperStepError as exc:
        _check(
            exc.step == helper.STEP_SEND_PICKUP_CR,
            f"failing final CR send names step=send_pickup_cr (got {exc.step!r})",
        )
    _check(
        session.sent == [" ", "\x15", "/clear", "\r", "\r", "pickup text"],
        f"failing final CR send happens only after the clear leg + pickup text (got {session.sent!r})",
    )


# ─── run_rotation(inject_only=True) — the settle-timeout recovery path ────


def test_run_rotation_inject_only_skips_the_clear_send() -> None:
    """The recovery path added after live leg #1: the pane was ALREADY
    cleared when the ORIGINAL /clear was sent, so recovery must never
    re-send /clear -- only settle-wait then inject."""
    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines=["ready>"])
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "resume the work", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=2.0,
            inject_only=True,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(result["status"] == "completed", "inject-only recovery completes")
    _check(
        session.sent == ["resume the work", "\r"],
        f"inject-only NEVER sends /clear -- only pickup text + CR (got {session.sent!r})",
    )


def test_run_rotation_inject_only_still_settle_waits_before_injecting() -> None:
    """RED-FIRST safety contract: inject-only skips the /clear SEND, never
    the settle-wait CHECK -- it must still positively confirm the cleared
    signature before injecting, not assume a prior refusal means the pane
    is fine now. Kills a mutation that lets inject_only bypass settle-wait
    entirely (which would inject the pickup into an unconfirmed state)."""
    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines=["still not cleared..."])
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "resume the work", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=0.05,
            inject_only=True,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(
        result["status"] == "refused" and result["code"] == helper.CODE_SETTLE_TIMEOUT,
        "inject-only STILL refuses on an unconfirmed screen -- settle-wait is never bypassed",
    )
    _check(
        session.sent == [],
        f"inject-only refusal sends NOTHING -- no /clear (skipped by design) and no pickup "
        f"(blocked by the still-enforced settle-wait) (got {session.sent!r})",
    )


# ─── last_nonempty_line — pure helper for submit_only mode ──────────────


def test_last_nonempty_line_returns_the_final_stripped_nonblank_line() -> None:
    _check(
        helper.last_nonempty_line("line1\nline2\n  line3  \n") == "line3",
        "returns the last non-blank line, stripped of surrounding whitespace",
    )


def test_last_nonempty_line_skips_trailing_blank_lines() -> None:
    _check(
        helper.last_nonempty_line("real content\n\n\n") == "real content",
        "trailing blank lines are skipped -- the LAST content, not the last line",
    )


def test_last_nonempty_line_all_blank_returns_empty_string() -> None:
    _check(helper.last_nonempty_line("") == "", "empty text returns empty string, never raises")
    _check(helper.last_nonempty_line("\n\n  \n") == "", "all-whitespace text returns empty string")


# ─── wait_for_screen_stable — paste-render stabilization (fix loop #2 item 2) ─


def test_wait_for_screen_stable_returns_after_n_identical_consecutive_samples() -> None:
    lines_sequence = [["typing1"], ["typing2"], ["stable"], ["stable"], ["stable"], ["stable"]]

    class _SequencedSession:
        def __init__(self) -> None:
            self._iter = iter(lines_sequence)

        async def async_get_screen_contents(self) -> _StubScreenContents:
            return _StubScreenContents(next(self._iter))

    diagnostics = asyncio.run(helper.wait_for_screen_stable(
        _SequencedSession(), stable_samples_required=3,
        poll_interval_seconds=0.0, timeout_seconds=5.0,
        now_fn=_counting_clock(), sleep_fn=_noop_sleep,
    ))
    _check(
        diagnostics.samples_taken == 6,
        "settles after 3 consecutive IDENTICAL samples -- 2 changing + 1 to establish the "
        "baseline + 3 matching = 6 total polls",
    )


def test_wait_for_screen_stable_times_out_when_screen_never_stops_changing() -> None:
    """RED-FIRST: a screen that changes on EVERY poll (the exact
    still-rendering-a-paste state) must never be mistaken for stable --
    kills a mutation that treats absence-of-a-target-signature as
    'stable enough'."""
    class _AlwaysChangingSession:
        def __init__(self) -> None:
            self._counter = 0

        async def async_get_screen_contents(self) -> _StubScreenContents:
            self._counter += 1
            return _StubScreenContents([f"still-rendering-{self._counter}"])

    try:
        asyncio.run(helper.wait_for_screen_stable(
            _AlwaysChangingSession(), stable_samples_required=2,
            poll_interval_seconds=0.0, timeout_seconds=3.0,
            now_fn=_counting_clock(step=1.0), sleep_fn=_noop_sleep,
        ))
        _check(False, "an ever-changing screen raises ScreenStabilityTimeoutError")
    except helper.ScreenStabilityTimeoutError as exc:
        _check(True, "an ever-changing screen raises ScreenStabilityTimeoutError")
        _check(
            exc.diagnostics.streak_reset_count == 0,
            "content that NEVER even matches once has nothing to reset (mirrors the "
            "never-seen-signature settle test)",
        )


# ─── run_rotation — the post-paste-stable + post-submit confirmation chain ──


def test_run_rotation_paste_unstable_refuses_before_sending_final_cr() -> None:
    """RED-FIRST, fix loop #2 item 2's own safety contract: if the screen
    never stabilizes after the pickup text is sent, the submitting CR must
    NEVER be sent -- kills a mutation that sends the CR regardless of
    paste-stability confirmation (the exact live leg #2 shape: text sent,
    CR sent too early, absorbed)."""
    def _screen_fn(s: Any) -> list[str]:
        if "pickup text" not in s.sent:
            return ["ready>"]
        return [f"still-rendering-{s.get_screen_calls}"]

    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines_fn=_screen_fn)
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "pickup text", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=3, settle_timeout_seconds=0.05,
            send_gap_seconds=0.0,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(
        result["status"] == "refused" and result["code"] == helper.CODE_PASTE_STABLE_TIMEOUT,
        f"an unstabilizing screen -> refused/paste_stable_timeout (got {result!r})",
    )
    _check(
        session.sent == [" ", "\x15", "/clear", "\r", "\r", "pickup text"],
        f"the pickup TEXT was sent (needed to trigger the paste) but the submitting CR "
        f"was NEVER sent (got {session.sent!r})",
    )
    _check("paste_stable_diagnostics" in result, "the refusal carries paste_stable_diagnostics")


def test_run_rotation_submit_never_confirmed_refuses_and_never_reports_completed() -> None:
    """THE core false-green regression test (live leg #2, findings §4): both
    send_text calls succeed (nothing raises) but the composer NEVER returns
    to the cleared signature after the final CR -- exactly the measured
    leg #2 failure (CR absorbed into the paste; composer stays populated;
    no API call fails). The helper must refuse, NEVER report completed,
    on send-API success alone."""
    def _screen_fn(s: Any) -> list[str]:
        if "pickup text" not in s.sent:
            return ["ready>"]
        return ["populated with pickup text"]  # never returns to "ready>", even post-CR

    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines_fn=_screen_fn)
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "pickup text", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=0.05,
            send_gap_seconds=0.0,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(
        result["status"] == "refused" and result["code"] == helper.CODE_SUBMIT_TIMEOUT,
        f"an unconfirmed submission -> refused/submit_timeout, NEVER completed (got {result!r})",
    )
    _check(
        session.sent == [" ", "\x15", "/clear", "\r", "\r", "pickup text", "\r"],
        f"EVERY send call succeeded -- this IS the false-green trap: send success != submit "
        f"truth (got {session.sent!r})",
    )
    _check("submit_diagnostics" in result, "the refused envelope carries submit_diagnostics")


def test_run_rotation_happy_path_carries_both_settle_and_submit_diagnostics() -> None:
    """GREEN companion to the false-green test above: when submission IS
    confirmed, completed carries diagnostics for BOTH the pre-pickup settle
    and the post-CR submit confirmation -- proves the new step doesn't
    silently drop the original settle_diagnostics."""
    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines=["ready>"])
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "pickup text", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=2.0,
            send_gap_seconds=0.0,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(result["status"] == "completed", "happy path still completes")
    _check("settle_diagnostics" in result, "carries the pre-pickup settle_diagnostics")
    _check("submit_diagnostics" in result, "carries the NEW post-CR submit_diagnostics")
    _check(
        session.sent == [" ", "\x15", "/clear", "\r", "\r", "pickup text", "\r"],
        f"exact send sequence unchanged by the confirmation steps (got {session.sent!r})",
    )


# ─── run_rotation — capture-then-clear (relay-not-disregard, 2026-08-28) ──


def test_run_rotation_captures_stranded_composer_content_before_killing_it() -> None:
    """RED-FIRST for the relay-not-disregard policy: content sitting in the
    composer when the clear leg starts (a stranded remote-control draft, or
    a genuine unsent operator draft) is about to be destroyed by the
    kill-line -- the helper must CAPTURE it off the screen first and carry
    it in the envelope, never silently discard it. Kills a mutation that
    sends the preamble without reading the screen."""
    def _screen_fn(s: Any) -> list[str]:
        if "/clear" not in s.sent:
            return ["old turn text", "ready> stranded RC draft"]
        return ["ready>"]

    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines_fn=_screen_fn)
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "pickup text", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=2.0,
            send_gap_seconds=0.0,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(result["status"] == "completed", "capture never blocks the rotation itself")
    _check(
        result.get("composer_capture") == ["ready> stranded RC draft"],
        f"the stranded composer row is captured verbatim in the envelope "
        f"(got {result.get('composer_capture')!r})",
    )
    _check(
        session.sent[:2] == [" ", "\x15"],
        f"the kill happens only AFTER the capture-bearing screen read (got {session.sent!r})",
    )


def test_run_rotation_empty_composer_captures_nothing() -> None:
    """GREEN companion: an empty composer (the bare cleared-signature row)
    yields an empty capture -- the signature row itself must never be
    mistaken for stranded content."""
    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines=["ready>"])
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", "pickup text", cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=2.0,
            send_gap_seconds=0.0,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(result["status"] == "completed", "empty-composer happy path completes")
    _check(
        result.get("composer_capture") == [],
        f"bare signature row captures nothing (got {result.get('composer_capture')!r})",
    )


# ─── run_rotation(submit_only=True) — the stranded-composer recovery path ──


def test_run_rotation_submit_only_confirms_content_then_sends_cr_and_confirms_submit() -> None:
    pickup = "context line 1\nEXPECTED_FRAGMENT_XYZ"

    def _screen_fn(s: Any) -> list[str]:
        if "\r" in s.sent:
            return ["ready>"]
        return ["❯ EXPECTED_FRAGMENT_XYZ (stranded)"]

    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines_fn=_screen_fn)
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", pickup, cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=2.0,
            submit_only=True,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(
        result["status"] == "completed",
        f"submit-only completes when composer content matches and the CR confirms (got {result!r})",
    )
    _check(
        session.sent == ["\r"],
        f"submit-only sends ONLY the bare CR -- no /clear, no re-injected pickup text "
        f"(got {session.sent!r})",
    )


def test_run_rotation_submit_only_refuses_and_sends_nothing_on_content_mismatch() -> None:
    """RED-FIRST safety contract, explicitly named in fix loop #2 item 3:
    submit_only must NEVER inject text -- on a content mismatch it sends
    absolutely nothing, not even a diagnostic keystroke."""
    pickup = "context line 1\nEXPECTED_FRAGMENT_XYZ"
    session = _StubSession(role="Coordinator-Main", session_id="s1", screen_lines=["totally different content"])
    app = _one_pane_app(session)

    async def _go() -> dict[str, Any]:
        return await helper.run_rotation(
            "Coordinator-Main", pickup, cleared_signature="ready>",
            poll_interval_seconds=0.0, stable_samples_required=1, settle_timeout_seconds=2.0,
            submit_only=True,
        )

    result = asyncio.run(_run_with_stub(app, _go))
    _check(
        result["status"] == "refused" and result["code"] == helper.CODE_COMPOSER_CONTENT_MISMATCH,
        f"submit-only refuses when the composer doesn't show the expected content (got {result!r})",
    )
    _check(
        session.sent == [],
        f"submit-only sends ABSOLUTELY NOTHING on a mismatch -- never a blind CR either "
        f"(got {session.sent!r})",
    )


# ─── Driver ───────────────────────────────────────────────────────────────


def _import_with_iterm2_blocked(module_name: str) -> Any:
    """Import ``module_name`` fresh on a machine that has NO ``iterm2`` module.

    A meta-path finder refusing exactly ``iterm2`` reproduces a headless adopter
    box ON a developer machine that has the bindings installed. That input class
    is the whole reason this defect survived every dev-machine gate: here the
    module is present, so a hard import is invisibly satisfied, and only a born
    clone (or this finder) can see the absence.
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
    """The module must IMPORT with no ``iterm2`` present, and disclose at use.

    ``iterm2`` is not declared by this plugin; it arrives only with
    ``iterm2_coding_agent_management_plugin``, which the shipped macos-bizops profile
    deliberately excludes. A module-scope hard import therefore made this entire
    module -- pure rotation logic included -- unimportable on an adopter box.
    Absence must be a DISCLOSED condition at the one place that drives iTerm2,
    never a silent degradation and never an import-time crash.
    """
    reloaded = _import_with_iterm2_blocked("agent_messaging_plugin.seat_rotation_helper")
    _check(
        reloaded._iterm is None,  # noqa: SLF001
        "seat_rotation_helper imports with no iterm2 module and records the absence as None",
    )

    async def _go() -> dict[str, Any]:
        return await reloaded.run_rotation("Coordinator-Main", "pickup", cleared_signature="ready>")

    raised: BaseException | None = None
    try:
        asyncio.run(_go())
    except BaseException as exc:  # noqa: BLE001 - the identity of the error IS the assertion
        raised = exc
    # ``reloaded`` is dynamically imported, so ``reloaded.HelperStepError`` is Any and
    # isinstance against it cannot narrow ``raised`` past ``BaseException | None``.
    # getattr keeps the assertion exactly as strong -- the class identity is still
    # checked, and the step is still compared -- without asking the type checker to
    # narrow through a dynamic class object.
    _check(
        isinstance(raised, reloaded.HelperStepError)
        and getattr(raised, "step", None) == "connect",
        f"driving a rotation without the bindings fails LOUD at the connect step (got {raised!r})",
    )
    _check(
        raised is not None and "iterm2" in str(raised).lower(),
        f"the disclosed reason names the missing bindings (got {raised!r})",
    )


def main() -> int:
    print("=== seat_rotation_helper smoke ===")
    test_default_settle_timeout_has_real_margin_over_the_measured_seat_floor()
    test_resolve_zero_matches_refuses()
    test_resolve_one_match_resolves()
    test_resolve_two_matches_refuses_with_both_candidates()
    test_resolve_pane_matches_ignores_other_roles()
    test_clean_screen_text_strips_nul_bytes()
    test_is_cleared_state_requires_signature_present()
    test_is_cleared_state_needs_the_stripped_form_to_match()
    test_is_cleared_state_rejects_a_line_where_signature_is_only_a_prefix()
    test_wait_for_settle_returns_after_n_consecutive_stable_samples()
    test_wait_for_settle_resets_counter_on_non_matching_sample()
    test_wait_for_settle_times_out_when_signature_never_seen()
    test_run_rotation_zero_matches_never_sends_anything()
    test_run_rotation_ambiguous_matches_never_sends_anything()
    test_run_rotation_happy_path_sends_exact_two_call_shape_twice()
    test_run_rotation_model_none_is_byte_identical_to_the_existing_sequence()
    test_run_rotation_model_leg_follows_clear_settle_before_pickup()
    test_run_rotation_model_send_failure_aborts_before_model_cr_or_pickup()
    test_run_rotation_settle_timeout_never_sends_pickup()
    test_run_rotation_send_failure_on_clear_text_aborts_before_cr()
    test_run_rotation_send_failure_on_pickup_text_names_that_step()
    test_run_rotation_send_failure_on_final_cr_names_pickup_cr_step()
    test_run_rotation_inject_only_skips_the_clear_send()
    test_run_rotation_inject_only_still_settle_waits_before_injecting()
    test_last_nonempty_line_returns_the_final_stripped_nonblank_line()
    test_last_nonempty_line_skips_trailing_blank_lines()
    test_last_nonempty_line_all_blank_returns_empty_string()
    test_wait_for_screen_stable_returns_after_n_identical_consecutive_samples()
    test_wait_for_screen_stable_times_out_when_screen_never_stops_changing()
    test_run_rotation_paste_unstable_refuses_before_sending_final_cr()
    test_run_rotation_submit_never_confirmed_refuses_and_never_reports_completed()
    test_run_rotation_happy_path_carries_both_settle_and_submit_diagnostics()
    test_run_rotation_captures_stranded_composer_content_before_killing_it()
    test_run_rotation_empty_composer_captures_nothing()
    test_run_rotation_submit_only_confirms_content_then_sends_cr_and_confirms_submit()
    test_run_rotation_submit_only_refuses_and_sends_nothing_on_content_mismatch()
    test_module_imports_and_discloses_when_iterm2_bindings_are_absent()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
