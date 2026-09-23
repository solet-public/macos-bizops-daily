"""Detached iTerm2 seat self-rotation helper (rotation-systematization P2
slice A, ratified 2026-08-07). Console one-shot invoked by the seat itself
at a natural rotation boundary — deliberately NOT a ``@platform_process``
verb, because it must survive the seat's own turn ending, which a
synchronous verb call cannot do.

Ruling 1 (P1 ratification, `workbench/2026-08-07_rotation_systematization_
findings_rotation-impl.md`): composes the ``iterm2`` module directly, the
same primitives `iterm2_coding_agent_management_plugin/iterm_python_api.py`
uses, rather than importing from that plugin — measured this lane: no
cross-plugin import precedent exists in the direction this helper needs
(``agent_messaging_plugin`` -> ``iterm2_coding_agent_management_plugin``);
the only existing coupling runs the other way. Self-contained boilerplate,
small duplication accepted per that ruling.

Sequence (brief-ruled fix shape, P1 §P1(a)): resolve the seat's own pane by
a LIVE ``user.role`` tag read (the tag does not replay to late-attaching
clients — R1 spike finding — so it is never cached across invocations),
gated 0/1/N (exactly one match or refuse — the ambiguity trap this lane's
own measurement found the sibling plugin's ``list()``/``_terminate_by_
snapshot`` do NOT defend against, `iterm2_coding_agent_management_plugin/
plugin.py:328,547-548`); CAPTURE any non-empty composer content from the
screen (relay-not-disregard, 2026-08-28: killed text could be a stranded
remote-control draft or a genuine unsent operator draft — it is carried in
the envelope and printed immediately for the fire log, never silently
destroyed); send the clear leg as FIVE separate sends with gaps — space,
kill-line (C-u, 0x15), ``/clear``, ``\\r``, ``\\r``. The preamble is the
2026-08-28 ghost-text-matrix finding: a leading flush-Enter is DANGEROUS
(it SUBMITS a stranded remote-control draft as a prompt); the space
suppresses any ghost suggestion and realizes composer state, and the
kill-line empties all real content. The trailing double CR is the
2026-08-27 injection joseki: a slash command's first CR can be consumed by
the autocomplete menu as "accept selection", so a second CR performs the
actual submit and is a no-op when no menu opened — a single CR after
``/clear`` was measured live leaving the command accepted-but-unsubmitted,
the stranded-composer signature. Then poll-settle on a POSITIVE
cleared-state signature (ruling 4 — quiescence alone does not prove
clear); send the pickup prompt and a separate ``\\r``. Every step failure
aborts in place and is reported — never proceeds to a later step on a
failed earlier one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .driver_texts import render_driver_text
from .submit_conventions import ITERM2_SEAT_SUBMIT_CONVENTION

try:
    import iterm2 as _iterm2_module  # pyright: ignore[reportMissingImports]
except ModuleNotFoundError as _exc:  # pragma: no cover - covered by the blocked-import smoke leg
    # DISCLOSED, never silent, and never fatal at import. This plugin does not
    # DECLARE iterm2: the distribution arrives only with
    # iterm2_coding_agent_management_plugin, which the shipped macos-bizops profile
    # deliberately excludes. A module-scope hard import therefore made this whole
    # module -- pane resolution, settle detection, the send-sequence ordering
    # contract, all of it pure logic -- unimportable on a headless adopter box.
    # Measured by the born-clone gate 2026-08-08 on a bundle from master.
    #
    # The absence is recorded and re-raised at the ONE place that actually drives
    # iTerm2 (see run_rotation's connect step), so the import survives while a
    # real attempt to drive a pane still fails loudly and by name.
    _iterm2_module = None
    _iterm2_import_error: str | None = str(_exc)
else:
    _iterm2_import_error = None

# Bound exactly once: pyright strict treats an uppercase name as a constant and
# refuses a second assignment, so the branch writes the lowercase working name.
ITERM2_IMPORT_ERROR: Final[str | None] = _iterm2_import_error

# The iterm2 distribution ships no py.typed marker (same rationale as
# iterm_python_api.py) -- held as Any to bypass pyright strict's blanket
# private-import flags on it. ``None`` here means the bindings are absent, which
# is a legitimate state on any machine that is not an operator's iTerm2 seat.
_iterm: Any = _iterm2_module

ITERM2_UNAVAILABLE_MESSAGE: Final[str] = (
    "the iTerm2 Python bindings are not installed, so no pane can be driven. This is "
    "SEAT machinery for an operator's iTerm2 workstation and is meaningless on a "
    "headless box; the 'iterm2' distribution ships with "
    "iterm2_coding_agent_management_plugin, which this profile may deliberately exclude"
)

USER_ROLE_VAR = "user.role"

DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_STABLE_SAMPLES_REQUIRED = 3
# Recalibrated 2026-08-07 after live leg #1's measured failure (findings file
# §4): the original 15.0s was margined against the P2 disposable-rig floor
# (~0.3s, a bare-boot pane with none of the real seat's post-/clear work) --
# the REAL seat's settle floor, live-measured, is ~14-15s (SessionStart hooks
# including memory passthrough, MCP stdio bridge respawn/reconnect, full TUI
# redraw), which consumed the entire original margin and produced exactly
# the no-manual-keystroke failure this helper exists to prevent. 120s gives
# ~8x margin over the measured floor. A long timeout costs nothing in the
# common case (wait_for_settle returns as soon as it sees
# DEFAULT_STABLE_SAMPLES_REQUIRED consecutive matches, not after the full
# window) -- a short one strands the seat dark, needing an operator
# keystroke, which is the one failure mode this whole helper exists to
# eliminate. Asymmetric cost is the whole rationale: this errs long.
DEFAULT_SETTLE_TIMEOUT_SECONDS = 120.0
# Gap between the clear leg's individual sends (space, kill-line, /clear,
# CR, menu-confirm CR) so no send lands while the prior one is still being
# processed/rendered — the operator injection joseki (2026-08-27) specifies
# "settle gaps between sends". Injectable per call so smokes run gapless.
DEFAULT_SEND_GAP_SECONDS = 1.0

# C-u: empties the composer of ALL real content, including a stranded
# remote-control draft (measured 2026-08-28). The old flush-Enter preamble
# would SUBMIT such a draft instead — that is why it was removed.
COMPOSER_KILL_LINE = "\x15"

STEP_RESOLVE_PANE = "resolve_pane"
STEP_CAPTURE_COMPOSER = "capture_composer"
STEP_SEND_CLEAR_PREAMBLE_SPACE = "send_clear_preamble_space"
STEP_SEND_CLEAR_PREAMBLE_KILL = "send_clear_preamble_kill"
STEP_SEND_CLEAR_TEXT = "send_clear_text"
STEP_SEND_CLEAR_CR = "send_clear_cr"
STEP_SEND_CLEAR_MENU_CONFIRM_CR = "send_clear_menu_confirm_cr"
STEP_SETTLE_WAIT = "settle_wait"
STEP_SEND_MODEL_PREAMBLE_SPACE = "send_model_preamble_space"
STEP_SEND_MODEL_PREAMBLE_KILL = "send_model_preamble_kill"
STEP_SEND_MODEL_TEXT = "send_model_text"
STEP_SEND_MODEL_CR = "send_model_cr"
STEP_SEND_MODEL_MENU_CONFIRM_CR = "send_model_menu_confirm_cr"
STEP_MODEL_SETTLE_WAIT = "model_settle_wait"
STEP_CONFIRM_COMPOSER_CONTENT = "confirm_composer_content"
STEP_SEND_PICKUP_TEXT = "send_pickup_text"
STEP_PASTE_STABLE_WAIT = "paste_stable_wait"
STEP_SEND_PICKUP_CR = "send_pickup_cr"
STEP_CONFIRM_SUBMIT = "confirm_submit"

CODE_NO_PANE_MATCH = "no_pane_match"
CODE_AMBIGUOUS_PANE_MATCH = "ambiguous_pane_match"
CODE_SETTLE_TIMEOUT = "settle_timeout"
CODE_PASTE_STABLE_TIMEOUT = "paste_stable_timeout"
CODE_SUBMIT_TIMEOUT = "submit_timeout"
CODE_COMPOSER_CONTENT_MISMATCH = "composer_content_mismatch"


@dataclass(frozen=True, slots=True)
class RoleRow:
    """One live-scanned iTerm2 session's role tag + opaque identifiers.

    Deliberately a flat, iterm2-module-free shape so pane resolution
    (:func:`resolve_pane_matches`/:func:`resolve_single_pane`) is a pure
    function, smokeable without a real iTerm2 connection.
    """

    role: str
    session_id: str
    tab_id: int
    window_id: int


@dataclass(frozen=True, slots=True)
class PaneMatch:
    session_id: str
    tab_id: int
    window_id: int


class PaneResolutionError(Exception):
    """Raised by :func:`resolve_single_pane` on 0 or N>1 matches (the
    strict 0/1/N gate this lane's own measurement found the sibling
    plugin's dict-collapse composition does NOT enforce)."""

    def __init__(self, code: str, message: str, candidates: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidates = candidates or []


class HelperStepError(Exception):
    """Raised when a specific numbered step in the send sequence fails.
    Callers must abort at the failing step -- never attempt a later step
    on a failed earlier one (see module docstring)."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.message = message


@dataclass(frozen=True, slots=True)
class SettleDiagnostics:
    """Summary counters (never a per-sample log) from one
    :func:`wait_for_settle` run -- carried on BOTH the completed and refused
    paths, per the live leg #1 finding that a settle-timeout envelope with no
    timing detail leaves flapping-vs-genuinely-slow indistinguishable after
    the fact (findings file §4: "the log records no per-sample timing, so
    flapping renders inside the window cannot be excluded from this envelope
    alone"). ``current_streak_first_match_*`` describes the streak that was
    EITHER the winning one (on success) or still in progress at timeout (on
    a refusal) -- it resets to ``None`` every time a non-matching sample
    breaks an in-progress streak, so on return/raise it always describes
    only the CURRENT (most recent) streak, never a stale one from earlier in
    the poll."""

    samples_taken: int
    streak_reset_count: int
    current_streak_first_match_sample_index: int | None
    current_streak_first_match_elapsed_seconds: float | None
    elapsed_at_last_reset_seconds: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples_taken": self.samples_taken,
            "streak_reset_count": self.streak_reset_count,
            "current_streak_first_match_sample_index": self.current_streak_first_match_sample_index,
            "current_streak_first_match_elapsed_seconds": (
                self.current_streak_first_match_elapsed_seconds
            ),
            "elapsed_at_last_reset_seconds": self.elapsed_at_last_reset_seconds,
        }


class SettleTimeoutError(Exception):
    """Raised by :func:`wait_for_settle` when the cleared-state signature
    never stabilizes within the timeout -- fails closed: the caller must
    NOT send the pickup prompt after this. Carries :attr:`diagnostics` so
    the caller's refused envelope can localize settle timing directly."""

    def __init__(self, message: str, diagnostics: SettleDiagnostics) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class ScreenStabilityTimeoutError(Exception):
    """Raised by :func:`wait_for_screen_stable` when the screen never stops
    changing within the timeout. Fix loop #2 item 1/2 (live leg #2, findings
    file §4): the TUI coalesces a large multi-line ``async_send_text`` as a
    bracketed PASTE, and a ``\\r`` arriving while that paste is still being
    processed/rendered gets absorbed as a literal newline inside the
    composer instead of submitting -- live-measured on a disposable pane
    (a CR sent immediately never submitted a 12KB payload across 6+
    seconds; a CR sent only after the screen stabilized submitted cleanly
    in under 1s). This is the fail-closed guard for that race: the caller
    must NOT send the submitting CR while the screen is still settling
    from the paste render."""

    def __init__(self, message: str, diagnostics: SettleDiagnostics) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class _PollTimeoutError(Exception):
    """Internal-only timeout signal from :func:`_poll_until_match` -- both
    :func:`wait_for_settle` and :func:`wait_for_screen_stable` catch this
    and re-raise their OWN public exception type, so callers never see this
    class directly."""

    def __init__(self, message: str, diagnostics: SettleDiagnostics) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


async def _poll_until_match(
    session: Any,
    *,
    is_match: Any,
    what: str,
    poll_interval_seconds: float,
    stable_samples_required: int,
    timeout_seconds: float,
    now_fn: Any,
    sleep_fn: Any,
) -> SettleDiagnostics:
    """Shared poll-until-N-consecutive-matches engine behind BOTH
    :func:`wait_for_settle` (``is_match`` = a positive signature check
    against the CURRENT sample) and :func:`wait_for_screen_stable`
    (``is_match`` = "identical to the PREVIOUS sample", carried via a
    closure) -- same diagnostics shape, same fail-closed timeout contract,
    different meaning of "match" per caller. ``is_match`` takes the current
    sample's cleaned line list and returns whether this sample counts
    toward the consecutive-match streak; a callable rather than a fixed
    predicate is what lets the two callers share this loop despite needing
    different comparisons."""
    deadline = now_fn() + timeout_seconds
    start = deadline - timeout_seconds
    stable_count = 0
    samples_taken = 0
    streak_reset_count = 0
    streak_first_index: int | None = None
    streak_first_elapsed: float | None = None
    elapsed_at_last_reset: float | None = None
    while True:
        now = now_fn()
        if now > deadline:
            raise _PollTimeoutError(
                f"{what} not confirmed within {timeout_seconds}s "
                f"({stable_count}/{stable_samples_required} consecutive stable samples "
                f"at timeout, {samples_taken} total samples)",
                SettleDiagnostics(
                    samples_taken=samples_taken,
                    streak_reset_count=streak_reset_count,
                    current_streak_first_match_sample_index=streak_first_index,
                    current_streak_first_match_elapsed_seconds=streak_first_elapsed,
                    elapsed_at_last_reset_seconds=elapsed_at_last_reset,
                ),
            )
        contents = await session.async_get_screen_contents()
        lines = [contents.line(i).string for i in range(contents.number_of_lines)]
        samples_taken += 1
        elapsed = now - start
        if is_match(lines):
            if stable_count == 0:
                streak_first_index = samples_taken
                streak_first_elapsed = elapsed
            stable_count += 1
            if stable_count >= stable_samples_required:
                return SettleDiagnostics(
                    samples_taken=samples_taken,
                    streak_reset_count=streak_reset_count,
                    current_streak_first_match_sample_index=streak_first_index,
                    current_streak_first_match_elapsed_seconds=streak_first_elapsed,
                    elapsed_at_last_reset_seconds=elapsed_at_last_reset,
                )
        else:
            if stable_count > 0:
                streak_reset_count += 1
                elapsed_at_last_reset = elapsed
            stable_count = 0
            streak_first_index = None
            streak_first_elapsed = None
        await sleep_fn(poll_interval_seconds)


def resolve_pane_matches(rows: list[RoleRow], role_tag: str) -> list[PaneMatch]:
    """Every row whose ``role`` exactly matches ``role_tag``, in scan order.

    Pure filter, no I/O -- the 0/1/N COUNT is what :func:`resolve_single_pane`
    gates on; this function itself never refuses, so a caller inspecting
    ambiguity (rather than failing on it) can still use it directly."""
    return [
        PaneMatch(session_id=row.session_id, tab_id=row.tab_id, window_id=row.window_id)
        for row in rows
        if row.role == role_tag
    ]


def resolve_single_pane(rows: list[RoleRow], role_tag: str) -> PaneMatch:
    """The strict 0/1/N gate: exactly one match or refuse.

    Never returns on N=0 or N>1 -- raises :class:`PaneResolutionError`
    with the exact session_ids found on ambiguity, so the caller can
    report them rather than guess."""
    matches = resolve_pane_matches(rows, role_tag)
    if not matches:
        raise PaneResolutionError(
            CODE_NO_PANE_MATCH, f"no pane tagged user.role={role_tag!r} found",
        )
    if len(matches) > 1:
        raise PaneResolutionError(
            CODE_AMBIGUOUS_PANE_MATCH,
            f"{len(matches)} panes tagged user.role={role_tag!r} found; refusing",
            candidates=[m.session_id for m in matches],
        )
    return matches[0]


def clean_screen_text(raw: str) -> str:
    """Strip NUL bytes iTerm2's screen-content API renders in place of
    spacing (measured trap, `workbench/2026-08-03_r1_tmux_single_substrate_
    spike/FINDINGS.md` era iTerm2-API findings) before any string match."""
    return raw.replace("\x00", "")


def is_cleared_state(lines: list[str], cleared_signature: str) -> bool:
    """Ruling 4: quiescence alone never proves clear -- this is the POSITIVE
    check. Matches EXACTLY (post NUL-strip and edge-whitespace-strip), never
    by substring containment -- a substring check is unsound for this
    purpose: found via live measurement (P3 runbook, rotation-systematization
    findings) that the empty-composer prompt row ``"❯\\xa0"`` strips to the
    bare glyph ``"❯"``, but so does the PREFIX of a still-populated composer
    row like ``"❯\\xa0/clear"`` under substring containment (``"❯" in
    "❯\\xa0/clear"`` is True) -- exactly the negative-control screen state
    this whole helper exists to never act on. Exact-match after stripping
    correctly rejects that row (``"❯\\xa0/clear".strip() != "❯"``) while
    still accepting the genuinely empty one (``"❯\\xa0".strip() == "❯"``) --
    both measured directly against real captured screen lines, not assumed.
    ``cleared_signature`` is caller-supplied and P2-measured against a real
    disposable pane, never guessed here."""
    return any(clean_screen_text(line).strip() == cleared_signature for line in lines)


def composer_content_rows(lines: list[str], cleared_signature: str) -> list[str]:
    """Screen rows showing a POPULATED composer: cleaned+stripped text that
    starts with the empty-composer signature but carries content beyond it
    (e.g. signature ``"❯"`` matches the row ``"❯ stranded draft"`` and not
    the bare ``"❯"``). Captured BEFORE the preamble kill-line destroys the
    content, so a stranded remote-control draft — or a genuine unsent
    operator draft — is relayed via the envelope and fire log, never
    silently discarded (relay-not-disregard, 2026-08-28)."""
    rows: list[str] = []
    for line in lines:
        cleaned = clean_screen_text(line).strip()
        if cleaned.startswith(cleared_signature) and cleaned != cleared_signature:
            rows.append(cleaned)
    return rows


def last_nonempty_line(text: str) -> str:
    """The last non-blank, stripped line of ``text`` -- used by
    ``submit_only`` mode (fix loop #2 item 3) as the recognizable fragment
    to look for on screen before positively confirming the composer already
    holds the EXPECTED pickup content. NOT a full-text reconstruction --
    the TUI can render a large multi-line paste as a collapsed bracket
    (``"[+N lines]"``), not the raw text, so a distinctive fragment is what
    is actually checkable; this limitation is documented, not silently
    assumed away. Returns ``""`` for an all-blank/empty ``text``."""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


async def wait_for_settle(
    session: Any,
    *,
    cleared_signature: str,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    stable_samples_required: int = DEFAULT_STABLE_SAMPLES_REQUIRED,
    timeout_seconds: float = DEFAULT_SETTLE_TIMEOUT_SECONDS,
    now_fn: Any = time.monotonic,
    sleep_fn: Any = asyncio.sleep,
) -> SettleDiagnostics:
    """Poll-until-stable settle detection (ruling 4 design target).

    Polls ``session.async_get_screen_contents()`` until ``stable_samples_
    required`` CONSECUTIVE samples positively match ``cleared_signature``
    (:func:`is_cleared_state`) -- a single matching sample is not enough,
    since that could be a transient render mid-redraw. Any non-matching
    sample resets the consecutive counter to 0 (a genuine positive check,
    not a decaying quiescence guess). Raises :class:`SettleTimeoutError`
    (fail-closed, carrying its own :class:`SettleDiagnostics`) if the
    deadline passes first. ``now_fn``/``sleep_fn`` are injectable so the
    poll loop is smokeable without real wall-clock waits. Returns
    :class:`SettleDiagnostics` on success -- summary counters only (no
    per-sample log), per the live leg #1 finding this exists to close.
    Thin wrapper over :func:`_poll_until_match`, shared with
    :func:`wait_for_screen_stable`.
    """
    def _is_match(lines: list[str]) -> bool:
        return is_cleared_state(lines, cleared_signature)

    try:
        return await _poll_until_match(
            session, is_match=_is_match, what="settle",
            poll_interval_seconds=poll_interval_seconds,
            stable_samples_required=stable_samples_required,
            timeout_seconds=timeout_seconds, now_fn=now_fn, sleep_fn=sleep_fn,
        )
    except _PollTimeoutError as exc:
        raise SettleTimeoutError(str(exc), exc.diagnostics) from exc


async def wait_for_screen_stable(
    session: Any,
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    stable_samples_required: int = DEFAULT_STABLE_SAMPLES_REQUIRED,
    timeout_seconds: float = DEFAULT_SETTLE_TIMEOUT_SECONDS,
    now_fn: Any = time.monotonic,
    sleep_fn: Any = asyncio.sleep,
) -> SettleDiagnostics:
    """Poll until the screen stops changing -- fix loop #2 item 2 (live leg
    #2, findings §4): a CR sent while a large multi-line paste is still
    being processed/rendered gets absorbed as a literal newline instead of
    submitting (live-measured: a CR sent immediately after a 12KB payload
    never submitted across 6+ seconds; a CR sent only once the screen held
    IDENTICAL content across 3 consecutive samples submitted in under 1s).
    Unlike :func:`wait_for_settle`, this has no target signature -- it
    can't, since the pickup text is arbitrary caller content whose
    on-screen (possibly paste-collapsed) rendering isn't known in advance.
    "Match" here means "identical to the immediately preceding sample,"
    tracked via a closure since :func:`_poll_until_match`'s ``is_match``
    only sees the CURRENT sample. Raises :class:`ScreenStabilityTimeoutError`
    (fail-closed) if the screen never settles -- the caller must NOT send
    the submitting CR after this."""
    prev_lines: list[str] | None = None

    def _is_match(lines: list[str]) -> bool:
        nonlocal prev_lines
        matched = prev_lines is not None and lines == prev_lines
        prev_lines = lines
        return matched

    try:
        return await _poll_until_match(
            session, is_match=_is_match, what="screen stability",
            poll_interval_seconds=poll_interval_seconds,
            stable_samples_required=stable_samples_required,
            timeout_seconds=timeout_seconds, now_fn=now_fn, sleep_fn=sleep_fn,
        )
    except _PollTimeoutError as exc:
        raise ScreenStabilityTimeoutError(str(exc), exc.diagnostics) from exc


async def _live_role_rows(app: Any) -> tuple[list[RoleRow], dict[str, Any]]:
    """Scan every live iTerm2 session for its ``user.role`` tag (live read,
    never cached -- the tag does not replay to late-attaching clients).
    Returns the flat row list plus a session_id -> live Session object map
    so the caller can act on the resolved match without a second scan."""
    rows: list[RoleRow] = []
    session_by_id: dict[str, Any] = {}
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                role = await session.async_get_variable(USER_ROLE_VAR)
                session_id = str(session.session_id)
                rows.append(RoleRow(
                    role=str(role or ""),
                    session_id=session_id,
                    tab_id=_safe_int(tab.tab_id),
                    window_id=_safe_int(window.window_id),
                ))
                session_by_id[session_id] = session
    return rows, session_by_id


async def _send_pickup_and_confirm_submit(
    session: Any,
    pickup_text: str,
    *,
    cleared_signature: str,
    poll_interval_seconds: float,
    stable_samples_required: int,
    settle_timeout_seconds: float,
) -> SettleDiagnostics:
    """Send the pickup text, wait for the paste render to STABILIZE before
    sending the submitting CR (fix loop #2 item 2 -- a CR sent while the
    TUI is still processing a large multi-line paste gets absorbed as a
    literal newline instead of submitting; live-measured, not assumed),
    then positively confirm the CR actually submitted (item 1 -- closes
    the false green: the caller must never report ``completed`` on
    send-API success alone). Raises :class:`HelperStepError` for a
    send-primitive failure, :class:`ScreenStabilityTimeoutError` if the
    paste render never stabilizes, or :class:`SettleTimeoutError` if the
    post-CR submit is never confirmed -- :func:`run_rotation` catches the
    latter two into refused envelopes and lets ``HelperStepError``
    propagate to ``main``'s handler, same convention as every other step.
    """
    try:
        await session.async_send_text(pickup_text)
    except Exception as exc:  # noqa: BLE001
        raise HelperStepError(STEP_SEND_PICKUP_TEXT, str(exc)) from exc

    await wait_for_screen_stable(
        session,
        poll_interval_seconds=poll_interval_seconds,
        stable_samples_required=stable_samples_required,
        timeout_seconds=settle_timeout_seconds,
    )

    try:
        await session.async_send_text(ITERM2_SEAT_SUBMIT_CONVENTION.submit_value or "")
    except Exception as exc:  # noqa: BLE001
        raise HelperStepError(STEP_SEND_PICKUP_CR, str(exc)) from exc

    return await wait_for_settle(
        session,
        cleared_signature=cleared_signature,
        poll_interval_seconds=poll_interval_seconds,
        stable_samples_required=stable_samples_required,
        timeout_seconds=settle_timeout_seconds,
    )


async def _submit_only_flow(
    session: Any,
    pickup_text: str,
    match: PaneMatch,
    *,
    cleared_signature: str,
    poll_interval_seconds: float,
    stable_samples_required: int,
    settle_timeout_seconds: float,
) -> dict[str, Any]:
    """``submit_only`` recovery mode (fix loop #2 item 3): the pane is
    assumed STRANDED with the pickup already visibly present but
    unsubmitted (the live leg #2 failure mode) -- NEVER injects text.
    Positively confirms the composer already shows a recognizable fragment
    of the EXPECTED pickup content (:func:`last_nonempty_line`) before
    sending a bare CR; refuses immediately, sending nothing, if it doesn't.
    Submission is then confirmed the same way the primary path does."""
    expected_fragment = last_nonempty_line(pickup_text)
    contents = await session.async_get_screen_contents()
    lines = [contents.line(i).string for i in range(contents.number_of_lines)]
    if not any(expected_fragment in clean_screen_text(line) for line in lines):
        return {
            "status": "refused", "step": STEP_CONFIRM_COMPOSER_CONTENT,
            "code": CODE_COMPOSER_CONTENT_MISMATCH,
            "message": (
                f"composer does not show the expected pickup content "
                f"(looked for {expected_fragment!r}); refusing to send blind"
            ),
        }
    try:
        await session.async_send_text(ITERM2_SEAT_SUBMIT_CONVENTION.submit_value or "")
    except Exception as exc:  # noqa: BLE001
        raise HelperStepError(STEP_SEND_PICKUP_CR, str(exc)) from exc
    try:
        submit_diagnostics = await wait_for_settle(
            session,
            cleared_signature=cleared_signature,
            poll_interval_seconds=poll_interval_seconds,
            stable_samples_required=stable_samples_required,
            timeout_seconds=settle_timeout_seconds,
        )
    except SettleTimeoutError as exc:
        return {
            "status": "refused", "step": STEP_CONFIRM_SUBMIT, "code": CODE_SUBMIT_TIMEOUT,
            "message": str(exc), "submit_diagnostics": exc.diagnostics.to_dict(),
        }
    return {
        "status": "completed",
        "session_id": match.session_id, "tab_id": match.tab_id, "window_id": match.window_id,
        "submit_diagnostics": submit_diagnostics.to_dict(),
    }



async def _send_slash_command_leg(
    session: Any,
    command_text: str,
    *,
    preamble_space_step: str,
    preamble_kill_step: str,
    command_step: str,
    command_cr_step: str,
    menu_confirm_cr_step: str,
    send_gap_seconds: float,
) -> None:
    """Send the proven space, kill-line, slash-command, double-CR shape.

    Both ``/clear`` and the opt-in ``/model <name>`` command need the exact
    same preamble and menu-confirm discipline. Keeping their individual
    step names preserves the failure envelope's actionable precision while
    making divergence in the five-send sequence impossible.
    """
    command_cr = ITERM2_SEAT_SUBMIT_CONVENTION.submit_value or ""
    command_leg: tuple[tuple[str, str], ...] = (
        (preamble_space_step, " "),
        (preamble_kill_step, COMPOSER_KILL_LINE),
        (command_step, command_text),
        (command_cr_step, command_cr),
        (menu_confirm_cr_step, command_cr),
    )
    for step_name, payload in command_leg:
        try:
            await session.async_send_text(payload)
        except Exception as exc:  # noqa: BLE001 -- surfaced verbatim via HelperStepError
            raise HelperStepError(step_name, str(exc)) from exc
        if send_gap_seconds > 0:
            await asyncio.sleep(send_gap_seconds)


async def _send_clear_leg(
    session: Any, *, cleared_signature: str, send_gap_seconds: float,
) -> list[str]:
    """Capture-then-clear (2026-08-28), extracted from :func:`run_rotation`
    to keep that function under the cyclomatic-complexity gate — the
    sequence contract it documents is worth more unbroken than one inlined
    block. Returns the captured composer rows.

    The kill-line below destroys any composer content, and that content
    could be a stranded remote-control draft or a genuine unsent operator
    draft — so it is read off the screen FIRST and both returned for the
    envelope and printed immediately (the fire log keeps it even if a later
    step fails). The old flush-Enter preamble is GONE: measured 2026-08-28,
    it SUBMITS a stranded RC draft as a prompt into the old context. Space
    suppresses ghost-suggestion text and realizes composer state; C-u then
    empties all real content. The double CR after the clear command is the
    2026-08-27 injection joseki (the first CR can be consumed by the
    slash-command autocomplete menu as "accept selection"; the second
    performs the actual submit, a no-op when no menu opened — a single CR
    was measured leaving "/clear" accepted-but-unsubmitted).
    """
    try:
        contents = await session.async_get_screen_contents()
        lines = [contents.line(i).string for i in range(contents.number_of_lines)]
    except Exception as exc:  # noqa: BLE001 -- surfaced verbatim via HelperStepError
        raise HelperStepError(STEP_CAPTURE_COMPOSER, str(exc)) from exc
    composer_capture = composer_content_rows(lines, cleared_signature)
    if composer_capture:
        print(json.dumps({"composer_capture": composer_capture}), flush=True)
    await _send_slash_command_leg(
        session,
        render_driver_text("seat.clear"),
        preamble_space_step=STEP_SEND_CLEAR_PREAMBLE_SPACE,
        preamble_kill_step=STEP_SEND_CLEAR_PREAMBLE_KILL,
        command_step=STEP_SEND_CLEAR_TEXT,
        command_cr_step=STEP_SEND_CLEAR_CR,
        menu_confirm_cr_step=STEP_SEND_CLEAR_MENU_CONFIRM_CR,
        send_gap_seconds=send_gap_seconds,
    )
    return composer_capture


async def _send_model_leg(session: Any, model: str, *, send_gap_seconds: float) -> None:
    """Send the opt-in model command using the same fail-closed command leg."""
    await _send_slash_command_leg(
        session,
        render_driver_text("seat.model", model_name=model),
        preamble_space_step=STEP_SEND_MODEL_PREAMBLE_SPACE,
        preamble_kill_step=STEP_SEND_MODEL_PREAMBLE_KILL,
        command_step=STEP_SEND_MODEL_TEXT,
        command_cr_step=STEP_SEND_MODEL_CR,
        menu_confirm_cr_step=STEP_SEND_MODEL_MENU_CONFIRM_CR,
        send_gap_seconds=send_gap_seconds,
    )


async def _connect_app() -> Any:
    """Open the iTerm2 connection and return the app, or fail loudly at ``connect``.

    Extracted from :func:`run_rotation` so the bindings-absent guard does not push
    that function past the cyclomatic-complexity gate -- the sequence contract it
    documents is worth more unbroken than one inlined branch.
    """
    if _iterm is None:
        raise HelperStepError("connect", f"{ITERM2_UNAVAILABLE_MESSAGE} ({ITERM2_IMPORT_ERROR})")
    connection = await _iterm.Connection.async_create()
    app = await _iterm.async_get_app(connection)
    if app is None:
        raise HelperStepError("connect", "iTerm2 Python API returned no app object")
    return app


async def run_rotation(
    role_tag: str,
    pickup_text: str,
    *,
    cleared_signature: str,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    stable_samples_required: int = DEFAULT_STABLE_SAMPLES_REQUIRED,
    settle_timeout_seconds: float = DEFAULT_SETTLE_TIMEOUT_SECONDS,
    send_gap_seconds: float = DEFAULT_SEND_GAP_SECONDS,
    model: str | None = None,
    inject_only: bool = False,
    submit_only: bool = False,
) -> dict[str, Any]:
    """The full sequence: resolve -> capture composer content (relayed via
    envelope + immediate print, never silently killed) -> send space ->
    send kill-line -> send /clear -> send CR -> send menu-confirm CR
    (preamble per the 2026-08-28 ghost-text matrix, double CR per the
    2026-08-27 injection joseki, ``send_gap_seconds`` between the clear
    leg's sends) -> settle-wait -> optionally send the same five-step
    ``/model <name>`` leg -> settle-wait again -> send pickup ->
    wait-for-paste-stable -> send CR -> confirm-submit.
    Every step's own failure aborts in place and returns/raises a report
    naming exactly which step failed -- see module docstring. Returns a
    ``{"status": "completed", ...}`` envelope on success, or
    ``{"status": "refused", "step": ..., "code": ..., ...}`` on a
    fail-closed refusal. Raises :class:`HelperStepError` for a
    send-primitive failure (iTerm2 API exception mid-sequence) -- the
    caller (``main``) turns that into the same refused-envelope shape for
    the CLI's exit path.

    ``inject_only`` (the sanctioned recovery path after a settle-timeout
    refusal, added after live leg #1: the pane was ALREADY cleared -- the
    original ``/clear`` had submitted -- only the settle DETECTION was too
    impatient, so re-sending ``/clear`` into an already-clear pane is
    unnecessary): skips the ``/clear``-send steps entirely, but the
    settle-wait step is NEVER skipped -- this still positively confirms the
    cleared-state signature before injecting, never assuming a prior
    refusal means "actually fine now." A caller must always re-verify, not
    just retry blind.

    ``submit_only`` (the sanctioned recovery path for the STRANDED-composer
    state added after live leg #2: the pickup text was already injected
    but its CR never submitted) -- see :func:`_submit_only_flow`. Mutually
    exclusive with ``inject_only`` in practice (the CLI enforces this via
    an argparse mutually-exclusive group); if a caller passes both,
    ``submit_only`` wins (checked first) since it is the narrower, more
    conservative mode (it sends nothing at all on a mismatch).
    """
    app = await _connect_app()
    rows, session_by_id = await _live_role_rows(app)
    try:
        match = resolve_single_pane(rows, role_tag)
    except PaneResolutionError as exc:
        return {
            "status": "refused", "step": STEP_RESOLVE_PANE, "code": exc.code,
            "message": exc.message, "candidates": exc.candidates,
        }
    session = session_by_id[match.session_id]

    if submit_only:
        return await _submit_only_flow(
            session, pickup_text, match,
            cleared_signature=cleared_signature,
            poll_interval_seconds=poll_interval_seconds,
            stable_samples_required=stable_samples_required,
            settle_timeout_seconds=settle_timeout_seconds,
        )

    composer_capture: list[str] = []
    if not inject_only:
        composer_capture = await _send_clear_leg(
            session, cleared_signature=cleared_signature, send_gap_seconds=send_gap_seconds,
        )

    try:
        settle_diagnostics = await wait_for_settle(
            session,
            cleared_signature=cleared_signature,
            poll_interval_seconds=poll_interval_seconds,
            stable_samples_required=stable_samples_required,
            timeout_seconds=settle_timeout_seconds,
        )
    except SettleTimeoutError as exc:
        return {
            "status": "refused", "step": STEP_SETTLE_WAIT, "code": CODE_SETTLE_TIMEOUT,
            "message": str(exc),
            "settle_diagnostics": exc.diagnostics.to_dict(),
            "composer_capture": composer_capture,
        }

    model_settle_diagnostics: SettleDiagnostics | None = None
    if model is not None:
        await _send_model_leg(session, model, send_gap_seconds=send_gap_seconds)
        try:
            model_settle_diagnostics = await wait_for_settle(
                session,
                cleared_signature=cleared_signature,
                poll_interval_seconds=poll_interval_seconds,
                stable_samples_required=stable_samples_required,
                timeout_seconds=settle_timeout_seconds,
            )
        except SettleTimeoutError as exc:
            return {
                "status": "refused", "step": STEP_MODEL_SETTLE_WAIT, "code": CODE_SETTLE_TIMEOUT,
                "message": str(exc),
                "settle_diagnostics": settle_diagnostics.to_dict(),
                "model_settle_diagnostics": exc.diagnostics.to_dict(),
                "composer_capture": composer_capture,
            }

    try:
        submit_diagnostics = await _send_pickup_and_confirm_submit(
            session, pickup_text,
            cleared_signature=cleared_signature,
            poll_interval_seconds=poll_interval_seconds,
            stable_samples_required=stable_samples_required,
            settle_timeout_seconds=settle_timeout_seconds,
        )
    except ScreenStabilityTimeoutError as exc:
        return {
            "status": "refused", "step": STEP_PASTE_STABLE_WAIT, "code": CODE_PASTE_STABLE_TIMEOUT,
            "message": str(exc), "paste_stable_diagnostics": exc.diagnostics.to_dict(),
            "composer_capture": composer_capture,
        }
    except SettleTimeoutError as exc:
        return {
            "status": "refused", "step": STEP_CONFIRM_SUBMIT, "code": CODE_SUBMIT_TIMEOUT,
            "message": str(exc), "submit_diagnostics": exc.diagnostics.to_dict(),
            "composer_capture": composer_capture,
        }

    result = {
        "status": "completed",
        "session_id": match.session_id,
        "tab_id": match.tab_id,
        "window_id": match.window_id,
        "settle_diagnostics": settle_diagnostics.to_dict(),
        "submit_diagnostics": submit_diagnostics.to_dict(),
        "composer_capture": composer_capture,
    }
    if model_settle_diagnostics is not None:
        result["model_settle_diagnostics"] = model_settle_diagnostics.to_dict()
    return result


def _safe_int(value: Any) -> int:
    """Same coercion convention as iterm_python_api.py's ``_safe_int`` --
    iTerm2 ids come back as int or str depending on API version; the 0
    sentinel makes a coercion failure visible without crashing the scan."""
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detached iTerm2 seat self-rotation helper (send /clear, "
        "wait for settle, inject the pickup first turn).",
    )
    parser.add_argument(
        "--role-tag", required=True,
        help="user.role value to resolve the target pane by (live read, never cached).",
    )
    parser.add_argument(
        "--pickup-prompt-file", required=True, type=Path,
        help="Path to the first-turn text to inject post-clear. The helper only "
        "reads this file; it never authors pickup content.",
    )
    parser.add_argument(
        "--cleared-signature", required=True,
        help="EXACT string a screen line must equal (post NUL-strip and "
        "edge-whitespace-strip) to positively confirm the cleared, empty-composer "
        "state (ruling 4: quiescence alone never proves clear -- and a substring "
        "match is unsound here, since an empty composer's prompt glyph is also a "
        "PREFIX of a still-populated one). P2-measured against a real disposable "
        "pane, not guessed -- e.g. the bare prompt glyph '❯'.",
    )
    parser.add_argument(
        "--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--stable-samples-required", type=int, default=DEFAULT_STABLE_SAMPLES_REQUIRED,
    )
    parser.add_argument(
        "--settle-timeout-seconds", type=float, default=DEFAULT_SETTLE_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--send-gap-seconds", type=float, default=DEFAULT_SEND_GAP_SECONDS,
        help="Gap between the clear leg's individual sends (space, kill-line, /clear, CR, "
        "menu-confirm CR) so no send lands mid-render of the prior one.",
    )
    parser.add_argument(
        "--model",
        help="Optional Claude model name to select after /clear has positively settled and "
        "before injecting the pickup prompt. Omit to preserve the existing rotation exactly.",
    )
    recovery_group = parser.add_mutually_exclusive_group()
    recovery_group.add_argument(
        "--inject-only", action="store_true",
        help="Skip the /clear send entirely and go straight to settle-wait then inject "
        "the pickup prompt. The sanctioned recovery path after a settle-timeout refusal "
        "when the pane is ALREADY cleared (a prior /clear submitted; only the settle "
        "DETECTION was too impatient) -- never re-sends /clear into an already-clear pane. "
        "Still performs the full settle-wait positive confirmation before injecting; never "
        "assumes a prior refusal means the pane is fine now.",
    )
    recovery_group.add_argument(
        "--submit-only", action="store_true",
        help="Never send /clear or the pickup text -- only positively confirm the composer "
        "already shows the EXPECTED pickup content (its last non-blank line), then send a "
        "bare CR and confirm submission. The sanctioned recovery path for the STRANDED-"
        "composer state (pickup injected, its CR never submitted). Refuses, sending "
        "nothing at all, if the composer doesn't show the expected content -- never sends "
        "text in this mode.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    pickup_text = args.pickup_prompt_file.read_text(encoding="utf-8")
    try:
        result = asyncio.run(run_rotation(
            args.role_tag,
            pickup_text,
            cleared_signature=args.cleared_signature,
            poll_interval_seconds=args.poll_interval_seconds,
            stable_samples_required=args.stable_samples_required,
            settle_timeout_seconds=args.settle_timeout_seconds,
            send_gap_seconds=args.send_gap_seconds,
            model=args.model,
            inject_only=args.inject_only,
            submit_only=args.submit_only,
        ))
    except HelperStepError as exc:
        print(json.dumps({
            "status": "error", "step": exc.step, "message": exc.message,
        }))
        return 1
    print(json.dumps(result))
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
