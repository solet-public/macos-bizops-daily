#!/usr/bin/env python3
"""Smoke for the ``ingest_export`` EDGE verb (claude_ai_export plugin).

Mechanical clone of the chatgpt_export sibling adapted to the claude_ai
vendor seam triple. Replaces the retired ``route_integration_smoke.py``
+ ``upload_route_streaming_smoke.py`` tests for this plugin.

Cases (matches the dispatch brief):

1. ``file_path`` invocation — happy path
2. ``content_bytes`` invocation — happy path
3. XOR (both supplied) → ``both_path_and_bytes``
4. XOR (neither supplied) → ``neither_path_nor_bytes``
5. 100 MiB cap → ``payload_too_large``
6. Idempotent re-invocation returns distinct ids without raising

Plus defensive coverage: missing filename with bytes, invalid base64,
blob_storage_service envelope shape, ledger register raising.
"""

from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_SRC = (
    _REPO_ROOT
    / "plugins"
    / "claude_ai_export_session_source_plugin"
    / "src"
)
for _candidate in (_PLUGIN_SRC, _REPO_ROOT / "ananta" / "src"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from claude_ai_export_session_source_plugin.ingest_verb import (  # noqa: E402
    CONTENT_BYTES_CAP,
    ERR_BLOB_STORE_FAILED,
    ERR_BLOB_STORE_FROM_FILE_FAILED,
    ERR_BOTH_PATH_AND_BYTES,
    ERR_INVALID_BASE64,
    ERR_MISSING_FILENAME_WITH_BYTES,
    ERR_NEITHER_PATH_NOR_BYTES,
    ERR_PAYLOAD_TOO_LARGE,
    ERR_SOURCE_REGISTRATION_FAILED,
    IngestExportError,
    perform_ingest,
)


class _StubBlobService:
    def __init__(self) -> None:
        self.store_blob_calls: list[dict[str, Any]] = []
        self.store_blob_from_file_calls: list[dict[str, Any]] = []
        self._next_blob_id = 0

    def _mint(self) -> str:
        self._next_blob_id += 1
        return f"blob-stub-{self._next_blob_id:03d}"

    def store_blob(self, **kwargs: Any) -> dict[str, Any]:
        self.store_blob_calls.append(kwargs)
        return {"action_status": "completed", "data": {"blob_id": self._mint()}}

    def store_blob_from_file(self, **kwargs: Any) -> dict[str, Any]:
        self.store_blob_from_file_calls.append(kwargs)
        return {"action_status": "completed", "data": {"blob_id": self._mint()}}

    def resolve_blob_path(self, uri: str) -> str:
        """A2 content-bytes path: the verb resolves blob://<id> to a fs path."""
        return f"/tmp/resolved-{uri.removeprefix('blob://')}.zip"


class _StubLedgerService:
    def __init__(self) -> None:
        self.register_calls: list[dict[str, Any]] = []
        self.ingest_calls: list[dict[str, Any]] = []
        self._next_id = 0

    def _mint(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-stub-{self._next_id:03d}"

    def register_claude_ai_export_source(
        self, *, blob_id: str, account_label: str | None,
    ) -> dict[str, str]:
        # A2: claude_ai_export is PUSHED — register opens NO batch, returns
        # source_id only; the push (ingest_raw_chunk) owns + returns the batch.
        self.register_calls.append({"blob_id": blob_id, "account_label": account_label})
        return {"source_id": self._mint("src")}

    def ingest_raw_chunk(
        self, *, source_kind: str, chunk_text: str, source_id: str,
    ) -> dict[str, Any]:
        # A2: dispatch_pushed binds to source_id and surfaces the real batch_id.
        self.ingest_calls.append(
            {"source_kind": source_kind, "chunk_text": chunk_text, "source_id": source_id},
        )
        return {"events_persisted": 1, "batch_id": self._mint("bch")}


class _BadEnvelopeBlobService(_StubBlobService):
    def store_blob(self, **kwargs: Any) -> Any:
        self.store_blob_calls.append(kwargs)
        return "not-a-dict"

    def store_blob_from_file(self, **kwargs: Any) -> Any:
        self.store_blob_from_file_calls.append(kwargs)
        return "not-a-dict"


class _RaisingLedgerService(_StubLedgerService):
    def register_claude_ai_export_source(self, **kwargs: Any) -> dict[str, str]:
        del kwargs
        raise RuntimeError("postgres unavailable for test")


class IngestExportVerbSmokeTest(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self._tmp_root = Path(self._tmp_obj.name)

    def tearDown(self) -> None:
        self._tmp_obj.cleanup()

    def test_file_path_happy_path(self) -> None:
        zip_path = self._tmp_root / "export.zip"
        zip_path.write_bytes(b"PK\x03\x04stub")
        blob = _StubBlobService()
        ledger = _StubLedgerService()
        result = perform_ingest(
            blob_storage_service=blob,
            session_ledger_service=ledger,
            file_path=str(zip_path),
            content_bytes=None,
            filename=None,
            account_label=None,
        )
        self.assertIn("source_id", result)
        self.assertIn("batch_id", result)
        self.assertIn("blob_id", result)
        self.assertEqual(len(blob.store_blob_from_file_calls), 1)
        self.assertEqual(blob.store_blob_from_file_calls[0]["mime_type"], "application/zip")
        self.assertEqual(len(ledger.register_calls), 1)
        self.assertEqual(len(ledger.ingest_calls), 1)
        # A2: the push binds to the registered source and carries the fs path.
        self.assertEqual(ledger.ingest_calls[0]["source_id"], result["source_id"])
        self.assertEqual(ledger.ingest_calls[0]["chunk_text"], str(zip_path))

    def test_content_bytes_happy_path(self) -> None:
        raw = b"PK\x03\x04inline-content"
        encoded = base64.b64encode(raw).decode("ascii")
        blob = _StubBlobService()
        ledger = _StubLedgerService()
        result = perform_ingest(
            blob_storage_service=blob,
            session_ledger_service=ledger,
            file_path=None,
            content_bytes=encoded,
            filename="inline.zip",
            account_label="operator-x",
        )
        self.assertIn("source_id", result)
        self.assertIn("batch_id", result)
        self.assertIn("blob_id", result)
        self.assertEqual(blob.store_blob_calls[0]["content"], raw)
        self.assertEqual(blob.store_blob_calls[0]["metadata"]["filename"], "inline.zip")
        self.assertEqual(ledger.register_calls[0]["account_label"], "operator-x")

    def test_xor_both_supplied_raises(self) -> None:
        with self.assertRaises(IngestExportError) as ctx:
            perform_ingest(
                blob_storage_service=_StubBlobService(),
                session_ledger_service=_StubLedgerService(),
                file_path="/tmp/x.zip",
                content_bytes="AAAA",
                filename="x.zip",
                account_label=None,
            )
        self.assertEqual(ctx.exception.code, ERR_BOTH_PATH_AND_BYTES)

    def test_xor_neither_supplied_raises(self) -> None:
        with self.assertRaises(IngestExportError) as ctx:
            perform_ingest(
                blob_storage_service=_StubBlobService(),
                session_ledger_service=_StubLedgerService(),
                file_path=None,
                content_bytes=None,
                filename=None,
                account_label=None,
            )
        self.assertEqual(ctx.exception.code, ERR_NEITHER_PATH_NOR_BYTES)

    def test_payload_too_large_raises(self) -> None:
        oversized = b"\x00" * (CONTENT_BYTES_CAP + 1)
        encoded = base64.b64encode(oversized).decode("ascii")
        with self.assertRaises(IngestExportError) as ctx:
            perform_ingest(
                blob_storage_service=_StubBlobService(),
                session_ledger_service=_StubLedgerService(),
                file_path=None,
                content_bytes=encoded,
                filename="too-big.zip",
                account_label=None,
            )
        self.assertEqual(ctx.exception.code, ERR_PAYLOAD_TOO_LARGE)

    def test_idempotent_reinvocation_returns_fresh_ids_per_call(self) -> None:
        zip_path = self._tmp_root / "export.zip"
        zip_path.write_bytes(b"PK\x03\x04stub")
        blob = _StubBlobService()
        ledger = _StubLedgerService()
        first = perform_ingest(
            blob_storage_service=blob,
            session_ledger_service=ledger,
            file_path=str(zip_path),
            content_bytes=None,
            filename="same-name.zip",
            account_label="op-1",
        )
        second = perform_ingest(
            blob_storage_service=blob,
            session_ledger_service=ledger,
            file_path=str(zip_path),
            content_bytes=None,
            filename="same-name.zip",
            account_label="op-1",
        )
        self.assertNotEqual(first["blob_id"], second["blob_id"])
        self.assertNotEqual(first["source_id"], second["source_id"])
        self.assertNotEqual(first["batch_id"], second["batch_id"])

    def test_missing_filename_with_bytes_raises(self) -> None:
        encoded = base64.b64encode(b"ok").decode("ascii")
        with self.assertRaises(IngestExportError) as ctx:
            perform_ingest(
                blob_storage_service=_StubBlobService(),
                session_ledger_service=_StubLedgerService(),
                file_path=None,
                content_bytes=encoded,
                filename=None,
                account_label=None,
            )
        self.assertEqual(ctx.exception.code, ERR_MISSING_FILENAME_WITH_BYTES)

    def test_invalid_base64_raises(self) -> None:
        with self.assertRaises(IngestExportError) as ctx:
            perform_ingest(
                blob_storage_service=_StubBlobService(),
                session_ledger_service=_StubLedgerService(),
                file_path=None,
                content_bytes="not-valid-base64!@#",
                filename="x.zip",
                account_label=None,
            )
        self.assertEqual(ctx.exception.code, ERR_INVALID_BASE64)

    def test_blob_store_failed_surfaces_structured_error(self) -> None:
        encoded = base64.b64encode(b"ok").decode("ascii")
        with self.assertRaises(IngestExportError) as ctx:
            perform_ingest(
                blob_storage_service=_BadEnvelopeBlobService(),
                session_ledger_service=_StubLedgerService(),
                file_path=None,
                content_bytes=encoded,
                filename="x.zip",
                account_label=None,
            )
        self.assertEqual(ctx.exception.code, ERR_BLOB_STORE_FAILED)

    def test_blob_store_from_file_failed_surfaces_structured_error(self) -> None:
        zip_path = self._tmp_root / "export.zip"
        zip_path.write_bytes(b"PK\x03\x04stub")
        with self.assertRaises(IngestExportError) as ctx:
            perform_ingest(
                blob_storage_service=_BadEnvelopeBlobService(),
                session_ledger_service=_StubLedgerService(),
                file_path=str(zip_path),
                content_bytes=None,
                filename=None,
                account_label=None,
            )
        self.assertEqual(ctx.exception.code, ERR_BLOB_STORE_FROM_FILE_FAILED)

    def test_source_registration_failed_surfaces_structured_error(self) -> None:
        encoded = base64.b64encode(b"ok").decode("ascii")
        with self.assertRaises(IngestExportError) as ctx:
            perform_ingest(
                blob_storage_service=_StubBlobService(),
                session_ledger_service=_RaisingLedgerService(),
                file_path=None,
                content_bytes=encoded,
                filename="x.zip",
                account_label=None,
            )
        self.assertEqual(ctx.exception.code, ERR_SOURCE_REGISTRATION_FAILED)


if __name__ == "__main__":
    unittest.main()
