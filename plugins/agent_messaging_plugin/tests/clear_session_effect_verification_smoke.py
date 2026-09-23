#!/usr/bin/env python3
"""Unit smoke for GAU-09 — ``clear_session`` must report the EFFECT, not the SEND.

THE DEFECT, measured 2026-08-18 by ``lane-rotation-notice`` attempting to
rotate itself (recorded as GAU-09 in the workbench backlog): ``clear_session`` returned
``success TRUE {'lifecycle_state': 'live', 'parked': False}`` for a ``/clear``
THAT NEVER HAPPENED. The verb's own docstring was the admission — a
"fire-and-forget ``/clear`` over the resolved host driver's channel" — so it
reported that bytes left, in a return value shaped like a verdict. ``ARMED !=
FIRED``, one layer up from where this repo usually meets it, and the same
family as GAU-02: an instrument whose success is indistinguishable from the
healthy case.

WHAT THIS FILE PINS. Written RED-FIRST, before the fix, so the false-success
shape is captured by an executable assertion rather than by prose:
  * a driver that CAN read its pane back, whose pane is never cleared, must
    make ``clear_session`` FAIL LOUD — pre-fix it returned the success shape
    above, which is exactly the lie;
  * a confirmed clear reports ``cleared=True`` positively;
  * a driver that CANNOT verify reports ``cleared=None`` +
    ``clear_verification='unsupported_on_driver'`` and NEVER ``cleared=True`` —
    the honest-naming degradation, so ``success`` can still never be read as
    "the context was cleared";
  * the park edge is not taken when the clear could not be confirmed;
  * ★ THE HARD RULE: the ``/clear`` text is written EXACTLY ONCE on every
    path, including the failing one. Each fire deposits real text into a live
    input buffer, so a "fix" that retries the send is worse than the defect
    (memory: ``feedback_never_retry_a_blind_injection_that_cannot_confirm_
    itself``). A retry mutation must fail this file.

THE POSITIVE-SIGNATURE TRAP, inherited deliberately rather than reinvented:
``seat_rotation_helper.is_cleared_state`` already learned (P3 live
measurement) that a SUBSTRING check is unsound here — the empty-composer row
``"❯\xa0"`` strips to the bare glyph ``"❯"``, but so does the prefix
of a STILL-POPULATED row like ``"❯\xa0/clear"`` under containment, which
is precisely the un-cleared screen this whole check exists to never accept.
Exact-match-after-strip is the discriminator, and the tmux leg below pins it
with that exact negative control.

SCOPE HONESTY — what this file does NOT prove. Offline and stub-driven: a
fake ``run_fn`` simulates pane content, no real tmux and NO LIVE SESSION IS
EVER DRIVEN (lane constraint: never fire clear_session/drive_session at a live
session while testing). It proves the SHAPE of the fix — ordering, the
positive-signature contract, fail-closed on timeout, the no-retry invariant —
exactly the offline-shape/live-measurement split ``tmux_driver_channel_
smoke.py`` and ``seat_rotation_helper_smoke.py`` already establish. The claim
that the tmux Claude Code composer's cleared signature is ``"❯"`` is
INHERITED from the iTerm2 measurement recorded in ``seat_rotation_helper_
smoke.py`` (same TUI, different host); it is configurable per channel and a
live tmux confirmation leg remains OUTSTANDING, requested from the seat rather
than taken here.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/clear_session_effect_verification_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402

from agent_messaging_plugin import session_hosts  # noqa: E402
from agent_messaging_plugin import tmux_adapter as tmux_module  # noqa: E402
from agent_messaging_plugin.schema import LIFECYCLE_LIVE, LIFECYCLE_SPAWNING  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import VerbError, clear_session  # noqa: E402

_passed = 0
_failed: list[str] = []

_TEST_HOST_VERIFYING = "test-host-verifying"
_TEST_HOST_BLIND = "test-host-blind"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _VerifyingChannel:
    """A driver channel that CAN read its target back — the tmux class of
    driver. ``cleared`` is the simulated post-send truth."""

    def __init__(self, *, cleared: bool) -> None:
        self.sent: list[str] = []
        self._cleared = cleared
        self.verify_calls = 0

    def send(self, text: str) -> None:
        self.sent.append(text)

    def verify_cleared(self) -> bool:
        self.verify_calls += 1
        return self._cleared


class _BlindChannel:
    """A driver channel with no read-back surface at all — the headless
    class of driver. Deliberately has NO ``verify_cleared`` attribute, so
    the verb's capability probe is exercised for real rather than through a
    flag the test sets."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class _FakeDriver:
    def __init__(self, channel: Any) -> None:
        self.channel = channel

    def spawn(self, spec: Any) -> str:
        del spec
        return "host-ref"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> Any:
        del host_ref
        return self.channel

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _live_session(state: StateManagementInterface, agent_instance_id: str, host: str) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id, lane_id="lane-gau09", brief_ref="",
            work_class="read_only", budget_line="gau_fixes_2026-08-18", host=host,
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
    )


