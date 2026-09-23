"""P4 -- idle-boundary seat watcher (rotation-systematization P4(b), P4(a)
ratification 2026-08-07, six bound conditions --
``workbench/2026-08-07_rotation_systematization_findings_rotation-impl.md``).

Detached, long-running, out-of-process poll loop (nohup/pidfile-hosted, same
class as :mod:`seat_rotation_helper`'s own invocation -- no launchd, no pty).
Closes the measured incident this slice exists for: the seat sat idle ~1.5h
with two actionable role-inbox messages unserved, until the operator manually
``/clear``'d it post-cache-expiry (the only wake that existed for the seat
before this watcher). The watcher issues ZERO model/inference calls ever --
pure polling, cost bounded per tick (one ``ps eww``, one file stat, and only
when the idle signal alone suggests it's worth looking closer, one iTerm2
screen read and/or one merged ``solet-bridge inbox`` drain).

Two actions, mutually exclusive per tick, poke evaluated first (P4(a).4
precedence, verbatim): ``poke`` -- pending role-inbox items AND idle past
:data:`agent_messaging_plugin.rotation_thresholds.IDLE_POKE_THRESHOLD_SECONDS`
-> inject a short wake turn via :func:`seat_rotation_helper.run_rotation`'s
``inject_only`` mode (no ``/clear``, no pickup -- exactly the same
already-proven send-confirm-submit sequence, just without the leading
clear). ``rotate`` -- idle past
:data:`...IDLE_ROTATE_THRESHOLD_SECONDS` with NOTHING pending -> the full
rotation helper. Any pending mail defers rotate entirely, regardless of
whether poke itself is currently on cooldown -- draining real pending work
in a live turn is judged better than rotating past unread mail (P4(a).4).

Hard design rules, each individually ratified (not self-imposed):

- **Identity re-resolved every poll, never cached** (condition 3): seat pid,
  ``AGENT_SESSION_ID``, ``AGENT_INSTANCE_ID``, and pane tag are all read
  fresh each tick via :func:`resolve_seat_identity`.
- **Pending-COUNT only, message bodies never logged/persisted/forwarded**
  (condition 2, HARD design rule): :func:`resolve_pending_count` returns a
  bare ``int | None`` -- the merged inbox payload exists only inside that one
  function's local scope
  and are never returned, printed, or otherwise carried past it. The poke
  message itself is a fixed, operator-authored constant
  (:data:`DEFAULT_POKE_MESSAGE`) that takes no inbox-derived input at all --
  structurally incapable of leaking inbox content, not merely disciplined
  not to.
- **ACT-time rail, immediately before ANY injection** (condition 5,
  unchanged from proposal): :func:`_confirm_pane_ready_for_action` re-reads
  the pane fresh and requires BOTH a stable screen
  (:func:`seat_rotation_helper.wait_for_screen_stable`) and a positively
  confirmed empty composer (:func:`seat_rotation_helper.is_cleared_state`)
  before calling :func:`seat_rotation_helper.run_rotation` at all -- a
  standalone outer gate, distinct from and prior to ``run_rotation``'s own
  internal settle-wait, matching the operator-present rail: the watcher
  must never race a live human conversation with the seat.
- **No self-restart machinery, fail-safe degradation accepted** (condition
  6): a dead watcher simply stops acting -- the seat's floor reverts to
  manual operator ``/clear``, exactly as before this slice existed. The
  runbook (P3, watcher section) must name this limitation explicitly, never
  claim standing coverage.

The pending check uses ``solet-bridge inbox`` so it reaches the same merged,
fully-paged frontier a seat later reads; it has no consumption side effect.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from agent_messaging_plugin import rotation_thresholds as _rotation_thresholds
from agent_messaging_plugin import seat_rotation_helper as _rotation_helper

from .driver_texts import render_driver_text

try:
    import iterm2 as _iterm2_module  # pyright: ignore[reportMissingImports]
except ModuleNotFoundError as _exc:  # pragma: no cover - covered by the blocked-import smoke leg
    # Same disclosure contract as seat_rotation_helper.py, and deliberately its
    # own guard rather than a shared one: each module must be independently
    # importable on a box with no iTerm2 bindings, so neither can be made to
    # depend on the other having succeeded. See that module for the full
    # rationale (iterm2 is undeclared here and arrives only with
    # iterm2_coding_agent_management_plugin, excluded from the shipped profile).
    _iterm2_module = None
    _iterm2_import_error: str | None = str(_exc)
else:
    _iterm2_import_error = None

# Bound exactly once: pyright strict treats an uppercase name as a constant and
# refuses a second assignment, so the branch writes the lowercase working name.
ITERM2_IMPORT_ERROR: Final[str | None] = _iterm2_import_error

# Same rationale as seat_rotation_helper.py: the iterm2 distribution ships no
# py.typed marker. ``None`` means the bindings are absent -- legitimate on any
# machine that is not an operator's iTerm2 seat.
_iterm: Any = _iterm2_module

ACTION_POKE = "poke"
ACTION_ROTATE = "rotate"
ACTION_NONE = "none"

DEFAULT_POKE_MESSAGE = render_driver_text("seat.poke")
"""Fixed, operator-authored constant -- takes no inbox-derived input at all
(condition 2). This is the ENTIRE text ever injected on a poke; it is never
built from, or interpolated with, anything read from ``peer_inbox``."""

_STATUS_IDLE = "idle"
_AGENT_SESSION_ID_ENV = "AGENT_SESSION_ID"
_AGENT_INSTANCE_ID_ENV = "AGENT_INSTANCE_ID"


def _log(message: str) -> None:
    print(f"[seat-idle-watcher] {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True, slots=True)
class SeatIdentity:
    """One fresh, point-in-time resolution of the seat's identity -- never
    held across ticks (condition 3). ``agent_instance_id`` is ``None`` when
    absent from the process env -- MEASURED live-fire evidence (fix loop #3,
    2026-08-07) is that the real seat's interactive launch does not export
    it today (only ``AGENT_SESSION_ID``/``AGENT_SESSION_LABEL``/``AGENT_WAKE_
    CLI`` are present -- a separate, deliberately-deferred machine-config
    gap, not something this watcher can or should paper over by fabricating
    a value). ``agent_session_id`` stays a hard requirement -- it is the one
    field this watcher actually NEEDS to act (``peer_inbox`` is keyed on
    it); ``agent_instance_id`` is used only for poke-cooldown state-file
    keying, where an explicit, honestly-labeled placeholder is safe."""

    pid: int
    claude_session_id: str
    agent_session_id: str
    agent_instance_id: str | None
    status: str
    status_updated_at_ms: float
    session_label: str


@dataclass(frozen=True, slots=True)
class WatchDecision:
    action: str
    reason: str


def is_pid_alive(pid: int) -> bool:
    """Same-user liveness check, no signal actually delivered (signal 0)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def find_session_file(role_tag: str) -> Path | None:
    """The live ``~/.claude/sessions/<pid>.json`` file whose ``name`` field
    equals ``role_tag`` and whose pid (the filename stem) is a currently
    running process -- filters out stale files left by earlier launches."""
    sessions_dir = Path.home() / ".claude" / "sessions"
    try:
        candidates = sorted(sessions_dir.glob("*.json"))
    except OSError:
        return None
    for candidate in candidates:
        try:
            pid = int(candidate.stem)
        except ValueError:
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("name") or "") != role_tag:
            continue
        if is_pid_alive(pid):
            return candidate
    return None


