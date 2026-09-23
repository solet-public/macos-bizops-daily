#!/usr/bin/env python3
"""Unit smoke for public issue #9 — ``drive_session`` must report the EFFECT,
not the SEND. The ``drive_session`` sibling of the GAU-09 fix to
``clear_session`` (``clear_session_effect_verification_smoke.py``).

THE DEFECT, measured 2026-08-18 by ``lane-rotation-notice`` (recorded as
GAU-09 in the workbench backlog, drive_session half): ``drive_session``
returned ``success TRUE {'lifecycle_state': 'live', 'unparked': False}`` for
a drive whose text sat unsubmitted in the target's input buffer. ``send()``
alone can only ever mean "bytes left" — ``ARMED != FIRED``.

WHAT THIS FILE PINS:
  * a driver that CAN read its pane back, whose driven text is observed
    STRANDED (bright white, never left the composer), must make
    ``drive_session`` FAIL LOUD with ``drive_unverified``;
  * a confirmed submit reports ``submitted=True`` positively;
  * a driver that CANNOT verify reports ``submitted=None`` +
    ``drive_verification='unsupported_on_driver'`` and NEVER
    ``submitted=True``;
  * the unpark edge is not taken when the drive could not be confirmed;
  * ★ THE HARD RULE: the driven text is written EXACTLY ONCE on every path.
    A retry mutation must fail this file (memory: ``feedback_never_retry_a_
    blind_injection_that_cannot_confirm_itself``).

★ THE CORRECTION THIS FILE EXISTS TO PIN (steward review, 2026-08-19):
composer-idle is NOT a sufficient signature for "was the text taken up as a
turn" the way it is for clear_session's "is this pane blank at a fresh
prompt". An idle composer is what BOTH a genuine submit and an entirely
unsent drive (``send()`` silently no-op'd on a dead pane — this channel's
own ``send()`` swallows that failure into a logged warning) look like from
the outside. ``test_tmux_verify_driven_rejects_idle_that_never_populated``
below is the DISCRIMINATING test: a composer that is idle for the ENTIRE
poll, having never once shown the driven text land, must return ``None``
(could not determine) rather than ``True``. A "fix" that treats composer-
idle-by-absence as a confirmed submit — collapsing the population gate —
survives every OTHER test in this file and is caught only by this one.

SCOPE HONESTY, same posture as the GAU-09 file this mirrors: offline and
stub-driven, no real tmux, NO LIVE SESSION IS EVER DRIVEN. Proves the SHAPE
of the fix — ordering, the two-phase population-then-idle contract, the
positive stranded-colour signature, fail-toward-unknown on timeout, the
no-retry invariant.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/drive_session_effect_verification_smoke.py
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
from agent_messaging_plugin.schema import LIFECYCLE_LIVE, LIFECYCLE_PARKED  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    VerbError,
    drive_session,
)

_passed = 0
_failed: list[str] = []

_TEST_HOST_VERIFYING = "test-host-drive-verifying"
_TEST_HOST_BLIND = "test-host-drive-blind"


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
    driver. ``submitted`` is the simulated post-send truth: ``True``/
    ``False``/``None`` map onto ``verify_driven``'s own tri-state."""

    def __init__(self, *, submitted: bool | None) -> None:
        self.sent: list[str] = []
        self._submitted = submitted
        self.verify_calls: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def verify_driven(self, text: str) -> bool | None:
        self.verify_calls.append(text)
        return self._submitted


class _BlindChannel:
    """A driver channel with no read-back surface at all — the headless
    class of driver. Deliberately has NO ``verify_driven`` attribute, so the
    verb's capability probe is exercised for real."""

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


def _live_session(
    state: StateManagementInterface, agent_instance_id: str, host: str,
    lifecycle_state: str = LIFECYCLE_LIVE,
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id, lane_id="lane-drive-honesty",
            brief_ref="", work_class="read_only",
            budget_line="fleet:session-mgmt-bugwave-2026-08-19", host=host,
        ),
    )
    from agent_messaging_plugin.schema import LIFECYCLE_SPAWNING  # noqa: PLC0415
    if lifecycle_state == LIFECYCLE_SPAWNING:
        return
    transition_lifecycle_state(
        state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
    )
    if lifecycle_state != LIFECYCLE_LIVE:
        transition_lifecycle_state(
            state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_LIVE,
            to_state=lifecycle_state, directed_by="operator:none",
        )


