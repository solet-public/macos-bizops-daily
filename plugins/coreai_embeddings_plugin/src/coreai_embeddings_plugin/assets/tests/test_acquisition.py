"""Small real-file fixtures for the pinned asset acquisition boundary."""

from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from coreai_embeddings_plugin.assets import acquisition, archive


class _Response:
    def __init__(
        self,
        data: bytes,
        *,
        status: int,
        headers: Mapping[str, str],
        fail_after: int | None = None,
    ) -> None:
        self.status = status
        self.headers = headers
        self._data = data
        self._position = 0
        self._fail_after = fail_after

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self._fail_after is not None and self._position >= self._fail_after:
            raise OSError("injected interrupted transport")
        if size < 0:
            size = len(self._data)
        if self._fail_after is not None:
            size = min(size, self._fail_after - self._position)
        block = self._data[self._position : self._position + size]
        self._position += len(block)
        return block


class AcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bytes_by_path = {
            "model.aimodel/main.mlirb": b"graph-bytes-0123456789",
            "model.aimodel/main.hash": b"graph-hash-0123456789",
            "model.aimodel/metadata.json": b'{"license":"Apache-2.0"}',
            "tokenizer.json": b'{"tokens":["a","b"]}',
        }
        self.pins = tuple(
            acquisition.FilePin(
                pin.relative_path,
                pin.release_name,
                hashlib.sha256(self.bytes_by_path[pin.relative_path]).hexdigest(),
                len(self.bytes_by_path[pin.relative_path]),
            )
            for pin in acquisition.FILE_PINS
        )
        for module in (acquisition, archive):
            pin_patch = patch.object(module, "FILE_PINS", self.pins)
            pin_patch.start()
            self.addCleanup(pin_patch.stop)
        model = self.root / "source.aimodel"
        model.mkdir()
        tokenizer = self.root / "source-tokenizer.json"
        for relative_path, data in self.bytes_by_path.items():
            destination = tokenizer if relative_path == "tokenizer.json" else model / Path(relative_path).name
            destination.write_bytes(data)
        stream = io.BytesIO()
        archive.stream_archive(model, tokenizer, stream)
        self.archive_bytes = stream.getvalue()
        self.archive_pin = acquisition.FilePin(
            "archive",
            "fixture-nomic-model.tar",
            hashlib.sha256(self.archive_bytes).hexdigest(),
            len(self.archive_bytes),
        )
        archive_patch = patch.object(acquisition, "ARCHIVE_PIN", self.archive_pin)
        archive_patch.start()
        self.addCleanup(archive_patch.stop)
        self.manifest_data: dict[str, object] = {
            "schema_version": 1,
            "model_id": acquisition.MODEL_ID,
            "source_url": acquisition.SOURCE_URL,
            "upstream_revision": acquisition.UPSTREAM_REVISION,
            "variant": acquisition.VARIANT,
            "license": acquisition.LICENSE,
            "release_repository": acquisition.RELEASE_REPOSITORY,
            "release_tag": "fixture-model-v1",
            "archive": {
                "release_name": self.archive_pin.release_name,
                "sha256": self.archive_pin.sha256,
                "size_bytes": self.archive_pin.size_bytes,
            },
            "files": {
                pin.relative_path: {
                    "sha256": pin.sha256,
                    "size_bytes": pin.size_bytes,
                }
                for pin in (*self.pins, acquisition.LICENSE_PIN, acquisition.NOTICE_PIN)
            },
        }
        self.manifest = acquisition.DistributionManifest.from_mapping(self.manifest_data)
        self.requests: list[str | None] = []
        self.fail_first = False

    def opener(self, request: Request, timeout: int) -> _Response:
        self.assertEqual(timeout, 30)
        name = request.full_url.rsplit("/", 1)[1]
        self.assertEqual(name, self.archive_pin.release_name)
        range_header = request.get_header("Range")
        self.requests.append(range_header)
        data = self.archive_bytes
        if range_header is not None:
            offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
            return _Response(
                data[offset:],
                status=206,
                headers={
                    "Content-Length": str(len(data) - offset),
                    "Content-Range": f"bytes {offset}-{len(data) - 1}/{len(data)}",
                },
            )
        if self.fail_first:
            self.fail_first = False
            return _Response(data, status=200, headers={"Content-Length": str(len(data))}, fail_after=5)
        return _Response(data, status=200, headers={"Content-Length": str(len(data))})

    def test_interrupted_download_resumes_then_activates_atomically(self) -> None:
        self.fail_first = True
        with self.assertRaises(acquisition.AssetDownloadError):
            acquisition.acquire_asset(self.root, self.manifest, opener=self.opener)
        self.assertFalse((self.root / acquisition.ASSET_DIRECTORY).exists())
        partial = self.root / f".{self.archive_pin.release_name}.part"
        self.assertEqual(partial.read_bytes(), self.archive_bytes[:5])

        installed = acquisition.acquire_asset(self.root, self.manifest, opener=self.opener)
        self.assertEqual(installed.model_path.name, "model.aimodel")
        self.assertEqual(installed.tokenizer_path.read_bytes(), self.bytes_by_path["tokenizer.json"])
        self.assertIn("bytes=5-", self.requests)
        self.assertFalse(partial.exists())

        def forbidden_opener(request: Request, timeout: int) -> _Response:
            raise AssertionError("valid installed asset must not fetch")

        self.assertEqual(
            acquisition.acquire_asset(self.root, self.manifest, opener=forbidden_opener),
            installed,
        )

    def test_same_size_corruption_refuses_active_asset(self) -> None:
        installed = acquisition.acquire_asset(self.root, self.manifest, opener=self.opener)
        path = installed.tokenizer_path
        path.write_bytes(b"x" * path.stat().st_size)
        with self.assertRaises(acquisition.AssetCorruptError):
            acquisition.verify_installed_asset(self.root)
        with self.assertRaises(acquisition.AssetCorruptError):
            acquisition.acquire_asset(self.root, self.manifest, opener=self.opener)

    def test_license_copy_is_required_and_pinned(self) -> None:
        installed = acquisition.acquire_asset(self.root, self.manifest, opener=self.opener)
        license_path = installed.model_path.parent / "LICENSE-2.0.txt"
        self.assertEqual(hashlib.sha256(license_path.read_bytes()).hexdigest(), acquisition.LICENSE_PIN.sha256)
        license_path.write_bytes(b"x" * acquisition.LICENSE_PIN.size_bytes)
        with self.assertRaises(acquisition.AssetCorruptError):
            acquisition.verify_installed_asset(self.root)

    def test_manifest_cannot_change_pinned_archive_digest(self) -> None:
        archive_row = self.manifest_data["archive"]
        assert isinstance(archive_row, dict)
        archive_row["sha256"] = "0" * 64
        with self.assertRaises(acquisition.AssetManifestError):
            acquisition.DistributionManifest.from_mapping(self.manifest_data)

    def test_extra_archive_member_is_rejected(self) -> None:
        bad_archive = self.root / "extra.tar"
        bad_archive.write_bytes(self.archive_bytes)
        with tarfile.open(bad_archive, mode="a") as tar:
            extra = tarfile.TarInfo(f"{acquisition.ASSET_DIRECTORY}/extra.bin")
            extra.size = 1
            tar.addfile(extra, io.BytesIO(b"x"))
        staging = self.root / "staging"
        staging.mkdir()
        with self.assertRaises(acquisition.AssetCorruptError):
            archive.extract_archive(bad_archive, staging)

    def test_unexpected_model_member_is_corruption(self) -> None:
        installed = acquisition.acquire_asset(self.root, self.manifest, opener=self.opener)
        (installed.model_path / "extra.bin").write_bytes(b"extra")
        with self.assertRaises(acquisition.AssetCorruptError):
            acquisition.verify_installed_asset(self.root)


if __name__ == "__main__":
    unittest.main()
