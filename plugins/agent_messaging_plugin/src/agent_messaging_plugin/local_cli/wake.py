"""`<name> wake` — MCP-free turn injection for a watcher-held session.

`<name> watch` (the delivery half) tees every message-bearing line into a
per-session spool file. This command (the wake half) blocks until the spool
grows past the consumed offset, surfaces the new lines on stderr, and exits
with the Claude Code hook wake code (2). Idle cost is a 1 Hz ``stat`` — zero
tokens, zero inference, zero network.

Wired as a `Stop` hook it is correct under BOTH hook shapes with the same
exit contract:

* ``asyncRewake: true`` (current Claude Code): the hook runs in the
  background, the session goes properly idle, and the exit-2 stderr is shown
  to the model as a system reminder — a real wake on a real message.
* synchronous (older Claude Code, no ``asyncRewake`` support): exit 2 blocks
  the stop and feeds stderr to the model — the retired bizops session
  broker's proven blocking shape.

Because it is a shell hook and not an inference channel, it is
provider-agnostic: it works identically on Anthropic-direct and Bedrock, on
machines where MCP is policy-blocked (an enterprise posture documented against
a live deployment).
"""

from __future__ import annotations

import fcntl
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

import click
from ananta.constants import ExitCodes

from ..env_contract import AGENT_INSTANCE_ID_ENV, enforce_no_legacy_agent_env
from .client import SoletIdentityError, resolve_solet_name
from .spool import (
    WATCH_SESSION_ID_ENV,
    WATCH_SESSION_LABEL_ENV,
    default_spool_path,
    read_watch_pairing,
    resolve_watch_instance_id,
    spool_lock_path,
    spool_offset_path,
    watch_pairing_path,
)
from .wake_role_receipts import reconcile_role_receipts

# Claude Code hook contract: exit 2 is the wake/block signal for BOTH the
# asyncRewake background shape and the synchronous block-stop shape.
WAKE_EXIT_SIGNAL: Final[int] = 2
WAKE_POLL_INTERVAL_S: Final[float] = 1.0
# Just under the 86400s hook timeout the hydration settings template ships,
# so the waker always exits cleanly instead of being cancelled mid-poll.
WAKE_DEFAULT_MAX_WAIT_S: Final[float] = 86100.0
WAKE_MAX_SURFACED_LINES: Final[int] = 40
# Truncate the spool once everything is consumed and it has grown past this;
# fleet message traffic makes this a months-scale horizon, not a hot path.
WAKE_SPOOL_TRUNCATE_BYTES: Final[int] = 1_048_576


@dataclass(frozen=True)
class WakeTarget:
    """The per-session spool trio the wake hook operates on."""

    role: str
    solet_name: str
    spool: Path
    offset_file: Path
    lock_file: Path
    agent_session_id: str = ""
    agent_instance_id: str = ""


@click.command(name="wake")
@click.option(
    "--spool",
    "spool_override",
    type=click.Path(path_type=Path),
    default=None,
    help="Spool file to block on (default: this session's derived watch spool).",
)
@click.option(
    "--max-wait",
    "max_wait_s",
    type=float,
    default=WAKE_DEFAULT_MAX_WAIT_S,
    show_default=True,
    help="Seconds to wait for a delivery before allowing the stop (exit 0).",
)
def wake(spool_override: Path | None, max_wait_s: float) -> None:
    """Block until this session's watcher delivers, then wake the session.

    Exit 2 + the new spool lines on stderr = wake (Claude Code Stop-hook
    contract, sync or asyncRewake alike). Exit 0 = nothing to do: not a
    fleet session, another waker already armed, or --max-wait expired idle.
    """
    target = _resolve_target(spool_override)
    if target is None:
        return
    singleton = _acquire_singleton(target.lock_file)
    if singleton is None:
        return
    with singleton:
        delivery = _block_until_delivery(target, max_wait_s)
        if delivery is None:
            return
        lines, new_offset = delivery
        lines, warning = reconcile_role_receipts(
            agent_session_id=target.agent_session_id,
            agent_instance_id=target.agent_instance_id,
            lines=lines,
        )
        if warning:
            click.echo(f"solet-bridge wake: {warning}", err=True)
        if not lines:
            _write_offset(target.offset_file, new_offset)
            return
        # D5 / REL-05 symmetry: EMIT FIRST, THEN COMMIT. The offset flip is this
        # path's `/peer/delivered`, and REL-05 already ruled emit-before-flip on
        # the MCP half -- so both delivery halves are now confirm-then-commit.
        # Committing first meant a kill in the gap (crash, /clear, operator ^C,
        # hook timeout) recorded the lines as consumed while nothing had ever
        # been written: at-most-once, silently.
        #
        # What this does and does not prove: a successful stderr write proves
        # EMISSION, not consumption -- it says the packet left this process, not
        # that a model read it. The residual window (after the write, before the
        # flip) yields EXACTLY ONE duplicate wake per crash: the singleton flock
        # bounds concurrency to one waker, and the re-emitted lines are the same
        # bytes, so a session recognises them as already handled.
        click.echo(_compose_wake_packet(target, lines), err=True)
        _write_offset(target.offset_file, new_offset)
    raise SystemExit(WAKE_EXIT_SIGNAL)


