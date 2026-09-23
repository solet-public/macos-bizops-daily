"""Durable, additive crash-consistency record for the post-cutover finisher (B2).

After a cutover's irreversible symlink swap the prior (now-draining) colour must
be SIGTERM'd + unregistered. The normal path enqueues a ``complete_swap`` action
for the NEW active colour's poller to run. But if that enqueue throws AFTER the
irreversible cutover — a StateService/session-row failure (observed this
campaign) — the prior is left alive-but-inactive with NO durable intent and NO
reconcile path: ``ReleaseManager.reconcile`` repairs the symlink ledger but
cannot infer/clean a leftover prior process or its router binding. That is an
orphan.

This module persists a tiny durable record of the prior colour so a PERIODIC
backstop on the NEW active colour (``heartbeat_lifecycle``'s steady-state loop)
can finish the cleanup idempotently even when the enqueue failed or the
enqueuing process crashed right after the cutover.

**ADDITIVE — it never touches the release ledger.** This is a SEPARATE file from
the :class:`ReleaseManager` symlink ledger; it neither reads nor mutates
``current`` / ``previous``. Writing or clearing it cannot affect the durable
release pointers — so adding it is safe on a live solet.

Durability mirrors the ledger's pattern: write to a temp sibling, ``fsync`` the
file, ``os.replace`` (atomic rename), then ``fsync`` the directory so the rename
survives a crash. Reads tolerate an absent or corrupt file by returning
``None`` (a missing/garbled backstop record must never crash the heartbeat loop;
the worst case is the action-driven normal path still runs, or the next swap
overwrites it).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Final

from macos_self_deployment_plugin.constants import (
    FINISHER_ESCALATION_NEEDS_INTERVENTION,
    FINISHER_ESCALATION_SIGKILL_AFTER_GRACE,
)

PENDING_FINISHER_SUFFIX: Final[str] = ".pending_finisher.json"
_TMP_SUFFIX: Final[str] = ".tmp"

_KEY_PRIOR_PID: Final[str] = "prior_pid"
_KEY_PRIOR_INSTANCE_ID: Final[str] = "prior_instance_id"
_KEY_PRIOR_COLOR: Final[str] = "prior_color"
_KEY_CANDIDATE_RELEASE_ID: Final[str] = "candidate_release_id"
_KEY_PRIOR_START_TOKEN: Final[str] = "prior_start_token"
_KEY_ESCALATION: Final[str] = "escalation"

_ESCALATION_VALUES: Final[frozenset[str]] = frozenset(
    {FINISHER_ESCALATION_SIGKILL_AFTER_GRACE, FINISHER_ESCALATION_NEEDS_INTERVENTION},
)


@dataclass(frozen=True, slots=True)
class PendingFinisher:
    """The prior colour a post-cutover finisher still owes cleanup for.

    ``candidate_release_id`` is the release the cutover is swapping TO. The
    backstop refuses to act until ``ReleaseManager.current`` actually names it —
    so the record is INERT in the ``{record-write → durable swap}`` window and
    after an aborted/rolled-back swap (Codex round-2 B2·1: the record must not be
    actionable until the swap's durable condition is observable).

    ``prior_start_token`` is the prior process's start-time identity token
    (:func:`process_identity.start_token`), captured about our own pid at write
    time. The backstop SIGTERMs only when the live token still matches — the
    PID-reuse guard the router's pid-less bindings cannot provide (B2·3).
    ``None`` when the write-time probe could not run (the prior is then treated
    as un-verifiable and never signalled — fail-safe).

    ``escalation`` is what the finisher may do when the prior ignores SIGTERM
    past the grace window: ``FINISHER_ESCALATION_SIGKILL_AFTER_GRACE`` (the
    ordinary service-driven ladder) or ``FINISHER_ESCALATION_NEEDS_INTERVENTION``
    (a swap that entered through the reconciliation/``cutover_release``
    channel, where signal 9 is forbidden — iss_8d1ec833). The executor sets it
    from the swap's reconciliation provenance at write time, so the finisher
    — which runs in a DIFFERENT process (the new colour) and may be a
    different release — reads the policy from the record rather than
    re-deriving it.

    A record written by a release that predates this field carries no
    ``escalation`` key; it is read as ``sigkill_after_grace`` because that is
    the only ladder such a writer could have entered with. This is a
    cross-release data contract (the prior colour writes, the next colour
    reads), not a compatibility shim.
    """

    prior_pid: int
    prior_instance_id: str
    prior_color: str
    candidate_release_id: str
    prior_start_token: str | None
    escalation: str = FINISHER_ESCALATION_SIGKILL_AFTER_GRACE

    def __post_init__(self) -> None:
        if self.escalation not in _ESCALATION_VALUES:
            msg = (
                f"PendingFinisher.escalation must be one of "
                f"{sorted(_ESCALATION_VALUES)}, got {self.escalation!r}"
            )
            raise ValueError(msg)

    def to_dict(self) -> dict[str, object]:
        return {
            _KEY_PRIOR_PID: self.prior_pid,
            _KEY_PRIOR_INSTANCE_ID: self.prior_instance_id,
            _KEY_PRIOR_COLOR: self.prior_color,
            _KEY_CANDIDATE_RELEASE_ID: self.candidate_release_id,
            _KEY_PRIOR_START_TOKEN: self.prior_start_token,
            _KEY_ESCALATION: self.escalation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PendingFinisher:
        pid = data[_KEY_PRIOR_PID]
        instance_id = data[_KEY_PRIOR_INSTANCE_ID]
        color = data[_KEY_PRIOR_COLOR]
        candidate_release_id = data[_KEY_CANDIDATE_RELEASE_ID]
        start_token = data.get(_KEY_PRIOR_START_TOKEN)
        escalation = data.get(_KEY_ESCALATION, FINISHER_ESCALATION_SIGKILL_AFTER_GRACE)
        if (
            not isinstance(pid, int)
            or not isinstance(instance_id, str)
            or not isinstance(color, str)
            or not isinstance(candidate_release_id, str)
            or not (start_token is None or isinstance(start_token, str))
            or not isinstance(escalation, str)
            or escalation not in _ESCALATION_VALUES
        ):
            msg = f"malformed pending-finisher record: {data!r}"
            raise ValueError(msg)
        return cls(
            prior_pid=pid,
            prior_instance_id=instance_id,
            prior_color=color,
            candidate_release_id=candidate_release_id,
            prior_start_token=start_token,
            escalation=escalation,
        )


def pending_finisher_path(runtime_dir: Path, solet_name: str) -> Path:
    """The per-solet pending-finisher file under the runtime dir."""
    return runtime_dir / f"{solet_name}{PENDING_FINISHER_SUFFIX}"


def write_pending_finisher(path: Path, record: PendingFinisher) -> None:
    """Atomically persist ``record`` to ``path`` (temp → fsync → replace → fsync dir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + _TMP_SUFFIX)
    payload = json.dumps(record.to_dict())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_pending_finisher(path: Path) -> PendingFinisher | None:
    """Return the persisted record, or ``None`` when absent or corrupt.

    Never raises on a missing/garbled file: the backstop reads this on every
    heartbeat tick and a transient/partial write must not crash the loop.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return PendingFinisher.from_dict(data)
    except (ValueError, KeyError):
        return None


def clear_pending_finisher(path: Path) -> None:
    """Remove the record (idempotent — a no-op when already absent)."""
    path.unlink(missing_ok=True)


__all__ = [
    "PENDING_FINISHER_SUFFIX",
    "PendingFinisher",
    "clear_pending_finisher",
    "pending_finisher_path",
    "read_pending_finisher",
    "write_pending_finisher",
]
