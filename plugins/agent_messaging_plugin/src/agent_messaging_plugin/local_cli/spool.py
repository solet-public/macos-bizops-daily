"""Shared watch/wake session identity and spool-file mechanics (no MCP).

The `watch` command (delivery half) and the `wake` command (turn-injection
half) coordinate through a per-session spool file: watch appends one JSON
line per message-bearing delivery; wake blocks until the spool grows past
its consumed offset. Both derive the SAME per-session identity from the
launcher-exported environment, so the pairing needs no flags and no ambient
configuration.

Appends and reads are serialized with ``flock`` so wake's
truncate-when-fully-consumed can never race an in-flight append.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
from typing import Final

from ananta.core.runtime.port_manager import get_runtime_dir

from ..env_contract import AGENT_SESSION_ID_ENV, AGENT_SESSION_LABEL_ENV
from ..models import WATCH_AGENT_INSTANCE_PREFIX

# Launcher-exported per-session identity (see hydration TEMPLATE_VARS.md —
# these names are part of the seed contract; env_contract.py is the single
# source of truth for the family).
WATCH_SESSION_LABEL_ENV: Final[str] = AGENT_SESSION_LABEL_ENV
WATCH_SESSION_ID_ENV: Final[str] = AGENT_SESSION_ID_ENV

_DIGEST_LENGTH: Final[int] = 24


def watch_instance_digest(agent_session_id: str) -> str:
    """Deterministic per-session digest shared by watch identity and spool path.

    Keyed on the launcher's stable ``ases-...`` session id so a reconnecting
    watcher REPLACES its binding (never mints a sibling) and the wake hook
    finds the same spool without any handshake.
    """
    return hashlib.sha256(
        agent_session_id.encode("utf-8"),
    ).hexdigest()[:_DIGEST_LENGTH]


def resolve_watch_instance_id(
    agent_session_id: str,
    agent_instance_id: str | None,
) -> str:
    """Resolve the shared watch/wake spool identity (managed id wins).

    Managed launchers provide the ledger's concrete ``AGENT_INSTANCE_ID``.
    Bare watches have no ledger row, so they retain the deterministic derived
    identity that lets their watch and wake halves meet without a handshake.
    """
    return (agent_instance_id or "").strip() or (
        f"{WATCH_AGENT_INSTANCE_PREFIX}{watch_instance_digest(agent_session_id)}"
    )


def default_spool_path(solet_name: str, agent_instance_id: str) -> Path:
    """The per-session spool file, colocated with the bridge port files."""
    runtime_dir = get_runtime_dir(solet_name)
    return runtime_dir / f"{solet_name}.{agent_instance_id}.spool"


def watch_pairing_path(solet_name: str, agent_instance_id: str) -> Path:
    """Sidecar naming the spool the WATCHER actually chose, for this session.

    Census D4: ``watch`` accepts ``--spool`` / ``--no-spool``, so the delivery
    half can tee somewhere the wake half never looks — and the wake half would
    then block out its whole ``--max-wait`` (≈23.9 h) on a file nothing writes,
    reporting nothing. Deriving the same DEFAULT path in both halves proves only
    that they agree when both are on defaults; it says nothing about the pair
    that is actually configured.

    This sidecar makes the watcher the authority. Its own location is derived
    from session identity alone — the one thing both halves always know without
    a handshake — so ``wake`` can find it no matter where the spool went.
    """
    runtime_dir = get_runtime_dir(solet_name)
    return runtime_dir / f"{solet_name}.{agent_instance_id}.watchpair"


def watch_singleton_lock_path(solet_name: str, agent_instance_id: str) -> Path:
    """Sidecar the WATCHER flocks to stay a per-session singleton (W1, §34.3).

    Derived from session identity, deliberately NOT from the spool path. Two
    watchers under one ``$AGENT_SESSION_ID`` already share an instance id and a
    spool, and nothing downstream can tell them apart — so the invariant being
    enforced is one watcher per SESSION. Keying the lock on the spool would let
    ``--spool /elsewhere`` or ``--no-spool`` arm a second watcher for the same
    session and defeat the singleton at exactly the moment it matters.

    The kernel releases flocks on process death, so this can never go stale and
    needs no cleanup path.
    """
    runtime_dir = get_runtime_dir(solet_name)
    return runtime_dir / f"{solet_name}.{agent_instance_id}.watch.lock"


def watch_marks_path(solet_name: str, agent_instance_id: str) -> Path:
    """Sidecar holding this SESSION's inbox high-water marks (census D1).

    Derived from session identity, deliberately NOT from the spool path — the
    same reasoning as the singleton lock above. Marks describe what this
    session has already been shown; ``--spool /elsewhere`` changes where lines
    are teed, not what was seen, so keying on the spool would replay the whole
    backlog every time the tee moved.

    Session identity is also what makes the marks survive what they must: a
    bridge rotation, a blue-green swap, and any number of re-arms all keep the
    same ``agi-watch-<digest(AGENT_SESSION_ID)>``, so a re-arm resumes rather
    than replays. A genuinely NEW session gets a new digest and therefore no
    marks, which is the intended "seed to newest and spool nothing" case.
    """
    runtime_dir = get_runtime_dir(solet_name)
    return runtime_dir / f"{solet_name}.{agent_instance_id}.marks"


def write_watch_marks(marks: Path, *, instance_after: str, role_high_water: str) -> None:
    """Persist both high-water marks; empty string means "no mark yet"."""
    payload = json.dumps(
        {"instance_after": instance_after, "role_high_water": role_high_water},
    )
    marks.write_text(payload + "\n", encoding="utf-8")


def read_watch_marks(marks: Path) -> tuple[str, str]:
    """Read ``(instance_after, role_high_water)``; ``("", "")`` when absent.

    A missing, unreadable, or malformed sidecar reads as NO MARKS rather than
    raising. That is the deliberate choice: the caller's no-marks branch seeds
    to newest and spools nothing, so a corrupt sidecar costs a session its
    automatic wake for already-arrived mail — which the durable inbox still
    holds and ``peer_inbox`` can still pull — instead of replaying a backlog as
    a wake storm. Loud would be worse here: there is nothing for an operator to
    fix, and the recovery path is a documented one-line command.
    """
    if not marks.is_file():
        return "", ""
    try:
        parsed = json.loads(marks.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    if not isinstance(parsed, dict):
        return "", ""
    instance_after = parsed.get("instance_after")
    role_high_water = parsed.get("role_high_water")
    return (
        instance_after if isinstance(instance_after, str) else "",
        role_high_water if isinstance(role_high_water, str) else "",
    )


def write_watch_pairing(pairing: Path, spool: Path | None) -> None:
    """Record the watcher's spool choice; ``None`` means the tee is disabled."""
    payload = json.dumps({"spool": None if spool is None else str(spool)})
    pairing.write_text(payload + "\n", encoding="utf-8")


