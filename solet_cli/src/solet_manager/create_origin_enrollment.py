"""Prove a Manager-created instance against the Manager's own create record (iss_836499b3).

``solet create`` records in its v1 registry row, and in its create transaction, exactly which seed
release it installed.  The transaction must show the install over (every stage final, every completion
check answered); a create blocked only at completion is eligible (iss_fcbfabb7).  An inspection that compares that instance with the *installed*
channel release can prove it only while nothing is left to update: after a Manager upgrade the
instance is one release behind and classifies ``source_identity_unproven``.  This module makes the
Manager's own record the inspection contract instead -- the move ``update_execution.enrolled_metadata``
already makes for an enrolled row -- so the unchanged inspection and classification rows decide.

The stamp values (provenance digest, ``seed_id``, ``origin_id``, manifest) are read from the target's
working ``PROVENANCE.json``.  The inspection then requires HEAD to be the recorded commit, the committed
``HEAD:PROVENANCE.json`` to be strict with the same digest, the working file to be byte-identical to it,
and the seal trailers to verify; a commit id binds its tree, so the values used are the recorded
release's own.  Before they are used, the record is bound to the installed channel: the same
repository, the same profile, and the same minting origin (``origin_id`` is per origin and survives
every re-mint; ``seed_id`` is per mint and does not).

The proof is content identity, not directory identity: v1 records the created target's path but no
device or inode, so a byte-identical checkout at the recorded path proves the same as the original
directory would.  What the path must be is closed instead (review B2): ``solet create`` records a
resolved path (``config_loading.resolve_target``), so the recorded target must still be that literal
path with no symbolic-link component, and the directory the inspection pinned must be the one observed
at that path before it ran.  A symlink planted at, or above, the recorded path is refused, never
followed.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from solet_setup_contracts.provenance_v1 import (
    ProvenanceV1,
    ProvenanceV1Error,
    canonical_provenance_sha256,
    parse_provenance_v1,
)

from .errors import ManagedIdentityDriftError, OperationInProgressError
from .existing_install_inspection import (
    ChannelInspectionIdentity,
    InspectionEffectTracker,
    InstalledInspectionMetadata,
    InstalledInspectionMetadataLoader,
    TargetFilesystemIdentity,
)
from .models import CheckpointStatus, InstanceRecord, JsonValue, TransactionStatus
from .paths import ManagerPaths
from .registry import InstanceRegistry
from .transaction import Transaction, load_transaction

_PROVENANCE = "PROVENANCE.json"
_SETTLED_STAGE = frozenset({CheckpointStatus.VERIFIED, CheckpointStatus.DECLINED, CheckpointStatus.NOT_APPLICABLE})
_ANSWERED_CHECK = frozenset({CheckpointStatus.VERIFIED, CheckpointStatus.BLOCKED, CheckpointStatus.FAILED})
_ELIGIBLE_STATUS = frozenset({TransactionStatus.VERIFIED, TransactionStatus.BLOCKED, TransactionStatus.FAILED})
_FOREIGN_SEED_REPAIR = (
    "This Solet was created from a different seed than the installed Manager serves; "
    "it cannot be updated from this channel."
)


def find_create_origin_record(paths: ManagerPaths, name: str) -> InstanceRecord | None:
    """The v1 create row named ``name``, or ``None`` when the Manager never created it."""
    return next((item for item in InstanceRegistry(paths.registry_path).list() if item.name == name), None)


@dataclass(frozen=True, slots=True)
class CreateOriginEligibility:
    """What an update-eligible create transaction proves, and which completion checks it never verified."""

    create_status: TransactionStatus
    unverified_checks: tuple[str, ...]

    def disclosure(self) -> dict[str, JsonValue]:
        return {"status": self.create_status.value, "unverified_completion_checks": list(self.unverified_checks)}


def require_update_eligible_create_origin(paths: ManagerPaths, record: InstanceRecord) -> CreateOriginEligibility:
    """The one create-origin gate ``update`` and ``import`` share, at preview and at apply (iss_fcbfabb7).

    Refuse a create row whose transaction is missing or disagrees with it, then apply
    :func:`create_origin_eligibility`.
    """
    transaction = load_transaction(paths.transaction_path(record.name))
    if transaction is None:
        raise ManagedIdentityDriftError("create-origin transaction identity is unproven")
    if not create_transaction_matches_record(transaction, record):
        raise ManagedIdentityDriftError("create-origin transaction does not match its v1 registry record")
    return create_origin_eligibility(transaction)


def create_origin_eligibility(transaction: Transaction) -> CreateOriginEligibility:
    """An installed Solet is update-eligible once its install is over, whatever its completion checks said.

    Eligible exactly when every install stage is final (``verified``, ``declined``,
    ``not_applicable`` -- the set ``journal_rollup`` rolls up as final) and every bound completion
    check ran to an answer (``verified``, ``blocked`` or ``failed``).  The transaction is then
    ``verified``, or ``blocked``/``failed`` at completion only.  Completion checks are read-only probes
    of the running Solet; the ones r46-r48 creates left blocked (the embeddings defect, iss_2eefe356)
    are what the update repairs, and the update's final doctor re-runs its own checks against the new
    release.  A stage that is not final, or a check still ``pending``, ``applying``,
    ``awaiting_user`` or otherwise unanswered, is an install in flight: refused.  The recorded status
    must agree (``load_transaction`` already refuses one the roll-up contradicts).
    """
    unsettled = _outside(transaction.stages, _SETTLED_STAGE)
    unanswered = _outside(transaction.completion, _ANSWERED_CHECK)
    recorded = transaction.status in _ELIGIBLE_STATUS and bool(transaction.stages) and bool(transaction.completion)
    if unsettled or unanswered or not recorded:
        raise OperationInProgressError(
            f"create-origin transaction remains nonterminal ({transaction.status.value}): "
            f"install stages not final {unsettled}, completion checks not run {unanswered}",
            repair=f"Resume with: solet create {transaction.name}",
        )
    unverified = _outside(transaction.completion, frozenset({CheckpointStatus.VERIFIED}))
    return CreateOriginEligibility(transaction.status, tuple(unverified))


def _outside(statuses: dict[str, CheckpointStatus], allowed: frozenset[CheckpointStatus]) -> list[str]:
    return sorted(key for key, status in statuses.items() if status not in allowed)


def create_transaction_matches_record(transaction: Transaction, record: InstanceRecord) -> bool:
    """Match every immutable seed field persisted by the v1 create registry."""
    return (
        transaction.name == record.name
        and transaction.target == record.target
        and transaction.seed.repository == record.seed_repository
        and transaction.seed.commit == record.seed_commit
        and transaction.seed.tree_hash == record.seed_tree_hash
        and transaction.seed.release_tag == record.seed_tag
        and transaction.seed.profile == record.profile
    )


@dataclass(frozen=True, slots=True)
class RecordedTarget:
    """The literal recorded target and the directory observed there before inspection."""

    path: Path
    device: int
    inode: int

    def require_inspected(self, identity: TargetFilesystemIdentity) -> None:
        """The inspection pinned this very directory, at this very path."""
        if identity.canonical_display != self.path or (identity.target_device, identity.target_inode) != (
            self.device,
            self.inode,
        ):
            raise ManagedIdentityDriftError(
                f"the create-origin target {self.path} changed while it was being inspected",
                repair="Leave the Solet's directory in place, then retry.",
            )


def recorded_target_identity(record: InstanceRecord) -> RecordedTarget:
    """The recorded target as a real directory at its literal path, or a loud refusal (review B2).

    ``solet create`` records a resolved path, so any symbolic link at or above it was planted after
    create: following it would prove whatever directory it points at.
    """
    recorded = Path(record.target)
    try:
        resolved = recorded.resolve(strict=True)
        observed = os.lstat(recorded)
    except OSError as exc:
        raise ManagedIdentityDriftError(
            f"the create-origin target {recorded} is no longer observable: {exc}",
            repair=f"Restore the Manager-created checkout at {recorded}, then retry.",
        ) from exc
    if resolved != recorded or not stat.S_ISDIR(observed.st_mode):
        raise ManagedIdentityDriftError(
            f"the create-origin target {recorded} for {record.name!r} is no longer a real directory at its "
            f"recorded path (it resolves to {resolved}); a symbolic link is never followed",
            repair=f"Put the Manager-created checkout back at {recorded} as a real directory, then retry.",
        )
    return RecordedTarget(recorded, observed.st_dev, observed.st_ino)


def require_create_origin_target(record: InstanceRecord, target: Path) -> RecordedTarget:
    """``--target`` must resolve to the literal recorded target, which must be a real directory.

    The operator's own ``--target`` may reach it through a link (it is resolved); the recorded path may
    not, and it is the recorded path, never the requested one, that is inspected.
    """
    recorded = recorded_target_identity(record)
    try:
        requested = target.resolve(strict=True)
    except OSError as exc:
        raise ManagedIdentityDriftError(f"--target {target} is not observable: {exc}") from exc
    if requested != recorded.path:
        raise ManagedIdentityDriftError(
            f"--target {requested} is not the directory the Manager created for {record.name!r} ({recorded.path})",
            repair=f"Pass --target {recorded.path}.",
        )
    return recorded


class CreateOriginMetadataLoader:
    """An inspection metadata loader that proves against the create record, not the installed release.

    It retains the installed channel metadata it bound to, so an enrollment can record whether the
    proven release is behind the channel.
    """

    def __init__(
        self,
        record: InstanceRecord,
        target: Path,
        installed_loader: InstalledInspectionMetadataLoader,
    ) -> None:
        self._record = record
        self._target = target
        self._installed_loader = installed_loader
        self.installed: InstalledInspectionMetadata | None = None

    def __call__(self, channel: str, tracker: InspectionEffectTracker) -> InstalledInspectionMetadata:
        installed = self._installed_loader(channel, tracker)
        payload = _working_provenance(self._target, tracker)
        try:
            stamp = parse_provenance_v1(payload)
            digest = canonical_provenance_sha256(payload)
        except ProvenanceV1Error as exc:
            raise ManagedIdentityDriftError(
                f"the created instance's {_PROVENANCE} is not a strict provenance stamp: {exc}"
            ) from exc
        _require_channel_binding(self._record, installed.channel_identity, stamp)
        self.installed = installed
        identity = _recorded_release_identity(self._record, installed.channel_identity, stamp, digest)
        return InstalledInspectionMetadata(identity, installed.seed_lock, ())


def _require_channel_binding(
    record: InstanceRecord, channel: ChannelInspectionIdentity, stamp: ProvenanceV1
) -> None:
    for field, recorded, installed in (
        ("repository", record.seed_repository, channel.repository),
        ("profile", record.profile, channel.profile),
        ("origin_id", stamp.origin_id, channel.origin_id),
    ):
        if recorded != installed:
            raise ManagedIdentityDriftError(
                f"create-origin {field} {recorded!r} does not match the installed channel's {installed!r}",
                repair=_FOREIGN_SEED_REPAIR,
            )


def _recorded_release_identity(
    record: InstanceRecord, channel: ChannelInspectionIdentity, stamp: ProvenanceV1, digest: str
) -> ChannelInspectionIdentity:
    """The recorded release as the inspection contract; every channel/contract identity stays installed."""
    return ChannelInspectionIdentity(
        channel.channel_id,
        record.seed_repository,
        record.seed_tag or record.seed_commit,
        record.seed_commit,
        record.seed_tree_hash,
        record.profile,
        digest,
        stamp.seed_id,
        stamp.origin_id,
        stamp.manifest_sha256,
        channel.existing_install_contract,
        channel.catalog_resource,
        channel.catalog_sha256,
        channel.seed_lock_resource,
        channel.seed_lock_sha256,
        channel.descriptor_digest,
        channel.anchor_table_resource,
        channel.anchor_table_sha256,
    )


def _working_provenance(target: Path, tracker: InspectionEffectTracker) -> bytes:
    """Read the target's regular ``PROVENANCE.json`` once, never following a final symlink."""
    tracker.record_resource_read(f"target:{_PROVENANCE}")
    try:
        descriptor = os.open(target / _PROVENANCE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ManagedIdentityDriftError(f"the created instance's {_PROVENANCE} is unreadable: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ManagedIdentityDriftError(f"the created instance's {_PROVENANCE} is not a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1_048_576):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)