def _install(host: str, channel: Any) -> None:
    session_hosts._REGISTRY[host] = _FakeDriver(channel)  # noqa: SLF001


def _uninstall(host: str) -> None:
    session_hosts._REGISTRY.pop(host, None)  # noqa: SLF001


# ---------------------------------------------------------------------------
# THE RED PIN — the measured GAU-09 lie, as an executable assertion.
# ---------------------------------------------------------------------------

def test_unconfirmed_clear_fails_loud_instead_of_returning_success() -> None:
    """THE DEFECT. A pane that is never cleared must not yield a success
    shape. Pre-fix this returned ``{'lifecycle_state': 'live', 'parked':
    False}`` with success=True — the exact verbatim return the backlog
    records — because the verb only ever knew that bytes had left."""
    channel = _VerifyingChannel(cleared=False)
    _install(_TEST_HOST_VERIFYING, channel)
    try:
        state = _state()
        _live_session(state, "agi-gau09-red", _TEST_HOST_VERIFYING)
        raised: VerbError | None = None
        try:
            clear_session(
                state, agent_instance_id="agi-gau09-red", park=False,
                directed_by="operator:none",
            )
        except VerbError as exc:
            raised = exc
        _check(
            raised is not None,
            "an unconfirmed /clear FAILS LOUD instead of returning the success shape",
        )
        _check(
            raised is not None and raised.code == "clear_unverified",
            "the failure carries the stable 'clear_unverified' error token",
        )
        _check(
            channel.verify_calls == 1,
            "the verb actually consulted the driver's read-back surface",
        )
        # ★ THE HARD RULE.
        _check(
            channel.sent == ["/clear"],
            "HARD RULE: '/clear' is deposited EXACTLY ONCE — the failing path never retries",
        )
    finally:
        _uninstall(_TEST_HOST_VERIFYING)


