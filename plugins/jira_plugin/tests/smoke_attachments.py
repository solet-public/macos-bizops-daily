#!/usr/bin/env python3
"""JIR-D attachment blob-bridge smoke tests (no pytest, no live Jira).

Hermetic — a faked JIRA client (Attachment resource with .raw + .get()) and a
fake blob writer / attachment loader. Red-first: each check asserts real
blob-bridge behavior + the blob-only ingest invariant (§2.1).

Exercises:
  1. download_attachment — attachment(id).get() bytes -> blob writer -> blob_key + meta
  2. download_attachment — non-bytes content raises ValueError
  3. download_attachment — missing attachment_id raises ValueError
  4. add_attachment      — loader(blob_key) bytes -> BytesIO upload; returns id+filename
  5. add_attachment      — filename override honored
  6. BLOB-ONLY (red-first) — the verb schema exposes NO path/file param, only blob_key
  7. BLOB-ONLY (red-first) — a path-only call (no blob_key) is REFUSED and the loader
     (the only byte source) is never invoked, so no local file is read

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/jira_plugin/tests/smoke_attachments.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "jira_plugin" / "src"))

from jira_plugin.attachment_actions import (  # noqa: E402
    OutgoingAttachment,
    add_attachment,
    download_attachment,
)
from jira_plugin.plugin import JiraPlugin  # noqa: E402

_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _unused_loader(blob_key: str) -> OutgoingAttachment:  # pragma: no cover
    raise AssertionError("attachment loader must not be called on the refusal / no-blob path")


def _unused_writer(content: bytes, filename: str, mime_type: str) -> str:  # pragma: no cover
    raise AssertionError("blob writer must not be called on the rejection path")


# ---------------------------------------------------------------------------
# download_attachment
# ---------------------------------------------------------------------------


def test_download_writes_blob() -> None:
    client = MagicMock()
    att = MagicMock()
    att.raw = {"filename": "log.txt", "mimeType": "text/plain", "size": 12}
    att.get.return_value = b"hello bytes!"
    client.attachment.return_value = att
    captured: dict[str, Any] = {}

    def writer(content: bytes, filename: str, mime_type: str) -> str:
        captured["content"] = content
        captured["filename"] = filename
        captured["mime"] = mime_type
        return "bl-att-1"

    result = download_attachment(client, {"attachment_id": "att-1"}, writer)
    _assert("returns attachment_blob_key", result["attachment_blob_key"] == "bl-att-1")
    _assert("filename from raw", result["filename"] == "log.txt")
    _assert("mime from raw", result["mime"] == "text/plain")
    _assert("size is actual byte length", result["size"] == len(b"hello bytes!"))
    _assert("writer got the bytes", captured.get("content") == b"hello bytes!")
    _assert("writer got the mime", captured.get("mime") == "text/plain")


def test_download_non_bytes_rejected() -> None:
    client = MagicMock()
    att = MagicMock()
    att.raw = {"filename": "x", "mimeType": "text/plain"}
    att.get.return_value = "not bytes"
    client.attachment.return_value = att
    raised = False
    try:
        download_attachment(client, {"attachment_id": "att-1"}, _unused_writer)
    except ValueError:
        raised = True
    _assert("non-bytes content raises ValueError", raised)


def test_download_requires_id() -> None:
    raised = False
    try:
        download_attachment(MagicMock(), {}, _unused_writer)
    except ValueError:
        raised = True
    _assert("missing attachment_id raises ValueError", raised)


# ---------------------------------------------------------------------------
# add_attachment (blob-only ingest)
# ---------------------------------------------------------------------------


def test_add_attachment_from_blob() -> None:
    client = MagicMock()
    client.add_attachment.return_value.id = "att-99"

    def loader(blob_key: str) -> OutgoingAttachment:
        _assert("loader receives blob_key", blob_key == "blob-7")
        return OutgoingAttachment(filename="data.csv", mime_type="text/csv", content=b"x,y\n1,2\n")

    result = add_attachment(client, {"key": "EXAMPLE-1", "blob_key": "blob-7"}, loader)
    _assert("returns attachment_id", result["attachment_id"] == "att-99")
    _assert("returns filename from blob", result["filename"] == "data.csv")
    kwargs = client.add_attachment.call_args.kwargs
    _assert("issue passed", kwargs.get("issue") == "EXAMPLE-1")
    _assert("attachment is a BytesIO (not a path)", isinstance(kwargs.get("attachment"), BytesIO))
    _assert("BytesIO carries the loader bytes", kwargs["attachment"].getvalue() == b"x,y\n1,2\n")


def test_add_attachment_filename_override() -> None:
    client = MagicMock()
    client.add_attachment.return_value.id = "att-100"

    def loader(blob_key: str) -> OutgoingAttachment:
        return OutgoingAttachment(filename="original.bin", mime_type="application/octet-stream", content=b"\x00")

    result = add_attachment(client, {"key": "EXAMPLE-1", "blob_key": "b1", "filename": "renamed.bin"}, loader)
    _assert("filename override honored", result["filename"] == "renamed.bin")
    _assert("override passed to api", client.add_attachment.call_args.kwargs.get("filename") == "renamed.bin")


def test_add_attachment_schema_has_no_path_param() -> None:
    params = JiraPlugin.add_attachment._platform_process_metadata.parameters
    keys = set(params.keys())
    _assert("no 'path' param on the verb schema", "path" not in keys)
    _assert("no 'file' param on the verb schema", "file" not in keys)
    _assert("blob_key is the byte source param", "blob_key" in keys)
    _assert("schema is exactly key/blob_key/filename", keys == {"key", "blob_key", "filename"})


def test_add_attachment_path_only_refused_no_local_read() -> None:
    # RED-FIRST blob-only: a caller passing a local `path` and NO blob_key must be
    # refused (blob_key required), and the loader — the ONLY byte source — must
    # never fire, so no local file is ever read. To see red, add a path fallback
    # to add_attachment and this refusal disappears.
    client = MagicMock()
    raised = False
    try:
        add_attachment(client, {"key": "EXAMPLE-1", "path": "/etc/passwd"}, _unused_loader)
    except ValueError:
        raised = True
    _assert("path-only call refused (blob_key required)", raised)
    _assert("client.add_attachment never called", not client.add_attachment.called)


def main() -> int:
    print("\njira_plugin JIR-D attachment blob-bridge smoke tests")
    print("=" * 40)
    test_download_writes_blob()
    test_download_non_bytes_rejected()
    test_download_requires_id()
    test_add_attachment_from_blob()
    test_add_attachment_filename_override()
    test_add_attachment_schema_has_no_path_param()
    test_add_attachment_path_only_refused_no_local_read()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All JIR-D attachment smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
