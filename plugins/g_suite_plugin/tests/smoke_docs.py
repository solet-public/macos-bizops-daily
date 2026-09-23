#!/usr/bin/env python3
"""Docs action smoke tests for g_suite_plugin (no pytest, no live Google).

Exercises the pure docs_actions functions against a faked Docs/Drive service
and a faked blob writer — no network, no credentials. Red-first: each check
asserts real behavior, so a regression in docs_actions fails here.

Exercises:
  1. create_document — title-only create; content triggers a batchUpdate insertText
  2. get_document    — walks paragraph.elements[].textRun.content into body_text,
                        skipping non-paragraph structural elements (e.g. tables)
  3. batch_update    — requests passthrough; replies returned; empty requests rejected
  4. export_document — default pdf mime, custom docx/txt mime, non-bytes rejected

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/g_suite_plugin/tests/smoke_docs.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))

from g_suite_plugin.docs_actions import (  # noqa: E402
    batch_update,
    create_document,
    export_document,
    get_document,
)

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


def _fake_docs(
    create_value: dict[str, Any] | None = None,
    get_value: dict[str, Any] | None = None,
    batch_value: dict[str, Any] | None = None,
) -> MagicMock:
    docs = MagicMock()
    documents = docs.documents.return_value
    documents.create.return_value.execute.return_value = create_value or {}
    documents.get.return_value.execute.return_value = get_value or {}
    documents.batchUpdate.return_value.execute.return_value = batch_value or {}
    return docs


def test_create_document_title_only() -> None:
    docs = _fake_docs(create_value={"documentId": "d1"})
    result = create_document(docs, {"title": "Plan"})
    _assert("returns document id", result["id"] == "d1")
    _assert("no batchUpdate for title-only create", not docs.documents.return_value.batchUpdate.called)


def test_create_document_with_content() -> None:
    docs = _fake_docs(create_value={"documentId": "d2"})
    create_document(docs, {"title": "Notes", "content": "Hello doc"})
    kwargs = docs.documents.return_value.batchUpdate.call_args.kwargs
    requests = kwargs["body"]["requests"]
    _assert("insertText request built", requests[0]["insertText"]["text"] == "Hello doc")
    _assert("inserted at index 1", requests[0]["insertText"]["location"]["index"] == 1)


def test_get_document_extracts_text() -> None:
    document = {
        "title": "Report",
        "body": {
            "content": [
                {"paragraph": {"elements": [{"textRun": {"content": "Hello "}}]}},
                # A non-paragraph structural element carrying its own top-level
                # "elements"/textRun (unrealistic Docs API shape, but it makes this
                # fixture actually discriminate: if the paragraph-skip guard were
                # removed, this bogus content would leak into body_text).
                {
                    "table": {"tableRows": []},
                    "elements": [{"textRun": {"content": "LEAKED-TABLE-TEXT "}}],
                },
                {"paragraph": {"elements": [{"textRun": {"content": "world\n"}}]}},
            ]
        },
    }
    docs = _fake_docs(get_value=document)
    result = get_document(docs, {"id": "d1"})
    _assert("title returned", result["title"] == "Report")
    _assert("body text concatenated, table skipped", result["body_text"] == "Hello world\n")


def test_get_document_requires_id() -> None:
    docs = _fake_docs()
    raised = False
    try:
        get_document(docs, {})
    except ValueError:
        raised = True
    _assert("missing id raises ValueError", raised)


def test_batch_update_passthrough() -> None:
    docs = _fake_docs(batch_value={"replies": [{"insertText": {}}]})
    requests = [{"insertText": {"location": {"index": 1}, "text": "x"}}]
    result = batch_update(docs, {"id": "d1", "requests": requests})
    _assert("replies returned", result["replies"] == [{"insertText": {}}])
    kwargs = docs.documents.return_value.batchUpdate.call_args.kwargs
    _assert("requests passed through", kwargs["body"]["requests"] == requests)


def test_batch_update_rejects_empty_requests() -> None:
    docs = _fake_docs()
    raised = False
    try:
        batch_update(docs, {"id": "d1", "requests": []})
    except ValueError:
        raised = True
    _assert("empty requests raises ValueError", raised)


def test_export_document_default_pdf() -> None:
    drive = MagicMock()
    drive.files.return_value.export_media.return_value.execute.return_value = b"%PDF-1.7"
    captured: dict[str, Any] = {}

    def writer(content: bytes, filename: str, mime_type: str) -> str:
        captured["mime"] = mime_type
        return "bl-doc-1"

    result = export_document(drive, {"id": "d1"}, writer)
    _assert("returns doc_blob_key", result["doc_blob_key"] == "bl-doc-1")
    _assert("default mime is application/pdf", captured.get("mime") == "application/pdf")


def test_export_document_docx() -> None:
    drive = MagicMock()
    drive.files.return_value.export_media.return_value.execute.return_value = b"PK\x03\x04"
    captured: dict[str, Any] = {}

    def writer(content: bytes, filename: str, mime_type: str) -> str:
        captured["mime"] = mime_type
        return "bl-doc-2"

    export_document(drive, {"id": "d1", "format": "docx"}, writer)
    _assert(
        "docx mime resolved",
        captured.get("mime")
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


def test_export_document_rejects_non_bytes() -> None:
    drive = MagicMock()
    drive.files.return_value.export_media.return_value.execute.return_value = None
    raised = False
    try:
        export_document(drive, {"id": "d1"}, _unused_writer)
    except ValueError:
        raised = True
    _assert("non-bytes export content raises ValueError", raised)


def _unused_writer(content: bytes, filename: str, mime_type: str) -> str:  # pragma: no cover
    raise AssertionError("blob writer should not be called on the rejection paths")


def main() -> int:
    print("\ng_suite_plugin Docs smoke tests")
    print("=" * 40)
    test_create_document_title_only()
    test_create_document_with_content()
    test_get_document_extracts_text()
    test_get_document_requires_id()
    test_batch_update_passthrough()
    test_batch_update_rejects_empty_requests()
    test_export_document_default_pdf()
    test_export_document_docx()
    test_export_document_rejects_non_bytes()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All Docs smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