def test_unverified_clear_guidance_names_deferral_not_an_ordinary_message() -> None:
    """GAU-27 — the ``clear_unverified`` message is the CALLER'S ONLY
    INSTRUMENT, and the pre-fix text pointed it straight at the trap.

    Measured 2026-08-19 (GAU-27): a ``/clear`` dispatched at a MID-TURN
    target is QUEUED AS A COMMAND and fires at that turn's end — observed
    ~2 minutes after this very error was returned. The pre-fix message
    asserted the opposite mechanism ("takes it as an ordinary message
    rather than a command") and closed with an action ("clear it externally
    instead") that, taken immediately, deposits a SECOND clear which fires
    into the SUCCESSOR context after the first one lands. So the message
    has to carry three things and refuse a fourth:

      * unverified is NOT lost — the clear may be PENDING;
      * the discriminator: the target's own input QUEUE, readable only
        from outside (the target cannot see it);
      * the protocol that worked — wait for idle, then exactly ONE
        re-issue;
      * and NOT the refuted mechanism claim, which is the part that makes
        a caller believe the clear is gone.

    This asserts message CONTENT because content is the whole defect: the
    error code was already correct and unchanged.
    """
    channel = _VerifyingChannel(cleared=False)
    _install(_TEST_HOST_VERIFYING, channel)
    try:
        state = _state()
        _live_session(state, "agi-gau27-guidance", _TEST_HOST_VERIFYING)
        raised: VerbError | None = None
        try:
            clear_session(
                state, agent_instance_id="agi-gau27-guidance", park=False,
                directed_by="operator:none",
            )
        except VerbError as exc:
            raised = exc
        message = (raised.message if raised is not None else "").lower()
        _check(
            raised is not None and raised.code == "clear_unverified",
            "the error token is unchanged — GAU-27 corrects the guidance, not the code",
        )
        _check(
            "ordinary message" not in message,
            "the REFUTED mechanism claim ('ordinary message rather than a command') is gone",
        )
        _check(
            "clear it externally instead" not in message,
            "the message no longer closes with the immediate re-clear that double-deposits",
        )
        _check(
            "not lost" in message or "not-yet-executed" in message,
            "the message states that an unverified clear may be PENDING, not lost",
        )
        _check(
            "queued" in message,
            "the message names the input QUEUE — the state that discriminates the two worlds",
        )
        _check(
            "do not" in message and "re-send" in message,
            "the no-blind-retry rule survives the rewrite",
        )
        _check(
            "one re-issue" in message and "idle" in message,
            "the message carries the protocol that worked: wait for idle, then ONE re-issue",
        )
        _check(
            channel.sent == ["/clear"],
            "HARD RULE holds on this path too: '/clear' is deposited EXACTLY ONCE",
        )
    finally:
        _uninstall(_TEST_HOST_VERIFYING)


def test_unconfirmed_clear_does_not_take_the_park_edge() -> None:
    """A park recorded beside a clear that never happened would put the lie
    in the LEDGER, outliving the return value that carried it."""
    channel = _VerifyingChannel(cleared=False)
    _install(_TEST_HOST_VERIFYING, channel)
    try:
        state = _state()
        _live_session(state, "agi-gau09-nopark", _TEST_HOST_VERIFYING)
        try:
            clear_session(
                state, agent_instance_id="agi-gau09-nopark", park=True,
                directed_by="operator:none",
            )
        except VerbError:
            pass
        row = read_managed_session(state, "agi-gau09-nopark")
        _check(
            str(row.get("lifecycle_state")) == LIFECYCLE_LIVE,
            "an unconfirmed clear leaves the row LIVE — it never parks on an unproven clear",
        )
        _check(
            channel.sent == ["/clear"],
            "HARD RULE holds on the park path too: '/clear' deposited exactly once",
        )
    finally:
        _uninstall(_TEST_HOST_VERIFYING)


def test_confirmed_clear_reports_the_effect_positively() -> None:
    channel = _VerifyingChannel(cleared=True)
    _install(_TEST_HOST_VERIFYING, channel)
    try:
        state = _state()
        _live_session(state, "agi-gau09-green", _TEST_HOST_VERIFYING)
        result = clear_session(
            state, agent_instance_id="agi-gau09-green", park=False,
            directed_by="operator:none",
        )
        _check(result.get("cleared") is True, "a CONFIRMED clear reports cleared=True")
        _check(
            result.get("clear_verification") == "confirmed",
            "a confirmed clear names its verification as 'confirmed'",
        )
        _check(
            result.get("dispatched") is True,
            "the send is still reported separately as 'dispatched'",
        )
        _check(channel.sent == ["/clear"], "the confirmed path sends '/clear' exactly once")
    finally:
        _uninstall(_TEST_HOST_VERIFYING)


