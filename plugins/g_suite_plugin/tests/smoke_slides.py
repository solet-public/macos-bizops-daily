#!/usr/bin/env python3
"""Slides action smoke tests for g_suite_plugin (no pytest, no live Google).

Exercises the pure slides_actions functions against a faked Slides/Drive
service and a faked blob writer — no network, no credentials. Red-first: each
check asserts real behavior, so a regression in slides_actions fails here.

Exercises:
  1. create_presentation — returns id, title passed through
  2. get_presentation    — slide row shape (object_id + element_count) + count
  3. batch_update        — requests passthrough; replies returned; empty requests rejected
  4. export_presentation — default pdf mime, custom pptx mime, non-bytes rejected

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/g_suite_plugin/tests/smoke_slides.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))

from g_suite_plugin.slides_actions import (  # noqa: E402
    batch_update,
    create_presentation,
    export_presentation,
    get_presentation,
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


def _fake_slides(
    create_value: dict[str, Any] | None = None,
    get_value: dict[str, Any] | None = None,
    batch_value: dict[str, Any] | None = None,
) -> MagicMock:
    slides = MagicMock()
    presentations = slides.presentations.return_value
    presentations.create.return_value.execute.return_value = create_value or {}
    presentations.get.return_value.execute.return_value = get_value or {}
    presentations.batchUpdate.return_value.execute.return_value = batch_value or {}
    return slides


def test_create_presentation() -> None:
    slides = _fake_slides(create_value={"presentationId": "p1"})
    result = create_presentation(slides, {"title": "Deck"})
    _assert("returns presentation id", result["id"] == "p1")
    kwargs = slides.presentations.return_value.create.call_args.kwargs
    _assert("title passed through", kwargs["body"]["title"] == "Deck")


def test_get_presentation_shape() -> None:
    presentation = {
        "slides": [
            {"objectId": "s1", "pageElements": [{}, {}]},
            {"objectId": "s2", "pageElements": []},
        ]
    }
    slides = _fake_slides(get_value=presentation)
    result = get_presentation(slides, {"id": "p1"})
    _assert("count is 2", result["count"] == 2)
    _assert("object_id carried", result["slides"][0]["object_id"] == "s1")
    _assert("element_count computed", result["slides"][0]["element_count"] == 2)
    _assert("empty pageElements -> 0", result["slides"][1]["element_count"] == 0)


def test_get_presentation_requires_id() -> None:
    slides = _fake_slides()
    raised = False
    try:
        get_presentation(slides, {})
    except ValueError:
        raised = True
    _assert("missing id raises ValueError", raised)


def test_batch_update_passthrough() -> None:
    slides = _fake_slides(batch_value={"replies": [{"createSlide": {}}]})
    requests = [{"createSlide": {}}]
    result = batch_update(slides, {"id": "p1", "requests": requests})
    _assert("replies returned", result["replies"] == [{"createSlide": {}}])
    kwargs = slides.presentations.return_value.batchUpdate.call_args.kwargs
    _assert("requests passed through", kwargs["body"]["requests"] == requests)


def test_batch_update_rejects_empty_requests() -> None:
    slides = _fake_slides()
    raised = False
    try:
        batch_update(slides, {"id": "p1", "requests": []})
    except ValueError:
        raised = True
    _assert("empty requests raises ValueError", raised)


def test_export_presentation_default_pdf() -> None:
    drive = MagicMock()
    drive.files.return_value.export_media.return_value.execute.return_value = b"%PDF-1.7"
    captured: dict[str, Any] = {}

    def writer(content: bytes, filename: str, mime_type: str) -> str:
        captured["mime"] = mime_type
        return "bl-deck-1"

    result = export_presentation(drive, {"id": "p1"}, writer)
    _assert("returns deck_blob_key", result["deck_blob_key"] == "bl-deck-1")
    _assert("default mime is application/pdf", captured.get("mime") == "application/pdf")


def test_export_presentation_pptx() -> None:
    drive = MagicMock()
    drive.files.return_value.export_media.return_value.execute.return_value = b"PK\x03\x04"
    captured: dict[str, Any] = {}

    def writer(content: bytes, filename: str, mime_type: str) -> str:
        captured["mime"] = mime_type
        return "bl-deck-2"

    export_presentation(drive, {"id": "p1", "format": "pptx"}, writer)
    _assert(
        "pptx mime resolved",
        captured.get("mime")
        == "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


def test_export_presentation_rejects_non_bytes() -> None:
    drive = MagicMock()
    drive.files.return_value.export_media.return_value.execute.return_value = "not bytes"
    raised = False
    try:
        export_presentation(drive, {"id": "p1"}, _unused_writer)
    except ValueError:
        raised = True
    _assert("non-bytes export content raises ValueError", raised)


def _unused_writer(content: bytes, filename: str, mime_type: str) -> str:  # pragma: no cover
    raise AssertionError("blob writer should not be called on the rejection paths")


def main() -> int:
    print("\ng_suite_plugin Slides smoke tests")
    print("=" * 40)
    test_create_presentation()
    test_get_presentation_shape()
    test_get_presentation_requires_id()
    test_batch_update_passthrough()
    test_batch_update_rejects_empty_requests()
    test_export_presentation_default_pdf()
    test_export_presentation_pptx()
    test_export_presentation_rejects_non_bytes()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All Slides smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
