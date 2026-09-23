#!/usr/bin/env python3
"""Unit smoke for ``_TmuxSendKeysDriverChannel`` (spawn/registration-gaps fix,
2026-08-08 — ``workbench/2026-08-08_spawn_registration_gaps_findings_
rotation-impl.md``). Offline, stub-driven — a fake ``run_fn`` records every
``tmux`` invocation and simulates pane content across successive
``capture-pane`` calls; no real tmux process. The claim that this actually
fixes the real Claude Code paste-coalescing race is proven LIVE, against a
real disposable spawn (recorded in the findings file), not by this offline
smoke — this file's job is to prove the SHAPE of the fix (ordering, the
stability-poll contract, the fail-open timeout, and that a send-primitive
failure never attempts the Enter), matching this repo's own established
split between offline-shape smokes and live-measurement legs
(``seat_rotation_helper_smoke.py`` / rotation-systematization fix loop #2).

Proves: the submitting Enter is sent ONLY after
``stable_samples_required`` CONSECUTIVE identical ``capture-pane`` reads
(kills an early-Enter mutation); any non-matching sample resets the
consecutive counter to zero (kills an off-by-one/no-reset mutation); a
timeout still sends the Enter (fail-open, not fail-closed — this channel's
own established fire-and-forget contract); and a failure sending the
literal text NEVER attempts the Enter at all.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/tmux_driver_channel_smoke.py
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import tmux_adapter as tmux_module  # noqa: E402
from agent_messaging_plugin.codex_tmux import _CodexTmuxDriverChannel  # noqa: E402
from agent_messaging_plugin.session_hosts import DriverChannelSendError  # noqa: E402

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


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


class _FakeTmuxRunner:
    """Records every invocation's argv; ``capture_pane_sequence`` is popped
    one value per ``capture-pane`` call (the last value repeats once
    exhausted, simulating a pane that settles and stays settled)."""

    def __init__(
        self, *, capture_pane_sequence: list[str] | None = None,
        fail_payload_insert: bool = False, fail_enter_send: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self.payloads: list[str] = []
        self._capture_pane_sequence = list(capture_pane_sequence or [])
        self._last_capture_value = ""
        self._fail_payload_insert = fail_payload_insert
        self._fail_enter_send = fail_enter_send

    def __call__(self, argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        self.calls.append(argv)
        if "capture-pane" in argv:
            if self._capture_pane_sequence:
                self._last_capture_value = self._capture_pane_sequence.pop(0)
            return _FakeCompletedProcess(stdout=self._last_capture_value)
        if "load-buffer" in argv:
            if self._fail_payload_insert:
                raise OSError("payload buffer load failed")
            self.payloads.append(str(kwargs.get("input", "")))
        if "send-keys" in argv and "Enter" in argv and self._fail_enter_send:
            raise OSError("enter send failed")
        return _FakeCompletedProcess()

    def capture_pane_call_count(self) -> int:
        return sum(1 for c in self.calls if "capture-pane" in c)

    def enter_sent(self) -> bool:
        return any("send-keys" in c and "Enter" in c for c in self.calls)

    def payload_inserted(self) -> bool:
        return any("paste-buffer" in c for c in self.calls)

    def payload_insert_index(self) -> int:
        return next(i for i, c in enumerate(self.calls) if "paste-buffer" in c)

    def send_keys_literal_used(self) -> bool:
        """Whether the OLD, head-truncating ``send-keys -l`` payload path ran.

        Kept as a named probe rather than deleted with the mechanism: this is
        the assertion that pins the 2026-09-07 fix, and a future edit that
        reintroduces ``send-keys -l`` for the payload must fail a test that
        says so in as many words."""
        return any("send-keys" in c and "-l" in c for c in self.calls)


def _fake_sleep(_seconds: float) -> None:
    return None


class _FakeClock:
    """Deterministic monotonic clock — each call advances by a fixed step,
    so a timeout test needs no real wall-clock wait."""

    def __init__(self, step: float = 1.0) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        value = self._now
        self._now += self._step
        return value


def test_enter_sent_only_after_n_consecutive_stable_samples() -> None:
    """Baseline-gate fix (driver-channel strand fix, 2026-08-14): the FIRST
    capture-pane read establishes the BASELINE (the pre-render screen) as
    well as seeding the cold-start ``prev`` — a value is only eligible to
    start the stability streak once it genuinely differs from that first
    look. Updated from the pre-fix fixture (five IDENTICAL samples): a
    pane that never visibly differs from its own baseline is now
    indistinguishable from the un-rendered-paste defect this fix closes
    and correctly falls through to the timeout/fail-open path instead of
    declaring quick stability (covered separately by
    ``test_timeout_still_sends_enter_fail_open``) — so demonstrating the
    N-consecutive-match counting shape now needs a fixture with a genuine
    pre/post transition: 1 baseline + 1 sample establishing the new value
    (no count, off-by-one, same shape ``wait_for_screen_stable`` has) + 3
    matching confirmations = 5 raw captures for a 3-sample streak. (This
    fixture's baseline already differs from its stable value on the very
    first transition, so it does not itself distinguish the baseline-gated
    algorithm from the pre-fix one — that discriminating coverage lives in
    ``tmux_adapter_smoke.py``'s ``test_driver_channel_enter_waits_for_
    baseline_change``, whose fixture holds the PRE-send screen steady across
    the whole stability window before switching. This test's own job stays
    the N-consecutive-match counting shape and the call-ordering invariants
    below.)
    """
    runner = _FakeTmuxRunner(capture_pane_sequence=["A", "B", "B", "B", "B"])
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=runner,
        stable_samples_required=3, stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=0.1),
    )
    channel.send("hello")
    _check(runner.payload_inserted(), "the payload is inserted into the composer")
    _check(
        runner.capture_pane_call_count() == 5,
        "5 capture-pane reads for a 3-sample streak (1 baseline + 1 transition "
        f"sample + 3 confirmations), got {runner.capture_pane_call_count()}",
    )
    _check(runner.enter_sent(), "Enter is sent once the pane stabilizes")
    literal_idx = runner.payload_insert_index()
    enter_idx = next(i for i, c in enumerate(runner.calls) if "Enter" in c)
    _check(enter_idx > literal_idx, "Enter is sent strictly AFTER the payload insert")
    capture_indices = [i for i, c in enumerate(runner.calls) if "capture-pane" in c]
    _check(
        all(literal_idx < i < enter_idx for i in capture_indices),
        "every capture-pane read happens strictly between the payload insert and the Enter",
    )


def test_non_matching_sample_resets_the_consecutive_counter() -> None:
    """RED-FIRST regression against an early-settle mutation: a stray
    non-matching sample (e.g. mid-render) must not count toward the streak
    -- kills a mutation that forgets to reset on mismatch."""
    runner = _FakeTmuxRunner(capture_pane_sequence=["A", "B", "B", "B"])
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=runner,
        stable_samples_required=3, stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=0.1),
    )
    channel.send("hello")
    _check(
        runner.capture_pane_call_count() == 5,
        f"a mismatch resets the streak -- needs 5 samples (baseline, then a mismatch that "
        f"becomes the new baseline, then 3 confirmations), got {runner.capture_pane_call_count()}",
    )
    _check(runner.enter_sent(), "Enter is still eventually sent once the pane genuinely stabilizes")


def test_timeout_still_sends_enter_fail_open() -> None:
    """The channel's own established contract is fire-and-forget best-
    effort (never silently do nothing) -- a pane that never stabilizes must
    still get the Enter, not be abandoned mid-composer forever."""
    ever_changing = [f"frame-{i}" for i in range(1000)]
    runner = _FakeTmuxRunner(capture_pane_sequence=ever_changing)
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=runner,
        stable_samples_required=3, stable_timeout_seconds=2.0,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=1.0),
    )
    channel.send("hello")
    _check(runner.enter_sent(), "Enter is still sent (fail-open) after a stability timeout")


def test_payload_insert_failure_never_attempts_enter() -> None:
    runner = _FakeTmuxRunner(fail_payload_insert=True)
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=runner,
        stable_samples_required=3, stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=0.1),
    )
    channel.send("hello")  # must not raise -- fire-and-forget contract
    _check(
        not runner.enter_sent(),
        "Enter is NEVER attempted when the payload insert itself failed",
    )
    _check(
        runner.capture_pane_call_count() == 0,
        "no stability wait is even attempted after a failed payload insert",
    )


def test_submit_after_settling_insert_never_reinserts_text() -> None:
    """Red-first: a later submit must not put a second copy in the composer."""
    runner = _FakeTmuxRunner(
        capture_pane_sequence=["before", "after", "after", "after", "after"],
    )
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=runner,
        stable_samples_required=3, stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=0.1),
    )
    channel.insert("large\\npaste")
    channel.submit()
    payload_inserts = [call for call in runner.calls if "paste-buffer" in call]
    _check(len(payload_inserts) == 1, "submit() cannot reinsert settling text")
    _check(runner.enter_sent(), "submit() sends Enter after the paste settles")


def test_payload_insert_uses_bracketed_paste_never_send_keys_literal() -> None:
    """★ THE KILLING TEST for the 2026-09-07 head-truncation defect.

    ``send-keys -l`` was measured DROPPING THE HEAD of a large payload
    against a real Claude Code TUI pane: a 10.8KB / 142-line message
    beginning ``HEADMARKERZZ1`` and ending ``TAILMARKERZZ2`` was sent, and
    the receiving model — asked for the first and last 25 characters of what
    it had actually received — reported filler from the MIDDLE of the
    payload plus the intact tail, with the head marker appearing nowhere in
    the pane's entire scrollback. The identical payload delivered through
    ``load-buffer`` + ``paste-buffer -p`` arrived head-intact. Nothing
    raised, logged, or reported a failure on the truncating path: dispatch
    briefs were arriving with their opening scope and authority sections
    silently missing.

    This pins BOTH halves, because either alone is satisfiable by a
    mutation that keeps the defect: the payload must go through the buffer
    path (with ``-p``, so the payload's own newlines stay literal instead of
    racing the submitting Enter), AND ``send-keys -l`` must not carry it.
    """
    runner = _FakeTmuxRunner(capture_pane_sequence=["A", "B", "B", "B", "B"])
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=runner,
        stable_samples_required=3, stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=0.1),
    )
    payload = "line one\nline two\nline three"
    channel.send(payload)
    _check(
        not runner.send_keys_literal_used(),
        "the payload NEVER goes through send-keys -l (the head-truncating path)",
    )
    _check(
        any("load-buffer" in c for c in runner.calls),
        "the payload is loaded into a tmux buffer",
    )
    paste_calls = [c for c in runner.calls if "paste-buffer" in c]
    _check(len(paste_calls) == 1, "the payload is pasted exactly once")
    _check("-p" in paste_calls[0], "the paste is BRACKETED (-p), keeping newlines literal")
    _check("-d" in paste_calls[0], "the payload buffer is deleted after the paste")
    _check(
        runner.payloads == [payload],
        f"the buffer carries the payload verbatim, got {runner.payloads!r}",
    )
    load_call = next(c for c in runner.calls if "load-buffer" in c)
    paste_call = paste_calls[0]
    load_buffer_name = load_call[load_call.index("-b") + 1]
    paste_buffer_name = paste_call[paste_call.index("-b") + 1]
    _check(
        load_buffer_name == paste_buffer_name,
        "the paste reads back the buffer this call loaded",
    )
    other = _FakeTmuxRunner(capture_pane_sequence=["A", "B", "B", "B", "B"])
    tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s2", run_fn=other,
        stable_samples_required=3, stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=0.1),
    ).send("a different lane's brief")
    other_load = next(c for c in other.calls if "load-buffer" in c)
    _check(
        other_load[other_load.index("-b") + 1] != load_buffer_name,
        "two concurrent drives never share a buffer name (one lane cannot paste "
        "another lane's payload)",
    )


def test_insert_raises_when_the_payload_buffer_load_fails() -> None:
    """``insert()`` used to call ``_insert`` and DISCARD its boolean, so a
    caller using the declared insert/submit split (``session_hosts.
    DriverChannel``) got a silent no-op followed by a bare ``Enter`` into
    whatever the composer already held. The Codex twin raises here; this one
    now does too.

    ``send()`` is deliberately NOT covered by this change and is asserted
    alongside: it keeps consuming ``_insert``'s boolean, so its established
    fire-and-forget contract (a dead pane is a logged warning, never an
    exception through the verb layer) is unchanged.
    """
    runner = _FakeTmuxRunner(fail_payload_insert=True)
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=runner,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=0.1),
    )
    raised = False
    try:
        channel.insert("some work")
    except DriverChannelSendError:
        raised = True
    _check(raised, "insert() RAISES when the payload never reached the composer")
    _check(not runner.enter_sent(), "a failed insert never leaves a bare Enter behind")

    fire_and_forget = _FakeTmuxRunner(fail_payload_insert=True)
    tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=fire_and_forget,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=0.1),
    ).send("some work")  # must not raise
    _check(
        not fire_and_forget.enter_sent(),
        "send() stays fire-and-forget and still refuses the Enter",
    )


class _CodexWedgeRunner:
    """Hermetic Codex composer model for the stable-gate rollback proofs."""

    def __init__(self, *, stranded_text: str) -> None:
        self.calls: list[list[str]] = []
        self._stranded_text = stranded_text
        self.composer = ""
        self._mode = "idle"
        self._unstable_captures = 0

    def __call__(self, argv: list[str], **_kwargs: Any) -> _FakeCompletedProcess:
        self.calls.append(argv)
        if "capture-pane" in argv:
            return _FakeCompletedProcess(stdout=self._capture())
        if "send-keys" in argv and "-l" in argv:
            text = argv[-1]
            self.composer = text
            self._mode = "unstable" if text == self._stranded_text else "stable"
        elif "send-keys" in argv and argv[-1] == "C-u":
            self.composer = ""
            self._mode = "idle"
        return _FakeCompletedProcess()

    def _capture(self) -> str:
        if not self.composer:
            return "› Ask Codex to do anything"
        if self._mode == "unstable":
            self._unstable_captures += 1
            if self._unstable_captures < 3:
                return f"› {self.composer} rendering-{self._unstable_captures}"
        return f"› {self.composer}"

    def literal_texts(self) -> list[str]:
        return [call[-1] for call in self.calls if "send-keys" in call and "-l" in call]

    def clear_sent(self) -> bool:
        return any(call[-1:] == ["C-u"] for call in self.calls)

    def enter_sent(self) -> bool:
        return any(call[-1:] == ["Enter"] for call in self.calls)


def _codex_wedge_channel(runner: _CodexWedgeRunner) -> _CodexTmuxDriverChannel:
    return _CodexTmuxDriverChannel(
        tmux_bin="tmux",
        session="codex-wedge-proof",
        run_fn=runner,
        sleep_fn=_fake_sleep,
        now_fn=_FakeClock(step=0.1),
        stable_samples=2,
        verify_timeout_seconds=0.4,
    )


def test_stable_failure_rolls_back_to_verified_idle_composer() -> None:
    """RED FIRST: a post-literal stable timeout must not strand the text.

    Failing mutation: delete the failed-insert rollback. The pre-fix channel
    raises the same stable-gate error but leaves ``stranded_text`` in the fake
    composer; this assertion fails before the repair and proves the damaging
    path rather than the benign ready-gate miss.
    """
    stranded_text = "delivery waiting from Coordinator — drain peer_inbox"
    runner = _CodexWedgeRunner(stranded_text=stranded_text)
    channel = _codex_wedge_channel(runner)
    failure = ""
    try:
        channel.insert(stranded_text)
    except DriverChannelSendError as exc:
        failure = str(exc)
    _check(
        failure == (
            "Codex tmux pane 'codex-wedge-proof' did not stabilize before Enter; "
            "submission was not attempted."
        ),
        "stable-gate failure retains its original DriverChannelSendError message",
    )
    _check(
        runner.composer == "" and "Ask Codex to do anything" in runner._capture(),
        "RED proof 1: failed post-literal insert leaves the composer empty "
        "with its idle placeholder",
    )
    _check(runner.clear_sent(), "rollback clears only the failed call's confirmed composer text")
    _check(not runner.enter_sent(), "rollback never sends Enter")


def test_stable_failure_does_not_wedge_the_next_ready_gate() -> None:
    """RED FIRST: without rollback, stranded text erases the next idle prompt.

    Failing mutation: remove rollback or change it to a no-op. The second
    insert then fails at ``_wait_until_ready`` before it can send anything,
    exactly the permanent wedge this repair prevents.
    """
    stranded_text = "delivery waiting from Coordinator — drain peer_inbox"
    follow_up_text = "follow-up"
    runner = _CodexWedgeRunner(stranded_text=stranded_text)
    channel = _codex_wedge_channel(runner)
    with contextlib.suppress(DriverChannelSendError):
        channel.insert(stranded_text)
    next_failure = ""
    try:
        channel.insert(follow_up_text)
    except DriverChannelSendError as exc:
        next_failure = str(exc)
    _check(
        not next_failure,
        "RED proof 2: the next ready gate sees the restored idle placeholder, not stranded text",
    )
    _check(
        runner.literal_texts() == [stranded_text, follow_up_text],
        "the next insert reaches its literal send after rollback",
    )


def test_ready_gate_busy_pane_remains_a_benign_no_paste_failure() -> None:
    """Control: deleting the ready check makes this red by pasting into busy UI."""
    runner = _CodexWedgeRunner(stranded_text="unused")
    runner.composer = "busy human draft"
    channel = _codex_wedge_channel(runner)
    failure = ""
    try:
        channel.insert("must not paste")
    except DriverChannelSendError as exc:
        failure = str(exc)
    _check("never reached an idle prompt" in failure, "busy pane still fails at the ready gate")
    _check(runner.literal_texts() == [], "busy ready-gate failure pastes nothing")
    _check(
        not runner.clear_sent(),
        "busy ready-gate failure never clears a composer it does not own",
    )


def main() -> int:
    print("=== tmux driver channel smoke ===")
    test_enter_sent_only_after_n_consecutive_stable_samples()
    test_non_matching_sample_resets_the_consecutive_counter()
    test_timeout_still_sends_enter_fail_open()
    test_payload_insert_failure_never_attempts_enter()
    test_payload_insert_uses_bracketed_paste_never_send_keys_literal()
    test_insert_raises_when_the_payload_buffer_load_fails()
    test_submit_after_settling_insert_never_reinserts_text()
    test_stable_failure_rolls_back_to_verified_idle_composer()
    test_stable_failure_does_not_wedge_the_next_ready_gate()
    test_ready_gate_busy_pane_remains_a_benign_no_paste_failure()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
