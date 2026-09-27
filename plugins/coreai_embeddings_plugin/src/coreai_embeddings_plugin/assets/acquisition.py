"""Pinned Nomic Core AI asset verification and explicit acquisition.

Runtime callers use :func:`verify_installed_asset`, which is read only. The
installer calls :func:`acquire_asset` with a reviewed distribution manifest.
One call makes one bounded network attempt; an interrupted call leaves only
its private partial files, which the next call resumes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self
from urllib.error import URLError
from urllib.request import Request, urlopen

MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
SOURCE_URL = "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5"
UPSTREAM_REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"
VARIANT = "coreai-fp16-buckets-v1"
LICENSE = "Apache-2.0"
ASSET_DIRECTORY = "nomic-embed-text-v1.5"
RELEASE_REPOSITORY = "solet-public/macos-bizops"
_CHUNK_BYTES = 1024 * 1024
_SOCKET_TIMEOUT_SECONDS = 30
_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z")


class AssetError(RuntimeError):
    """Base error for this pinned asset."""


class AssetManifestError(AssetError):
    """A distribution manifest differs from the pinned artifact."""


class AssetMissingError(AssetError):
    """An installed asset or one of its required files is absent."""


class AssetCorruptError(AssetError):
    """An installed or downloaded asset differs from its pinned bytes."""


class AssetDownloadError(AssetError):
    """A bounded download did not finish; a verified prefix may be resumed."""


@dataclass(frozen=True)
class FilePin:
    relative_path: str
    release_name: str
    sha256: str
    size_bytes: int


FILE_PINS: tuple[FilePin, ...] = (
    FilePin(
        "model.aimodel/main.mlirb",
        "nomic-v1.5-coreai-fp16-buckets-main.mlirb",
        "016241f8a4d80784503c1a2b871afe3edd664e2b4de7282c155ab9cea7192503",
        275879458,
    ),
    FilePin(
        "model.aimodel/main.hash",
        "nomic-v1.5-coreai-fp16-buckets-main.hash",
        "aa632a548cce790e5f2e9ce2cf4175a9466374c4d52a13be38154a1242b86508",
        32,
    ),
    FilePin(
        "model.aimodel/metadata.json",
        "nomic-v1.5-coreai-fp16-buckets-metadata.json",
        "07829eae4b49d1d36ab892492d186309f2deec02d4e4564bf0c32396680a04a5",
        252,
    ),
    FilePin(
        "tokenizer.json",
        "nomic-v1.5-tokenizer.json",
        "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
        711396,
    ),
)
ARCHIVE_PIN = FilePin(
    "archive",
    "nomic-embed-text-v1.5-coreai-fp16-buckets-63d1ab27ccf4.tar",
    "63d1ab27ccf4ce739aa085425ebac4fd15d7b85c62f9b74628175d1dac404c46",
    276613120,
)
NOTICE_BYTES = (
    "Nomic Embed Text v1.5 Core AI distribution\n"
    f"Original model and tokenizer: {SOURCE_URL}\n"
    f"Pinned upstream revision: {UPSTREAM_REVISION}\n"
    "Model and tokenizer license: Apache-2.0\n"
    "License text: https://www.apache.org/licenses/LICENSE-2.0\n"
    "This attribution notice was prepared for the Core AI conversion.\n"
    f"Tokenizer SHA-256: {FILE_PINS[-1].sha256}\n"
).encode()
NOTICE_PIN = FilePin("NOTICE.txt", "", hashlib.sha256(NOTICE_BYTES).hexdigest(), len(NOTICE_BYTES))
LICENSE_PIN = FilePin(
    "LICENSE-2.0.txt", "",
    "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    11358,
)


@dataclass(frozen=True)
class InstalledAsset:
    model_path: Path
    tokenizer_path: Path
    model_id: str = MODEL_ID
    upstream_revision: str = UPSTREAM_REVISION
    variant: str = VARIANT


@dataclass(frozen=True)
class DistributionManifest:
    """An exact release location whose content is checked against FILE_PINS."""

    release_tag: str

    def __post_init__(self) -> None:
        if _TAG_RE.fullmatch(self.release_tag) is None:
            raise AssetManifestError("release_tag is not a bounded GitHub tag")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Self:
        _validate_manifest_identity(raw)
        tag = raw["release_tag"]
        if not isinstance(tag, str) or _TAG_RE.fullmatch(tag) is None:
            raise AssetManifestError("release_tag is not a bounded GitHub tag")
        _validate_manifest_archive(raw["archive"])
        _validate_manifest_files(raw["files"])
        return cls(release_tag=tag)

    @classmethod
    def from_file(cls, path: Path) -> Self:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AssetManifestError(f"cannot read distribution manifest: {path}") from exc
        if not isinstance(raw, dict):
            raise AssetManifestError("distribution manifest must be an object")
        return cls.from_mapping(raw)

    def url_for_archive(self) -> str:
        return (
            f"https://github.com/{RELEASE_REPOSITORY}/releases/download/"
            f"{self.release_tag}/{ARCHIVE_PIN.release_name}"
        )


def _validate_manifest_identity(raw: Mapping[str, object]) -> None:
    expected_identity: dict[str, object] = {
        "schema_version": 1,
        "model_id": MODEL_ID,
        "upstream_revision": UPSTREAM_REVISION,
        "variant": VARIANT,
        "license": LICENSE,
        "source_url": SOURCE_URL,
        "release_repository": RELEASE_REPOSITORY,
    }
    if set(raw) != {*expected_identity, "release_tag", "archive", "files"}:
        raise AssetManifestError("distribution manifest fields differ from schema v1")
    for key, expected in expected_identity.items():
        if type(raw[key]) is not type(expected) or raw[key] != expected:
            raise AssetManifestError(f"distribution manifest {key} differs from pin")


def _validate_manifest_archive(archive: object) -> None:
    expected = {
        "release_name": ARCHIVE_PIN.release_name,
        "sha256": ARCHIVE_PIN.sha256,
        "size_bytes": ARCHIVE_PIN.size_bytes,
    }
    if not isinstance(archive, dict) or archive != expected:
        raise AssetManifestError("distribution archive differs from pin")


def _validate_manifest_files(files: object) -> None:
    pins = (*FILE_PINS, LICENSE_PIN, NOTICE_PIN)
    if not isinstance(files, dict) or set(files) != {pin.relative_path for pin in pins}:
        raise AssetManifestError("distribution manifest file set differs from pin")
    for pin in pins:
        expected = {"sha256": pin.sha256, "size_bytes": pin.size_bytes}
        if files[pin.relative_path] != expected:
            raise AssetManifestError(f"distribution manifest {pin.relative_path} differs from pin")


def _regular_file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise AssetMissingError(f"required asset file is missing: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise AssetCorruptError(f"required asset path is not a regular file: {path}")
    return info


def _directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise AssetMissingError(f"required asset directory is missing: {path}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise AssetCorruptError(f"required asset path is not a directory: {path}")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_CHUNK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _verify_file(path: Path, pin: FilePin) -> None:
    info = _regular_file(path)
    if info.st_size != pin.size_bytes:
        raise AssetCorruptError(f"asset size differs for {pin.relative_path}")
    if _digest(path) != pin.sha256:
        raise AssetCorruptError(f"asset SHA-256 differs for {pin.relative_path}")


def verify_source_files(model_path: Path, tokenizer_path: Path) -> InstalledAsset:
    """Verify existing files in place, without copying the measured prototype."""
    _directory(model_path)
    if {path.name for path in model_path.iterdir()} != {"main.mlirb", "main.hash", "metadata.json"}:
        raise AssetCorruptError("model.aimodel contains unexpected or missing members")
    for pin in FILE_PINS:
        path = tokenizer_path if pin.relative_path == "tokenizer.json" else model_path / Path(pin.relative_path).name
        _verify_file(path, pin)
    return InstalledAsset(model_path=model_path, tokenizer_path=tokenizer_path)


def verify_installed_asset(asset_root: Path) -> InstalledAsset:
    """Synchronous read-only runtime check; never fetch or repair here."""
    _directory(asset_root)
    asset_dir = asset_root / ASSET_DIRECTORY
    _directory(asset_dir)
    if {path.name for path in asset_dir.iterdir()} != {"model.aimodel", "tokenizer.json", "LICENSE-2.0.txt", "NOTICE.txt"}:
        raise AssetCorruptError("installed asset contains unexpected or missing members")
    _verify_file(asset_dir / "LICENSE-2.0.txt", LICENSE_PIN)
    _verify_file(asset_dir / "NOTICE.txt", NOTICE_PIN)
    return verify_source_files(asset_dir / "model.aimodel", asset_dir / "tokenizer.json")


class DownloadResponse(Protocol):
    status: int
    headers: Mapping[str, str]

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> object: ...


type UrlOpener = Callable[[Request, int], AbstractContextManager[DownloadResponse]]


def _default_opener(request: Request, timeout: int) -> AbstractContextManager[DownloadResponse]:
    return urlopen(request, timeout=timeout)  # type: ignore[return-value]


def _checked_partial_size(path: Path, pin: FilePin) -> int:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(info.st_mode):
        raise AssetCorruptError(f"partial asset path is not a regular file: {path}")
    if info.st_size > pin.size_bytes:
        raise AssetCorruptError(f"partial asset exceeds bound: {pin.relative_path}")
    return info.st_size


def _response_mode(response: DownloadResponse, offset: int, pin: FilePin) -> tuple[str, int]:
    if offset and response.status == 206:
        match = _CONTENT_RANGE_RE.fullmatch(response.headers.get("Content-Range", ""))
        if match is None or (int(match[1]), int(match[2]), int(match[3])) != (
            offset, pin.size_bytes - 1, pin.size_bytes,
        ):
            raise AssetDownloadError(f"invalid Content-Range for {pin.relative_path}")
        mode = "ab"
    elif response.status == 200:
        offset = 0
        mode = "wb"
    else:
        raise AssetDownloadError(f"HTTP {response.status} for {pin.relative_path}")
    declared = response.headers.get("Content-Length")
    if declared is not None and (not declared.isdecimal() or int(declared) != pin.size_bytes - offset):
        raise AssetDownloadError(f"invalid Content-Length for {pin.relative_path}")
    return mode, offset


def _stream_to_partial(response: DownloadResponse, partial: Path, mode: str, offset: int, pin: FilePin) -> int:
    total = offset
    with partial.open(mode) as stream:
        while block := response.read(_CHUNK_BYTES):
            total += len(block)
            if total > pin.size_bytes:
                raise AssetDownloadError(f"download exceeds bound for {pin.relative_path}")
            stream.write(block)
        stream.flush()
        os.fsync(stream.fileno())
    return total


def _download_one(url: str, partial: Path, pin: FilePin, opener: UrlOpener) -> None:
    offset = _checked_partial_size(partial, pin)
    if offset == pin.size_bytes:
        if _digest(partial) == pin.sha256:
            return
        partial.unlink()
        offset = 0
    request = Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
    try:
        with opener(request, _SOCKET_TIMEOUT_SECONDS) as response:
            mode, offset = _response_mode(response, offset, pin)
            total = _stream_to_partial(response, partial, mode, offset, pin)
    except (URLError, TimeoutError, OSError) as exc:
        raise AssetDownloadError(f"download interrupted for {pin.relative_path}: {exc}") from exc
    if total != pin.size_bytes:
        raise AssetDownloadError(f"download incomplete for {pin.relative_path}: {total}/{pin.size_bytes}")
    _verify_file(partial, pin)


@contextmanager
def _acquisition_lock(path: Path) -> Generator[None]:
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def acquire_asset(
    asset_root: Path,
    distribution_manifest: DistributionManifest,
    *,
    opener: UrlOpener = _default_opener,
) -> InstalledAsset:
    """Fetch one pinned archive, extract privately, then activate atomically.

    A preexisting valid asset is reused. A preexisting corrupt active asset is
    refused, so repair must be an explicit installer decision. Interrupted
    archive partial remains available for the next call's Range request.
    """
    if asset_root.is_symlink():
        raise AssetCorruptError(f"asset root is a symlink: {asset_root}")
    asset_root.mkdir(parents=True, exist_ok=True)
    _directory(asset_root)
    with _acquisition_lock(asset_root / f".{ASSET_DIRECTORY}.lock"):
        active = asset_root / ASSET_DIRECTORY
        if active.exists() or active.is_symlink():
            return verify_installed_asset(asset_root)
        archive_partial = asset_root / f".{ARCHIVE_PIN.release_name}.part"
        _download_one(distribution_manifest.url_for_archive(), archive_partial, ARCHIVE_PIN, opener)
        from .archive import extract_archive

        with tempfile.TemporaryDirectory(prefix=f".{ASSET_DIRECTORY}.staging-", dir=asset_root) as temporary:
            staging = Path(temporary)
            extract_archive(archive_partial, staging)
            verify_source_files(staging / "model.aimodel", staging / "tokenizer.json")
            if active.exists() or active.is_symlink():
                raise AssetCorruptError("active asset appeared during acquisition")
            os.rename(staging, active)
        archive_partial.unlink()
        return verify_installed_asset(asset_root)
