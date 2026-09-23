"""Fleet session-management Phase B, D2 (§5) — the ``tmux`` HostDriver:
the preferred-where-declared substrate, hardening the R1 spike
(``workbench/2026-08-03_r1_tmux_single_substrate_spike/FINDINGS.md``, MEASURED
GREEN) into production code. Spawns a real, detached tmux session running the
INTERACTIVE ``claude`` CLI (not the ``headless`` driver's stream-json pipe —
a tmux pane is a terminal, so the driver channel injects literal keystrokes,
exactly what a human attaching via ``tmux -CC`` would type). iTerm2 ``-CC``
attach is presentation ONLY (§8 item 2): nothing on this module's spawn/
lifecycle path touches the iTerm2 Python API.

ADAPTER REQUIREMENTS THE SPIKE ESTABLISHES (FINDINGS.md, baked in here):
1. ``allow-passthrough on`` — set globally on the tmux SERVER right after the
   first session this driver creates (a fresh server defaults it off on
   tmux>=3.3; there is no way to read a global option before a server
   exists, so this driver SETS it rather than only checking it).
2. Tag emission must be tmux-aware: the spawned pane emits the DCS-wrapped
   OSC 1337 role tag via ``emit_role_tag.sh`` (the reference impl this
   module ships alongside, per scope item 2) — the raw/unwrapped form is
   confirmed swallowed inside tmux.
3. The role tag is NOT the source of truth — L0 (the ledger) owns identity;
   the terminal var is presentation only. Re-emit-on-attach automation (a
   tmux hook re-invoking the emit on ``client-attached``) is NOT implemented
   in this slice — named as a follow-up in the D2 land report, not silently
   waved off (FINDINGS.md already flags multi-client/re-emit as "not
   measured", exactly the ripeness gap this lane's smokes/land-report
   record, per dispatch-brief scope item 5).
4. Presentation cleanup is this driver's job on the TMUX side: ``terminate``
   always runs ``kill-session`` so no native tmux session lingers. Cleaning
   up a DEAD iTerm2 window left over from a killed tmux session is iTerm2
   Python-API territory — out of scope per dispatch-brief scope item 4
   ("documented, not coded").

VERSION GATE: ``allow-passthrough`` and ``new-session -e`` (per-session
environment injection, used for identity wiring) both require tmux>=3.2/3.3;
:meth:`TmuxHostDriver.verify_config` refuses anything older by name rather
than failing opaquely mid-spawn.

IDENTITY WIRING mirrors ``headless_adapter.py`` exactly (same
``AGENT_INSTANCE_ID``/``AGENT_SESSION_ID`` contract ``backfill_registration``
depends on) — injected via ``tmux new-session -e KEY=VAL`` instead of a
``subprocess.Popen`` env mapping. ``AGENT_SESSION_ID`` is minted exactly ONCE,
here, same as the headless driver's own module docstring warns (two
evaluations of an identity expression is two identities).

SWAP-DURABLE BY CONSTRUCTION (D2 live-acceptance evidence, 2026-08-04
13:07-08Z): a tmux-hosted worker's pane belongs to the independent tmux
daemon, not to this plugin process — nothing on the platform's stop/swap
path touches it, so a tmux-hosted worker survives a blue-green deploy swap
that the headless driver's process-child workers do NOT (``headless_adapter.
py``'s own module docstring names that contrasting property: ``shutdown()``
SIGTERM-then-SIGKILLs every tracked headless worker on platform stop,
unconditionally, including mid-swap).

PROCESS TARGETING: the pane's command is ``sh -c 'exec <claude argv>'`` so the
pane's own pid (``#{pane_pid}``) IS the ``claude`` process, not an
intermediate shell — and, as a fresh tmux pane, that pid is also a process
group leader. :meth:`terminate` signals the WHOLE group (``os.killpg`` via
``_sigterm_then_kill_process_group``, falling back to
``headless_adapter``'s single-pid ``_sigterm_then_kill``/``_pid_alive``
helpers only if ``os.getpgid`` itself fails), not just the leader pid — a
child the claude process backgrounds (e.g. a watch-transport worker's own
``solet-bridge watch --role X &``, the standard rename-skill onboarding path)
shares the pane's process group and would otherwise survive the leader's
death as a launchd orphan, still holding the worker's role in the peer
registry (measured live, 2026-08-09/10 restart_session run — see
``workbench/2026-08-09_choreography_live_verify_mverbs-impl.md`` §4).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .headless_adapter import (
    WorkerHookResolutionError,
    _announce_managed_policy,
    _authority_system_prompt,
    _managed_policy_remedy,
    _permission_state_report,
    _pid_alive,
    _resolve_coordination_receipt_path,
    _resolve_heartbeat_marker_dir,
    _resolve_local_label,
    _resolve_session_mapping_spool_dir,
    _resolve_spawn_cwd,
    _resolve_worker_hook_paths,
    _sigterm_then_kill,
)
from .lane_worktrees import worktree_pythonpath
from .solet_cli import (
    WakeCliResolver,
    expose_worker_cli,
    watch_sidecar_argv,
)
from .submit_conventions import TMUX_CLAUDE_SUBMIT_CONVENTION

logger = logging.getLogger(__name__)

_CLAUDE_AGENT_ID = "claude_code"
_ENV_PERMISSION_MODE = "FLEET_HEADLESS_PERMISSION_MODE"
_MIN_TMUX_VERSION = (3, 3)
DEFAULT_TERMINATE_GRACE_SECONDS = 10.0
DEFAULT_PANE_WIDTH = 200
DEFAULT_PANE_HEIGHT = 50
_SESSION_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

# PTY-confirm fix (D3 slice 0a): a tmux-hosted claude launched with
# --dangerously-load-development-channels sits at a real interactive
# confirmation prompt with no CLI bypass flag -- D2 live-acceptance
# evidence (2026-08-04 13:0xZ) confirmed a driver-spawned worker would hang
# here forever, one send-keys Enter clears it. Piped-stdin/headless spawns
# never hit this (the prompt is PTY-only), so the loop below is gated on the
# flag's actual presence in the built claude argv, never assumed.
_DEV_CHANNELS_FLAG = "--dangerously-load-development-channels"
_DEV_CHANNELS_PROMPT_MARKERS = ("WARNING: Loading development channels", "Enter to confirm")
DEFAULT_CONFIRM_TIMEOUT_SECONDS = 20.0
DEFAULT_CONFIRM_POLL_INTERVAL_SECONDS = 0.5


def _needs_dev_channels_confirmation(claude_cmd: list[str]) -> bool:
    """Gate for the bounded PTY-confirm expect loop in :meth:`TmuxHostDriver.
    spawn` -- only a claude argv carrying ``--dangerously-load-development-
    channels`` ever shows the interactive confirmation prompt; an argv
    without the flag must never enter the loop at all, not even for a
    single ``capture-pane`` poll (the no-flag path stays exactly as fast and
    side-effect-free as before this fix)."""
    return _DEV_CHANNELS_FLAG in claude_cmd


def _pane_shows_dev_channels_prompt(pane_text: str) -> bool:
    return any(marker in pane_text for marker in _DEV_CHANNELS_PROMPT_MARKERS)


# Third-party-provider fix (§39.1/§40.1, reported and field-verified by a seed
# adopter): on a third-party inference provider, Claude Code IGNORES
# --dangerously-load-development-channels ("... ignored (server:<name>)" /
# "Channels are not available on third-party providers") and never raises the
# interactive confirmation. The expect loop above has exactly two branches --
# prompt-appeared -> confirm, no-prompt -> assume-hung -> KILL -- so a
# fully-booted worker was killed after DEFAULT_CONFIRM_TIMEOUT_SECONDS on every
# such spawn, making the swap-durable tmux fleet unspawnable on exactly the
# provider configuration an enterprise adopter without Anthropic-API access is
# forced onto.
#
# The fix is to omit the flag when it is already inert, so no prompt is ever
# expected: with the flag absent, _needs_dev_channels_confirmation returns False
# and the loop is skipped entirely. ONE mechanism, not two -- deliberately NOT
# also adding a pane-text success marker for the "ignored" lines, because a
# second guard would make this guard's smoke legs vacuous. The Anthropic /
# no-provider path is byte-for-byte unchanged (flag present, prompt-and-confirm
# exactly as before).
#
# EVIDENCE BOUND ON THIS TUPLE: CLAUDE_CODE_USE_BEDROCK is the ONLY marker with
# live evidence behind it (the adopter's verified spawn). Other third-party
# providers plausibly have their own switches, but we have no third-party
# endpoint in this checkout and no field report naming one, so adding a marker
# here on inference would be an unmarked guess -- a wrong name is silently inert
# and a right-name-wrong-semantics entry would omit the flag on an Anthropic
# spawn. Extend this tuple ONLY with a measured spawn or an adopter report.
_THIRD_PARTY_PROVIDER_ENV_MARKERS = ("CLAUDE_CODE_USE_BEDROCK",)


def _effective_spawn_env(spec: Mapping[str, object]) -> Mapping[str, str]:
    """The environment the spawned worker will ACTUALLY receive, which is what
    provider-detection must key on -- not this driver's own notion of a
    provider, and not the adopter's ``provider_env`` overlay alone (their Part
    36 §36.1 per-spawn provider selection does not exist in our tree yet; that
    is the ``provider-select-design`` lane).

    TODAY that is this process's own environment: the tmux driver injects only
    identity vars via ``new-session -e`` (see :func:`_env_pairs`), so any
    provider switch reaches the worker by inheritance. LATER, when the per-spawn
    overlay lands, it composes here and nowhere else -- the ``provider_env``
    read below is the seam, deliberately written now so the predicate needs no
    rework at that landing.

    ⚠ Two honest limits, neither silently papered over:
    * The overlay KEY NAME (``provider_env``) is taken from the adopter's
      described shape; nothing in our tree defines it yet, so it is inert until
      the provider-select lane lands and MUST be reconciled with whatever that
      lane actually threads through (a key that is never passed is a knob that
      does nothing).
    * When a tmux SERVER already exists, a new pane inherits that server's
      environment, which is the environment of whoever started the server --
      not necessarily this process's. In the deployed shape (launchd daemon
      creates the server on first spawn) the two are the same; under an
      operator-started pre-existing server they can diverge.
    """
    env: dict[str, str] = dict(os.environ)
    overlay = spec.get("provider_env")
    if overlay is None:
        return env
    if not isinstance(overlay, Mapping):
        raise TypeError(
            "spawn spec 'provider_env' must be a mapping of environment "
            f"variables, got {type(overlay).__name__} -- refusing to guess the "
            "effective spawn environment provider detection keys on.",
        )
    env.update({str(key): str(value) for key, value in overlay.items()})
    return env


def _provider_ignores_dev_channels(spawn_env: Mapping[str, str]) -> bool:
    """True when the effective spawn environment selects a third-party provider
    that ignores ``--dangerously-load-development-channels``. Adopter's shape,
    bound to OUR effective-env computation rather than to their overlay."""
    return any(
        str(spawn_env.get(marker, "")).strip() == "1"
        for marker in _THIRD_PARTY_PROVIDER_ENV_MARKERS
    )


def _emit_role_tag_path() -> Path:
    return Path(__file__).resolve().parent / "tmux_support" / "emit_role_tag.sh"


def _env_pairs(
    *, agent_instance_id: str, agent_session_id: str, label: str,
    solet_name: str, solet_bin: str, allowed_tools: object, transport: str,
    context_gauge_reporter_path: Path, cwd: Path,
) -> list[str]:
    """``-e KEY=VAL`` args for ``tmux new-session`` — split out of
    :meth:`TmuxHostDriver.spawn` to keep it under the radon cc threshold.
    Same identity-wiring contract as ``headless_adapter._spawn_env``.
    ``transport`` is caller-resolved (fleet-watch-transport-migration phase
    2 slice 1, 2026-08-06) -- never hardcoded here, the same declared,
    never-probed FLEET_TRANSPORT contract every consumer reads
    independently."""
    # Registration-loss fix (2026-08-14): AGENT_WAKE_CLI must be the wake
    # CLI's own binary, never `solet_name` (the solet INSTANCE name, e.g.
    # "mysolet") -- that conflation was the 2026-08-08 deaf-wake defect. The
    # 2026-08-08 fix used the bare command name "solet-bridge", which is only
    # correct when PATH can resolve it. A tmux pane inherits the tmux
    # SERVER's environment, not this process's, so a worker ran with a
    # minimal PATH carrying no venv/bin: MEASURED live in a spawned pane,
    # `heartbeat_report_alive.py` -> "[Errno 2] No such file or directory:
    # 'solet-bridge'", exit 0, silent. Absolute binary + PATH prepend closes both
    # halves (the same treatment codex_common landed on 2026-08-13).
    cli_env: dict[str, str] = {"PATH": os.environ.get("PATH", "")}
    expose_worker_cli(cli_env, solet_bin)
    pairs: list[str] = []
    for key, value in {
        "SOLET_NAME": solet_name,
        "AGENT_IDENTITY": _CLAUDE_AGENT_ID,
        "AGENT_INSTANCE_ID": agent_instance_id,
        "AGENT_SESSION_ID": agent_session_id,
        "AGENT_SESSION_LABEL": label,
        # GAU-01(a): preserve exactly one reporting writer for this spawned
        # worker even when a user-scope plugin-cache hook also fires.
        "AGENT_CONTEXT_GAUGE_REPORTER_PATH": str(context_gauge_reporter_path.resolve()),
        "AGENT_WAKE_CLI": cli_env["AGENT_WAKE_CLI"],
        "FLEET_TRANSPORT": transport,
        # `new-session -e` is an explicit allowlist boundary -- tmux drops
        # every variable not named here, which is exactly how the managed
        # Codex path lost its release PATH until 52edfb559 passed it
        # through. Same boundary, same fix.
        "PATH": cli_env["PATH"],
        # A lane worktree isolates FILES, not IMPORTS (2026-09-07,
        # iss_2236d297): a pane opened in the lane tree with no matching
        # import path still resolves `agent_messaging_plugin` and every
        # other repo module out of the SHARED checkout -- a quieter version
        # of exactly the contamination the worktree exists to prevent. The
        # Codex drivers already anchor this (codex_tmux._spawn_env ->
        # worktree_pythonpath); this is the same helper, not a second
        # implementation. `cwd` is the spawn's RESOLVED root, so on a spawn
        # with no worktree_path this reduces to the driver's own default
        # cwd and the value is what a shared-root spawn already had.
        "PYTHONPATH": worktree_pythonpath(cwd, os.environ.get("PYTHONPATH", "")),
    }.items():
        pairs += ["-e", f"{key}={value}"]
    git_controller_name = os.environ.get("GIT_CONTROLLER_NAME", "").strip()
    if git_controller_name:
        pairs += ["-e", f"GIT_CONTROLLER_NAME={git_controller_name}"]
    if isinstance(allowed_tools, (list, tuple)) and allowed_tools:
        pairs += ["-e", f"FLEET_HEADLESS_TOOL_ALLOWLIST={','.join(str(t) for t in allowed_tools)}"]
    # T1 usage-capture lane (ruling 2026-08-05, Q1(a)): same declared-not-
    # derived contract as headless_adapter._spawn_env -- omitted entirely
    # when APP_HOME is unset.
    spool_dir = _resolve_session_mapping_spool_dir()
    if spool_dir is not None:
        pairs += ["-e", f"ANANTA_SESSION_MAPPING_SPOOL_DIR={spool_dir}"]
    # T2 heartbeat lane (seat's redesign ruling 2026-08-05): same
    # declared-not-derived, conditional-export contract.
    heartbeat_dir = _resolve_heartbeat_marker_dir()
    if heartbeat_dir is not None:
        pairs += ["-e", f"AGENT_HEARTBEAT_MARKER_DIR={heartbeat_dir}"]
    receipt_path = _resolve_coordination_receipt_path()
    if receipt_path is not None:
        pairs += ["-e", f"AGENT_COORDINATION_RECEIPT_PATH={receipt_path}"]
    return pairs


def _pane_command(
    claude_cmd: list[str], *, label: str, solet_bin: str, transport: str,
) -> str:
    """The tag emit MUST run and complete BEFORE ``exec`` replaces this
    shell with the claude process — once exec'd, the pane has no shell left
    to receive a follow-up command; anything sent after that point is raw
    input to whatever is running (a first version of :meth:`TmuxHostDriver.
    spawn` had exactly this bug: a post-spawn ``send-keys`` landed as
    garbage input to the just-exec'd process instead of running as a
    command).

    REGISTRATION SIDECAR (2026-08-14, ratified — mirrors
    ``codex_tmux._pane_command`` exactly): on the ``watch`` transport this
    also backgrounds ``solet-bridge watch``, which is what actually REGISTERS the
    worker's presence (``local_cli.cli.watch``: "Hold this session's
    REGISTERED PRESENCE"). Before this, no claude_code spawn path armed it —
    registration depended entirely on the worker model volunteering to run
    the ``/rename`` skill, which the fallback first turn explicitly tells it
    not to do ("take no other action"). Measured consequence, twice on
    2026-08-13/14: a multi-hour productive worker that never appeared in
    ``peer_list``, unreachable by ``peer_send``, contributing zero liveness.

    Branched on the CALLER-RESOLVED ``transport``, never probed — the same
    declared-FLEET_TRANSPORT contract every other consumer reads.
    """
    emit_script = _emit_role_tag_path()
    parts = [
        f"sh {shlex.quote(str(emit_script))} {shlex.quote(label)}; "
        if emit_script.exists() else "",
    ]
    if transport == "watch" and solet_bin:
        # spool=True, deliberately UNLIKE codex's --no-spool: wake_waiter.py
        # is a real async Stop hook on this runtime and the spool tee is
        # exactly what it consumes. Arming without it would re-deafen the
        # wake this sidecar exists to enable.
        watch_cmd = shlex.join(
            watch_sidecar_argv(solet_bin, agent_id=_CLAUDE_AGENT_ID, spool=True),
        )
        # $$ is shell-expanded BEFORE exec; the exec'd claude process retains
        # that same shell pid, so the sidecar gets a true parent-liveness
        # target without minting a second identity (it inherits
        # AGENT_SESSION_ID/AGENT_INSTANCE_ID from `new-session -e`, so the
        # watcher claims under the id it inherits, as the identity rules
        # require -- exactly one mint, in spawn()).
        parts.append(f"{watch_cmd} --exit-with-parent $$ >/dev/null 2>&1 & ")
    parts.append(f"exec {shlex.join(claude_cmd)}")
    return "".join(parts)


def _sanitize_session_name(raw: str) -> str:
    """tmux session names may not contain ``:`` or ``.`` (both are
    target-syntax separators); collapse anything else non-shell-friendly
    too, so a lane_id with slashes/spaces never breaks ``-t`` targeting."""
    return _SESSION_NAME_UNSAFE.sub("-", raw) or "session"


def _parse_tmux_version(version_output: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", version_output)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _resolve_str(explicit: str | None, env_key: str, *, default: str = "") -> str:
    """``explicit if explicit is not None else os.environ.get(env_key) or
    default`` — factored out so :meth:`TmuxHostDriver.__init__` reads as a
    flat list of field assignments instead of N inline ternaries (each one
    is a branch radon's cyclomatic-complexity count charges to the
    function). ``is not None``, never ``or``: an explicit empty string is
    "explicitly blank", distinct from "not provided" (mirrors
    ``headless_adapter``'s own contract)."""
    if explicit is not None:
        return explicit
    return os.environ.get(env_key) or default


def _resolve_bin(explicit: str | None, which_name: str, default: str) -> str:
    """Same ``is not None`` contract as :func:`_resolve_str`, but the
    fallback is ``shutil.which`` instead of an env var (the ``tmux_bin``/
    ``claude_bin`` shape)."""
    if explicit is not None:
        return explicit
    return shutil.which(which_name) or default


def _configure_server_options(tmux_bin: str, session_name: str, run_fn: Any) -> None:
    """Set every server-global terminal capability a managed pane needs.

    A newly-created server has no opportunity to obtain these values from a
    user's shell. In particular, tmux's default extended-key handling strips
    the modifier from Return before Claude Code can distinguish a newline
    chord from submit. Apply the options after ``new-session`` has made the
    server exist, and refuse the spawn if either setting cannot be installed.
    """
    from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

    commands = (
        [tmux_bin, "set", "-g", "allow-passthrough", "on"],
        [tmux_bin, "set", "-s", "extended-keys", "on"],
        [tmux_bin, "set", "-as", "terminal-features", "xterm*:extkeys"],
    )
    try:
        for command in commands:
            outcome = run_fn(command, capture_output=True, text=True, timeout=5)
            if outcome.returncode != 0:
                raise HostCannotSpawnError(
                    "tmux server capability setup failed "
                    f"(exit {outcome.returncode}): {outcome.stderr.strip()}"
                )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _abort_unconfigured_session(tmux_bin, session_name, run_fn)
        raise HostCannotSpawnError(f"tmux server capability setup raised: {exc}") from exc
    except HostCannotSpawnError:
        _abort_unconfigured_session(tmux_bin, session_name, run_fn)
        raise


def _abort_unconfigured_session(tmux_bin: str, session_name: str, run_fn: Any) -> None:
    """Best-effort cleanup when a just-created pane lacks required keys."""

    try:
        run_fn(
            [tmux_bin, "kill-session", "-t", session_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("could not remove unconfigured tmux session %r", session_name)


DEFAULT_CLEARED_COMPOSER_SIGNATURE = "\u276f"
"""The rendered EMPTY-composer row of a Claude Code TUI, stripped.

INHERITED FROM A MEASUREMENT, NOT GUESSED HERE, and the provenance matters:
this exact value was measured against a real iTerm2-hosted Claude Code pane
during rotation-systematization P3 and is pinned in
``seat_rotation_helper_smoke.py`` -- the empty row renders as the prompt glyph
plus a trailing NBSP (``"\u276f\xa0"``), which strips to the bare glyph. The
TUI is the same program here; only the HOST terminal differs, which is why
this is carried over rather than re-derived.

★ A live tmux confirmation leg is OUTSTANDING and is deliberately not
claimed by the offline smoke that covers this file. That is also why the
signature is a constructor knob rather than a hardcode: if a tmux-hosted pane
ever renders its empty composer differently, the fix is a caller-supplied
signature, not an edit here (the same posture ``seat_rotation_helper`` takes,
where the signature is a required CLI argument for exactly this reason).
"""

DEFAULT_CLEAR_VERIFY_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_CLEAR_VERIFY_SAMPLES_REQUIRED = 3
DEFAULT_CLEAR_VERIFY_TIMEOUT_SECONDS = 120.0
"""Matched to ``seat_rotation_helper``'s settle window, and long on purpose.

That constant was RECALIBRATED after a live failure: the real post-``/clear``
settle floor is ~14-15s (SessionStart hooks, MCP bridge respawn, full TUI
redraw), which consumed a 15s budget entirely. The asymmetry is the whole
argument -- a long timeout costs nothing in the common case, because the poll
returns as soon as it sees its consecutive matches, while a short one reports
a real clear as unverified and sends a steward chasing a healthy session.
"""

DEFAULT_DRIVE_VERIFY_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_DRIVE_VERIFY_SAMPLES_REQUIRED = 3
DEFAULT_DRIVE_VERIFY_TIMEOUT_SECONDS = 120.0
"""Same settle-window reasoning as ``DEFAULT_CLEAR_VERIFY_TIMEOUT_SECONDS`` --
a long timeout costs nothing in the common case (the poll returns as soon as
either signal is confirmed) while a short one reports a real submit as
unverified."""

_PASTE_PLACEHOLDER_RE = re.compile(
    r"^\[Pasted text #\d+ \+(?P<lines>\d+) lines?\]$",
)
"""The composer row a Claude Code TUI renders for a BRACKETED paste it has
collapsed rather than echoed inline — measured live 2026-09-07 against a real
pane (``[Pasted text #1 +141 lines]`` for a 142-line payload,
``[Pasted text #1 +8 lines]`` for a 9-line one).

The declared count is the payload's own ``text.count("\n")`` in BOTH
measurements, which is what makes this a legitimate POPULATION anchor rather
than a wildcard: the number is derived from the driven text, so another
writer's paste sitting in the composer does not match ours unless it happens
to have the identical line count. It is a weaker anchor than the inline
prefix check (a coincidental line-count collision is possible where a prefix
collision is not), and it is used ONLY as an alternative to that check, never
as a replacement for it."""

_STRANDED_INPUT_SGR = "\x1b[97m"
"""Bright-white SGR — the measured signature of REAL stranded input sitting
in a composer (public issue #9 / GAU-09 pane read, backlog.md GAU-09)."""

_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
"""Matches an SGR escape sequence so it can be stripped for content
comparison, or walked to find the style in effect at a given position."""

DEFAULT_PASTE_STABLE_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_PASTE_STABLE_SAMPLES_REQUIRED = 3
DEFAULT_PASTE_STABLE_TIMEOUT_SECONDS = 10.0
"""Same conservative constants as the already-proven iTerm2 precedent
(``seat_rotation_helper.py``'s ``wait_for_screen_stable``, rotation-
systematization fix loop #2) — inherited as a starting default, not
independently recalibrated for tmux's own render timing beyond the live
measurement in ``workbench/2026-08-08_spawn_registration_gaps_findings_
rotation-impl.md`` (a real charter-sized payload's stranding was visible
within ~2s and stayed stable, never resolving on its own — consistent with
tmux's local paste-chunking completing fast once ``send-keys -l`` returns).
Flagged as an open item if further tuning is ever warranted; not treated as
precisely measured for tmux specifically the way the iTerm2 defaults were."""


def _is_collapsed_paste_of(composer_content: str, probe_text: str) -> bool:
    """Whether the composer row is the TUI's collapsed stand-in for
    exactly ``probe_text``.

    ★ THE PREFIX CHECK ABOVE CANNOT SEE A REAL DISPATCH. Measured live
    2026-09-07: a Claude Code TUI does not echo a multi-line paste into
    the composer at all — it collapses it to ``[Pasted text #1 +141
    lines]``. That row is not a prefix of the driven text, so the
    population anchor never fired, ``verify_driven`` polled its full
    120s deadline observing nothing, and ``drive_session`` raised
    ``drive_unverified`` for a drive that had in fact been delivered —
    skipping the un-park and the ``report_by`` re-arm on the way out.
    The failure is a deterministic function of payload size, which is
    why it outlived every earlier fix to this channel: every regression
    in the suite drives ``"claim role and report"``, 21 characters on
    one composer row — the one payload shape the prefix check handles.

    The line count is what keeps this a POSITIVE anchor. It is read off
    the payload (``text.count("\n")``, matching both live measurements
    exactly), so a paste some other writer left in the composer only
    matches when its line count collides with ours. That is a genuinely
    weaker guarantee than the prefix check — hence an alternative to it,
    never a replacement — and a single-line payload, which the TUI
    echoes inline rather than collapsing, is still held to the prefix
    check alone.
    """
    match = _PASTE_PLACEHOLDER_RE.match(composer_content)
    if match is None:
        return False
    declared_lines = int(match.group("lines"))
    return declared_lines > 0 and declared_lines == probe_text.count("\n")


class _TmuxSendKeysDriverChannel:
    """The ``DriverChannel`` (§5) for a tmux-hosted pane — injects literal
    keystrokes (``send-keys -l``) followed by a separate ``Enter`` key press,
    exactly what a human attaching to the same pane would type. ``-l``
    (literal) is load-bearing: without it, tmux interprets key-name-shaped
    text (e.g. a message that happens to read ``Enter`` or ``C-c``) as a key
    binding instead of literal characters.

    **Paste-stability wait (spawn/registration-gaps fix, 2026-08-08,
    live-measured — see the findings file section named above):** a large
    literal payload sent via ``send-keys -l`` can get chunked by tmux into
    multiple separate bracketed-paste events on the receiving Claude Code
    TUI (measured: a ~9.7KB payload split into 9 ``[Pasted text #N]``
    fragments), and an ``Enter`` sent immediately afterward — the OLD,
    unguarded behavior — lands while that multi-chunk paste is still being
    assembled/rendered and gets absorbed as a literal newline inside it
    instead of submitting. Same failure shape as the already-fixed iTerm2
    ``async_send_text`` race (rotation-systematization fix loop #2);
    independently reproduced here for tmux with the real production code,
    not merely inferred from the parallel. The fix mirrors that one exactly:
    poll the pane's own rendered content (``tmux capture-pane -p``) until it
    stops changing across N consecutive samples, THEN send the submitting
    ``Enter`` — never immediately after the text send.

    **Baseline-gated stability fix (driver-channel strand fix, 2026-08-14,
    hermetically reproduced — workbench/2026-08-14_driver_channel_strand_
    fix_report_lane_d.md):** the stability wait above had no BASELINE — it
    only compared each sample to the one immediately before it. When the TUI
    is slow to render the injected paste (busy event loop, mid-turn work),
    the first several samples can all show the PRE-SEND screen, which is
    just as internally-identical as a genuinely-stable POST-render screen —
    "stability" was declared against the OLD content, Enter fired into a
    composer that did not yet contain the text, and the paste rendered
    afterward with its submitting Enter already spent. The fix captures a
    baseline sample before the stability poll begins and only counts a
    sample toward ``stable_samples_required`` once it both matches the
    immediately-preceding sample AND differs from that baseline — the
    fail-open timeout (log loudly, still send Enter) is unchanged byte for
    byte, so a render that never visibly changes the pane (e.g. output
    scrolled away) still submits within the timeout rather than hanging."""

    def __init__(
        self, *, tmux_bin: str, session: str, run_fn: Any,
        poll_interval_seconds: float = DEFAULT_PASTE_STABLE_POLL_INTERVAL_SECONDS,
        stable_samples_required: int = DEFAULT_PASTE_STABLE_SAMPLES_REQUIRED,
        stable_timeout_seconds: float = DEFAULT_PASTE_STABLE_TIMEOUT_SECONDS,
        cleared_signature: str = DEFAULT_CLEARED_COMPOSER_SIGNATURE,
        clear_verify_poll_interval_seconds: float = (
            DEFAULT_CLEAR_VERIFY_POLL_INTERVAL_SECONDS
        ),
        clear_stable_samples_required: int = DEFAULT_CLEAR_VERIFY_SAMPLES_REQUIRED,
        clear_verify_timeout_seconds: float = DEFAULT_CLEAR_VERIFY_TIMEOUT_SECONDS,
        drive_verify_poll_interval_seconds: float = (
            DEFAULT_DRIVE_VERIFY_POLL_INTERVAL_SECONDS
        ),
        drive_verify_samples_required: int = DEFAULT_DRIVE_VERIFY_SAMPLES_REQUIRED,
        drive_verify_timeout_seconds: float = DEFAULT_DRIVE_VERIFY_TIMEOUT_SECONDS,
        sleep_fn: Any = time.sleep,
        now_fn: Any = time.monotonic,
    ) -> None:
        self._tmux_bin = tmux_bin
        self._session = session
        self._run_fn = run_fn
        self._poll_interval_seconds = poll_interval_seconds
        self._stable_samples_required = stable_samples_required
        self._stable_timeout_seconds = stable_timeout_seconds
        self._cleared_signature = cleared_signature
        self._clear_verify_poll_interval_seconds = clear_verify_poll_interval_seconds
        self._clear_stable_samples_required = clear_stable_samples_required
        self._clear_verify_timeout_seconds = clear_verify_timeout_seconds
        self._drive_verify_poll_interval_seconds = drive_verify_poll_interval_seconds
        self._drive_verify_samples_required = drive_verify_samples_required
        self._drive_verify_timeout_seconds = drive_verify_timeout_seconds
        self._sleep_fn = sleep_fn
        self._now_fn = now_fn

    convention = TMUX_CLAUDE_SUBMIT_CONVENTION

    def insert(self, text: str) -> None:
        """Put ``text`` in the pane's composer, raising if it did not land.

        The Codex twin (``codex_tmux._CodexTmuxDriverChannel.insert``) raises
        on a failed insert; this one used to CALL :meth:`_insert` and discard
        its boolean, so a caller using the declared insert/submit split
        (``session_hosts.DriverChannel``) got a silent no-op followed by a
        bare ``Enter`` into whatever the composer already held. Nothing in
        this build drives the split externally today — :meth:`send` is the
        only caller — so this is closing a latent protocol hazard, not a
        measured field failure, and :meth:`send` deliberately keeps using
        ``_insert``'s boolean so its established fire-and-forget contract is
        unchanged.
        """
        from .session_hosts import DriverChannelSendError  # noqa: PLC0415

        if not self._insert(text):
            raise DriverChannelSendError(
                f"tmux pane {self._session!r} did not accept the composer insert.",
            )

    def _insert(self, text: str) -> bool:
        """Load ``text`` into a private tmux buffer and BRACKETED-paste it.

        ★ THIS REPLACES ``send-keys -l``, WHICH SILENTLY TRUNCATED THE HEAD
        OF EVERY LARGE PAYLOAD. Measured live 2026-09-07 against a real
        Claude Code TUI pane: a 10.8KB / 142-line payload beginning
        ``HEADMARKERZZ1`` and ending ``TAILMARKERZZ2`` was sent with
        ``send-keys -l``; the receiving model, asked for the first and last
        25 characters of what it had actually received, answered with filler
        from the MIDDLE of the payload and the intact tail, and the head
        marker appeared zero times anywhere in the pane's full scrollback.
        The identical payload delivered through ``load-buffer`` +
        ``paste-buffer -p`` came back with the head marker intact. Same pane,
        same payload, only the mechanism differs. A dispatch brief driven
        through the old path arrived with its opening scope and authority
        sections missing, and nothing anywhere reported an error.

        Bracketed paste (``-p``) is also what makes the payload's own
        newlines literal. Under ``send-keys -l`` an embedded newline is a
        submission the pane may act on mid-payload; inside a bracketed paste
        it is just a character, so a multi-line brief becomes ONE turn rather
        than a race between its own lines and the submitting ``Enter``.

        The buffer name is per-call and ``paste-buffer -d`` deletes it on the
        way out, so two lanes driving different panes concurrently cannot
        read each other's payload out of a shared buffer name.

        Fire-and-forget by contract (``session_hosts.DriverChannel``), same
        as before: a pane that died between ``driver_channel()``'s liveness
        check and this write (TOCTOU) gets a swallowed, logged failure rather
        than an unmapped exception escaping through the verb layer, and
        :meth:`send` never attempts the ``Enter`` when this returns ``False``
        — nor does anything here touch ``capture-pane``
        (``tmux_driver_channel_smoke.py``'s own pre-existing invariant): a
        session we could not even write to gets zero further probing.
        """
        buffer_name = f"adadrv-{os.getpid()}-{time.monotonic_ns()}"
        try:
            loaded = self._run_fn(
                [self._tmux_bin, "load-buffer", "-b", buffer_name, "-"],
                input=text, capture_output=True, text=True, timeout=10,
            )
            if getattr(loaded, "returncode", 1) != 0:
                logger.warning(
                    "tmux driver channel send() failed — session %r rejected the "
                    "payload buffer load",
                    self._session,
                )
                return False
            self._run_fn(
                [
                    self._tmux_bin, "paste-buffer", "-b", buffer_name,
                    "-t", self._session, "-d", "-p",
                ],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning(
                "tmux driver channel send() failed — session %r is no longer reachable",
                self._session,
            )
            return False
        return True

    def submit(self) -> None:
        self._wait_for_paste_stable()
        try:
            self._run_fn(
                [self._tmux_bin, "send-keys", "-t", self._session, self.convention.submit_value],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning(
                "tmux driver channel send() failed sending Enter — session %r is no "
                "longer reachable",
                self._session,
            )

    def send(self, text: str) -> None:
        if self._insert(text):
            self.submit()

    def _capture_pane(self) -> str | None:
        """The pane's own rendered content, or ``None`` on any capture
        failure — treated as "not stable yet" by the caller, never as a
        crash (fire-and-forget contract, same as :meth:`send` itself)."""
        try:
            result = self._run_fn(
                [self._tmux_bin, "capture-pane", "-t", self._session, "-p"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        return str(getattr(result, "stdout", "") or "")

    def _is_cleared_screen(self, captured: str) -> bool:
        """POSITIVE cleared-state check by EXACT match after stripping,
        never by substring containment.

        ★ The containment version is UNSOUND here, and this is the single
        most important line in the whole GAU-09 fix. The empty composer row
        strips to the bare prompt glyph -- but so does the PREFIX of a
        still-populated row like ``"\u276f\xa0/clear"``, so ``glyph in row``
        is True for exactly the stranded-``/clear`` screen that the GAU-09
        measurement actually found on the lane's pane. A containment check
        would therefore certify the defect's own signature as a successful
        clear. Exact-match-after-strip rejects that row while still accepting
        the genuinely empty one. Both halves were measured directly against
        real captured screen lines by the iTerm2 precedent this mirrors
        (``seat_rotation_helper.is_cleared_state``), not reasoned about.
        """
        return any(
            line.replace("\x00", "").strip() == self._cleared_signature
            for line in captured.splitlines()
        )

    def verify_cleared(self) -> bool:
        """Poll the pane until ``clear_stable_samples_required`` CONSECUTIVE
        samples positively show a cleared composer. Satisfies
        ``session_hosts.ClearVerifyingDriverChannel``.

        Three properties, each load-bearing and each pinned by
        ``clear_session_effect_verification_smoke.py``:

        * POSITIVE, not quiescent (ruling 4, carried over from the iTerm2
          helper): a screen that has merely stopped changing proves nothing
          -- a pane stranded mid-``/clear`` is perfectly quiet. Only a match
          against the cleared signature counts, and any non-matching sample
          resets the streak to ZERO rather than decaying it, so a transient
          mid-redraw frame cannot accumulate into a false confirmation.

        * FAIL-CLOSED on the deadline -- deliberately the OPPOSITE of
          :meth:`_wait_for_paste_stable`, a few lines below, which fails
          OPEN. The two are not inconsistent: a paste whose render is never
          observed should still submit, because the alternative is never
          submitting at all; but a clear whose effect is never observed must
          never be REPORTED as one, because the alternative is the exact lie
          GAU-09 filed. Opposite costs, opposite defaults.

        * SENDS NOTHING. This method only ever reads. Re-firing ``/clear``
          on a failed verification would deposit a second copy of real text
          into a live input buffer, which is why the no-retry rule is an
          invariant of the verification step itself and not merely of its
          caller.
        """
        deadline = self._now_fn() + self._clear_verify_timeout_seconds
        stable_count = 0
        while True:
            captured = self._capture_pane()
            if captured is not None and self._is_cleared_screen(captured):
                stable_count += 1
                if stable_count >= self._clear_stable_samples_required:
                    return True
            else:
                stable_count = 0
            if self._now_fn() > deadline:
                logger.warning(
                    "tmux driver channel: pane %r never showed a cleared composer "
                    "within %.1fs -- reporting the clear as UNVERIFIED (fail-closed). "
                    "No key is re-sent: the /clear text is already in that pane's "
                    "input buffer and re-firing it would deposit a second copy.",
                    self._session, self._clear_verify_timeout_seconds,
                )
                return False
            self._sleep_fn(self._clear_verify_poll_interval_seconds)

    def _capture_pane_colored(self) -> str | None:
        """Same contract as :meth:`_capture_pane` but with ``-e``, which
        preserves the ANSI SGR escape codes tmux otherwise strips on plain
        ``capture-pane -p``. Without ``-e`` every pane reads as colourless
        and the stranded-input probe below has nothing to detect, ever —
        the standing trap this guards against."""
        try:
            result = self._run_fn(
                [self._tmux_bin, "capture-pane", "-e", "-t", self._session, "-p"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        return str(getattr(result, "stdout", "") or "")

    def _composer_content_matches(self, raw_line: str, probe_text: str) -> bool:
        """The POPULATION anchor: True if ``raw_line`` is the composer row
        AND its stripped content is a genuine PREFIX of the driven text,
        REGARDLESS of colour. This is proof the driven text actually
        reached the composer at all, independent of what happens to it
        next — used both to gate a confirmed submit (see
        :meth:`verify_driven`) and, paired with a colour check, to detect a
        stranded one.

        The glyph anchor (the same composer-row signature
        :meth:`_is_cleared_screen` uses) restricts the check to the
        COMPOSER row, not scrollback — a probe that scanned the whole pane
        would match unrelated content wherever it happens to look similar,
        the "pane detector matches the echo of its own probe" trap. The
        content anchor (a genuine prefix, not mere presence) guards the
        other direction: a composer row showing something else entirely is
        not evidence of THIS drive."""
        plain = raw_line.replace("\x00", "")
        stripped = _ANSI_SGR_RE.sub("", plain).strip()
        if not stripped.startswith(self._cleared_signature):
            return False
        composer_content = stripped[len(self._cleared_signature) :].strip()
        probe = probe_text.strip()
        if not (composer_content and probe):
            return False
        if probe.startswith(composer_content):
            return True
        return _is_collapsed_paste_of(composer_content, probe_text)


    def _composer_style_at_content(self, raw_line: str) -> str | None:
        """The SGR style in effect at the first content character after the
        composer glyph, or ``None`` if the row carries no style at that
        point. Walks past whitespace and escape codes to find the style
        that actually applies to the composer's TEXT, not merely the first
        code emitted anywhere on the line."""
        plain = raw_line.replace("\x00", "")
        glyph_pos = plain.find(self._cleared_signature)
        if glyph_pos < 0:
            return None
        after_glyph = plain[glyph_pos + len(self._cleared_signature) :]
        last_style: str | None = None
        i = 0
        n = len(after_glyph)
        while i < n:
            match = _ANSI_SGR_RE.match(after_glyph, i)
            if match:
                last_style = match.group(0)
                i = match.end()
                continue
            if after_glyph[i] in (" ", "\xa0", "\x00"):
                i += 1
                continue
            break
        return last_style

    def _is_stranded_drive_line(self, raw_line: str, probe_text: str) -> bool:
        """POSITIVE detection that ``probe_text`` is sitting in the composer
        as REAL stranded input, not a submitted-and-gone turn: the
        population anchor (:meth:`_composer_content_matches`) plus a colour
        check. SGR 97 (bright white) is the measured stranded-input
        signature (public issue #9 / GAU-09 pane read); anything else
        (SGR 2 dim ghost text, or no style at all) is not stranded."""
        if not self._composer_content_matches(raw_line, probe_text):
            return False
        return self._composer_style_at_content(raw_line) == _STRANDED_INPUT_SGR

    def _classify_drive_sample(self, captured: str, text: str) -> tuple[bool, bool, bool]:
        """Classify one polled sample as ``(stranded, populated, idle)`` —
        a pure reader, no state. Split out of :meth:`verify_driven` to keep
        that method's own branching under the CC gate; the state machine
        (the idle streak, the population gate) stays in the caller."""
        lines = captured.splitlines()
        stranded = any(self._is_stranded_drive_line(line, text) for line in lines)
        populated = any(self._composer_content_matches(line, text) for line in lines)
        idle = any(
            _ANSI_SGR_RE.sub("", line.replace("\x00", "")).strip() == self._cleared_signature
            for line in lines
        )
        return stranded, populated, idle

    def verify_driven(self, text: str) -> bool | None:
        """Poll the pane for evidence of what happened to a driven ``text``
        after :meth:`send` — POSITIVE evidence on EVERY side, two-phase.

        Composer-idle alone CANNOT serve as the success signal, and not
        only for the reason it looks like at first: it is the observation
        a genuine submit produces, but it is ALSO the observation of a
        ``send()`` that silently no-op'd (this channel's own ``send()``
        swallows a dead-pane failure into a logged warning and simply
        returns — a fire-and-forget contract by design) — in BOTH cases the
        composer reads "idle" for the entire poll, with no way to tell them
        apart from idle alone. The fix is a POPULATION anchor: this method
        does not accept "idle" as success until it has independently
        observed the driven text actually LAND in the composer at least
        once (any colour) — proof bytes genuinely reached the pane — and
        only THEN treats a subsequent return to idle as a confirmed submit.
        The stranded check is checked on every sample, independently and
        first, and is the load-bearing FAILURE signal; the population
        gate is the load-bearing SUCCESS-side signal. Idle observed before
        population is not evidence of anything and keeps polling.

        Returns ``True`` on a confirmed submit (text observed landing, then
        observed leaving without ever being seen stranded), ``False`` on a
        POSITIVE stranded-input observation, and ``None`` on a deadline
        passing with neither — could-not-determine, genuinely distinct from
        ``False`` (looked and found it stranded). Collapsing either pairing
        is the exact shape of the defect this closes.

        ``submitted=True`` here means the text was taken up as a turn by
        THIS pane, nothing more — it is not evidence the model acted on
        it, only that it left the composer. Sends nothing on any path,
        same no-retry invariant as :meth:`verify_cleared`."""
        deadline = self._now_fn() + self._drive_verify_timeout_seconds
        idle_streak = 0
        observed_populated = False
        while True:
            captured = self._capture_pane_colored()
            if captured is not None:
                stranded, populated, is_idle = self._classify_drive_sample(captured, text)
                if stranded:
                    return False
                observed_populated = observed_populated or populated
                if is_idle and observed_populated:
                    idle_streak += 1
                    if idle_streak >= self._drive_verify_samples_required:
                        return True
                elif not is_idle:
                    idle_streak = 0
                # is_idle and not observed_populated: idle-by-never-arrived --
                # NOT evidence of a submit, the exact trap this method
                # exists to close. Keep polling toward the deadline rather
                # than resetting or accepting it.
            if self._now_fn() > deadline:
                logger.warning(
                    "tmux driver channel: pane %r showed neither a confirmed "
                    "submit nor a positively stranded composer for the driven "
                    "text within %.1fs -- reporting the drive as UNVERIFIED "
                    "(could-not-determine). No key is re-sent.",
                    self._session, self._drive_verify_timeout_seconds,
                )
                return None
            self._sleep_fn(self._drive_verify_poll_interval_seconds)

    def _wait_for_paste_stable(self) -> None:
        """Poll until the pane's rendered content is IDENTICAL across
        ``stable_samples_required`` consecutive samples AND differs from the
        BASELINE — this function's own first sample, taken right after the
        text send succeeded — matching ``wait_for_screen_stable``'s
        "identical to the immediately preceding sample" semantics, but
        gated on an actual change from that first look first. Captured here
        rather than before the text send (tmux_driver_channel_smoke.py's
        own pre-existing invariants: zero ``capture-pane`` calls when the
        literal send itself fails, and every capture-pane read happening
        strictly between the text send and the submitting Enter) — the
        brief's fix note explicitly allows either timing ("before, or
        immediately after, the text send").

        Without the baseline gate, a slow-rendering TUI can hold that same
        first-look screen across every remaining sample in the window, and N
        identical PRE-render samples satisfy "stable" exactly as well as N
        identical POST-render ones — the strand-fix defect this gate closes
        (hermetically reproduced, workbench/2026-08-14_driver_channel_
        strand_fix_report_lane_d.md). There is still no target signature to
        check the CHANGED content against (the payload's post-render form
        isn't known in advance, same reasoning as the iTerm2 precedent).
        Fails OPEN on timeout (logs loudly, still sends the Enter) rather
        than closed: this channel's own established contract is
        fire-and-forget best-effort, never silently doing nothing — a
        render that never visibly differs from that first look (e.g. output
        scrolled away, or a render so fast the very first sample already
        shows it) still submits via this same timeout path, exactly as
        before this fix."""
        deadline = self._now_fn() + self._stable_timeout_seconds
        baseline: str | None = None
        have_baseline = False
        prev: str | None = None
        stable_count = 0
        while True:
            if self._now_fn() > deadline:
                logger.warning(
                    "tmux driver channel: pane %r did not stabilize within %.1fs before "
                    "the submitting Enter — sending it anyway (fail-open; the alternative "
                    "is never submitting at all).",
                    self._session, self._stable_timeout_seconds,
                )
                return
            current = self._capture_pane()
            if not have_baseline:
                baseline = current
                have_baseline = True
            if current is not None and current == prev and current != baseline:
                stable_count += 1
                if stable_count >= self._stable_samples_required:
                    return
            else:
                stable_count = 0
            prev = current
            self._sleep_fn(self._poll_interval_seconds)


def _sigterm_then_kill_process_group(pgid: int, grace_seconds: float) -> None:
    """SIGTERM then SIGKILL a whole process group — mirrors
    ``headless_adapter._sigterm_then_kill``'s shape but targets ``pgid`` via
    ``os.killpg`` instead of a single pid via ``os.kill``, so a pane's own
    backgrounded children die with it. No ``Popen`` handle to reap here (the
    tmux daemon owns the process, not this Python process), so liveness is
    always the raw ``kill -0`` probe."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        logger.error("SIGTERM denied for tmux pane process group pgid=%d", pgid)
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pgid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        logger.error("SIGKILL denied for tmux pane process group pgid=%d", pgid)


# 2026-08-17, found while working §45.1 (#17); a bounded scope addition,
# authorized separately from §45.1 and separable from it.
#
# The W4A managed-policy preflight shipped for #8 §43.1 was wired into
# ``HeadlessHostDriver.verify_config`` ONLY. This driver duplicated the
# claude-launch checks and omitted that one, so the refusal §43.1 asked for was
# never performed on the tmux host — the host ``spawn_session``'s own
# documentation tells adopters to PREFER for any worker expected to outlive a
# release, because it is the swap-durable one. #8's own diagnosis named
# ``tmux_adapter.py`` explicitly alongside ``headless_adapter.py``; the fix
# covered one of the two files the report named.
#
# A managed policy that strips non-plugin hooks strips them from THIS driver's
# ``--settings`` blob exactly as it does from the headless driver's, so there
# was never a reason for the two to differ: the omission was a gap, not a
# decision. The remedy TEXT is the headless one verbatim (imported, never
# restated) so the two hosts can never drift.
#
# This is a behaviour change to §43.1's REFUSAL, not to §45.1's warning.
def _claude_launch_remedies(
    *, claude_bin: str, solet_name: str, permission_mode: str | None,
    driver_permission_mode: str, transport: str | None, driver_transport: str,
    mcp_config_path: Path,
) -> list[str]:
    """The underlying-``claude``-launch checks, shared with the headless
    driver's own :meth:`verify_config` — a tmux-hosted pane still launches a
    real ``claude`` process.

    Module-level, taking the driver's resolved values as arguments rather than
    reading ``self``: this is a pure function over them, and
    :class:`TmuxHostDriver` sits close enough to the god-class LOC ceiling that
    a method this size crowds out the next legitimate addition. Same reason
    :func:`_tmux_hook_settings_json` is module-level. Behaviour is unchanged —
    every remedy string and every condition below is verbatim what the method
    it replaced produced.
    """
    remedies: list[str] = []
    if not (claude_bin and os.access(claude_bin, os.X_OK)):
        remedies.append(
            f"no executable 'claude' binary found (checked PATH via "
            f"shutil.which, then {claude_bin!r}) — install Claude "
            "Code or pass claude_bin explicitly.",
        )
    if not solet_name:
        remedies.append(
            "SOLET_NAME is not set — the spawned session cannot "
            "discover its bridge port without it.",
        )
    if not (permission_mode or driver_permission_mode):
        remedies.append(
            f"no permission mode configured (neither a per-spawn override nor "
            f"{_ENV_PERMISSION_MODE} is set) — an unattended tmux-hosted worker "
            "needs an explicit operator-ruled posture; this driver never "
            "defaults to bypass.",
        )
    resolved_transport = transport if transport is not None else (driver_transport or "watch")
    if resolved_transport == "mcp" and not mcp_config_path.exists():
        remedies.append(
            f"no MCP config found at {mcp_config_path} — required because "
            "the resolved transport is 'mcp' (a real MCP bridge config must "
            "exist and is passed verbatim to --mcp-config); 'watch' transport "
            "spawns with an inline empty MCP config and never reads this file, "
            "so switching FLEET_SESSION_HOST/transport to 'watch' (the charter "
            "default) also satisfies this remedy without creating the file.",
        )
    return remedies


def _managed_policy_remedies(
    managed_settings_paths: tuple[Path, ...] | None,
    *,
    degraded_hooks_acknowledged: bool,
) -> list[str]:
    """The hooks-stripping refusal as a splattable list (empty means clean)."""
    remedy = _managed_policy_remedy(
        managed_settings_paths,
        degraded_hooks_acknowledged=degraded_hooks_acknowledged,
    )
    return [] if remedy is None else [remedy]


def _tmux_capability_report() -> dict[str, object]:
    """Pure function over module constants, no ``self`` needed — module-level
    for the same god-class-ceiling reason :func:`_tmux_hook_settings_json` is.

    ``permission_denies_default`` reports the DEFAULT deny list: the per-spawn
    ``allow_askuserquestion`` escape hatch is not visible from here, so naming
    it as this spawn's list would be a claim this function cannot make.
    """
    return {
        "host": "tmux",
        "topology": "detached-session",
        "inspectable_via": ["tmux attach -t <host_ref>", "tmux -CC attach -t <host_ref>"],
        "attach_hint": (
            "detached tmux session; attach directly or via iTerm2 'tmux -CC attach "
            "-t <host_ref>' for native-window presentation (documented separately, "
            "not automated by this driver)"
        ),
        **_permission_state_report(
            _tmux_denied_tools(allow_askuserquestion=False),
            denies_key="permission_denies_default",
        ),
    }


def _tmux_denied_tools(*, allow_askuserquestion: bool) -> tuple[str, ...]:
    """The deny list this driver's ``--settings`` blob emits.

    A function rather than a constant because ``AskUserQuestion``'s presence is
    per-spawn, and a single source of truth because §45.1's inert-permissions
    warning names this list back to the operator: a warning that enumerates a
    different set of tools than the blob actually denies would be worse than
    no warning, since it would be believed.
    """
    deny = ["Agent", "Task"]
    if not allow_askuserquestion:
        deny.append("AskUserQuestion")
    return tuple(deny)


def _tmux_denied_tools_for(spec: Mapping[str, object]) -> tuple[str, ...]:
    """This spawn's deny list, resolved from its spec."""
    return _tmux_denied_tools(
        allow_askuserquestion=bool(spec.get("allow_askuserquestion")),
    )


def _tmux_hook_settings_json(
    *,
    allowlist_hook_path: Path,
    capture_hook_path: Path,
    heartbeat_hook_path: Path,
    rotation_due_hook_path: Path,
    wake_hook_path: Path,
    check_messages_hook_path: Path,
    step_zero_hook_path: Path,
    role_binding_hook_path: Path,
    allow_askuserquestion: bool,
) -> str:
    """The tmux driver's ``--settings`` JSON: the same hook wiring and tool
    deny rule as ``headless_adapter.py``'s ``_hook_settings_json`` (module-
    level here, not a method, to keep :class:`TmuxHostDriver` under the
    god-class LOC ceiling — pure function over resolved hook paths, no
    ``self`` needed).

    Agent/Task tool deny (capability-tier guardrail redesign, fleet-watch-
    transport-migration phase 2 slice 1+5, 2026-08-06): carried here, not
    just in this checkout's own project-scope ``.claude/settings.json``,
    because ``--setting-sources project`` resolves relative to whatever
    ``cwd`` the spawned worker actually runs in. AskUserQuestion default-
    deny (operator ruling 2026-08-14): this is the only Claude-runtime host
    driver where the tool's blocking picker can render (a real interactive
    CLI) — the headless driver never enumerates the tool at all (measured;
    see ``headless_adapter.py``'s own docstring), so it needs no equivalent
    here. ``allow_askuserquestion`` is the per-spawn escape hatch
    (``SpawnSessionRequest.allow_askuserquestion``, mirrors the seed
    launcher's ``SOLET_ALLOW_ASKUSERQUESTION=1``); ``False`` (the default)
    keeps the deny on."""
    settings = {
        "permissions": {
            "deny": list(_tmux_denied_tools(allow_askuserquestion=allow_askuserquestion)),
        },
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": f"python3 {allowlist_hook_path}"}]},
            ],
            "SessionStart": [
                {"hooks": [{"type": "command", "command": f"python3 {capture_hook_path}"}]},
                {
                    # Matches coordination-hooks@<solet>'s own hooks.json
                    # matcher for this event exactly (fidelity, not a
                    # new choice) -- check_messages_reminder.py also
                    # fires here (in addition to UserPromptSubmit
                    # below) matching the plugin's own dual wiring.
                    "matcher": "startup|resume|clear",
                    "hooks": [
                        {"type": "command", "command": f"python3 {check_messages_hook_path}"},
                        {"type": "command", "command": f"python3 {role_binding_hook_path}"},
                    ],
                },
            ],
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": f"python3 {step_zero_hook_path}"}]},
                {"hooks": [{"type": "command", "command": f"python3 {check_messages_hook_path}"}]},
            ],
            "PostToolUse": [
                {"hooks": [
                    {"type": "command", "command": f"python3 {heartbeat_hook_path}"},
                    {"type": "command", "command": f"python3 {rotation_due_hook_path}"},
                ]},
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"python3 {wake_hook_path}",
                            # Matches wake_waiter.js's own required
                            # shape exactly -- without this the Stop
                            # hook applies Claude Code's short default
                            # timeout instead of blocking indefinitely
                            # for a peer-message wake.
                            "asyncRewake": True,
                            "timeout": 86400,
                        },
                    ],
                },
            ],
        },
    }
    return json.dumps(settings)


class TmuxHostDriver:
    """The ``tmux`` driver (§5): preferred-where-declared substrate. One
    detached tmux session per :meth:`spawn` call, running the interactive
    ``claude`` CLI; driven via literal keystrokes over ``send-keys``."""

    def __init__(
        self,
        *,
        tmux_bin: str | None = None,
        claude_bin: str | None = None,
        solet_name: str | None = None,
        solet_bin: str | None = None,
        permission_mode: str | None = None,
        transport: str | None = None,
        mcp_config_path: Path | None = None,
        cwd: Path | None = None,
        python_executable: str | None = None,
        run_fn: Any = subprocess.run,
        grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
        pane_width: int = DEFAULT_PANE_WIDTH,
        pane_height: int = DEFAULT_PANE_HEIGHT,
        confirm_timeout_seconds: float = DEFAULT_CONFIRM_TIMEOUT_SECONDS,
        confirm_poll_interval_seconds: float = DEFAULT_CONFIRM_POLL_INTERVAL_SECONDS,
        sleep_fn: Any = time.sleep,
    ) -> None:
        from .headless_adapter import (
            _resolve_default_cwd,  # noqa: PLC0415 -- avoid module-load cycle risk
        )

        self._tmux_bin = _resolve_bin(tmux_bin, "tmux", "tmux")
        self._claude_bin = _resolve_bin(
            claude_bin, "claude", str(Path.home() / ".local" / "bin" / "claude"),
        )
        self._solet_name = _resolve_str(solet_name, "SOLET_NAME")
        # R11 (2026-08-17): resolve the wake CLI per read, never once here.
        # See the `_solet_bin` property and WakeCliResolver's own note.
        self._cli_resolver = WakeCliResolver(
            solet_bin, python_executable=python_executable,
        )
        self._permission_mode = _resolve_str(permission_mode, _ENV_PERMISSION_MODE)
        # fleet-watch-transport-migration phase 2 slice 1 (2026-08-06):
        # mirrors headless_adapter.HeadlessHostDriver's own floor exactly --
        # spec-level (spawn_session's policy resolution) always wins; never
        # a fail-closed gate (unlike permission_mode), so an unset floor
        # resolves to the charter's own default ("watch") in _spawn_command
        # rather than refusing to spawn.
        self._transport = transport if transport is not None else ""
        self._cwd = cwd if cwd is not None else _resolve_default_cwd()
        self._mcp_config_path = (
            mcp_config_path if mcp_config_path is not None else (self._cwd / ".mcp.json")
        )
        self._run_fn = run_fn
        self._grace_seconds = grace_seconds
        self._pane_width = pane_width
        self._pane_height = pane_height
        self._confirm_timeout_seconds = confirm_timeout_seconds
        self._confirm_poll_interval_seconds = confirm_poll_interval_seconds
        self._sleep_fn = sleep_fn

    @property
    def _solet_bin(self) -> str:
        """The wake CLI, resolved FRESH on every read (R11, 2026-08-17).

        This used to be resolved once in ``__init__`` and stored. The answer
        depends on the ``current`` symlink's target, which cutover moves under a
        long-lived process, so a single evaluation is a snapshot of filesystem
        state masquerading as a constant. Resolving during a cutover skew window
        correctly declines to rewrite; caching that decline pinned every
        subsequent spawn to a reapable versioned directory for the process's
        whole life. Full account in :func:`resolve_solet_bin`.

        KNOWN, BOUNDED RESIDUAL: this resolves per READ, not per spawn, and
        :meth:`spawn` reads it twice (env pairs, then the pane command). A
        cutover landing between those two reads yields one stable and one
        versioned path. Both name a valid executable at the instant they are
        read, so the worst case is a mixed-but-working spawn — against the
        pre-fix guarantee of permanent staleness. Resolving once per spawn and
        threading the value down is the exactly-correct granularity and is
        recorded as a follow-up, deliberately not folded into this landing.
        """
        return self._cli_resolver.resolve()

    def _tmux_binary_remedies(self) -> list[str]:
        """Split out of :meth:`verify_config` to keep it under the radon cc
        threshold — the tmux-specific half (binary presence + version)."""
        remedies: list[str] = []
        resolved_tmux = shutil.which(self._tmux_bin) or (
            self._tmux_bin if os.path.isabs(self._tmux_bin) else None
        )
        if not (resolved_tmux and os.access(resolved_tmux, os.X_OK)):
            remedies.append(
                f"no executable 'tmux' binary found (checked PATH, then "
                f"{self._tmux_bin!r}) — install tmux (e.g. 'brew install tmux') "
                "or pass tmux_bin explicitly.",
            )
            return remedies
        try:
            version_check = self._run_fn(
                [resolved_tmux, "-V"], capture_output=True, text=True, timeout=5,
            )
            version_stdout = version_check.stdout or ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            version_stdout = ""
            logger.warning("tmux -V raised while checking version: %s", exc)
        parsed = _parse_tmux_version(version_stdout)
        if parsed is None or parsed < _MIN_TMUX_VERSION:
            remedies.append(
                f"tmux version {version_stdout.strip()!r} is older than "
                f"{_MIN_TMUX_VERSION[0]}.{_MIN_TMUX_VERSION[1]} (or unparseable/unreachable) — "
                "'allow-passthrough' and per-session '-e' env injection both "
                "require it; upgrade tmux.",
            )
        return remedies

    def verify_config(
        self, *, permission_mode: str | None = None, transport: str | None = None,
        degraded_hooks_acknowledged: bool = False,
        managed_settings_paths: tuple[Path, ...] | None = None,
    ) -> list[str]:
        """Config-time, fail-closed remedies (§5) — empty means ready to
        spawn.

        ``degraded_hooks_acknowledged``/``managed_settings_paths`` carry the
        same meaning and the same fixture-injection purpose as their headless
        counterparts; :func:`_managed_policy_remedies`' module-level note above
        it records why this host grew them on 2026-08-17.

        ``transport`` is a per-spawn override, same shape and same reason as
        the one :meth:`HeadlessHostDriver.verify_config` grew for Dax Part 36
        §36.3 (ported here 2026-08-10, authorized scope addition — the tmux
        driver carried the identical unconditional check): :meth:
        `_spawn_command` only ever reads ``self._mcp_config_path`` when the
        resolved transport is ``"mcp"`` — a ``"watch"`` spawn passes an inline
        literal empty MCP config (``'{"mcpServers":{}}'``) and never touches
        the file. Requiring the file unconditionally refused every
        watch-transport tmux spawn on a born clone, which ships no
        ``.mcp.json`` at all, for a file that spawn was never going to read —
        and tmux is the swap-durable fleet host, so that refusal took out the
        durable substrate on exactly the clone that has no MCP config to
        begin with. Resolution mirrors :meth:`_resolve_transport` exactly
        (spec-level override, then this driver's constructor/env floor, then
        the charter default ``"watch"``) so a bare call sees the same posture
        :meth:`spawn` would actually take."""
        return [
            *self._tmux_binary_remedies(),
            *_claude_launch_remedies(
                claude_bin=self._claude_bin, solet_name=self._solet_name,
                permission_mode=permission_mode,
                driver_permission_mode=self._permission_mode,
                transport=transport, driver_transport=self._transport,
                mcp_config_path=self._mcp_config_path,
            ),
            *_managed_policy_remedies(
                managed_settings_paths,
                degraded_hooks_acknowledged=degraded_hooks_acknowledged,
            ),
        ]

    def capability_report(self) -> dict[str, object]:
        return _tmux_capability_report()

    def _spawn_command(
        self, spec: Mapping[str, object], *, transport: str, label: str,
        resolved_hooks: Mapping[str, Path] | None = None,
    ) -> list[str]:
        permission_mode = str(spec.get("permission_mode") or "") or self._permission_mode
        # R4 Package C (2026-08-10): resolved via the two-rung ladder
        # (origin checkout, then the plugin's shipped fallback copy) --
        # see headless_adapter._resolve_worker_hook_paths's own docstring.
        # Raises WorkerHookResolutionError, converted to
        # HostCannotSpawnError by the caller (spawn()), if any file
        # resolves at neither rung -- never silently emits settings
        # pointing at a missing path.
        resolved_hooks = resolved_hooks or _resolve_worker_hook_paths(self._cwd)
        allowlist_hook_path = resolved_hooks["headless_tool_allowlist_gate.py"]
        capture_hook_path = resolved_hooks["capture_session_mapping.py"]
        heartbeat_hook_path = resolved_hooks["heartbeat_report_alive.py"]
        rotation_due_hook_path = resolved_hooks["rotation_due_watch.py"]
        # Deaf-wake fix (2026-08-08): project-vendored Python ports of
        # coordination-hooks@<solet>'s four JS hooks, wired here (the adapter's
        # own generated --settings blob) rather than the shared project-
        # scope .claude/settings.json, specifically so this does not
        # double-fire the wake for the seat -- the seat already gets these
        # via the user-scope plugin, which --setting-sources project
        # excludes for a spawned worker in the first place (that exclusion
        # is the root cause this fix addresses; see
        # workbench/2026-08-08_deaf_wake_diagnosis_findings_rotation-impl.md).
        wake_hook_path = resolved_hooks["wake_waiter.py"]
        check_messages_hook_path = resolved_hooks["check_messages_reminder.py"]
        step_zero_hook_path = resolved_hooks["step_zero_reminder.py"]
        role_binding_hook_path = resolved_hooks["role_binding_reminder.py"]
        settings_json = _tmux_hook_settings_json(
            allowlist_hook_path=allowlist_hook_path,
            capture_hook_path=capture_hook_path,
            heartbeat_hook_path=heartbeat_hook_path,
            rotation_due_hook_path=rotation_due_hook_path,
            wake_hook_path=wake_hook_path,
            check_messages_hook_path=check_messages_hook_path,
            step_zero_hook_path=step_zero_hook_path,
            role_binding_hook_path=role_binding_hook_path,
            allow_askuserquestion=bool(spec.get("allow_askuserquestion")),
        )
        cmd = [
            self._claude_bin,
            # W6 (#13 §44.3, Z-Q4 ruling 2026-08-14): the tmux host had NEVER
            # been guard-nameable — the Git-Controller gate resolves its caller
            # from ~/.claude/sessions/<pid>.json's "name", a file only the
            # headless driver has ever populated (it alone passed --name), so
            # a tmux worker's name was always auto-derived and could never
            # match. That asymmetry is the latent defect under #13; naming the
            # tmux session was never going to be enough on its own.
            #
            # OBSERVED, not inferred (the flag's headless behaviour does not
            # establish its interactive behaviour, and tmux launches a real
            # interactive CLI): live argv of the operator's own interactive
            # session carries `--name <coordinator-role>` and its session file
            # reads {"kind":"interactive","name":"<coordinator-role>"} with NO
            # "nameSource" field, while every tmux worker without the flag reads
            # "nameSource":"derived" with an auto-name. --name populates the
            # guard's file in interactive mode.
            "--name", label,
            "--remote-control", label,
            "--permission-mode", permission_mode,
            "--setting-sources", "project",
            "--settings", settings_json,
        ]
        # fleet-watch-transport-migration phase 2 slice 1 (2026-08-06):
        # mirrors headless_adapter.py's own posture exactly -- "mcp" gets
        # the real bridge config, "watch" gets an EXPLICIT empty one
        # (WS-6-verified precedent), never omitted (omitting risks ambient
        # .mcp.json re-attachment). Dev-channel loading is orthogonal to
        # MCP-vs-watch -- a separate mechanism per the phase-2 scope ruling.
        if transport == "mcp":
            cmd += ["--mcp-config", str(self._mcp_config_path), "--strict-mcp-config"]
        else:
            cmd += ["--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"]
        # §39.1/§40.1: append the dev-channels flag ONLY where it does
        # something. On a third-party provider it is inert AND the confirm
        # loop it arms would kill the booted worker -- see
        # _provider_ignores_dev_channels.
        if not _provider_ignores_dev_channels(_effective_spawn_env(spec)):
            cmd += ["--dangerously-load-development-channels", f"server:{self._solet_name}"]
        model = str(spec.get("model") or "")
        if model:
            cmd += ["--model", model]
        effort = str(spec.get("effort") or "")
        if effort:
            cmd += ["--effort", effort]
        # T2 authority-template (seat's design ruling 2026-08-05): same
        # ON-by-default contract as the headless adapter.
        cmd += ["--append-system-prompt", _authority_system_prompt(spec)]
        return cmd

    def _resolve_transport(self, spec: Mapping[str, object]) -> str:
        """fleet-watch-transport-migration phase 2 slice 1 (2026-08-06):
        mirrors headless_adapter.HeadlessHostDriver._resolve_transport
        exactly. Split out of :meth:`spawn` to keep it under the radon cc
        threshold."""
        return str(spec.get("transport") or "") or self._transport or "watch"

    def spawn(self, spec: Mapping[str, object]) -> str:
        # Deferred: session_hosts imports THIS module to populate its
        # driver registry — a module-level import here would be circular.
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        # Resolved ONCE, here, and threaded into verify_config (the .mcp.json
        # existence gate, transport-scoped since Dax Part 36 §36.3), _env_pairs
        # (FLEET_TRANSPORT) and _spawn_command (the MCP-config argv posture).
        transport = self._resolve_transport(spec)
        degraded_ok = bool(spec.get("degraded_hooks_acknowledged"))
        remedies = self.verify_config(
            permission_mode=str(spec.get("permission_mode") or ""),
            transport=transport,
            degraded_hooks_acknowledged=degraded_ok,
        )
        if remedies:
            raise HostCannotSpawnError("; ".join(remedies))
        agent_instance_id = str(spec.get("agent_instance_id") or "")
        if not agent_instance_id:
            raise HostCannotSpawnError(
                "spawn spec is missing agent_instance_id — the tmux driver "
                "cannot register the spawned session under the ledger's "
                "identity without it.",
            )
        _announce_managed_policy(
            degraded_ok, agent_instance_id=agent_instance_id, host="tmux",
            denied_tools=_tmux_denied_tools_for(spec),
        )
        # W6 (#13 §44.3): shared with the headless driver so the two can never
        # disagree about what a worker is called. The tmux session name derives
        # from it, so a role-named spawn gets a role-named session for free.
        label = _resolve_local_label(spec, agent_instance_id=agent_instance_id)
        session_name = _sanitize_session_name(f"fleet-{label}-{agent_instance_id[-8:]}")
        # Minted exactly ONCE, here — never re-derived elsewhere (two
        # evaluations of an identity expression is two identities).
        agent_session_id = f"ases-{agent_instance_id}"

        # The lane root this spawn actually runs in (2026-09-07,
        # iss_2236d297): spawn_session provisions a lane worktree and hands
        # it over as `worktree_path`, and until this landed the tmux driver
        # was the one host driver that ignored the key -- the worktree was
        # created, registered, and silently abandoned while the pane opened
        # in the shared checkout. `_resolve_spawn_cwd` is headless_adapter's
        # own helper, shared for the same reason `_resolve_local_label` is:
        # the two Claude host drivers must never disagree about which tree a
        # worker runs in, and it raises HostCannotSpawnError rather than
        # falling back, so a declared-but-unusable worktree fails loudly.
        # Resolved BEFORE the env pairs, which anchor PYTHONPATH to it: the
        # pane's cwd and its import root are two halves of one isolation
        # decision and must never be computed from different roots.
        cwd = _resolve_spawn_cwd(spec, self._cwd)
        try:
            resolved_hooks = _resolve_worker_hook_paths(self._cwd)
        except WorkerHookResolutionError as exc:
            raise HostCannotSpawnError(str(exc)) from exc
        env_pairs = _env_pairs(
            agent_instance_id=agent_instance_id, agent_session_id=agent_session_id,
            label=label, solet_name=self._solet_name, solet_bin=self._solet_bin,
            allowed_tools=spec.get("allowed_tools") or (), transport=transport,
            context_gauge_reporter_path=resolved_hooks["rotation_due_watch.py"],
            cwd=cwd)
        try:
            claude_cmd = self._spawn_command(
                spec, transport=transport, label=label, resolved_hooks=resolved_hooks,
            )
        except WorkerHookResolutionError as exc:
            raise HostCannotSpawnError(str(exc)) from exc
        pane_command = _pane_command(
            claude_cmd, label=label, solet_bin=self._solet_bin, transport=transport)
        new_session_cmd = [
            self._tmux_bin, "new-session", "-d", "-s", session_name,
            "-x", str(self._pane_width), "-y", str(self._pane_height),
            *env_pairs,
            "-c", str(cwd),
            "sh", "-c", pane_command,
        ]
        self._launch_new_session(new_session_cmd)
        _configure_server_options(self._tmux_bin, session_name, self._run_fn)
        if _needs_dev_channels_confirmation(claude_cmd):
            self._confirm_dev_channels_prompt(session_name)
        return session_name

    def _launch_new_session(self, new_session_cmd: list[str]) -> None:
        """Split out of :meth:`spawn` to keep it under the radon cc
        threshold -- the ``tmux new-session`` subprocess call and its two
        failure shapes (raised exception, non-zero exit)."""
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        try:
            result = self._run_fn(
                new_session_cmd, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostCannotSpawnError(f"tmux new-session raised: {exc}") from exc
        if result.returncode != 0:
            raise HostCannotSpawnError(
                f"tmux new-session failed (exit {result.returncode}): {result.stderr.strip()}",
            )

    def _capture_pane_text(self, session_name: str) -> str:
        try:
            result = self._run_fn(
                [self._tmux_bin, "capture-pane", "-t", session_name, "-p"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout or ""

    def _await_pane_condition(self, session_name: str, predicate: Any) -> bool:
        """Bounded poll loop shared by both stages of the PTY-confirm expect
        sequence -- true the moment ``predicate`` first matches captured pane
        text, false once ``confirm_timeout_seconds`` elapses with no match."""
        deadline = time.monotonic() + self._confirm_timeout_seconds
        while True:
            if predicate(self._capture_pane_text(session_name)):
                return True
            if time.monotonic() >= deadline:
                return False
            self._sleep_fn(self._confirm_poll_interval_seconds)

    def _confirm_dev_channels_prompt(self, session_name: str) -> None:
        """The bounded expect loop (D3 slice 0a): wait for the interactive
        ``--dangerously-load-development-channels`` confirmation to appear on
        the pane, clear it with a literal Enter, then wait for it to actually
        clear before trusting the spawn. A real tmux-hosted claude sits at
        this prompt forever with no CLI bypass flag (D2 live-acceptance
        evidence, 2026-08-04 13:0xZ) -- returning early here would hand back
        a session id for a pane that never actually reaches the agent."""
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        if not self._await_pane_condition(session_name, _pane_shows_dev_channels_prompt):
            self._kill_session(session_name)
            raise HostCannotSpawnError(
                f"tmux pane {session_name!r} never showed the "
                "--dangerously-load-development-channels confirmation prompt "
                f"within {self._confirm_timeout_seconds}s -- killed the "
                "half-alive session instead of returning one that may hang "
                "forever.",
            )
        self._run_fn(
            [self._tmux_bin, "send-keys", "-t", session_name, "Enter"],
            capture_output=True, text=True, timeout=10,
        )
        cleared = self._await_pane_condition(
            session_name, lambda text: not _pane_shows_dev_channels_prompt(text),
        )
        if not cleared:
            self._kill_session(session_name)
            raise HostCannotSpawnError(
                f"tmux pane {session_name!r} still showed the "
                "development-channels confirmation prompt "
                f"{self._confirm_timeout_seconds}s after Enter was sent -- "
                "killed the half-alive session instead of returning one "
                "stuck at the prompt.",
            )

    def alive(self, host_ref: str) -> bool:
        try:
            result = self._run_fn(
                [self._tmux_bin, "has-session", "-t", host_ref],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _pane_pid(self, host_ref: str) -> int | None:
        try:
            result = self._run_fn(
                [self._tmux_bin, "list-panes", "-t", host_ref, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        first_line = (result.stdout or "").strip().splitlines()
        if not first_line:
            return None
        try:
            return int(first_line[0])
        except ValueError:
            return None

    def _kill_session(self, host_ref: str) -> None:
        """Native-session cleanup on teardown (FINDINGS requirement 4, tmux
        side) — idempotent: a session already gone is success, not an
        error, matching ``terminate_session``'s own idempotent contract."""
        with_suppress = self._run_fn(
            [self._tmux_bin, "kill-session", "-t", host_ref],
            capture_output=True, text=True, timeout=5,
        )
        del with_suppress  # non-zero here just means "already gone" — not fatal

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        grace = grace_seconds if grace_seconds > 0 else self._grace_seconds
        pane_pid = self._pane_pid(host_ref)
        if pane_pid is not None and _pid_alive(pane_pid):
            # Signal the pane's WHOLE process group, not just pane_pid: a
            # tmux pane is a fresh process-group leader, and any child the
            # pane's own claude process backgrounds (e.g. a watch-transport
            # worker's `solet-bridge watch --role X &`, the standard
            # rename-skill onboarding path for every non-MCP spawn) inherits
            # that same group. Signaling pane_pid alone leaves such a child
            # orphaned under launchd, still holding the worker's role in the
            # peer registry — peer_holds_role then reports a dead role as
            # healthy (measured live, 2026-08-09/10 restart_session run).
            try:
                pgid = os.getpgid(pane_pid)
            except ProcessLookupError:
                pgid = None
            if pgid is not None:
                _sigterm_then_kill_process_group(pgid, grace)
            else:
                _sigterm_then_kill(pane_pid, None, grace)
        # Kill the tmux session regardless of whether a pane pid was found —
        # this is the ONE unconditional native-cleanup step (FINDINGS
        # requirement 4): it must run even if the pid-level signal path
        # found nothing (a session with no pane, or a race).
        self._kill_session(host_ref)

    def driver_channel(self, host_ref: str) -> _TmuxSendKeysDriverChannel | None:
        if not self.alive(host_ref):
            return None
        return _TmuxSendKeysDriverChannel(
            tmux_bin=self._tmux_bin, session=host_ref, run_fn=self._run_fn,
        )


__all__ = [
    "DEFAULT_TERMINATE_GRACE_SECONDS",
    "TmuxHostDriver",
]
