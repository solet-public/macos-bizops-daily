"""Package-owned metadata reader for existing-install inspection.

The public inspection module retains the loader entry point; keeping the
metadata projection here prevents package parsing from obscuring the pinned
target inspection flow.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import json
import os
import stat
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import cast

from .existing_install_inspection import (
    ChannelInspectionIdentity,
    ChannelRelation,
    ExistingInstallContractIdentity,
    InspectionAnchor,
    InspectionAnchorKind,
    InspectionEffectTracker,
    InstalledInspectionMetadata,
)
from .models import JsonValue
from .paths import ManagerPaths, descriptor_copy_dir
from .release_lock import SeedLock, seed_lock_from_fields
from .seed_lock_parser import parse_seed_lock_bytes

COPY_SOURCE = "copy"


@dataclass(frozen=True, slots=True)
class InstalledUpdateDescriptor:
    """The installed channel metadata together with its exact descriptor bytes.

    Step 4 needs the raw ``seed.lock.json`` bytes: the descriptor digest is the
    digest of exactly those bytes, and candidate acquisition re-parses them.
    """

    metadata: InstalledInspectionMetadata
    descriptor_bytes: bytes


def load_installed_inspection_metadata(
    channel: str, tracker: InspectionEffectTracker
) -> InstalledInspectionMetadata:
    """Load only package-owned catalog/anchors plus the formula seed lock."""
    return load_installed_update_descriptor(channel, tracker).metadata


def load_installed_update_descriptor(
    channel: str, tracker: InspectionEffectTracker
) -> InstalledUpdateDescriptor:
    """Load the installed metadata and retain the exact descriptor bytes."""
    catalog_raw, anchors_raw = _released_metadata_bytes(tracker)
    catalog, anchors_document = _metadata_documents(catalog_raw, anchors_raw)
    row = _catalog_channel(catalog, channel)
    _verify_digest(anchors_raw, row["anchor_table_sha256"], "anchors")
    seed_raw = _installed_seed_bytes(tracker)
    _verify_digest(seed_raw, row["seed_lock_sha256"], "seed lock")
    seed_lock = seed_lock_from_fields(parse_seed_lock_bytes(seed_raw))
    _validate_seed_lock(seed_lock, channel)
    provenance = cast(dict[str, object], seed_lock.provenance)
    contract = cast(dict[str, object], seed_lock.existing_install_contract)
    descriptor_digest = "sha256:" + hashlib.sha256(seed_raw).hexdigest()
    if descriptor_digest == contract["bundle_digest"]:
        raise ValueError("installed seed descriptor and transition contract identities are conflated")
    identity = ChannelInspectionIdentity(
        channel,
        seed_lock.repository,
        cast(str, seed_lock.release_tag),
        seed_lock.commit,
        seed_lock.tree_hash,
        seed_lock.profile,
        cast(str, provenance["provenance_sha256"]),
        cast(str, provenance["seed_id"]),
        cast(str, provenance["origin_id"]),
        cast(str, provenance["manifest_sha256"]),
        ExistingInstallContractIdentity(
            cast(str, contract["flow_id"]),
            cast(int, contract["flow_schema_version"]),
            cast(str, contract["bundle_digest"]),
        ),
        "released_metadata/existing_install_inspection_catalog.v1.json",
        hashlib.sha256(catalog_raw).hexdigest(),
        "share/solet/seed.lock.json",
        hashlib.sha256(seed_raw).hexdigest(),
        descriptor_digest,
        "released_metadata/existing_install_inspection_anchors.v1.json",
        hashlib.sha256(anchors_raw).hexdigest(),
    )
    metadata = InstalledInspectionMetadata(identity, seed_lock, _parse_anchors(anchors_document, channel))
    return InstalledUpdateDescriptor(metadata, seed_raw)


def descriptor_from_copy(paths: ManagerPaths, descriptor_digest: str) -> InstalledUpdateDescriptor:
    """Rebuild the installed metadata from the durable descriptor copy (Step 6 section 3.2, M2).

    Exactly what :func:`load_installed_update_descriptor` produces, except that
    the seed-lock bytes are proven against the *journaled* digest and the
    anchors against the copy receipt -- never against the installed catalog
    row, which a Manager upgrade may have moved to a different release.
    """
    from .contract_copies import read_descriptor_copy  # noqa: PLC0415 - avoids a paths<->metadata import cycle at module load

    seed_raw, anchors_document, receipt = read_descriptor_copy(paths, descriptor_digest)
    channel = cast(str, receipt["channel_id"])
    seed_lock = seed_lock_from_fields(parse_seed_lock_bytes(seed_raw))
    _validate_seed_lock(seed_lock, channel)
    provenance = cast(dict[str, object], seed_lock.provenance)
    contract = cast(dict[str, object], seed_lock.existing_install_contract)
    if descriptor_digest == contract["bundle_digest"]:
        raise ValueError("copied seed descriptor and transition contract identities are conflated")
    identity = ChannelInspectionIdentity(
        channel,
        seed_lock.repository,
        cast(str, seed_lock.release_tag),
        seed_lock.commit,
        seed_lock.tree_hash,
        seed_lock.profile,
        cast(str, provenance["provenance_sha256"]),
        cast(str, provenance["seed_id"]),
        cast(str, provenance["origin_id"]),
        cast(str, provenance["manifest_sha256"]),
        ExistingInstallContractIdentity(
            cast(str, contract["flow_id"]),
            cast(int, contract["flow_schema_version"]),
            cast(str, contract["bundle_digest"]),
        ),
        cast(str, receipt["catalog_path"]),
        cast(str, receipt["catalog_sha256"]),
        f"{COPY_SOURCE}:{descriptor_copy_dir(paths, descriptor_digest).name}",
        descriptor_digest.removeprefix("sha256:"),
        descriptor_digest,
        f"{COPY_SOURCE}:existing_install_inspection_anchors.v1.json",
        cast(str, receipt["anchors_sha256"]),
    )
    metadata = InstalledInspectionMetadata(identity, seed_lock, _parse_anchors(anchors_document, channel))
    return InstalledUpdateDescriptor(metadata, seed_raw)


def anchors_document(metadata: InstalledInspectionMetadata) -> dict[str, JsonValue]:
    """Serialise the parsed anchors back to the closed v1 table shape for the durable copy."""
    rows: list[JsonValue] = [
        {
            "anchor_id": anchor.anchor_id,
            "anchor_kind": anchor.anchor_kind.value,
            "channel_id": anchor.channel_id,
            "repository": anchor.repository,
            "commit": anchor.commit,
            "tree_hash": anchor.tree_hash,
            "provenance_sha256": anchor.provenance_sha256,
            "seed_id": anchor.seed_id,
            "origin_id": anchor.origin_id,
            "manifest_sha256": anchor.manifest_sha256,
            "channel_relation": anchor.channel_relation.value,
            "transition_paths": [],
        }
        for anchor in metadata.anchors
    ]
    return {"schema_version": 1, "anchors": rows}


def _released_metadata_bytes(tracker: InspectionEffectTracker) -> tuple[bytes, bytes]:
    return (
        _package_bytes("existing_install_inspection_catalog.v1.json", tracker),
        _package_bytes("existing_install_inspection_anchors.v1.json", tracker),
    )


def _package_bytes(name: str, tracker: InspectionEffectTracker) -> bytes:
    tracker.record_resource_read(f"released_metadata/{name}")
    resource = resources.files("solet_manager").joinpath("released_metadata", name)
    if not isinstance(resource, Path):
        raise ValueError("installed inspection metadata is not filesystem-backed")
    path = resource
    return _read_recorded_package_bytes(path)


def _metadata_documents(catalog_raw: bytes, anchors_raw: bytes) -> tuple[dict[str, object], object]:
    try:
        catalog_value: object = json.loads(catalog_raw)
        anchors_value: object = json.loads(anchors_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("installed inspection metadata is malformed") from exc
    if not isinstance(catalog_value, dict):
        raise ValueError("installed inspection catalog is malformed")
    catalog = cast(dict[str, object], catalog_value)
    valid_catalog = (
        frozenset(catalog) == frozenset({"schema_version", "channels"})
        and catalog.get("schema_version") == 1
        and isinstance(catalog["channels"], list)
    )
    if not valid_catalog:
        raise ValueError("installed inspection catalog is malformed")
    return catalog, anchors_value


def _catalog_channel(catalog: dict[str, object], channel: str) -> dict[str, str]:
    channels = cast(list[object], catalog["channels"])
    rows = [
        cast(dict[str, object], item)
        for item in channels
        if isinstance(item, dict) and item.get("channel_id") == channel
    ]
    if len(rows) != 1:
        raise ValueError("inspection channel is not installed")
    row = rows[0]
    required = {"channel_id", "seed_lock_sha256", "anchor_table_sha256"}
    if set(row) != required or not all(isinstance(row[key], str) for key in required):
        raise ValueError("installed inspection catalog channel is malformed")
    return cast(dict[str, str], row)


def _verify_digest(value: bytes, expected: str, label: str) -> None:
    if hashlib.sha256(value).hexdigest() != expected:
        raise ValueError(f"installed inspection {label} digest mismatch")


def _installed_seed_bytes(tracker: InspectionEffectTracker) -> bytes:
    tracker.record_resource_read("installed_seed_lock")
    try:
        return _read_regular_bytes(Path(sys.prefix) / "share" / "solet" / "seed.lock.json")
    except OSError as exc:
        raise ValueError("installed seed lock is unavailable") from exc


def _validate_seed_lock(seed_lock: SeedLock, channel: str) -> None:
    fields = (
        seed_lock.channel_id == channel,
        seed_lock.provenance is not None,
        seed_lock.existing_install_contract is not None,
        seed_lock.release_tag is not None,
    )
    if not all(fields):
        raise ValueError("installed seed lock lacks v3 inspection identity")


def _read_regular_bytes(path: Path) -> bytes:
    """Read a package/install file only when its final entry is safely owned."""
    root = path.parent
    owner = _safe_directory_owner(root, root)
    return _read_owned_regular_bytes(path, root, owner)


def _read_recorded_package_bytes(path: Path) -> bytes:
    """Read one wheel-owned resource only after its RECORD entry validates it.

    ``importlib.resources`` may resolve an editable checkout.  Inspection is a
    released-manager operation, so its catalog and anchor bytes must instead
    come from one ordinary installed wheel whose ``RECORD`` owns the exact
    on-disk resource.  The RECORD digest is checked after descriptor-based
    reading; a swapped, symlinked, or group-writable package tree therefore
    cannot become an identity authority.
    """
    package_root, install_root = _installed_package_roots(path)
    owner = _safe_directory_owner(package_root, install_root)
    record_path = _wheel_record_path(install_root)
    record_raw = _read_owned_regular_bytes(record_path, install_root, owner)
    raw = _read_owned_regular_bytes(path, package_root, owner)
    _verify_record_entry(path, install_root, record_path, raw, record_raw)
    return raw


def _installed_package_roots(path: Path) -> tuple[Path, Path]:
    if path.parent.name != "released_metadata" or path.parent.parent.name != "solet_manager":
        raise ValueError("installed inspection metadata has an invalid package path")
    package_root = path.parent.parent
    return package_root, package_root.parent


def _wheel_record_path(install_root: Path) -> Path:
    candidates = tuple(sorted(install_root.glob("solet_cli-*.dist-info/RECORD")))
    if len(candidates) != 1:
        raise ValueError("installed inspection metadata requires exactly one wheel RECORD")
    return candidates[0]


def _safe_directory_owner(path: Path, install_root: Path) -> int:
    """Require one non-symlinked, non-group-writable installed resource tree."""
    try:
        relative = path.relative_to(install_root)
    except ValueError as exc:
        raise ValueError("installed inspection metadata escapes its install root") from exc
    directories = (install_root, *(install_root / part for part in relative.parts))
    owner: int | None = None
    for directory in directories:
        metadata = os.stat(directory, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("installed inspection metadata directory is unsafe")
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(
                "installed inspection metadata directory is writable by group or other"
            )
        if owner is None:
            owner = metadata.st_uid
        elif metadata.st_uid != owner:
            raise ValueError("installed inspection metadata owner mismatch")
    if owner is None:
        raise ValueError("installed inspection metadata install root is unavailable")
    return owner


def _read_owned_regular_bytes(path: Path, root: Path, owner: int) -> bytes:
    if _safe_directory_owner(path.parent, root) != owner:
        raise ValueError("inspection resource owner mismatch")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("inspection resource is not a regular file")
        if metadata.st_uid != owner:
            raise ValueError("inspection resource owner mismatch")
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("inspection resource mode is writable by group or other")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1_048_576):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _verify_record_entry(
    path: Path, install_root: Path, record_path: Path, raw: bytes, record_raw: bytes
) -> None:
    try:
        relative_path = path.relative_to(install_root).as_posix()
        record_relative_path = record_path.relative_to(install_root).as_posix()
    except ValueError as exc:
        raise ValueError("installed inspection metadata escapes wheel RECORD") from exc
    entries = _record_entries(record_raw, record_relative_path)
    entry = entries.get(relative_path)
    if entry is None:
        raise ValueError("installed inspection metadata is absent from wheel RECORD")
    digest, size = entry
    if size != len(raw) or not hmac.compare_digest(digest, hashlib.sha256(raw).digest()):
        raise ValueError("installed inspection metadata wheel RECORD mismatch")


def _record_entries(record_raw: bytes, record_path: str) -> dict[str, tuple[bytes, int]]:
    try:
        rows = csv.reader(record_raw.decode("utf-8").splitlines())
        entries: dict[str, tuple[bytes, int]] = {}
        for row in rows:
            entry = _record_entry(row, record_path)
            if entry is None:
                continue
            name, value = entry
            if name in entries:
                raise ValueError
            entries[name] = value
    except (UnicodeDecodeError, csv.Error, ValueError) as exc:
        raise ValueError("installed inspection wheel RECORD is malformed") from exc
    return entries


def _record_entry(row: list[str], record_path: str) -> tuple[str, tuple[bytes, int]] | None:
    if len(row) != 3:
        raise ValueError
    name, digest_text, size_text = row
    if not digest_text and not size_text:
        if name == record_path or _generated_bytecode_record(name):
            return None
        raise ValueError
    if not digest_text.startswith("sha256=") or not size_text.isdecimal():
        raise ValueError
    digest = _record_digest(digest_text.removeprefix("sha256="))
    return name, (digest, int(size_text))


def _generated_bytecode_record(name: str) -> bool:
    return name.startswith("solet_manager/__pycache__/") and name.endswith(".pyc")


def _record_digest(value: str) -> bytes:
    try:
        digest = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("installed inspection wheel RECORD is malformed") from exc
    if len(digest) != hashlib.sha256().digest_size:
        raise ValueError("installed inspection wheel RECORD is malformed")
    return digest


def _parse_anchors(value: object, channel: str) -> tuple[InspectionAnchor, ...]:
    if not isinstance(value, dict):
        raise ValueError("installed inspection anchors are malformed")
    document = cast(dict[str, object], value)
    valid_document = (
        set(document) == {"schema_version", "anchors"}
        and document["schema_version"] == 1
        and isinstance(document["anchors"], list)
    )
    if not valid_document:
        raise ValueError("installed inspection anchors are malformed")
    return tuple(
        _anchor_from_row(cast(dict[str, object], item), channel)
        for item in cast(list[object], document["anchors"])
        if isinstance(item, dict) and item.get("channel_id") == channel
    )


def _anchor_from_row(row: dict[str, object], channel: str) -> InspectionAnchor:
    required = {
        "anchor_id",
        "anchor_kind",
        "channel_id",
        "repository",
        "commit",
        "tree_hash",
        "provenance_sha256",
        "seed_id",
        "origin_id",
        "manifest_sha256",
        "channel_relation",
        "transition_paths",
    }
    if set(row) != required:
        raise ValueError("installed inspection anchor is malformed")
    try:
        kind = InspectionAnchorKind(cast(str, row["anchor_kind"]))
        relation = ChannelRelation(cast(str, row["channel_relation"]))
    except ValueError as exc:
        raise ValueError("installed inspection anchor is malformed") from exc
    return InspectionAnchor(
        cast(str, row["anchor_id"]),
        kind,
        channel,
        cast(str, row["repository"]),
        cast(str, row["commit"]),
        cast(str, row["tree_hash"]),
        cast(str | None, row["provenance_sha256"]),
        cast(str, row["seed_id"]),
        cast(str, row["origin_id"]),
        cast(str, row["manifest_sha256"]),
        relation,
        (),
    )