def _install(host: str, channel: Any) -> None:
    session_hosts._REGISTRY[host] = _FakeDriver(channel)  # noqa: SLF001


def _uninstall(host: str) -> None:
    session_hosts._REGISTRY.pop(host, None)  # noqa: SLF001


# ---------------------------------------------------------------------------
# THE RED PIN — the measured lie, as an executable assertion.
# ---------------------------------------------------------------------------

def test_stranded_drive_fails_loud_instead_of_returning_success() -> None:
    """THE DEFECT. A drive positively observed stranded must not yield a
    success shape. Pre-fix this returned ``{'lifecycle_state': 'live',
    'unparked': False}`` with success=True — the exact verbatim return the
    backlog records — because the verb only ever knew that bytes had left."""
    channel = _VerifyingChannel(submitted=False)
    _install(_TEST_HOST_VERIFYING, channel)
    try:
        state = _state()
        _live_session(state, "agi-drive-red", _TEST_HOST_VERIFYING)
        raised: VerbError | None = None
        try:
            drive_session(
                state, agent_instance_id="agi-drive-red", text="do the thing",
                directed_by="operator:none",
            )
        except VerbError as exc:
            raised = exc
        _check(
            raised is not None,
            "a stranded drive FAILS LOUD instead of returning the success shape",
        )
        _check(
            raised is not None and raised.code == "drive_unverified",
            "the failure carries the stable 'drive_unverified' error token",
        )
        _check(
            channel.verify_calls == ["do the thing"],
            "the verb actually consulted the driver's read-back surface with the driven text",
        )
        # ★ THE HARD RULE.
        _check(
            channel.sent == ["do the thing"],
            "HARD RULE: the driven text is deposited EXACTLY ONCE — the failing path never retries",
        )
    finally:
        _uninstall(_TEST_HOST_VERIFYING)


def test_unconfirmed_drive_does_not_take_the_unpark_edge() -> None:
    """An unpark recorded beside a drive that never landed would put the
    lie in the LEDGER, outliving the return value that carried it."""
    channel = _VerifyingChannel(submitted=False)
    _install(_TEST_HOST_VERIFYING, channel)
    try:
        state = _state()
        _live_session(
            state, "agi-drive-nounpark", _TEST_HOST_VERIFYING,
            lifecycle_state=LIFECYCLE_PARKED,
        )
        try:
            drive_session(
                state, agent_instance_id="agi-drive-nounpark", text="wake up",
                directed_by="operator:none",
            )
        except VerbError:
            pass
        row = read_managed_session(state, "agi-drive-nounpark")
        _check(
            str(row.get("lifecycle_state")) == LIFECYCLE_PARKED,
            "an unconfirmed drive leaves the row PARKED — it never unparks on an unproven drive",
        )
        _check(
            channel.sent == ["wake up"],
            "HARD RULE holds on the unpark path too: text deposited exactly once",
        )
    finally:
        _uninstall(_TEST_HOST_VERIFYING)


def test_confirmed_drive_reports_the_effect_positively() -> None:
    channel = _VerifyingChannel(submitted=True)
    _install(_TEST_HOST_VERIFYING, channel)
    try:
        state = _state()
        _live_session(state, "agi-drive-green", _TEST_HOST_VERIFYING)
        result = drive_session(
            state, agent_instance_id="agi-drive-green", text="do the thing",
            directed_by="operator:none",
        )
        _check(result.get("submitted") is True, "a CONFIRMED drive reports submitted=True")
        _check(
            result.get("drive_verification") == "confirmed",
            "a confirmed drive names its verification as 'confirmed'",
        )
        _check(
            result.get("dispatched") is True,
            "the send is still reported separately as 'dispatched'",
        )
        _check(
            channel.sent == ["do the thing"], "the confirmed path sends the text exactly once",
        )
    finally:
        _uninstall(_TEST_HOST_VERIFYING)


