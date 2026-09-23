"""The one nameable pointer repair (Step 6 design sections 3.1, 4.4, 5.3 step 4).

An active pointer that names a ``promoted``, ``abandoned`` or retired update
journal, or a ``verified`` import journal, is stale: the operation is over
and only the release of the pointer is missing (a crash after the terminal
write).  ``update --yes`` and ``reconcile --yes`` perform the release on
sight; ``reconcile --release-pointer --yes`` performs exactly this and
nothing else; ``doctor`` only *reports* it (D5).  The proof argument the CAS
demands is read here from the journal at the pointer, never assumed.
"""

from __future__ import annotations

from typing import cast

from solet_setup_contracts import canonical_sha256

from .errors import StateError
from .maintenance_inventory import TerminalProof, release_active_operation
from .maintenance_journal import read_maintenance_operation
from .models import ActiveOperation, InstanceInventoryRecordV2, JsonValue, MaintenanceOperationKind
from .paths import ManagerPaths
from .state_io import instance_lock
from .transaction import utc_now
from .update_journal import is_retired, read_update_journal

__all__ = ["release_terminal_pointer", "terminal_proof"]


def terminal_proof(paths: ManagerPaths, record: InstanceInventoryRecordV2) -> TerminalProof | None:
    """Read the journal at the active pointer and describe it as a release proof; ``None`` without a pointer."""
    active = record.active_operation
    if active is None:
        return None
    path = paths.operation_path(record.instance_id, active.operation_id)
    if active.kind is MaintenanceOperationKind.UPDATE:
        journal = read_update_journal(path)
        if journal["operation_id"] != active.operation_id or journal["instance_id"] != record.instance_id:
            raise StateError("the update journal at the active pointer names a different operation")
        result = journal["result"]
        digest = None if result is None else canonical_sha256(cast(dict[str, JsonValue], result))
        return TerminalProof(active.operation_id, active.kind, cast(str, journal["status"]), digest, is_retired(journal))
    if active.kind is MaintenanceOperationKind.IMPORT:
        document = read_maintenance_operation(path)
        return TerminalProof(active.operation_id, active.kind, cast(str, document["status"]), None, False)
    raise StateError("the active pointer names a doctor operation; this Manager never writes that")


def release_terminal_pointer(paths: ManagerPaths, record: InstanceInventoryRecordV2) -> InstanceInventoryRecordV2 | None:
    """Release a pointer whose journal is over; returns the new row, or ``None`` when nothing was released."""
    proof = terminal_proof(paths, record)
    if proof is None or not proof.releasable:
        return None
    with instance_lock(paths.registry_lock_path, create=True):
        released = release_active_operation(paths.maintenance_inventory_path, record, proof=proof, now=utc_now())
    if released.active_operation is not None:
        raise StateError("the active pointer did not release")
    return released


def pointer_names(record: InstanceInventoryRecordV2, kind: MaintenanceOperationKind, operation_id: str) -> bool:
    return record.active_operation == ActiveOperation(kind, operation_id)