def read_watch_pairing(pairing: Path) -> tuple[bool, Path | None]:
    """Read the watcher's spool choice as ``(found, spool)``.

    ``(False, None)`` — no watcher has published a choice (no sidecar, or an
    unreadable/malformed one): the caller falls back to the derived default,
    which is the pre-D4 behaviour and correct when no watcher ever armed.
    ``(True, None)`` — a watcher armed with the tee DISABLED, which is the case
    the wake half must report rather than block on.
    """
    if not pairing.is_file():
        return False, None
    try:
        parsed = json.loads(pairing.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, None
    if not isinstance(parsed, dict) or "spool" not in parsed:
        return False, None
    raw = parsed["spool"]
    if raw is None:
        return True, None
    return (True, Path(raw)) if isinstance(raw, str) and raw else (False, None)


def spool_offset_path(spool: Path) -> Path:
    """Sidecar recording the byte offset the wake hook has consumed."""
    return spool.with_name(spool.name + ".offset")


def spool_lock_path(spool: Path) -> Path:
    """Sidecar the wake hook flocks to stay a per-session singleton."""
    return spool.with_name(spool.name + ".lock")


def spool_append(spool: Path, line: str) -> None:
    """Append one delivery line under an exclusive lock.

    Opened per append (deliveries are rare) so wake's locked
    truncate-after-full-consumption always operates on a settled file.
    """
    with spool.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line + "\n")
        handle.flush()