def _resolve_target(spool_override: Path | None) -> WakeTarget | None:
    """Derive the session's spool trio; ``None`` for non-fleet sessions.

    A missing launcher identity is the one silent path: the Stop hook is
    installed at user scope, so it fires in plain unlabeled sessions too and
    must be a perfect no-op there. A LEGACY identity (an un-migrated
    launcher exporting either pre-rename prefixed generation, e.g.
    ``HOMUNCULUS_AGENT_*``) is not silent: it surfaces loudly on the
    non-wake error exit, mirroring the identity-failure path below.
    """
    try:
        enforce_no_legacy_agent_env()
    except RuntimeError as exc:
        click.echo(f"solet-bridge wake: {exc}", err=True)
        # Same contract as the identity failure below: a plain non-blocking
        # hook error exit, never the wake/block signal.
        raise SystemExit(int(ExitCodes.UNKNOWN_ERROR)) from exc
    role = os.environ.get(WATCH_SESSION_LABEL_ENV, "")
    session_id = os.environ.get(WATCH_SESSION_ID_ENV, "")
    if not role and not session_id:
        return None
    if not role or not session_id:
        # Census D3: NEITHER present is a plain non-fleet session and stays
        # silent (above). Exactly ONE present is a fleet session whose launcher
        # identity is broken, and staying silent there makes it permanently
        # un-wakeable with no diagnostic anywhere — while `watch`, given the
        # same environment, dies loud. The two halves of one mechanism must not
        # disagree about whether the same missing input is fatal.
        missing = WATCH_SESSION_LABEL_ENV if not role else WATCH_SESSION_ID_ENV
        present = WATCH_SESSION_ID_ENV if not role else WATCH_SESSION_LABEL_ENV
        click.echo(
            f"solet-bridge wake: ${present} is set but ${missing} is not, so this "
            "session cannot be paired with its watcher and will never be woken. "
            "A launcher that exports one must export both.",
            err=True,
        )
        raise SystemExit(int(ExitCodes.UNKNOWN_ERROR))
    try:
        name = resolve_solet_name()
    except SoletIdentityError as exc:
        click.echo(f"solet-bridge wake: {exc}", err=True)
        # Deliberately NOT ExitCodes.CONNECTION_ERROR: that is 2, the hook
        # wake/block signal — an identity failure must surface as a plain
        # non-blocking hook error, never impersonate a wake.
        raise SystemExit(int(ExitCodes.UNKNOWN_ERROR)) from exc
    instance_id = resolve_watch_instance_id(
        session_id,
        os.environ.get(AGENT_INSTANCE_ID_ENV),
    )
    spool = spool_override or _paired_spool(name, instance_id)
    return WakeTarget(
        role=role,
        solet_name=name,
        spool=spool,
        offset_file=spool_offset_path(spool),
        lock_file=spool_lock_path(spool),
        agent_session_id=session_id,
        agent_instance_id=instance_id,
    )


def _paired_spool(name: str, instance_id: str) -> Path:
    """The spool THIS session's watcher actually chose (census D4).

    Precedence: an explicit ``wake --spool`` wins (handled by the caller), then
    the watcher's published choice, then the derived default. Reading the
    watcher's sidecar is what stops ``watch --spool <other>`` from silently
    decoupling the pair — previously both halves derived the default
    independently and neither could observe that the other had moved.

    A watcher that armed with ``--no-spool`` publishes a null choice: that is a
    configuration in which no wake can ever arrive, so it is reported and the
    hook exits non-wake rather than blocking out its whole ``--max-wait``.
    """
    found, spool = read_watch_pairing(watch_pairing_path(name, instance_id))
    if found and spool is None:
        click.echo(
            "solet-bridge wake: this session's watcher armed with --no-spool, so "
            "no delivery can ever reach the wake hook. Re-arm `watch` without "
            "--no-spool to restore waking.",
            err=True,
        )
        raise SystemExit(int(ExitCodes.UNKNOWN_ERROR))
    return spool if spool is not None else default_spool_path(name, instance_id)


