"""Durable, content-addressed Manager-state copies of the contracts an update binds.

Step 6 design section 3.2: the transition bundle a journal approved and the
release descriptor that named it are copied under ``state_dir`` at Step-4
apply time, so neither the purgeable candidate cache nor the *installed*
formula descriptor (which a Manager upgrade moves) is needed to resume or to
run the final doctor.  Every read re-digests the bytes against the identity
the journal recorded; a mismatch is ``transition_contract_mismatch``, never a
silent fallback to another source.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

from .contracts import transition_bundle_filenames
from .errors import StateError, TransitionContractMismatchError
from .existing_install_bundle import transition_bundle_digest
from .models import JsonValue
from .paths import ManagerPaths, descriptor_copy_dir, transition_contract_dir
from .state_io import atomic_write_json, ensure_private_directory, load_json_object

__all__ = [
    "ANCHORS_COPY_NAME",
    "RECEIPT_NAME",
    "SEED_LOCK_COPY_NAME",
    "descriptor_copy_exists",
    "read_descriptor_copy",
    "read_transition_contract",
    "transition_contract_exists",
    "write_descriptor_copy",
    "write_transition_contract",
]

SEED_LOCK_COPY_NAME = "seed.lock.json"
ANCHORS_COPY_NAME = "existing_install_inspection_anchors.v1.json"
RECEIPT_NAME = "copy-receipt.json"
_RECEIPT_KEYS = frozenset({"descriptor_digest", "anchors_sha256", "catalog_path", "catalog_sha256", "channel_id", "manager_version"})


def transition_contract_exists(paths: ManagerPaths, digest: str) -> bool:
    directory = transition_contract_dir(paths, digest)
    return all((directory / name).is_file() for name in transition_bundle_filenames())


def write_transition_contract(paths: ManagerPaths, digest: str, files: dict[str, bytes]) -> None:
    """Persist the exact bundle bytes; an existing copy must already be byte-identical."""
    if transition_bundle_digest(files) != digest:
        raise TransitionContractMismatchError("transition bundle bytes do not digest to the journaled contract identity")
    directory = transition_contract_dir(paths, digest)
    ensure_private_directory(directory)
    for name in transition_bundle_filenames():
        _write_exact_bytes(directory / name, files[name])
    read_transition_contract(paths, digest)


def read_transition_contract(paths: ManagerPaths, digest: str) -> dict[str, bytes]:
    """Read the copied bundle files and re-prove their digest (Step 6 section 4.6 step 5)."""
    directory = transition_contract_dir(paths, digest)
    files: dict[str, bytes] = {}
    for name in transition_bundle_filenames():
        try:
            files[name] = (directory / name).read_bytes()
        except OSError as exc:
            raise TransitionContractMismatchError(f"durable transition contract copy is missing {name}") from exc
    if transition_bundle_digest(files) != digest:
        raise TransitionContractMismatchError("durable transition contract copy does not re-digest to the journaled identity", repair="Do not edit Manager state; re-run the update so the copy is backfilled from the candidate cache.")
    return files


def descriptor_copy_exists(paths: ManagerPaths, descriptor_digest: str) -> bool:
    directory = descriptor_copy_dir(paths, descriptor_digest)
    return all((directory / name).is_file() for name in (SEED_LOCK_COPY_NAME, ANCHORS_COPY_NAME, RECEIPT_NAME))


def write_descriptor_copy(
    paths: ManagerPaths,
    *,
    descriptor_bytes: bytes,
    anchors_document: dict[str, JsonValue],
    catalog_path: str,
    catalog_sha256: str,
    channel_id: str,
    manager_version: str,
) -> str:
    """Persist the self-sufficient descriptor copy and return its digest."""
    digest = "sha256:" + hashlib.sha256(descriptor_bytes).hexdigest()
    directory = descriptor_copy_dir(paths, digest)
    ensure_private_directory(directory)
    anchors_bytes = (json.dumps(anchors_document, indent=2, sort_keys=True) + "\n").encode()
    receipt: dict[str, JsonValue] = {
        "descriptor_digest": digest,
        "anchors_sha256": hashlib.sha256(anchors_bytes).hexdigest(),
        "catalog_path": catalog_path,
        "catalog_sha256": catalog_sha256,
        "channel_id": channel_id,
        "manager_version": manager_version,
    }
    _write_exact_bytes(directory / SEED_LOCK_COPY_NAME, descriptor_bytes)
    _write_exact_bytes(directory / ANCHORS_COPY_NAME, anchors_bytes)
    existing = load_json_object(directory / RECEIPT_NAME, missing_ok=True)
    if existing is None:
        atomic_write_json(directory / RECEIPT_NAME, receipt)
    elif existing != receipt:
        raise StateError("descriptor copy receipt differs from the copy being written")
    read_descriptor_copy(paths, digest)
    return digest


def read_descriptor_copy(paths: ManagerPaths, descriptor_digest: str) -> tuple[bytes, dict[str, JsonValue], dict[str, JsonValue]]:
    """Return ``(seed_lock_bytes, anchors_document, receipt)`` after verifying every digest the receipt names."""
    directory = descriptor_copy_dir(paths, descriptor_digest)
    try:
        seed_raw = (directory / SEED_LOCK_COPY_NAME).read_bytes()
        anchors_raw = (directory / ANCHORS_COPY_NAME).read_bytes()
    except OSError as exc:
        raise StateError("durable descriptor copy is incomplete") from exc
    receipt_raw = load_json_object(directory / RECEIPT_NAME, missing_ok=True)
    if receipt_raw is None or frozenset(receipt_raw) != _RECEIPT_KEYS:
        raise StateError("durable descriptor copy receipt is missing or malformed")
    receipt = receipt_raw
    if "sha256:" + hashlib.sha256(seed_raw).hexdigest() != descriptor_digest or receipt["descriptor_digest"] != descriptor_digest:
        raise StateError("durable descriptor copy does not digest to the journaled descriptor identity")
    if hashlib.sha256(anchors_raw).hexdigest() != receipt["anchors_sha256"]:
        raise StateError("durable descriptor copy anchors do not digest to the receipt")
    try:
        anchors = json.loads(anchors_raw)
    except json.JSONDecodeError as exc:
        raise StateError("durable descriptor copy anchors are unreadable") from exc
    if not isinstance(anchors, dict):
        raise StateError("durable descriptor copy anchors are malformed")
    return seed_raw, cast(dict[str, JsonValue], anchors), receipt


def _write_exact_bytes(path: Path, value: bytes) -> None:
    """Immutable raw-bytes write: refuse an existing mismatch, read back what was written."""
    if path.exists():
        if path.read_bytes() != value:
            raise StateError(f"durable contract copy differs from requested bytes: {path.name}")
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(value)
    temporary.chmod(0o600)
    temporary.replace(path)
    if path.read_bytes() != value:
        raise StateError(f"durable contract copy read-back mismatch: {path.name}")