def test_blind_driver_degrades_to_honest_naming_never_to_a_verdict() -> None:
    """Where the driver genuinely cannot read the target back, the answer is
    an explicit NOT-VERIFIED, never a quiet True. ``cleared=None`` is the
    same tri-state discipline this plugin already enforces for the gauge's
    cache columns: 'not measured' must stay distinct from 'measured false'."""
    channel = _BlindChannel()
    _install(_TEST_HOST_BLIND, channel)
    try:
        state = _state()
        _live_session(state, "agi-gau09-blind", _TEST_HOST_BLIND)
        result = clear_session(
            state, agent_instance_id="agi-gau09-blind", park=False,
            directed_by="operator:none",
        )
        _check(
            "cleared" in result and result["cleared"] is None,
            "a driver with no read-back surface reports cleared=None, NOT True",
        )
        _check(
            result.get("clear_verification") == "unsupported_on_driver",
            "the un-verifiable case names WHY it is unverified",
        )
        _check(
            result.get("dispatched") is True,
            "the one thing actually known — the send — is still reported",
        )
        _check(channel.sent == ["/clear"], "the blind path sends '/clear' exactly once")
    finally:
        _uninstall(_TEST_HOST_BLIND)


def test_blind_driver_still_parks_on_request() -> None:
    """The park edge is steward bookkeeping and is NOT contingent on a
    verification the driver could never provide — only on a verification
    that was attempted and FAILED (covered above). Collapsing those two
    would strand every headless session unparkable."""
    channel = _BlindChannel()
    _install(_TEST_HOST_BLIND, channel)
    try:
        state = _state()
        _live_session(state, "agi-gau09-blindpark", _TEST_HOST_BLIND)
        result = clear_session(
            state, agent_instance_id="agi-gau09-blindpark", park=True,
            directed_by="operator:none",
        )
        _check(result.get("parked") is True, "a blind driver still honours park=True")
        _check(
            "cleared" in result and result["cleared"] is None,
            "...while STILL refusing to claim the clear was verified",
        )
    finally:
        _uninstall(_TEST_HOST_BLIND)


# ---------------------------------------------------------------------------
# The tmux leg — the real read-back surface, and the substring trap.
# ---------------------------------------------------------------------------

class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


