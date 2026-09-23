"""Materialized-release management for true local blue-green code rollback.

This module is the Phase-1 standalone component of the local
true-blue-green design
(``workbench/2026-06-27_true_local_blue_green_materialized_artifacts_design.md``,
Option A + all §6 recommendations, operator-approved). It implements the
Capistrano-style symlinked-release pattern (§4.1): every deploy
materializes an *immutable* release directory under
``~/.ananta/releases/<name>/``, an atomic ``current`` symlink names the
active release, a ``previous`` symlink names the rollback target, and a
crash-consistent ``state.json`` ledger lets startup reconcile a coherent
pair after a mid-cutover crash.

Four collaborating classes, split by responsibility so no single class
becomes a god class:

- :class:`ReleaseLedger` — crash-consistent reader/writer for the
  ``state.json`` deployment ledger (atomic replace + ``fsync`` of file
  and directory, §4.6).
- :class:`ReleaseSymlinks` — the ``current``/``previous`` symlink pair:
  atomic relative-target repointing + basename readback (§4.2), and the
  power-loss durability coupling to ``ReleaseLedger``.
- :class:`ReleaseGc` — tail-deletion of old releases subject to §4.6
  GC-safety (never deletes a symlinked or in-progress release).
- :class:`ReleaseBuilder` — materializes one immutable release:
  ``cp -c`` CoW clone of the working tree (§4.4), ``.pth`` re-point +
  target validation (§4.7), ``VERSION``, atomic finalize.
- :class:`ReleaseManager` — the lifecycle coordinator and the public
  surface Phase-2 integration consumes. Delegates building to
  :class:`ReleaseBuilder`, ledger I/O to :class:`ReleaseLedger`, the
  symlink pair to :class:`ReleaseSymlinks`, and GC to :class:`ReleaseGc`.

Responsibility boundary (load-bearing): none of these touch a database,
hold service injection, or know anything about the router, drain windows,
or process spawning. Per design §4.8 this component "lives plugin-side,
no DB access." Phase-2 integration (NOT this file) wires
``build_candidate`` / ``cutover`` / ``rollback`` into
``SwapOrchestrator`` + the autostart plist + ``SOLET_RELEASE_ID``
env. This lands standalone, tested-but-unwired; Phase 2 is its named
consumer.

Public surface (the four §4.8 verbs Phase 2 calls verbatim on
:class:`ReleaseManager`, plus ``reconcile`` which §4.6 mandates as a
startup step):

- ``build_candidate() -> CandidatePaths`` — CoW-clone the working tree
  into a fresh immutable release.
- ``cutover(candidate) -> SwapResult`` — crash-consistent ledger +
  ``previous``/``current`` symlink swap (§4.6 ordering).
- ``rollback() -> SwapResult`` — durable rollback: swap ``current`` and
  ``previous`` so a cold boot launches the prior release (§4.5).
- ``gc(keep=...) -> GcResult`` — keep the last K releases, never
  deleting one named by ``current``/``previous`` or an in-progress
  ledger row (§4.6 GC-safety).
- ``reconcile() -> ReconcileResult`` — startup reconcile after an
  interrupted cutover (§4.6). Additive to the four verbatim verbs.

Public failure contract (F1, Phase-2 adversarial review): every
mutating public method (``cutover``, ``rollback``, ``reconcile``,
``gc``) raises ONLY :class:`ReleaseManagerError` on failure — never a
raw ``OSError`` / ``json.JSONDecodeError`` (a ``ValueError``) /
``KeyError`` from ledger I/O or filesystem ops — chaining the original
as ``__cause__``. This lets the Phase-2 orchestrator's
``except ReleaseManagerError`` compensation fire on any failure. A
structurally-wrong ``state.json`` (a JSON array/scalar rather than an
object) is rejected as a ``ValueError`` inside :meth:`ReleaseLedger.read`
so it cannot escape as a raw ``AttributeError`` — the contract holds for
the parse-shape vector, not just torn-JSON.

Crash-consistency invariant (the property that makes ``reconcile``
correct, per the Architect F1 ruling): a surviving ``in_progress`` row
always records the **terminal target** a swap was driving toward —
``{current: new_rel, previous: old_rel}`` — and ``reconcile`` simply
drives both symlinks to it and clears the row, direction-agnostic:

- A forward ``PHASE_CUTOVER`` intent (interrupted by process death
  mid-swap, where the non-``OSError`` interruption escapes uncaught)
  targets the candidate. §4.7 places the symlink swap *after* a
  successful router ``activate``, so the candidate is already the live
  serving color and driving forward keeps cold-boot consistent.
- A ``PHASE_COMPENSATE`` intent (written by :meth:`_compensate_failed_swap`
  *before* it restores symlinks, when an in-process catchable ``OSError``
  / ``ValueError`` / ``KeyError`` fails a swap) targets the PRIOR pair, so
  a death mid-compensation drives BACK to prior — not forward to the
  candidate. Encoding the direction in the intent is what lets one
  reconcile path serve both cases with no extra branch.

Both ``new_rel`` and ``old_rel`` may be ``None`` (first-deploy
boundary); a ``None`` terminal is the empty state. The only residual is
a *persistent* ledger-write failure that defeats even the compensate
intent write — the original forward intent then survives and reconcile
forward-completes to the router-validated candidate, which is benign
(see :meth:`_compensate_failed_swap`).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from macos_self_deployment_plugin.constants import PLUGIN_NAME
from macos_self_deployment_plugin.release_code_collection import (
    CollectionPlan,
    ReleaseCodeCollectionError,
    ReleaseCodeCollector,
    write_selected_file_manifest,
)
from macos_self_deployment_plugin.venv_interpreter_pin import (
    VenvInterpreterPinError,
    pin_and_report,
)

# --- Layout tokens (design §4.2). Kept local to this module so the
# component stays self-contained; no foreign-file edits. -------------------
RELEASES_ROOT_DEFAULT: Final[str] = "~/.ananta/releases"
RELEASE_ID_PREFIX: Final[str] = "rel-"
STAGING_SUFFIX: Final[str] = ".incoming"
CODE_DIRNAME: Final[str] = "code"
VENV_DIRNAME: Final[str] = "venv"
# The interpreter inside a materialized release's venv: ``venv/bin/python3``.
VENV_BIN_DIRNAME: Final[str] = "bin"
VENV_PYTHON_BASENAME: Final[str] = "python3"
VERSION_FILENAME: Final[str] = "VERSION"
SELECTED_FILE_MANIFEST_FILENAME: Final[str] = "SELECTED_FILES.json"

#: Why ``VERSION.reconciliation_provenance`` exists and why it is written even
#: when empty. A seed-materialized target has no git identity to appeal to, so
#: the approved reconciliation's fingerprint IS the provenance for its bytes;
#: without it an attestation can say WHAT is served but not WHICH approved act
#: put it there. The field is ``null`` on every ordinary deploy rather than
#: absent, so "built by a reconciliation" stays a positive, readable claim
#: instead of an inference from absence — the same rule ``tree_state`` follows.
RECONCILIATION_PROVENANCE_NOTE: Final[str] = (
    "reconciliation_provenance is null on ordinary deploys and carries "
    "{reconciliation_id, source_surface_sha256, release_surface_sha256} on "
    "releases built by an approved reconciliation cutover."
)
STATE_FILENAME: Final[str] = "state.json"
CURRENT_LINK_NAME: Final[str] = "current"
PREVIOUS_LINK_NAME: Final[str] = "previous"

# Source-tree subdirectories that make up the first-party ``code/`` clone
# (design §4.2: ``code/`` = ``ananta/`` + ``plugins/`` + ``solet_cli/`` +
# ``solet_setup_contracts/``, NOT ``.git``). ``solet_cli`` and
# ``solet_setup_contracts`` ship because each is an ananta runtime dependency:
# session_ledger_service/selected_sources.py loads manager transactions as
# consent proof and imports setup contracts. A release without either fails the
# L1.1 fresh-source import probe — the same root cause the born-clone gate's
# layer-0 installs fixed on the clone path.
#
# ONE constant, deliberately: it is the clone loop's path list AND the
# dirty-tree gate's porcelain scope (Architect's dirty-tree ruling §2). Two
# separate lists would mean that the day someone adds a fourth cloned subtree
# the gate silently under-covers it — the same failure shape as a gate
# registration that lands without its file. Both readers dereference this
# module global at call time, so widening it widens both at once.
CODE_SUBTREES: Final[tuple[str, ...]] = (
    "ananta", "plugins", "solet_cli", "solet_setup_contracts",
)
VENV_SUBTREE: Final[str] = ".venv"

# ``VERSION.tree_state`` tokens (dirty-tree ruling §3). The attestation is
# POSITIVE IN BOTH DIRECTIONS: a clean build still writes the field, because
# the one inference it must never support is "field absent ⇒ tree was clean".
# Absence means only "this release predates the gate".
#
# UNKNOWN is the third state the ruling's table did not enumerate (it assumed
# a git checkout): git could not answer at all — not a repo, git missing, or
# the call timed out. It does NOT refuse the build (a hydrated seed clone may
# legitimately carry no ``.git``, and refusing there would brick install-time
# release-0 seeding), but it is never reported as clean and is logged loudly.
TREE_STATE_CLEAN: Final[str] = "clean"
TREE_STATE_DIRTY: Final[str] = "dirty"
TREE_STATE_UNKNOWN: Final[str] = "unknown"

# Ledger ``phase`` tokens (design §4.6 ordering).
PHASE_CUTOVER: Final[str] = "cutover"
PHASE_DONE: Final[str] = "done"
# Compensation-intent phase (Architect F1 ruling): written by
# _compensate_failed_swap BEFORE restoring symlinks, so a death (or a
# persistent fault) mid-compensation leaves an in_progress row whose
# terminal target is the PRIOR pair — reconcile then drives BACK to prior
# (not forward to the candidate), using the same forward-completion path.
PHASE_COMPENSATE: Final[str] = "compensate"

# Swap-step labels passed to the optional ``ledger_write_hook`` test seam
# so a crash-consistency smoke can fail a specific ledger write within a
# cutover/rollback — before the symlink swap (BEGIN) or after both
# symlinks (DONE / CLEAR). Production leaves the hook ``None``.
LEDGER_STEP_BEGIN: Final[str] = "begin"
LEDGER_STEP_DONE: Final[str] = "done"
LEDGER_STEP_CLEAR: Final[str] = "clear"
LEDGER_STEP_RECONCILE: Final[str] = "reconcile"
LEDGER_STEP_COMPENSATE_INTENT: Final[str] = "compensate_intent"
LEDGER_STEP_COMPENSATE_CLEAR: Final[str] = "compensate_clear"

# ``reconcile`` outcome tokens. ``forward_completed`` and ``compensated``
# are direction-distinct: the recovery drove FORWARD to the candidate (an
# interrupted cutover) vs BACK to the prior pair (an interrupted
# compensation). A ReconcileResult consumer can tell a recovery-forward
# from a rollback-back without re-reading the (now-cleared) ledger.
RECONCILE_NOOP: Final[str] = "noop"
RECONCILE_FORWARD_COMPLETED: Final[str] = "forward_completed"
RECONCILE_COMPENSATED: Final[str] = "compensated"
RECONCILE_ABANDONED: Final[str] = "abandoned"

# Tooling knobs.
CP_BINARY_DEFAULT: Final[str] = "/bin/cp"
CLONE_TIMEOUT_SECONDS_DEFAULT: Final[float] = 300.0
GIT_TIMEOUT_SECONDS: Final[float] = 10.0
GIT_SHA_UNKNOWN: Final[str] = "nogit"
DEFAULT_KEEP_RELEASES: Final[int] = 3


class ReleaseManagerError(RuntimeError):
    """A release build, cutover, rollback, or reconcile operation failed."""


@dataclass(frozen=True, slots=True)
class CandidatePaths:
    """Paths to a finalized-but-not-yet-active release.

    Returned by :meth:`ReleaseManager.build_candidate`; passed verbatim
    to :meth:`ReleaseManager.cutover` by the Phase-2 consumer.

    ``missing_pth_targets`` is the list of re-pointed ``.pth`` entries
    whose target does not resolve on disk (design §4.7 / §8.6 — e.g. the
    stale ``local_blue_green_deployment_plugin`` editable install).
    Empty in the healthy case; non-empty surfaces a stale editable
    install without silently baking it into the release.

    ``schema_snapshot`` is whatever the optional ``schema_snapshot_fn``
    returned at build time (also persisted in ``VERSION``); ``None`` when
    no producer was supplied. It is the NEW side of the Phase-2 preflight
    DDL-free gate's diff (the OLD side is
    :meth:`ReleaseManager.current_schema_snapshot`). ReleaseManager
    stores it verbatim and never interprets its shape.

    ``manifest_plugins`` is the profile plugin SET that governed this release at
    ITS OWN build time, captured here because the live manifest is overwritten
    by the next deploy before the §3 derive path ever reads it (BLG-02). ``None``
    on any release built before this capture landed — absent, not empty, and the
    two must not be conflated: an empty tuple would claim "this release had no
    plugins", while ``None`` correctly says "this release never recorded it".

    ``manifest_plugins_error`` carries a REJECTED capture without raising, so a
    garbage schema-gate field cannot make an otherwise-bootable release
    un-rollback-able. It is set only when ``VERSION`` held a present-but-invalid
    value; the §3 derive refuses on it at the point of use, and every other
    consumer — rollback included — ignores it.
    """

    release_id: str
    release_dir: Path
    code_root: Path
    venv_python: Path
    version_file: Path
    missing_pth_targets: tuple[str, ...]
    schema_snapshot: dict[str, object] | None
    manifest_plugins: tuple[str, ...] | None = None
    manifest_plugins_error: str | None = None


@dataclass(frozen=True, slots=True)
class SwapResult:
    """Outcome of a ``cutover`` / ``rollback`` symlink swap."""

    current: str
    previous: str | None


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Outcome of a startup :meth:`ReleaseManager.reconcile`."""

    action: str
    current: str | None
    previous: str | None