def read_process_env_var(pid: int, var_name: str) -> str | None:
    """Read a same-user process's own exported environment via
    ``ps eww`` -- no special entitlement required on macOS (measured live,
    P4(a).2). Returns ``None`` on any lookup failure (process gone, ``ps``
    unavailable, var not set)."""
    try:
        result = subprocess.run(
            ["ps", "eww", "-p", str(pid)],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    prefix = f"{var_name}="
    for token in result.stdout.split():
        if token.startswith(prefix):
            return token[len(prefix):]
    return None


def _parse_session_file(session_file: Path) -> tuple[int, dict[str, Any]] | None:
    """The pid (from the filename) + parsed JSON body, or ``None`` on any
    read/parse/pid failure. Split out of :func:`resolve_seat_identity` to
    keep it a straight-line dispatcher (radon cc)."""
    try:
        pid = int(session_file.stem)
    except ValueError:
        return None
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return pid, data


def _resolve_process_identity_env(pid: int) -> tuple[str, str | None] | None:
    """``(agent_session_id, agent_instance_id)`` read fresh off the live
    process's own environment. Returns ``None`` ONLY when ``agent_session_id``
    itself is missing -- that field is a hard requirement (``peer_inbox`` is
    keyed on it). ``agent_instance_id`` being absent is tolerated and passed
    through as ``None`` -- MEASURED (fix loop #3, 2026-08-07): the real
    seat's process env carries ``AGENT_SESSION_ID`` but not
    ``AGENT_INSTANCE_ID`` today (a separate, deliberately-deferred
    machine-config gap -- see :class:`SeatIdentity`'s docstring). Split out
    of :func:`resolve_seat_identity` to keep it a straight-line dispatcher
    (radon cc)."""
    agent_session_id = read_process_env_var(pid, _AGENT_SESSION_ID_ENV)
    if not agent_session_id:
        return None
    agent_instance_id = read_process_env_var(pid, _AGENT_INSTANCE_ID_ENV)
    return agent_session_id, agent_instance_id


def resolve_seat_identity(role_tag: str) -> SeatIdentity | None:
    """Fresh, uncached resolution of every identity fact this watcher needs
    (condition 3). Returns ``None`` -- never raises -- for any missing piece,
    so a caller treats "can't resolve identity right now" as a legitimate
    skip-this-tick outcome, not a crash."""
    session_file = find_session_file(role_tag)
    if session_file is None:
        return None
    parsed = _parse_session_file(session_file)
    if parsed is None:
        return None
    pid, data = parsed
    claude_session_id = str(data.get("sessionId") or "")
    status = str(data.get("status") or "")
    status_updated_at_ms = data.get("statusUpdatedAt")
    if not claude_session_id or not isinstance(status_updated_at_ms, (int, float)):
        return None
    env_identity = _resolve_process_identity_env(pid)
    if env_identity is None:
        return None
    agent_session_id, agent_instance_id = env_identity
    return SeatIdentity(
        pid=pid,
        claude_session_id=claude_session_id,
        agent_session_id=agent_session_id,
        agent_instance_id=agent_instance_id,
        status=status,
        status_updated_at_ms=float(status_updated_at_ms),
        session_label=str(data.get("name") or role_tag),
    )


def compute_idle_seconds(identity: SeatIdentity, *, now_ms: float) -> float:
    """0.0 whenever the session-status file doesn't say ``idle`` -- a
    non-idle status is never treated as "idle for a negative/zero amount of
    time," it is treated as flatly not idle."""
    if identity.status != _STATUS_IDLE:
        return 0.0
    return max(0.0, (now_ms - identity.status_updated_at_ms) / 1000.0)


def project_dir_slug_for(project_dir: Path) -> str:
    """The transcript-directory slug Claude Code derives from an absolute
    project path -- every ``/`` becomes ``-`` (measured live, P1(b)/P4(a).1:
    ``/Users/alice/Workspace/solet`` -> ``-Users-alice-Workspace-solet``)."""
    return str(project_dir).replace("/", "-")


def transcript_path_for(project_dir_slug: str, claude_session_id: str) -> Path:
    return Path.home() / ".claude" / "projects" / project_dir_slug / f"{claude_session_id}.jsonl"


def cross_check_idle(
    identity: SeatIdentity, transcript_path: Path, *,
    agreement_tolerance_seconds: float = 5.0,
) -> bool:
    """``True`` when the transcript corroborates the status file's claimed
    idle state, OR the transcript can't be read at all (nothing to disagree
    with -- the status file is trusted alone in that case). ``False`` means
    the two signals actively disagree enough that idle should NOT be trusted
    this tick -- fail-safe: skip, never act on a contested signal.

    ONE-SIDED since 2026-08-09. The contradiction this guard exists to catch
    is a status file claiming "idle" while the session is in fact still
    working -- and that shows up as a transcript written AFTER the idle
    stamp. A transcript OLDER than the stamp is not a contradiction at all:
    it says the session has been quiet for even longer than the status file
    claims, which corroborates idleness rather than refuting it.

    The original symmetric ``abs()`` form silently assumed idle is stamped at
    the same instant as the turn's last transcript write (measured that way
    live in P4(a).1: both signals within 1s). That assumption holds only when
    nothing occupies the turn boundary. Once the coordination Stop hook waits
    a bounded interval before releasing (see ``wake_waiter.py``), idle is
    stamped when the WAIT ends -- by construction minutes after the last
    transcript write -- and the symmetric form would reject every genuinely
    idle tick. That is the same class of defect as the unbounded wait it
    accompanies: a guard whose reference point drifted out from under it.
    """
    try:
        mtime_ms = transcript_path.stat().st_mtime * 1000.0
    except OSError:
        return True
    return mtime_ms - identity.status_updated_at_ms <= agreement_tolerance_seconds * 1000.0


def _solet_inbox() -> dict[str, Any] | None:
    """Run the merged, frontier-paged inbox reader for this seat session."""
    if shutil.which("solet-bridge") is None:
        _log("solet-bridge is unavailable on PATH; refusing the seat-idle inbox read")
        return None
    try:
        result = subprocess.run(
            ["solet-bridge", "inbox", "--observer"], capture_output=True, text=True,
            timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"solet-bridge inbox failed to run: {exc}")
        return None
    if result.returncode != 0:
        _log(f"solet-bridge inbox exited {result.returncode}")
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        _log(f"solet-bridge inbox returned unparseable output: {exc}")
        return None
    if not isinstance(payload, dict) or payload.get("complete") is not True:
        _log("solet-bridge inbox did not report a complete merged drain")
        return None
    return payload


def resolve_pending_count(agent_session_id: str) -> int | None:
    """Return the full merged drain's role count without retaining its content.

    ``agent_session_id`` remains an explicit argument because this caller
    resolved it fresh immediately before the poll; the CLI independently
    verifies the same environment-derived identity. ``None`` means the
    frontier could not be established, never "empty".
    """
    if not agent_session_id:
        return None
    payload = _solet_inbox()
    if payload is None:
        return None
    role_count = payload.get("role_count")
    return role_count if isinstance(role_count, int) and role_count >= 0 else None


_NO_AGENT_INSTANCE_ID_PLACEHOLDER = "no-agent-instance-id"
"""An explicit, honestly-labeled placeholder -- never a fabricated ID.
``claude_session_id`` alone already uniquely identifies a session-generation
(it changes on every rotation/clear), so this placeholder costs nothing in
correctness; it exists only so the state filename stays legible about WHY
the identity component is missing, rather than silently omitting it."""


def _state_path(marker_dir: Path, agent_instance_id: str | None, claude_session_id: str) -> Path:
    instance_component = agent_instance_id or _NO_AGENT_INSTANCE_ID_PLACEHOLDER
    return marker_dir / f"{instance_component}__{claude_session_id}.seat_idle_watcher_poke_state.json"


def _read_last_poke_at(state_path: Path) -> float | None:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("last_poke_at")
    return float(value) if isinstance(value, (int, float)) else None


def _record_poke(state_path: Path, *, at: float) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_poke_at": at}))


def is_poke_on_cooldown(
    last_poke_at: float | None, *, now: float,
    cooldown_seconds: float = _rotation_thresholds.POKE_COOLDOWN_SECONDS,
) -> bool:
    if last_poke_at is None:
        return False
    return (now - last_poke_at) < cooldown_seconds


def decide_action(
    *, idle_seconds: float, pending_count: int, poke_on_cooldown: bool,
    idle_poke_threshold_seconds: float = _rotation_thresholds.IDLE_POKE_THRESHOLD_SECONDS,
    idle_rotate_threshold_seconds: float = _rotation_thresholds.IDLE_ROTATE_THRESHOLD_SECONDS,
) -> WatchDecision:
    """Pure precedence decision (P4(a).4, verbatim): poke evaluated first;
    ANY pending mail defers rotate entirely -- rotate only ever fires when
    idle alone crosses its threshold with NOTHING pending, regardless of
    whether poke itself happens to be on cooldown at that moment."""
    has_pending = pending_count > 0
    poke_due = has_pending and idle_seconds >= idle_poke_threshold_seconds
    rotate_due = idle_seconds >= idle_rotate_threshold_seconds

    if poke_due and not poke_on_cooldown:
        return WatchDecision(
            ACTION_POKE,
            f"idle {idle_seconds:.0f}s >= poke threshold "
            f"({idle_poke_threshold_seconds:.0f}s) with {pending_count} pending",
        )
    if has_pending:
        cooldown_note = "poke on cooldown" if poke_on_cooldown else "idle below poke threshold"
        return WatchDecision(
            ACTION_NONE,
            f"{pending_count} pending, {cooldown_note} -- rotate deferred per precedence",
        )
    if rotate_due:
        return WatchDecision(
            ACTION_ROTATE,
            f"idle {idle_seconds:.0f}s >= rotate threshold "
            f"({idle_rotate_threshold_seconds:.0f}s), nothing pending",
        )
    return WatchDecision(ACTION_NONE, "below both thresholds, nothing pending")


async def _confirm_pane_ready_for_action(
    role_tag: str, cleared_signature: str, *, settle_timeout_seconds: float,
) -> tuple[bool, str]:
    """Condition 5's dedicated outer gate: fresh pane resolution, a
    stable-screen check, THEN a positive empty-composer check -- performed
    immediately before the caller decides whether to invoke
    :func:`seat_rotation_helper.run_rotation` at all. Distinct from (and
    prior to) ``run_rotation``'s own internal settle-wait. Never trusts the
    idle-file signal alone for the ACT decision -- only for the "should I
    even look" decision upstream of this call."""
    if _iterm is None:
        # This site reports in-band rather than raising, matching its own
        # adjacent no-app failure mode -- the disclosure is the reason string.
        return False, f"{_rotation_helper.ITERM2_UNAVAILABLE_MESSAGE} ({ITERM2_IMPORT_ERROR})"
    connection = await _iterm.Connection.async_create()
    app = await _iterm.async_get_app(connection)
    if app is None:
        return False, "iTerm2 Python API returned no app object"
    rows, session_by_id = await _rotation_helper._live_role_rows(app)  # noqa: SLF001
    try:
        match = _rotation_helper.resolve_single_pane(rows, role_tag)
    except _rotation_helper.PaneResolutionError as exc:
        return False, f"pane resolution failed: {exc.message}"
    session = session_by_id[match.session_id]
    try:
        await _rotation_helper.wait_for_screen_stable(
            session, timeout_seconds=settle_timeout_seconds,
        )
    except _rotation_helper.ScreenStabilityTimeoutError:
        return False, "screen not stable (mid-redraw or an active turn in progress)"
    contents = await session.async_get_screen_contents()
    lines = [contents.line(i).string for i in range(contents.number_of_lines)]
    if not _rotation_helper.is_cleared_state(lines, cleared_signature):
        return False, "composer not confirmed empty"
    return True, "confirmed ready"


async def run_tick(
    *, role_tag: str, cleared_signature: str, pickup_prompt_path: Path,
    poke_message: str, marker_dir: Path, project_dir: Path,
    idle_poke_threshold_seconds: float = _rotation_thresholds.IDLE_POKE_THRESHOLD_SECONDS,
    idle_rotate_threshold_seconds: float = _rotation_thresholds.IDLE_ROTATE_THRESHOLD_SECONDS,
    poke_cooldown_seconds: float = _rotation_thresholds.POKE_COOLDOWN_SECONDS,
    settle_timeout_seconds: float = _rotation_helper.DEFAULT_SETTLE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One full poll tick: resolve identity fresh, compute idle, cross-check,
    check pending mail, decide, gate, act. Every branch returns a
    JSON-serializable envelope describing exactly what happened (or didn't)
    -- nothing is silently swallowed."""
    identity = resolve_seat_identity(role_tag)
    if identity is None:
        return {"tick_status": "skipped", "reason": f"could not resolve a live session for role_tag={role_tag!r}"}

    now = time.time()
    idle_seconds = compute_idle_seconds(identity, now_ms=now * 1000.0)
    if idle_seconds <= 0.0:
        return {"tick_status": "skipped", "reason": "seat not idle", "status": identity.status}

    transcript_path = transcript_path_for(project_dir_slug_for(project_dir), identity.claude_session_id)
    if not cross_check_idle(identity, transcript_path):
        return {"tick_status": "skipped", "reason": "idle-file/transcript-mtime cross-check disagreed; fail-safe skip"}

    pending_count = resolve_pending_count(identity.agent_session_id)
    if pending_count is None:
        pending_count = 0

    state_path = _state_path(marker_dir, identity.agent_instance_id, identity.claude_session_id)
    on_cooldown = is_poke_on_cooldown(
        _read_last_poke_at(state_path), now=now, cooldown_seconds=poke_cooldown_seconds,
    )
    decision = decide_action(
        idle_seconds=idle_seconds, pending_count=pending_count, poke_on_cooldown=on_cooldown,
        idle_poke_threshold_seconds=idle_poke_threshold_seconds,
        idle_rotate_threshold_seconds=idle_rotate_threshold_seconds,
    )
    if decision.action == ACTION_NONE:
        return {
            "tick_status": "no_action", "reason": decision.reason,
            "idle_seconds": idle_seconds, "pending_count": pending_count,
        }

    ready, ready_reason = await _confirm_pane_ready_for_action(
        role_tag, cleared_signature, settle_timeout_seconds=settle_timeout_seconds,
    )
    if not ready:
        return {
            "tick_status": "refused", "decision": decision.action,
            "reason": f"ACT-time gate failed: {ready_reason}",
        }

    if decision.action == ACTION_POKE:
        result = await _rotation_helper.run_rotation(
            role_tag, poke_message, cleared_signature=cleared_signature,
            settle_timeout_seconds=settle_timeout_seconds, inject_only=True,
        )
        if result.get("status") == "completed":
            _record_poke(state_path, at=now)
        return {"tick_status": "acted", "action": ACTION_POKE, "result": result}

    pickup_text = pickup_prompt_path.read_text(encoding="utf-8")
    result = await _rotation_helper.run_rotation(
        role_tag, pickup_text, cleared_signature=cleared_signature,
        settle_timeout_seconds=settle_timeout_seconds,
    )
    return {"tick_status": "acted", "action": ACTION_ROTATE, "result": result}


def write_liveness(liveness_path: Path, *, pid: int, last_poll_at: float) -> None:
    """Condition 6's required liveness surface: pidfile + last-poll
    timestamp, checked by the runbook's status command. A stale/missing
    file IS the fail-safe-degraded signal -- no self-restart machinery
    reads or reacts to this file; a human/steward does."""
    liveness_path.parent.mkdir(parents=True, exist_ok=True)
    liveness_path.write_text(json.dumps({"pid": pid, "last_poll_at": last_poll_at}))


async def poll_forever(args: argparse.Namespace) -> None:
    marker_dir = Path(args.marker_dir)
    liveness_path = Path(args.liveness_path)
    while True:
        try:
            result = await run_tick(
                role_tag=args.role_tag,
                cleared_signature=args.cleared_signature,
                pickup_prompt_path=Path(args.pickup_prompt_file),
                poke_message=args.poke_message,
                marker_dir=marker_dir,
                project_dir=Path(args.project_dir),
                idle_poke_threshold_seconds=args.idle_poke_threshold_seconds,
                idle_rotate_threshold_seconds=args.idle_rotate_threshold_seconds,
                poke_cooldown_seconds=args.poke_cooldown_seconds,
                settle_timeout_seconds=args.settle_timeout_seconds,
            )
            _log(json.dumps(result))
        except Exception as exc:  # noqa: BLE001 -- the watcher must never crash-loop silently
            _log(f"tick raised: {exc!r}")
        write_liveness(liveness_path, pid=os.getpid(), last_poll_at=time.time())
        if args.once:
            return
        await asyncio.sleep(args.poll_interval_seconds)


def _default_marker_dir() -> str:
    return str(Path.home() / ".claude" / "fleet_watchers" / "seat_idle_watcher")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Idle-boundary seat watcher: poke on pending mail, rotate near cache expiry.",
    )
    parser.add_argument("--role-tag", required=True)
    parser.add_argument("--cleared-signature", default="❯")
    parser.add_argument("--pickup-prompt-file", required=True, type=Path)
    parser.add_argument("--poke-message", default=DEFAULT_POKE_MESSAGE)
    parser.add_argument("--marker-dir", default=_default_marker_dir())
    parser.add_argument(
        "--liveness-path",
        default=str(Path.home() / ".claude" / "fleet_watchers" / "seat_idle_watcher" / "liveness.json"),
    )
    parser.add_argument("--project-dir", default=str(Path.cwd()))
    parser.add_argument(
        "--poll-interval-seconds", type=float,
        default=_rotation_thresholds.WATCH_POLL_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--idle-poke-threshold-seconds", type=float,
        default=_rotation_thresholds.IDLE_POKE_THRESHOLD_SECONDS,
    )
    parser.add_argument(
        "--idle-rotate-threshold-seconds", type=float,
        default=_rotation_thresholds.IDLE_ROTATE_THRESHOLD_SECONDS,
    )
    parser.add_argument(
        "--poke-cooldown-seconds", type=float,
        default=_rotation_thresholds.POKE_COOLDOWN_SECONDS,
    )
    parser.add_argument(
        "--settle-timeout-seconds", type=float,
        default=_rotation_helper.DEFAULT_SETTLE_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single tick and exit (status checks, manual testing) instead of looping forever.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    asyncio.run(poll_forever(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
