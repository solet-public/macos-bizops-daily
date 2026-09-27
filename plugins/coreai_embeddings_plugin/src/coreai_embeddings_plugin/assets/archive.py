"""Canonical one-file USTAR distribution for the pinned Nomic Core AI asset."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from .acquisition import ASSET_DIRECTORY, FILE_PINS, LICENSE_PIN, NOTICE_BYTES, NOTICE_PIN, AssetCorruptError, FilePin, verify_source_files

_CHUNK_BYTES = 1024 * 1024


class WriteSink(Protocol):
    def write(self, data: bytes) -> int: ...


class Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


@dataclass
class HashSink:
    size_bytes: int = 0
    _digest: Digest = field(default_factory=hashlib.sha256)

    def write(self, data: bytes) -> int:
        self._digest.update(data)
        self.size_bytes += len(data)
        return len(data)

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


def _info(name: str, *, size: int = 0, directory: bool = False) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    member.size = size
    member.mode = 0o755 if directory else 0o644
    member.mtime = 0
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def stream_archive(model_path: Path, tokenizer_path: Path, sink: WriteSink) -> None:
    """Stream canonical archive bytes without creating a second local model."""
    verify_source_files(model_path, tokenizer_path)
    root = ASSET_DIRECTORY
    # tarfile's w| mode calls write only, despite its wider fileobj stub.
    with tarfile.open(fileobj=cast(BinaryIO, sink), mode="w|", format=tarfile.USTAR_FORMAT) as tar:
        tar.addfile(_info(f"{root}/", directory=True))
        tar.addfile(_info(f"{root}/model.aimodel/", directory=True))
        for pin in FILE_PINS:
            source = tokenizer_path if pin.relative_path == "tokenizer.json" else model_path / Path(pin.relative_path).name
            with source.open("rb") as stream:
                tar.addfile(_info(f"{root}/{pin.relative_path}", size=pin.size_bytes), stream)
        license_bytes = Path(__file__).with_name("LICENSE-2.0.txt").read_bytes()
        if len(license_bytes) != LICENSE_PIN.size_bytes or hashlib.sha256(license_bytes).hexdigest() != LICENSE_PIN.sha256:
            raise AssetCorruptError("build-time Apache license differs from pin")
        tar.addfile(_info(f"{root}/LICENSE-2.0.txt", size=len(license_bytes)), io.BytesIO(license_bytes))
        tar.addfile(_info(f"{root}/NOTICE.txt", size=len(NOTICE_BYTES)), io.BytesIO(NOTICE_BYTES))


def hash_archive(model_path: Path, tokenizer_path: Path) -> tuple[str, int]:
    sink = HashSink()
    stream_archive(model_path, tokenizer_path, sink)
    return sink.sha256, sink.size_bytes


def _validate_members(members: list[tarfile.TarInfo]) -> None:
    expected_names = [
        ASSET_DIRECTORY,
        f"{ASSET_DIRECTORY}/model.aimodel",
        *(f"{ASSET_DIRECTORY}/{pin.relative_path}" for pin in FILE_PINS),
        f"{ASSET_DIRECTORY}/LICENSE-2.0.txt",
        f"{ASSET_DIRECTORY}/NOTICE.txt",
    ]
    if [member.name.rstrip("/") for member in members] != expected_names:
        raise AssetCorruptError("archive member list or order differs from pin")
    pins = (None, None, *FILE_PINS, LICENSE_PIN, NOTICE_PIN)
    for member, pin in zip(members, pins, strict=True):
        is_directory = pin is None
        expected = (
            tarfile.DIRTYPE if is_directory else tarfile.REGTYPE,
            0 if is_directory else pin.size_bytes,
            0o755 if is_directory else 0o644,
            0,
            0,
            0,
        )
        observed = (member.type, member.size, member.mode & 0o7777, member.uid, member.gid, member.mtime)
        if observed != expected:
            raise AssetCorruptError(f"archive member metadata differs: {member.name}")


def _extract_file(tar: tarfile.TarFile, member: tarfile.TarInfo, pin: FilePin, destination: Path) -> None:
    source = tar.extractfile(member)
    if source is None:
        raise AssetCorruptError(f"archive member has no data: {member.name}")
    digest = hashlib.sha256()
    total = 0
    with destination.open("xb") as output:
        while block := source.read(_CHUNK_BYTES):
            total += len(block)
            if total > pin.size_bytes:
                raise AssetCorruptError(f"archive member exceeds bound: {member.name}")
            digest.update(block)
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if total != pin.size_bytes or digest.hexdigest() != pin.sha256:
        raise AssetCorruptError(f"archive member digest differs: {member.name}")


def extract_archive(archive_path: Path, staging: Path) -> None:
    """Extract only the eight exact canonical members into a private empty dir."""
    if tuple(staging.iterdir()):
        raise AssetCorruptError("asset extraction staging directory is not empty")
    try:
        with tarfile.open(archive_path, mode="r:") as tar:
            members = tar.getmembers()
            _validate_members(members)
            (staging / "model.aimodel").mkdir()
            for member, pin in zip(members[2:], (*FILE_PINS, LICENSE_PIN, NOTICE_PIN), strict=True):
                _extract_file(tar, member, pin, staging / pin.relative_path)
    except (OSError, tarfile.TarError) as exc:
        raise AssetCorruptError(f"cannot extract pinned archive: {exc}") from exc