def test_blind_driver_degrades_to_honest_naming_never_to_a_verdict() -> None:
    """Where the driver genuinely cannot read the target back, the answer is
    an explicit NOT-VERIFIED, never a quiet True."""
    channel = _BlindChannel()
    _install(_TEST_HOST_BLIND, channel)
    try:
        state = _state()
        _live_session(state, "agi-drive-blind", _TEST_HOST_BLIND)
        result = drive_session(
            state, agent_instance_id="agi-drive-blind", text="do the thing",
            directed_by="operator:none",
        )
        _check(
            "submitted" in result and result["submitted"] is None,
            "a driver with no read-back surface reports submitted=None, NOT True",
        )
        _check(
            result.get("drive_verification") == "unsupported_on_driver",
            "the un-verifiable case names WHY it is unverified",
        )
        _check(
            result.get("dispatched") is True,
            "the one thing actually known — the send — is still reported",
        )
        _check(channel.sent == ["do the thing"], "the blind path sends text exactly once")
    finally:
        _uninstall(_TEST_HOST_BLIND)


def test_blind_driver_still_unparks_on_request() -> None:
    """The unpark edge is steward bookkeeping and is NOT contingent on a
    verification the driver could never provide — only on a verification
    that was attempted and FAILED (covered above)."""
    channel = _BlindChannel()
    _install(_TEST_HOST_BLIND, channel)
    try:
        state = _state()
        _live_session(
            state, "agi-drive-blindunpark", _TEST_HOST_BLIND,
            lifecycle_state=LIFECYCLE_PARKED,
        )
        result = drive_session(
            state, agent_instance_id="agi-drive-blindunpark", text="wake up",
            directed_by="operator:none",
        )
        _check(result.get("unparked") is True, "a blind driver still un-parks the row")
        _check(
            "submitted" in result and result["submitted"] is None,
            "...while STILL refusing to claim the drive was verified",
        )
    finally:
        _uninstall(_TEST_HOST_BLIND)


# ---------------------------------------------------------------------------
# The tmux leg — the real read-back surface, and the composer-idle trap.
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

    def capture_pane_used_dash_e(self) -> bool:
        return all("-e" in c for c in self.calls if "capture-pane" in c)


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


_PROBE_TEXT = "claim role and report"


def test_tmux_verify_driven_confirms_population_then_idle() -> None:
    """The genuine success shape: the driven text is observed LANDING in
    the composer, then the composer settles back to idle. Only THIS
    sequence — population, then idle — counts.

    The population frame is deliberately UNCOLOURED here, not bright
    white: by the time ``verify_driven`` starts polling, ``send()`` has
    ALREADY pressed Enter (this method is only ever called after ``send()``
    returns), so there is no legitimate "mid-flight, about to submit"
    bright-white frame left to observe during this poll — any bright-white
    sample seen here is genuine evidence of a stuck composer, which is
    exactly what the stranded-detector test below covers instead."""
    runner = _FakeTmuxRunner(
        capture_pane_sequence=[
            f"❯ {_PROBE_TEXT}",
            "❯\xa0", "❯\xa0", "❯\xa0",
        ],
    )
    channel = _tmux_channel(runner)
    _check(
        channel.verify_driven(_PROBE_TEXT) is True,
        "tmux verify_driven() confirms a submit once text is observed landing then leaving",
    )
    _check(
        runner.send_key_count() == 0, "verify_driven only ever READS the pane — never sends a key",
    )
    _check(
        runner.capture_pane_used_dash_e(),
        "every capture-pane call uses -e (colour-preserving) — plain capture sees no colour at all",
    )


_MULTILINE_PROBE_TEXT = (
    "Dispatch: establish ground truth first.\n\n"
    "1. Read the driver channel code.\n"
    "2. Fix the readiness gate.\n"
    "3. Add rollback for a failed insert.\n"
)
"""A payload shaped like a real dispatch: multi-line, so the TUI COLLAPSES it
in the composer instead of echoing it. ``_PROBE_TEXT`` above is 21 characters
on one row — the only payload shape the pre-2026-09-07 population anchor
could see, and the reason the defect below survived every earlier fix to this
channel."""