@dataclass(frozen=True, slots=True)
class GcResult:
    """Releases deleted vs retained by :meth:`ReleaseManager.gc`."""

    deleted: tuple[str, ...]
    retained: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _InProgress:
    """The ledger's in-flight-swap row (design §4.6 + Architect F1 ruling).

    ``new_rel`` is the release the swap drives ``current`` to; ``old_rel``
    is the release it drives ``previous`` to. For a forward cutover these
    are (candidate, prior_current); for a ``PHASE_COMPENSATE`` intent they
    are the PRIOR pair (prior_current, prior_previous), so reconcile drives
    BACK. Both are ``str | None`` — ``new_rel`` is ``None`` for a
    first-deploy compensation (no prior current), which reconcile drives
    to the empty state (``current`` unlinked) rather than blowing up.
    """

    old_rel: str | None
    new_rel: str | None
    phase: str


@dataclass(frozen=True, slots=True)
class _Ledger:
    """The ``state.json`` deployment ledger (design §4.6)."""

    current: str | None
    previous: str | None
    in_progress: _InProgress | None


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _default_git_sha(source_root: Path) -> str:
    """Read-only ``git rev-parse --short HEAD`` against ``source_root``.

    Read-only git is permitted for any session under the Git-Controller
    policy. Returns :data:`GIT_SHA_UNKNOWN` (logged by the caller) when
    the source tree is not a git repo or git is unavailable — the
    release id stays unique via its UTC timestamp and the
    build-fails-if-dir-exists guard.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return GIT_SHA_UNKNOWN
    if result.returncode != 0:
        return GIT_SHA_UNKNOWN
    return result.stdout.strip() or GIT_SHA_UNKNOWN


def _default_dirty_paths(
    source_root: Path, subtrees: tuple[str, ...]
) -> tuple[str, ...] | None:
    """Read-only ``git status --porcelain`` SCOPED to the shipped subtrees.

    The dirty-tree gate's measurement (ruling §1). Returns the porcelain
    lines — modified, staged, deleted, and untracked-unignored — or an
    empty tuple when the shipped subtrees are clean.

    Returns ``None`` when git cannot answer AT ALL (not a repo, git
    unavailable, timeout). ``None`` is deliberately distinct from ``()``:
    "could not measure" is not "measured clean", and collapsing the two is
    exactly the inference :data:`TREE_STATE_UNKNOWN` exists to prevent.

    The scope is what makes refuse-by-default survivable: this is a shared
    checkout with a permanently dirty ``workbench/`` by design, and dirt
    outside the cloned subtrees never reaches the artifact. Pathspecs
    resolve against ``-C source_root``, so they mean the same directories
    the clone loop copies.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), "status", "--porcelain", "--", *subtrees],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return tuple(line for line in result.stdout.splitlines() if line.strip())


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    """``fsync`` a directory so a rename/replace is durable.

    Guarded per §4.6 ("where supported"): some filesystems reject a
    directory ``fsync``; durability of the file write is already ensured
    by the file-level ``fsync``.
    """
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class ReleaseLedger:
    """Crash-consistent reader/writer for the ``state.json`` ledger (§4.6).

    Each :meth:`write` writes a temp file, ``fsync``s it, atomically
    ``os.replace``s it over ``state.json``, then ``fsync``s the
    directory — so a crash never leaves a torn ledger.
    """

    def __init__(self, releases_root: Path) -> None:
        self._releases_root = releases_root

    @property
    def _state_path(self) -> Path:
        return self._releases_root / STATE_FILENAME

    def read(self) -> _Ledger:
        if not self._state_path.is_file():
            return _Ledger(current=None, previous=None, in_progress=None)
        raw = json.loads(self._state_path.read_text())
        if not isinstance(raw, dict):
            # A structurally-wrong ledger (a JSON array/scalar, not an
            # object) would make the ``.get`` calls below raise a raw
            # ``AttributeError`` that escapes _read_ledger_or_raise's
            # (OSError, ValueError, KeyError) net. Raise ValueError (in that
            # net) so the F1 contract — "ledger reads only ever surface as
            # ReleaseManagerError" — holds for the parse-shape vector too.
            raise ValueError(
                f"state.json is not a JSON object (got {type(raw).__name__})"
            )
        in_progress_raw = raw.get("in_progress")
        in_progress = (
            _InProgress(
                old_rel=in_progress_raw.get("old_rel"),
                new_rel=in_progress_raw["new_rel"],
                phase=in_progress_raw["phase"],
            )
            if isinstance(in_progress_raw, dict)
            else None
        )
        return _Ledger(
            current=raw.get("current"),
            previous=raw.get("previous"),
            in_progress=in_progress,
        )

    def write(self, ledger: _Ledger) -> None:
        self._releases_root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._to_dict(ledger), indent=2, sort_keys=True)
        tmp = self._state_path.with_name(f"{STATE_FILENAME}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(descriptor, payload.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(tmp, self._state_path)
        _fsync_dir(self._releases_root)

    @staticmethod
    def _to_dict(ledger: _Ledger) -> dict[str, object]:
        in_progress: dict[str, object] | None = None
        if ledger.in_progress is not None:
            in_progress = {
                "old_rel": ledger.in_progress.old_rel,
                "new_rel": ledger.in_progress.new_rel,
                "phase": ledger.in_progress.phase,
            }
        return {
            "current": ledger.current,
            "previous": ledger.previous,
            "in_progress": in_progress,
        }


def _read_ledger_or_raise(ledger: ReleaseLedger) -> _Ledger:
    """Read ``ledger``, converting a raw I/O / parse error to ReleaseManagerError.

    ``ValueError`` covers ``json.JSONDecodeError`` AND a structurally-wrong
    top-level shape (a JSON array/scalar, rejected in
    :meth:`ReleaseLedger.read`); ``KeyError`` covers a malformed
    ``in_progress`` row missing ``new_rel`` / ``phase``. Part of the §4.6
    public failure contract: callers never see a raw stdlib exception from a
    ledger read.
    """
    try:
        return ledger.read()
    except (OSError, ValueError, KeyError) as exc:
        raise ReleaseManagerError(f"ledger read failed: {exc}") from exc


def _read_version_or_raise(version_file: Path) -> dict[str, object]:
    """Read+parse a release ``VERSION`` file, converting raw I/O / parse /
    shape errors to ReleaseManagerError (the F1 contract, mirroring
    :func:`_read_ledger_or_raise`).

    A torn ``VERSION`` (``ValueError``), a structurally-wrong top level (a
    JSON array/scalar rather than an object — the same ``[]`` →
    ``AttributeError`` vector the ledger guards), or an unreadable file
    (``OSError``) all surface as ``ReleaseManagerError`` chaining the cause —
    so neither :meth:`ReleaseManager.candidate_for` (rehydrating a rollback
    target) nor :meth:`ReleaseManager.current_schema_snapshot` (the preflight
    OLD side) can leak a raw stdlib exception or — worse for the gate —
    silently treat a corrupt snapshot as absent. Callers that tolerate a
    MISSING ``VERSION`` (``current_schema_snapshot`` on first deploy) check
    ``is_file()`` first.
    """
    try:
        raw = json.loads(version_file.read_text())
        if not isinstance(raw, dict):
            raise ValueError(f"VERSION is not a JSON object: got {type(raw).__name__}")
    except (OSError, ValueError) as exc:
        raise ReleaseManagerError(
            f"VERSION read failed ({version_file}): {exc}"
        ) from exc
    return raw


def _read_manifest_plugins(
    payload: dict[str, object], release_id: str,
) -> tuple[tuple[str, ...] | None, str | None]:
    """Read a release's captured plugin set back, as ``(plugins, error)``.

    ABSENCE is the only tolerated degradation, and it is a real one: every
    release built before per-release capture landed has no ``manifest_plugins``
    key, and the §3 derive path falls back to the live manifest for those —
    exactly today's behaviour, false-refuse exposure included. That fallback is
    self-limiting; once every release in the chain postdates this, it stops
    firing.

    A PRESENT-but-malformed value is REPORTED, not raised. This function is on
    the rehydrate path, which ``rollback`` also walks — and ``manifest_plugins``
    is advisory input to the schema gate that says nothing about whether a
    release BOOTS. Raising here turned a garbage field into
    ``ROLLBACK_TARGET_UNBOOTABLE`` on a release whose ``code/`` and ``venv/``
    are perfectly intact, precisely when the fleet is already in trouble and
    reaching for its safety net. So the refusal moves to the point of USE
    (``swap_orchestrator._resolve_current_snapshot``), which is the only place
    the value is actually trusted.

    An EMPTY list is malformation, not an empty capture, and this is the
    important one. No writer can emit it: ``_capture_manifest_plugins`` returns
    ``None`` or a non-empty tuple, and ``load_manifest_plugin_set`` itself
    rejects an empty ``plugins`` list. Tolerating ``[]`` would hand the collector
    an empty allow-set, which discovers ZERO plugins (the unresolved-plugin
    check there is a non-fatal warning), satisfies the completeness assert
    vacuously (``set() - set()`` is empty), and yields an EMPTY old snapshot —
    and ``classify_snapshot_diff`` iterates the OLD side, so an empty old never
    enters the loop and reports ``is_additive`` unconditionally. That is the
    §3 gate failing OPEN on every dropped column. "Our writer would never emit
    that" is the wrong standard for a field we now read off an adopter's disk.
    """
    raw = payload.get("manifest_plugins")
    if raw is None:
        return None, None
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        return None, (
            f"release {release_id} has a malformed 'manifest_plugins' in VERSION "
            f"(expected a list of plugin names, got {type(raw).__name__})"
        )
    if not raw:
        return None, (
            f"release {release_id} has an EMPTY 'manifest_plugins' in VERSION — no "
            "writer can emit that, and an empty plugin set would collapse the §3 "
            "snapshot to nothing and pass the additive check vacuously"
        )
    return tuple(raw), None


class ReleaseSymlinks:
    """The ``current``/``previous`` release symlink pair (design §4.2).

    Atomic relative-target repointing (create-temp + ``os.replace``) and
    basename readback. Relative targets keep the releases directory
    relocatable.

    Power-loss durability of a repoint is NOT ``fsync``'d here — it rides
    on the *next* :meth:`ReleaseLedger.write`'s directory ``fsync``, which
    is safe only because the ledger and these symlinks share
    ``releases_root`` and every swap path issues a ledger write
    immediately after the repoint (so the directory entry is flushed
    before the ``in_progress`` marker is cleared). Moving the ledger to a
    *different* directory would silently break this coupling — add an
    explicit ``_fsync_dir`` on the link's parent here if that happens.
    The process-death threat model is moot (reconcile re-applies the swap
    idempotently); this note covers a true power loss.
    """

    def __init__(self, releases_root: Path) -> None:
        self._releases_root = releases_root

    @property
    def current(self) -> Path:
        return self._releases_root / CURRENT_LINK_NAME

    @property
    def previous(self) -> Path:
        return self._releases_root / PREVIOUS_LINK_NAME

    def point(self, link: Path, target_release_id: str) -> None:
        """Atomically point ``link`` at ``target_release_id`` (relative target).

        ``os.replace`` over an existing symlink is atomic; you cannot
        repoint a symlink in place, so a temp symlink is created in the
        same directory and renamed over the target.
        """
        tmp = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
        os.symlink(target_release_id, tmp)
        os.replace(tmp, link)

    def read(self, link: Path) -> str | None:
        """Return ``link``'s target ``rel-<id>`` basename, or ``None``."""
        if not link.is_symlink():
            return None
        return Path(os.readlink(link)).name

    def restore(self, link: Path, target: str | None) -> None:
        """Idempotently restore ``link`` to ``target`` (or unlink when ``None``)."""
        if target is not None:
            self.point(link, target)
        elif link.is_symlink():
            link.unlink()


class ReleaseGc:
    """Tail-deletion of old releases, subject to §4.6 GC-safety.

    Keeps the newest K finalized ``rel-<id>`` directories; never deletes
    one named by the ``current``/``previous`` symlinks or an in-progress
    ledger row. Filesystem enumeration + symlink reads convert errors to
    ``ReleaseManagerError`` (the Q4 public failure contract).
    """

    def __init__(
        self,
        *,
        releases_root: Path,
        ledger: ReleaseLedger,
        symlinks: ReleaseSymlinks,
        logger: logging.Logger,
    ) -> None:
        self._releases_root = releases_root
        self._ledger = ledger
        self._symlinks = symlinks
        self._logger = logger

    def collect(self, *, keep: int) -> GcResult:
        """Keep the newest ``keep`` finalized releases; delete the tail."""
        protected = self._protected_release_ids()
        releases = self._list_finalized_releases()
        deleted: list[str] = []
        retained: list[str] = []
        for index, release_id in enumerate(releases):
            if index < keep or release_id in protected:
                retained.append(release_id)
                continue
            try:
                shutil.rmtree(self._releases_root / release_id)
            except OSError as exc:
                raise ReleaseManagerError(
                    f"gc failed to delete {release_id}: {exc}"
                ) from exc
            deleted.append(release_id)
        if deleted:
            self._logger.info("gc deleted %d release(s): %s", len(deleted), deleted)
        return GcResult(deleted=tuple(deleted), retained=tuple(retained))

    def _protected_release_ids(self) -> set[str]:
        """The releases GC must never delete (ledger + live symlinks).

        Q4 (Architect): the raw ``_symlinks.read`` (``os.readlink``) is
        wrapped so a filesystem error surfaces as ``ReleaseManagerError``.
        """
        ledger = _read_ledger_or_raise(self._ledger)
        protected: set[str] = set()
        for value in (ledger.current, ledger.previous):
            if value is not None:
                protected.add(value)
        if ledger.in_progress is not None:
            for value in (ledger.in_progress.old_rel, ledger.in_progress.new_rel):
                if value is not None:
                    protected.add(value)
        try:
            for link in (self._symlinks.current, self._symlinks.previous):
                target = self._symlinks.read(link)
                if target is not None:
                    protected.add(target)
        except OSError as exc:
            raise ReleaseManagerError(
                f"gc protected-set symlink read failed: {exc}"
            ) from exc
        return protected

    def _list_finalized_releases(self) -> list[str]:
        """Finalized ``rel-<id>`` dirs, newest first (ids sort chronologically).

        Q4 (Architect): the raw directory enumeration (``iterdir`` /
        ``is_dir`` / ``is_file``) is wrapped so a filesystem error
        surfaces as ``ReleaseManagerError``.
        """
        try:
            if not self._releases_root.is_dir():
                return []
            finalized = [
                entry.name
                for entry in self._releases_root.iterdir()
                if entry.is_dir()
                and not entry.is_symlink()
                and entry.name.startswith(RELEASE_ID_PREFIX)
                and not entry.name.endswith(STAGING_SUFFIX)
                and (entry / VERSION_FILENAME).is_file()
            ]
        except OSError as exc:
            raise ReleaseManagerError(
                f"gc release enumeration failed: {exc}"
            ) from exc
        return sorted(finalized, reverse=True)


def _pin_staged_interpreter(venv_dir: Path, logger: logging.Logger) -> None:
    """Freeze the staged venv's base interpreter, in this module's error type.

    Module-level rather than a ``ReleaseBuilder`` method: it needs nothing from
    the builder's state, and the builder is already at the god-class gate's
    ceiling — a helper that only translates an exception type does not belong in
    that budget.
    """
    try:
        pin_and_report(venv_dir, logger)
    except VenvInterpreterPinError as exc:
        raise ReleaseManagerError(
            f"could not pin the staged venv's interpreter: {exc}",
        ) from exc


def _repoint_and_validate_pth(
    venv_root: Path,
    *,
    source_root: Path,
    final_code_root: Path,
    strict_pth_validation: bool,
    logger: logging.Logger,
    excluded_roots: tuple[str, ...],
) -> tuple[str, ...]:
    """Repoint editable paths while refusing references to excluded code."""
    source_prefix = f"{source_root}{os.sep}"
    final_prefix = f"{final_code_root}{os.sep}"
    staging_code_root = venv_root.parent / CODE_DIRNAME
    missing: list[str] = []
    for pth in sorted(venv_root.glob(f"lib/python*/site-packages/*{os.extsep}pth")):
        missing.extend(
            _rewrite_one_pth(
                pth, source_prefix, final_prefix, staging_code_root, excluded_roots
            )
        )
    _assert_no_residual_prefix(venv_root, source_prefix)
    if missing and strict_pth_validation:
        raise ReleaseManagerError(f".pth targets do not resolve: {missing}")
    for target in missing:
        logger.warning(".pth target does not resolve (stale editable install?): %s", target)
    return tuple(missing)


def _rewrite_one_pth(
    pth: Path,
    source_prefix: str,
    final_prefix: str,
    staging_code_root: Path,
    excluded_roots: tuple[str, ...],
) -> list[str]:
    """Repoint one editable path file and return non-excluded missing targets."""
    rewritten: list[str] = []
    missing: list[str] = []
    changed = False
    for line in pth.read_text().splitlines():
        if not line.startswith(source_prefix):
            rewritten.append(line)
            continue
        remainder = line[len(source_prefix):]
        if any(remainder == root or remainder.startswith(f"{root}{os.sep}") for root in excluded_roots):
            raise ReleaseManagerError(
                f".pth target points into excluded nested Python environment: {line}"
            )
        new_line = final_prefix + remainder
        rewritten.append(new_line)
        changed = True
        if not (staging_code_root / remainder).exists():
            missing.append(new_line)
    if changed:
        pth.write_text("\n".join(rewritten) + "\n")
    return missing


def _assert_no_residual_prefix(venv_root: Path, source_prefix: str) -> None:
    """Fail if a materialized venv would still import mutable source bytes."""
    residual = [
        str(pth)
        for pth in venv_root.glob(f"lib/python*/site-packages/*{os.extsep}pth")
        if source_prefix in pth.read_text()
    ]
    if residual:
        raise ReleaseManagerError(
            f"residual source-root references after .pth rewrite: {residual}"
        )


class ReleaseBuilder:
    """Materializes one immutable release from the working tree (§4.4 / §4.7).

    Builds into a ``.incoming`` staging dir and atomically renames it to
    the final ``rel-<id>/`` so a half-built release never appears under
    its finalized name. All dependencies are keyword-only and injectable
    for deterministic smokes.
    """

    def __init__(
        self,
        *,
        source_root: Path,
        releases_root: Path,
        cp_binary: str,
        clone_timeout_seconds: float,
        clock: Callable[[], datetime],
        git_sha_resolver: Callable[[Path], str],
        dirty_paths_resolver: Callable[[Path, tuple[str, ...]], tuple[str, ...] | None],
        strict_pth_validation: bool,
        logger: logging.Logger,
    ) -> None:
        self._source_root = source_root
        self._releases_root = releases_root
        self._cp_binary = cp_binary
        self._clone_timeout = clone_timeout_seconds
        self._clock = clock
        self._git_sha_resolver = git_sha_resolver
        self._dirty_paths_resolver = dirty_paths_resolver
        self._strict_pth_validation = strict_pth_validation
        self._logger = logger

    def build(
        self,
        *,
        manifest_etag: str = "",
        manifest_plugins: tuple[str, ...] | None = None,
        schema_snapshot_fn: Callable[[Path], dict[str, object]] | None = None,
        allow_dirty: bool = False,
        reconciliation_provenance: dict[str, str] | None = None,
    ) -> CandidatePaths:
        """CoW-clone the working tree into a fresh immutable release.

        Steps (§4.7): ``cp -c`` the :data:`CODE_SUBTREES` subtrees
        into ``code/`` and ``.venv`` into ``venv/``; re-point every
        ``.pth`` repo-root prefix at the final ``code/``; capture the
        optional schema snapshot; validate every ``.pth`` target
        resolves; write ``VERSION``; ``fsync`` + atomic rename.

        ``schema_snapshot_fn`` (the Phase-2 preflight seam, dependency-
        inverted so the platform ``collect_schemas`` import stays in the
        caller, NOT here) is invoked ONCE after ``code/`` is materialized,
        receiving the staging ``code/`` root (the physical location of the
        cloned :data:`CODE_SUBTREES` at that moment — the final path
        does not exist until the atomic rename). Its return value is
        stored verbatim in ``VERSION`` under ``schema_snapshot`` and on
        ``CandidatePaths``. A raise propagates and fails the build (a
        snapshot-less release would silently defeat the gate).

        ``manifest_plugins`` is the profile plugin set governing THIS build,
        supplied by the caller for the same dependency-inversion reason as
        ``schema_snapshot_fn`` (the ``ananta.core.plugins`` import stays in the
        caller). It is persisted verbatim into ``VERSION`` and onto
        ``CandidatePaths`` so a LATER deploy — which overwrites the live
        manifest before the §3 derive runs — cannot retroactively change what
        this release claims it was built with (BLG-02).

        ``allow_dirty`` is the ruled override for the dirty-tree gate: the
        artifact is built from the WORKING TREE but IDENTIFIED by HEAD, so
        by default a modification inside the cloned subtrees refuses rather
        than shipping unreviewed code under a clean SHA. ``True`` builds
        anyway and stamps the full porcelain list into ``VERSION`` at
        WARNING volume — loud, not convenient, and caller-supplied only
        (no environment-variable back door: a knob an env var passes
        invisibly is worse than no knob). Its intended caller is the
        operator-confirmation path.

        ``reconciliation_provenance`` is persisted verbatim into ``VERSION``;
        see :data:`RECONCILIATION_PROVENANCE_NOTE`.
        """
        # THE WRITE END of the same invariant the read side enforces. `()` is
        # not `None`, so an empty tuple would serialize as `[]` — the exact
        # value the reader classifies as malformation, permanently disqualifying
        # this release as a derive source with nothing saying so at build time.
        # Production cannot reach it (``_capture_manifest_plugins`` returns
        # ``None`` or non-empty), but ``build_candidate`` is public and this
        # parameter is caller-supplied. Refusing at BOTH ends is what stops an
        # invariant enforced on one side from rotting on the other.
        if manifest_plugins is not None and not manifest_plugins:
            msg = (
                "manifest_plugins must be a non-empty tuple or None — an empty capture "
                "would write [] into VERSION, which the §3 derive rejects as malformed. "
                "Pass None to mean 'ungated / not captured'."
            )
            raise ReleaseManagerError(msg)
        self._releases_root.mkdir(parents=True, exist_ok=True)
        tree_state, dirty_paths = self._attest_tree_state(allow_dirty=allow_dirty)
        collector = ReleaseCodeCollector(
            source_root=self._source_root,
            code_subtrees=CODE_SUBTREES,
            cp_binary=self._cp_binary,
            clone_timeout_seconds=self._clone_timeout,
        )
        try:
            collection_plan = collector.plan()
        except ReleaseCodeCollectionError as exc:
            raise ReleaseManagerError(f"release code collection refused: {exc}") from exc
        git_sha = self._resolve_git_sha()
        release_id = self._mint_release_id(git_sha)
        final_dir = self._releases_root / release_id
        if final_dir.exists():
            msg = f"release dir already exists, refusing to clobber: {final_dir}"
            raise ReleaseManagerError(msg)
        staging = self._releases_root / f"{release_id}{STAGING_SUFFIX}"
        if staging.exists():
            # Leftover from a prior aborted build (never finalized, never
            # symlinked, never in the ledger) — safe to clear.
            shutil.rmtree(staging)

        final_code_root = final_dir / CODE_DIRNAME
        self._build_into_staging(staging, collector, collection_plan)
        tree_state, dirty_paths = self._reattest_after_clone(
            staging, before=dirty_paths, before_state=tree_state, allow_dirty=allow_dirty
        )
        schema_snapshot = (
            schema_snapshot_fn(staging / CODE_DIRNAME)
            if schema_snapshot_fn is not None
            else None
        )
        missing = _repoint_and_validate_pth(
            staging / VENV_DIRNAME,
            source_root=self._source_root,
            final_code_root=final_code_root,
            strict_pth_validation=self._strict_pth_validation,
            logger=self._logger,
            excluded_roots=tuple(item.relative_path for item in collection_plan.exclusions),
        )
        try:
            collector.verify_source_identity(collection_plan)
            selected_file_manifest_sha256 = write_selected_file_manifest(
                staging / SELECTED_FILE_MANIFEST_FILENAME,
                collector.manifest_payload(collection_plan),
            )
            collector.verify_materialized_manifest(
                collection_plan,
                staging / CODE_DIRNAME,
                staging / SELECTED_FILE_MANIFEST_FILENAME,
            )
        except ReleaseCodeCollectionError as exc:
            raise ReleaseManagerError(f"release code collection verification refused: {exc}") from exc
        self._write_version(
            staging / VERSION_FILENAME,
            release_id=release_id,
            git_sha=git_sha,
            manifest_etag=manifest_etag,
            manifest_plugins=manifest_plugins,
            missing_pth=missing,
            schema_snapshot=schema_snapshot,
            tree_state=tree_state,
            dirty_paths=dirty_paths,
            reconciliation_provenance=reconciliation_provenance,
            selected_file_manifest_sha256=selected_file_manifest_sha256,
        )
        self._fsync_staging(staging)
        os.replace(staging, final_dir)
        _fsync_dir(self._releases_root)
        self._logger.info(
            "built release %s (git_sha=%s, missing_pth=%d, schema_snapshot=%s)",
            release_id, git_sha, len(missing),
            "present" if schema_snapshot is not None else "none",
        )
        return self._compose_candidate(
            release_id,
            final_dir,
            missing_pth_targets=missing,
            schema_snapshot=schema_snapshot,
            manifest_plugins=manifest_plugins,
            # A freshly-built release validated its capture above, so there is
            # nothing rejected to carry.
            manifest_plugins_error=None,
        )

    def rehydrate(self, release_id: str) -> CandidatePaths:
        """Reconstruct :class:`CandidatePaths` for an EXISTING release.

        The inverse of :meth:`build` for a release already materialized on
        disk: the caller (:meth:`ReleaseManager.candidate_for`) has already
        asserted finalization, so the layout exists; this reads
        ``missing_pth_targets`` + ``schema_snapshot`` back from the release's
        ``VERSION`` (via :func:`_read_version_or_raise`, so a torn/malformed
        VERSION surfaces as ReleaseManagerError) and composes the same paths
        ``build`` would have returned. Layout knowledge lives ONLY here.
        """
        release_dir = self._releases_root / release_id
        payload = _read_version_or_raise(release_dir / VERSION_FILENAME)
        raw_missing = payload.get("missing_pth_targets")
        missing = tuple(raw_missing) if isinstance(raw_missing, list) else ()
        snapshot = payload.get("schema_snapshot")
        manifest_plugins, manifest_plugins_error = _read_manifest_plugins(payload, release_id)
        return self._compose_candidate(
            release_id,
            release_dir,
            missing_pth_targets=missing,
            schema_snapshot=snapshot if isinstance(snapshot, dict) else None,
            manifest_plugins=manifest_plugins,
            manifest_plugins_error=manifest_plugins_error,
        )

    @staticmethod
    def _compose_candidate(
        release_id: str,
        release_dir: Path,
        *,
        missing_pth_targets: tuple[str, ...],
        schema_snapshot: dict[str, object] | None,
        manifest_plugins: tuple[str, ...] | None,
        # No default, deliberately: a future call site that forgets this would
        # otherwise construct a CandidatePaths silently CLAIMING no error.
        manifest_plugins_error: str | None,
    ) -> CandidatePaths:
        """Derive the release's path layout into a :class:`CandidatePaths`.

        The single owner of the code/venv/VERSION layout mapping, shared by
        :meth:`build` (with in-memory ``missing`` / ``schema_snapshot`` /
        ``manifest_plugins``) and :meth:`rehydrate` (reading them back from
        ``VERSION``) so the two can never drift.
        """
        return CandidatePaths(
            release_id=release_id,
            release_dir=release_dir,
            code_root=release_dir / CODE_DIRNAME,
            venv_python=release_dir / VENV_DIRNAME / VENV_BIN_DIRNAME / VENV_PYTHON_BASENAME,
            version_file=release_dir / VERSION_FILENAME,
            missing_pth_targets=missing_pth_targets,
            schema_snapshot=schema_snapshot,
            manifest_plugins=manifest_plugins,
            manifest_plugins_error=manifest_plugins_error,
        )

    def _build_into_staging(
        self,
        staging: Path,
        collector: ReleaseCodeCollector,
        collection_plan: CollectionPlan,
    ) -> None:
        """CoW-clone the code subtrees + venv into the staging dir, then pin.

        The cloned venv inherits the checkout's FLOATING base-interpreter
        symlink, which Homebrew repoints on every patch upgrade. Pinning it here
        makes the interpreter a property of the release rather than of the
        host's current brew state.
        """
        code_root = staging / CODE_DIRNAME
        try:
            collector.materialize(collection_plan, code_root)
        except ReleaseCodeCollectionError as exc:
            raise ReleaseManagerError(f"release code materialization refused: {exc}") from exc
        self._clone_tree(self._source_root / VENV_SUBTREE, staging / VENV_DIRNAME)
        _pin_staged_interpreter(staging / VENV_DIRNAME, self._logger)


    def _clone_tree(self, src: Path, dst: Path) -> None:
        """``cp -cR src dst`` (APFS copy-on-write clone, design §4.4).

        No non-CoW fallback: a non-APFS volume fails the clone loudly
        rather than silently doubling disk consumption.
        """
        if not src.exists():
            raise ReleaseManagerError(f"clone source does not exist: {src}")
        try:
            result = subprocess.run(
                [self._cp_binary, "-cR", str(src), str(dst)],
                capture_output=True,
                text=True,
                check=False,
                timeout=self._clone_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseManagerError(f"cp -cR {src} -> {dst} failed: {exc}") from exc
        if result.returncode != 0:
            msg = (
                f"cp -cR {src} -> {dst} exited {result.returncode}: "
                f"{result.stderr.strip()}"
            )
            raise ReleaseManagerError(msg)

    def _write_version(
        self,
        path: Path,
        *,
        release_id: str,
        git_sha: str,
        manifest_etag: str,
        manifest_plugins: tuple[str, ...] | None,
        missing_pth: tuple[str, ...],
        schema_snapshot: dict[str, object] | None,
        tree_state: str,
        dirty_paths: tuple[str, ...],
        reconciliation_provenance: dict[str, str] | None,
        selected_file_manifest_sha256: str,
    ) -> None:
        payload = {
            "release_id": release_id,
            "git_sha": git_sha,
            "build_time_utc": self._clock().isoformat(),
            "source_root": str(self._source_root),
            "manifest_etag": manifest_etag,
            # The plugin set THIS release was built with, so the §3 derive path
            # can gate an old tree by its OWN manifest rather than by whatever
            # the next deploy wrote over the live one (BLG-02). ``null`` is
            # written explicitly when the caller supplied none, so a reader can
            # tell "not captured" from "captured as empty".
            "manifest_plugins": (
                sorted(manifest_plugins) if manifest_plugins is not None else None
            ),
            "missing_pth_targets": list(missing_pth),
            "schema_snapshot": schema_snapshot,
            # Written on EVERY build, clean included: absence must never be
            # readable as clean (dirty-tree ruling §3).
            "tree_state": tree_state,
            "dirty_paths": list(dirty_paths),
            # What the attestation actually covers. ``.venv`` is cloned but
            # gitignored, so porcelain cannot attest it (ruling §5); recording
            # the scope keeps "tree_state: clean" from being read as "artifact
            # fully attested".
            "tree_state_scope": list(CODE_SUBTREES),
            "selected_file_manifest": {
                "filename": SELECTED_FILE_MANIFEST_FILENAME,
                "sha256": selected_file_manifest_sha256,
            },
            # See RECONCILIATION_PROVENANCE_NOTE: null on ordinary deploys.
            "reconciliation_provenance": reconciliation_provenance,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def _fsync_staging(self, staging: Path) -> None:
        """``fsync`` the ``VERSION`` file + the staging dir before finalize."""
        for filename in (VERSION_FILENAME, SELECTED_FILE_MANIFEST_FILENAME):
            path = staging / filename
            if path.is_file():
                _fsync_file(path)
        _fsync_dir(staging)

    def _mint_release_id(self, git_sha: str) -> str:
        stamp = self._clock().strftime("%Y%m%dT%H%M%SZ")
        return f"{RELEASE_ID_PREFIX}{stamp}-{git_sha}"

    def _attest_tree_state(self, *, allow_dirty: bool) -> tuple[str, tuple[str, ...]]:
        """The dirty-tree gate: refuse by default on dirt the artifact SHIPS.

        Runs BEFORE anything is staged, so a refusal leaves no ``.incoming``
        dir and no finalized release behind.

        Scoped to :data:`CODE_SUBTREES`, dereferenced here at call time so
        the gate's population is the clone loop's population by construction
        rather than by a second list somebody has to remember to update.

        Returns the ``(tree_state, dirty_paths)`` pair recorded in
        ``VERSION``. Raises :class:`ReleaseManagerError` — naming the
        offending paths, since "the tree is dirty" without them is not
        actionable — when the shipped subtrees are dirty and the caller did
        not pass ``allow_dirty``.
        """
        subtrees = CODE_SUBTREES
        dirty = self._dirty_paths_resolver(self._source_root, subtrees)
        if dirty is None:
            self._logger.warning(
                "tree state UNATTESTABLE for %s (git could not report on %s); "
                "recording tree_state=%r — this release is NOT attested clean",
                self._source_root, ", ".join(subtrees), TREE_STATE_UNKNOWN,
            )
            return TREE_STATE_UNKNOWN, ()
        if not dirty:
            return TREE_STATE_CLEAN, ()
        listed = "; ".join(dirty)
        if not allow_dirty:
            raise ReleaseManagerError(
                f"refusing to build a release from a dirty working tree: "
                f"{len(dirty)} uncommitted path(s) under {', '.join(subtrees)} "
                f"would ship inside the artifact but be labelled with HEAD's sha "
                f"— {listed}. Commit them, or pass allow_dirty=True to build "
                f"anyway (the override is recorded in VERSION)."
            )
        self._logger.warning(
            "allow_dirty override: building a release from a DIRTY working tree; "
            "%d uncommitted path(s) ship under HEAD's sha — %s",
            len(dirty), listed,
        )
        return TREE_STATE_DIRTY, dirty

    def _reattest_after_clone(
        self,
        staging: Path,
        *,
        before: tuple[str, ...],
        before_state: str,
        allow_dirty: bool,
    ) -> tuple[str, tuple[str, ...]]:
        """Re-measure AFTER the clone and refuse if the tree moved underneath it.

        NOT in Architect's ruling table; added from the live 2026-08-01 20:41Z
        incident, where a lane kept editing while a release was being built and
        five shipped files ended up matching NEITHER HEAD NOR the worktree.

        The pre-clone gate cannot catch that on its own: ``cp -cR`` is not a
        snapshot, so a tree that measures clean at gate time can be edited
        while it is still being copied. The artifact then corresponds to no
        commit and no working tree, which is strictly worse than "dirty" —
        there is nothing to diff it against, so it cannot be reviewed at all.

        Comparing the porcelain population before and after closes that window
        to "changed during the clone ⇒ refused". It cannot cry wolf on the
        permanently-dirty ``workbench/``: the population is scoped to the
        shipped subtrees, so a build only fails here if the code actually
        being copied moved mid-copy.

        Skipped when the tree was UNATTESTABLE going in — there is no baseline
        to compare against, and that release is already labelled ``unknown``.

        Returns the ``(tree_state, dirty_paths)`` pair that ``VERSION`` must
        actually record. This is NOT the pre-clone pair on the override path:
        if the tree moved mid-clone under ``allow_dirty``, attesting the
        pre-clone measurement would stamp ``clean`` (or a short list) onto a
        torn artifact — reintroducing through the override exactly the
        inference ruling §3 forbids, and breaking §4's "the override stamps
        the FULL porcelain list".
        """
        if before_state == TREE_STATE_UNKNOWN:
            return before_state, before
        after = self._dirty_paths_resolver(self._source_root, CODE_SUBTREES)
        if after is None:
            # Measurable before, unmeasurable after: we cannot say the clone
            # captured a clean tree, so we must not claim it did.
            self._logger.warning(
                "tree state became UNATTESTABLE during the clone of %s; "
                "downgrading tree_state to %r rather than keeping %r",
                self._source_root, TREE_STATE_UNKNOWN, before_state,
            )
            return TREE_STATE_UNKNOWN, before
        if after == before:
            return before_state, before
        appeared = [line for line in after if line not in before]
        if allow_dirty:
            self._logger.warning(
                "tree changed DURING the clone (allow_dirty override): %s",
                "; ".join(appeared) or "population changed",
            )
            return TREE_STATE_DIRTY, after
        shutil.rmtree(staging, ignore_errors=True)
        raise ReleaseManagerError(
            f"working tree changed while the release was being cloned: "
            f"{'; '.join(appeared) or 'the dirty population changed'}. The "
            f"artifact would be a torn snapshot matching neither HEAD nor the "
            f"working tree, so it could not be reviewed against anything. "
            f"Re-run the build once the tree is quiet."
        )

    def _resolve_git_sha(self) -> str:
        sha = self._git_sha_resolver(self._source_root)
        if sha == GIT_SHA_UNKNOWN:
            self._logger.warning(
                "could not resolve git sha for %s; release id uses %r",
                self._source_root, GIT_SHA_UNKNOWN,
            )
        return sha


class ReleaseSwapper:
    """The crash-consistent ``current``/``previous`` swap transaction + recovery.

    Owns the single responsibility :class:`ReleaseManager` delegates the
    riskiest mechanics to: the ordered ledger-write → symlink-swap → ledger
    -write transaction shared by cutover/rollback (:meth:`perform_swap`), its
    best-effort compensation on an in-process failure
    (:meth:`_compensate_failed_swap`), and the startup
    :meth:`reconcile` that forward-completes or compensation-completes a
    swap interrupted by process death. Extracted from ReleaseManager so the
    coordinator stays a thin public surface (the F1 contract + crash-
    consistency invariant live here, where the swap actually happens).

    ``is_finalized`` is injected (ReleaseManager owns the finalization check,
    used by both the public verbs and reconcile's abandon path) so the
    swapper need not duplicate the layout knowledge. ``mid_swap_hook`` /
    ``ledger_write_hook`` are the test seams (production ``None``).
    """

    def __init__(
        self,
        *,
        symlinks: ReleaseSymlinks,
        ledger: ReleaseLedger,
        is_finalized: Callable[[str], bool],
        mid_swap_hook: Callable[[], None] | None,
        ledger_write_hook: Callable[[str], None] | None,
        logger: logging.Logger,
    ) -> None:
        self._symlinks = symlinks
        self._ledger = ledger
        self._is_finalized = is_finalized
        self._mid_swap_hook = mid_swap_hook
        self._ledger_write_hook = ledger_write_hook
        self._logger = logger

    def perform_swap(
        self,
        *,
        new_current: str,
        new_previous: str | None,
        prior_current: str | None,
        prior_previous: str | None,
    ) -> SwapResult:
        """Crash-consistent ledger + symlink swap shared by cutover/rollback.

        Wraps the FULL transaction (ledger ``phase=cutover`` write →
        symlink swaps → ``phase=done`` + clear writes) so a raw
        ``OSError`` / ``ValueError`` / ``KeyError`` from ANY ledger write
        or symlink op surfaces as ``ReleaseManagerError`` (chained) after
        a best-effort compensation back to the pre-swap state. A
        non-``OSError`` interruption (the process-death simulation / a
        real crash) escapes UNCAUGHT, deliberately leaving the
        ``in_progress=cutover`` row for :meth:`reconcile` to
        forward-complete.
        """
        try:
            self._ledger_write(
                _Ledger(
                    current=prior_current,
                    previous=prior_previous,
                    in_progress=_InProgress(
                        old_rel=new_previous, new_rel=new_current, phase=PHASE_CUTOVER,
                    ),
                ),
                step=LEDGER_STEP_BEGIN,
            )
            if new_previous is not None:
                self._symlinks.point(self._symlinks.previous, new_previous)
            if self._mid_swap_hook is not None:
                self._mid_swap_hook()
            self._symlinks.point(self._symlinks.current, new_current)
            self._ledger_write(
                _Ledger(
                    current=new_current,
                    previous=new_previous,
                    in_progress=_InProgress(
                        old_rel=new_previous, new_rel=new_current, phase=PHASE_DONE,
                    ),
                ),
                step=LEDGER_STEP_DONE,
            )
            self._ledger_write(
                _Ledger(current=new_current, previous=new_previous, in_progress=None),
                step=LEDGER_STEP_CLEAR,
            )
        except (OSError, ValueError, KeyError) as exc:
            # In-process, catchable failure (disk full, perms, a torn-ledger
            # parse). §4.7: a failed cutover leaves current/previous
            # unchanged — best-effort compensate back to the pre-swap state
            # (see _compensate_failed_swap for the residual limit), then
            # raise the ReleaseManagerError the orchestrator catches. (A
            # non-OSError interruption — process death — is NOT caught here:
            # it escapes, leaving the in_progress row for reconcile to
            # forward-complete, which is what the crash-consistency
            # invariant depends on.)
            self._compensate_failed_swap(prior_current, prior_previous)
            raise ReleaseManagerError(
                f"swap failed; compensated to pre-swap state: {exc}"
            ) from exc
        return SwapResult(current=new_current, previous=new_previous)

    def _compensate_failed_swap(
        self, prior_current: str | None, prior_previous: str | None
    ) -> None:
        """Best-effort rollback to the pre-swap state after a failed swap.

        Three best-effort steps, each logged-and-swallowed so the caller
        always raises the PRIMARY failure as ``ReleaseManagerError``
        (Architect F1 ruling):

        1. Write a ``PHASE_COMPENSATE`` intent whose terminal target is the
           PRIOR pair (``current=prior_current``, ``previous=prior_previous``)
           BEFORE touching the symlinks. If a death — or a persistent fault
           — interrupts the remaining steps, this surviving intent makes
           :meth:`reconcile` drive BACK to the prior pair. That closes the
           gap a binary ``in_progress`` could not: clear-first would strand
           at the candidate (reconcile no-ops on cleared state), a bare
           reorder would forward-complete to it — the intent encodes the
           *direction*, so reconcile is direction-correct with no new branch.
        2. Restore both symlinks to their prior targets.
        3. CLEAR ``in_progress`` last, but only after BOTH symlinks were
           restored successfully. A failed restore leaves the compensate
           intent intact for :meth:`reconcile` to finish driving back to the
           prior pair.

        Residual (BENIGN — Architect Q3 disposition): under a *persistent*
        ledger-write failure (disk full), step 1's intent write also fails,
        so the original forward ``in_progress=cutover`` row survives and
        reconcile forward-completes to the candidate. Harmless: §4.7
        guarantees the candidate was router-activated BEFORE ``cutover``
        ran, so reconcile leaves a coherent ``current=candidate,
        previous=prior`` ledger (rollback intact) and a cold start
        re-establishes routing.
        """
        try:
            self._ledger_write(
                _Ledger(
                    current=prior_current,
                    previous=prior_previous,
                    in_progress=_InProgress(
                        old_rel=prior_previous,
                        new_rel=prior_current,
                        phase=PHASE_COMPENSATE,
                    ),
                ),
                step=LEDGER_STEP_COMPENSATE_INTENT,
            )
        except (OSError, ValueError, KeyError) as exc:
            self._logger.error(
                "swap compensation: writing compensate-intent failed: %s", exc,
            )
        restore_failed = False
        for link, target in (
            (self._symlinks.current, prior_current),
            (self._symlinks.previous, prior_previous),
        ):
            try:
                self._symlinks.restore(link, target)
            except OSError as exc:
                restore_failed = True
                self._logger.error(
                    "swap compensation: restoring symlink %s failed: %s", link, exc,
                )
        if restore_failed:
            self._logger.error(
                "swap compensation: preserving compensate intent after incomplete "
                "symlink restoration",
            )
            return
        try:
            self._ledger_write(
                _Ledger(current=prior_current, previous=prior_previous, in_progress=None),
                step=LEDGER_STEP_COMPENSATE_CLEAR,
            )
        except (OSError, ValueError, KeyError) as exc:
            self._logger.error(
                "swap compensation: clearing in_progress failed: %s", exc,
            )

    def _ledger_write(self, ledger: _Ledger, *, step: str) -> None:
        """Write the ledger, firing the optional ``ledger_write_hook`` test seam.

        The hook (production: ``None``) lets a failure-contract smoke raise
        ``OSError`` at a chosen swap step. EVERY ledger write routes through
        here — including the compensation path's intent + clear writes
        (:meth:`_compensate_failed_swap`, labelled ``compensate_intent`` /
        ``compensate_clear``) — so a smoke can drive the on-disk state to any
        crash window. The step labels are distinct, so a fault injected at a
        *forward* step (``begin`` / ``done``) selectively triggers
        compensation WITHOUT also breaking it (the existing F1 contract
        smokes rely on exactly this); an organic production test faults
        ``compensate_clear`` specifically — leaving the intent on disk — to
        assert ``_compensate_failed_swap`` writes the correct field mapping
        (a transposition there reintroduces the Codex gap yet passes every
        reconcile-consumption test).
        """
        if self._ledger_write_hook is not None:
            self._ledger_write_hook(step)
        self._ledger.write(ledger)

    def reconcile(self) -> ReconcileResult:
        """Reconcile a coherent ``current``/``previous`` pair at startup.

        A surviving ``in_progress`` row records the terminal target a swap
        was driving toward; reconcile drives both symlinks to it and
        clears the row. A forward ``PHASE_CUTOVER`` intent targets the
        candidate (the live router decision — §4.7 swaps symlinks only
        *after* a successful ``activate``); a ``PHASE_COMPENSATE`` intent
        targets the PRIOR pair (a swap that failed mid-compensation drives
        BACK, not forward). A ``None`` terminal is the empty state
        (first-deploy compensation → ``current`` unlinked). Idempotent:
        re-running after a completed reconcile is a no-op.

        The drive is direction-agnostic but the returned ``action`` token is
        not: a ``PHASE_COMPENSATE`` recovery returns
        :data:`RECONCILE_COMPENSATED` (drove back), every other phase returns
        :data:`RECONCILE_FORWARD_COMPLETED` (drove forward) — so a
        ``ReconcileResult`` consumer can distinguish a recovery-forward from
        a rollback-back without re-reading the now-cleared ledger.
        """
        ledger = _read_ledger_or_raise(self._ledger)
        if ledger.in_progress is None:
            return ReconcileResult(
                action=RECONCILE_NOOP,
                current=ledger.current,
                previous=ledger.previous,
            )
        in_progress = ledger.in_progress
        # A ``None`` terminal target (first-deploy compensation) is the empty
        # state — always valid, never "unfinalized". Only a non-None target
        # that is missing/corrupt is abandoned.
        if in_progress.new_rel is not None and not self._is_finalized(in_progress.new_rel):
            return self._abandon_reconcile(in_progress.new_rel)
        try:
            # Drive both symlinks to the recorded terminal via ``restore``
            # (which unlinks on ``None``) — direction-agnostic: a forward
            # cutover intent points at the candidate, a PHASE_COMPENSATE
            # intent points back at the prior pair.
            self._symlinks.restore(self._symlinks.previous, in_progress.old_rel)
            self._symlinks.restore(self._symlinks.current, in_progress.new_rel)
            self._ledger_write(
                _Ledger(
                    current=in_progress.new_rel,
                    previous=in_progress.old_rel,
                    in_progress=None,
                ),
                step=LEDGER_STEP_RECONCILE,
            )
        except (OSError, ValueError, KeyError) as exc:
            # Idempotent: in_progress stays set on a partial failure, so a
            # subsequent reconcile re-applies cleanly.
            raise ReleaseManagerError(
                f"reconcile (phase={in_progress.phase}) failed: {exc}"
            ) from exc
        self._logger.info(
            "reconcile completed in_progress(phase=%s): current=%s previous=%s",
            in_progress.phase, in_progress.new_rel, in_progress.old_rel,
        )
        # The drive is direction-agnostic, but the OUTCOME token is not: a
        # PHASE_COMPENSATE intent drove BACK to the prior pair (rollback
        # recovery), every other phase drove FORWARD to the candidate
        # (cutover recovery). phase is already in hand — surface it so a
        # consumer needn't re-derive direction from the cleared ledger.
        action = (
            RECONCILE_COMPENSATED
            if in_progress.phase == PHASE_COMPENSATE
            else RECONCILE_FORWARD_COMPLETED
        )
        return ReconcileResult(
            action=action,
            current=in_progress.new_rel,
            previous=in_progress.old_rel,
        )

    def _abandon_reconcile(self, new_rel: str) -> ReconcileResult:
        """Clear an ``in_progress`` row whose candidate never finalized.

        Cannot forward to a non-existent/corrupted release (that would
        brick cold-boot), so leave the live ``current`` as-is and clear
        the marker, logging loudly.
        """
        try:
            current_now = self._symlinks.read(self._symlinks.current)
            previous_now = self._symlinks.read(self._symlinks.previous)
        except OSError as exc:  # Q4: raw os.readlink → ReleaseManagerError
            raise ReleaseManagerError(
                f"reconcile abandon symlink read failed: {exc}"
            ) from exc
        self._logger.error(
            "reconcile: in_progress new_rel=%s not finalized; clearing marker, "
            "current unchanged (%s)",
            new_rel, current_now,
        )
        try:
            self._ledger_write(
                _Ledger(current=current_now, previous=previous_now, in_progress=None),
                step=LEDGER_STEP_RECONCILE,
            )
        except (OSError, ValueError, KeyError) as exc:
            raise ReleaseManagerError(
                f"reconcile abandon-clear failed: {exc}"
            ) from exc
        return ReconcileResult(
            action=RECONCILE_ABANDONED, current=current_now, previous=previous_now,
        )


class ReleaseManager:
    """Lifecycle coordinator for the materialized-release artifacts.

    Owns the ``current``/``previous`` symlinks; delegates building to
    :class:`ReleaseBuilder` and ledger I/O to :class:`ReleaseLedger`. All
    constructor dependencies are keyword-only and injectable so the
    standalone smokes can drive the component against a synthetic source
    tree under ``~/.ananta`` scratch without cloning the real 1.8 GB
    ``.venv`` or touching the operator's real releases directory —
    mirroring the ``spawn_fn`` / ``plist_dir`` injection pattern in
    ``swap_orchestrator.py`` / ``autostart_manager.py``.

    Args:
        solet_name: Names the per-solet releases sub-directory
            (``~/.ananta/releases/<name>/``).
        source_root: The working-tree repo root to snapshot.
        releases_root: Override for the releases directory. Defaults to
            ``~/.ananta/releases/<name>/``.
        keep_releases: GC retention count K (design §4.6, default 3).
        cp_binary: Path to ``cp`` (must support BSD ``-c`` APFS clone).
        clone_timeout_seconds: Per-subtree ``cp -cR`` timeout.
        clock: Injectable UTC clock for deterministic release ids.
        git_sha_resolver: Injectable short-sha resolver.
        strict_pth_validation: When True, a missing ``.pth`` target
            *fails* the build instead of warning (design §4.7 leaves
            fail-or-warn a choice; warn is the default so the one
            pre-existing stale editable install does not brick every
            real build).
        mid_swap_hook: Test seam invoked between the ``previous`` and
            ``current`` symlink ``os.replace`` calls. Production leaves it
            ``None``. A crash-consistency smoke injects it to simulate
            either process death (raise a non-``OSError``) or an
            in-process abort (raise ``OSError``).
        ledger_write_hook: Test seam invoked with the swap-step label
            (:data:`LEDGER_STEP_BEGIN` / ``DONE`` / ``CLEAR``) before each
            cutover/rollback ledger write. Production leaves it ``None``.
            A failure-contract smoke injects it to raise ``OSError`` on a
            chosen step (a ledger write failing before the symlink swap or
            after both) to assert cutover converts it to
            ``ReleaseManagerError``.
        logger: Optional logger; defaults to the plugin logger.
    """

    def __init__(
        self,
        *,
        solet_name: str,
        source_root: Path,
        releases_root: Path | None = None,
        keep_releases: int = DEFAULT_KEEP_RELEASES,
        cp_binary: str = CP_BINARY_DEFAULT,
        clone_timeout_seconds: float = CLONE_TIMEOUT_SECONDS_DEFAULT,
        clock: Callable[[], datetime] | None = None,
        git_sha_resolver: Callable[[Path], str] | None = None,
        dirty_paths_resolver: (
            Callable[[Path, tuple[str, ...]], tuple[str, ...] | None] | None
        ) = None,
        strict_pth_validation: bool = False,
        mid_swap_hook: Callable[[], None] | None = None,
        ledger_write_hook: Callable[[str], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._releases_root = (
            releases_root.expanduser()
            if releases_root is not None
            else Path(RELEASES_ROOT_DEFAULT).expanduser() / solet_name
        )
        self._keep_releases = keep_releases
        self._logger = logger or logging.getLogger(PLUGIN_NAME)
        self._ledger = ReleaseLedger(self._releases_root)
        self._symlinks = ReleaseSymlinks(self._releases_root)
        self._gc = ReleaseGc(
            releases_root=self._releases_root,
            ledger=self._ledger,
            symlinks=self._symlinks,
            logger=self._logger,
        )
        self._builder = ReleaseBuilder(
            source_root=source_root.expanduser(),
            releases_root=self._releases_root,
            cp_binary=cp_binary,
            clone_timeout_seconds=clone_timeout_seconds,
            clock=clock or _default_clock,
            git_sha_resolver=git_sha_resolver or _default_git_sha,
            dirty_paths_resolver=dirty_paths_resolver or _default_dirty_paths,
            strict_pth_validation=strict_pth_validation,
            logger=self._logger,
        )
        self._swapper = ReleaseSwapper(
            symlinks=self._symlinks,
            ledger=self._ledger,
            is_finalized=self._is_finalized,
            mid_swap_hook=mid_swap_hook,
            ledger_write_hook=ledger_write_hook,
            logger=self._logger,
        )

    # ------------------------------------------------------------------
    # Read-only introspection (for the Phase-2 consumer + smokes)
    # ------------------------------------------------------------------

    @property
    def releases_root(self) -> Path:
        return self._releases_root

    @property
    def current_release(self) -> str | None:
        """The ``rel-<id>`` ``current`` points at, or ``None``."""
        return self._symlinks.read(self._symlinks.current)

    @property
    def previous_release(self) -> str | None:
        """The ``rel-<id>`` ``previous`` points at, or ``None``."""
        return self._symlinks.read(self._symlinks.previous)

    # ------------------------------------------------------------------
    # build_candidate (delegates to ReleaseBuilder, design §4.4 / §4.7)
    # ------------------------------------------------------------------

    def build_candidate(
        self,
        *,
        manifest_etag: str = "",
        manifest_plugins: tuple[str, ...] | None = None,
        schema_snapshot_fn: Callable[[Path], dict[str, object]] | None = None,
        allow_dirty: bool = False,
        reconciliation_provenance: dict[str, str] | None = None,
    ) -> CandidatePaths:
        """Materialize a fresh immutable release; see :meth:`ReleaseBuilder.build`.

        ``schema_snapshot_fn`` is the Phase-2 preflight seam (see
        :meth:`ReleaseBuilder.build`): a caller-supplied producer,
        invoked against the materialized clone's ``code/``, whose result
        is recorded in ``VERSION`` + on ``CandidatePaths`` and later read
        back via :meth:`current_schema_snapshot`.

        ``manifest_plugins`` is this build's own profile plugin set, persisted
        so the §3 derive path can later gate this tree by the manifest that
        actually governed it (see :meth:`ReleaseBuilder.build`, BLG-02).

        ``allow_dirty`` overrides the dirty-tree gate (see
        :meth:`ReleaseBuilder.build`). No production call site passes it
        today — by design, since it is the operator-confirmation path's
        knob, not a deploy-verb default.

        ``reconciliation_provenance`` is forwarded unchanged; see
        :meth:`ReleaseBuilder.build`.
        """
        return self._builder.build(
            manifest_etag=manifest_etag,
            manifest_plugins=manifest_plugins,
            schema_snapshot_fn=schema_snapshot_fn,
            allow_dirty=allow_dirty,
            reconciliation_provenance=reconciliation_provenance,
        )

    def candidate_for(self, release_id: str) -> CandidatePaths:
        """Rehydrate a FINALIZED ``release_id`` into :class:`CandidatePaths`.

        The inverse of :meth:`build_candidate` for a release already on disk
        — the durable-rollback verb's seam: it turns ``previous_release``
        back into the spawn paths (``code_root`` / ``venv_python`` / …) so a
        rollback can launch the prior release as a fresh process WITHOUT the
        orchestrator hand-duplicating the release layout (that lives in
        :class:`ReleaseBuilder`).

        Raises ``ReleaseManagerError`` if ``release_id`` is not finalized
        (missing dir or ``VERSION``) or its ``VERSION`` is
        unreadable/torn/malformed (F1 contract). Finalization atomicity
        guarantees a complete ``code/`` + ``venv/`` tree, so no extra
        existence check beyond :meth:`_assert_finalized` — a tampered tree
        fails loudly at spawn (fast-fail).
        """
        self._assert_finalized(release_id)
        return self._builder.rehydrate(release_id)

    def current_schema_snapshot(self) -> dict[str, object] | None:
        """The ``schema_snapshot`` recorded in the CURRENT release's ``VERSION``.

        The OLD side of the Phase-2 preflight DDL-free gate's diff (the
        NEW side is ``CandidatePaths.schema_snapshot`` for the candidate
        about to be built). ``None`` when there is no current release
        (first deploy) or the current release predates schema-snapshot
        capture (deferred-producer cycle). A corrupt/torn/malformed current
        ``VERSION`` is NOT silently treated as absent — it raises
        ``ReleaseManagerError`` via :func:`_read_version_or_raise`, so a
        corrupt OLD snapshot cannot masquerade as 'no old schema → additive'
        in the gate's diff (false-additive).
        """
        version_path = self._symlinks.current / VERSION_FILENAME
        if not version_path.is_file():
            return None
        snapshot = _read_version_or_raise(version_path).get("schema_snapshot")
        return snapshot if isinstance(snapshot, dict) else None

    # ------------------------------------------------------------------
    # cutover / rollback (design §4.5 / §4.6)
    # ------------------------------------------------------------------

    def cutover(self, candidate: CandidatePaths) -> SwapResult:
        """Atomically activate ``candidate``, demoting ``current`` to ``previous``.

        Crash-consistent per §4.6: ledger ``phase=cutover`` →
        ``os.replace previous`` → ``os.replace current`` → ledger
        ``phase=done`` → clear ``in_progress``.

        Public failure contract (F1, Phase-2 adversarial review): cutover
        raises ONLY :class:`ReleaseManagerError` — never a raw ``OSError``
        / ``json.JSONDecodeError`` (a ``ValueError``) / ``KeyError`` from
        the bracketing ledger reads + writes — so the orchestrator's
        ``except ReleaseManagerError`` compensation always fires. Any
        in-process failure (a mid-swap symlink error OR a bracketing
        ledger I/O error) triggers a BEST-EFFORT compensation back to the
        pre-swap state (§4.7: current/previous unchanged) before the
        chained re-raise — see :meth:`_compensate_failed_swap` for the
        residual-divergence limit under a persistent ledger-write
        failure. A non-``OSError`` interruption (process death) still
        escapes mid-swap, deliberately leaving the forward-completable
        ``in_progress`` row reconcile relies on.
        """
        self._assert_finalized(candidate.release_id)
        ledger = _read_ledger_or_raise(self._ledger)
        return self._swapper.perform_swap(
            new_current=candidate.release_id,
            new_previous=ledger.current,
            prior_current=ledger.current,
            prior_previous=ledger.previous,
        )

    def rollback(self) -> SwapResult:
        """Durable rollback (§4.5): swap ``current`` and ``previous``.

        After rollback the prior release is ``current`` (a cold boot
        launches it) and the rolled-back-from release is ``previous`` (so
        a subsequent ``rollback`` rolls forward again). Works at any time
        — post-drain, post-reboot — because both trees persist on disk.
        Same ``ReleaseManagerError``-only failure contract as
        :meth:`cutover`.
        """
        ledger = _read_ledger_or_raise(self._ledger)
        if ledger.previous is None:
            raise ReleaseManagerError("no previous release to roll back to")
        self._assert_finalized(ledger.previous)
        return self._swapper.perform_swap(
            new_current=ledger.previous,
            new_previous=ledger.current,
            prior_current=ledger.current,
            prior_previous=ledger.previous,
        )

    def reconcile(self) -> ReconcileResult:
        """Startup reconcile of an interrupted swap (design §4.6).

        Phase-2 calls this at boot to forward-complete — or compensation-
        complete — a swap left ``in_progress`` by process death. Delegated
        to :class:`ReleaseSwapper`, which owns the crash-consistency
        invariant; the returned ``action`` distinguishes a recovery-forward
        (:data:`RECONCILE_FORWARD_COMPLETED`) from a rollback-back
        (:data:`RECONCILE_COMPENSATED`).
        """
        return self._swapper.reconcile()

    # ------------------------------------------------------------------
    # gc (design §4.6 GC-safety)
    # ------------------------------------------------------------------

    def gc(self, *, keep: int | None = None) -> GcResult:
        """Keep the newest K finalized releases; delete the tail.

        Never deletes a release named by ``current``, ``previous``, or an
        in-progress ledger row (§4.6 GC-safety), regardless of K.
        Delegates to :class:`ReleaseGc`.
        """
        keep_n = self._keep_releases if keep is None else keep
        return self._gc.collect(keep=keep_n)

    # ------------------------------------------------------------------
    # Finalization checks (symlink + ledger mechanics live in their own
    # classes: ReleaseSymlinks, ReleaseLedger)
    # ------------------------------------------------------------------

    def _is_finalized(self, release_id: str) -> bool:
        # Q4 (Architect): this pre-try check runs OUTSIDE cutover/rollback/
        # reconcile's wrap, so its raw is_dir/is_file must convert a
        # filesystem error to ReleaseManagerError itself.
        release_dir = self._releases_root / release_id
        try:
            return release_dir.is_dir() and (release_dir / VERSION_FILENAME).is_file()
        except OSError as exc:
            raise ReleaseManagerError(
                f"finalization check failed for {release_id}: {exc}"
            ) from exc

    def _assert_finalized(self, release_id: str) -> None:
        if not self._is_finalized(release_id):
            raise ReleaseManagerError(f"release not finalized: {release_id}")


__all__ = [
    "CandidatePaths",
    "GcResult",
    "ReconcileResult",
    "ReleaseBuilder",
    "ReleaseGc",
    "ReleaseLedger",
    "ReleaseManager",
    "ReleaseManagerError",
    "ReleaseSwapper",
    "ReleaseSymlinks",
    "SwapResult",
]
