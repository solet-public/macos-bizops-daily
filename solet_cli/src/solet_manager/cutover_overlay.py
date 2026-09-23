"""Prepare and append receipt-authorized cutover overlays through the I2 API.

Seam amendment v1.3 gives T2 preparatory-only ownership: manager-side refresh
builds an ordinal-free ``OverlayFile`` payload for the changed
``plugins/*/src`` paths and records it as ``managed_tree_overlay`` journal
evidence.  T2 has no production call to this module and does not claim overlay
consumption complete.

M3 is the production caller.  Only after its target-local terminal receipt is
durable does M3 call :func:`contribute_cutover_overlay`, which creates the
``OverlayEntry`` at the ledger's current ordinal and delegates the append and
receipt authority to :mod:`solet_manager.managed_tree`.  M3 also owns
reconciliation-id idempotency and recovery: same id is a no-op; divergent files
for that id are a refusal; a successful terminal receipt with no ledger entry
is retried.

Nothing here digests a managed tree, recomputes a ledger, or decides whether an
overlay verifies.  Runtime surface digests likewise remain distinct from the
per-path ``before``/``after`` file identities prepared here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from solet_manager.managed_tree import (
    ManagedTreeLedger,
    ManagedTreeStore,
    OverlayEntry,
    OverlayFile,
)

__all__ = [
    "REFRESHED_SURFACE_GLOB",
    "contribute_cutover_overlay",
    "overlay_files_for_refresh",
]

#: The path set a cutover refresh contributes, as the seam contract names it.
REFRESHED_SURFACE_GLOB = "plugins/*/src"


def file_sha256(path: Path) -> str:
    """``sha256:<hex>`` over one regular file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def overlay_files_for_refresh(
    *,
    target: Path,
    before_sha256_by_path: dict[str, str | None],
    changed_paths: Iterable[str],
) -> tuple[OverlayFile, ...]:
    """Build the ordered overlay-file tuple for one refresh.

    ``before_sha256_by_path`` is the manager's OWN before-image record, taken
    before the bytes moved; it is not re-derived here, because after the
    refresh the "before" state no longer exists to be read.  A path absent from
    that mapping is treated as newly created (``before`` null), which is the
    contract's meaning for null — not a missing measurement.

    ``after`` is read from disk now.  A path that no longer exists on disk is a
    deletion (``after`` null), which the contract represents explicitly rather
    than by omitting the path — an overlay that silently dropped deletions
    would digest to a tree that never existed.

    Ordering is by path ascending, which the API also enforces; sorting here
    means a caller cannot produce a refusal by accident of iteration order.
    """
    files = [
        OverlayFile(
            path=relative,
            before_sha256=before_sha256_by_path.get(relative),
            after_sha256=_after_digest(target / relative),
        )
        for relative in sorted(set(changed_paths))
    ]
    if not files:
        raise ValueError("a cutover overlay entry must cover at least one path")
    return tuple(files)


def _after_digest(absolute: Path) -> str | None:
    if absolute.is_symlink():
        # Symlinks are not representable in managed-tree-v1 (a NAMED v1 limit,
        # not an oversight). Refusing here keeps the omission visible instead
        # of letting the entry claim a tree the target does not have.
        raise ValueError(f"managed-tree-v1 cannot represent a symlink: {absolute}")
    return file_sha256(absolute) if absolute.is_file() else None


def contribute_cutover_overlay(
    *,
    target: Path,
    reconciliation_id: str,
    baseline_digest: str,
    before_sha256_by_path: dict[str, str | None],
    changed_paths: Iterable[str],
) -> ManagedTreeLedger:
    """Append this reconciliation's single overlay entry through the API.

    The ordinal is derived from the ledger's current length rather than
    supplied: the append is the only writer that knows what "next" is, and a
    caller-chosen ordinal is a race waiting to be lost.  The API re-checks it,
    so a concurrent append refuses rather than silently reordering history.

    The receipt-authorization check is NOT performed here.  ``append`` resolves
    ``reconciliation_id`` to a terminal receipt itself and refuses without one,
    which is the seam's whole point: the entry exists only under a receipt's
    authority, and only one component gets to decide that.
    """
    store = ManagedTreeStore(target)
    existing = store.load()
    ordinal = 0 if existing is None else len(existing.entries)
    entry = OverlayEntry(
        ordinal=ordinal,
        reconciliation_id=reconciliation_id,
        files=overlay_files_for_refresh(
            target=target,
            before_sha256_by_path=before_sha256_by_path,
            changed_paths=changed_paths,
        ),
    )
    return store.append(baseline_digest=baseline_digest, entry=entry)