def _collapsed_paste_row(text: str) -> str:
    """The composer row a Claude Code TUI renders for a collapsed bracketed
    paste of ``text`` — measured live 2026-09-07 (``[Pasted text #1 +141
    lines]`` for a 142-line payload, ``+8`` for a 9-line one; the declared
    count is the payload's own newline count in both)."""
    return f"❯ [Pasted text #1 +{text.count(chr(10))} lines]"


def test_tmux_verify_driven_confirms_a_collapsed_multiline_paste() -> None:
    """★ THE KILLING TEST for the 2026-09-07 drive false-negative.

    ``_composer_content_matches`` required the composer row to be a PREFIX of
    the driven text. A Claude Code TUI does not echo a multi-line paste into
    the composer at all — it collapses it to ``[Pasted text #1 +N lines]``,
    which is not a prefix of anything. Measured live: an 8.6KB real dispatch
    brief held ``populated=False`` for the entire poll window, so
    ``verify_driven`` ran its full 120s deadline observing nothing, returned
    ``None``, and ``drive_session`` raised ``drive_unverified`` for a drive
    that HAD been delivered — skipping the un-park and the ``report_by``
    re-arm on the way out. The pane was live and healthy the whole time.

    Deterministic in payload size, which is why it recurred on 2026-08-24,
    08-30, 09-02, 09-06 and three times on 09-07 across fixes that each
    correctly closed a different defect in this same channel.
    """
    runner = _FakeTmuxRunner(
        capture_pane_sequence=[
            _collapsed_paste_row(_MULTILINE_PROBE_TEXT),
            "❯\xa0", "❯\xa0", "❯\xa0",
        ],
    )
    channel = _tmux_channel(runner)
    _check(
        channel.verify_driven(_MULTILINE_PROBE_TEXT) is True,
        "tmux verify_driven() confirms a submit whose composer form was a COLLAPSED paste",
    )
    _check(
        runner.send_key_count() == 0,
        "the collapsed-paste path still only ever READS the pane",
    )


def test_tmux_verify_driven_rejects_a_collapsed_paste_of_someone_elses_text() -> None:
    """The discriminator that keeps the fix above from being a wildcard.

    A paste placeholder is a WEAKER anchor than the prefix check — the row
    carries no payload bytes, only a line count — so the count is what makes
    it positive evidence about OUR drive rather than about any paste at all.
    A placeholder whose line count is not ours must not satisfy the
    population gate; without this, ``verify_driven`` would confirm a submit
    against another writer's paste sitting in the composer, which is the
    same class of lie (``ARMED != FIRED``) the method exists to close.
    """
    foreign = _collapsed_paste_row("one\ntwo\nthree\nfour\nfive\nsix\nseven\n")
    runner = _FakeTmuxRunner(
        capture_pane_sequence=[foreign, "❯\xa0", "❯\xa0", "❯\xa0", "❯\xa0"],
    )
    channel = _tmux_channel(runner)
    _check(
        channel.verify_driven(_MULTILINE_PROBE_TEXT) is None,
        "a collapsed paste with a FOREIGN line count is not evidence of our drive",
    )


def test_tmux_verify_driven_detects_positive_stranded_input() -> None:
    """★ THE POSITIVE-FAILURE DETECTOR. Bright white (SGR 97) driven text
    still sitting in the composer is a definitive, immediate FALSE — no
    need to poll to a timeout, unlike could-not-determine."""
    runner = _FakeTmuxRunner(
        capture_pane_sequence=[f"❯ \x1b[97m{_PROBE_TEXT}\x1b[0m"],
    )
    channel = _tmux_channel(runner)
    _check(
        channel.verify_driven(_PROBE_TEXT) is False,
        "tmux verify_driven() REJECTS bright-white stranded driven text",
    )