def _acquire_singleton(lock_file: Path) -> IO[str] | None:
    """Take the per-session waker lock; ``None`` when a waker is already armed.

    Every turn's Stop event spawns a fresh waker; the flock (released by the
    kernel on process death, so never stale) collapses them to one.
    """
    handle = lock_file.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _block_until_delivery(
    target: WakeTarget, max_wait_s: float,
) -> tuple[list[str], int] | None:
    """Poll the spool until complete new lines land; ``None`` on idle expiry.

    Returns the lines PLUS the offset they end at, and deliberately does NOT
    commit it. The commit belongs to the caller, after the packet has actually
    been written -- see the emit-then-commit note there. Reading and committing
    in one step is exactly what made the consumed offset advance for lines no
    one had been shown.
    """
    offset = _reconcile_offset(target)
    deadline = time.monotonic() + max_wait_s
    while True:
        lines, new_offset = _consume_complete_lines(target.spool, offset)
        if lines:
            return lines, new_offset
        if time.monotonic() >= deadline:
            return None
        time.sleep(WAKE_POLL_INTERVAL_S)


def _reconcile_offset(target: WakeTarget) -> int:
    """Load the consumed offset; clamp on spool replacement; opportunistically
    truncate a fully-consumed oversized spool (locked, so appends never race).
    """
    offset = _read_offset(target.offset_file)
    size = target.spool.stat().st_size if target.spool.exists() else 0
    if offset > size:
        # The spool was recreated shorter than the recorded offset (manual
        # cleanup or a fresh runtime dir): resurface from the start rather
        # than silently skipping deliveries.
        offset = 0
        _write_offset(target.offset_file, offset)
    if offset == size and size > WAKE_SPOOL_TRUNCATE_BYTES:
        offset = _truncate_consumed_spool(target, offset)
    return offset


def _truncate_consumed_spool(target: WakeTarget, offset: int) -> int:
    """Truncate the spool iff it is still fully consumed under the lock."""
    with target.spool.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0, os.SEEK_END)
        if handle.tell() != offset:
            return offset  # an append won the race; nothing is lost
        handle.truncate(0)
    _write_offset(target.offset_file, 0)
    return 0


def _consume_complete_lines(spool: Path, offset: int) -> tuple[list[str], int]:
    """Read complete lines past ``offset``; a trailing partial line waits."""
    if not spool.exists():
        return [], offset
    with spool.open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        handle.seek(offset)
        chunk = handle.read()
    boundary = chunk.rfind(b"\n")
    if boundary < 0:
        return [], offset
    complete = chunk[: boundary + 1]
    lines = [
        line for line in complete.decode("utf-8").splitlines() if line.strip()
    ]
    return lines, offset + len(complete)


def _read_offset(offset_file: Path) -> int:
    """The consumed byte offset; 0 before first consumption."""
    if not offset_file.exists():
        return 0
    raw = offset_file.read_text(encoding="utf-8").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(
            f"solet-bridge wake: offset sidecar {offset_file} is corrupt "
            f"({raw!r}) — delete it to resurface the spool from the start.",
        ) from exc
    return max(value, 0)


def _write_offset(offset_file: Path, value: int) -> None:
    offset_file.write_text(f"{value}\n", encoding="utf-8")


def _compose_wake_packet(target: WakeTarget, lines: list[str]) -> str:
    """The stderr wake packet — shown to the model as its reason to act.

    The durable-copies command carries ``agent_session_id`` because the process
    resolves the reader's identity from that argument alone: ``solet-bridge call``
    opens a fresh, unregistered bridge, so unlike the ``/peer/inbox`` route
    there is no calling session for the server to recognise. This footer used to
    advertise the same key with no identity at all — which 500'd before Part 24
    registered the process and would have failed 'missing_argument' after. The
    shell expands ``$AGENT_SESSION_ID`` from the launcher's own export, so the
    line stays copy-runnable as printed.
    """
    surfaced = lines[:WAKE_MAX_SURFACED_LINES]
    overflow = len(lines) - len(surfaced)
    header = (
        f"SOLET WAKE — {target.solet_name} watch delivered "
        f"{len(lines)} new event(s) for role '{target.role}':"
    )
    body = list(surfaced)
    if overflow > 0:
        body.append(f"(+{overflow} more line(s) in {target.spool})")
    footer = (
        "Act on these now — they are durably persisted/transport-delivered, "
        "not proof that any model consumed them. Durable copies: "
        f"`{target.solet_name} call "
        "plugin::agent_messaging_plugin::peer_inbox "
        '\'{"agent_session_id": "\'"$AGENT_SESSION_ID"\'"}\'`. '
        "Do not reply with bare acknowledgements."
    )
    return "\n".join([header, *body, footer])
