"""Advance a completed create's setup-contract pin to the release an update promotes (iss_0a10e6db).

v1 ``solet doctor`` and ``solet start`` load the checkout's setup contract against the create record's
``flow_contract_digest``.  An update fast-forwards the checkout, so after any release whose setup contract moved
(r48 ``e42e7515`` -> r56 ``19350a8d``, r63 ``19350a8d`` -> ``ce20096b``) both refused ``pinned setup contract
identity mismatch``, and ``reconcile-contract`` refuses a completed solet.  The promotion is the Manager's own proof
that the checkout is the candidate release, so it is where the pin moves: in the same journaled step, after the
v2 row is published, with both files backed up under the update operation first.

Only a pin the Manager can vouch for moves, and only to a digest the release itself carries:

- the create must be complete (``lifecycle_state`` ``verified``).  A setup-incomplete create keeps its pin; its
  contract moves only through a declared ``reconcile-contract`` bridge;
- the new digest is computed from the candidate commit's committed contract blobs, never from the working tree,
  so a hand edit in the checkout still refuses afterwards;
- the old pin must be the committed contract of the create release or of this update's journaled baseline, so a
  pin that already disagreed with its own release is left for the operator to see.  The create release is
  accepted so a solet promoted by a Manager without this step is repaired by its next update, including a
  verify-mode one at the release it is already on.

The write order is the create journal, then the registry row; a crash between them leaves the journal at the
release and the row at a proven old pin, which the rerun of the promotion finishes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Literal

from .contracts import contract_digest_from_bytes, contract_digested_filenames, target_contract_directory
from .errors import StateConflictError
from .installer_pins import read_target_blob
from .managed_artifact_backup import write_backup
from .models import InstanceInventoryRecordV2, InstanceRecord
from .paths import ManagerPaths
from .registry import InstanceRegistry, is_create_origin_alias
from .state_io import instance_lock
from .transaction import Transaction, load_transaction, utc_now, write_transaction

__all__ = ["CreateContractAdvance", "advance_create_contract_identity", "committed_contract_files"]

CreateContractAdvance = Literal["no_create_record", "setup_incomplete", "current", "unproven_pin", "advanced"]

_REGISTRY_ARTIFACT = "setup-contract.registry"
_TRANSACTION_ARTIFACT = "setup-contract.transaction"


def advance_create_contract_identity(
    paths: ManagerPaths,
    record: InstanceInventoryRecordV2,
    *,
    operation_id: str,
    baseline_commit: str,
    candidate_commit: str,
) -> CreateContractAdvance:
    """Move the create record and its journal to the candidate's setup contract; idempotent across a rerun.

    The caller holds the instance lock (``update --yes`` takes it for the whole command), which is also the lock v1
    ``doctor`` and ``start`` hold while they read and rewrite the create journal.
    """
    registry = InstanceRegistry(paths.registry_path)
    create = registry.get(record.name)
    if create is None or not is_create_origin_alias(create, record):
        return "no_create_record"
    if create.lifecycle_state != "verified":
        return "setup_incomplete"
    transaction = _create_transaction(paths, create)
    target = Path(create.target)
    declared = committed_contract_digest(target, candidate_commit)
    pins = {create.flow_contract_digest, transaction.flow_contract_digest}
    if pins == {declared}:
        return "current"
    proven = {declared} | {committed_contract_digest(target, commit) for commit in {create.seed_commit, baseline_commit}}
    if not pins <= proven:
        return "unproven_pin"
    for artifact, destination in ((_TRANSACTION_ARTIFACT, paths.transaction_path(create.name)), (_REGISTRY_ARTIFACT, paths.registry_path)):
        write_backup(paths, record.instance_id, operation_id, artifact, destination)
    _write_pins(paths, registry, create, transaction, declared)
    return "advanced"


def _create_transaction(paths: ManagerPaths, create: InstanceRecord) -> Transaction:
    """The create journal, bound to its registry row on every pinned field except the digest that may be mid-move."""
    transaction = load_transaction(paths.transaction_path(create.name))
    if transaction is None:
        raise StateConflictError(f"managed instance {create.name!r} lacks its create transaction journal")
    bound = (transaction.name, transaction.target, transaction.flow_id, transaction.flow_source_revision)
    if bound != (create.name, create.target, create.flow_id, create.flow_source_revision):
        raise StateConflictError("registry and transaction pinned identities differ")
    return transaction


def _write_pins(paths: ManagerPaths, registry: InstanceRegistry, create: InstanceRecord, transaction: Transaction, declared: str) -> None:
    """The journal first, then the registry row, each only when it is not already at ``declared``."""
    now = utc_now()
    if transaction.flow_contract_digest != declared:
        write_transaction(paths.transaction_path(create.name), replace(transaction, flow_contract_digest=declared, updated_at=now))
    if create.flow_contract_digest != declared:
        with instance_lock(paths.registry_lock_path, create=True):
            registry.reconcile_contract(expected=create, flow_contract_digest=declared, updated_at=now)


def committed_contract_digest(target: Path, commit: str) -> str:
    """The setup-contract digest of ``commit``'s committed blobs in the target's own object store."""
    return contract_digest_from_bytes(committed_contract_files(target, commit, contract_digested_filenames()))


def committed_contract_files(target: Path, commit: str, names: Iterable[str]) -> dict[str, bytes]:
    """``commit``'s committed setup-contract blobs by bare name, read from the target's own object store, never the working tree."""
    directory = target_contract_directory(Path()).as_posix()
    read = read_target_blob(target)
    return {name: read(f"{commit}:{directory}/{name}") for name in names}