def test_tmux_verify_driven_does_not_treat_dim_ghost_text_as_stranded() -> None:
    """★ THE NEGATIVE CONTROL for the colour discriminator. SGR 2 (dim) is
    ghost/placeholder text, not real stranded input — a check that matched
    on ANY colour, or on content alone, would wrongly reject this pane."""
    runner = _FakeTmuxRunner(
        capture_pane_sequence=[
            f"❯ \x1b[2m{_PROBE_TEXT}\x1b[0m",
            "❯\xa0", "❯\xa0", "❯\xa0",
        ],
    )
    channel = _tmux_channel(runner)
    _check(
        channel.verify_driven(_PROBE_TEXT) is not False,
        "dim ghost text is NOT read as stranded (would otherwise be a False positive)",
    )


def test_tmux_verify_driven_rejects_idle_that_never_populated() -> None:
    """★★ THE DISCRIMINATING TEST FOR THE CORRECTION (steward review,
    2026-08-19). A composer that is idle for the ENTIRE poll, having NEVER
    once shown the driven text land, must return None — could not
    determine — not True. This is the exact shape of ``send()`` silently
    no-op'd on a dead pane (this channel's own contract: a dead-pane send
    failure is swallowed into a logged warning, never raised) followed by
    a caller mistaking "never populated" for "already submitted".

    A "fix" that accepts composer-idle alone as a confirmed submit —
    dropping the population gate — makes THIS test fail while every other
    test in this file still passes: that is precisely why it exists as its
    own case rather than folded into the confirm test above."""
    runner = _FakeTmuxRunner(
        capture_pane_sequence=["❯\xa0"] * 6,
    )
    channel = _tmux_channel(runner, drive_verify_timeout_seconds=3.0)
    _check(
        channel.verify_driven(_PROBE_TEXT) is None,
        "an idle composer that NEVER observed the driven text land is could-not-determine, "
        "not a confirmed submit (kills the composer-idle-by-absence mutation)",
    )


def test_tmux_verify_driven_fails_toward_unknown_on_stranded_that_never_clears() -> None:
    """A populated composer that stays populated (never goes idle, never
    hits the stranded colour check because our fixture uses no colour at
    all here) still resolves to could-not-determine on timeout, never a
    silent True."""
    runner = _FakeTmuxRunner(
        capture_pane_sequence=[f"❯ {_PROBE_TEXT}"] * 6,
    )
    channel = _tmux_channel(runner, drive_verify_timeout_seconds=3.0)
    _check(
        channel.verify_driven(_PROBE_TEXT) is None,
        "a populated-but-uncolored composer that never idles times out to None, never True",
    )


def test_tmux_channel_is_recognised_as_verification_capable() -> None:
    runner = _FakeTmuxRunner(capture_pane_sequence=["❯\xa0"])
    channel = _tmux_channel(runner)
    _check(
        isinstance(channel, session_hosts.DriveVerifyingDriverChannel),
        "the tmux channel satisfies the DriveVerifyingDriverChannel protocol",
    )
    _check(
        not isinstance(_BlindChannel(), session_hosts.DriveVerifyingDriverChannel),
        "...and a channel with no read-back surface does NOT (the probe discriminates)",
    )


def main() -> int:
    print("drive_session effect-verification smoke (public issue #9)")
    for fn in (
        test_stranded_drive_fails_loud_instead_of_returning_success,
        test_unconfirmed_drive_does_not_take_the_unpark_edge,
        test_confirmed_drive_reports_the_effect_positively,
        test_blind_driver_degrades_to_honest_naming_never_to_a_verdict,
        test_blind_driver_still_unparks_on_request,
        test_tmux_verify_driven_confirms_population_then_idle,
        test_tmux_verify_driven_confirms_a_collapsed_multiline_paste,
        test_tmux_verify_driven_rejects_a_collapsed_paste_of_someone_elses_text,
        test_tmux_verify_driven_detects_positive_stranded_input,
        test_tmux_verify_driven_does_not_treat_dim_ghost_text_as_stranded,
        test_tmux_verify_driven_rejects_idle_that_never_populated,
        test_tmux_verify_driven_fails_toward_unknown_on_stranded_that_never_clears,
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