class _FakeTmuxRunner:
    def __init__(self, *, capture_pane_sequence: list[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._capture_pane_sequence = list(capture_pane_sequence or [])
        self._last = ""

    def __call__(self, argv: list[str], **_kwargs: Any) -> _FakeCompletedProcess:
        self.calls.append(argv)
        if "capture-pane" in argv:
            if self._capture_pane_sequence:
                self._last = self._capture_pane_sequence.pop(0)
            return _FakeCompletedProcess(stdout=self._last)
        return _FakeCompletedProcess()

    def send_key_count(self) -> int:
        return sum(1 for c in self.calls if "send-keys" in c)


class _FakeClock:
    def __init__(self, step: float = 1.0) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        value = self._now
        self._now += self._step
        return value


def _tmux_channel(runner: _FakeTmuxRunner, **kwargs: Any) -> Any:
    return tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=runner,
        sleep_fn=lambda _s: None, now_fn=_FakeClock(),
        **kwargs,
    )


def test_tmux_verify_cleared_accepts_a_genuinely_empty_composer() -> None:
    runner = _FakeTmuxRunner(capture_pane_sequence=["❯\xa0"])
    channel = _tmux_channel(runner)
    _check(
        channel.verify_cleared() is True,
        "tmux verify_cleared() confirms an empty composer row",
    )


def test_tmux_verify_cleared_rejects_a_still_populated_composer() -> None:
    """★ THE NEGATIVE CONTROL, and the whole reason this is an exact match
    rather than a containment test: ``"❯" in "❯\xa0/clear"`` is
    True, so a substring check would call the STRANDED-``/clear`` screen —
    the precise screen the GAU-09 measurement found on the lane's pane —
    a successful clear. This is the mutation that must fail this test."""
    runner = _FakeTmuxRunner(capture_pane_sequence=["❯\xa0/clear"])
    channel = _tmux_channel(runner)
    _check(
        channel.verify_cleared() is False,
        "tmux verify_cleared() REJECTS a composer still holding '/clear' (substring trap)",
    )


def test_tmux_verify_cleared_requires_consecutive_samples() -> None:
    """A reset-then-recover run still confirms, once the streak genuinely
    reaches N in a row. Pairs with the FLAPPING test below — this one alone
    does NOT discriminate reset-vs-no-reset (both reach 3 here), which is
    precisely why the next test exists."""
    runner = _FakeTmuxRunner(
        capture_pane_sequence=["❯\xa0", "❯\xa0/clear", "❯\xa0", "❯\xa0", "❯\xa0"],
    )
    channel = _tmux_channel(runner, clear_stable_samples_required=3)
    _check(
        channel.verify_cleared() is True,
        "a reset-then-recover sequence still confirms once 3 CONSECUTIVE samples match",
    )
    _check(
        runner.calls and all("capture-pane" in c for c in runner.calls),
        "verify_cleared only ever READS the pane — it never sends a key",
    )


def test_tmux_verify_cleared_never_accumulates_across_a_flapping_pane() -> None:
    """★ THE DISCRIMINATING TEST for the streak RESET, added after the
    reset-removal mutation SURVIVED the fixture above.

    The sequence alternates cleared / not-cleared and never once shows two
    cleared frames in a row. A correct positive check therefore NEVER
    confirms and times out fail-closed. A check that merely ACCUMULATES
    matches — dropping the ``stable_count = 0`` reset, turning the positive
    check back into a decaying quiescence guess — tallies five separate
    matches here and wrongly reports the clear as confirmed.

    A flapping pane is the realistic shape of the thing being guarded
    against: a composer that momentarily renders empty mid-redraw while the
    stranded ``/clear`` is still there. Counting those frames cumulatively
    is exactly how a never-cleared pane certifies itself.
    """
    runner = _FakeTmuxRunner(
        capture_pane_sequence=["❯\xa0", "❯\xa0/clear"] * 6,
    )
    channel = _tmux_channel(
        runner, clear_stable_samples_required=3, clear_verify_timeout_seconds=10.0,
    )
    _check(
        channel.verify_cleared() is False,
        "a pane that never shows 2 cleared frames IN A ROW is never confirmed "
        "(kills the no-reset mutation)",
    )


def test_tmux_verify_cleared_fails_closed_on_timeout() -> None:
    """FAIL-CLOSED, unlike this channel's paste-stability wait, which
    deliberately fails OPEN. Opposite defaults on purpose: a paste that
    never visibly renders should still submit, but a clear that was never
    observed must never be reported as one."""
    runner = _FakeTmuxRunner(capture_pane_sequence=["❯\xa0/clear"])
    channel = _tmux_channel(runner, clear_verify_timeout_seconds=3.0)
    _check(
        channel.verify_cleared() is False,
        "verify_cleared() returns False when the deadline passes unconfirmed (fail-CLOSED)",
    )
    _check(
        runner.send_key_count() == 0,
        "HARD RULE: a timed-out verification sends NO keys — it never re-fires '/clear'",
    )


def test_tmux_channel_is_recognised_as_verification_capable() -> None:
    runner = _FakeTmuxRunner(capture_pane_sequence=["❯\xa0"])
    channel = _tmux_channel(runner)
    _check(
        isinstance(channel, session_hosts.ClearVerifyingDriverChannel),
        "the tmux channel satisfies the ClearVerifyingDriverChannel protocol",
    )
    _check(
        not isinstance(_BlindChannel(), session_hosts.ClearVerifyingDriverChannel),
        "...and a channel with no read-back surface does NOT (the probe discriminates)",
    )


def main() -> int:
    print("clear_session effect-verification smoke (GAU-09)")
    for fn in (
        test_unconfirmed_clear_fails_loud_instead_of_returning_success,
        test_unconfirmed_clear_does_not_take_the_park_edge,
        test_unverified_clear_guidance_names_deferral_not_an_ordinary_message,
        test_confirmed_clear_reports_the_effect_positively,
        test_blind_driver_degrades_to_honest_naming_never_to_a_verdict,
        test_blind_driver_still_parks_on_request,
        test_tmux_verify_cleared_accepts_a_genuinely_empty_composer,
        test_tmux_verify_cleared_rejects_a_still_populated_composer,
        test_tmux_verify_cleared_requires_consecutive_samples,
        test_tmux_verify_cleared_never_accumulates_across_a_flapping_pane,
        test_tmux_verify_cleared_fails_closed_on_timeout,
        test_tmux_channel_is_recognised_as_verification_capable,
    ):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
